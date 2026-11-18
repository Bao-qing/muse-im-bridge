"""Telegram delivery. Native references are scoped to the chat, not global IDs."""
from __future__ import annotations

import asyncio
import re

from ...core.delivery import DeliveryEnvelope, DeliveryReceipt, DeliveryResult, DeliveryStatus, ReactionEnvelope
from .client import PHOTO_CAP, UPLOAD_CAP, TelegramAPIError


_ID = re.compile(r"-?[1-9][0-9]{0,15}")
_MESSAGE_ID = re.compile(r"[1-9][0-9]{0,15}")


def message_key(chat_id: int | str, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _reply_id(candidate: str | None, chat_id: str) -> int | None:
    if isinstance(candidate, str):
        chat, _, number = candidate.partition(":")
        if chat == chat_id and _MESSAGE_ID.fullmatch(number):
            return int(number)
    return None


class TelegramSender:
    def __init__(self, config, client):
        self.config, self.client = config, client
        self._locks = {}
        self._uncertain = {}

    @staticmethod
    def _valid_target(kind, target):
        return (kind in ("private", "group") and isinstance(target, str) and _ID.fullmatch(target)
                and (kind == "group") == target.startswith("-"))

    async def _guarded(self, operation, envelope, send):
        target = (envelope.target_type, envelope.target_id)
        key = (operation, *target, envelope.delivery_id)
        async with self._locks.setdefault(target, asyncio.Lock()):
            if key in self._uncertain:
                return self._uncertain[key]
            self._uncertain[key] = DeliveryResult(DeliveryStatus.UNKNOWN, reason="Telegram delivery interrupted")
            result = await send()
            if result.status == DeliveryStatus.UNKNOWN:
                self._uncertain[key] = result
            else:
                self._uncertain.pop(key, None)
            return result

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult:
        if not envelope.delivery_id or not self._valid_target(envelope.target_type, envelope.target_id):
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Invalid Telegram delivery target or ID")
        return await self._guarded("message", envelope, lambda: self._deliver_locked(envelope))

    async def deliver_reaction(self, envelope: ReactionEnvelope) -> DeliveryResult:
        ref = envelope.message_ref
        message_id = (_reply_id(ref[1], envelope.target_id)
                      if isinstance(ref, tuple) and len(ref) == 2 and ref[0] == "id" else None)
        reactions = envelope.reactions
        if (not envelope.delivery_id or not self._valid_target(envelope.target_type, envelope.target_id)
                or not message_id or not isinstance(reactions, tuple) or len(reactions) > 1
                or any(not isinstance(emoji, str) or not emoji.strip() for emoji in reactions)):
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Invalid Telegram reaction target or state")

        async def send():
            try:
                result = await self.client.call("setMessageReaction", {
                    "chat_id": int(envelope.target_id), "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji} for emoji in reactions],
                })
            except TelegramAPIError as exc:
                status = DeliveryStatus.REJECTED if exc.rejected else DeliveryStatus.UNKNOWN
                return DeliveryResult(status, reason=str(exc))
            if result is True:
                return DeliveryResult(DeliveryStatus.CONFIRMED)
            return DeliveryResult(DeliveryStatus.UNKNOWN, reason="Telegram reaction not confirmed")

        return await self._guarded("reaction", envelope, send)

    async def _deliver_locked(self, envelope):
        target = envelope.target_id
        ref = dict(envelope.quote.references).get("id") if envelope.quote else None
        # Routing IDs are not instructions to render a native quote.
        reply = _reply_id(ref, target)
        prefix = "" if reply else envelope.quote_fallback
        receipts = []

        def failure(reason, *, uncertain=False):
            status = (DeliveryStatus.UNKNOWN if uncertain else
                      DeliveryStatus.PARTIAL if receipts else DeliveryStatus.REJECTED)
            return DeliveryResult(status, receipts, reason)

        async def send(method, extra=None, files=None):
            params = {"chat_id": int(target), **(extra or {})}
            if reply and not receipts:
                params["reply_parameters"] = {"message_id": reply, "allow_sending_without_reply": True}
            try:
                try:
                    body = await self.client.call(method, params, files=files)
                except TelegramAPIError as exc:
                    if params.get("parse_mode") != "MarkdownV2" or not exc.rejected or not exc.invalid_markdown:
                        raise
                    # Telegram explicitly refused this segment before sending it.
                    # Retry the same text without formatting; never replay other segments.
                    params.pop("parse_mode")
                    body = await self.client.call(method, params, files=files)
            except TelegramAPIError as exc:
                return failure(str(exc), uncertain=not exc.rejected)
            mid = body.get("message_id") if isinstance(body, dict) else None
            chat = body.get("chat") if isinstance(body, dict) else None
            if type(mid) is not int or mid <= 0 or not isinstance(chat, dict) or chat.get("id") != int(target):
                return failure("Telegram delivery receipt not confirmed", uncertain=True)
            receipts.append(DeliveryReceipt(envelope.target_type, target, (("id", message_key(target, mid)),)))
            return None

        text = prefix + envelope.text
        use_markdown = not envelope.plain_text and not prefix
        limit = max(1, min(4096, self.config.chunk_chars))
        count = max(1, self.config.max_chunks)
        truncated = len(text) > limit * count
        if truncated:
            notice = "\n\n[内容已截断]"[:limit]
            text = text[:limit * count - len(notice)] + notice
        for start in range(0, len(text), limit):
            end = start + limit
            params = {"text": text[start:end]}
            if use_markdown and not (truncated and end >= len(text)):
                params["parse_mode"] = "MarkdownV2"
            error = await send("sendMessage", params)
            if error is not None:
                return error

        for label, field, method, media, cap in (
            ("image", "photo", "sendPhoto", envelope.images, min(PHOTO_CAP, self.config.max_image_bytes)),
            ("file", "document", "sendDocument", envelope.files, min(UPLOAD_CAP, self.config.max_file_bytes)),
            ("video", "video", "sendVideo", envelope.videos, min(UPLOAD_CAP, self.config.max_video_upload_bytes)),
        ):
            for descriptor in media:
                data = descriptor.get("data")
                if isinstance(data, (bytes, bytearray)) and len(data) > cap:
                    return failure(f"Telegram {label} exceeds upload limit ({len(data)} > {cap} bytes)")
                if not isinstance(data, (bytes, bytearray)) or not data:
                    return failure(f"Telegram {label} preparation failed (file bytes required)")
                name = descriptor.get("filename")
                if not isinstance(name, str) or not name.strip():
                    name = {"image": "image.jpg", "file": "file.bin", "video": "video.mp4"}[label]
                error = await send(method, files={field: (name, bytes(data))})
                if error is not None:
                    return error
        if not receipts:
            return failure("Empty Telegram delivery")
        return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
