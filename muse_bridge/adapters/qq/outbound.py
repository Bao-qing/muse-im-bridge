"""Unified QQ Outbound message dispatcher.

The SOLE exit point for all outbound messages from Muse to QQ.
Unifies both interactive conversational replies and proactive notifications:
1. Listens for completed assistant events from the Muse /chat/subscribe event stream.
2. Extracts text, images (URLs & workspace), files (workspace & attachments), and videos.
3. Automatically reads file-store bytes via client.read_file() and downloads external URLs.
4. Uploads media to QQ files API (file_type: 1=image, 2=video, 4=file) and sends cards.
5. Resolves destination via RouteManager (replying to group/private or default proactive target).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...config.models import QQConfig
from ...core.deduplicator import MessageDeduplicator
from ...core.message import chunk_text
from ...core.session_map import MODE_GLOBAL, SessionMapper
from ...muse.client.errors import ConnectionLost, HandshakeError, MuseError, RPCError, SubscribeError
from ...muse.client.push import PushTurnTracker
from ...muse.client.render import (
    extract_files,
    extract_images,
    extract_workspace_files,
    extract_workspace_images,
    extract_workspace_videos,
)
from ...muse.service import MuseService
from .client import QQClient
from .route_manager import RouteInfo, RouteManager

_SANDBOX_MEDIA_RE = re.compile(r"!?\[([^\]]*)\]\(sandbox://([^)]+)\)")
_WIDGET_RE = re.compile(r"\[\[hatch_widget:[^\]]+\]\]")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def _clean_text(text: str) -> str:
    cleaned = _SANDBOX_MEDIA_RE.sub("", text)
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


class QQOutboundPusher:
    def __init__(
        self,
        config: QQConfig,
        client: QQClient,
        muse_service: MuseService,
        deduplicator: MessageDeduplicator,
        route_manager: RouteManager,
        session_id: str = "",
        session_mapper: Optional[SessionMapper] = None,
        on_login_required: Optional[Callable[[Optional[str]], None]] = None,
    ):
        self.config = config
        self.client = client
        self.muse_service = muse_service
        self.deduplicator = deduplicator
        self.route_manager = route_manager
        self._on_login_required = on_login_required
        # Without an explicit mapper behave exactly like the legacy single-session pusher.
        self.session_mapper = session_mapper or SessionMapper(
            mode=MODE_GLOBAL, global_session_id=session_id
        )

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

    # -- legacy accessor: the single global session ------------------------
    @property
    def session_id(self) -> str:
        return self.session_mapper.global_session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        if not self.session_mapper.per_chat:
            self.session_mapper._global_sid = value
            if value:
                self._pending_subscribe.add(value)

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
        state_file = self.config.push.state_file
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
        state_file = self.config.push.state_file
        if not state_file:
            return
        tmp = f"{state_file}.tmp.{os.getpid()}"
        try:
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
        default_type, default_id = self.config.resolve_push_target()
        print(f"[QQOutbound] Started event-driven outbound dispatcher (Default push target: {default_type}:{default_id})")

        while self._running:
            try:
                await self._run_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                if self.config.debug:
                    print(f"[QQOutbound] Event stream loop error: {e}, reconnecting in 3s...")
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
                        if self.config.debug:
                            print(f"[QQOutbound] Failed reading workspace media {wpath}: {e}")
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
        if not self.session_mapper.per_chat:
            return watched

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
            if self.config.debug:
                print(f"[QQOutbound] session list failed ({e}); falling back to watched sessions")

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
        (/switch or a config binding) so old history is never pushed to QQ.
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
            if self.config.debug:
                print(f"[QQOutbound] baseline fetch for {session_id[:8]} failed ({e}); treating as empty")
            hist = []
        for e in hist:
            if e.get("role") == "assistant" and e.get("message_id"):
                self._seen_ids.add(e["message_id"])
        self._gated.add(session_id)
        self._save_state()

    async def _notify_login_required(self) -> None:
        admin_target = self.config.get_default_private_target()
        if admin_target:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self.client.send_message,
                    "private",
                    admin_target,
                    "【系统提示】获取 Muse 消息时检测到凭据失效（401 Unauthorized）。\n已自动进入登录回落流程，请直接在此私聊发送：\n/login <您的邮箱>\n（或直接回复您的邮箱地址）即可获取验证码重新登录恢复服务 ♪",
                )
            except Exception as e:
                if self.config.debug:
                    print(f"[QQOutbound] Failed to notify admin for login: {e}")
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
                enabled=self.config.push.catch_up_enabled,
                window_s=self.config.push.catch_up_window_s,
                max_messages=self.config.push.catch_up_max_messages,
            )
            self._seen_ids = seen_ids
            if action == "baseline":
                self._last_seq = max(self._last_seq, max_seq)
                self._save_state()
                if self.config.debug:
                    print(f"[QQOutbound] Startup baseline {sid[:8]} ({reason}): marked {skipped} messages seen, 0 pushed.")
            else:
                print(f"[QQOutbound] Startup catch-up {sid[:8]}: recovering up to {kept} recent message(s) (skipped {skipped} older).")

        if hist:
            await self._scan_and_dispatch(hist, sid)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in ("credential", "cookie", "handshake", "unauthorized", "401", "hatch_vm_auth"))

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
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            kind = data.get("type") or payload.get("kind")
            if kind in ("image", "file", "video") or "file" in data or "image_path" in payload or "video_path" in payload:
                return sid, "sync"
            return sid, None

        if name == "task.status":
            if payload.get("status") == "completed":
                return sid, "sync"
            return sid, "stream"

        return sid, None

    async def _run_loop(self) -> None:
        """Pure event-driven loop powered by /chat/subscribe over persistent Noise WebSocket."""
        loop = asyncio.get_running_loop()

        while self._running:
            if not self.muse_service.token_manager.has_credentials():
                if not self._login_alerted:
                    self._login_alerted = True
                    print("[QQOutbound] Muse 尚未登录或凭据失效，暂停出站监听，触发回落登录...")
                    await self._notify_login_required()
                await asyncio.sleep(5.0)
                continue

            client = None
            try:
                def _connect_client():
                    c = self.muse_service.create_client()
                    c.connect()
                    return c

                client = await loop.run_in_executor(None, _connect_client)
                self._login_alerted = False

                # Compatibility fallback if a test mock does not implement `subscribe`
                if not hasattr(client, "subscribe"):
                    await self._run_legacy_poll_once(client)
                    now = time.monotonic()
                    is_hot = any(now < self._hot_until.get(s, 0.0) for s in self.session_mapper.session_ids())
                    sleep_s = 1.0 if is_hot else max(1.0, float(self.config.push.recheck_sleep_s))
                    await asyncio.sleep(sleep_s)
                    continue

                subscribed_sids: Set[str] = set()
                trackers: Dict[str, PushTurnTracker] = {}

                # Inner persistent event loop over the single connected MuseClient
                while self._running:
                    if not self.muse_service.token_manager.has_credentials():
                        break

                    watched = self.session_mapper.session_ids()
                    if not watched:
                        await asyncio.sleep(1.0)
                        continue

                    # 1. Sync subscriptions & initial hydration for newly watched or re-activated sessions
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
                        if hasattr(client, "read_any_subscription_events"):
                            events = client.read_any_subscription_events(timeout=1.0)
                        else:
                            # Fallback if single subscription object
                            subs = list(getattr(client, "_subscriptions", {}).values())
                            events = subs[0].read_events(timeout=1.0) if subs else []

                        if not events:
                            return []

                        default_sid = next(iter(subscribed_sids)) if len(subscribed_sids) == 1 else self.session_id
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
                                if self.config.debug:
                                    print(f"[QQOutbound] Event-triggered sync failed for {sid[:8]}: {ex}")
                        return synced

                    synced_batches = await loop.run_in_executor(None, _read_And_sync_events)
                    for sid, hist in synced_batches:
                        await self._hydrate_and_dispatch(sid, hist, is_initial_gate=(sid not in self._gated))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._is_auth_error(e):
                    self.muse_service.token_manager.invalidate()
                    if not self._login_alerted:
                        self._login_alerted = True
                        print(f"[QQOutbound] 获取 Muse 消息时凭据失效 ({e})，触发回落登录流程...")
                        await self._notify_login_required()
                    await asyncio.sleep(10.0)
                    continue
                if self.config.debug:
                    print(f"[QQOutbound] Subscribe stream error ({e}), reconnecting in 3s...")
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
                    if self.config.debug:
                        print(f"[QQOutbound] history fetch failed for {sid[:8]}: {ex}")
            return results

        results = await loop.run_in_executor(None, _tick)
        for sid, hist in results:
            await self._hydrate_and_dispatch(sid, hist, is_initial_gate=(sid not in self._gated))

    def _proactive_target(self, session_id: Optional[str]) -> Tuple[str, str]:
        """Destination for events that match no inbound route (reminders etc.)."""
        if (
            session_id
            and self.session_mapper.per_chat
            and self.config.proactive_to_session_owner
        ):
            owners = self.session_mapper.owners(session_id)
            if owners:
                return owners[0]
        return self.config.resolve_push_target()

    async def _scan_and_dispatch(self, hist: List[dict], session_id: Optional[str] = None) -> None:
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

            # Ignore pure web UI widget cards (e.g. HTML timer cards) that have no image/file media
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

            if event_name == "delta.presentation" and not is_media_presentation:
                self._seen_ids.add(mid)
                continue

            # For normal text messages, ensure generation is completed (never partial/streaming!)
            if event_name == "message.assistant":
                status = payload.get("status") or event.get("status")
                if status != "completed":
                    if self.config.debug:
                        print(f"[QQOutbound] Skip {mid}: generation in progress (status={status})")
                    continue

            raw_text = event.get("display_text") or ""
            clean_text = _clean_text(raw_text)

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
                # Proactive push: session owner (per_chat) or the configured push target
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
                if self.config.debug:
                    print(f"[QQOutbound] No target found for message {mid}, skipping.")
                continue

            # Mark processing
            self.deduplicator.mark_processing([mid])

            try:
                success = await self._send_to_qq(
                    target_type=target_type,
                    target_id=target_id,
                    text=clean_text,
                    images=images,
                    files=files,
                    videos=videos,
                    route=route,
                )
                if success:
                    self.deduplicator.mark_delivered([mid])
                    self._seen_ids.add(mid)
                    if seq > self._last_seq:
                        self._last_seq = seq
                    self._save_state()
                    print(f"[QQOutbound] Dispatched {mid} -> {target_type}:{target_id} [{route_type}] (imgs={len(images)}, files={len(files)}, vids={len(videos)})")
                else:
                    self.deduplicator.rollback_processing([mid])
            except Exception as e:
                self.deduplicator.rollback_processing([mid])
                print(f"[QQOutbound] Dispatch error for {mid}: {e}")

    async def _send_to_qq(
        self,
        target_type: str,
        target_id: str,
        text: str,
        images: List[dict],
        files: List[dict],
        videos: List[dict],
        route: Optional[RouteInfo] = None,
    ) -> bool:
        loop = asyncio.get_running_loop()
        chunks = chunk_text(text, self.config.chunk_chars, self.config.max_chunks) if text else []
        passive_limit = 4 if target_type == "private" else 5
        active_budget = self.config.max_active_chunks

        def _get_passive_meta():
            if not route or not route.msg_id:
                return None, None
            s = self.route_manager.next_seq(route.user_mid)
            if s <= passive_limit:
                return route.msg_id, s
            return None, None

        async def _deliver_item(content=None, md=None, media_info=None) -> bool:
            nonlocal active_budget
            curr_mid, curr_seq = _get_passive_meta()

            if not curr_mid:
                if active_budget <= 0:
                    print(f"[QQOutbound] Active budget ({self.config.max_active_chunks}) exhausted; item dropped.")
                    return False
                active_budget -= 1

            code, body = await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                content,
                md,
                media_info,
                curr_mid,
                curr_seq,
            )

            # Retry as active if passive rejected
            if curr_mid and not (200 <= code < 300):
                if self.config.debug:
                    print(f"[QQOutbound] Passive send failed ({code}): {body}, retrying as active...")
                if active_budget > 0:
                    active_budget -= 1
                    code, body = await loop.run_in_executor(
                        None,
                        self.client.send_message,
                        target_type,
                        target_id,
                        content,
                        md,
                        media_info,
                    )

            if not (200 <= code < 300):
                # If active send to group fails with permission error (40034105), fall back to admin private
                if target_type == "group" and any(k in str(body) for k in ("40034105", "无权限", "主动消息失败")):
                    admin_type, admin_id = self.config.resolve_push_target()
                    if admin_id and (admin_type != target_type or admin_id != target_id):
                        if self.config.debug:
                            print(f"[QQOutbound] Group active send rejected ({code}); falling back to admin {admin_type}:{admin_id}")
                        code, body = await loop.run_in_executor(
                            None,
                            self.client.send_message,
                            admin_type,
                            admin_id,
                            f"【群消息提醒】\n{content}" if content else content,
                            f"【群消息提醒】\n{md}" if md else md,
                            media_info,
                        )

            if not (200 <= code < 300):
                print(f"[QQOutbound] Failed sending item to {target_type}:{target_id} ({code}): {body}")
                return False
            return True

        # 1. Send text chunks
        for chunk in chunks:
            await _deliver_item(content=chunk, md=chunk)

        # 2. Send images
        for img in images:
            data = img.get("data")
            if not data and img.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        img["url"],
                        self.config.max_image_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download image {img['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_image_bytes:
                print(f"[QQOutbound] Image exceeds limit ({len(data)} > {self.config.max_image_bytes})")
                continue

            try:
                fname = img.get("filename") or "image.jpg"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    1,  # 1=image
                    fname,
                )
                await _deliver_item(media_info=file_info)
            except Exception as e:
                print(f"[QQOutbound] Failed to upload/send image: {e}")

        # 3. Send videos
        for vid in videos:
            data = vid.get("data")
            if not data and vid.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        vid["url"],
                        self.config.max_video_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download video {vid['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_video_upload_bytes:
                print(f"[QQOutbound] Video exceeds upload limit ({len(data)} > {self.config.max_video_upload_bytes})")
                continue

            try:
                fname = vid.get("filename") or "video.mp4"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    2,  # 2=video
                    fname,
                )
                await _deliver_item(media_info=file_info)
            except Exception as e:
                print(f"[QQOutbound] Failed to upload/send video: {e}")

        # 4. Send files
        for fl in files:
            data = fl.get("data")
            if not data and fl.get("url"):
                try:
                    data = await loop.run_in_executor(
                        None,
                        self.client.download_bytes,
                        fl["url"],
                        self.config.max_file_bytes,
                    )
                except Exception as e:
                    print(f"[QQOutbound] Failed to download file {fl['url']}: {e}")
                    continue
            if not data:
                continue
            if len(data) > self.config.max_file_bytes:
                print(f"[QQOutbound] File exceeds limit ({len(data)} > {self.config.max_file_bytes})")
                continue

            try:
                fname = fl.get("filename") or "file.dat"
                file_info = await loop.run_in_executor(
                    None,
                    self.client.upload_media,
                    target_type,
                    target_id,
                    data,
                    4,  # 4=file
                    fname,
                )
                await _deliver_item(media_info=file_info)
            except Exception as e:
                print(f"[QQOutbound] Failed to upload/send file: {e}")

        return True
