"""QQ platform driver mounted on the shared business gateway.

Parses QQ events and awaits gateway intake; QQSender handles resolved outbound
requests. The driver owns no Muse session, interaction or subscription state.
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from ...config.models import QQConfig
from ...core.delivery import ReactionEnvelope, reaction_as_delivery
from ...core.identity import valid_id
from ...core.message import Attachment, InboundMessage, MessageSource
from ..base import BaseAdapter
from ...observability import get_logger
from .client import QQClient
from .gateway import QQGateway
from .outbound import QQSender

logger = get_logger(__name__)

MESSAGE_EVENTS = frozenset({"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"})


class QQAdapter(BaseAdapter):
    def __init__(self, config: QQConfig, *, client=None):
        self.config = config
        self.client = client or QQClient(
            app_id=config.app_id, app_secret=config.app_secret,
            proxy_url=config.proxy_url, download_proxy=config.download_proxy,
            user_agent=config.user_agent)
        self.sender = QQSender(config, self.client)
        self._running = False

        # Inbound Gateway (receives QQ events)
        self.gateway = QQGateway(
            client=self.client,
            on_event=self.handle_event,
            proxy_url=self.config.proxy_url,
            debug=self.config.debug,
            ws_guid=self.config.ws_guid,
            user_agent=self.config.user_agent,
        )

    def account_identity(self):
        return self.config.app_id

    async def deliver(self, envelope):
        return await self.sender.deliver(envelope)

    async def deliver_reaction(self, envelope: ReactionEnvelope):
        return await self.deliver(reaction_as_delivery(envelope, quote=self.config.reaction_quote))

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
        logger.info('Starting QQ platform driver (AppID: %s)...', self.config.app_id)

        await self.gateway.start()

    async def stop(self) -> None:
        self._running = False
        await self.gateway.stop()

    async def download_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.client.download_bytes, att.url, max_bytes, self.config.download_timeout_s
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
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

    @staticmethod
    def own_references(event):
        scene = event.get("message_scene") or {}
        entries = scene.get("ext", []) if isinstance(scene, dict) else []
        return [("ref", entry.partition("=")[2]) for entry in entries
                if isinstance(entry, str) and entry.startswith("msg_idx=")]

    async def handle_event(self, event_type: str, d: dict) -> None:
        msg_id = d.get("id")
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
        await self.emit(InboundMessage(
            id=msg_id or "",
            source=MessageSource(platform="qq", chat_type=kind, target_id=target,
                                 sender_id=user,
                                 sender_name=author.get("username") or author.get("nickname") or ""),
            text=content,
            attachments=self.extract_attachments(d),
            references=self.message_references(d),
            own_references=self.own_references(d),
        ))
