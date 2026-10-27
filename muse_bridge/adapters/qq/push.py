"""QQ proactive push subsystem.

Subscribes to Muse live event streams, detects assistant messages
generated from web UI or external turns, and proactively pushes
them to the configured QQ target (defaults to the first whitelisted private chat).

Fully unified within QQAdapter, sharing the same QQClient and Deduplicator.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ...config.models import QQConfig
from ...core.deduplicator import MessageDeduplicator
from ...core.message import chunk_text
from ...muse.service import MuseService
from .client import QQClient

_SANDBOX_IMG_RE = re.compile(r"!\[([^\]]*)\]\(sandbox://[^)]+\)")


def _clean_sandbox_images(text: str) -> str:
    return _SANDBOX_IMG_RE.sub(r"[图片：\1]" if r"\1" else "[图片]", text)


def _extract_images_from_payload(payload: dict) -> List[dict]:
    parts = payload.get("parts") or []
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "image" and p.get("url"):
            out.append({
                "url": p["url"],
                "filename": p.get("filename") or "image.jpg",
                "content_type": p.get("mime_type") or "image/jpeg",
            })
    return out


class QQPushService:
    def __init__(
        self,
        config: QQConfig,
        client: QQClient,
        muse_service: MuseService,
        deduplicator: MessageDeduplicator,
        session_id: str,
    ):
        self.config = config
        self.client = client
        self.muse_service = muse_service
        self.deduplicator = deduplicator
        self.session_id = session_id

        self._running = False
        self._target_type, self._target_id = self.config.resolve_push_target()
        self._seen_ids: Set[str] = set()
        self._last_seq: int = 0
        self._load_state()

    def _load_state(self) -> None:
        state_file = self.config.push.state_file
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_seq = int(data.get("last_seq", 0))
                    self._seen_ids = set(data.get("seen_ids", []))
            except Exception:
                pass

    def _save_state(self) -> None:
        state_file = self.config.push.state_file
        if not state_file:
            return
        tmp = f"{state_file}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"last_seq": self._last_seq, "seen_ids": list(self._seen_ids)[-2000:]},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, state_file)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    async def start(self) -> None:
        if not self.config.push.enabled:
            return
        if not self._target_id:
            print("[QQPush] Warning: No push target found (no private whitelist or target_id). Push disabled.")
            return

        self._running = True
        print(f"[QQPush] Started proactive push service -> {self._target_type}:{self._target_id}")

        while self._running:
            try:
                await self._run_subscriber_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                if self.config.debug:
                    print(f"[QQPush] Subscription error: {e}, reconnecting in 5s...")
                await asyncio.sleep(self.config.push.reconnect_sleep_s)

    async def stop(self) -> None:
        self._running = False

    async def _run_subscriber_loop(self) -> None:
        loop = asyncio.get_running_loop()

        def _connect_and_subscribe():
            client = self.muse_service.create_client()
            client.connect()
            sub = client.subscribe(self.session_id, timeout=self.config.push.recheck_sleep_s)
            return client, sub

        client, sub = await loop.run_in_executor(None, _connect_and_subscribe)
        try:
            # Baseline existing history on startup if last_seq is 0
            if self._last_seq == 0:
                hist = await loop.run_in_executor(None, client.get_history, self.session_id, 40)
                for item in hist:
                    mid = item.get("message_id")
                    if mid:
                        self._seen_ids.add(mid)
                    seq = item.get("seq", 0)
                    if seq > self._last_seq:
                        self._last_seq = seq
                self._save_state()

            last_periodic = time.monotonic()
            while self._running:
                # Read chunks from push subscription
                events = []
                try:
                    events = await loop.run_in_executor(None, sub.read_events, 2.0)
                except Exception:
                    pass

                now = time.monotonic()
                if events or (now - last_periodic >= 5.0):
                    last_periodic = now
                    await self._scan_and_push(client)

                await asyncio.sleep(0.5)
        finally:
            try:
                client.close()
            except Exception:
                pass

    async def _scan_and_push(self, client: Any) -> None:
        loop = asyncio.get_running_loop()
        hist = await loop.run_in_executor(None, client.get_history, self.session_id, 20)
        if not hist:
            return

        for event in hist:
            role = event.get("role")
            mid = event.get("message_id")
            seq = event.get("seq", 0)

            if not mid or mid in self._seen_ids:
                continue

            # Skip user messages, but update seq
            if role == "user":
                self._seen_ids.add(mid)
                if seq > self._last_seq:
                    self._last_seq = seq
                continue

            if role != "assistant":
                continue

            # Assistant message: check deduplicator
            if self.deduplicator.is_delivered(mid) or self.deduplicator.is_processing(mid):
                self._seen_ids.add(mid)
                continue

            # Check if text is ready
            if not event.get("display_text_ready", True):
                continue

            raw_text = event.get("display_text") or ""
            clean_text = _clean_sandbox_images(raw_text)

            # Check attachments
            payload = event.get("payload") or {}
            images = _extract_images_from_payload(payload)

            # Mark processing
            self.deduplicator.mark_processing([mid])

            try:
                success = await self._push_to_qq(clean_text, images)
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    print(f"[QQPush] Proactively pushed message {mid} to {self._target_type}:{self._target_id}")
                else:
                    self.deduplicator.rollback_processing([mid])
            except Exception as e:
                self.deduplicator.rollback_processing([mid])
                print(f"[QQPush] Push failed for {mid}: {e}")

    async def _push_to_qq(self, text: str, images: List[dict]) -> bool:
        candidates = self.config.get_candidate_push_targets()
        if not candidates:
            return False

        for t_type, t_id in candidates:
            success = await self._push_to_target(t_type, t_id, text, images)
            if success:
                self._target_type = t_type
                self._target_id = t_id
                return True
        return False

    async def _push_to_target(self, target_type: str, target_id: str, text: str, images: List[dict]) -> bool:
        loop = asyncio.get_running_loop()
        chunks = chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []

        # 1. Push text chunks
        for chunk in chunks:
            code, body = await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                chunk,
                chunk,  # markdown_content
            )
            if not (200 <= code < 300):
                if self.config.debug:
                    print(f"[QQPush] Failed sending text to {target_type}:{target_id} ({code}): {body}")
                return False

        # 2. Push images
        for img in images:
            url = img.get("url")
            if not url:
                continue
            try:
                data = await loop.run_in_executor(
                    None,
                    self.client.download_bytes,
                    url,
                    self.config.max_image_bytes,
                )
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    1,  # image
                    img.get("filename") or "image.jpg",
                )
                code, body = await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    None,
                    None,
                    file_info,
                )
                if not (200 <= code < 300):
                    if self.config.debug:
                        print(f"[QQPush] Failed sending media to {target_type}:{target_id} ({code}): {body}")
                    return False
            except Exception as e:
                print(f"[QQPush] Failed to push image {url}: {e}")

        return True
