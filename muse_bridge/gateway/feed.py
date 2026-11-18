"""Gateway-owned Muse feed polling, rate limiting, reservation ledger and routing.

Platform adapters never poll Muse or choose chat recipients:
- ``native`` mode delegates to ``adapter.publish_feed(FeedEnvelope)`` (e.g. Qzone).
- ``chat`` mode delivers rendered feed posts to ``ctx.notification_target()`` via
  the gateway's existing outbound envelope pipeline.

Every post is reserved durably BEFORE sending; uncertain or failed outcomes are
never automatically retried so public timelines and private chats are not flooded.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from ..core.delivery import DeliveryEnvelope, FeedEnvelope
from ..observability import get_logger, safe_error

logger = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class _FeedTargetPlan:
    adapter: object
    mode: str  # "native" or "chat"
    poll_interval_s: int
    max_age_s: int
    scan_limit: int
    max_per_scan: int
    min_publish_interval_s: int
    publish_window_s: int
    max_per_window: int


def _feed_images_to_descriptors(images: list[str]) -> list[dict]:
    descriptors: list[dict] = []
    for idx, item in enumerate(images, start=1):
        if item.startswith("base64://"):
            raw = base64.b64decode(item[len("base64://"):])
            ext = ("png" if raw.startswith(b"\x89PNG\r\n\x1a\n")
                   else "gif" if raw.startswith((b"GIF87a", b"GIF89a"))
                   else "webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
                   else "bmp" if raw.startswith(b"BM")
                   else "jpg")
            descriptors.append({"data": raw, "filename": f"feed-{idx}.{ext}"})
        else:
            descriptors.append({"url": item, "filename": f"feed-{idx}.jpg"})
    return descriptors


class GatewayFeedCoordinator:
    def __init__(self, gateway, *, clock: Callable[[], float] = time.time):
        self.gateway = gateway
        self.muse_service = gateway.muse_service
        self.clock = clock
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def _plans(self) -> list[_FeedTargetPlan]:
        gw_feed = self.gateway.config.feed
        enabled_adapters = [a for a in self.gateway.adapters.values() if a.is_enabled]
        if gw_feed.enabled:
            if gw_feed.targets:
                pairs = [(a, gw_feed.targets[a.name]) for a in enabled_adapters if a.name in gw_feed.targets]
            else:
                pairs = [(a, "auto") for a in enabled_adapters]
            plans = []
            for adapter, raw_mode in pairs:
                if raw_mode == "auto":
                    mode = "native" if adapter.supports_native_feed() else "chat"
                elif raw_mode == "native":
                    if not adapter.supports_native_feed():
                        raise ValueError(f"Adapter {adapter.name} does not support native feed publishing")
                    mode = "native"
                else:
                    mode = "chat"
                plans.append(_FeedTargetPlan(
                    adapter=adapter,
                    mode=mode,
                    poll_interval_s=gw_feed.poll_interval_s,
                    max_age_s=gw_feed.max_age_s,
                    scan_limit=gw_feed.scan_limit,
                    max_per_scan=gw_feed.max_per_scan,
                    min_publish_interval_s=gw_feed.min_publish_interval_s,
                    publish_window_s=gw_feed.publish_window_s,
                    max_per_window=gw_feed.max_per_window,
                ))
            return plans

        plans = []
        for adapter in enabled_adapters:
            qzone = getattr(adapter.config, "qzone_feed", None)
            if qzone is not None and qzone.enabled:
                if not adapter.supports_native_feed():
                    raise ValueError(f"Adapter {adapter.name} does not support native feed publishing")
                plans.append(_FeedTargetPlan(
                    adapter=adapter,
                    mode="native",
                    poll_interval_s=qzone.poll_interval_s,
                    max_age_s=qzone.max_age_s,
                    scan_limit=qzone.scan_limit,
                    max_per_scan=qzone.max_per_scan,
                    min_publish_interval_s=qzone.min_publish_interval_s,
                    publish_window_s=qzone.publish_window_s,
                    max_per_window=qzone.max_per_window,
                ))
        return plans

    @property
    def is_enabled(self) -> bool:
        return bool(self._plans())

    async def stop(self) -> None:
        self._stop.set()

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def run(self) -> None:
        plans = self._plans()
        if not plans:
            return
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Gateway feed scan failed: %s", safe_error(exc))
            plans = self._plans()
            interval = min((p.poll_interval_s for p in plans), default=300)
            if not await self._wait(interval):
                break

    @staticmethod
    def _state_spec(ctx, mode: str, source_id: str) -> tuple[Path, dict]:
        account_id = ctx.account_identity()
        qzone = getattr(ctx.config, "qzone_feed", None)
        if ctx.name == "onebot" and mode == "native" and qzone is not None:
            # Preserve exact legacy Qzone state path and identity format across upgrades.
            return Path(qzone.state_file), {"onebot": str(account_id), "muse": source_id}
        base = ctx.config.routes_file or f"data/{ctx.name}_routes.json"
        return (
            Path(f"{base}.feed.{mode}.local.json"),
            {"platform": ctx.name, "account": str(account_id), "mode": mode, "muse": source_id},
        )

    @staticmethod
    def _load(path: Path, identity: dict) -> dict:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "identity": identity, "attempts": {}}
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("identity") != identity
                or not isinstance(state.get("attempts"), dict)):
            raise ValueError("Feed state is corrupt or belongs to another account")
        for key, value in state["attempts"].items():
            if (not isinstance(key, str) or not key or type(value) not in (int, float)
                    or not math.isfinite(value) or value < 0):
                raise ValueError("Feed state contains invalid attempts")
        return state

    @staticmethod
    def _save(path: Path, state: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", delete=False) as out:
                name = out.name
                json.dump(state, out, ensure_ascii=False, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    async def _ensure_context(self, adapter):
        if not adapter.account_identity():
            await adapter.prepare()
        self.gateway.identity_changed(adapter)
        ctx = self.gateway.contexts.get(adapter)
        account_id = ctx.account_identity() if ctx is not None else ""
        if not account_id or (adapter.name == "onebot" and not str(account_id).isdigit()):
            raise ValueError(f"Cannot identify the {adapter.name} feed publishing account")
        return ctx

    async def scan_once(self) -> int:
        plans = self._plans()
        if not plans or self._stop.is_set():
            return 0
        async with self._lock:
            resolved_targets = []
            for plan in plans:
                ctx = await self._ensure_context(plan.adapter)
                recipient = None
                if plan.mode == "chat":
                    recipient = ctx.notification_target()
                    if not recipient:
                        raise ValueError(f"Adapter {ctx.name} has no notification recipient for chat feed")
                resolved_targets.append((plan, ctx, recipient))

            max_scan = max(plan.scan_limit for plan, _, _ in resolved_targets)
            page = await asyncio.to_thread(self.muse_service.list_feed, max_scan)
            if not page.source_id:
                raise ValueError("Cannot identify the Muse feed source")

            image_cache: dict[tuple[str, int], list[str]] = {}
            total_published = 0
            first_error: Exception | None = None

            for plan, ctx, recipient in resolved_targets:
                if self._stop.is_set():
                    break
                try:
                    total_published += await self._publish_for_target(
                        plan, ctx, recipient, page, image_cache)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    if len(resolved_targets) > 1:
                        logger.warning("Feed target %s (%s) failed: %s", ctx.name, plan.mode, safe_error(exc))

            if first_error is not None:
                raise first_error
            return total_published

    async def _publish_for_target(self, plan: _FeedTargetPlan, ctx, recipient, page, image_cache) -> int:
        state_path, identity = self._state_spec(ctx, plan.mode, page.source_id)
        state = await asyncio.to_thread(self._load, state_path, identity)
        attempts = state["attempts"]
        now = self.clock()
        retention = max(plan.max_age_s, plan.publish_window_s, plan.min_publish_interval_s)
        attempts = {key: ts for key, ts in attempts.items() if ts >= now - retention}
        state["attempts"] = attempts
        posts = page.posts[:plan.scan_limit]
        candidates = [post for post in posts
                      if post.id not in attempts
                      and 0 <= now - post.created_at <= plan.max_age_s
                      and post.text.strip()]
        candidates.sort(key=lambda post: (post.created_at, post.id), reverse=True)
        candidates = list(reversed(candidates[:plan.max_per_scan]))
        published = 0

        for post in candidates:
            if self._stop.is_set():
                break
            if post.id in attempts:
                continue
            now = self.clock()
            if not 0 <= now - post.created_at <= plan.max_age_s:
                continue
            recent = [ts for ts in attempts.values() if ts > now - plan.publish_window_s]
            if len(recent) >= plan.max_per_window:
                break
            delay = max(0, max(attempts.values(), default=0) + plan.min_publish_interval_s - now)
            if delay and not await self._wait(delay):
                break
            now = self.clock()
            if now - post.created_at > plan.max_age_s:
                continue
            if len(attempts) >= 10000:
                raise ValueError("Feed state capacity reached; refusing to discard deduplication")

            max_img = ctx.config.max_image_bytes
            cache_key = (post.id, max_img)
            if post.images:
                if cache_key not in image_cache:
                    image_cache[cache_key] = await asyncio.to_thread(
                        self.muse_service.prepare_feed_images, post, max_img)
                images = image_cache[cache_key]
            else:
                images = []

            if self._stop.is_set():
                break
            now = self.clock()
            if now - post.created_at > plan.max_age_s:
                continue

            attempts[post.id] = now
            # Persistence failure MUST prevent this send.
            await asyncio.to_thread(self._save, state_path, state)
            if self._stop.is_set():
                break

            if plan.mode == "native":
                result = await ctx.adapter.publish_feed(
                    FeedEnvelope(post.id, post.text, images, post.created_at))
                if not result.confirmed:
                    logger.warning("Native feed publish unconfirmed on %s (%s); reserved post will not be retried",
                                   ctx.name, result.reason or result.status.value)
                    raise RuntimeError(result.reason or "Native feed publishing failed or outcome unknown")
            else:
                envelope = DeliveryEnvelope(
                    delivery_id=f"feed:{ctx.account_identity()}:{page.source_id}:{post.id}",
                    target_type="private",
                    target_id=recipient,
                    text="【动态】\n" + post.text.strip(),
                    images=_feed_images_to_descriptors(images),
                    purpose="proactive",
                )
                result = await ctx.outbound_pusher.send_envelope(envelope)
                if not result.confirmed:
                    logger.warning("Chat feed delivery unconfirmed on %s (%s); reserved post will not be retried",
                                   ctx.name, result.reason or result.status.value)
                    raise RuntimeError(result.reason or "Chat feed delivery failed or outcome unknown")

            published += 1
            logger.info("Published Muse feed item via %s [%s] (%d this scan)",
                        ctx.name, plan.mode, published)
        return published
