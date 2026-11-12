"""A thin iLink platform on the shared chat gateway."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import random
import time

from ...core.commands import HELP_TEXT
from ...core.message import Attachment, InboundMessage, MessageSource
from ...gateway.chat import ChatGateway
from ...observability import get_logger, safe_error
from .client import WeChatClient
from .gateway import WeChatGateway
from .media import WeChatMedia
from .outbound import WeChatOutboundPusher
from .sender import WeChatSender
from .state import WeChatState, WeChatStateError, _id, _text

logger = get_logger(__name__)


def parse_event(event, bot_id):
    """Return public message + private media descriptors, never a raw token event."""
    if (not isinstance(event, dict) or type(event.get("message_type")) is not int
            or event.get("message_type") != 1 or event.get("group_id")):
        return None
    user = event.get("from_user_id")
    mid = event.get("message_id")
    if type(mid) is int:
        mid = str(mid)
    if not _id(user) or not _id(mid) or (event.get("to_user_id") and event["to_user_id"] != bot_id):
        return None
    token = event.get("context_token")
    if token is not None and token != "" and not _text(token):
        return None
    items = event.get("item_list", [])
    if not isinstance(items, list) or len(items) > 64:
        return None
    texts, attachments, media = [], [], {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if type(kind) is not int:
            continue
        if kind == 1:
            text_item = item.get("text_item") or {}
            text = text_item.get("text") if isinstance(text_item, dict) else None
            if isinstance(text, str):
                reference = item.get("ref_msg")
                if isinstance(reference, dict) and isinstance(reference.get("title"), str):
                    text = "[引用: " + reference["title"][:2000] + "]\n" + text
                texts.append(text)
        elif kind in {2, 3, 4, 5}:
            field, att_type = {2: ("image_item", "image"), 3: ("voice_item", "file"),
                               4: ("file_item", "file"), 5: ("video_item", "video")}[kind]
            descriptor = item.get(field)
            if not isinstance(descriptor, dict):
                continue
            if kind == 3 and isinstance(descriptor.get("text"), str) and descriptor["text"]:
                texts.append(descriptor["text"])
                continue
            key = hashlib.sha256(f"{user}\0{mid}\0{index}".encode()).hexdigest()
            name = descriptor.get("file_name") or {2: "image.jpg", 3: "voice.audio", 4: "file.dat", 5: "video.mp4"}[kind]
            # Never use the remote file name as a local path.
            name = os.path.basename(str(name).replace("\\", "/"))[:200] or "file.dat"
            attachments.append(Attachment(type=att_type, filename=name, file_info=key))
            media[key] = (copy.deepcopy(descriptor), kind == 2)
    text = "\n".join(texts)
    if len(text) > 1048576:
        return None
    stamp = event.get("create_time_ms", 0)
    stamp = stamp / 1000 if type(stamp) in (int, float) and 0 <= stamp <= 2**63 - 1 else 0
    message = InboundMessage(str(mid), MessageSource("wechat", "private", user, user, raw_event={}),
                             text, attachments, stamp)
    return message, media, token


class IntakeRetryable(Exception):
    pass


class WeChatAdapter(ChatGateway):
    def __init__(self, config, muse_service, deduplicator=None, *, state=None, client=None):
        self.state = state or WeChatState(config.state_dir)
        if not self.state.credentials:
            raise WeChatStateError("WeChat is not logged in; run python main.py --wechat-login")
        self.state.recover_inputs()
        config = config.for_account(self.state.bot_id)
        credentials = self.state.credentials
        self.client = client or WeChatClient(
            credentials["bot_token"], credentials["baseurl"],
            channel_version=config.channel_version, route_tag=config.route_tag,
            timeout=config.http_timeout_s, cooling_until=lambda: self.state.cooldown_until,
            on_session_invalid=self.state.pause)
        self.media = WeChatMedia(self.client)
        self._media_descriptors = {}
        self.sender = WeChatSender(self.client, self.state, config, self.media)
        super().__init__(config, muse_service, deduplicator)
        self.gateway = WeChatGateway(self.client, self.state, self.handle_batch, config.polling_timeout_ms)
        self._tasks = []
        self._typing_task = None
        self._typing_tickets = {}
        self._typing_retry = {}

    @property
    def name(self):
        return "wechat"

    def account_identity(self):
        return self.state.bot_id

    def notification_target(self):
        return self.state.credentials["user_id"]

    def can_recreate_session(self, error):
        # No recovery POST: even an error response may follow an accepted input.
        return False

    def create_outbound(self):
        return WeChatOutboundPusher(self.config, self.client, self.muse_service, self.deduplicator,
                                   self.route_manager, self.session_mapper,
                                   on_login_required=self.enter_login_mode, state=self.state, sender=self.sender)

    def render_command_output(self, text):
        if text != HELP_TEXT:
            return text
        # Adapt only the shared help, never arbitrary Muse output or user text.
        lines = []
        for line in text.splitlines():
            if line.startswith("/申请"):
                continue
            if line.startswith("/登录 "):
                line = "/登录 [邮箱] — 显式登录 Muse"
            elif line.startswith("/凭证 "):
                line = "/凭证 [编号] [保存|一次] — 填写凭证"
            lines.append(line)
        return "\n".join(lines)

    async def send_reply(self, target_type, target_id, text, msg_id=None):
        if target_type != "private":
            raise ValueError("WeChat only supports private chats")
        purpose = "reply" if msg_id else "notification"
        logical_id = f"input:{msg_id}:{hashlib.sha256(text.encode()).hexdigest()}" if msg_id else None
        ok = await self.sender.send(target_id, self.render_command_output(text), purpose=purpose,
                                    msg_id=msg_id, logical_id=logical_id)
        if not ok:
            raise RuntimeError("WeChat reply was not confirmed; inspect local status")
        return {}  # ret=0 is not a server message ID or a native quote receipt.

    async def download_attachment(self, att, max_bytes):
        entry = self._media_descriptors.get(att.file_info)
        if entry is None:
            raise ValueError("WeChat media descriptor is unavailable")
        descriptor, is_image = entry
        return await self.media.download(descriptor, max_bytes, image=is_image)

    async def handle_batch(self, events):
        ready = []
        for event in events:
            parsed = parse_event(event, self.state.bot_id)
            if parsed is None:
                continue
            message, media, token = parsed
            user = message.source.sender_id
            if self.state.message_status(user, message.id) in {"handled", "unknown", "ignored"}:
                continue
            self.state.remember_message(user, message.id, token)
            ready.append((message, media))
        # Latest reflects the whole accepted batch; immediate replies use snapshots.
        for message, media in ready:
            user = message.source.sender_id
            if self.state.message_status(user, message.id) != "accepted":
                continue
            self._media_descriptors.update(media)
            try:
                # Persist before any non-idempotent call; unknown results never replay.
                self.state.finish_message(user, message.id, "unknown")
                result = await self._with_typing(message)
                if result == "retryable":
                    self.state.retry_message(user, message.id)
                    raise IntakeRetryable("WeChat intake not accepted; cursor retained")
                self.state.finish_message(user, message.id, "unknown" if result == "unknown" else "handled")
            finally:
                for key in media:
                    self._media_descriptors.pop(key, None)

    async def _with_typing(self, message):
        # Only cover the local intake operation; no guessed Muse execution lifetime.
        task = None
        if self.config.typing_enabled and not message.text.startswith("/"):
            task = asyncio.create_task(self._typing(message.source.sender_id, message.id))
            self._typing_task = task
        try:
            return await self.handle_inbound(message)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._typing_task = None

    async def _typing_ticket(self, user, msg_id):
        now = time.monotonic()
        cached = self._typing_tickets.get(user)
        if cached and cached[1] > now:
            return cached[0]
        retry_at, delay = self._typing_retry.get(user, (0, 2))
        if retry_at > now:
            return None
        try:
            response = await self.client.get_config(user, self.state.context(user, msg_id))
            ticket = response.get("typing_ticket")
            if not isinstance(ticket, str) or not ticket:
                raise ValueError("No WeChat typing ticket")
        except Exception as exc:
            if user not in self._typing_retry and len(self._typing_retry) >= 2048:
                self._typing_retry.pop(next(iter(self._typing_retry)))
            self._typing_retry[user] = (now + delay, min(3600, delay * 2))
            logger.debug("WeChat typing config unavailable: %s", safe_error(exc))
            return None
        self._typing_retry.pop(user, None)
        if user not in self._typing_tickets and len(self._typing_tickets) >= 2048:
            self._typing_tickets.pop(next(iter(self._typing_tickets)))
        self._typing_tickets[user] = (ticket, now + random.uniform(20 * 3600, 24 * 3600))
        return ticket

    async def _typing(self, user, msg_id):
        ticket = None
        try:
            ticket = await self._typing_ticket(user, msg_id)
            if not ticket:
                return
            while True:
                await self.client.send_typing(user, ticket, 1)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("WeChat typing unavailable: %s", safe_error(exc))
        finally:
            if ticket:
                try:
                    await asyncio.wait_for(self.client.send_typing(user, ticket, 2),
                                           timeout=min(3, self.config.http_timeout_s))
                except Exception:
                    pass

    async def start(self):
        if not self.is_enabled:
            return
        if self._tasks:
            raise RuntimeError("WeChat adapter is already running")
        self._running = True
        self._tasks = [asyncio.create_task(self.gateway.start()),
                       asyncio.create_task(self.outbound_pusher.start())]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.gateway.stop()
        await self.outbound_pusher.stop()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        if self._typing_task:
            self._typing_task.cancel()
            tasks.append(self._typing_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        await self.client.aclose()
