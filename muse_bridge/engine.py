"""Core Bridge Engine orchestrating MuseService and all enabled adapters."""

from __future__ import annotations

import asyncio
from typing import List

from .adapters.base import BaseAdapter
from .adapters.onebot import OneBotAdapter
from .adapters.qq import QQAdapter
from .config.models import AppConfig
from .gateway.chat import ChatGateway
from .muse.service import MuseService
from .muse.token_manager import MuseTokenManager
from .observability import get_logger, safe_error


logger = get_logger(__name__)


class BridgeEngine:
    def __init__(self, config: AppConfig):
        self.config = config

        # 1. Muse Service Layer
        self.token_manager = MuseTokenManager(
            mint_file=self.config.muse.mint_file,
            cookies_file=self.config.muse.cookies_file,
            vm_id=self.config.muse.vm_id,
            base_url="https://muse.ai",
        )
        self.muse_service = MuseService(config=self.config.muse, token_manager=self.token_manager)

        # 2. Adapters
        self.adapters: List[BaseAdapter] = []
        if self.config.qq.enabled:
            self.adapters.append(QQAdapter(config=self.config.qq, muse_service=self.muse_service))
        if self.config.onebot.enabled:
            self.adapters.append(OneBotAdapter(config=self.config.onebot, muse_service=self.muse_service))

        self._running = False

    async def start(self) -> None:
        self._running = True
        enabled_adapters = [a for a in self.adapters if a.is_enabled]
        if not enabled_adapters:
            logger.warning("No adapters enabled. Nothing to run.")
            return

        logger.info("Starting %s adapter(s): %s", len(enabled_adapters), [a.name for a in enabled_adapters])

        # Verify Muse credentials on startup before launching adapters to avoid
        # concurrent notary_token minting/handshake races with outbound pushers
        loop = asyncio.get_running_loop()
        try:
            cred_ok = await loop.run_in_executor(None, self.muse_service.verify_connection)
            login_required = not cred_ok
        except Exception as exc:
            cred_ok = False
            login_required = False
            logger.warning("启动验证暂不可用，保留 Cookie，后台重试。 error=%s", safe_error(exc))
        if login_required:
            logger.warning("启动验证：Muse 会话凭据未就绪或已失效，进入登录回落流程。")
            for adapter in self.adapters:
                if isinstance(adapter, ChatGateway):
                    adapter.enter_login_mode()
            login_adapter = self._login_adapter()
            if login_adapter:
                admin_target = login_adapter.config.admin_target()
                if admin_target:
                    asyncio.create_task(self._notify_admin_login(login_adapter, admin_target))
        elif cred_ok:
            logger.info('启动验证：Muse 会话凭据有效，认证通过。')

        if cred_ok:
            try:
                await self.muse_service.interaction_protocol.reconcile()
            except Exception as exc:
                logger.warning("审批状态尚未就绪，普通消息保持门禁。 error=%s", safe_error(exc))

        tasks = [asyncio.create_task(a.start()) for a in enabled_adapters]
        tasks.append(asyncio.create_task(self.muse_service.interaction_protocol.run(
            self._notify_interaction, lambda: self._running)))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def _notify_interaction(self, item, text):
        """Push a card through the adapters whose chats own its session.

        A session no chat owns (e.g. the configured main session) goes to every
        adapter, which then falls back to its own admin.
        """
        chats = [a for a in self.adapters if isinstance(a, ChatGateway) and a.is_enabled]
        owners = [a for a in chats if a.owns_session(item.session_id)]
        delivered = False
        for adapter in owners or chats:
            delivered = await adapter.notify_interaction(item, text) or delivered
        return delivered

    def _login_adapter(self):
        """Adapter whose admin is asked to log in (``gateway.login_through_adapter``)."""
        name = self.config.gateway.login_through_adapter
        return next((a for a in self.adapters if isinstance(a, ChatGateway) and a.name == name), None)

    async def _notify_admin_login(self, adapter, admin_target: str) -> None:
        await asyncio.sleep(4)  # Wait for the chat gateway to connect
        try:
            await adapter.send_notice(
                admin_target,
                "【系统提示】Muse 会话凭据未就绪或已失效，已进入登录回落流程。\n您可直接在此私聊发送：\n/login <您的邮箱>\n即可获取验证码完成在线认证",
            )
            adapter.enter_login_mode(admin_target)
        except Exception as e:
            logger.debug('Failed to notify admin for login: %s', safe_error(e))

    async def stop(self) -> None:
        self._running = False
        self.muse_service.interactions.close()
        logger.info('Stopping all adapters...')
        stop_tasks = [a.stop() for a in self.adapters if a.is_enabled]
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
