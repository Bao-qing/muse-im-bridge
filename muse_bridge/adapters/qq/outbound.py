"""QQ's platform-only delivery port, including passive sequence ownership."""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from ...core.delivery import DeliveryEnvelope, DeliveryReceipt, DeliveryResult, DeliveryStatus
from ...core.message import chunk_text
from ...core.state_file import atomic_json_write


class QQSender:
    """Send resolved envelopes without consulting routes or registering associations.

    Passive counters contain the *next* sequence, keyed by native input msg_id.
    They are reserved durably before a send, including sends whose outcome is
    unknown. One sender instance must be shared by all sends for this account.
    """

    def __init__(self, config, client):
        self.config, self.client = config, client
        self._locks = {}
        self._passive_lock = asyncio.Lock()
        self._uncertain = {}
        routes = getattr(config, "routes_file", None)
        self.passive_state_file = Path(routes).with_suffix(".passive.local.json") if routes else None
        self._next_sequences = self._load_sequences(Path(routes) if routes else None)

    def _load_sequences(self, routes):
        sequences = {}
        if self.passive_state_file and self.passive_state_file.exists():
            with self.passive_state_file.open(encoding="utf-8") as handle:
                saved = json.load(handle)
            if (not isinstance(saved, dict) or saved.get("version") != 1
                    or not isinstance(saved.get("next_sequences"), dict)):
                raise ValueError("Invalid QQ passive sequence state")
            for mid, seq in saved["next_sequences"].items():
                if not isinstance(mid, str) or not mid or type(seq) is not int or seq < 1:
                    raise ValueError("Invalid QQ passive sequence state")
                sequences[mid] = seq
        # Import without constructing a RouteManager or modifying its file.
        if routes and routes.exists():
            with routes.open(encoding="utf-8") as handle:
                legacy = json.load(handle)
            if not isinstance(legacy, dict):
                raise ValueError("Invalid legacy QQ sequence state")
            for route in legacy.values():
                if not isinstance(route, dict):
                    continue
                mid, seq = route.get("msg_id"), route.get("seq", 1)
                if isinstance(mid, str) and mid and type(seq) is int and seq >= 1:
                    sequences[mid] = max(sequences.get(mid, 1), seq)
        return sequences

    async def _reserve_passive(self, envelope):
        if not envelope.passive_id:
            return None, None
        async with self._passive_lock:
            mid = envelope.passive_id
            seq = self._next_sequences.get(mid, 1)
            if seq > (4 if envelope.target_type == "private" else 5):
                return None, None
            updated = {**self._next_sequences, mid: seq + 1}
            if self.passive_state_file:
                atomic_json_write(self.passive_state_file, {"version": 1, "next_sequences": updated})
            self._next_sequences = updated
            return mid, seq

    @staticmethod
    def _response_status(code):
        if type(code) is int and 200 <= code < 300:
            return DeliveryStatus.CONFIRMED
        # Timeouts, server errors and malformed responses do not prove refusal.
        if type(code) is int and 400 <= code < 500 and code not in {408, 409, 425}:
            return DeliveryStatus.REJECTED
        return DeliveryStatus.UNKNOWN

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult:
        if not envelope.delivery_id or envelope.target_type not in {"private", "group"} or not envelope.target_id:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Invalid QQ delivery ID or target")
        key = (envelope.delivery_id, envelope.target_type, envelope.target_id)
        targets = {(envelope.target_type, envelope.target_id)}
        if envelope.target_type == "group" and envelope.fallback_target:
            targets.add(("private", envelope.fallback_target))
        async with AsyncExitStack() as stack:
            for target in sorted(targets):
                await stack.enter_async_context(self._locks.setdefault(target, asyncio.Lock()))
            if key in self._uncertain:
                return self._uncertain[key]
            # Cancellation while a worker thread is sending is also uncertain.
            self._uncertain[key] = DeliveryResult(DeliveryStatus.UNKNOWN, reason="QQ delivery interrupted")
            result = await self._deliver_locked(envelope)
            if result.status == DeliveryStatus.UNKNOWN:
                self._uncertain[key] = result
            else:
                self._uncertain.pop(key, None)
            return result

    async def _deliver_locked(self, envelope):
        receipts, failures = [], []
        active_used = {}
        reference = dict(envelope.quote.references) if envelope.quote else {}
        ref_id = reference.get("ref") or reference.get("id")
        quote_prefix = "" if ref_id else envelope.quote_fallback

        async def item(content=None, media=None):
            # QQ file_info is scoped to the recipient; prepare again on fallback.
            uploaded = {}

            async def attempt(kind, target, mid=None, seq=None, fallback=False):
                media_info = None
                if media:
                    if (kind, target) not in uploaded:
                        data, file_type, name = media
                        try:
                            uploaded[kind, target] = await asyncio.to_thread(
                                self.client.upload_media, kind, target, data, file_type, name)
                            if not uploaded[kind, target]:
                                raise ValueError("Missing file_info")
                        except Exception:
                            return DeliveryStatus.REJECTED, None, "QQ media preparation failed"
                    media_info = uploaded[kind, target]
                if not mid:
                    count = active_used.get((kind, target), 0)
                    if count >= self.config.max_active_chunks:
                        return DeliveryStatus.REJECTED, None, "QQ active quota exhausted"
                    active_used[kind, target] = count + 1
                body_text = quote_prefix + (content or "")
                if fallback and body_text:
                    body_text = "【群消息提醒】\n" + body_text
                markdown = body_text if content is not None and not envelope.plain_text else None
                options = ({"message_reference": {"message_id": ref_id, "ignore_get_message_error": False}}
                           if ref_id else {})
                try:
                    code, body = await asyncio.to_thread(
                        self.client.send_message, kind, target, body_text or None,
                        markdown, media_info, mid, seq, **options)
                except Exception:
                    return DeliveryStatus.UNKNOWN, None, "QQ send outcome unknown"
                status = self._response_status(code)
                return status, body, "" if status == DeliveryStatus.CONFIRMED else f"QQ send status {code}"

            try:
                mid, seq = await self._reserve_passive(envelope)
            except Exception:
                return DeliveryStatus.REJECTED, "Cannot reserve QQ passive sequence"
            kind, target = envelope.target_type, envelope.target_id
            status, body, reason = await attempt(kind, target, mid, seq)
            if status == DeliveryStatus.REJECTED and mid and reason.startswith("QQ send status"):
                status, body, reason = await attempt(kind, target)
            if (status == DeliveryStatus.REJECTED and reason.startswith("QQ send status")
                    and kind == "group" and envelope.fallback_target and not ref_id):
                kind, target = "private", envelope.fallback_target
                status, body, reason = await attempt(kind, target, fallback=True)
            if status == DeliveryStatus.CONFIRMED:
                receipts.append(DeliveryReceipt.from_body(kind, target, body))
            return status, reason

        async def send_item(*args, **kwargs):
            status, reason = await item(*args, **kwargs)
            if status != DeliveryStatus.CONFIRMED:
                failures.append(reason)
            return status

        for chunk in chunk_text(envelope.text, self.config.chunk_chars, self.config.max_chunks) if envelope.text else []:
            if await send_item(content=chunk) == DeliveryStatus.UNKNOWN:
                return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, failures[-1])

        for label, items, download_cap, upload_cap, file_type, default_name in (
                ("image", envelope.images, self.config.max_image_bytes, self.config.max_image_bytes, 1, "image.jpg"),
                ("video", envelope.videos, self.config.max_video_bytes, self.config.max_video_upload_bytes, 2, "video.mp4"),
                ("file", envelope.files, self.config.max_file_bytes, self.config.max_file_bytes, 4, "file.dat")):
            for descriptor in items:
                name = descriptor.get("filename") or default_name
                try:
                    data = descriptor.get("data")
                    if not data and descriptor.get("url"):
                        data = await asyncio.to_thread(self.client.download_bytes, descriptor["url"], download_cap)
                    if not isinstance(data, (bytes, bytearray)) or not data:
                        raise ValueError("Missing media data")
                except Exception as exc:
                    if "exceeded limit" in str(exc):
                        failures.append(f"QQ {label} exceeds upload limit (>{upload_cap} bytes)")
                    else:
                        failures.append(f"QQ {label} preparation failed: media data unavailable")
                    continue
                if len(data) > upload_cap:
                    # Size/limit only: never include file names, URLs or content.
                    failures.append(f"QQ {label} exceeds upload limit ({len(data)} > {upload_cap} bytes)")
                    continue
                if await send_item(media=(data, file_type, name)) == DeliveryStatus.UNKNOWN:
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, failures[-1])

        if failures:
            return DeliveryResult(DeliveryStatus.PARTIAL if receipts else DeliveryStatus.REJECTED,
                                  receipts, "; ".join(dict.fromkeys(failures)))
        if not receipts:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Empty QQ delivery")
        return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
