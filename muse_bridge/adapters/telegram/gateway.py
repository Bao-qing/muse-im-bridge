"""Single-owner Bot API long poller with durable intake checkpoints."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ...core.state_file import atomic_json_write
from ...observability import get_logger, safe_error
from .client import TelegramAPIError


logger = get_logger(__name__)
# Telegram holds updates for at most 24h and may randomize their IDs after a
# week of inactivity. Stop sending a stale offset before that ID reset.
_CURSOR_TTL = 2 * 86400


class TelegramPoller:
    def __init__(self, client, poll_timeout: int, cursor_path: Path, bot_id: int,
                 handle: Callable[[dict], Awaitable[str]]):
        self.client = client
        self.poll_timeout = poll_timeout
        self.cursor_path = cursor_path
        self.bot_id = bot_id
        self.handle = handle
        self._state = self._load()

    def _load(self) -> dict:
        try:
            with self.cursor_path.open(encoding="utf-8") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return {}
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("bot_id") != self.bot_id
                or type(state.get("saved_at")) is not int
                or any(state.get(k) is not None and (type(state[k]) is not int or state[k] <= 0)
                       for k in ("offset", "pending"))):
            raise ValueError("Telegram polling state is invalid or belongs to another bot")
        return state

    def _save(self, offset: int | None, pending: int | None = None):
        state = {"version": 1, "bot_id": self.bot_id, "saved_at": int(time.time()),
                 "offset": offset, "pending": pending}
        atomic_json_write(self.cursor_path, state)
        self._state = state

    async def poll_once(self) -> bool:
        if time.time() - self._state.get("saved_at", 0) > _CURSOR_TTL:
            self._state = {}
        offset = self._state.get("offset")
        pending = self._state.get("pending")
        for update in await self.client.updates(offset, self.poll_timeout):
            uid = update["update_id"]
            if offset is not None and uid < offset:
                continue
            if pending is not None and uid <= pending:
                # Processing may have reached Muse before the previous exit.
                # Keep the marker if an older duplicate somehow precedes it.
                offset = uid + 1
                if uid == pending:
                    logger.warning("Telegram update %s interrupted during intake; not replaying", uid)
                    pending = None
                self._save(offset, pending)
                continue
            # Save before handing a potentially non-idempotent update to the host.
            self._save(offset, uid)
            try:
                result = await self.handle(update)
            except Exception as exc:
                # A platform response could fail after a command or post ran.
                logger.warning("Telegram intake %s outcome unknown: %s", uid, safe_error(exc))
                result = "unknown"
            if result == "retryable":
                self._save(offset)
                return False
            if result not in {"handled", "ignored", "unknown"}:
                raise RuntimeError("Telegram intake returned an invalid status")
            offset, pending = uid + 1, None
            self._save(offset)
        return True

    async def run(self):
        delay = 1
        while True:
            try:
                progressed = await self.poll_once()
            except TelegramAPIError as exc:
                if exc.status in {401, 404, 409}:
                    raise RuntimeError("Telegram polling unavailable: check the Bot Token and make sure "
                                       "no webhook or other poller is active") from exc
                logger.warning("%s; polling will retry", exc)
                wait = max(delay, exc.retry_after)
            else:
                if progressed:
                    delay = 1
                    continue
                wait = delay
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)
