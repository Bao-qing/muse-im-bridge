"""QQ outbound sender on the platform-neutral dispatcher.

``OutboundDispatcher`` decides what to send and where; this module only
delivers to QQ: passive replies first (msg_id + msg_seq), then active sends
within ``max_active_chunks``, media through the QQ files API
(file_type: 1=image, 2=video, 4=file), and the admin fallback when QQ
refuses an active group message.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from ...core.message import chunk_text
from ...gateway.outbound import (  # noqa: F401  (re-exported for callers of the old module)
    OutboundDispatcher,
    _clean_text,
    _extract_media_descriptors,
    gate_catch_up,
)
from ...gateway.routes import RouteInfo
from ...observability import get_logger, safe_error

logger = get_logger(__name__)


class QQOutboundPusher(OutboundDispatcher):
    platform = "qq"

    async def _deliver(self, *args, **kwargs) -> bool:
        return await self._send_to_qq(*args, **kwargs)

    async def _send_notice(self, target_id: str, text: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.client.send_message, "private", target_id, text)

    async def _send_to_qq(
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
        loop = asyncio.get_running_loop()
        chunks = chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []
        passive_limit = 4 if target_type == "private" else 5
        active_budget = self.config.max_active_chunks
        all_delivered = True

        def _get_passive_meta():
            if not route or not route.msg_id:
                return None, None
            s = self.route_manager.next_seq(route.user_mid)
            if s <= passive_limit:
                return route.msg_id, s
            return None, None

        async def _deliver_item(content=None, md=None, media_info=None) -> bool:
            nonlocal active_budget
            curr_mid, curr_seq = _get_passive_meta()

            async def send(t, i, body, markdown, media, passive_id=None, sequence=None):
                reference = self.quote_for(delivery, t, i)
                ref_id = next((v for k, v in reference.references if k == "ref"), None) if reference else None
                if not ref_id and reference:
                    ref_id = next((v for k, v in reference.references if k == "id"), None)
                options = {}
                if ref_id:
                    options["message_reference"] = {"message_id": ref_id, "ignore_get_message_error": False}
                elif delivery and delivery.reply_to_id:
                    prefix = self.quote_text(delivery, t, i)
                    body = prefix + (body or "")
                    if markdown is not None:
                        markdown = prefix + markdown
                return await asyncio.to_thread(
                    self.client.send_message, t, i, body, markdown, media, passive_id, sequence, **options)
            delivered_type, delivered_id = target_type, target_id

            if not curr_mid:
                if active_budget <= 0:
                    logger.warning('Active budget (%s) exhausted; item dropped.', self.config.max_active_chunks)
                    return False
                active_budget -= 1

            code, body = await send(target_type, target_id, content, md, media_info, curr_mid, curr_seq)

            # Retry as active if passive rejected
            if curr_mid and not (200 <= code < 300):
                logger.debug("Passive send failed (status=%s), retrying as active...", code)
                if active_budget > 0:
                    active_budget -= 1
                    code, body = await send(target_type, target_id, content, md, media_info)

            if not (200 <= code < 300):
                # A known native quote must not turn into a private text fallback.
                if (target_type == "group"
                        and self.quote_for(delivery, target_type, target_id) is None
                        and any(k in str(body) for k in ("40034105", "无权限", "主动消息失败"))):
                    admin_type, admin_id = "private", self.config.admin_target()
                    if admin_id:
                        logger.debug('Group active send rejected (%s); falling back to admin %s:%s', code, admin_type, admin_id)
                        delivered_type, delivered_id = admin_type, admin_id
                        code, body = await send(
                            admin_type, admin_id,
                            "【群消息提醒】" + chr(10) + content if content else content,
                            "【群消息提醒】" + chr(10) + md if md else md, media_info)

            if not (200 <= code < 300):
                logger.error("Failed sending item to %s:%s (status=%s)", target_type, target_id, code)
                return False
            self.remember_delivery(delivery, delivered_type, delivered_id, body)
            if credential is not None:
                self.muse_service.interactions.remember_credential_message(
                    delivered_type, delivered_id, body, credential,
                    target=self.interaction_target(delivered_type, delivered_id))
            return True

        # Reaction notifications use text; ordinary replies retain Markdown.
        plain_text = delivery is not None and delivery.plain_text
        for chunk in chunks:
            all_delivered = await _deliver_item(content=chunk, md=None if plain_text else chunk) and all_delivered

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
                    logger.error('Failed to download image: %s', safe_error(e))
                    continue
            if not data:
                continue
            if len(data) > self.config.max_image_bytes:
                logger.warning('Image exceeds limit (%s > %s)', len(data), self.config.max_image_bytes)
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
                await _deliver_item(media_info=file_info)
            except Exception as e:
                logger.error('Failed to upload/send image: %s', safe_error(e))

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
                    logger.error('Failed to download video: %s', safe_error(e))
                    continue
            if not data:
                continue
            if len(data) > self.config.max_video_upload_bytes:
                logger.warning('Video exceeds upload limit (%s > %s)', len(data), self.config.max_video_upload_bytes)
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
                await _deliver_item(media_info=file_info)
            except Exception as e:
                logger.error('Failed to upload/send video: %s', safe_error(e))

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
                    logger.error('Failed to download file: %s', safe_error(e))
                    continue
            if not data:
                continue
            if len(data) > self.config.max_file_bytes:
                logger.warning('File exceeds limit (%s > %s)', len(data), self.config.max_file_bytes)
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
                await _deliver_item(media_info=file_info)
            except Exception as e:
                logger.error('Failed to upload/send file: %s', safe_error(e))

        return all_delivered
