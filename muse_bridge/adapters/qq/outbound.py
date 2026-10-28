"""Unified QQ Outbound message dispatcher.

The SOLE exit point for all outbound messages from Muse to QQ.
Unifies both interactive conversational replies and proactive notifications:
1. Listens for completed assistant events from the Muse event stream.
2. Extracts text, images (URLs & workspace), files (workspace & attachments), and videos.
3. Automatically reads file-store bytes via client.read_file() and downloads external URLs.
4. Uploads media to QQ files API (file_type: 1=image, 2=video, 4=file) and sends cards.
5. Resolves destination via RouteManager (replying to group/private or default proactive target).
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
from ...muse.client.render import (
    extract_files,
    extract_images,
    extract_workspace_files,
    extract_workspace_images,
    extract_workspace_videos,
)
from ...muse.service import MuseService
from .client import QQClient
from .route_manager import RouteInfo, RouteManager

_SANDBOX_MEDIA_RE = re.compile(r"!?\[([^\]]*)\]\(sandbox://([^)]+)\)")
_WIDGET_RE = re.compile(r"\[\[hatch_widget:[^\]]+\]\]")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def _clean_text(text: str) -> str:
    cleaned = _SANDBOX_MEDIA_RE.sub("", text)
    cleaned = _WIDGET_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_media_descriptors(event: dict, raw_text: str) -> Tuple[List[dict], List[dict], List[dict]]:
    images = []
    files = []
    videos = []

    # 1. From event payload
    images.extend(extract_images(event) + extract_workspace_images(event))
    files.extend(extract_files(event) + extract_workspace_files(event))
    videos.extend(extract_workspace_videos(event))

    # 2. From sandbox:// markdown links in text
    if raw_text:
        for alt, uri in _SANDBOX_MEDIA_RE.findall(raw_text):
            path = uri.strip().lstrip("/")
            base = path.lower().split("?", 1)[0]
            if base.endswith(_VIDEO_EXTS):
                videos.append({"workspace_path": path, "filename": alt or "video.mp4"})
            elif base.endswith(_IMAGE_EXTS):
                images.append({"workspace_path": path, "filename": alt or "image.jpg"})
            else:
                files.append({
                    "workspace_path": path,
                    "filename": alt or os.path.basename(path) or "file.dat",
                })

    def _dedup(items):
        seen = set()
        out = []
        for it in items:
            key = it.get("url") or it.get("workspace_path")
            if key and key not in seen:
                seen.add(key)
                out.append(it)
        return out

    return _dedup(images), _dedup(files), _dedup(videos)


def gate_catch_up(
    hist: List[dict],
    seen_ids: Set[str],
    last_ts: Optional[float],
    now: float,
    enabled: bool,
    window_s: int,
    max_messages: int,
) -> Tuple[Set[str], str, int, int, int, str]:
    """Startup catch-up gate."""
    all_assistant_ids = {
        e.get("message_id")
        for e in hist
        if e.get("role") == "assistant" and e.get("message_id")
    }
    max_seq = max([e.get("seq") or 0 for e in hist] or [0])

    if not enabled:
        reason = "catch-up disabled"
    elif last_ts is None:
        reason = "no last-run timestamp"
    elif window_s <= 0 or max_messages <= 0:
        reason = "catch-up window/max is 0"
    elif now - last_ts > window_s:
        reason = f"gap {now - last_ts:.0f}s exceeds window {window_s}s"
    else:
        reason = ""

    if reason:
        return seen_ids | all_assistant_ids, "baseline", 0, len(all_assistant_ids - seen_ids), max_seq, reason

    unseen = sorted(
        [
            e
            for e in hist
            if e.get("role") == "assistant"
            and e.get("message_id")
            and e.get("message_id") not in seen_ids
        ],
        key=lambda e: e.get("seq") or 0,
    )
    kept = unseen[-max_messages:]
    kept_ids = {e.get("message_id") for e in kept}
    skipped = {e.get("message_id") for e in unseen} - kept_ids

    return seen_ids | skipped, "catch_up", len(kept_ids), len(skipped), max_seq, ""


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
        self._last_ts: Optional[float] = None
        self._load_state()

    def _load_state(self) -> None:
        state_file = self.config.push.state_file
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_seq = int(data.get("last_seq", 0))
                    self._seen_ids = set(data.get("seen_ids", []))
                    self._last_ts = data.get("updated_ts")
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
                    {
                        "last_seq": self._last_seq,
                        "seen_ids": list(self._seen_ids)[-2000:],
                        "updated_ts": time.time(),
                    },
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
        startup_gated = False
        login_alerted = False

        while self._running:
            if not self.muse_service.token_manager.has_credentials():
                if not login_alerted:
                    login_alerted = True
                    print("[QQOutbound] Muse 尚未登录或 Cookies 不存在，暂停出站扫描（等待管理员在私聊发送 /login 进行登录认证）。")
                await asyncio.sleep(5.0)
                continue

            login_alerted = False
            try:
                def _fetch_history_and_media():
                    with self.muse_service.create_client() as client:
                        client.connect()
                        hist = client.get_history(self.session_id, 25)

                        # For candidate assistant events, pre-fetch any workspace media bytes
                        for ev in hist:
                            mid = ev.get("message_id")
                            if not mid or mid in self._seen_ids or self.deduplicator.is_delivered(mid):
                                continue
                            if ev.get("role") != "assistant":
                                continue
                            raw_t = ev.get("display_text") or ""
                            imgs, fls, vids = _extract_media_descriptors(ev, raw_t)
                            for it in imgs + fls + vids:
                                wpath = it.get("workspace_path")
                                if wpath and not it.get("data"):
                                    try:
                                        it["data"] = client.read_file(wpath)
                                    except Exception as e:
                                        if self.config.debug:
                                            print(f"[QQOutbound] Failed reading workspace media {wpath}: {e}")
                            ev["_extracted_images"] = imgs
                            ev["_extracted_files"] = fls
                            ev["_extracted_videos"] = vids
                        return hist

                hist = await loop.run_in_executor(None, _fetch_history_and_media)
                if hist:
                    if not startup_gated:
                        startup_gated = True
                        seen_ids, action, kept, skipped, max_seq, reason = gate_catch_up(
                            hist=hist,
                            seen_ids=self._seen_ids,
                            last_ts=self._last_ts,
                            now=time.time(),
                            enabled=self.config.push.catch_up_enabled,
                            window_s=self.config.push.catch_up_window_s,
                            max_messages=self.config.push.catch_up_max_messages,
                        )
                        self._seen_ids = seen_ids
                        if action == "baseline":
                            self._last_seq = max(self._last_seq, max_seq)
                            self._save_state()
                            if self.config.debug:
                                print(f"[QQOutbound] Startup baseline ({reason}): marked {skipped} messages seen, 0 pushed.")
                        else:
                            print(f"[QQOutbound] Startup catch-up: recovering up to {kept} recent message(s) (skipped {skipped} older).")

                    await self._scan_and_dispatch(hist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = str(e).lower()
                if any(k in err_msg for k in ("credential", "cookie", "handshake", "unauthorized", "401")):
                    if not login_alerted:
                        login_alerted = True
                        print(f"[QQOutbound] Muse 凭据失效 ({e})，暂停出站扫描，等待重新登录...")
                    await asyncio.sleep(10.0)
                    continue
                if self.config.debug:
                    print(f"[QQOutbound] Scan tick error: {e}")
                await asyncio.sleep(3.0)
                continue
            await asyncio.sleep(1.0)

    async def _scan_and_dispatch(self, hist: List[dict]) -> None:
        ordered = sorted(hist, key=lambda e: e.get("seq") or 0)
        last_user_mid: Optional[str] = None

        for event in ordered:
            role = event.get("role")
            mid = event.get("message_id")
            seq = event.get("seq", 0)

            if role == "user":
                if last_user_mid and last_user_mid != mid:
                    self.route_manager.finish_turn(last_user_mid)
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

            # Assistant message or presentation event
            payload = event.get("payload") or {}
            event_name = event.get("event_name")
            kind = payload.get("kind")
            data_field = payload.get("data") or {}

            # Check if this is an assistant message or presentation
            if event_name not in ("message.assistant", "delta.presentation"):
                continue

            # Ignore pure web UI widget cards (e.g. HTML timer cards) that have no image/file media
            is_media_presentation = (
                event_name == "delta.presentation"
                and (
                    kind in ("file", "image", "video")
                    or "file" in data_field
                    or "image_path" in data_field
                    or event.get("_extracted_images")
                    or event.get("_extracted_files")
                    or event.get("_extracted_videos")
                )
            )

            if event_name == "delta.presentation" and not is_media_presentation:
                self._seen_ids.add(mid)
                continue

            # For normal text messages, ensure generation is completed (never partial/streaming!)
            if event_name == "message.assistant":
                status = payload.get("status") or event.get("status")
                if status != "completed":
                    if self.config.debug:
                        print(f"[QQOutbound] Skip {mid}: generation in progress (status={status})")
                    continue

            raw_text = event.get("display_text") or ""
            clean_text = _clean_text(raw_text)

            images = event.get("_extracted_images") or []
            files = event.get("_extracted_files") or []
            videos = event.get("_extracted_videos") or []

            # If text simply mirrors the filename of an extracted file or image, suppress redundant text chunk
            if files and clean_text in [fl.get("filename") for fl in files if fl.get("filename")]:
                clean_text = ""
            if images and clean_text in [im.get("filename") for im in images if im.get("filename")]:
                clean_text = ""

            if not clean_text and not images and not files and not videos:
                continue

            # Route resolution: "回给谁？"
            meta = payload.get("metadata") or {}
            root_exec_id = meta.get("root_message_execution_id")

            route = self.route_manager.match_route(last_user_mid, root_exec_id)
            if route:
                target_type = route.target_type
                target_id = route.target_id
                reply_msg_id = route.msg_id
                route_type = "interactive_reply"
            else:
                # Proactive push: strictly to configured push target
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
                success = await self._send_to_qq(
                    target_type=target_type,
                    target_id=target_id,
                    text=clean_text,
                    images=images,
                    files=files,
                    videos=videos,
                    msg_id=reply_msg_id,
                )
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    print(f"[QQOutbound] Dispatched {mid} -> {target_type}:{target_id} [{route_type}] (imgs={len(images)}, files={len(files)}, vids={len(videos)})")
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
        files: List[dict],
        videos: List[dict],
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
                chunk,
                None,
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
            data = img.get("data")
            if not data and img.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        img["url"],
                        self.config.max_image_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download image {img['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_image_bytes:
                print(f"[QQOutbound] Image exceeds limit ({len(data)} > {self.config.max_image_bytes})")
                continue

            try:
                fname = img.get("filename") or "image.jpg"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    1,  # 1=image
                    fname,
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
                print(f"[QQOutbound] Failed to upload/send image: {e}")

        # 3. Send videos
        for vid in videos:
            data = vid.get("data")
            if not data and vid.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        vid["url"],
                        self.config.max_video_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download video {vid['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_video_upload_bytes:
                print(f"[QQOutbound] Video exceeds upload limit ({len(data)} > {self.config.max_video_upload_bytes})")
                continue

            try:
                fname = vid.get("filename") or "video.mp4"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    2,  # 2=video
                    fname,
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
                print(f"[QQOutbound] Failed to upload/send video: {e}")

        # 4. Send files
        for fl in files:
            data = fl.get("data")
            if not data and fl.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        fl["url"],
                        self.config.max_file_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download file {fl['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_file_bytes:
                print(f"[QQOutbound] File exceeds limit ({len(data)} > {self.config.max_file_bytes})")
                continue

            try:
                fname = fl.get("filename") or "file.dat"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    4,  # 4=file
                    fname,
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
                print(f"[QQOutbound] Failed to upload/send file: {e}")

        return True
