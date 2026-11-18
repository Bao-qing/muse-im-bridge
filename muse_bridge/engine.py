"""Composition root: one business gateway and mounted platform drivers."""
from __future__ import annotations

from .adapters.onebot import OneBotAdapter
from .adapters.qq import QQAdapter
from .gateway.runtime import UnifiedGateway
from .muse.service import MuseService
from .muse.token_manager import MuseTokenManager


class BridgeEngine:
    def __init__(self, config):
        self.config = config
        self.token_manager = MuseTokenManager(
            mint_file=config.muse.mint_file, cookies_file=config.muse.cookies_file,
            vm_id=config.muse.vm_id, base_url="https://muse.ai", worker=config.muse.worker)
        self.muse_service = MuseService(config.muse, self.token_manager)
        self.gateway = UnifiedGateway(self.muse_service, config.gateway)
        self.adapters = []
        if config.qq.enabled:
            self.adapters.append(self.gateway.mount(QQAdapter(config.qq)))
        if config.onebot.enabled:
            self.adapters.append(self.gateway.mount(OneBotAdapter(config.onebot)))
        if config.wechat.enabled:
            from .adapters.wechat.adapter import WeChatAdapter
            self.adapters.append(self.gateway.mount(WeChatAdapter(config.wechat)))
        self._running = False

    async def start(self):
        if not any(a.is_enabled for a in self.adapters):
            return
        self._running = True
        try:
            await self.gateway.start()
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.gateway.stop()
