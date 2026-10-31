"""OneBot (NapCat) Adapter.

Coordinates:
- OneBotClient (HTTP API message sending using Array format).
- OneBotGateway (WebSocket real-time event reception).
- MuseService integration.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ...config.models import OneBotConfig
from ...core.message import Attachment
from ...muse.service import MuseService
from ...muse.interactions import ApprovalBlocked
from ...observability import get_logger, safe_error, trace_event
from ..base import BaseAdapter
from .client import OneBotClient
from .gateway import OneBotGateway

logger = get_logger(__name__)


class OneBotAdapter(BaseAdapter):
    def __init__(self, config: OneBotConfig, muse_service: MuseService):
        self.config = config
        self.muse_service = muse_service
        self.client = OneBotClient(base_url=self.config.base_url, access_token=self.config.access_token)
        self.gateway = OneBotGateway(
            ws_url=self.config.ws_url,
            access_token=self.config.access_token,
            on_message=self.handle_message,
        )
        self.active_session_id = self.config.session_id
        if self.active_session_id:
            self.muse_service.interactions.managed_sessions.add(self.active_session_id)

    @property
    def name(self) -> str:
        return "onebot"

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url)

    async def start(self) -> None:
        if not self.is_enabled:
            return
        logger.info('Starting OneBot Adapter (%s)...', self.config.base_url)
        await self.gateway.start()

    async def stop(self) -> None:
        await self.gateway.stop()

    def is_allowed(self, event: dict) -> bool:
        """Check user_id or group_id against whitelist if configured."""
        wl = self.config.whitelist
        if not wl:
            return True  # If no whitelist specified, allow

        msg_type = event.get("message_type")
        if msg_type == "group":
            gid = event.get("group_id")
            groups = wl.get("groups") or []
            return "*" in groups or gid in groups
        elif msg_type == "private":
            uid = event.get("user_id")
            privates = wl.get("private") or []
            return "*" in privates or uid in privates
        return False

    async def handle_message(self, event: dict) -> None:
        if not self.is_allowed(event):
            return

        raw_message = event.get("raw_message", "")
        msg_type = event.get("message_type")
        group_id = event.get("group_id")
        user_id = event.get("user_id")

        if not raw_message.strip():
            return

        # Forward turn to MuseService
        loop = asyncio.get_running_loop()
        try:
            turn_result = await loop.run_in_executor(
                None,
                self.muse_service.send_turn,
                self.active_session_id,
                raw_message,
            )
            trace_event("onebot.adapter", "turn.completed", session_id=self.active_session_id,
                        msg_id=event.get("message_id"), target_type=msg_type,
                        group_id=group_id, user_id=user_id)
            reply_text = turn_result.text
            if msg_type == "group" and group_id:
                await loop.run_in_executor(None, self.client.send_group_msg, group_id, reply_text)
            elif msg_type == "private" and user_id:
                await loop.run_in_executor(None, self.client.send_private_msg, user_id, reply_text)
        except ApprovalBlocked as e:
            if msg_type == "group" and group_id:
                await loop.run_in_executor(None, self.client.send_group_msg, group_id, str(e))
            elif msg_type == "private" and user_id:
                await loop.run_in_executor(None, self.client.send_private_msg, user_id, str(e))
        except Exception as e:
            logger.error('Turn failed: %s', safe_error(e))
