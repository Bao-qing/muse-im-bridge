"""Opt-in, bounded Muse feed -> Qzone publishing.

Reserve each post durably BEFORE sending. An uncertain send is never retried
by this worker: missing a post is preferable to duplicating a public post.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
from pathlib import Path

from ...observability import get_logger, safe_error

logger = get_logger(__name__)


class QzoneFeedWorker:
    def __init__(self, config, client, muse_service, *, clock=time.time,
                 max_image_bytes=10 * 1024 * 1024):
        self.config = config
        self.client = client
        self.muse_service = muse_service
        self.clock = clock
        self.max_image_bytes = max_image_bytes
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def stop(self):
        self._stop.set()

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def run(self):
        if not self.config.enabled:
            return
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Qzone feed scan failed: %s", safe_error(exc))
            if not await self._wait(self.config.poll_interval_s):
                break

    def _load(self, identity):
        path = Path(self.config.state_file)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "identity": identity, "attempts": {}}
        # Never reset bad or foreign state silently and flood a new account.
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("identity") != identity
                or not isinstance(state.get("attempts"), dict)):
            raise ValueError("Qzone feed state is corrupt or belongs to another account")
        for key, value in state["attempts"].items():
            if (not isinstance(key, str) or not key or type(value) not in (int, float)
                    or not math.isfinite(value) or value < 0):
                raise ValueError("Qzone feed state contains invalid attempts")
        return state

    def _save(self, state):
        path = Path(self.config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", delete=False) as out:
                name = out.name
                json.dump(state, out, ensure_ascii=False, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    async def scan_once(self):
        if not self.config.enabled or self._stop.is_set():
            return 0
        async with self._lock:
            account = await asyncio.to_thread(self.client.get_login_info)
            account_id = account.get("user_id") if isinstance(account, dict) else None
            if not account_id or not str(account_id).isdigit():
                raise ValueError("Cannot identify the Qzone publishing account")
            # A fresh Muse client per scan; never share the chat subscription socket.
            page = await asyncio.to_thread(self.muse_service.list_feed, self.config.scan_limit)
            if not page.source_id:
                raise ValueError("Cannot identify the Muse feed source")
            identity = {"onebot": str(account_id), "muse": page.source_id}
            state = await asyncio.to_thread(self._load, identity)
            attempts = state["attempts"]
            now = self.clock()
            retention = max(self.config.max_age_s, self.config.publish_window_s,
                            self.config.min_publish_interval_s)
            attempts = {key: ts for key, ts in attempts.items() if ts >= now - retention}
            state["attempts"] = attempts
            candidates = [post for post in page.posts
                          if post.id not in attempts
                          and 0 <= now - post.created_at <= self.config.max_age_s
                          and post.text.strip()]
            # Select newest bounded catch-up, then publish in chronological order.
            candidates.sort(key=lambda post: (post.created_at, post.id), reverse=True)
            candidates = list(reversed(candidates[:self.config.max_per_scan]))
            published = 0
            for post in candidates:
                if self._stop.is_set():
                    break
                # Duplicate IDs in a response must not create duplicate sends.
                if post.id in attempts:
                    continue
                now = self.clock()
                if not 0 <= now - post.created_at <= self.config.max_age_s:
                    continue
                recent = [ts for ts in attempts.values() if ts > now - self.config.publish_window_s]
                if len(recent) >= self.config.max_per_window:
                    break
                delay = max(0, max(attempts.values(), default=0)
                            + self.config.min_publish_interval_s - now)
                if delay and not await self._wait(delay):
                    break
                now = self.clock()
                if now - post.created_at > self.config.max_age_s:
                    continue
                if len(attempts) >= 10000:
                    raise ValueError("Qzone feed state capacity reached; refusing to discard deduplication")
                images = (await asyncio.to_thread(self.muse_service.prepare_feed_images,
                                                  post, self.max_image_bytes)
                          if post.images else [])
                if self._stop.is_set():
                    break
                now = self.clock()
                if now - post.created_at > self.config.max_age_s:
                    continue
                attempts[post.id] = now
                # Persistence failure MUST prevent this send.
                await asyncio.to_thread(self._save, state)
                if self._stop.is_set():
                    break
                try:
                    await asyncio.to_thread(self.client.send_qzone_msg, post.text,
                                            images=images, ugc_right=self.config.ugc_right)
                except Exception:
                    logger.warning("Qzone publishing failed or outcome unknown; reserved post will not be retried")
                    raise
                published += 1
                logger.info("Published Muse feed item to Qzone (%d this scan)", published)
            return published
