"""OneBot v11 (NapCat) platform driver mounted on UnifiedGateway.

Owns forward WebSocket intake, actual account identity, segment/media parsing
and OneBotSender. Business policy, sessions and Muse subscriptions live in the
host. QQ numeric IDs are normalized as strings above this boundary.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import time
from typing import Dict, List, Optional, Tuple

from ...config.models import OneBotConfig
from ...core.delivery import DeliveryResult, DeliveryStatus, FeedEnvelope
from ...core.identity import valid_id
from ...core.message import Attachment, InboundMessage, MessageSource
from ..base import BaseAdapter
from ...observability import get_logger, safe_error
from .client import OneBotAPIError, OneBotClient
from .gateway import OneBotGateway
from .outbound import OneBotSender

logger = get_logger(__name__)

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+|all)[^\]]*\]")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)[^\]]*\]")
_TYPING_MAX_SECONDS = 600.0
_TYPING_INTERVAL_SECONDS = 5.0


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


class OneBotAdapter(BaseAdapter):
    def __init__(self, config: OneBotConfig, *, client=None):
        self.config = config
        self.client = client or OneBotClient(base_url=config.base_url, access_token=config.access_token)
        self.sender = OneBotSender(config, self.client)
        self._running = False
        self.gateway = OneBotGateway(
            ws_url=config.ws_url or _default_ws_url(config.base_url),
            access_token=config.access_token,
            on_message=self.handle_event,
        )
        self.self_id: Optional[str] = None
        # file_id -> group id of a received group file (get_group_file_url needs it).
        self._file_groups: Dict[str, str] = {}
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        self._typing_sessions: Dict[str, str] = {}
        self._typing_deadlines: Dict[str, float] = {}
        self._typing_retry: Dict[str, Tuple[float, float]] = {}

    def account_identity(self):
        return self.self_id or ""

    async def deliver(self, envelope):
        return await self.sender.deliver(envelope)

    def activity_started(self, target: str, token: str, *, kind: str = "private") -> None:
        if kind != "private" or not self.config.typing_enabled or not target:
            return
        if token.startswith("intake:"):
            self._typing_sessions.setdefault(target, token)
        else:
            self._typing_sessions[target] = token
        self._ensure_typing(target)

    async def activity(self, target: str, token: str, state: str, *, kind: str = "private") -> None:
        if kind != "private" or not target:
            return
        if state == "started" and self.config.typing_enabled:
            if token.startswith("intake:"):
                self._typing_sessions.setdefault(target, token)
            else:
                self._typing_sessions[target] = token
            self._ensure_typing(target)
        elif state == "finished" and self._typing_sessions.get(target) == token:
            await self._stop_typing(target)

    def _ensure_typing(self, user: str) -> None:
        self._typing_deadlines[user] = time.monotonic() + _TYPING_MAX_SECONDS
        existing = self._typing_tasks.get(user)
        if existing and not existing.done():
            return
        self._typing_tasks[user] = asyncio.create_task(self._typing(user))

    async def _stop_typing(self, user: str) -> None:
        self._typing_sessions.pop(user, None)
        self._typing_deadlines.pop(user, None)
        task = self._typing_tasks.pop(user, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _send_input_status(self, user: str, event_type: int, timeout: int = 5) -> None:
        await asyncio.to_thread(self.client.set_input_status, user, event_type, timeout)

    async def _typing(self, user: str) -> None:
        attempted_active = False
        failed_active = False
        now = time.monotonic()
        retry_at, delay = self._typing_retry.get(user, (0.0, 2.0))
        deadline = self._typing_deadlines.setdefault(user, now + _TYPING_MAX_SECONDS)
        try:
            if retry_at > now:
                return
            while True:
                remaining = self._typing_deadlines.get(user, deadline) - time.monotonic()
                if remaining <= 0:
                    break
                attempted_active = True
                await self._send_input_status(user, 1)
                self._typing_retry.pop(user, None)
                remaining = self._typing_deadlines.get(user, deadline) - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(_TYPING_INTERVAL_SECONDS, remaining))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed_active = True
            now = time.monotonic()
            if user not in self._typing_retry and len(self._typing_retry) >= 2048:
                self._typing_retry.pop(next(iter(self._typing_retry)))
            self._typing_retry[user] = (now + delay, min(3600.0, delay * 2))
            logger.debug("OneBot typing unavailable: %s", safe_error(exc))
        finally:
            current = asyncio.current_task()
            if self._typing_tasks.get(user) is current:
                self._typing_tasks.pop(user, None)
                self._typing_sessions.pop(user, None)
                self._typing_deadlines.pop(user, None)
            if attempted_active and not failed_active:
                try:
                    await asyncio.wait_for(self._send_input_status(user, 2, timeout=3), timeout=3.0)
                except Exception:
                    pass

    def supports_native_feed(self) -> bool:
        return True

    async def publish_feed(self, feed: FeedEnvelope) -> DeliveryResult:
        try:
            await asyncio.to_thread(
                self.client.send_qzone_msg,
                feed.text,
                images=feed.images,
                ugc_right=self.config.qzone_feed.ugc_right,
            )
        except OneBotAPIError as exc:
            if type(exc.retcode) is int and exc.retcode != 0:
                return DeliveryResult(DeliveryStatus.REJECTED, reason="OneBot Qzone refused")
            return DeliveryResult(DeliveryStatus.UNKNOWN, reason="OneBot Qzone response outcome unknown")
        except Exception:
            return DeliveryResult(DeliveryStatus.UNKNOWN, reason="OneBot Qzone outcome unknown")
        return DeliveryResult(DeliveryStatus.CONFIRMED)

    @property
    def name(self) -> str:
        return "onebot"

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url)

    async def prepare(self):
        try:
            info = await asyncio.to_thread(self.client.get_login_info)
            if info.get("user_id"):
                self.self_id = str(info["user_id"])
                if self.host:
                    self.host.identity_changed(self)
        except Exception as exc:
            logger.warning("OneBot identity pending: %s", safe_error(exc))

    async def start(self):
        if not self.is_enabled:
            return
        self._running = True
        await self.gateway.start()

    async def stop(self):
        self._running = False
        await self.gateway.stop()
        tasks = [t for t in self._typing_tasks.values() if t and not t.done()]
        for task in tasks:
            task.cancel()
        self._typing_tasks.clear()
        self._typing_sessions.clear()
        self._typing_deadlines.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
    async def handle_event(self, event: dict) -> None:
        if event.get("post_type") != "message":
            return
        kind = event.get("message_type")
        if kind not in ("private", "group"):
            return
        msg_id = str(event.get("message_id") or "")
        self_id = str(event["self_id"]) if event.get("self_id") else self.self_id or self.gateway.self_id
        if self_id:
            self.self_id = self_id
            if self.host:
                self.host.identity_changed(self)
        user = str(event.get("user_id") or "")
        if self_id and user == self_id:
            return
        target = user if kind == "private" else str(event.get("group_id") or "")
        if not valid_id(user) or not valid_id(target):
            return

        text, mentioned, attachments, references = parse_segments(event.get("message"), self_id, strip_text=False)
        for att in attachments:
            if att.file_info and kind == "group":
                self._file_groups[att.file_info] = target
                while len(self._file_groups) > 512:
                    self._file_groups.pop(next(iter(self._file_groups)))
        sender = event.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        await self.emit(InboundMessage(
            id=msg_id,
            source=MessageSource(platform="onebot", chat_type=kind, target_id=target,
                                 sender_id=user,
                                 sender_name=sender.get("nickname") or "",
                                 sender_card=sender.get("card") or ""),
            text=text,
            attachments=attachments,
            references=references,
            mentioned=mentioned,
        ))
