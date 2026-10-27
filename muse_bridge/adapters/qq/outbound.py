"""Unified QQ Outbound message dispatcher.

The SOLE exit point for all outbound messages from Muse to QQ.
Unifies both interactive conversational replies and proactive notifications:
1. Listens for completed assistant events from the Muse event stream.
2. Checks RouteManager using the preceding user message ID to determine destination.
   - If triggered by a QQ user message -> replies to the matching group/private chat.
   - If unsolicited (e.g. reminder, timer, web turn) -> pushes to the default target.
3. Completely decouples message sending from generation, eliminating chat blocking.
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
from .route_manager import RouteInfo, RouteManager

_SANDBOX_IMG_RE = re.compile(r"!\[([^\]]*)\]\(sandbox://[^)]+\)")
_WIDGET_RE = re.compile(r"\[\[hatch_widget:[^\]]+\]\]")


def _clean_text(text: str) -> str:
    cleaned = _SANDBOX_IMG_RE.sub(r"[图片：\1]" if r"\1" else "[图片]", text)
    cleaned = _WIDGET_RE.sub("", cleaned)
    return cleaned.strip()


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


class QQOutboundPusher:
    def __init__(
        self,
        config: QQConfig,
        client: QQClient,
        muse_service: MuseService,
        deduplicator: MessageDeduplicator,
        route_manager: RouteManager,
        session_id: str,
    ):
        self.config = config
        self.client = client
        self.muse_service = muse_service
        self.deduplicator = deduplicator
        self.route_manager = route_manager
        self.session_id = session_id

        self._running = False
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
        self._running = True
        default_type, default_id = self.config.resolve_push_target()
        print(f"[QQOutbound] Started unified outbound dispatcher (Default push target: {default_type}:{default_id})")

        while self._running:
            try:
                await self._run_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                if self.config.debug:
                    print(f"[QQOutbound] Polling error: {e}, retrying in 3s...")
                await asyncio.sleep(3.0)

    async def stop(self) -> None:
        self._running = False

    async def _run_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                def _fetch():
                    with self.muse_service.create_client() as client:
                        client.connect()
                        return client.get_history(self.session_id, 25)

                hist = await loop.run_in_executor(None, _fetch)
                if hist:
                    # Baseline on initial run if last_seq is 0
                    if self._last_seq == 0:
                        for item in hist:
                            mid = item.get("message_id")
                            if mid:
                                self._seen_ids.add(mid)
                            seq = item.get("seq", 0)
                            if seq > self._last_seq:
                                self._last_seq = seq
                        self._save_state()
                    else:
                        await self._scan_and_dispatch(hist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.config.debug:
                    print(f"[QQOutbound] Scan tick error: {e}")
            await asyncio.sleep(1.0)

    async def _scan_and_dispatch(self, hist: List[dict]) -> None:
        ordered = sorted(hist, key=lambda e: e.get("seq") or 0)
        last_user_mid: Optional[str] = None

        for event in ordered:
            role = event.get("role")
            mid = event.get("message_id")
            seq = event.get("seq", 0)

            if role == "user":
                last_user_mid = mid
                if mid:
                    self._seen_ids.add(mid)
                if seq > self._last_seq:
                    self._last_seq = seq
                continue

            if role != "assistant":
                continue

            if not mid or mid in self._seen_ids:
                continue

            if self.deduplicator.is_delivered(mid) or self.deduplicator.is_processing(mid):
                self._seen_ids.add(mid)
                continue

            # Assistant message
            payload = event.get("payload") or {}
            event_name = event.get("event_name")

            # Ignore web UI widget cards (e.g. HTML timer cards) that have no image/file media
            if event_name == "delta.presentation" and not payload.get("data", {}).get("image_path"):
                self._seen_ids.add(mid)
                continue

            # Only dispatch completed assistant messages (never partial/streaming!)
            status = payload.get("status") or event.get("status")
            if status != "completed":
                if self.config.debug:
                    print(f"[QQOutbound] Skip {mid}: generation in progress (status={status})")
                continue

            if not event.get("display_text_ready", True):
                continue

            raw_text = event.get("display_text") or ""
            clean_text = _clean_text(raw_text)
            images = _extract_images_from_payload(payload)

            if not clean_text and not images:
                continue

            # Route resolution: "回给谁？"
            route = self.route_manager.pop(last_user_mid)
            if route:
                target_type = route.target_type
                target_id = route.target_id
                reply_msg_id = route.msg_id
                route_type = "interactive_reply"
            else:
                # Proactive push: strictly to configured push target (private chat)
                target_type, target_id = self.config.resolve_push_target()
                reply_msg_id = None
                route_type = "proactive_push"

            if not target_id:
                if self.config.debug:
                    print(f"[QQOutbound] No target found for message {mid}, skipping.")
                continue

            # Mark processing
            self.deduplicator.mark_processing([mid])

            try:
                success = await self._send_to_qq(target_type, target_id, clean_text, images, msg_id=reply_msg_id)
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    print(f"[QQOutbound] Dispatched {mid} -> {target_type}:{target_id} [{route_type}]")
                else:
                    self.deduplicator.rollback_processing([mid])
            except Exception as e:
                self.deduplicator.rollback_processing([mid])
                print(f"[QQOutbound] Dispatch error for {mid}: {e}")

    async def _send_to_qq(
        self,
        target_type: str,
        target_id: str,
        text: str,
        images: List[dict],
        msg_id: Optional[str] = None,
    ) -> bool:
        loop = asyncio.get_running_loop()
        chunks = chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []
        passive_limit = 4 if target_type == "private" else 5
        active_budget = self.config.max_active_chunks
        seq = 1

        # 1. Send text chunks
        for chunk in chunks:
            use_passive = bool(msg_id and seq <= passive_limit)
            curr_msg_id = msg_id if use_passive else None
            curr_seq = seq if use_passive else None

            if not use_passive:
                if active_budget <= 0:
                    break
                active_budget -= 1

            code, body = await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                chunk,
                chunk,  # markdown content
                None,   # media file info
                curr_msg_id,
                curr_seq,
            )

            # Retry as active if passive rejected
            if use_passive and not (200 <= code < 300) and active_budget > 0:
                active_budget -= 1
                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    chunk,
                    chunk,
                )
            seq += 1

        # 2. Send images
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
                    1,
                    img.get("filename") or "image.jpg",
                )
                use_passive = bool(msg_id and seq <= passive_limit)
                curr_msg_id = msg_id if use_passive else None
                curr_seq = seq if use_passive else None

                if not use_passive:
                    if active_budget <= 0:
                        break
                    active_budget -= 1

                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    None,
                    None,
                    file_info,
                    curr_msg_id,
                    curr_seq,
                )
                seq += 1
            except Exception as e:
                print(f"[QQOutbound] Failed to send image: {e}")

        return True
