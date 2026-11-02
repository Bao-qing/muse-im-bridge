"""Platform-neutral outbound dispatcher: the sole exit from Muse to a chat.

Unifies interactive replies and proactive notifications for every adapter:
1. Listens for completed assistant events from the Muse /chat/subscribe event stream.
2. Extracts text, images (URLs & workspace), files (workspace & attachments), and videos.
3. Automatically reads file-store bytes via client.read_file().
4. Resolves the destination via RouteManager (the chat that asked, else the
   chat bound to the session, else the first admin privately).
5. Hands the item to the platform's ``_deliver`` (QQ upload API, OneBot segments, ...).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.models import ChatConfig
from ..core.deduplicator import MessageDeduplicator
from ..core.message import chunk_text
from ..core.session_map import SessionMapper
from ..core.state_file import ensure_parent
from ..muse.client.errors import ConnectionLost, HandshakeError, MuseError, RPCError, SubscribeError, LoginRequired, is_runtime_auth_error
from ..muse.client.push import PushTurnTracker
from ..muse.client.render import (
    extract_files,
    extract_images,
    extract_workspace_files,
    extract_workspace_images,
    extract_workspace_videos,
    render_widget_to_text,
    render_presentation_card_to_text,
)
from ..observability import get_logger, safe_error, trace_event
from ..muse.service import MuseService
from .routes import RouteInfo, RouteManager

logger = get_logger(__name__)

_SANDBOX_MEDIA_RE = re.compile(r"!?\[([^\]]*)\]\(sandbox://([^)]+)\)")
_WIDGET_RE = re.compile(r"\[\[hatch_widget:[^\]]+\]\]")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def _clean_text(text: str, widgets: Optional[List[dict]] = None) -> str:
    cleaned = _SANDBOX_MEDIA_RE.sub("", text)
    if widgets:
        w_map = {w["widget_id"]: w for w in widgets if isinstance(w, dict) and w.get("widget_id")}
        def _replace_widget(m):
            wid = m.group(1)
            w = w_map.get(wid)
            if w:
                rendered = render_widget_to_text(w)
                if rendered:
                    return f"\n\n{rendered}\n\n"
            return ""
        cleaned = re.sub(r"\[\[hatch_widget:([^\]]+)\]\]", _replace_widget, cleaned)
    else:
        cleaned = _WIDGET_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_media_descriptors(event: dict, raw_text: str) -> Tuple[List[dict], List[dict], List[dict]]:
    images = []
    files = []
    videos = []

    # 1. From event payload
    images.extend(extract_images(event) + extract_workspace_images(event))
    files.extend(extract_files(event) + extract_workspace_files(event))
    videos.extend(extract_workspace_videos(event))

    # 2. From sandbox:// markdown links in text
    if raw_text:
        for alt, uri in _SANDBOX_MEDIA_RE.findall(raw_text):
            path = uri.strip().lstrip("/")
            base = path.lower().split("?", 1)[0]
            if base.endswith(_VIDEO_EXTS):
                videos.append({"workspace_path": path, "filename": alt or "video.mp4"})
            elif base.endswith(_IMAGE_EXTS):
                images.append({"workspace_path": path, "filename": alt or "image.jpg"})
            else:
                files.append({
                    "workspace_path": path,
                    "filename": alt or os.path.basename(path) or "file.dat",
                })

    def _dedup(items):
        seen = set()
        out = []
        for it in items:
            key = it.get("url") or it.get("workspace_path")
            if key and key not in seen:
                seen.add(key)
                out.append(it)
        return out

    return _dedup(images), _dedup(files), _dedup(videos)


def gate_catch_up(
    hist: List[dict],
    seen_ids: Set[str],
    last_ts: Optional[float],
    now: float,
    enabled: bool,
    window_s: int,
    max_messages: int,
) -> Tuple[Set[str], str, int, int, int, str]:
    """Startup catch-up gate."""
    all_assistant_ids = {
        e.get("message_id")
        for e in hist
        if e.get("role") == "assistant" and e.get("message_id")
    }
    max_seq = max([e.get("seq") or 0 for e in hist] or [0])

    if not enabled:
        reason = "catch-up disabled"
    elif last_ts is None:
        reason = "no last-run timestamp"
    elif window_s <= 0 or max_messages <= 0:
        reason = "catch-up window/max is 0"
    elif now - last_ts > window_s:
        reason = f"gap {now - last_ts:.0f}s exceeds window {window_s}s"
    else:
        reason = ""

    if reason:
        return seen_ids | all_assistant_ids, "baseline", 0, len(all_assistant_ids - seen_ids), max_seq, reason

    unseen = sorted(
        [
            e
            for e in hist
            if e.get("role") == "assistant"
            and e.get("message_id")
            and e.get("message_id") not in seen_ids
        ],
        key=lambda e: e.get("seq") or 0,
    )
    kept = unseen[-max_messages:]
    kept_ids = {e.get("message_id") for e in kept}
    skipped = {e.get("message_id") for e in unseen} - kept_ids

    return seen_ids | skipped, "catch_up", len(kept_ids), len(skipped), max_seq, ""


class OutboundDispatcher:
    """Subclasses implement ``_deliver`` and ``_send_notice`` for their platform."""

    platform = "chat"

    def __init__(
        self,
        config: ChatConfig,
        client: Any,
        muse_service: MuseService,
        deduplicator: MessageDeduplicator,
        route_manager: RouteManager,
        session_mapper: Optional[SessionMapper] = None,
        on_login_required: Optional[Callable[[Optional[str]], None]] = None,
    ):
        self.config = config
        self.client = client
        self.muse_service = muse_service
        self.deduplicator = deduplicator
        self.route_manager = route_manager
        self._on_login_required = on_login_required
        self.session_mapper = session_mapper or SessionMapper()

        self._running = False
        self._login_alerted: bool = False
        self._seen_ids: Set[str] = set()
        self._last_seq: int = 0
        self._last_ts: Optional[float] = None

        # Per-session watch & subscription state
        self._gated: Set[str] = set()                 # sessions whose startup gate already ran
        self._last_updated: Dict[str, int] = {}       # sid -> last seen updated_at_ms
        self._hot_until: Dict[str, float] = {}        # sid -> keep active until this monotonic ts
        self._last_sweep: float = 0.0
        self._pending_subscribe: Set[str] = set()     # sessions that need /chat/subscribe opened
        self._uncreated_sessions: Set[str] = set()    # lazy sessions (404 until first message is posted)
        self._load_state()

    def track_session(self, session_id: str, fresh: bool = False) -> None:
        """Called by the adapter right after creating or posting into ``session_id``.

        ``fresh`` marks a session created just now (nothing to baseline, not on server yet).
        When ``fresh=False`` (called after ``post_message`` succeeds), the session now exists
        on the Muse server and is queued for immediate ``/chat/subscribe`` registration.
        """
        if not session_id:
            return
        if fresh:
            self._gated.add(session_id)
            self._uncreated_sessions.add(session_id)
        else:
            # A message was just posted to this session -> it now exists on server!
            self._uncreated_sessions.discard(session_id)
            self._pending_subscribe.add(session_id)
        self._hot_until[session_id] = time.monotonic() + max(5, self.config.session_hot_window_s)

    def _load_state(self) -> None:
        state_file = self.config.push_state_file
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_seq = int(data.get("last_seq", 0))
                    self._seen_ids = set(data.get("seen_ids", []))
                    self._last_ts = data.get("updated_ts")
            except Exception:
                pass

    def _save_state(self) -> None:
        state_file = self.config.push_state_file
        if not state_file:
            return
        tmp = f"{state_file}.tmp.{os.getpid()}"
        try:
            ensure_parent(state_file)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "last_seq": self._last_seq,
                        "seen_ids": list(self._seen_ids)[-2000:],
                        "updated_ts": time.time(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, state_file)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    async def start(self) -> None:
        self._running = True
        logger.info('Started %s outbound dispatcher (unbound sessions fall back to admin: %s)', self.platform,
                    "yes" if self.config.unbound_push_to_admin and self.config.admin_target() else "no")

        while self._running:
            try:
                await self._run_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.debug('Event stream loop error: %s, reconnecting in 3s...', safe_error(e))
                await asyncio.sleep(3.0)

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # History fetching & media extraction (triggered on demand by events)
    # ------------------------------------------------------------------
    def _fetch_session(self, client, session_id: str) -> List[dict]:
        """Fetch authoritative history of one session and read workspace media."""
        try:
            hist = client.get_history(session_id, 25)
            self._uncreated_sessions.discard(session_id)
        except Exception as e:
            msg = str(e).lower()
            if "status 404" in msg or "not_found" in msg or "session not found" in msg:
                # Session not yet created on Muse server (lazy-created upon first message)
                self._uncreated_sessions.add(session_id)
                return []
            raise

        for ev in hist:
            mid = ev.get("message_id")
            if not mid or mid in self._seen_ids or self.deduplicator.is_delivered(mid):
                continue
            if ev.get("role") != "assistant":
                continue
            raw_t = ev.get("display_text") or ""
            imgs, fls, vids = _extract_media_descriptors(ev, raw_t)
            for it in imgs + fls + vids:
                wpath = it.get("workspace_path")
                if wpath and not it.get("data"):
                    try:
                        it["data"] = client.read_file(wpath)
                    except Exception as e:
                        logger.debug('Failed reading workspace media: %s', safe_error(e))
            ev["_extracted_images"] = imgs
            ev["_extracted_files"] = fls
            ev["_extracted_videos"] = vids

        # A still-streaming assistant message keeps the session "hot" until it completes.
        for ev in hist:
            if ev.get("role") == "assistant" and ev.get("event_name") == "message.assistant":
                payload = ev.get("payload") or {}
                status = payload.get("status") or ev.get("status")
                if status != "completed":
                    self._touch_hot(session_id)
                    break
        return hist

    def _touch_hot(self, session_id: str) -> None:
        self._hot_until[session_id] = time.monotonic() + max(5, self.config.session_hot_window_s)

    def _sessions_to_fetch(self, client) -> List[str]:
        """Compatibility helper for initial / fallback session inspection."""
        watched = self.session_mapper.session_ids()
        if not watched:
            return []

        now = time.monotonic()
        sweep_due = now - self._last_sweep >= max(5, self.config.session_sweep_s)
        listing: Optional[Dict[str, int]] = None
        try:
            rows = client.list_sessions()
            listing = {
                r.get("session_id"): int(r.get("updated_at_ms") or 0)
                for r in rows
                if isinstance(r, dict) and r.get("session_id")
            }
        except Exception as e:
            logger.debug('session list failed (%s); falling back to watched sessions', safe_error(e))

        out: List[str] = []
        for sid in watched:
            if sid not in self._gated:
                out.append(sid)
                continue
            if listing is None or sweep_due:
                out.append(sid)
                continue
            upd = listing.get(sid)
            if upd is not None and upd > self._last_updated.get(sid, -1):
                self._last_updated[sid] = upd
                self._touch_hot(sid)
                out.append(sid)
                continue
            if now < self._hot_until.get(sid, 0.0):
                out.append(sid)
        if sweep_due:
            self._last_sweep = now
        if listing is not None:
            for sid in out:
                if sid in listing:
                    self._last_updated[sid] = max(self._last_updated.get(sid, -1), listing[sid])
        return out

    async def ensure_baseline(self, session_id: str) -> None:
        """Mark all existing assistant output of a session as seen.

        Used when a chat is (re)bound to an *existing* Muse session at runtime
        (/switch or a config binding) so old history is never pushed to the chat.
        """
        if not session_id or session_id in self._gated:
            return
        loop = asyncio.get_running_loop()

        def _do():
            with self.muse_service.create_client() as client:
                client.connect()
                return client.get_history(session_id, 25)

        try:
            hist = await loop.run_in_executor(None, _do)
            self._uncreated_sessions.discard(session_id)
            self._pending_subscribe.add(session_id)
        except Exception as e:
            # Session may not exist yet (nothing to baseline) – treat as fresh.
            msg = str(e).lower()
            if "status 404" in msg or "not_found" in msg or "session not found" in msg:
                self._uncreated_sessions.add(session_id)
            logger.debug('baseline fetch for %s failed (%s); treating as empty', session_id[:8], safe_error(e))
            hist = []
        for e in hist:
            if e.get("role") == "assistant" and e.get("message_id"):
                self._seen_ids.add(e["message_id"])
        self._gated.add(session_id)
        self._save_state()

    async def _notify_login_required(self) -> None:
        admin_target = self.config.admin_target()
        if admin_target:
            try:
                await self._send_notice(
                    admin_target,
                    "【系统提示】获取 Muse 消息时检测到凭据失效（401 Unauthorized）。\n已自动进入登录回落流程，请直接在此私聊发送：\n/login <您的邮箱>\n即可获取验证码重新登录恢复服务 ♪",
                )
            except Exception as e:
                logger.debug('Failed to notify admin for login: %s', safe_error(e))
        if self._on_login_required:
            try:
                self._on_login_required(admin_target)
            except Exception:
                pass

    def reset_login_alert(self) -> None:
        self._login_alerted = False

    async def _hydrate_and_dispatch(self, sid: str, hist: List[dict], is_initial_gate: bool) -> None:
        """Apply startup gate if needed, then dispatch any completed assistant messages."""
        if is_initial_gate:
            self._gated.add(sid)
            if not hist:
                return
            seen_ids, action, kept, skipped, max_seq, reason = gate_catch_up(
                hist=hist,
                seen_ids=self._seen_ids,
                last_ts=self._last_ts,
                now=time.time(),
                enabled=self.config.catch_up_enabled,
                window_s=self.config.catch_up_window_s,
                max_messages=self.config.catch_up_max_messages,
            )
            self._seen_ids = seen_ids
            if action == "baseline":
                self._last_seq = max(self._last_seq, max_seq)
                self._save_state()
                logger.debug('Startup baseline %s (%s): marked %s messages seen, 0 pushed.', sid[:8], reason, skipped)
            else:
                logger.info('Startup catch-up %s: recovering up to %s recent message(s) (skipped %s older).', sid[:8], kept, skipped)

        if hist:
            await self._scan_and_dispatch(hist, sid)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        return isinstance(exc, LoginRequired) or is_runtime_auth_error(exc)

    @staticmethod
    def _classify_push_event(ev: dict, default_sid: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Extract (session_id, trigger_kind) from a /chat/subscribe event.

        trigger_kind:
        - "sync": An assistant message or media presentation has finished/arrived and should be synced.
        - "stream": A turn is actively streaming in this session.
        - None: Non-triggering event.
        """
        if not isinstance(ev, dict):
            return None, None
        name = ev.get("event") or ev.get("event_name")
        if not name:
            return None, None

        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        sid = PushTurnTracker._event_session(ev) or default_sid

        if name in ("delta.message_start", "delta.text_append", "delta.tool_start"):
            return sid, "stream"

        if name in ("delta.message_done", "message.assistant"):
            status = payload.get("status") or ev.get("status")
            if status in ("interrupted", "cancelled"):
                return sid, None
            return sid, "sync"

        if name == "delta.presentation":
            # All presentations (media images/files/videos as well as browser cards,
            # credentials vaults, option boxes, etc.) trigger outbound sync.
            return sid, "sync"

        if name == "task.status":
            if payload.get("status") == "completed":
                return sid, "sync"
            return sid, "stream"

        return sid, None

    async def _run_loop(self) -> None:
        """Pure event-driven loop powered by /chat/subscribe over persistent Noise WebSocket."""
        loop = asyncio.get_running_loop()

        while self._running:
            if not await asyncio.to_thread(self.muse_service.token_manager.has_credentials):
                if not self._login_alerted:
                    self._login_alerted = True
                    logger.warning('Muse 尚未登录或凭据失效，暂停出站监听，触发回落登录...')
                    await self._notify_login_required()
                await asyncio.sleep(5.0)
                continue

            client = None
            try:
                def _connect_client():
                    c = self.muse_service.create_client()
                    try:
                        c.connect()
                        return c
                    except Exception:
                        c.close()
                        raise

                client = await loop.run_in_executor(None, _connect_client)
                self._login_alerted = False

                # Compatibility fallback if a test mock does not implement `subscribe`
                if not hasattr(client, "subscribe"):
                    await self._run_legacy_poll_once(client)
                    now = time.monotonic()
                    is_hot = any(now < self._hot_until.get(s, 0.0) for s in self.session_mapper.session_ids())
                    sleep_s = 1.0 if is_hot else max(1.0, float(self.config.recheck_sleep_s))
                    await asyncio.sleep(sleep_s)
                    continue

                subscribed_sids: Set[str] = set()
                trackers: Dict[str, PushTurnTracker] = {}
                last_ping_mono: float = time.monotonic()

                # Inner persistent event loop over the single connected MuseClient
                while self._running:
                    if not await asyncio.to_thread(self.muse_service.token_manager.has_credentials):
                        break
                    if hasattr(client, "credentials_current") and await asyncio.to_thread(client.credentials_current) is False:
                        break

                    watched = self.session_mapper.session_ids()
                    if not watched:
                        await asyncio.sleep(1.0)
                        continue

                    # 1. Sync subscriptions & initial hydration for newly watched or re-activated sessions
                    for sid in list(self._pending_subscribe):
                        if sid in subscribed_sids and sid in self._gated:
                            self._pending_subscribe.discard(sid)
                    need_setup = [
                        sid for sid in watched
                        if (sid not in self._gated)
                        or (sid in self._pending_subscribe)
                        or (sid not in subscribed_sids and sid not in self._uncreated_sessions)
                    ]

                    if need_setup:
                        def _setup_sessions(sids: List[str]) -> List[Tuple[str, List[dict], bool, bool]]:
                            out: List[Tuple[str, List[dict], bool, bool]] = []
                            for sid in sids:
                                is_init = sid not in self._gated
                                self._pending_subscribe.discard(sid)
                                hist: List[dict] = []
                                sub_ok = sid in subscribed_sids

                                if not sub_ok:
                                    try:
                                        sub = client.subscribe(sid)
                                        # Discard initial subscribe snapshot events; history hydration covers baseline/catch-up
                                        sub.reset()
                                        sub_ok = True
                                    except SubscribeError as se:
                                        s_msg = str(se).lower()
                                        if "not found" in s_msg or "not_found" in s_msg or "404" in s_msg:
                                            # Lazy session not created on server yet; mark gated & wait for post_message
                                            self._uncreated_sessions.add(sid)
                                            out.append((sid, [], is_init, False))
                                            continue
                                        raise

                                # Session exists on server: fetch history once for initial gate or post-create sync
                                hist = self._fetch_session(client, sid)
                                out.append((sid, hist, is_init, sub_ok))
                            return out

                        setup_results = await loop.run_in_executor(None, _setup_sessions, need_setup)
                        for sid, hist, is_init, sub_ok in setup_results:
                            if sub_ok:
                                subscribed_sids.add(sid)
                                trackers.setdefault(sid, PushTurnTracker(sid))
                            await self._hydrate_and_dispatch(sid, hist, is_initial_gate=is_init)

                    if not subscribed_sids:
                        # All watched sessions are lazy/uncreated (waiting for first user message)
                        await asyncio.sleep(0.5)
                        continue

                    # 2. Read real-time push events from active /chat/subscribe stream(s)
                    def _read_And_sync_events() -> List[Tuple[str, List[dict]]]:
                        nonlocal last_ping_mono
                        if hasattr(client, "read_any_subscription_events"):
                            events = client.read_any_subscription_events(timeout=1.0)
                        else:
                            # Fallback if single subscription object
                            subs = list(getattr(client, "_subscriptions", {}).values())
                            events = subs[0].read_events(timeout=1.0) if subs else []

                        now_mono = time.monotonic()
                        if events:
                            last_ping_mono = now_mono
                        elif now_mono - last_ping_mono >= 30.0:
                            last_ping_mono = now_mono
                            if hasattr(client, "ping") and callable(getattr(client, "ping", None)):
                                client.ping(presence="active", timeout=10.0)
                            if hasattr(client, "read_any_subscription_events"):
                                events = client.read_any_subscription_events(timeout=0.0)
                        if not events:
                            return []

                        default_sid = next(iter(subscribed_sids)) if len(subscribed_sids) == 1 else None
                        dirty_sids: Set[str] = set()

                        for ev in events:
                            sid, kind = self._classify_push_event(ev, default_sid=default_sid)
                            if not sid:
                                continue
                            tracker = trackers.setdefault(sid, PushTurnTracker(sid))
                            if tracker.feed(ev):
                                dirty_sids.add(sid)
                                trackers[sid] = PushTurnTracker(sid)
                            elif kind == "sync":
                                dirty_sids.add(sid)
                            elif kind == "stream":
                                self._touch_hot(sid)

                        if not dirty_sids:
                            return []

                        synced: List[Tuple[str, List[dict]]] = []
                        for sid in dirty_sids:
                            try:
                                synced.append((sid, self._fetch_session(client, sid)))
                            except Exception as ex:
                                if self._is_auth_error(ex):
                                    raise
                                logger.debug('Event-triggered sync failed for %s: %s', sid[:8], safe_error(ex))
                        return synced

                    synced_batches = await loop.run_in_executor(None, _read_And_sync_events)
                    for sid, hist in synced_batches:
                        await self._hydrate_and_dispatch(sid, hist, is_initial_gate=(sid not in self._gated))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if isinstance(e, LoginRequired):
                    if not self._login_alerted:
                        self._login_alerted = True
                        logger.warning('Cookie 会话不可用，等待管理员登录。 error=%s', safe_error(e))
                        await self._notify_login_required()
                    await asyncio.sleep(10.0)
                    continue
                if is_runtime_auth_error(e) and client is not None:
                    await asyncio.to_thread(self.muse_service.token_manager.invalidate_mint, client.token, client.notary_token)
                logger.warning("连接暂不可用，保留 Cookie，3 秒后重连。 error=%s", safe_error(e))
                await asyncio.sleep(3.0)
                continue
            finally:
                if client is not None:
                    try:
                        await loop.run_in_executor(None, client.close)
                    except Exception:
                        pass

    async def _run_legacy_poll_once(self, client) -> None:
        """Fallback single tick for test mocks that do not provide `subscribe`."""
        loop = asyncio.get_running_loop()

        def _tick() -> List[Tuple[str, List[dict]]]:
            results: List[Tuple[str, List[dict]]] = []
            if not self.session_mapper.session_ids():
                return results
            for sid in self._sessions_to_fetch(client):
                try:
                    results.append((sid, self._fetch_session(client, sid)))
                except Exception as ex:
                    if self._is_auth_error(ex):
                        raise
                    logger.debug('history fetch failed for %s: %s', sid[:8], safe_error(ex))
            return results

        results = await loop.run_in_executor(None, _tick)
        for sid, hist in results:
            await self._hydrate_and_dispatch(sid, hist, is_initial_gate=(sid not in self._gated))

    def _proactive_target(self, session_id: Optional[str]) -> Tuple[str, str]:
        """Destination for events that match no inbound route (reminders etc.).

        The chat bound to the session, else the first admin privately when
        ``unbound_push_to_admin`` is on; an empty id means there is nowhere to
        deliver.
        """
        if session_id:
            owners = self.session_mapper.owners(session_id)
            if owners:
                return owners[0]
        if self.config.unbound_push_to_admin:
            return "private", self.config.admin_target() or ""
        return "private", ""

    async def notify_interaction(self, item, text):
        # Routing is not authorization; only configured admins can decide.
        targets = list(self.session_mapper.owners(item.session_id))
        if not targets:
            target = self._proactive_target(item.session_id)
            if target[1]:
                targets.append(target)
        admins = [("private", admin) for admin in self.config.admin_id if admin]
        if self.config.approvals_in_place and item.kind == "approval":
            # In-place push replaces the admin copy; admins are only the
            # fallback when no chat of the session could show the card.
            return (await self._deliver_interaction(item, text, targets)
                    or await self._deliver_interaction(item, text, admins))
        return await self._deliver_interaction(item, text, targets + admins)

    async def _deliver_interaction(self, item, text, targets):
        protocol = self.muse_service.interaction_protocol
        delivered = False
        for target_type, target_id in dict.fromkeys(targets):
            if not target_id:
                continue
            if (item.kind == "approval" and item.context.get("displayed_to", {}).get((target_type, target_id))
                    == item.context.get("fingerprint")):
                delivered = True  # This chat already shows the current card.
                continue
            route = self.route_manager.get_latest_for_target(target_type, target_id, max_age_s=280.0)
            snapshot = protocol.display_snapshot(item) if item.kind == "approval" else None
            ok = await self._deliver(target_type, target_id, text, [], [], [], route=route)
            if ok and snapshot is not None:
                protocol.mark_displayed(item, (target_type, target_id), snapshot)
            delivered = bool(ok) or delivered
        return delivered

    async def _scan_and_dispatch(self, hist: List[dict], session_id: Optional[str] = None) -> None:
        # Consume state updates before message delivery deduplication.
        for event in hist:
            if event.get("event_name") == "delta.presentation" and session_id:
                self.muse_service.interaction_protocol.register_credential(event, session_id)
        ordered = sorted(hist, key=lambda e: e.get("seq") or 0)
        last_user_mid: Optional[str] = None

        for event in ordered:
            role = event.get("role")
            mid = event.get("message_id")
            seq = event.get("seq", 0)

            if role == "user":
                if last_user_mid and last_user_mid != mid:
                    self.route_manager.finish_turn(last_user_mid)
                last_user_mid = mid
                if mid:
                    self._seen_ids.add(mid)
                if seq > self._last_seq:
                    self._last_seq = seq
                continue

            if role != "assistant":
                continue

            if not mid or mid in self._seen_ids:
                continue

            if self.deduplicator.is_delivered(mid) or self.deduplicator.is_processing(mid):
                self._seen_ids.add(mid)
                continue

            # Assistant message or presentation event
            payload = event.get("payload") or {}
            event_name = event.get("event_name")
            kind = payload.get("kind")
            data_field = payload.get("data") or {}

            # Check if this is an assistant message or presentation
            if event_name not in ("message.assistant", "delta.presentation"):
                continue

            is_media_presentation = (
                event_name == "delta.presentation"
                and (
                    kind in ("file", "image", "video")
                    or "file" in data_field
                    or "image_path" in data_field
                    or event.get("_extracted_images")
                    or event.get("_extracted_files")
                    or event.get("_extracted_videos")
                )
            )

            # For normal text messages, ensure generation is completed (never partial/streaming!)
            if event_name == "message.assistant":
                status = payload.get("status") or event.get("status")
                if status != "completed":
                    logger.debug('Skip %s: generation in progress (status=%s)', mid, status)
                    continue

            credential = None
            raw_text = event.get("display_text") or ""
            if event_name == "delta.presentation" and not is_media_presentation:
                # Non-media presentation cards (browser tasks, credentials vaults, image options, etc.)
                credential = self.muse_service.interaction_protocol.register_credential(event, session_id) if session_id else None
                if credential and not credential.terminal:
                    if credential.context.get("auth_providers") or credential.context.get("grant_request_type"):
                        hint = "请在 Muse 网页完成此登录。"
                    elif not self.config.credentials_in_place:
                        hint = "请管理员私聊填写。"
                    elif credential.context.get("capture") == "verification_code":
                        hint = "管理员可引用此消息，发送验证码。"
                    else:
                        hint = "管理员可引用此消息，分两行发送账号和密码。"
                    clean_text = chr(10).join((
                        f"【凭证 {credential.id}】{credential.context['page_url']}",
                        hint, f"也可发送 \n/凭证 {credential.id} [保存/一次] [凭证]"))
                elif credential:
                    clean_text = "凭证卡已结束。"
                else:
                    clean_text = render_presentation_card_to_text(event) or ""
            else:
                clean_text = _clean_text(raw_text, payload.get("widgets"))

            images = event.get("_extracted_images") or []
            files = event.get("_extracted_files") or []
            videos = event.get("_extracted_videos") or []

            # If text simply mirrors the filename of an extracted file or image, suppress redundant text chunk
            if files and clean_text in [fl.get("filename") for fl in files if fl.get("filename")]:
                clean_text = ""
            if images and clean_text in [im.get("filename") for im in images if im.get("filename")]:
                clean_text = ""

            if not clean_text and not images and not files and not videos:
                continue

            # Route resolution: "回给谁？"
            meta = payload.get("metadata") or {}
            root_exec_id = meta.get("root_message_execution_id")

            route = self.route_manager.match_route(last_user_mid, root_exec_id)
            if route:
                target_type = route.target_type
                target_id = route.target_id
                reply_msg_id = route.msg_id
                route_type = "interactive_reply"
            else:
                # Proactive push: the chat bound to the session, else the admin
                target_type, target_id = self._proactive_target(session_id)
                route_type = "proactive_push"
                reply_msg_id = None
                # If target is a group, try to use a recent passive route within 5-min TTL
                if target_type == "group":
                    fallback_route = self.route_manager.get_latest_for_target("group", target_id, max_age_s=280.0)
                    if fallback_route and fallback_route.msg_id:
                        route = fallback_route
                        reply_msg_id = route.msg_id

            if not target_id:
                reason = ("no admin is configured" if self.config.unbound_push_to_admin
                          else "unbound_push_to_admin is disabled")
                logger.warning('Message %s of session %s has no bound chat (%s); dropped.',
                               mid, (session_id or "")[:8], reason)
                continue

            # Mark processing
            self.deduplicator.mark_processing([mid])

            try:
                success = await self._deliver(
                    target_type=target_type,
                    target_id=target_id,
                    text=clean_text,
                    images=images,
                    files=files,
                    videos=videos,
                    route=route,
                    **({"credential": credential} if credential else {}),
                )
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    logger.info('Dispatched %s -> %s:%s [%s] (imgs=%s, files=%s, vids=%s)', mid, target_type, target_id, route_type, len(images), len(files), len(videos))
                    trace_event(f"{self.platform}.outbound", "message.dispatched", session_id=session_id,
                                message_id=mid, sent_mid=last_user_mid, msg_id=reply_msg_id, target_type=target_type,
                                target_id=target_id, route_type=route_type,
                                image_count=len(images), file_count=len(files), video_count=len(videos))
                else:
                    self.deduplicator.rollback_processing([mid])
            except Exception as e:
                self.deduplicator.rollback_processing([mid])
                logger.error('Dispatch error for %s: %s', mid, safe_error(e))

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------
    async def _deliver(
        self,
        target_type: str,
        target_id: str,
        text: str,
        images: List[dict],
        files: List[dict],
        videos: List[dict],
        route: Optional[RouteInfo] = None,
        credential=None,
    ) -> bool:
        """Send one outbound item; True when its text reached the chat.

        ``credential`` set means the sent message may be quoted later to fill
        it: pass each sent body to ``interactions.remember_credential_message``.
        """
        raise NotImplementedError

    async def _send_notice(self, target_id: str, text: str) -> None:
        """Send a plain system notice to a private chat (admin alerts)."""
        raise NotImplementedError
