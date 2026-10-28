"""Configuration models and loader.

Distinguishes between user-editable credentials/settings and program defaults.
Supports deep-merging user config over internal defaults.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Union

from . import constants


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _detect_egress_ip(proxy_url: Optional[str] = None, direct: bool = True) -> str:
    """Best-effort public egress IP auto-detection."""
    if direct:
        handler = urllib.request.ProxyHandler({})
    elif proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        handler = urllib.request.ProxyHandler()

    opener = urllib.request.build_opener(handler)
    for url in constants.EGRESS_IP_SERVICES:
        try:
            with opener.open(url, timeout=5) as resp:
                text = resp.read().decode("utf-8", "ignore").strip()
                try:
                    return str(ipaddress.ip_address(text))
                except ValueError:
                    continue
        except Exception:
            continue
    return ""


@dataclasses.dataclass
class MuseConfig:
    mint_file: str = ".mint.json"
    cookies_file: str = "cookies.json"
    session_id: str = ""
    vm_id: str = ""
    host: str = constants.DEFAULT_MUSE_HOST
    client_ip: str = ""
    timezone: str = constants.DEFAULT_MUSE_TIMEZONE
    proxy_url: Optional[str] = None
    direct: bool = constants.DEFAULT_MUSE_DIRECT
    reply_mode: str = constants.DEFAULT_MUSE_REPLY_MODE


@dataclasses.dataclass
class QQPushConfig:
    enabled: bool = True
    target_type: str = "private"  # "private" or "group"
    target_id: Optional[str] = None  # None -> auto-select first private openid from whitelist
    catch_up_enabled: bool = constants.DEFAULT_CATCH_UP_ENABLED
    catch_up_window_s: int = constants.DEFAULT_CATCH_UP_WINDOW_S
    catch_up_max_messages: int = constants.DEFAULT_CATCH_UP_MAX_MESSAGES
    in_flight_ttl_s: int = constants.DEFAULT_IN_FLIGHT_TTL_S
    processing_ttl_s: int = constants.DEFAULT_PROCESSING_TTL_S
    periodic_scan_s: int = constants.DEFAULT_PERIODIC_SCAN_S
    turn_end_fallback_s: int = constants.DEFAULT_TURN_END_FALLBACK_S
    recheck_sleep_s: int = constants.DEFAULT_RECHECK_SLEEP_S
    reconnect_sleep_s: int = constants.DEFAULT_RECONNECT_SLEEP_S
    state_file: str = "qq_push_state.json"
    delivered_file: str = "qq_delivered.jsonl"


@dataclasses.dataclass
class QQConfig:
    enabled: bool = True
    app_id: str = ""
    app_secret: str = ""
    session_id: str = ""
    whitelist: Dict[str, Union[List[str], str]] = dataclasses.field(default_factory=dict)
    push: QQPushConfig = dataclasses.field(default_factory=QQPushConfig)
    session_state_file: str = "qq_session.json"
    delivered_file: str = "qq_delivered.jsonl"
    proxy_url: Optional[str] = None
    download_proxy: str = ""
    debug: bool = False

    # Chunking & budget
    chunk_chars: int = constants.DEFAULT_CHUNK_CHARS
    max_chunks: int = constants.DEFAULT_MAX_CHUNKS
    max_active_chunks: int = constants.DEFAULT_MAX_ACTIVE_CHUNKS

    # Media limits
    max_image_bytes: int = constants.DEFAULT_MAX_IMAGE_BYTES
    max_file_bytes: int = constants.DEFAULT_MAX_FILE_BYTES
    max_video_bytes: int = constants.DEFAULT_MAX_VIDEO_BYTES
    max_video_upload_bytes: int = constants.DEFAULT_MAX_VIDEO_UPLOAD_BYTES
    file_ref_threshold_bytes: int = constants.DEFAULT_FILE_REF_THRESHOLD_BYTES

    # Protocol & Headers
    ws_guid: str = constants.DEFAULT_WS_GUID
    user_agent: str = constants.DEFAULT_USER_AGENT

    # Timeouts
    gateway_dedup_s: int = constants.DEFAULT_GATEWAY_DEDUP_S
    download_timeout_s: int = constants.DEFAULT_DOWNLOAD_TIMEOUT_S
    push_timeout_s: int = constants.DEFAULT_PUSH_TIMEOUT_S
    push_grace_s: int = constants.DEFAULT_PUSH_GRACE_S
    poll_timeout_s: int = constants.DEFAULT_POLL_TIMEOUT_S
    poll_interval_s: int = constants.DEFAULT_POLL_INTERVAL_S
    poll_quiet_polls: int = constants.DEFAULT_POLL_QUIET_POLLS

    def get_default_private_target(self) -> Optional[str]:
        """Derive the first private openid from the whitelist."""
        if not self.whitelist:
            return None
        privates = self.whitelist.get("private")
        if isinstance(privates, list) and privates:
            for p in privates:
                if isinstance(p, str) and p and p != "*":
                    return p
        return None

    def get_default_group_target(self) -> Optional[str]:
        """Derive the first group openid from the whitelist."""
        if not self.whitelist:
            return None
        for k in self.whitelist:
            if k != "private" and isinstance(k, str) and k:
                return k
        return None

    def get_candidate_push_targets(self) -> List[tuple[str, str]]:
        """Return candidate push targets in order: specified target -> private targets -> group targets."""
        candidates = []
        if self.push.target_id:
            candidates.append((self.push.target_type, self.push.target_id))
            return candidates

        privates = self.whitelist.get("private")
        if isinstance(privates, list):
            for p in privates:
                if isinstance(p, str) and p and p != "*":
                    candidates.append(("private", p))

        for k in self.whitelist:
            if k != "private" and isinstance(k, str) and k:
                candidates.append(("group", k))

        return candidates

    def resolve_push_target(self) -> tuple[str, str]:
        """Resolve push target (type, target_id).

        Defaults to the first concrete private openid in whitelist.
        Falls back to the first group openid if no private openid exists.
        """
        candidates = self.get_candidate_push_targets()
        if candidates:
            return candidates[0]
        return "private", ""


@dataclasses.dataclass
class OneBotConfig:
    enabled: bool = False
    base_url: str = "https://bot.baoqing.eu.org:81"
    access_token: str = "baoqing666"
    ws_url: str = "wss://bot.baoqing.eu.org:81"
    target_group_id: Optional[int] = 836401578
    session_id: str = ""
    whitelist: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class AppConfig:
    muse: MuseConfig = dataclasses.field(default_factory=MuseConfig)
    qq: QQConfig = dataclasses.field(default_factory=QQConfig)
    onebot: OneBotConfig = dataclasses.field(default_factory=OneBotConfig)
    debug: bool = False


class ConfigLoader:
    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = ConfigLoader._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> AppConfig:
        raw_data: dict = {}
        target_path = config_path

        if not target_path:
            # Check default candidate filenames in order
            for name in ("config.json", "credentials.json"):
                if os.path.exists(name):
                    target_path = name
                    break

        if target_path and os.path.exists(target_path):
            with open(target_path, "r", encoding="utf-8") as f:
                try:
                    raw_data = json.load(f)
                except Exception as e:
                    raise RuntimeError(f"failed to parse config JSON in {target_path}: {e}")

        # If user has a const.json, merge as base
        if os.path.exists("const.json"):
            try:
                with open("const.json", "r", encoding="utf-8") as f:
                    const_data = json.load(f)
                    raw_data = cls._deep_merge(const_data, raw_data)
            except Exception:
                pass

        overrides = raw_data.get("overrides", {})

        def _get_val(section: dict, key: str, default: Any) -> Any:
            if key in overrides and overrides[key] is not None:
                return overrides[key]
            if key in section and section[key] is not None:
                return section[key]
            return default

        # 1. Muse config
        m_raw = raw_data.get("muse", {})
        muse_cfg = MuseConfig(
            mint_file=_get_val(m_raw, "mint_file", os.environ.get("MUSE_MINT_FILE") or ".mint.json"),
            cookies_file=_get_val(m_raw, "cookies_file", os.environ.get("MUSE_COOKIES_FILE") or "cookies.json"),
            session_id=_get_val(m_raw, "session_id", os.environ.get("MUSE_SESSION_ID") or ""),
            vm_id=_get_val(m_raw, "vm_id", os.environ.get("MUSE_VM_ID") or ""),
            host=_get_val(m_raw, "host", os.environ.get("MUSE_HOST") or constants.DEFAULT_MUSE_HOST),
            client_ip=_get_val(m_raw, "client_ip", os.environ.get("MUSE_CLIENT_IP") or ""),
            timezone=_get_val(m_raw, "timezone", os.environ.get("MUSE_TIMEZONE") or constants.DEFAULT_MUSE_TIMEZONE),
            proxy_url=_get_val(m_raw, "proxy_url", os.environ.get("MUSE_PROXY_URL")),
            direct=_get_val(m_raw, "direct", constants.DEFAULT_MUSE_DIRECT),
            reply_mode=_get_val(m_raw, "reply_mode", os.environ.get("MUSE_REPLY_MODE") or constants.DEFAULT_MUSE_REPLY_MODE),
        )

        if not muse_cfg.client_ip:
            detected = _detect_egress_ip(muse_cfg.proxy_url, muse_cfg.direct)
            if detected:
                muse_cfg.client_ip = detected

        # 2. QQ config (check adapters.qq or qq_official / qq)
        adapters_raw = raw_data.get("adapters", {})
        q_raw = adapters_raw.get("qq") or raw_data.get("qq_official") or raw_data.get("qq") or {}
        p_raw = q_raw.get("push") or adapters_raw.get("qq_push") or {}

        push_cfg = QQPushConfig(
            enabled=p_raw.get("enabled", True),
            target_type=p_raw.get("target_type", "private"),
            target_id=p_raw.get("target_id") or p_raw.get("target_user_openid") or p_raw.get("target_group_openid"),
            catch_up_enabled=bool(_get_val(p_raw, "catch_up_enabled", constants.DEFAULT_CATCH_UP_ENABLED)),
            catch_up_window_s=int(_get_val(p_raw, "catch_up_window_s", constants.DEFAULT_CATCH_UP_WINDOW_S)),
            catch_up_max_messages=int(_get_val(p_raw, "catch_up_max_messages", constants.DEFAULT_CATCH_UP_MAX_MESSAGES)),
            in_flight_ttl_s=int(_get_val(p_raw, "in_flight_ttl_s", constants.DEFAULT_IN_FLIGHT_TTL_S)),
            processing_ttl_s=int(_get_val(p_raw, "processing_ttl_s", constants.DEFAULT_PROCESSING_TTL_S)),
            periodic_scan_s=int(_get_val(p_raw, "periodic_scan_s", constants.DEFAULT_PERIODIC_SCAN_S)),
            turn_end_fallback_s=int(_get_val(p_raw, "turn_end_fallback_s", constants.DEFAULT_TURN_END_FALLBACK_S)),
            recheck_sleep_s=int(_get_val(p_raw, "recheck_sleep_s", constants.DEFAULT_RECHECK_SLEEP_S)),
            reconnect_sleep_s=int(_get_val(p_raw, "reconnect_sleep_s", constants.DEFAULT_RECONNECT_SLEEP_S)),
            state_file=p_raw.get("state_file", "qq_push_state.json"),
            delivered_file=p_raw.get("delivered_file", "qq_delivered.jsonl"),
        )

        qq_cfg = QQConfig(
            enabled=q_raw.get("enabled", True),
            app_id=str(q_raw.get("app_id") or os.environ.get("QQ_APP_ID") or ""),
            app_secret=str(q_raw.get("app_secret") or os.environ.get("QQ_APP_SECRET") or ""),
            session_id=str(q_raw.get("session_id") or muse_cfg.session_id or ""),
            whitelist=q_raw.get("whitelist") or {},
            push=push_cfg,
            session_state_file=q_raw.get("session_state_file", "qq_session.json"),
            delivered_file=q_raw.get("delivered_file", "qq_delivered.jsonl"),
            proxy_url=q_raw.get("proxy_url") or muse_cfg.proxy_url,
            download_proxy=q_raw.get("download_proxy") or "",
            debug=q_raw.get("debug", False) or _env_bool("QQ_DEBUG", False),
            chunk_chars=int(_get_val(q_raw, "chunk_chars", constants.DEFAULT_CHUNK_CHARS)),
            max_chunks=int(_get_val(q_raw, "max_chunks", constants.DEFAULT_MAX_CHUNKS)),
            max_active_chunks=int(_get_val(q_raw, "max_active_chunks", constants.DEFAULT_MAX_ACTIVE_CHUNKS)),
            max_image_bytes=int(_get_val(q_raw, "max_image_bytes", constants.DEFAULT_MAX_IMAGE_BYTES)),
            max_file_bytes=int(_get_val(q_raw, "max_file_bytes", constants.DEFAULT_MAX_FILE_BYTES)),
            max_video_bytes=int(_get_val(q_raw, "max_video_bytes", constants.DEFAULT_MAX_VIDEO_BYTES)),
            max_video_upload_bytes=int(_get_val(q_raw, "max_video_upload_bytes", constants.DEFAULT_MAX_VIDEO_UPLOAD_BYTES)),
            file_ref_threshold_bytes=int(_get_val(q_raw, "file_ref_threshold_bytes", constants.DEFAULT_FILE_REF_THRESHOLD_BYTES)),
            gateway_dedup_s=int(_get_val(q_raw, "gateway_dedup_s", constants.DEFAULT_GATEWAY_DEDUP_S)),
            download_timeout_s=int(_get_val(q_raw, "download_timeout_s", constants.DEFAULT_DOWNLOAD_TIMEOUT_S)),
            push_timeout_s=int(_get_val(q_raw, "push_timeout_s", constants.DEFAULT_PUSH_TIMEOUT_S)),
            push_grace_s=int(_get_val(q_raw, "push_grace_s", constants.DEFAULT_PUSH_GRACE_S)),
            poll_timeout_s=int(_get_val(q_raw, "poll_timeout_s", constants.DEFAULT_POLL_TIMEOUT_S)),
            poll_interval_s=int(_get_val(q_raw, "poll_interval_s", constants.DEFAULT_POLL_INTERVAL_S)),
            poll_quiet_polls=int(_get_val(q_raw, "poll_quiet_polls", constants.DEFAULT_POLL_QUIET_POLLS)),
            ws_guid=str(_get_val(q_raw, "ws_guid", constants.DEFAULT_WS_GUID)),
            user_agent=str(_get_val(q_raw, "user_agent", constants.DEFAULT_USER_AGENT)),
        )

        # 3. OneBot config
        ob_raw = adapters_raw.get("onebot") or raw_data.get("onebot") or {}
        ob_cfg = OneBotConfig(
            enabled=ob_raw.get("enabled", False),
            base_url=ob_raw.get("base_url", "https://bot.baoqing.eu.org:81"),
            access_token=ob_raw.get("access_token", "baoqing666"),
            ws_url=ob_raw.get("ws_url", "wss://bot.baoqing.eu.org:81"),
            target_group_id=ob_raw.get("target_group_id", 836401578),
            session_id=str(ob_raw.get("session_id") or muse_cfg.session_id or ""),
            whitelist=ob_raw.get("whitelist") or {},
        )

        return AppConfig(
            muse=muse_cfg,
            qq=qq_cfg,
            onebot=ob_cfg,
            debug=raw_data.get("debug", False) or qq_cfg.debug,
        )
