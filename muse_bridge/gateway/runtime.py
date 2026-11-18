"""Unified application gateway. Platform drivers mount here, never vice versa."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path

from ..config.models import ChatConfig, GatewayConfig
from ..core.identity import ChannelKey
from ..core.state_file import atomic_json_write
from ..muse.interactions import LOGIN_RELOGIN_NOTICE
from ..observability import get_logger, safe_error
from .chat import ChannelContext
from .feed import GatewayFeedCoordinator
from .inbound import InboundPipeline
from .muse_events import MuseEventPump

logger = get_logger(__name__)


class UnifiedGateway:
    def __init__(self, muse_service, config=None):
        self.muse_service = muse_service
        self.config = config or GatewayConfig()
        self.adapters = {}
        self.contexts = {}
        self.pipeline = InboundPipeline()
        self.feed = GatewayFeedCoordinator(self)
        self.events = None
        self._channel_locks = {}
        self._session_locks = {}
        self._contexts_ready = asyncio.Event()
        self._tasks = set()
        self._running = False
        self._stopping = False

    def mount(self, adapter, *, name=None):
        key = name or adapter.name
        if key in self.adapters:
            raise ValueError(f"Adapter mount already exists: {key}")
        paths = self._state_paths(adapter.config)
        for other in self.adapters.values():
            if paths & self._state_paths(other.config):
                raise ValueError("Mounted accounts cannot share mutable state files")
        self.adapters[key] = adapter
        adapter.mount(self)
        if adapter.account_identity():
            self.identity_changed(adapter)
        return adapter

    @staticmethod
    def _state_paths(config):
        paths = [getattr(config, name, None) for name in
                 ("routes_file", "session_map_file", "delivered_file", "push_state_file")]
        access = getattr(config, "access_requests", None)
        paths.append(getattr(access, "state_file", None))
        return {os.path.normcase(str(Path(path).resolve())) for path in paths if path}

    def _account_config(self, adapter):
        """Claim a legacy shard once; switching accounts never overwrites it."""
        config = adapter.config
        if not config.routes_file:
            return config
        path = Path(str(config.routes_file) + ".identity.local.json")
        identity = [adapter.name, adapter.account_identity()]
        if not path.exists():
            claim = identity
            if adapter.name == "onebot" and any(Path(value).is_file() for value in self._state_paths(config)):
                # A URL does not identify the logged-in account. Legacy OneBot
                # reference metadata can prove ownership; otherwise preserve the
                # old files and start a new account shard, never silently adopt.
                try:
                    legacy = json.loads(Path(str(config.routes_file) + ".references.local.json").read_text(encoding="utf-8"))
                    expected = f"onebot:{getattr(config, 'base_url', '')}:{adapter.account_identity()}"
                    verified = legacy.get("identity") == expected
                except (OSError, ValueError, AttributeError):
                    verified = False
                if not verified:
                    claim = [adapter.name, "legacy-unverified"]
                    logger.warning("Legacy OneBot state has no verified account owner; preserving it in a separate shard")
            atomic_json_write(path, {"version": 1, "identity": claim})
            if claim == identity:
                return config
        stored = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict) or not isinstance(stored.get("identity"), list):
            raise ValueError("Invalid account identity state")
        if stored["identity"] == identity:
            return config
        suffix = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
        changes = {name: str(value) + "." + suffix for name in
                   ("routes_file", "session_map_file", "delivered_file", "push_state_file")
                   if (value := getattr(config, name, None))}
        # Config seeds belong to the configured account, not a replacement login.
        changes["session_bindings"] = {}
        access = getattr(config, "access_requests", None)
        if access is not None:
            changes["access_requests"] = dataclasses.replace(access, state_file=str(access.state_file) + "." + suffix)
        return dataclasses.replace(config, **changes)

    def identity_changed(self, adapter):
        if adapter not in self.adapters.values():
            raise ValueError("Unregistered adapter cannot publish an identity")
        identity = adapter.account_identity()
        if not identity:
            return
        current = self.contexts.get(adapter)
        if current and current.account_identity() == identity:
            return
        if any(ctx.name == adapter.name and ctx.account_identity() == identity
               for other, ctx in self.contexts.items() if other is not adapter):
            raise ValueError("A platform account may only be mounted once")
        config = self._account_config(adapter)
        seed_main = (getattr(config, "seed_main_session", True) and config is adapter.config
                     and (current is None or current.account_identity() == identity))
        if current is not None and config is adapter.config:
            config = dataclasses.replace(config, session_bindings={})
        self.contexts[adapter] = ChannelContext(adapter, self, config=config, seed_main=seed_main)
        self._contexts_ready.set()

    def session_lock(self, sid):
        return self._session_locks.setdefault(sid, asyncio.Lock())

    async def accept(self, adapter, message):
        self.identity_changed(adapter)
        ctx = self.contexts.get(adapter)
        if ctx is None:
            return "retryable"
        if message.source.platform != adapter.name:
            raise ValueError("Inbound platform does not match the mounted driver")
        key = ChannelKey(ctx.identity, message.source.chat_type, message.source.target_id)
        lock = self._channel_locks.setdefault(key, asyncio.Lock())
        # Acquire before any await, download, classification, command or interaction.
        async with lock:
            if self.contexts.get(adapter) is not ctx:
                return "retryable"
            dedup_key = f"{key.kind}:{key.target}:{message.id}"
            if message.id and ctx.deduplicator.is_duplicate_gateway_msg(dedup_key):
                return "handled"
            sid = ctx.session_mapper.current(key.kind, key.target)
            if (not sid and ctx.seed_main and key.kind == "private" and isinstance(ctx.config, ChatConfig)
                    and ctx.can_manage(message.source.sender_id) and self.muse_service.config.session_id):
                sid = self.muse_service.config.session_id
                ctx.session_mapper.bind(key.kind, key.target, sid)
            try:
                if sid:
                    async with self.session_lock(sid):
                        result = await self.pipeline.handle_inbound(ctx, message)
                else:
                    result = await self.pipeline.handle_inbound(ctx, message)
            except asyncio.CancelledError:
                raise
            if result == "retryable":
                ctx.deduplicator._gateway_seen_ids.pop(dedup_key, None)
            return result

    def route_owner(self, sid, mid, root):
        # Causal origins outlive short-lived passive routes and current bindings.
        # A retired origin is a suppression tombstone, never an invitation to
        # broadcast old history to a different owner of the same Muse session.
        for ctx in self.contexts.values():
            if ctx.outbound_pusher.match_origin(sid, mid, root):
                return ctx
        for ctx in self.contexts.values():
            route = ctx.route_manager.match_route(mid, root) if ctx.owns_session(sid) else None
            if route and (route.target_type, route.target_id) in ctx.session_mapper.owners(sid):
                ctx.outbound_pusher.remember_origin(sid, mid, route.target_type, route.target_id, route.root_exec_id)
                return ctx
        return None

    def notification_owner(self, sid):
        return next((ctx for ctx in self.contexts.values() if ctx.owns_session(sid)), None)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def turn_started(self, ctx, sid):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for kind, target in ctx.session_mapper.owners(sid):
            ctx.adapter.activity_started(target, sid, kind=kind)

    async def turn_finished(self, ctx, sid):
        for kind, target in ctx.session_mapper.owners(sid):
            await ctx.adapter.activity(target, sid, "finished", kind=kind)

    async def notify_interaction(self, item, text):
        contexts = [ctx for ctx in self.contexts.values() if ctx.is_enabled]
        if item.kind == "approval" and item.context.get("background_approval"):
            return await self._notify_background(item, text, contexts)
        owners = [ctx for ctx in contexts if ctx.owns_session(item.session_id)]
        delivered = False
        for ctx in owners:
            delivered = await ctx.notify_interaction(item, text) or delivered
        return delivered

    async def _notify_background(self, item, text, contexts):
        contexts = [ctx for ctx in contexts if isinstance(ctx.config, ChatConfig)]
        configured = self.config.background_approval_push
        target = None
        ctx = None
        if configured:
            platform, target = configured.split(":", 1)
            ctx = next((c for c in contexts if c.name == platform and c.config.is_admin(target)
                        and target != "*"), None)
        else:
            saved = item.context.get("background_target")
            ctx = next((c for c in contexts if saved and (c.name, c.account_identity()) == tuple(saved[:2])
                        and c.config.is_admin(saved[2]) and saved[2] != "*"), None)
            target = saved[2] if ctx else None
            if ctx is None:
                candidates = [(c, recent) for c in contexts if (recent := c.latest_admin_private())]
                if candidates:
                    ctx, (target, _) = max(candidates, key=lambda entry: entry[1][1])
        if ctx is None:
            return False
        item.context["background_target"] = (ctx.name, ctx.account_identity(), target)
        return await ctx.notify_background_approval(item, text, target)

    async def login_required(self):
        for ctx in self.contexts.values():
            ctx.enter_login_mode()
        ctx = next((c for c in self.contexts.values() if c.name == self.config.login_through_adapter), None)
        if ctx and ctx.notification_target():
            try:
                await ctx.send_notice(ctx.notification_target(), "【系统提示】" + LOGIN_RELOGIN_NOTICE)
            except Exception as exc:
                logger.warning("Login notice delivery unconfirmed; gateway stays available. error=%s", safe_error(exc))

    async def _startup_login_notice(self):
        await self._contexts_ready.wait()
        # Drivers are started first; a transient verification failure with valid
        # cookies must not be presented as a request to log in again.
        await asyncio.sleep(0)
        await self.login_required()

    async def _run_events(self):
        await self._contexts_ready.wait()
        self.events = MuseEventPump(self)
        await self.events.start()

    async def start(self):
        self._running = True
        for adapter in self.adapters.values():
            if adapter.is_enabled:
                await adapter.prepare()
        try:
            try:
                ready = await asyncio.to_thread(self.muse_service.verify_connection)
            except Exception as exc:
                ready = False
                logger.warning("Muse connection pending; preserving cookies. error=%s", safe_error(exc))
            if ready:
                try:
                    await self.muse_service.interaction_protocol.reconcile()
                except Exception as exc:
                    logger.warning("Approval synchronization pending. error=%s", safe_error(exc))
            tasks = [self._spawn(a.start()) for a in self.adapters.values() if a.is_enabled]
            tasks += [self._spawn(self._run_events()), self._spawn(
                self.muse_service.interaction_protocol.run(self.notify_interaction, lambda: self._running))]
            if self.feed.is_enabled:
                tasks.append(self._spawn(self.feed.run()))
            if not ready and not await asyncio.to_thread(self.muse_service.token_manager.has_credentials):
                tasks.append(self._spawn(self._startup_login_notice()))
            await asyncio.gather(*tasks)
        finally:
            await self.stop()

    async def stop(self):
        # Cancelling a driver/start task re-enters finally:stop(). Never let that
        # task wait for the shutdown which is itself waiting for its cancellation.
        if self._stopping:
            return
        self._stopping = True
        self._running = False
        await self.feed.stop()
        if self.events is not None:
            await self.events.stop()
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        await asyncio.gather(*(task for task in self._tasks if task is not current), return_exceptions=True)
        await asyncio.gather(*(a.stop() for a in self.adapters.values() if a.is_enabled), return_exceptions=True)
        self.muse_service.interactions.close()
