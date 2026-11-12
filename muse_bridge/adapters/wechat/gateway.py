"""One cancellable long poll per iLink account; commit cursor after ingestion."""
from __future__ import annotations

import asyncio
import time

from ...observability import get_logger, safe_error
from .client import SessionCoolingError

logger = get_logger(__name__)


class WeChatGateway:
    def __init__(self, client, state, on_batch, timeout_ms=35000):
        self.client, self.state, self.on_batch = client, state, on_batch
        self.timeout_ms = timeout_ms
        self._stop = asyncio.Event()
        self._task = None
        self._running = False
        self.phase = "stopped"
        self.last_success = None

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            pass

    async def start(self):
        if self._running:
            raise RuntimeError("WeChat poller is already running")
        self._running = True
        self._stop.clear()
        self._task = asyncio.current_task()
        failures = 0
        try:
            while not self._stop.is_set():
                remaining = self.state.cooldown_until - time.time()
                if remaining > 0:
                    self.phase = "cooling"
                    await self._wait(remaining)
                    continue
                try:
                    self.phase = "polling"
                    started_at = time.monotonic()
                    response = await self.client.get_updates(self.state.cursor, self.timeout_ms)
                    messages = response.get("msgs", [])
                    if not isinstance(messages, list) or len(messages) > 10000:
                        raise ValueError("Invalid WeChat update batch")
                    # The adapter awaits business intake; no detached create_task here.
                    await self.on_batch(messages)
                    cursor = response.get("get_updates_buf")
                    if cursor:
                        self.state.set_cursor(cursor)
                    suggested = response.get("longpolling_timeout_ms")
                    if type(suggested) is int and 1000 <= suggested <= 180000:
                        self.timeout_ms = suggested
                    self.last_success = time.time()
                    failures = 0
                    # A long-poll endpoint can still return immediately; don't busy-poll.
                    if not messages and time.monotonic() - started_at < 1:
                        await self._wait(1 - (time.monotonic() - started_at))
                except SessionCoolingError:
                    self.phase = "cooling"
                    # Client normally persists this before raising; don't spin on a bad callback.
                    await self._wait(max(2, self.state.cooldown_until - time.time()))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self.phase = "backoff"
                    logger.warning("WeChat polling/intake failed: %s", safe_error(exc))
                    await self._wait(30 if failures >= 3 else 2)
        finally:
            self._running = False
            self.phase = "stopped"
            self._task = None

    async def stop(self):
        self._stop.set()
        task = self._task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
