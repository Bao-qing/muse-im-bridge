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
from typing import Any, ClassVar, Dict, List, Optional, Union

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
class ApprovalConfig:
    mode: str = "manual"
    notify: bool = True

    def __post_init__(self):
        if self.mode not in {"manual", "allow_all", "deny_all"}:
            raise ValueError("muse.approval.mode must be manual, allow_all, or deny_all")
        if not isinstance(self.notify, bool):
            raise ValueError("muse.approval.notify must be a boolean")


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
    approval: ApprovalConfig = dataclasses.field(default_factory=ApprovalConfig)


@dataclasses.dataclass
class AccessRequestsConfig:
    enabled: bool = True
    auto_approve: bool = False
    state_file: str = "data/qq_access.local.json"

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.auto_approve) is not bool:
            raise ValueError("adapters.qq.access_requests switches must be booleans")
        if not isinstance(self.state_file, str) or not self.state_file.strip():
            raise ValueError("adapters.qq.access_requests.state_file must be a nonempty path")


def _normalize_whitelist(raw: Any) -> Dict[str, Union[List[str], str]]:
    """Whitelist as ``{"private": [user...], "<group id>": "*" | [user...]}``.

    Ids become strings (OneBot ids are numbers in JSON). The legacy OneBot
    form ``{"groups": [gid...]}`` opens each listed group to all members.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Union[List[str], str]] = {}
    for key, value in raw.items():
        if key == "groups" and isinstance(value, list):
            for gid in value:
                if gid is not None and str(gid).strip():
                    out[str(gid).strip()] = "*"
            continue
        if value == "*":
            out[str(key)] = "*"
        elif isinstance(value, list):
            out[str(key)] = [str(v) for v in value if v is not None and str(v).strip()]
    return out


@dataclasses.dataclass
class ChatConfig:
    """Settings shared by every chat platform behind the gateway layer."""

    # Validation messages name the config section, e.g. ``adapters.qq``.
    section: ClassVar[str] = "adapters.chat"

    enabled: bool = True
    admin_id: List[str] = dataclasses.field(default_factory=list)
    admin_only: bool = constants.DEFAULT_ADMIN_ONLY
    # Send messages of a session with no bound chat to the first admin.
    unbound_push_to_admin: bool = constants.DEFAULT_UNBOUND_PUSH_TO_ADMIN
    credentials_in_place: bool = False
    approvals_in_place: bool = constants.DEFAULT_APPROVALS_IN_PLACE
    access_requests: AccessRequestsConfig = dataclasses.field(default_factory=AccessRequestsConfig)
    whitelist: Dict[str, Union[List[str], str]] = dataclasses.field(default_factory=dict)
    delivered_file: str = "data/qq_delivered.jsonl"
    debug: bool = False

    # Each group / private user gets its own Muse session.
    session_map_file: str = "data/qq_session_map.json"
    # Optional pre-seeded bindings, e.g. {"group:<openid>": "<muse session id>"}
    session_bindings: Dict[str, str] = dataclasses.field(default_factory=dict)
    # Keep fetching a session's history this long after the last detected
    # activity (covers streaming replies), and do a full sweep every
    # `session_sweep_s` as a safety net.
    session_hot_window_s: int = 45
    session_sweep_s: int = 60

    # Outbound push: watermark file, startup catch-up and delivery timing.
    push_state_file: str = "data/qq_push_state.json"
    routes_file: str = "data/qq_routes.json"
    catch_up_enabled: bool = constants.DEFAULT_CATCH_UP_ENABLED
    catch_up_window_s: int = constants.DEFAULT_CATCH_UP_WINDOW_S
    catch_up_max_messages: int = constants.DEFAULT_CATCH_UP_MAX_MESSAGES
    recheck_sleep_s: int = constants.DEFAULT_RECHECK_SLEEP_S
    in_flight_ttl_s: int = constants.DEFAULT_IN_FLIGHT_TTL_S
    processing_ttl_s: int = constants.DEFAULT_PROCESSING_TTL_S

    # Chunking
    chunk_chars: int = constants.DEFAULT_CHUNK_CHARS
    max_chunks: int = constants.DEFAULT_MAX_CHUNKS

    # Media limits
    max_image_bytes: int = constants.DEFAULT_MAX_IMAGE_BYTES
    max_file_bytes: int = constants.DEFAULT_MAX_FILE_BYTES
    max_video_bytes: int = constants.DEFAULT_MAX_VIDEO_BYTES
    max_video_upload_bytes: int = constants.DEFAULT_MAX_VIDEO_UPLOAD_BYTES
    file_ref_threshold_bytes: int = constants.DEFAULT_FILE_REF_THRESHOLD_BYTES

    # Timeouts
    gateway_dedup_s: int = constants.DEFAULT_GATEWAY_DEDUP_S
    download_timeout_s: int = constants.DEFAULT_DOWNLOAD_TIMEOUT_S

    def __post_init__(self):
        for name in ("credentials_in_place", "approvals_in_place", "unbound_push_to_admin"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{self.section}.{name} must be a boolean")

    def is_admin(self, user_id: str) -> bool:
        """Whether the user id belongs to an administrator."""
        return bool(user_id and user_id in self.admin_id)

    def admin_target(self) -> Optional[str]:
        """First administrator id: private fallback for messages with no chat."""
        return next((a for a in self.admin_id if a and a != "*"), None)


@dataclasses.dataclass
class QQConfig(ChatConfig):
    section: ClassVar[str] = "adapters.qq"

    app_id: str = ""
    app_secret: str = ""
    proxy_url: Optional[str] = None
    download_proxy: str = ""
    # Active (non-reply) messages per outbound item; QQ limits active sends.
    max_active_chunks: int = constants.DEFAULT_MAX_ACTIVE_CHUNKS

    # Protocol & Headers
    ws_guid: str = constants.DEFAULT_WS_GUID
    user_agent: str = constants.DEFAULT_USER_AGENT

    push_timeout_s: int = constants.DEFAULT_PUSH_TIMEOUT_S
    push_grace_s: int = constants.DEFAULT_PUSH_GRACE_S
    poll_timeout_s: int = constants.DEFAULT_POLL_TIMEOUT_S
    poll_interval_s: int = constants.DEFAULT_POLL_INTERVAL_S
    poll_quiet_polls: int = constants.DEFAULT_POLL_QUIET_POLLS


def _onebot_access_requests() -> AccessRequestsConfig:
    return AccessRequestsConfig(state_file="data/onebot_access.local.json")


@dataclasses.dataclass
class OneBotConfig(ChatConfig):
    section: ClassVar[str] = "adapters.onebot"

    enabled: bool = False
    access_requests: AccessRequestsConfig = dataclasses.field(default_factory=_onebot_access_requests)
    delivered_file: str = "data/onebot_delivered.jsonl"
    session_map_file: str = "data/onebot_session_map.json"
    push_state_file: str = "data/onebot_push_state.json"
    routes_file: str = "data/onebot_routes.json"

    base_url: str = ""
    access_token: str = ""
    ws_url: str = ""
    # Which group messages reach Muse: "all", "at" (@bot or a / command), "prefix" (/ only).
    group_trigger: str = constants.DEFAULT_ONEBOT_GROUP_TRIGGER

    def __post_init__(self):
        super().__post_init__()
        self.whitelist = _normalize_whitelist(self.whitelist)
        self.admin_id = [str(a) for a in self.admin_id if a is not None and str(a).strip()]
        if self.group_trigger not in {"all", "at", "prefix"}:
            raise ValueError("adapters.onebot.group_trigger must be all, at, or prefix")


@dataclasses.dataclass
class GatewayConfig:
    login_through_adapter: str = ""


@dataclasses.dataclass
class LoggingConfig:
    level: str = "INFO"
    console: bool = True
    file: Optional[str] = "logs/bridge.log"
    trace: bool = False
    trace_file: Optional[str] = "logs/trace.jsonl"
    payload_limit: int = 65536
    max_bytes: int = 10485760
    backup_count: int = 5

    def __post_init__(self):
        if not isinstance(self.level, str):
            raise ValueError("logging.level must be a string")
        self.level = self.level.strip().upper()
        if self.level not in {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"}:
            raise ValueError("logging.level must be TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL, or OFF")
        for name in ("console", "trace"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"logging.{name} must be a boolean")
        for name, minimum in (("payload_limit", 0), ("max_bytes", 1), ("backup_count", 1)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"logging.{name} must be an integer >= {minimum}")
        for name in ("file", "trace_file"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"logging.{name} must be a path string or null")
            if isinstance(value, str) and ("\x00" in value or (value and not value.strip())):
                raise ValueError(f"logging.{name} must be a valid path or empty")
        if self.trace:
            if not self.trace_file:
                raise ValueError("logging.trace_file must be nonempty when logging.trace is true")
            if self.file and os.path.normcase(os.path.realpath(self.file)) == os.path.normcase(os.path.realpath(self.trace_file)):
                raise ValueError("logging.trace_file must differ from logging.file")


@dataclasses.dataclass
class AppConfig:
    muse: MuseConfig = dataclasses.field(default_factory=MuseConfig)
    qq: QQConfig = dataclasses.field(default_factory=QQConfig)
    onebot: OneBotConfig = dataclasses.field(default_factory=OneBotConfig)
    gateway: GatewayConfig = dataclasses.field(default_factory=GatewayConfig)
    debug: bool = False
    logging: LoggingConfig = dataclasses.field(default_factory=LoggingConfig)


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
        approval_raw = m_raw.get("approval", {})
        if not isinstance(approval_raw, dict):
            raise ValueError("muse.approval must be an object")
        approval_cfg = ApprovalConfig(
            mode=approval_raw.get("mode", "manual"),
            notify=approval_raw.get("notify", True),
        )
        muse_cfg = MuseConfig(
            approval=approval_cfg,
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

        # 2. Chat platforms (adapters.qq / adapters.onebot) share one field set.
        adapters_raw = raw_data.get("adapters", {})

        def _chat_fields(raw: dict, cls) -> Dict[str, Any]:
            defaults = {f.name: (f.default if f.default is not dataclasses.MISSING else f.default_factory())
                        for f in dataclasses.fields(cls)}
            admin_raw = raw.get("admin_id") or []
            if isinstance(admin_raw, list):
                admin_ids = [str(a) for a in admin_raw if a]
            elif isinstance(admin_raw, (str, int)) and admin_raw:
                admin_ids = [str(admin_raw)]
            else:
                admin_ids = []
            access_raw = raw.get("access_requests", {})
            if not isinstance(access_raw, dict):
                raise ValueError(f"{cls.section}.access_requests must be an object")
            access_cfg = AccessRequestsConfig(
                enabled=access_raw.get("enabled", True),
                auto_approve=access_raw.get("auto_approve", False),
                state_file=access_raw.get("state_file", defaults["access_requests"].state_file),
            )

            def _int(key: str) -> int:
                return int(_get_val(raw, key, defaults[key]))

            return dict(
                access_requests=access_cfg,
                enabled=raw.get("enabled", defaults["enabled"]),
                admin_id=admin_ids,
                admin_only=bool(_get_val(raw, "admin_only", constants.DEFAULT_ADMIN_ONLY)),
                unbound_push_to_admin=_get_val(raw, "unbound_push_to_admin",
                                               constants.DEFAULT_UNBOUND_PUSH_TO_ADMIN),
                credentials_in_place=raw.get("credentials_in_place", False),
                approvals_in_place=_get_val(raw, "approvals_in_place",
                                            constants.DEFAULT_APPROVALS_IN_PLACE),
                whitelist=raw.get("whitelist") or {},
                delivered_file=raw.get("delivered_file", defaults["delivered_file"]),
                session_map_file=raw.get("session_map_file", defaults["session_map_file"]),
                session_bindings={
                    str(k): str(v) for k, v in (raw.get("session_bindings") or {}).items() if k and v
                },
                session_hot_window_s=int(raw.get("session_hot_window_s", 45)),
                session_sweep_s=int(raw.get("session_sweep_s", 60)),
                push_state_file=raw.get("push_state_file", defaults["push_state_file"]),
                routes_file=raw.get("routes_file", defaults["routes_file"]),
                catch_up_enabled=bool(_get_val(raw, "catch_up_enabled", constants.DEFAULT_CATCH_UP_ENABLED)),
                **{key: _int(key) for key in (
                    "catch_up_window_s", "catch_up_max_messages", "recheck_sleep_s", "in_flight_ttl_s",
                    "processing_ttl_s", "chunk_chars", "max_chunks", "max_image_bytes", "max_file_bytes",
                    "max_video_bytes", "max_video_upload_bytes", "file_ref_threshold_bytes",
                    "gateway_dedup_s", "download_timeout_s")},
            )

        q_raw = adapters_raw.get("qq") or raw_data.get("qq_official") or raw_data.get("qq") or {}
        qq_cfg = QQConfig(
            **_chat_fields(q_raw, QQConfig),
            app_id=str(q_raw.get("app_id") or os.environ.get("QQ_APP_ID") or ""),
            app_secret=str(q_raw.get("app_secret") or os.environ.get("QQ_APP_SECRET") or ""),
            proxy_url=q_raw.get("proxy_url") or muse_cfg.proxy_url,
            download_proxy=q_raw.get("download_proxy") or "",
            debug=q_raw.get("debug", False) or _env_bool("QQ_DEBUG", False),
            max_active_chunks=int(_get_val(q_raw, "max_active_chunks", constants.DEFAULT_MAX_ACTIVE_CHUNKS)),
            push_timeout_s=int(_get_val(q_raw, "push_timeout_s", constants.DEFAULT_PUSH_TIMEOUT_S)),
            push_grace_s=int(_get_val(q_raw, "push_grace_s", constants.DEFAULT_PUSH_GRACE_S)),
            poll_timeout_s=int(_get_val(q_raw, "poll_timeout_s", constants.DEFAULT_POLL_TIMEOUT_S)),
            poll_interval_s=int(_get_val(q_raw, "poll_interval_s", constants.DEFAULT_POLL_INTERVAL_S)),
            poll_quiet_polls=int(_get_val(q_raw, "poll_quiet_polls", constants.DEFAULT_POLL_QUIET_POLLS)),
            ws_guid=str(_get_val(q_raw, "ws_guid", constants.DEFAULT_WS_GUID)),
            user_agent=str(_get_val(q_raw, "user_agent", constants.DEFAULT_USER_AGENT)),
        )

        # 3. OneBot config. The token may come from the environment instead of the file.
        ob_raw = adapters_raw.get("onebot") or raw_data.get("onebot") or {}
        ob_cfg = OneBotConfig(
            **_chat_fields(ob_raw, OneBotConfig),
            debug=bool(ob_raw.get("debug", False)),
            base_url=str(ob_raw.get("base_url") or os.environ.get("ONEBOT_BASE_URL") or ""),
            access_token=str(ob_raw.get("access_token") or os.environ.get("ONEBOT_ACCESS_TOKEN") or ""),
            ws_url=str(ob_raw.get("ws_url") or os.environ.get("ONEBOT_WS_URL") or ""),
            group_trigger=ob_raw.get("group_trigger", constants.DEFAULT_ONEBOT_GROUP_TRIGGER),
        )

        # 4. Gateway config
        gw_raw = raw_data.get("gateway", {})
        gw_cfg = GatewayConfig(login_through_adapter=str(gw_raw.get("login_through_adapter", "")))

        # 5. Logging config. An explicit level wins over legacy debug flags.
        debug = raw_data.get("debug", False) or qq_cfg.debug
        logging_raw = raw_data.get("logging", {})
        if not isinstance(logging_raw, dict):
            raise ValueError("logging must be an object")
        logging_fields = {field.name for field in dataclasses.fields(LoggingConfig)}
        if logging_raw.keys() - logging_fields:
            raise ValueError("logging contains unknown configuration fields")
        logging_cfg = LoggingConfig(**{"level": "DEBUG" if debug else "INFO", **logging_raw})

        return AppConfig(
            muse=muse_cfg,
            qq=qq_cfg,
            onebot=ob_cfg,
            gateway=gw_cfg,
            debug=debug,
            logging=logging_cfg,
        )
