"""QQ official bot adapter on the chat gateway.

Completely decouples Inbound message reception from Outbound delivery:
1. Inbound: QQGateway events are parsed into ``InboundMessage`` and handed to
   ``ChatGateway.handle_inbound``, which fires-and-forgets to Muse immediately.
2. Outbound: QQOutboundPusher subscribes to the Muse event stream and sends
   replies / proactive pushes through the QQ API (passive replies first).
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from ...config.models import QQConfig
from ...core.deduplicator import MessageDeduplicator
from ...core.message import Attachment, InboundMessage, MessageSource
from ...gateway.access import valid_id
from ...gateway.chat import ChatGateway
from ...muse.service import MuseService
from ...observability import get_logger
from .client import QQClient
from .gateway import QQGateway
from .outbound import QQOutboundPusher

logger = get_logger(__name__)

MESSAGE_EVENTS = frozenset({"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"})


class QQAdapter(ChatGateway):
    def __init__(
        self,
        config: QQConfig,
        muse_service: MuseService,
        deduplicator: Optional[MessageDeduplicator] = None,
    ):
        self.config = config
        self.client = QQClient(
            app_id=config.app_id,
            app_secret=config.app_secret,
            proxy_url=config.proxy_url,
            download_proxy=config.download_proxy,
            user_agent=config.user_agent,
        )
        super().__init__(config, muse_service, deduplicator)

        # Inbound Gateway (receives QQ events)
        self.gateway = QQGateway(
            client=self.client,
            on_event=self.handle_event,
            proxy_url=self.config.proxy_url,
            debug=self.config.debug,
            ws_guid=self.config.ws_guid,
            user_agent=self.config.user_agent,
        )

    def create_outbound(self) -> QQOutboundPusher:
        return QQOutboundPusher(
            config=self.config,
            client=self.client,
            muse_service=self.muse_service,
            deduplicator=self.deduplicator,
            route_manager=self.route_manager,
            session_mapper=self.session_mapper,
            on_login_required=self.enter_login_mode,
        )

    def access_identity(self) -> str:
        return self.config.app_id

    @property
    def name(self) -> str:
        return "qq"

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.app_id and self.config.app_secret)

    async def start(self) -> None:
        if not self.is_enabled:
            return
        self._running = True
        logger.info('Starting decoupled QQ Adapter (AppID: %s)...', self.config.app_id)

        tasks = [
            asyncio.create_task(self.gateway.start()),
            asyncio.create_task(self.outbound_pusher.start()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        await self.gateway.stop()
        await self.outbound_pusher.stop()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def send_reply(self, target_type, target_id, text, msg_id=None) -> dict:
        loop = asyncio.get_running_loop()
        code, body = await loop.run_in_executor(
            None,
            self.client.send_message,
            target_type,
            target_id,
            text,
            text,
            None,
            msg_id,
            1 if msg_id else None,
        )
        if not (200 <= code < 300):
            # Fall back to active send so command replies are never dropped
            code, body = await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                text,
                text,
            )
        if not (200 <= code < 300):
            raise RuntimeError("QQ reply delivery failed")
        return body

    async def send_command_output(self, target_type, target_id, text, msg_id=None) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self.client.send_message,
            target_type,
            target_id,
            text,
            text,
            None,
            msg_id,
        )

    async def send_notice(self, target_id, text) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.client.send_message, "private", target_id, text)

    async def download_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.client.download_bytes, att.url, max_bytes, self.config.download_timeout_s
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def is_allowed(self, event_type: str, d: dict) -> bool:
        """Check sender against whitelist."""
        author = d.get("author", {})
        if not isinstance(author, dict):
            return False
        openid = author.get("user_openid") or author.get("member_openid") or author.get("id") or ""
        if not valid_id(openid):
            return False
        if event_type == "C2C_MESSAGE_CREATE":
            return self.access_requests.allows("private", openid, openid)
        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            gid = d.get("group_openid")
            return valid_id(gid) and self.access_requests.allows("group", gid, openid)
        return False

    def extract_attachments(self, d: dict) -> List[Attachment]:
        attachments = []
        raw_list = d.get("attachments") or []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url:
                continue
            ctype = str(item.get("content_type") or "").lower()
            filename = item.get("filename") or ""

            if ctype.startswith("image"):
                att_type = "image"
            elif ctype.startswith("video"):
                att_type = "video"
            else:
                att_type = "file"

            attachments.append(Attachment(
                type=att_type,
                filename=filename,
                content_type=ctype,
                url=url,
            ))
        return attachments

    @staticmethod
    def message_references(event):
        references = []
        scene = event.get("message_scene") or {}
        entries = scene.get("ext") if isinstance(scene, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str):
                key, _, value = entry.partition("=")
                if key == "ref_msg_idx" and value:
                    references.append(("ref", value))
        reference = event.get("message_reference") or {}
        if isinstance(reference, dict) and isinstance(reference.get("message_id"), str):
            references.append(("id", reference["message_id"]))
        return references

    async def handle_event(self, event_type: str, d: dict) -> None:
        msg_id = d.get("id")
        if msg_id and self.deduplicator.is_duplicate_gateway_msg(msg_id):
            logger.debug('Duplicate gateway message ignored: %s', msg_id)
            return

        if event_type not in MESSAGE_EVENTS:
            return
        author = d.get("author")
        if not isinstance(author, dict):
            return
        user = author.get("user_openid") or author.get("member_openid") or author.get("id") or ""
        kind = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
        target = user if kind == "private" else d.get("group_openid")
        if not valid_id(user) or not valid_id(target):
            return
        raw_content = d.get("content") or ""
        if not isinstance(raw_content, str):
            return
        content = re.sub(r"<@!?\w+>", "", raw_content)
        if not self.interaction_router.is_credential_input((kind, target, user), content, d):
            content = content.strip()
        await self.handle_inbound(InboundMessage(
            id=msg_id or "",
            source=MessageSource(platform="qq", chat_type=kind, target_id=target,
                                 sender_id=user, raw_event=d,
                                 sender_name=author.get("username") or author.get("nickname") or ""),
            text=content,
            attachments=self.extract_attachments(d),
        ))
