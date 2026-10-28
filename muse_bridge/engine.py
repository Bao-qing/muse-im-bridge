"""Core Bridge Engine orchestrating MuseService and all enabled adapters."""

from __future__ import annotations

import asyncio
from typing import List

from .adapters.base import BaseAdapter
from .adapters.onebot import OneBotAdapter
from .adapters.qq import QQAdapter
from .config.models import AppConfig
from .muse.service import MuseService
from .muse.token_manager import MuseTokenManager


class BridgeEngine:
    def __init__(self, config: AppConfig):
        self.config = config

        # 1. Muse Service Layer
        self.token_manager = MuseTokenManager(
            mint_file=self.config.muse.mint_file,
            cookies_file=self.config.muse.cookies_file,
            vm_id=self.config.muse.vm_id,
            base_url=f"https://{self.config.muse.host}" if "://" not in self.config.muse.host else self.config.muse.host,
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
            print("[BridgeEngine] No adapters enabled. Nothing to run.")
            return

        print(f"[BridgeEngine] Starting {len(enabled_adapters)} adapter(s): {[a.name for a in enabled_adapters]}")
        tasks = [asyncio.create_task(a.start()) for a in enabled_adapters]

        # Check login_through_adapter on startup if credentials need attention
        if self.config.gateway.login_through_adapter == "qq" and not self.token_manager.is_fresh():
            admin_target = self.config.qq.get_default_private_target()
            if admin_target:
                asyncio.create_task(self._notify_admin_login(admin_target))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def _notify_admin_login(self, admin_target: str) -> None:
        await asyncio.sleep(5)  # Wait for QQ gateway to connect
        qq_adapter = next((a for a in self.adapters if isinstance(a, QQAdapter)), None)
        if qq_adapter:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    qq_adapter.client.send_message,
                    "private",
                    admin_target,
                    "【系统提示】Muse 会话凭据未就绪或 Cookies 已失效。\n您可直接在此私聊发送：\n/login <您的邮箱>\n即可快速完成在线登录认证并恢复服务 ♪",
                )
            except Exception as e:
                if self.config.debug:
                    print(f"[BridgeEngine] Failed to notify admin for login: {e}")

    async def stop(self) -> None:
        self._running = False
        print("[BridgeEngine] Stopping all adapters...")
        stop_tasks = [a.stop() for a in self.adapters if a.is_enabled]
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
