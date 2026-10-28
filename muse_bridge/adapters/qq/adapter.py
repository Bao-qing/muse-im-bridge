"""QQ official bot adapter.

Completely decouples Inbound message reception from Outbound delivery:
1. Inbound Router: Receives QQ messages, downloads attachments, registers route,
   and fires-and-forgets to Muse immediately (takes ~0.2s without blocking).
2. Outbound Pusher: Subscribes to Muse event stream, listens for completed assistant events,
   resolves destination via RouteManager, and sends replies / proactive pushes to QQ.
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Tuple

from ...config.models import QQConfig
from ...core.commands import CommandError, CommandHandler
from ...core.deduplicator import MessageDeduplicator
from ...core.message import Attachment
from ...muse.service import MuseService
from ..base import BaseAdapter
from .client import QQClient
from .gateway import QQGateway
from .outbound import QQOutboundPusher
from .route_manager import RouteManager


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
            user_agent=self.config.user_agent,
        )

        self.deduplicator = deduplicator or MessageDeduplicator(
            persistence_file=self.config.delivered_file,
            in_flight_ttl_s=self.config.push.in_flight_ttl_s,
            processing_ttl_s=self.config.push.processing_ttl_s,
            gateway_dedup_s=self.config.gateway_dedup_s,
        )

        self.route_manager = RouteManager(ttl_s=600.0)

        self.active_session_id = CommandHandler.load_session_id(
            self.config.session_state_file,
            self.config.session_id,
        )

        # 1. Inbound Gateway (receives QQ events)
        self.gateway = QQGateway(
            client=self.client,
            on_event=self.handle_event,
            proxy_url=self.config.proxy_url,
            debug=self.config.debug,
            ws_guid=self.config.ws_guid,
            user_agent=self.config.user_agent,
        )

        # 2. Outbound Dispatcher (sole exit to QQ for both replies & proactive pushes)
        self.outbound_pusher = QQOutboundPusher(
            config=self.config,
            client=self.client,
            muse_service=self.muse_service,
            deduplicator=self.deduplicator,
            route_manager=self.route_manager,
            session_id=self.active_session_id,
        )

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
        print(f"[QQAdapter] Starting decoupled QQ Adapter (AppID: {self.config.app_id})...")

        tasks = [
            asyncio.create_task(self.gateway.start()),
            asyncio.create_task(self.outbound_pusher.start()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        await self.gateway.stop()
        await self.outbound_pusher.stop()

    def is_allowed(self, event_type: str, d: dict) -> bool:
        """Check sender against whitelist."""
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
                print(f"[QQAdapter] Duplicate gateway message ignored: {msg_id}")
            return

        if not self.is_allowed(event_type, d):
            if self.config.debug:
                print(f"[QQAdapter] Ignored event {event_type}: sender not in whitelist")
            return

        raw_content = (d.get("content") or "").strip()
        content = re.sub(r"<@!?\w+>", "", raw_content).strip()
        attachments = self.extract_attachments(d)

        if not content and not attachments:
            return

        target_type = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
        target_id = d.get("author", {}).get("user_openid") if target_type == "private" else d.get("group_openid")

        print(f"[QQAdapter] Inbound ({event_type}) from {target_type}:{target_id}: {content}")

        # 1. Local Slash Commands
        parsed = CommandHandler.parse_command(content)
        if parsed is not None:
            cmd_name, cmd_args = parsed
            loop = asyncio.get_running_loop()
            client = None
            try:
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
                    self.outbound_pusher.session_id = new_sid
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

            # Reply command output directly
            await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                reply,
                reply,
                None,
                msg_id,
            )
            return

        # 2. Asynchronous User Message Ingestion (Non-blocking!)
        loop = asyncio.get_running_loop()

        # Download attachments if any
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

        try:
            # Post message to Muse immediately without waiting for generation!
            sent_mid = await loop.run_in_executor(
                None,
                self.muse_service.post_message,
                self.active_session_id,
                content,
                attachments,
                self.config.file_ref_threshold_bytes,
            )

            # Register route for outbound pusher
            self.route_manager.register(sent_mid, target_type, target_id, msg_id)
            if self.config.debug:
                print(f"[QQAdapter] Message delivered to Muse (mid={sent_mid}), route registered for {target_type}:{target_id}")
        except Exception as e:
            print(f"[QQAdapter] Failed to post message to Muse: {e}")
            await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                f"转接出错了：{e}",
                f"转接出错了：{e}",
                None,
                msg_id,
            )
