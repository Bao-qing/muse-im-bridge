"""Platform-neutral outbound dispatcher: the sole exit from Muse to a chat.

Unifies interactive replies and proactive notifications for every adapter:
1. Listens for completed assistant events from the Muse /聊天/subscribe event stream.
2. Extracts text, images (URLs & workspace), files (workspace & attachments), and videos.
3. Automatically reads file-store bytes via client.read_file().
4. Resolves the destination via RouteManager (the chat that asked, else the
   chat bound to the session; unbound sessions are never pushed).
5. Hands the item to the platform's ``_deliver`` (QQ upload API, OneBot segments, ...).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.models import ChatConfig, ConversationConfig
from ..core.deduplicator import MessageDeduplicator
from ..core.message import DeliveryReference, chunk_text
from ..core.references import MessageReferenceStore
from ..core.session_map import SessionMapper
from ..core.state_file import atomic_json_write
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
from ..muse.interactions import credential_input_kind, credential_secret_label
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
        config: ConversationConfig,
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
        self._reference_state_file = str(config.routes_file) + ".references.local.json" if config.routes_file else None
        self._reference_identity = self.reference_identity()
        self.reference_store = MessageReferenceStore(
            state_file=None if self.platform == "onebot" else self._reference_state_file,
            identity=self._reference_identity)
        self._reference_account = None
        self.muse_service.interactions.register_namespace(self.platform, self.account_identity(), owner=self)

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
        self._pending_subscribe: Set[str] = set()     # sessions that need /聊天/subscribe opened
        self._uncreated_sessions: Set[str] = set()    # lazy sessions (404 until first message is posted)
        self._load_state()

    def reference_identity(self) -> str:
        """Persistent reference identity; retain legacy QQ/OneBot format."""
        identity = getattr(self.config, "app_id", None) or getattr(self.config, "base_url", "")
        return f"{self.platform}:{identity}"

    def account_identity(self) -> str:
        if self.platform == "onebot":
            return str(self._reference_account or getattr(self.config, "base_url", ""))
        return str(getattr(self.config, "app_id", ""))

    def notification_target(self) -> Optional[str]:
        """Private account notice recipient; personal platforms override this."""
        return self.config.admin_target() if isinstance(self.config, ChatConfig) else None

    def _interaction_admin_targets(self):
        """Only access-controlled platforms send extra administrator copies."""
        if not isinstance(self.config, ChatConfig):
            return []
        return [("private", admin) for admin in self.config.admin_id if admin]

    def interaction_target(self, kind, target):
        return (kind, target, self.platform, self.account_identity())

    def render_credential_hint(self, credential):
        if credential.context.get("auth_providers") or credential.context.get("grant_request_type"):
            return "请在 Muse 网页完成此登录。"
        if not self.config.credentials_in_place:
            return "请管理员私聊填写。"
        kind = credential_input_kind(credential.context)
        if kind == "code":
            return "管理员可引用此消息，发送验证码。"
        if kind == "secret":
            return f"管理员可引用此消息，发送{credential_secret_label(credential.context)}（支持整段多行内容，无需填写账号）。"
        return "管理员可引用此消息，第一行发送账号，后续内容发送密码或密钥（支持多行）。"

    def track_session(self, session_id: str, fresh: bool = False) -> None:
        """Called by the adapter right after creating or posting into ``session_id``.

        ``fresh`` marks a session created just now (nothing to baseline, not on server yet).
        When ``fresh=False`` (called after ``post_message`` succeeds), the session now exists
        on the Muse server and is queued for immediate ``/聊天/subscribe`` registration.
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
        try:
            atomic_json_write(state_file, {
                "last_seq": self._last_seq,
                "seen_ids": list(self._seen_ids)[-2000:],
                "updated_ts": time.time(),
            })
        except Exception as exc:
            logger.warning("Outbound state write failed: %s", safe_error(exc))

    async def start(self) -> None:
        self._running = True
        logger.info('Started %s outbound dispatcher (bound sessions only)', self.platform)

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
        (/切换 or a config binding) so old history is never pushed to the chat.
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
            await self._dispatch_reaction(e, session_id, baseline=True)
            if e.get("role") == "assistant" and e.get("message_id"):
                self._seen_ids.add(e["message_id"])
        self._gated.add(session_id)
        self._save_state()

    async def _notify_login_required(self) -> None:
        admin_target = self.notification_target()
        if admin_target:
            try:
                await self._send_notice(
                    admin_target,
                    "【系统提示】获取 Muse 消息时检测到凭据失效（401 Unauthorized）。\n已自动进入登录回落流程，请直接在此私聊发送：\n/登录 <您的邮箱>\n即可获取验证码重新登录恢复服务 ♪",
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
        if not self.session_mapper.owners(sid):
            return
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
                for event in hist:
                    await self._dispatch_reaction(event, sid, baseline=True)
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
        """Extract (session_id, trigger_kind) from a /聊天/subscribe event.

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

        if name == "reactions.updated":
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
        """Pure event-driven loop powered by /聊天/subscribe over persistent Noise WebSocket."""
        loop = asyncio.get_running_loop()
        reconnect_failures = 0

        while self._running:
            if not await asyncio.to_thread(self.muse_service.token_manager.has_credentials):
                reconnect_failures = 0
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
                    reconnect_failures = 0
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
                    if subscribed_sids.difference(watched):
                        # No per-session unsubscribe RPC: close this multiplexed
                        # connection and rebuild only the current bindings.
                        break
                    if not watched:
                        reconnect_failures = 0
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
                                if not self.session_mapper.owners(sid):
                                    continue
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
                        reconnect_failures = 0
                        for sid, hist, is_init, sub_ok in setup_results:
                            if sub_ok:
                                subscribed_sids.add(sid)
                                trackers.setdefault(sid, PushTurnTracker(sid))
                            await self._hydrate_and_dispatch(sid, hist, is_initial_gate=is_init)

                    if not subscribed_sids:
                        # All watched sessions are lazy/uncreated (waiting for first user message)
                        reconnect_failures = 0
                        await asyncio.sleep(0.5)
                        continue

                    # 2. Read real-time push events from active /聊天/subscribe stream(s)
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
                        reaction_events: Dict[str, List[dict]] = {}

                        for ev in events:
                            sid, kind = self._classify_push_event(ev, default_sid=default_sid)
                            if not sid or not self.session_mapper.owners(sid):
                                continue
                            if (ev.get("event") or ev.get("event_name")) == "reactions.updated":
                                reaction_events.setdefault(sid, []).append(ev)
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
                            if not self.session_mapper.owners(sid):
                                continue
                            try:
                                synced.append((sid, self._fetch_session(client, sid) + reaction_events.get(sid, [])))
                            except Exception as ex:
                                if self._is_auth_error(ex):
                                    raise
                                logger.debug('Event-triggered sync failed for %s: %s', sid[:8], safe_error(ex))
                                if reaction_events.get(sid):
                                    synced.append((sid, reaction_events[sid]))
                        return synced

                    synced_batches = await loop.run_in_executor(None, _read_And_sync_events)
                    reconnect_failures = 0
                    for sid, hist in synced_batches:
                        await self._hydrate_and_dispatch(sid, hist, is_initial_gate=(sid not in self._gated))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if isinstance(e, LoginRequired):
                    reconnect_failures = 0
                    if not self._login_alerted:
                        self._login_alerted = True
                        logger.warning('Cookie 会话不可用，等待管理员登录。 error=%s', safe_error(e))
                        await self._notify_login_required()
                    await asyncio.sleep(10.0)
                    continue
                if is_runtime_auth_error(e) and client is not None:
                    await asyncio.to_thread(self.muse_service.token_manager.invalidate_mint, client.token, client.notary_token)
                reconnect_failures += 1
                max_retries = self.config.reconnect_max_retries
                if reconnect_failures > max_retries:
                    logger.error(
                        "重新连接失败，连续重连已达上限（%d 次），停止自动重连，保留 Cookie。 error=%s",
                        max_retries,
                        safe_error(e),
                    )
                    self._running = False
                    break
                logger.warning(
                    "重新连接中，保留 Cookie，3 秒后重试（%d/%d）。 error=%s",
                    reconnect_failures,
                    max_retries,
                    safe_error(e),
                )
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

        Only a chat currently bound to the session may receive its output.
        Unbound sessions never fall back to the administrator.
        """
        if session_id:
            owners = self.session_mapper.owners(session_id)
            if owners:
                return owners[0]
        return "private", ""

    def resolve_delivery_target(self, session_id, last_user_mid, root_exec_id):
        """Return (kind, real target ID, passive route, delivery purpose)."""
        owners = self.session_mapper.owners(session_id) if session_id else []
        route = self.route_manager.match_route(last_user_mid, root_exec_id)
        if route and session_id and (route.target_type, route.target_id) not in owners:
            route = None
        if session_id and not owners:
            return "private", "", None, None
        if route:
            return route.target_type, route.target_id, route, None
        target_type, target_id = self._proactive_target(session_id)
        if target_type == "group":
            fallback = self.route_manager.get_latest_for_target("group", target_id, max_age_s=280.0)
            if fallback and fallback.msg_id:
                route = fallback
        return target_type, target_id, route, None

    async def notify_interaction(self, item, text):
        # Unbound sessions must not emit cards, including admin copies.
        if not self.session_mapper.owners(item.session_id):
            return False
        # Routing is not authorization; the inbound channel applies its own policy.
        targets = list(self.session_mapper.owners(item.session_id))
        if not targets:
            target = self._proactive_target(item.session_id)
            if target[1]:
                targets.append(target)
        admins = self._interaction_admin_targets()
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
            if not self.session_mapper.owners(item.session_id):
                return delivered
            if not target_id:
                continue
            if (item.kind == "approval" and item.context.get("displayed_to", {}).get(self.interaction_target(target_type, target_id))
                    == item.context.get("fingerprint")):
                delivered = True  # This chat already shows the current card.
                continue
            route = self.route_manager.get_latest_for_target(target_type, target_id, max_age_s=280.0)
            snapshot = protocol.display_snapshot(item) if item.kind == "approval" else None
            delivery = DeliveryReference(item.session_id, "",
                approval_binding=(item, snapshot) if snapshot is not None else None,
                purpose="notification")
            ok = await self._deliver(target_type, target_id, text, [], [], [], route=route, delivery=delivery)
            if ok and snapshot is not None:
                protocol.mark_displayed(item, self.interaction_target(target_type, target_id), snapshot)
            delivered = bool(ok) or delivered
        return delivered

    async def _scan_and_dispatch(self, hist: List[dict], session_id: Optional[str] = None) -> None:
        if session_id and not self.session_mapper.owners(session_id):
            return
        # Consume state updates before message delivery deduplication.
        for event in hist:
            if event.get("event_name") == "delta.presentation" and session_id:
                self.muse_service.interaction_protocol.register_credential(event, session_id)
        ordered = sorted(hist, key=lambda e: e.get("seq") or 0)
        live_reaction_ids = {
            (event.get("payload") or {}).get("message_id") for event in ordered
            if (event.get("event") or event.get("event_name")) == "reactions.updated"
        }
        for event in ordered:
            if event.get("message_id") in live_reaction_ids and "reaction_emoji" in event:
                continue
            await self._dispatch_reaction(event, session_id)
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
                    hint = self.render_credential_hint(credential)
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

            meta = payload.get("metadata") or {}
            root_exec_id = meta.get("root_message_execution_id")
            target_type, target_id, route, purpose = self.resolve_delivery_target(
                session_id, last_user_mid, root_exec_id)
            reply_msg_id = route.msg_id if route else None
            route_type = "interactive_reply" if route and purpose != "proactive" else "proactive_push"

            if not target_id:
                logger.warning('Message %s of session %s has no bound chat; dropped.',
                               mid, (session_id or "")[:8])
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
                    delivery=DeliveryReference(
                        session_id or "", mid, clean_text[:240],
                        self._event_reply_id(event),
                        {"type": "presentation", "presentation_event_id": payload.get("presentation_id") or mid}
                        if event_name == "delta.presentation" else None, purpose=purpose),
                )
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    logger.info('Dispatched %s -> %s:%s [%s] (imgs=%s, files=%s, vids=%s)', mid, target_type, target_id, route_type, len(images), len(files), len(videos))
                    trace_event(f"{self.platform}.outbound", "message.dispatched", session_id=session_id,
                                message_id=mid, sent_mid=last_user_mid if purpose != "proactive" else None,
                                msg_id=reply_msg_id, target_type=target_type,
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
    @staticmethod
    def _reply_target_id(target):
        if not isinstance(target, dict):
            return None
        if target.get("type") == "message":
            return target.get("message_id")
        if target.get("type") == "presentation":
            return target.get("presentation_event_id")
        return None

    @classmethod
    def _event_reply_id(cls, event):
        payload = event.get("payload") or {}

        def message_identity(mid):
            return mid.removeprefix("assistant-msg-") if isinstance(mid, str) else mid

        # Muse may link an assistant message to its own bare execution ID.
        own_ids = {
            message_identity(mid) for mid in (event.get("message_id"), payload.get("message_id"))
            if mid
        }
        for candidate in (event.get("reply_to_message_id"), payload.get("reply_to_message_id")):
            if candidate and message_identity(candidate) not in own_ids:
                return candidate
        target = payload.get("reply_to_target")
        candidate = cls._reply_target_id(target)
        if candidate and (target.get("type") == "presentation" or message_identity(candidate) not in own_ids):
            return candidate
        return None

    @staticmethod
    def _reaction_snapshot(event):
        name = event.get("event") or event.get("event_name")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if name == "reactions.updated":
            mid, emojis = payload.get("message_id"), payload.get("reactions")
            seq = event.get("seq") or payload.get("seq") or 0
            if not isinstance(emojis, list):
                return None
        elif name in ("message.user", "message.assistant", "delta.presentation") and "reaction_emoji" in event:
            mid = event.get("message_id")
            emoji = event.get("reaction_emoji")
            if emoji is not None and not isinstance(emoji, str):
                return None
            emojis = [emoji] if emoji else []
            # History seq is the original message sequence, NOT reaction revision.
            # Keep it separate from live update ordering.
            seq = 0
        else:
            return None
        if not isinstance(mid, str) or not mid or type(seq) is not int:
            return None
        emojis = [e.get("emoji") if isinstance(e, dict) else e for e in emojis]
        emojis = list(dict.fromkeys(e for e in emojis if isinstance(e, str) and 0 < len(e) <= 64))[:64]
        return mid, seq, emojis

    async def _dispatch_reaction(self, event, session_id, baseline=False):
        snapshot = self._reaction_snapshot(event)
        if not session_id or snapshot is None:
            return
        mid, seq, emojis = snapshot
        previous = self.reference_store.reaction_state(session_id, mid)
        if baseline:
            if previous is None:
                self.reference_store.set_reaction_state(session_id, mid, seq, emojis)
            return
        if previous:
            prev_seq, old = previous
            if seq and prev_seq and seq <= prev_seq:
                return
            # History is fetched after push batches and is authoritative even
            # when a newer reaction was missed during a transport disconnect.
        else:
            prev_seq, old = 0, []
        if old == emojis:
            if seq > prev_seq:
                self.reference_store.set_reaction_state(session_id, mid, seq, emojis)
            return
        target_type, target_id = self._proactive_target(session_id)
        if not target_id:
            return
        additions = [e for e in emojis if e not in old]
        removals = [e for e in old if e not in emojis]
        parts = []
        if additions:
            # parts.append("【Muse 反应】" + " ".join(additions))
            parts.append(" ".join(additions))
        if removals:
            # parts.append("【Muse 取消反应】" + " ".join(removals))
            parts.append(" ".join(removals))
        text = chr(10).join(parts)
        try:
            success = await self._deliver(
                target_type, target_id, text, [], [], [],
                delivery=DeliveryReference(session_id, mid, "", mid, plain_text=True, purpose="notification", revision=seq))
            if success:
                self.reference_store.set_reaction_state(session_id, mid, max(seq, prev_seq), emojis)
        except Exception as exc:
            logger.warning("Reaction delivery failed: %s", safe_error(exc))

    def bind_reference_account(self, account_id):
        """Never reuse another logged-in OneBot account's opaque IDs."""
        if self.platform != "onebot" or not account_id or self._reference_account == str(account_id):
            return
        self._reference_account = str(account_id)
        self.muse_service.interactions.register_namespace(self.platform, self.account_identity(), owner=self)
        self.reference_store = MessageReferenceStore(
            self._reference_state_file, self._reference_identity + ":" + self._reference_account)

    def quote_for(self, delivery, target_type, target_id):
        if not delivery or not delivery.reply_to_id:
            return None
        return self.reference_store.resolve_muse(
            delivery.session_id, target_type, target_id, delivery.reply_to_id)

    def quote_text(self, delivery, target_type, target_id):
        if not delivery or not delivery.reply_to_id:
            return ""
        reference = self.quote_for(delivery, target_type, target_id)
        excerpt = reference.excerpt if reference else "原消息在此聊天不可用"
        return "【引用】" + (excerpt or "[非文本消息]").replace(chr(10), chr(10) + "> ") + chr(10)

    def remember_delivery(self, delivery, target_type, target_id, body):
        if not delivery or not delivery.session_id or not isinstance(body, dict):
            return
        if delivery.approval_binding is not None:
            item, snapshot = delivery.approval_binding
            self.muse_service.interaction_protocol.remember_approval_message(
                self.interaction_target(target_type, target_id), body, [item], [snapshot])
            return
        if not delivery.muse_id:
            return
        info = body.get("ext_info") or {}
        refs = [("id", body.get("id") or body.get("message_id"))]
        if isinstance(info, dict):
            refs.append(("ref", info.get("ref_idx")))
        refs = [(kind, str(value)) for kind, value in refs if value is not None and str(value)]
        if refs:
            self.reference_store.remember(
                delivery.session_id, target_type, target_id, delivery.muse_id,
                refs, delivery.excerpt, delivery.reply_target)

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
        delivery=None,
    ) -> bool:
        """Send one outbound item; True when its text reached the chat.

        ``credential`` set means the sent message may be quoted later to fill
        it: pass each sent body to ``interactions.remember_credential_message``.
        """
        raise NotImplementedError

    async def _send_notice(self, target_id: str, text: str) -> None:
        """Send a plain system notice to a private chat (admin alerts)."""
        raise NotImplementedError
