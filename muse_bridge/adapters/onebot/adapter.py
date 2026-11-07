"""OneBot v11 (NapCat) adapter on the chat gateway.

Coordinates:
- OneBotGateway (forward WebSocket events) -> ``InboundMessage`` ->
  ``ChatGateway.handle_inbound``: the same sessions, commands, approvals and
  whitelist as QQ.
- OneBotOutboundPusher (Muse event stream -> send_*_msg / upload_*_file).
- OneBotClient (HTTP API, Array segments, retcode checked).

Ids are QQ numbers; they are handled as strings everywhere above this module.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
from typing import Dict, List, Optional, Tuple

from ...config.models import OneBotConfig
from ...core.deduplicator import MessageDeduplicator
from ...core.message import Attachment, InboundMessage, MessageSource
from ...gateway.access import valid_id
from ...gateway.chat import ChatGateway
from ...muse.service import MuseService
from ...observability import get_logger, safe_error
from .client import OneBotClient, text_segment
from .gateway import OneBotGateway
from .outbound import OneBotOutboundPusher

logger = get_logger(__name__)

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+|all)[^\]]*\]")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)[^\]]*\]")


def _default_ws_url(base_url: str) -> str:
    # NapCat serves HTTP and forward WS on the same port by default.
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://"):]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://"):]
    return base_url


def parse_segments(message, self_id: Optional[str], *, strip_text: bool = True) -> Tuple[str, bool, List[Attachment], List[Tuple[str, str]]]:
    """``(text, mentions_bot, attachments, references)`` from an event's ``message``.

    ``message`` is the Array form; a CQ-code string (``message_format: string``)
    is reduced to its text, @ and reply codes.
    """
    if isinstance(message, str):
        mentioned = bool(self_id) and any(qq == self_id for qq in _CQ_AT_RE.findall(message))
        references = [("id", rid) for rid in _CQ_REPLY_RE.findall(message)]
        text = _CQ_AT_RE.sub(lambda m: "" if m.group(1) == self_id else f"@{m.group(1)}", message)
        text = _CQ_RE.sub("", text)
        return text.strip() if strip_text else text, mentioned, [], references

    parts: List[str] = []
    mentioned = False
    attachments: List[Attachment] = []
    references: List[Tuple[str, str]] = []
    for seg in message if isinstance(message, list) else []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            if self_id and qq == self_id:
                mentioned = True
            elif qq:
                parts.append(f"@{data.get('name') or qq}")
        elif kind == "reply":
            if data.get("id") is not None:
                references.append(("id", str(data["id"])))
        elif kind in ("image", "video", "file", "record"):
            name = str(data.get("file") or data.get("name") or "")
            url = data.get("url") if str(data.get("url") or "").startswith(("http://", "https://")) else None
            att_type = "image" if kind == "image" else "video" if kind == "video" else "file"
            if kind == "image" and not name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
                name = (name or "image") + ".jpg"
            attachments.append(Attachment(
                type=att_type,
                filename=name,
                content_type=mimetypes.guess_type(name)[0] or ("image/jpeg" if att_type == "image" else ""),
                url=url,
                file_info=str(data.get("file_id") or "") or None,
            ))
    text = "".join(parts)
    return text.strip() if strip_text else text, mentioned, attachments, references


class OneBotAdapter(ChatGateway):
    def __init__(
        self,
        config: OneBotConfig,
        muse_service: MuseService,
        deduplicator: Optional[MessageDeduplicator] = None,
    ):
        self.config = config
        self.client = OneBotClient(base_url=config.base_url, access_token=config.access_token)
        super().__init__(config, muse_service, deduplicator)
        self.gateway = OneBotGateway(
            ws_url=config.ws_url or _default_ws_url(config.base_url),
            access_token=config.access_token,
            on_message=self.handle_event,
        )
        self.self_id: Optional[str] = None
        # file_id -> group id of a received group file (get_group_file_url needs it).
        self._file_groups: Dict[str, str] = {}

    def create_outbound(self) -> OneBotOutboundPusher:
        return OneBotOutboundPusher(
            config=self.config,
            client=self.client,
            muse_service=self.muse_service,
            deduplicator=self.deduplicator,
            route_manager=self.route_manager,
            session_mapper=self.session_mapper,
            on_login_required=self.enter_login_mode,
        )

    def access_identity(self) -> str:
        return "onebot"

    @property
    def name(self) -> str:
        return "onebot"

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url)

    async def start(self) -> None:
        if not self.is_enabled:
            return
        self._running = True
        logger.info('Starting OneBot Adapter (%s)...', self.config.base_url)
        try:
            info = await asyncio.to_thread(self.client.get_login_info)
            if info.get("user_id"):
                self.self_id = str(info["user_id"])
                self.outbound_pusher.bind_reference_account(self.self_id)
                logger.info('OneBot account %s (%s)', self.self_id, info.get("nickname") or "")
        except Exception as e:
            # Events carry self_id too; the WS lifecycle event fills it in.
            logger.warning('OneBot get_login_info unavailable: %s', safe_error(e))
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
        data = await asyncio.to_thread(self.client.send_msg, target_type, target_id, [text_segment(text)])
        # Same shape as QQ's sent body: a later reply segment quotes this id.
        return {"id": str(data.get("message_id") or "")}

    async def download_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        url = att.url
        if not url and att.file_info:
            group_id = self._file_groups.get(att.file_info)
            source = await asyncio.to_thread(self.client.file_source, att.file_info, group_id)
            if source.get("base64"):
                data = base64.b64decode(source["base64"])
                if len(data) > max_bytes:
                    raise ValueError(f"超过 {max_bytes // (1024 * 1024)}MB 上限")
                return data
            url = source.get("url")
        if not url:
            raise RuntimeError("NapCat 未提供可下载地址")
        return await asyncio.to_thread(self.client.download_bytes, url, max_bytes, self.config.download_timeout_s)

    @staticmethod
    def message_references(event):
        message = event.get("message") if isinstance(event, dict) else None
        return parse_segments(message, None)[3]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def triggers(self, kind: str, text: str, mentioned: bool) -> bool:
        """Whether a message reaches the gateway (group_trigger applies to groups only)."""
        if kind != "group" or self.config.group_trigger == "all":
            return True
        if text.startswith("/"):
            return True
        return self.config.group_trigger == "at" and mentioned

    async def handle_event(self, event: dict) -> None:
        if event.get("post_type") != "message":
            return
        kind = event.get("message_type")
        if kind not in ("private", "group"):
            return
        msg_id = str(event.get("message_id") or "")
        if msg_id and self.deduplicator.is_duplicate_gateway_msg(msg_id):
            logger.debug('Duplicate OneBot message ignored: %s', msg_id)
            return

        self_id = str(event["self_id"]) if event.get("self_id") else self.self_id or self.gateway.self_id
        if self_id:
            self.self_id = self_id
            self.outbound_pusher.bind_reference_account(self_id)
        user = str(event.get("user_id") or "")
        if self_id and user == self_id:
            return
        target = user if kind == "private" else str(event.get("group_id") or "")
        if not valid_id(user) or not valid_id(target):
            return

        text, mentioned, attachments, references = parse_segments(event.get("message"), self_id, strip_text=False)
        credential_input = self.interaction_router.is_credential_input((kind, target, user), text, event)
        if not credential_input:
            text = text.strip()
        session_id = self.session_mapper.current(kind, target) or ""
        known_quote = self.outbound_pusher.reference_store.resolve_channel(session_id, kind, target, references)
        if not credential_input and not known_quote and not self.triggers(kind, text, mentioned):
            return
        for att in attachments:
            if att.file_info and kind == "group":
                self._file_groups[att.file_info] = target
                while len(self._file_groups) > 512:
                    self._file_groups.pop(next(iter(self._file_groups)))
        sender = event.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        await self.handle_inbound(InboundMessage(
            id=msg_id,
            source=MessageSource(platform="onebot", chat_type=kind, target_id=target,
                                 sender_id=user, raw_event=event,
                                 sender_name=sender.get("nickname") or "",
                                 sender_card=sender.get("card") or ""),
            text=text,
            attachments=attachments,
            references=references,
        ))
