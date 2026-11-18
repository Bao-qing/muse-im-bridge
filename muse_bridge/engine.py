"""Composition root: one business gateway, mounted platform drivers and optional jobs."""
from __future__ import annotations

import asyncio

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
        self.jobs = []
        if config.onebot.enabled and config.onebot.qzone_feed.enabled:
            from .adapters.onebot.qzone import QzoneFeedWorker
            adapter = next(a for a in self.adapters if a.name == "onebot")
            self.jobs.append(QzoneFeedWorker(config.onebot.qzone_feed, adapter.client,
                                            self.muse_service, max_image_bytes=config.onebot.max_image_bytes))
        self._running = False
        self._job_tasks = []

    async def start(self):
        if not any(a.is_enabled for a in self.adapters):
            return
        self._running = True
        self._job_tasks = [asyncio.create_task(job.run()) for job in self.jobs]
        try:
            await self.gateway.start()
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.gateway.stop()
        await asyncio.gather(*(job.stop() for job in self.jobs), return_exceptions=True)
        for task in self._job_tasks:
            task.cancel()
        await asyncio.gather(*self._job_tasks, return_exceptions=True)
        self._job_tasks = []
