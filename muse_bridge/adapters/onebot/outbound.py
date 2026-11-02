"""OneBot outbound sender on the platform-neutral dispatcher.

``OutboundDispatcher`` decides what to send and where; this module only
delivers to NapCat: text chunks as ``text`` segments, images and videos as
inline ``base64://`` segments, files through ``upload_*_file``. OneBot has
no passive-reply window, so every item is a plain send. A group that refuses
the bot (muted, kicked) falls back to the first admin's private chat, like
QQ's refused active group message.
"""

from __future__ import annotations

import asyncio
from typing import Callable, List, Optional

from ...core.message import chunk_text
from ...gateway.outbound import OutboundDispatcher
from ...gateway.routes import RouteInfo
from ...observability import get_logger, safe_error
from .client import OneBotAPIError, media_segment, text_segment

logger = get_logger(__name__)

GROUP_FALLBACK_PREFIX = "【群消息提醒】\n"


class OneBotOutboundPusher(OutboundDispatcher):
    platform = "onebot"

    async def _send_notice(self, target_id: str, text: str) -> None:
        await asyncio.to_thread(self.client.send_msg, "private", target_id, [text_segment(text)])

    async def _media_bytes(self, item: dict, cap: int, label: str) -> Optional[bytes]:
        data = item.get("data")
        if not data and item.get("url"):
            try:
                data = await asyncio.to_thread(self.client.download_bytes, item["url"], cap,
                                               self.config.download_timeout_s)
            except Exception as e:
                logger.error('Failed to download %s: %s', label, safe_error(e))
                return None
        if not data:
            return None
        if len(data) > cap:
            logger.warning('%s exceeds limit (%s > %s)', label.capitalize(), len(data), cap)
            return None
        return data

    async def _deliver(
        self,
        target_type: str,
        target_id: str,
        text: str,
        images: List[dict],
        files: List[dict],
        videos: List[dict],
        route: Optional[RouteInfo] = None,
        credential=None,
        delivery=None,
    ) -> bool:
        admin_id = self.config.admin_target()

        async def _send(op: Callable[[str, str], dict], fallback_op: Optional[Callable[[str, str], dict]] = None):
            """Run ``op`` on the target; a refused group send retries privately to the admin."""
            try:
                return target_type, target_id, await asyncio.to_thread(op, target_type, target_id)
            except OneBotAPIError as e:
                if (target_type != "group" or not admin_id
                        or self.quote_for(delivery, target_type, target_id) is not None):
                    raise
                logger.debug('Group send refused (%s); falling back to admin %s', safe_error(e), admin_id)
                return "private", admin_id, await asyncio.to_thread(fallback_op or op, "private", admin_id)

        def segments(t, i, segment):
            reference = self.quote_for(delivery, t, i)
            ref_id = next((value for kind, value in reference.references if kind == "id"), None) if reference else None
            if ref_id:
                return [{"type": "reply", "data": {"id": ref_id}}, segment]
            if delivery and delivery.reply_to_id:
                return [text_segment(self.quote_text(delivery, t, i)), segment]
            return [segment]

        all_delivered = True

        # 1. Text chunks
        for chunk in chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []:
            try:
                sent_type, sent_id, body = await _send(
                    lambda t, i, c=chunk: self.client.send_msg(t, i, segments(t, i, text_segment(c))),
                    lambda t, i, c=chunk: self.client.send_msg(t, i, segments(t, i, text_segment(GROUP_FALLBACK_PREFIX + c))),
                )
            except Exception as e:
                logger.error("Failed sending item to %s:%s: %s", target_type, target_id, safe_error(e))
                all_delivered = False
                continue
            self.remember_delivery(delivery, sent_type, sent_id, body)
            if credential is not None:
                # Quoting this message later selects the credential (reply segment id).
                self.muse_service.interactions.remember_credential_message(
                    sent_type, sent_id, {"id": str((body or {}).get("message_id") or "")}, credential)

        # 2. Images and videos as inline segments
        for kind, items, cap in (("image", images, self.config.max_image_bytes),
                                 ("video", videos, self.config.max_video_upload_bytes)):
            for item in items:
                data = await self._media_bytes(item, cap, kind)
                if data is None:
                    continue
                try:
                    sent_type, sent_id, body = await _send(
                        lambda t, i, d=data, k=kind: self.client.send_msg(t, i, segments(t, i, media_segment(k, d))))
                    self.remember_delivery(delivery, sent_type, sent_id, body)
                except Exception as e:
                    logger.error('Failed to send %s: %s', kind, safe_error(e))

        # 3. Files through the upload API
        for item in files:
            data = await self._media_bytes(item, self.config.max_file_bytes, "file")
            if data is None:
                continue
            name = item.get("filename") or "file.dat"
            try:
                sent_type, sent_id, _ = await _send(
                    lambda t, i, d=data, n=name: self.client.upload_file(t, i, d, n))
                # OneBot upload_*_file has no chat message ID. A companion gives
                # the file a native quote context and a replyable reference.
                if delivery:
                    body = await asyncio.to_thread(
                        self.client.send_msg, sent_type, sent_id,
                        segments(sent_type, sent_id, text_segment("【文件】" + name)))
                    self.remember_delivery(delivery, sent_type, sent_id, body)
            except Exception as e:
                all_delivered = False
                logger.error('Failed to upload file: %s', safe_error(e))

        return all_delivered
