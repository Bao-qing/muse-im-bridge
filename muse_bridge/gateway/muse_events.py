"""One Muse subscription coordinator for all mounted platform accounts."""
from __future__ import annotations

import asyncio
import copy

from ..core.deduplicator import MessageDeduplicator
from ..observability import get_logger, safe_error
from .outbound import OutboundDispatcher

logger = get_logger(__name__)


class _SessionUnion:
    def __init__(self, gateway):
        self.gateway = gateway

    def session_ids(self):
        return list(dict.fromkeys(sid for ctx in self.gateway.contexts.values()
                                  for sid in ctx.session_mapper.session_ids()))

    def owners(self, sid):
        return [(ctx.identity, kind, target) for ctx in self.gateway.contexts.values()
                for kind, target in ctx.session_mapper.owners(sid)]


class MuseEventPump(OutboundDispatcher):
    """Reuse the protocol event loop, never a platform dispatcher or platform socket.

    One worker per mount serializes output. Slow platform I/O cannot block reads
    or another platform. Queue overflow forces resynchronization, not silent loss.
    """
    def __init__(self, gateway):
        self.gateway = gateway
        config = copy.copy(next(iter(gateway.contexts.values())).config)
        config.push_state_file = None
        config.routes_file = None
        config.reconnect_max_retries = max(c.config.reconnect_max_retries for c in gateway.contexts.values())
        super().__init__(config, None, gateway.muse_service, MessageDeduplicator(None), None,
                         _SessionUnion(gateway))
        self._queues = {}
        self._workers = {}

    async def start(self):
        try:
            await super().start()
        finally:
            for task in self._workers.values():
                task.cancel()
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
            self._workers.clear()

    async def _hydrate_and_dispatch(self, sid, hist, is_initial_gate):
        self._gated.add(sid)
        for ctx in list(self.gateway.contexts.values()):
            if not ctx.owns_session(sid):
                continue
            ctx.outbound_pusher._using_push_stream = True
            queue = self._queues.setdefault(ctx, asyncio.Queue(maxsize=64))
            if ctx not in self._workers:
                self._workers[ctx] = asyncio.create_task(self._worker(ctx, queue))
            queue.put_nowait((sid, hist))

    async def _worker(self, ctx, queue):
        while True:
            sid, hist = await queue.get()
            try:
                # Wait for post_message -> route registration, but never hold a
                # session submit lock during slow platform delivery.
                async with self.gateway.session_lock(sid):
                    valid = ctx in self.gateway.contexts.values() and ctx.owns_session(sid)
                if valid:
                    await ctx.outbound_pusher._hydrate_and_dispatch(
                        sid, hist, is_initial_gate=sid not in ctx.outbound_pusher._gated)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Gateway delivery failed; awaiting next authoritative sync. error=%s", safe_error(exc))
            finally:
                queue.task_done()

    async def on_turn_finished(self, sid):
        for ctx in list(self.gateway.contexts.values()):
            if ctx.owns_session(sid):
                await ctx.outbound_pusher.on_turn_finished(sid)

    async def on_turns_cleared(self):
        for ctx in list(self.gateway.contexts.values()):
            await ctx.outbound_pusher.on_turns_cleared()

    async def _notify_login_required(self):
        await self.gateway.login_required()

    def _is_event_already_handled(self, session_id, mid):
        owners = [ctx for ctx in self.gateway.contexts.values() if ctx.owns_session(session_id)]
        return not owners or all(ctx.outbound_pusher._is_event_already_handled(session_id, mid) for ctx in owners)

    def _media_upload_cap(self, session_id, kind):
        owners = [ctx for ctx in self.gateway.contexts.values() if ctx.owns_session(session_id)]
        attr = {"image": "max_image_bytes", "file": "max_file_bytes", "video": "max_video_upload_bytes"}[kind]
        return max((getattr(ctx.config, attr) for ctx in owners), default=getattr(self.config, attr))

    def account_identity(self):
        return "muse-event-pump"
