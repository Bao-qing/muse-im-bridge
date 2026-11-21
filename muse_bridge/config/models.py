"""Configuration models and loader.

Distinguishes between user-editable credentials/settings and program defaults.
Supports deep-merging user config over internal defaults.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import math
from pathlib import Path
import json
import os
import re
from typing import Any, ClassVar, Dict, List, Optional, Union
from urllib.parse import urlsplit

from . import constants
from ..network import validate_proxy_mode, validate_proxy_url, validate_worker_base


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass
class ApprovalConfig:
    mode: str = "manual"
    notify: bool = True
    sync_interval_s: int = 180

    def __post_init__(self):
        if self.mode not in {"manual", "allow_all", "deny_all"}:
            raise ValueError("muse.approval.mode must be manual, allow_all, or deny_all")
        if not isinstance(self.notify, bool):
            raise ValueError("muse.approval.notify must be a boolean")
        if type(self.sync_interval_s) is not int or self.sync_interval_s <= 0:
            raise ValueError("muse.approval.sync_interval_s must be a positive integer")


@dataclasses.dataclass
class WorkerConfig:
    base: str = ""
    ws: bool = True
    http: bool = True

    def __post_init__(self):
        for name in ("ws", "http"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"network.worker.{name} must be a boolean")
        self.base = validate_worker_base(self.base)


@dataclasses.dataclass
class NetworkConfig:
    proxy_url: str = dataclasses.field(default="", repr=False)
    worker: WorkerConfig = dataclasses.field(default_factory=WorkerConfig)

    def __post_init__(self):
        if not isinstance(self.worker, WorkerConfig):
            raise ValueError("network.worker must be a WorkerConfig")
        self.proxy_url = validate_proxy_url(self.proxy_url)


@dataclasses.dataclass
class MuseConfig:
    mint_file: str = ".mint.json"
    cookies_file: str = "cookies.json"
    session_id: str = ""
    vm_id: str = ""
    host: str = constants.DEFAULT_MUSE_HOST
    client_ip: str = ""
    timezone: str = constants.DEFAULT_MUSE_TIMEZONE
    proxy: str = "none"
    approval: ApprovalConfig = dataclasses.field(default_factory=ApprovalConfig)

    def __post_init__(self):
        self.proxy = validate_proxy_mode(self.proxy, "muse")


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
class ConversationConfig:
    """Shared session, delivery and media runtime settings; no access policy."""

    # Validation messages name the config section, e.g. ``adapters.qq``.
    section: ClassVar[str] = "adapters.chat"

    enabled: bool = True
    proxy: str = "none"
    credentials_in_place: bool = constants.DEFAULT_CREDENTIALS_IN_PLACE
    approvals_in_place: bool = constants.DEFAULT_APPROVALS_IN_PLACE
    reaction_quote: bool = constants.DEFAULT_REACTION_QUOTE
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
    reconnect_max_retries: int = constants.DEFAULT_RECONNECT_MAX_RETRIES
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
        self.proxy = validate_proxy_mode(self.proxy, self.section)
        for name in ("credentials_in_place", "approvals_in_place", "reaction_quote"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{self.section}.{name} must be a boolean")
        if type(self.reconnect_max_retries) is not int or self.reconnect_max_retries < 0:
            raise ValueError(f"{self.section}.reconnect_max_retries must be a non-negative integer")


@dataclasses.dataclass
class ChatConfig(ConversationConfig):
    """Group-capable adapter access policy and conversation defaults."""

    seed_main_session: bool = True
    admin_id: List[str] = dataclasses.field(default_factory=list)
    admin_only: bool = constants.DEFAULT_ADMIN_ONLY
    unbound_push_to_admin: bool = constants.DEFAULT_UNBOUND_PUSH_TO_ADMIN
    allow_all_private: bool = constants.DEFAULT_ALLOW_ALL_PRIVATE
    allow_all_group: bool = constants.DEFAULT_ALLOW_ALL_GROUP
    group_sender_info: bool = True
    access_requests: AccessRequestsConfig = dataclasses.field(default_factory=AccessRequestsConfig)
    whitelist: Dict[str, Union[List[str], str]] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        for name in ("seed_main_session", "unbound_push_to_admin", "allow_all_private",
                     "allow_all_group", "group_sender_info"):
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


@dataclasses.dataclass
class QzoneFeedConfig:
    """Opt-in Muse feed publishing; independent of chat message delivery."""

    enabled: bool = False
    poll_interval_s: int = 300
    max_age_s: int = 3600
    scan_limit: int = 50
    max_per_scan: int = 3
    min_publish_interval_s: int = 60
    publish_window_s: int = 3600
    max_per_window: int = 5
    ugc_right: int = 4
    state_file: str = "data/onebot_qzone_feed.local.json"

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("adapters.onebot.qzone_feed.enabled must be a boolean")
        for name in ("poll_interval_s", "max_age_s", "scan_limit", "max_per_scan",
                     "min_publish_interval_s", "publish_window_s", "max_per_window"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"adapters.onebot.qzone_feed.{name} must be a positive integer")
        if self.scan_limit > 100 or self.max_per_scan > self.scan_limit:
            raise ValueError("qzone_feed requires max_per_scan <= scan_limit <= 100")
        if type(self.ugc_right) is not int or self.ugc_right not in {1, 4, 64}:
            raise ValueError("qzone_feed.ugc_right must be 1 (public), 4 (friends), or 64 (self)")
        if not isinstance(self.state_file, str) or not self.state_file.strip():
            raise ValueError("qzone_feed.state_file must be a nonempty path")


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
    typing_enabled: bool = False
    qzone_feed: QzoneFeedConfig = dataclasses.field(default_factory=QzoneFeedConfig)

    def __post_init__(self):
        super().__post_init__()
        self.whitelist = _normalize_whitelist(self.whitelist)
        self.admin_id = [str(a) for a in self.admin_id if a is not None and str(a).strip()]
        if type(self.typing_enabled) is not bool:
            raise ValueError("adapters.onebot.typing_enabled must be a boolean")
        if not isinstance(self.qzone_feed, QzoneFeedConfig):
            raise ValueError("adapters.onebot.qzone_feed must be a QzoneFeedConfig")
        if self.group_trigger not in {"all", "at", "prefix"}:
            raise ValueError("adapters.onebot.group_trigger must be all, at, or prefix")


@dataclasses.dataclass
class TelegramConfig(ChatConfig):
    section: ClassVar[str] = "adapters.telegram"

    enabled: bool = False
    proxy: str = "worker"
    seed_main_session: bool = False
    access_requests: AccessRequestsConfig = dataclasses.field(
        default_factory=lambda: AccessRequestsConfig(state_file="data/telegram_access.local.json"))
    delivered_file: str = "data/telegram_delivered.jsonl"
    session_map_file: str = "data/telegram_session_map.json"
    push_state_file: str = "data/telegram_push_state.json"
    routes_file: str = "data/telegram_routes.json"

    bot_token: str = dataclasses.field(default="", repr=False)
    group_trigger: str = "at"
    poll_timeout_s: int = 30

    def __post_init__(self):
        super().__post_init__()
        self.whitelist = _normalize_whitelist(self.whitelist)
        self.admin_id = [str(a) for a in self.admin_id if a is not None and str(a).strip()]
        if type(self.enabled) is not bool:
            raise ValueError("adapters.telegram.enabled must be a boolean")
        if not isinstance(self.bot_token, str) or ((self.enabled or self.bot_token)
                and not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]+", self.bot_token)):
            raise ValueError("adapters.telegram requires a valid bot_token (or TELEGRAM_BOT_TOKEN)")
        if self.group_trigger not in ("all", "at", "prefix"):
            raise ValueError("adapters.telegram.group_trigger must be all, at, or prefix")
        if type(self.poll_timeout_s) is not int or not 1 <= self.poll_timeout_s <= 50:
            raise ValueError("adapters.telegram.poll_timeout_s must be an integer between 1 and 50")
        if self.enabled and (not isinstance(self.routes_file, str) or not self.routes_file.strip()):
            raise ValueError("adapters.telegram.routes_file is required for the polling cursor")


@dataclasses.dataclass
class WeChatConfig(ConversationConfig):
    """Private-only iLink channel; runtime state is isolated by bot identity."""

    section: ClassVar[str] = "adapters.wechat"
    enabled: bool = False
    chunk_chars: int = 4000
    state_dir: str = "data/wechat"
    channel_version: str = "2.4.6"
    route_tag: str = ""
    http_timeout_s: float = 35
    polling_timeout_ms: int = 35000
    typing_enabled: bool = False
    delivered_file: str = "data/wechat/delivered.jsonl"
    session_map_file: str = "data/wechat/session_map.json"
    push_state_file: str = "data/wechat/push_state.json"
    routes_file: str = "data/wechat/routes.json"

    def __post_init__(self):
        self.proxy = validate_proxy_mode(self.proxy, self.section)
        # WeChat preserves raw types instead of the legacy adapters' coercion.
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(field.default) is bool and type(value) is not bool:
                raise ValueError(f"{self.section}.{field.name} must be a boolean")
            if (type(field.default) is int and field.name != "http_timeout_s"
                    and (type(value) is not int or value <= 0)):
                raise ValueError(f"{self.section}.{field.name} must be a positive integer")
        if not 1 <= self.chunk_chars <= 4000:
            raise ValueError(f"{self.section}.chunk_chars must be in 1..4000")
        try:
            valid_timeout = (type(self.http_timeout_s) in (int, float)
                             and math.isfinite(self.http_timeout_s) and self.http_timeout_s > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError(f"{self.section}.http_timeout_s must be finite and positive")
        for name in ("state_dir", "channel_version", "delivered_file", "session_map_file",
                     "push_state_file", "routes_file"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{self.section}.{name} must be a nonempty string")
        if not isinstance(self.route_tag, str):
            raise ValueError(f"{self.section}.route_tag must be a string")
        if not isinstance(self.session_bindings, dict) or any(
                not isinstance(k, str) or not k.startswith("private:") or not k[8:]
                or not isinstance(v, str) or not v.strip()
                for k, v in self.session_bindings.items()):
            raise ValueError(f"{self.section}.session_bindings only supports private:<user> bindings")

    def for_account(self, bot_id: str) -> "WeChatConfig":
        if not isinstance(bot_id, str) or not bot_id.strip():
            raise ValueError("WeChat bot_id must be nonempty")
        account_dir = Path(self.state_dir) / "accounts" / hashlib.sha256(bot_id.encode("utf-8")).hexdigest()[:24]
        config = copy.deepcopy(self)
        for field, filename in (("delivered_file", "delivered.jsonl"),
                                ("session_map_file", "session_map.json"),
                                ("push_state_file", "push_state.json"),
                                ("routes_file", "routes.json")):
            setattr(config, field, str(account_dir / filename))
        return config


@dataclasses.dataclass
class GatewayFeedConfig:
    """Gateway-owned Muse feed publishing across mounted adapters."""

    enabled: bool = False
    targets: Dict[str, str] = dataclasses.field(default_factory=dict)
    poll_interval_s: int = 300
    max_age_s: int = 3600
    scan_limit: int = 50
    max_per_scan: int = 3
    min_publish_interval_s: int = 60
    publish_window_s: int = 3600
    max_per_window: int = 5

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("gateway.feed.enabled must be a boolean")
        if not isinstance(self.targets, dict):
            raise ValueError("gateway.feed.targets must be an object")
        normalized: Dict[str, str] = {}
        for key, mode in self.targets.items():
            if key not in {"qq", "onebot", "wechat"}:
                raise ValueError("gateway.feed.targets keys must be qq, onebot, or wechat")
            if mode not in {"auto", "native", "chat"}:
                raise ValueError("gateway.feed.targets values must be auto, native, or chat")
            normalized[key] = mode
        self.targets = normalized
        for name in ("poll_interval_s", "max_age_s", "scan_limit", "max_per_scan",
                     "min_publish_interval_s", "publish_window_s", "max_per_window"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"gateway.feed.{name} must be a positive integer")
        if self.scan_limit > 100 or self.max_per_scan > self.scan_limit:
            raise ValueError("gateway.feed requires max_per_scan <= scan_limit <= 100")


@dataclasses.dataclass
class GatewayConfig:
    background_approval_push: str = ""
    feed: GatewayFeedConfig = dataclasses.field(default_factory=GatewayFeedConfig)

    def __post_init__(self):
        if not isinstance(self.feed, GatewayFeedConfig):
            raise ValueError("gateway.feed must be a GatewayFeedConfig")
        value = self.background_approval_push
        if not isinstance(value, str):
            raise ValueError("gateway.background_approval_push must be a string")
        self.background_approval_push = value = value.strip()
        if value:
            platform, separator, target = value.partition(":")
            if (platform not in {"qq", "onebot"} or not separator or not target.strip()
                    or target != target.strip() or ":" in target or any(c.isspace() for c in target)):
                raise ValueError("gateway.background_approval_push must be qq:<admin id>, onebot:<admin id>, or empty")


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
    wechat: WeChatConfig = dataclasses.field(default_factory=WeChatConfig)
    telegram: TelegramConfig = dataclasses.field(default_factory=TelegramConfig)
    network: NetworkConfig = dataclasses.field(default_factory=NetworkConfig)

    def __post_init__(self):
        if self.gateway.background_approval_push:
            platform, target = self.gateway.background_approval_push.split(":", 1)
            adapter = getattr(self, platform)
            if not adapter.enabled or not adapter.is_admin(target) or target == "*":
                raise ValueError("gateway.background_approval_push must name an enabled adapter's configured administrator")
        if self.muse.proxy == "http" and not self.network.proxy_url:
            raise ValueError("muse.proxy='http' requires network.proxy_url")
        for name in ("qq", "onebot", "wechat", "telegram"):
            adapter = getattr(self, name)
            if adapter.enabled and adapter.proxy == "http" and not self.network.proxy_url:
                raise ValueError(f"adapters.{name}.proxy='http' requires network.proxy_url")


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
    def _read_raw(cls, config_path: Optional[str] = None) -> dict:
        """Read configuration only, with the same legacy const.json precedence."""
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

        return raw_data

    @classmethod
    def _wechat_from_raw(cls, raw_data: dict) -> WeChatConfig:
        adapters = raw_data.get("adapters", {})
        if not isinstance(adapters, dict):
            raise ValueError("adapters must be an object")
        # An old config without this new channel must retain its old semantics:
        # do not impose WeChat-only limits on legacy QQ/OneBot overrides.
        if "wechat" not in adapters:
            return WeChatConfig()
        raw = adapters["wechat"]
        if not isinstance(raw, dict):
            raise ValueError("adapters.wechat must be an object")
        # Old asdict output included inherited QQ/OneBot policy. Discard only
        # these removed keys; neither their values nor files become new policy.
        removed_policy = {"admin_id", "admin_only", "whitelist", "allow_all_private",
                          "allow_all_group", "allow_all_groups", "access_requests",
                          "unbound_push_to_admin", "group_sender_info"}
        values = {key: value for key, value in raw.items() if key not in removed_policy}
        fields = {f.name for f in dataclasses.fields(WeChatConfig)}
        if values.keys() - fields:
            raise ValueError("adapters.wechat contains unknown configuration fields")
        overrides = raw_data.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        # Preserve raw types: unlike legacy adapters no bool()/int() coercion.
        for key in fields - {"enabled", "proxy", "session_bindings"}:
            if key in overrides and overrides[key] is not None:
                values[key] = overrides[key]
        return WeChatConfig(**values)

    @classmethod
    def _network_from_raw(cls, raw_data: dict) -> NetworkConfig:
        net_raw = raw_data.get("network", {})
        if not isinstance(net_raw, dict):
            raise ValueError("network must be an object")
        net_fields = {f.name for f in dataclasses.fields(NetworkConfig)}
        if net_raw.keys() - net_fields:
            raise ValueError("network contains unknown configuration fields")
        worker_raw = net_raw.get("worker", {})
        if not isinstance(worker_raw, dict):
            raise ValueError("network.worker must be an object")
        worker_fields = {f.name for f in dataclasses.fields(WorkerConfig)}
        if worker_raw.keys() - worker_fields:
            raise ValueError("network.worker contains unknown configuration fields")
        proxy_url = net_raw.get("proxy_url", "")
        if proxy_url is None:
            proxy_url = ""
        return NetworkConfig(
            proxy_url=proxy_url,
            worker=WorkerConfig(**worker_raw),
        )

    @classmethod
    def load_network(cls, config_path: Optional[str] = None) -> NetworkConfig:
        """Offline CLI loader for the top-level network configuration."""
        return cls._network_from_raw(cls._read_raw(config_path))

    @classmethod
    def load_wechat(cls, config_path: Optional[str] = None) -> WeChatConfig:
        """Offline CLI loader: never initializes Muse or detects an egress IP."""
        return cls._wechat_from_raw(cls._read_raw(config_path))

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> AppConfig:
        raw_data = cls._read_raw(config_path)
        network_cfg = cls._network_from_raw(raw_data)
        wechat_cfg = cls._wechat_from_raw(raw_data)
        overrides = raw_data.get("overrides", {})

        def _get_val(section: dict, key: str, default: Any) -> Any:
            if key in overrides and overrides[key] is not None:
                return overrides[key]
            if key in section and section[key] is not None:
                return section[key]
            return default

        # 1. Muse config
        m_raw = raw_data.get("muse", {})
        if not isinstance(m_raw, dict):
            raise ValueError("muse must be an object")
        approval_raw = m_raw.get("approval", {})
        if not isinstance(approval_raw, dict):
            raise ValueError("muse.approval must be an object")
        approval_cfg = ApprovalConfig(
            mode=approval_raw.get("mode", "manual"),
            notify=approval_raw.get("notify", True),
            sync_interval_s=approval_raw.get("sync_interval_s", 180),
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
            proxy=m_raw.get("proxy", "none"),
        )

        # 2. Group-capable adapters share one field set.
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

            val_grp = _get_val(raw, "allow_all_group", None)
            if val_grp is None:
                val_grp = _get_val(raw, "allow_all_groups", constants.DEFAULT_ALLOW_ALL_GROUP)

            return dict(
                access_requests=access_cfg,
                enabled=raw.get("enabled", defaults["enabled"]),
                proxy=raw.get("proxy", defaults["proxy"]),
                admin_id=admin_ids,
                admin_only=bool(_get_val(raw, "admin_only", constants.DEFAULT_ADMIN_ONLY)),
                seed_main_session=_get_val(raw, "seed_main_session", defaults["seed_main_session"]),
                unbound_push_to_admin=_get_val(raw, "unbound_push_to_admin",
                                               constants.DEFAULT_UNBOUND_PUSH_TO_ADMIN),
                credentials_in_place=_get_val(raw, "credentials_in_place", defaults["credentials_in_place"]),
                approvals_in_place=_get_val(raw, "approvals_in_place",
                                            constants.DEFAULT_APPROVALS_IN_PLACE),
                reaction_quote=_get_val(raw, "reaction_quote", defaults["reaction_quote"]),
                allow_all_private=_get_val(raw, "allow_all_private",
                                           constants.DEFAULT_ALLOW_ALL_PRIVATE),
                allow_all_group=val_grp,
                group_sender_info=_get_val(raw, "group_sender_info", True),
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
                    "catch_up_window_s", "catch_up_max_messages", "recheck_sleep_s",
                    "reconnect_max_retries", "in_flight_ttl_s",
                    "processing_ttl_s", "chunk_chars", "max_chunks", "max_image_bytes", "max_file_bytes",
                    "max_video_bytes", "max_video_upload_bytes", "file_ref_threshold_bytes",
                    "gateway_dedup_s", "download_timeout_s")},
            )

        q_raw = adapters_raw.get("qq") or raw_data.get("qq_official") or raw_data.get("qq") or {}
        qq_cfg = QQConfig(
            **_chat_fields(q_raw, QQConfig),
            app_id=str(q_raw.get("app_id") or os.environ.get("QQ_APP_ID") or ""),
            app_secret=str(q_raw.get("app_secret") or os.environ.get("QQ_APP_SECRET") or ""),
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
        feed_raw = ob_raw.get("qzone_feed", {})
        feed_fields = {f.name for f in dataclasses.fields(QzoneFeedConfig)}
        if not isinstance(feed_raw, dict) or feed_raw.keys() - feed_fields:
            raise ValueError("adapters.onebot.qzone_feed must be an object with known fields")
        ob_cfg = OneBotConfig(
            qzone_feed=QzoneFeedConfig(**feed_raw),
            **_chat_fields(ob_raw, OneBotConfig),
            debug=bool(ob_raw.get("debug", False)),
            base_url=str(ob_raw.get("base_url") or os.environ.get("ONEBOT_BASE_URL") or ""),
            access_token=str(ob_raw.get("access_token") or os.environ.get("ONEBOT_ACCESS_TOKEN") or ""),
            ws_url=str(ob_raw.get("ws_url") or os.environ.get("ONEBOT_WS_URL") or ""),
            group_trigger=ob_raw.get("group_trigger", constants.DEFAULT_ONEBOT_GROUP_TRIGGER),
            typing_enabled=(
                overrides["typing_enabled"]
                if ("typing_enabled" in overrides and overrides["typing_enabled"] is not None)
                else ob_raw.get("typing_enabled", False)
            ),
        )

        tg_raw = adapters_raw.get("telegram", {})
        if not isinstance(tg_raw, dict):
            raise ValueError("adapters.telegram must be an object")
        telegram_cfg = TelegramConfig(
            **_chat_fields(tg_raw, TelegramConfig),
            bot_token=tg_raw.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            group_trigger=tg_raw.get("group_trigger", "at"),
            poll_timeout_s=tg_raw.get("poll_timeout_s", 30),
        )

        # 4. Gateway config
        gw_raw = raw_data.get("gateway", {})
        if not isinstance(gw_raw, dict):
            raise ValueError("gateway must be an object")
        gw_feed_raw = gw_raw.get("feed", {})
        gw_feed_fields = {f.name for f in dataclasses.fields(GatewayFeedConfig)}
        if not isinstance(gw_feed_raw, dict) or gw_feed_raw.keys() - gw_feed_fields:
            raise ValueError("gateway.feed must be an object with known fields")
        gw_cfg = GatewayConfig(
            background_approval_push=gw_raw.get("background_approval_push", ""),
            feed=GatewayFeedConfig(**gw_feed_raw),
        )

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
            wechat=wechat_cfg,
            telegram=telegram_cfg,
            gateway=gw_cfg,
            debug=debug,
            logging=logging_cfg,
            network=network_cfg,
        )
