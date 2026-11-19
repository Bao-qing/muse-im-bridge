"""A thin iLink platform on the shared chat gateway."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import random
import time

from ...core.delivery import DeliveryResult, DeliveryStatus, ReactionEnvelope, reaction_as_delivery
from ...core.message import Attachment, InboundMessage, MessageSource
from ..base import BaseAdapter
from ...observability import get_logger, safe_error
from .client import WeChatClient
from .gateway import WeChatGateway
from .media import WeChatMedia
from .outbound import WeChatSenderTransport
from .sender import WeChatSender
from .state import WeChatState, WeChatStateError, _id, _text

logger = get_logger(__name__)

_TYPING_MAX_SECONDS = 600.0
_MEDIA_ORDER = {"video": 0, "image": 1, "file": 2}


def _message_media_priority(message: InboundMessage) -> int:
    if not message.attachments:
        return 3
    return min(_MEDIA_ORDER.get(att.type, 2) for att in message.attachments)


def _order_ready_batch(ready):
    if len(ready) <= 1:
        return ready
    ordered = list(ready)
    by_user: dict[str, list[int]] = {}
    for idx, (message, _) in enumerate(ready):
        by_user.setdefault(message.source.sender_id, []).append(idx)
    for indices in by_user.values():
        if len(indices) <= 1:
            continue
        segment: list[int] = []
        for idx in indices:
            if (ready[idx][0].text or "").lstrip().startswith("/"):
                if len(segment) > 1:
                    items = sorted((ready[i] for i in segment), key=lambda pair: _message_media_priority(pair[0]))
                    for pos, item in zip(segment, items):
                        ordered[pos] = item
                segment.clear()
            else:
                segment.append(idx)
        if len(segment) > 1:
            items = sorted((ready[i] for i in segment), key=lambda pair: _message_media_priority(pair[0]))
            for pos, item in zip(segment, items):
                ordered[pos] = item
    return ordered


def _ref_msg_id(reference):
    if not isinstance(reference, dict):
        return None
    ref_item = reference.get("message_item")
    if not isinstance(ref_item, dict):
        return None
    mid = ref_item.get("msg_id")
    if type(mid) is int:
        mid = str(mid)
    return mid if _id(mid) else None


def _ref_fallback_text(reference):
    if not isinstance(reference, dict):
        return None
    title = reference.get("title")
    if isinstance(title, str) and title:
        return title
    ref_item = reference.get("message_item")
    if isinstance(ref_item, dict):
        text_item = ref_item.get("text_item")
        if isinstance(text_item, dict) and isinstance(text_item.get("text"), str) and text_item["text"]:
            return text_item["text"]
    return None


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
    texts, attachments, media, references = [], [], {}, []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if type(kind) is not int:
            continue
        reference = item.get("ref_msg")
        ref_mid = _ref_msg_id(reference)
        if ref_mid and ("id", ref_mid) not in references:
            references.append(("id", ref_mid))
        if kind == 1:
            text_item = item.get("text_item") or {}
            text = text_item.get("text") if isinstance(text_item, dict) else None
            if isinstance(text, str):
                if not ref_mid:
                    fallback = _ref_fallback_text(reference)
                    if fallback:
                        text = "[引用: " + fallback[:2000] + "]\n" + text
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
    attachments.sort(key=lambda att: _MEDIA_ORDER.get(att.type, 2))
    stamp = event.get("create_time_ms", 0)
    stamp = stamp / 1000 if type(stamp) in (int, float) and 0 <= stamp <= 2**63 - 1 else 0
    raw_event = {"references": list(references)} if references else {}
    message = InboundMessage(str(mid), MessageSource("wechat", "private", user, user, raw_event=raw_event),
                             text, attachments, stamp, references=references)
    return message, media, token


class IntakeRetryable(Exception):
    pass


class WeChatAdapter(BaseAdapter):
    def __init__(self, config, *, state=None, client=None):
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
        self._tasks = []
        self._typing_tasks = {}
        self._typing_sessions = {}
        self._typing_deadlines = {}
        self._typing_tickets = {}
        self._typing_retry = {}
        self.config = config
        self.transport = WeChatSenderTransport(config, self.sender)
        self._typing_inputs = {}
        self._running = False
        self.gateway = WeChatGateway(self.client, self.state, self.handle_batch, config.polling_timeout_ms)

    @property
    def name(self):
        return "wechat"

    def account_identity(self):
        return self.state.bot_id

    def notification_target(self):
        return self.state.credentials["user_id"]

    @property
    def is_enabled(self):
        return bool(self.config.enabled)

    async def deliver(self, envelope):
        return await self.transport.deliver(envelope)

    async def deliver_reaction(self, envelope: ReactionEnvelope):
        if envelope.is_removal:
            return DeliveryResult(DeliveryStatus.CONFIRMED)
        delivery = reaction_as_delivery(envelope, quote=self.config.reaction_quote)
        if delivery is None:
            return DeliveryResult(DeliveryStatus.CONFIRMED)
        if self.config.reaction_quote and delivery.quote and not delivery.quote_fallback:
            excerpt = delivery.quote.excerpt or "[非文本消息]"
            delivery.quote_fallback = "【引用】" + excerpt.replace("\n", "\n> ") + "\n"
        return await self.deliver(delivery)

    def activity_started(self, target, token, *, kind="private"):
        if kind != "private" or not self.config.typing_enabled:
            return
        if token.startswith("intake:"):
            self._typing_sessions.setdefault(target, token)
        else:
            self._typing_sessions[target] = token
        self._ensure_typing(target, self._typing_inputs.get(target))

    async def activity(self, target, token, state, *, kind="private"):
        if kind != "private":
            return
        if state == "started" and self.config.typing_enabled:
            if token.startswith("intake:"):
                self._typing_sessions.setdefault(target, token)
            else:
                self._typing_sessions[target] = token
            self._ensure_typing(target, self._typing_inputs.get(target))
        elif state == "finished" and self._typing_sessions.get(target) == token:
            await self._stop_typing(target)

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
        for message, media in _order_ready_batch(ready):
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

    def _ensure_typing(self, user, msg_id):
        self._typing_deadlines[user] = time.monotonic() + _TYPING_MAX_SECONDS
        existing = self._typing_tasks.get(user)
        if existing and not existing.done():
            return
        self._typing_tasks[user] = asyncio.create_task(self._typing(user, msg_id))

    async def _stop_typing(self, user):
        self._typing_sessions.pop(user, None)
        self._typing_deadlines.pop(user, None)
        task = self._typing_tasks.pop(user, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _with_typing(self, message):
        user = message.source.sender_id
        self._typing_inputs[user] = message.id
        if self.config.typing_enabled and not message.text.startswith("/"):
            self._ensure_typing(user, message.id)
        try:
            return await self.emit(message)
        finally:
            if user not in self._typing_sessions:
                await self._stop_typing(user)

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
        deadline = self._typing_deadlines.setdefault(user, time.monotonic() + _TYPING_MAX_SECONDS)
        try:
            ticket = await self._typing_ticket(user, msg_id)
            if not ticket:
                return
            while True:
                remaining = self._typing_deadlines.get(user, deadline) - time.monotonic()
                if remaining <= 0:
                    break
                await self.client.send_typing(user, ticket, 1)
                remaining = self._typing_deadlines.get(user, deadline) - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(5.0, remaining))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("WeChat typing unavailable: %s", safe_error(exc))
        finally:
            current = asyncio.current_task()
            if self._typing_tasks.get(user) is current:
                self._typing_tasks.pop(user, None)
                self._typing_sessions.pop(user, None)
                self._typing_deadlines.pop(user, None)
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
        self._tasks = [asyncio.create_task(self.gateway.start())]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.gateway.stop()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        for task in list(self._typing_tasks.values()):
            if task and not task.done():
                task.cancel()
                tasks.append(task)
        self._typing_tasks.clear()
        self._typing_sessions.clear()
        self._typing_deadlines.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        await self.client.aclose()
