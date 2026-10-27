"""QQ official bot adapter.

Unifies:
1. QQGateway (WebSocket event receiver).
2. QQClient (HTTP API & Token Manager).
3. QQPushService (Subsystem for proactive push to first private chat or configured target).
4. MessageDeduplicator (Ensures no message duplication or loss across passive and active channels).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ...config.models import QQConfig
from ...core.commands import CommandError, CommandHandler
from ...core.deduplicator import MessageDeduplicator
from ...core.message import Attachment, chunk_text
from ...muse.service import MuseService
from ..base import BaseAdapter
from .client import QQClient
from .gateway import QQGateway
from .push import QQPushService


class QQAdapter(BaseAdapter):
    def __init__(
        self,
        config: QQConfig,
        muse_service: MuseService,
        deduplicator: Optional[MessageDeduplicator] = None,
    ):
        self.config = config
        self.muse_service = muse_service
        self.client = QQClient(
            app_id=self.config.app_id,
            app_secret=self.config.app_secret,
            proxy_url=self.config.proxy_url,
            download_proxy=self.config.download_proxy,
        )

        self.deduplicator = deduplicator or MessageDeduplicator(
            persistence_file=self.config.delivered_file,
            in_flight_ttl_s=self.config.push.in_flight_ttl_s,
            processing_ttl_s=self.config.push.processing_ttl_s,
            gateway_dedup_s=self.config.gateway_dedup_s,
        )

        self.active_session_id = CommandHandler.load_session_id(
            self.config.session_state_file,
            self.config.session_id,
        )

        # Gateway component
        self.gateway = QQGateway(
            client=self.client,
            on_event=self.handle_event,
            proxy_url=self.config.proxy_url,
            debug=self.config.debug,
        )

        self.is_turn_busy = False

        # Proactive push subsystem
        self.push_service = QQPushService(
            config=self.config,
            client=self.client,
            muse_service=self.muse_service,
            deduplicator=self.deduplicator,
            session_id=self.active_session_id,
            adapter=self,
        )

        self._send_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._pending_waits: Dict[Tuple[str, str], Set[asyncio.Event]] = {}
        self._running = False

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
        print(f"[QQAdapter] Starting QQ Adapter (AppID: {self.config.app_id})...")

        tasks = [asyncio.create_task(self.gateway.start())]
        if self.config.push.enabled:
            tasks.append(asyncio.create_task(self.push_service.start()))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        await self.gateway.stop()
        await self.push_service.stop()

    def is_allowed(self, event_type: str, d: dict) -> bool:
        """Check sender against the whitelist."""
        author = d.get("author", {})
        openid = author.get("user_openid") or author.get("id") or ""
        wl = self.config.whitelist
        if not wl:
            return False

        if event_type == "C2C_MESSAGE_CREATE":
            privates = wl.get("private")
            return isinstance(privates, list) and openid in privates

        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            gid = d.get("group_openid")
            if not gid:
                return False
            allowed = wl.get(gid)
            if allowed == "*":
                return True
            return isinstance(allowed, list) and openid in allowed

        return False

    def chat_key(self, event_type: str, d: dict) -> Tuple[str, str]:
        author = d.get("author", {})
        openid = author.get("user_openid") or author.get("id") or ""
        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            return ("group", d.get("group_openid") or "")
        if event_type == "C2C_MESSAGE_CREATE":
            return ("private", openid)
        return (event_type, openid)

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

    async def handle_event(self, event_type: str, d: dict) -> None:
        msg_id = d.get("id")
        if msg_id and self.deduplicator.is_duplicate_gateway_msg(msg_id):
            if self.config.debug:
                print(f"[QQAdapter] Duplicate message ignored: {msg_id}")
            return

        if not self.is_allowed(event_type, d):
            if self.config.debug:
                print(f"[QQAdapter] Ignored event {event_type}: sender not in whitelist")
            return

        raw_content = (d.get("content") or "").strip()
        # Clean @bot prefix
        content = re.sub(r"<@!?\w+>", "", raw_content).strip()
        attachments = self.extract_attachments(d)

        if not content and not attachments:
            return

        key = self.chat_key(event_type, d)
        target_type = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
        target_id = d.get("author", {}).get("user_openid") if target_type == "private" else d.get("group_openid")
        print(f"[QQAdapter] Inbound message ({event_type}) from {target_type}:{target_id}: {content}")

        # 1. Check for slash command
        parsed = CommandHandler.parse_command(content)
        if parsed is not None:
            cmd_name, cmd_args = parsed
            # Cancel pending waits in this chat
            waits = self._pending_waits.get(key, set())
            for w in list(waits):
                w.set()

            loop = asyncio.get_running_loop()
            try:
                client = None
                if cmd_name not in ("help", "__help__", "__usage_hint__", "new"):
                    client = self.muse_service.create_client()
                    client.connect()
                reply, new_sid = await loop.run_in_executor(
                    None,
                    CommandHandler.execute,
                    cmd_name,
                    cmd_args,
                    client,
                    self.active_session_id,
                    self.config.session_state_file,
                )
                if new_sid != self.active_session_id:
                    self.active_session_id = new_sid
                    self.push_service.session_id = new_sid
            except CommandError as e:
                reply = str(e)
            except Exception as e:
                reply = f"命令执行出错：{e}"
            finally:
                if client:
                    try:
                        client.close()
                    except Exception:
                        pass

            await self._send_reply(target_type, target_id, reply, [], msg_id=msg_id)
            return

        # 2. Normal Chat Turn
        # Acquire chat lock so sending is serialized
        lock = self._send_locks.setdefault(key, asyncio.Lock())
        await lock.acquire()

        self.is_turn_busy = True
        cancel_event = asyncio.Event()
        waits = self._pending_waits.setdefault(key, set())
        for old_w in list(waits):
            old_w.set()
        waits.add(cancel_event)

        try:
            # Download attachments
            loop = asyncio.get_running_loop()
            for att in attachments:
                if att.url and not att.data:
                    max_cap = (
                        self.config.max_video_bytes if att.type == "video"
                        else self.config.max_file_bytes if att.type == "file"
                        else self.config.max_image_bytes
                    )
                    try:
                        att.data = await loop.run_in_executor(
                            None, self.client.download_bytes, att.url, max_cap, self.config.download_timeout_s
                        )
                    except Exception as e:
                        content += f"\n[{att.type}下载失败：{e}]"

            sent_user_mid: Optional[str] = None

            def _on_sent(mid: str):
                nonlocal sent_user_mid
                sent_user_mid = mid
                self.deduplicator.mark_in_flight(mid)
                if self.config.debug:
                    print(f"[QQAdapter] User message sent to Muse, marked in-flight: {mid}")

            import functools

            # Execute turn on Muse
            turn_result = await loop.run_in_executor(
                None,
                functools.partial(
                    self.muse_service.send_turn,
                    self.active_session_id,
                    content,
                    attachments,
                    cancel_event.is_set,
                    self.config.file_ref_threshold_bytes,
                    on_sent=_on_sent,
                ),
            )
        except Exception as e:
            if cancel_event.is_set():
                return
            await self._send_reply(target_type, target_id, f"转接出错了：{e}", [], msg_id=msg_id)
            return
        finally:
            self.is_turn_busy = False
            waits.discard(cancel_event)
            lock.release()

        if cancel_event.is_set():
            return

        # Mark turn messages as delivered so push service will not duplicate
        mids = [m.message_id for m in turn_result.messages if m.message_id]
        if mids:
            self.deduplicator.mark_delivered(mids)
        if sent_user_mid:
            self.deduplicator.clear_in_flight(sent_user_mid)

        try:
            # Send Muse reply back to QQ
            reply_text = turn_result.text
            # Extract images/files from turn messages if any
            images = []
            files = []
            for msg in turn_result.messages:
                for img in msg.images:
                    if img.data:
                        images.append(img.data)
                for f in msg.files:
                    if f.data:
                        files.append((f.filename, f.data))

            await self._send_reply(target_type, target_id, reply_text, images, files, msg_id=msg_id)
            if mids:
                self.deduplicator.mark_delivered(mids)
        except Exception as e:
            if mids:
                self.deduplicator.rollback_processing(mids)
            print(f"[QQAdapter] Send reply failed: {e}")

    async def _send_reply(
        self,
        target_type: str,
        target_id: str,
        text: str,
        images: Optional[List[bytes]] = None,
        files: Optional[List[Tuple[str, bytes]]] = None,
        msg_id: Optional[str] = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        chunks = chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []
        passive_limit = 4 if target_type == "private" else 5
        active_budget = self.config.max_active_chunks
        seq = 1

        # Send text chunks
        for i, chunk in enumerate(chunks):
            use_passive = bool(msg_id and seq <= passive_limit)
            curr_msg_id = msg_id if use_passive else None
            curr_seq = seq if use_passive else None

            if not use_passive:
                if active_budget <= 0:
                    break
                active_budget -= 1

            code, body = await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                chunk,
                chunk,  # markdown content
                None,   # media file info
                curr_msg_id,
                curr_seq,
            )

            # Retry active if passive failed
            if use_passive and not (200 <= code < 300) and active_budget > 0:
                active_budget -= 1
                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    chunk,
                    chunk,
                )
            seq += 1

        # Send images
        for img_bytes in images or []:
            try:
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    img_bytes,
                    1,
                    "image.jpg",
                )
                use_passive = bool(msg_id and seq <= passive_limit)
                curr_msg_id = msg_id if use_passive else None
                curr_seq = seq if use_passive else None

                if not use_passive:
                    if active_budget <= 0:
                        break
                    active_budget -= 1

                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    None,
                    None,
                    file_info,
                    curr_msg_id,
                    curr_seq,
                )
                seq += 1
            except Exception as e:
                print(f"[QQAdapter] Failed to send image: {e}")

        # Send files
        for fname, f_bytes in files or []:
            try:
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    f_bytes,
                    4,
                    fname,
                )
                use_passive = bool(msg_id and seq <= passive_limit)
                curr_msg_id = msg_id if use_passive else None
                curr_seq = seq if use_passive else None

                if not use_passive:
                    if active_budget <= 0:
                        break
                    active_budget -= 1

                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    target_type,
                    target_id,
                    None,
                    None,
                    file_info,
                    curr_msg_id,
                    curr_seq,
                )
                seq += 1
            except Exception as e:
                print(f"[QQAdapter] Failed to send file: {e}")
