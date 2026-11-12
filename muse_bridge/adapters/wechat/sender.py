"""The single iLink send path, including local commands and notifications."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time

from ...core.message import chunk_text
from ...observability import get_logger, safe_error
from .client import WeChatAPIError, SessionCoolingError

logger = get_logger(__name__)


class WeChatSender:
    def __init__(self, client, state, config, media, context_lookup=None):
        self.client, self.state, self.config, self.media = client, state, config, media
        self.context_lookup = context_lookup or state.context
        self._locks = {}

    def chunks(self, text):
        if not text:
            return []
        limit = min(self.config.chunk_chars, 4000)
        # Closing/reopening a fenced block can enlarge a split_markdown result.
        result = []
        for chunk in chunk_text(text, limit, self.config.max_chunks):
            result.extend(chunk[i:i + limit] for i in range(0, len(chunk), limit))
        return result

    async def _bytes(self, descriptor, cap):
        data = descriptor.get("data")
        if data is None and descriptor.get("url"):
            data = await self.client.download_public(descriptor["url"], cap)
        if not isinstance(data, bytes) or len(data) > cap:
            raise ValueError("WeChat outgoing media is missing or exceeds the configured limit")
        return data

    async def send(self, user_id, text="", *, images=(), files=(), videos=(), purpose="proactive",
                   msg_id=None, logical_id=None):
        if purpose not in {"reply", "proactive", "notification"}:
            raise ValueError("Invalid WeChat delivery purpose")
        if purpose == "reply" and not msg_id:
            raise ValueError("An immediate WeChat reply needs its input message ID")
        if self.state.cooldown_until > time.time():
            return False
        logical_id = logical_id or secrets.token_hex(16)
        parts = [("text", chunk, "") for chunk in self.chunks(text)]
        for kind, descriptors, cap in (("image", images, self.config.max_image_bytes),
                                       ("file", files, self.config.max_file_bytes),
                                       ("video", videos, self.config.max_video_upload_bytes)):
            parts.extend((kind, (descriptor, cap), descriptor.get("filename") or f"media.{kind}")
                         for descriptor in descriptors)
        if not parts:
            return False
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            context = self.context_lookup(user_id, msg_id if purpose == "reply" else None)
            if not context:
                logger.warning("WeChat delivery has no context for its recipient; not sent")
                return False
            for index, (kind, content, filename) in enumerate(parts):
                key = hashlib.sha256(json.dumps([logical_id, user_id, purpose, index, kind]).encode()).hexdigest()
                status = self.state.part_status(key)
                if status == "confirmed":
                    continue
                if status in {"sending", "unknown"}:
                    logger.warning("WeChat delivery outcome is uncertain; automatic resend suppressed")
                    return False
                try:
                    if kind == "text":
                        item = {"type": 1, "text_item": {"text": content}}
                    else:
                        descriptor, cap = content
                        data = await self._bytes(descriptor, cap)
                        item = await self.media.upload(data, user_id, kind, filename)
                except Exception as exc:
                    logger.warning("WeChat media preparation failed: %s", safe_error(exc))
                    return False
                message = {"from_user_id": "", "to_user_id": user_id,
                           "client_id": f"muse-wechat:{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                           "message_type": 2, "message_state": 2,
                           "context_token": context, "item_list": [item]}
                # Reserve before the non-idempotent request. A crash leaves 'sending'.
                self.state.reserve_part(key)
                try:
                    await self.client.send_message(message)
                except (WeChatAPIError, SessionCoolingError) as exc:
                    self.state.finish_part(key, "retryable")
                    logger.warning("WeChat send refused/deferred: %s", safe_error(exc))
                    return False
                except Exception as exc:
                    self.state.finish_part(key, "unknown")
                    logger.warning("WeChat send outcome uncertain: %s", safe_error(exc))
                    return False
                self.state.finish_part(key, "confirmed")
        return True
