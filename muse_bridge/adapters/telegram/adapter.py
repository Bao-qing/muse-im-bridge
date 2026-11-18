"""Translate Telegram Bot API messages into the gateway's platform contract."""
from __future__ import annotations

import asyncio
from pathlib import Path

from ...core.message import Attachment, InboundMessage, MessageSource
from ...observability import get_logger
from ..base import BaseAdapter
from .client import TelegramAPIError, TelegramClient
from .gateway import TelegramPoller
from .outbound import TelegramSender, message_key


logger = get_logger(__name__)


def _object(value):
    return value if isinstance(value, dict) else {}


def _targeted_text(text, entities, username, bot_id):
    if not isinstance(entities, list) or not entities or not text:
        return text, False
    raw = text.encode("utf-16-le")
    edits = []
    mentioned = False
    for entry in entities:
        entry = _object(entry)
        kind = entry.get("type")
        if kind not in ("mention", "text_mention", "bot_command"):
            continue
        offset, length = entry.get("offset"), entry.get("length")
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
            continue
        start, end = offset * 2, (offset + length) * 2
        if end > len(raw):
            continue
        try:
            token = raw[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if (kind == "mention" and token.casefold() == "@" + username
                or kind == "text_mention" and _object(entry.get("user")).get("id") == bot_id):
            edits.append((start, end, b""))
            mentioned = True
        elif kind == "bot_command" and token.startswith("/") and not raw[:start].decode("utf-16-le").strip():
            command, sep, addressee = token.partition("@")
            if sep:
                if addressee.casefold() != username:
                    return None, False  # the command explicitly addresses another bot
                edits.append((start, end, command.encode("utf-16-le")))
                mentioned = True
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    return raw.decode("utf-16-le"), mentioned


def _attachments(message):
    photos = message.get("photo")
    if isinstance(photos, list):
        for photo in reversed(photos):
            file_id = _object(photo).get("file_id")
            if isinstance(file_id, str) and file_id:
                yield Attachment("image", "photo.jpg", "image/jpeg", file_info=file_id)
                break
    for field, kind, filename, mime in (
        ("document", "file", "file.bin", "application/octet-stream"),
        ("video", "video", "video.mp4", "video/mp4"),
    ):
        media = _object(message.get(field))
        file_id = media.get("file_id")
        if isinstance(file_id, str) and file_id:
            name, content_type = media.get("file_name"), media.get("mime_type")
            yield Attachment(kind, name if isinstance(name, str) and name else filename,
                             content_type if isinstance(content_type, str) and content_type else mime,
                             file_info=file_id)


class TelegramAdapter(BaseAdapter):
    def __init__(self, config, *, transport=None):
        self.config = config
        self.client = TelegramClient(config, transport=transport)
        self.sender = TelegramSender(config, self.client)
        self._bot_id = 0
        self._username = ""
        self._poller = None
        self._task = None

    @property
    def name(self):
        return "telegram"

    @property
    def is_enabled(self):
        return self.config.enabled and bool(self.config.bot_token)

    def account_identity(self):
        return str(self._bot_id) if self._bot_id else ""

    async def _identify(self):
        bot = _object(await self.client.call("getMe"))
        bot_id, username = bot.get("id"), bot.get("username")
        if (type(bot_id) is not int or str(bot_id) != self.config.bot_token.partition(":")[0]
                or bot.get("is_bot") is not True or not isinstance(username, str) or not username):
            raise RuntimeError("Telegram getMe did not confirm the configured bot identity")
        path = Path(f"{self.config.routes_file}.telegram-{bot_id}.poll.local.json")
        self._poller = TelegramPoller(self.client, self.config.poll_timeout_s, path, bot_id, self._handle_update)
        self._bot_id, self._username = bot_id, username.casefold()
        if self.host is not None:
            self.host.identity_changed(self)

    async def prepare(self):
        if not self.is_enabled or self._poller is not None:
            return
        try:
            await self._identify()
        except TelegramAPIError as exc:
            if exc.rejected and exc.status != 429:
                raise
            logger.warning("%s; bot identity will be retried by the poller", exc)

    async def start(self):
        if not self.is_enabled or self._task is not None:
            return
        self._task = asyncio.current_task()
        try:
            delay = 1
            while self._poller is None:
                try:
                    await self._identify()
                except TelegramAPIError as exc:
                    if exc.rejected and exc.status != 429:
                        raise
                    logger.warning("%s; bot identity will retry", exc)
                    await asyncio.sleep(max(delay, exc.retry_after))
                    delay = min(delay * 2, 30)
            await self._poller.run()
        finally:
            self._task = None

    async def stop(self):
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.client.close()

    async def _handle_update(self, update):
        message = _object(update.get("message"))
        chat, user = _object(message.get("chat")), _object(message.get("from"))
        kind = {"private": "private", "group": "group", "supergroup": "group"}.get(chat.get("type"))
        mid, target, sender = message.get("message_id"), chat.get("id"), user.get("id")
        if (type(mid) is not int or mid <= 0 or type(target) is not int
                or type(sender) is not int or sender <= 0 or user.get("is_bot") is not False
                or sender == self._bot_id or not kind or message.get("sender_chat")
                or message.get("is_topic_message")
                or (kind == "private" and target != sender) or (kind == "group" and target >= 0)):
            return "ignored"
        if isinstance(message.get("text"), str):
            text, entities = message["text"], message.get("entities")
        else:
            caption = message.get("caption")
            text, entities = caption if isinstance(caption, str) else "", message.get("caption_entities")
        text, mentioned = _targeted_text(text, entities, self._username, self._bot_id)
        if text is None:
            return "ignored"
        references = []
        quoted = _object(message.get("reply_to_message"))
        quoted_id = quoted.get("message_id")
        if (type(quoted_id) is int and quoted_id > 0 and not quoted.get("is_topic_message")
                and _object(quoted.get("chat")).get("id") == target):
            references.append(("id", message_key(target, quoted_id)))
        name = " ".join(user[k] for k in ("first_name", "last_name") if isinstance(user.get(k), str))
        date = message.get("date")
        return await self.emit(InboundMessage(
            id=message_key(target, mid), source=MessageSource(self.name, kind, str(target), str(sender), name),
            text=text, attachments=list(_attachments(message)), timestamp=date if type(date) is int else 0,
            references=references, mentioned=mentioned,
        ))

    async def download_attachment(self, attachment, max_bytes):
        return await self.client.download(attachment.file_info, max_bytes)

    async def deliver(self, envelope):
        return await self.sender.deliver(envelope)
