#!/usr/bin/env python3
"""Muse IM Bridge 日常运维与首次配置控制台 (Daily Console & Setup Wizard)

统一面向首次启动与日常运维：
1. 【日常控制台】：顶部状态面板 + 渠道管理（配置/开关/发消息认领/单渠道测通）+
   账号登录（Muse / 微信）+ 一键连通性体检 + 高级全量配置 + 启动网桥 + 重新初始化；
2. 【首次引导向导】：自由渠道配置列表（QQ / OneBot / Telegram / 微信）、凭据即时校验、
   微信精简扫码引导、逐渠道发消息认领管理员（支持跳过/手动兜底）、无缝衔接 Muse 登录。
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import re
import shutil
import ssl
import sys
import unicodedata
import uuid
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# 确保项目根目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from muse_bridge.config import constants
from muse_bridge.config.models import (
    AccessRequestsConfig,
    ApprovalConfig,
    ConfigLoader,
    GatewayConfig,
    GatewayFeedConfig,
    LoggingConfig,
    MuseConfig,
    OneBotConfig,
    QQConfig,
    QzoneFeedConfig,
    TelegramConfig,
    WeChatConfig,
    WorkerConfig,
)
from muse_bridge.observability import configure_logging, get_logger, safe_error

logger = get_logger("setup_wizard")

CHANNEL_ORDER = ("qq", "onebot", "telegram", "wechat")
CLAIMABLE_CHANNELS = ("qq", "onebot", "telegram")
CHANNEL_LABELS = {
    "qq": "QQ 官方机器人",
    "onebot": "OneBot v11 (NapCat)",
    "telegram": "Telegram Bot",
    "wechat": "微信 iLink（仅私聊）",
}
_TG_TOKEN_RE = re.compile(r"^[1-9][0-9]*:[A-Za-z0-9_-]+$")


# ----------------------------------------------------------------------
# 交互与校验辅助函数
# ----------------------------------------------------------------------

_LAST_INPUT_EMPTY: bool = False


def _prompt_input(prompt: str, default: str = "") -> str:
    global _LAST_INPUT_EMPTY
    display_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        val = input(display_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[!] 已退出（控制台内已完成的修改均已实时保存）。")
        sys.exit(0)
    _LAST_INPUT_EMPTY = (val == "")
    return val if val else default


def _prompt_edit_str(title: str, current_disp: str, *, allow_clear: bool = False) -> Tuple[bool, Optional[str]]:
    clear_hint = "输入 clear 清空，" if allow_clear else ""
    raw = _prompt_input(f"{title}（当前: {current_disp}；{clear_hint}直接回车不修改）", default="").strip()
    if not raw:
        return False, None
    if allow_clear and raw.lower() in ("clear", "none", "null"):
        return True, ""
    return True, raw


def _prompt_edit_nullable_str(title: str, current_disp: str) -> Tuple[bool, Optional[str]]:
    raw = _prompt_input(f"{title}（当前: {current_disp}；输入 clear 禁用，直接回车不修改）", default="").strip()
    if not raw:
        return False, None
    if raw.lower() in ("clear", "none", "null"):
        return True, None
    return True, raw


def _prompt_edit_proxy(title: str, current_disp: str) -> Tuple[bool, Optional[str]]:
    raw = _prompt_input(f"{title}（当前: {current_disp}；输入 clear 恢复直连，直接回车不修改）", default="").strip()
    if not raw:
        return False, None
    try:
        return True, _validate_proxy_url(raw)
    except ValueError as exc:
        print(f"❌ {exc}")
        return False, None


def _prompt_edit_bool(title: str, current_disp: str) -> Tuple[bool, Optional[bool]]:
    while True:
        raw = _prompt_input(f"{title} (y/n，当前: {current_disp}，直接回车不修改)", default="").strip().lower()
        if not raw:
            return False, None
        if raw in ("y", "yes", "true", "1"):
            return True, True
        if raw in ("n", "no", "false", "0"):
            return True, False
        print("❌ 请输入 y 或 n（直接回车不修改）。")


def _prompt_edit_int(
    title: str,
    current_disp: str,
    *,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> Tuple[bool, Optional[int]]:
    while True:
        raw = _prompt_input(f"{title}（当前: {current_disp}，直接回车不修改）", default="").strip()
        if not raw:
            return False, None
        try:
            val = int(raw)
        except ValueError:
            print("❌ 请输入有效的整数（直接回车不修改）。")
            continue
        if min_val is not None and val < min_val:
            print(f"❌ 数值不能小于 {min_val}。")
            continue
        if max_val is not None and val > max_val:
            print(f"❌ 数值不能大于 {max_val}。")
            continue
        return True, val


def _prompt_edit_choice(title: str, choices: List[str], current_disp: str) -> Tuple[bool, Optional[str]]:
    disp_choices = "/".join(c if c else "clear清空" for c in choices)
    while True:
        raw = _prompt_input(f"{title} ({disp_choices}，当前: {current_disp}，直接回车不修改)", default="").strip()
        if not raw:
            return False, None
        if raw in choices:
            return True, raw
        if "" in choices and raw.lower() in ("none", "clear", "留空"):
            return True, ""
        print(f"❌ 请在选项目录中选择: {disp_choices}（直接回车不修改）")


def _prompt_edit_str_list(title: str, current_val: Any) -> Tuple[bool, Optional[List[str]]]:
    curr_list = current_val if isinstance(current_val, list) else []
    disp = ", ".join(str(x) for x in curr_list) if curr_list else "空"
    print(f"当前 {title}: {disp}")
    print("输入格式：以英文或中文逗号、空格分隔的列表；输入 clear 清空；直接回车不修改。")
    raw = _prompt_input("请输入新值", default="").strip()
    if not raw:
        return False, None
    if raw.lower() in ("clear", "none", "[]", "空"):
        return True, []
    items = re.split(r"[,，\s]+", raw)
    return True, [i.strip() for i in items if i.strip()]


def _prompt_str(title: str, current_val: Any, default_val: str = "") -> str:
    curr_str = "" if current_val is None else str(current_val)
    disp = curr_str if curr_str else default_val
    return _prompt_input(f"{title}", default=disp)


def _prompt_bool(title: str, current_val: Any) -> bool:
    curr_bool = bool(current_val) if current_val is not None else False
    disp = "Y/n" if curr_bool else "y/N"
    while True:
        raw = _prompt_input(f"{title} ({disp})", default="y" if curr_bool else "n").strip().lower()
        if raw in ("y", "yes", "true", "1"):
            return True
        if raw in ("n", "no", "false", "0"):
            return False
        print("❌ 请输入 y 或 n。")


def _prompt_int(title: str, current_val: Any, default_val: int = 0) -> int:
    curr_int = int(current_val) if (current_val is not None and str(current_val).isdigit()) else default_val
    while True:
        raw = _prompt_input(f"{title}", default=str(curr_int))
        try:
            return int(raw)
        except ValueError:
            print("❌ 请输入有效的整数。")


def _prompt_choice(title: str, choices: List[str], current_val: str) -> str:
    disp_choices = "/".join(c if c else "留空" for c in choices)
    while True:
        raw = _prompt_input(f"{title} ({disp_choices})", default=current_val).strip()
        if raw in choices:
            return raw
        if "" in choices and raw.lower() in ("none", "clear", "留空"):
            return ""
        print(f"❌ 请在选项目录中选择: {disp_choices}")


def _prompt_str_list(title: str, current_val: Any) -> List[str]:
    curr_list = current_val if isinstance(current_val, list) else []
    disp = ", ".join(str(x) for x in curr_list) if curr_list else "空"
    print(f"当前 {title}: {disp}")
    print("输入格式：以英文或中文逗号、空格分隔的列表；输入 clear 清空；直接回车保持不变。")
    raw = _prompt_input("请输入新值", default="").strip()
    if not raw:
        return [str(x) for x in curr_list]
    if raw.lower() in ("clear", "none", "[]", "空"):
        return []
    items = re.split(r"[,，\s]+", raw)
    return [i.strip() for i in items if i.strip()]


def _default_ws_url(base_url: str) -> str:
    url = base_url.strip()
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    return f"ws://{url}"


def _normalize_http_url(raw: str) -> str:
    url = raw.strip()
    if not url:
        return ""
    if url.isdigit():
        return f"http://127.0.0.1:{url}"
    if not url.startswith("http://") and not url.startswith("https://"):
        return "http://" + url
    return url


def _mask_secret(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return "******"
    return f"{value[:4]}***{value[-4:]}"


def _validate_proxy_url(raw: str) -> Optional[str]:
    val = (raw or "").strip()
    if not val or val.lower() in ("none", "clear", "direct", "无", "直连"):
        return None
    if not val.startswith("http://") and not val.startswith("https://"):
        val = "http://" + val
    parsed = urlsplit(val)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port == 0:
        raise ValueError("代理地址格式无效，请输入如 http://127.0.0.1:7890 的 HTTP(S) 地址")
    return val


def _validate_telegram_token(token: str) -> bool:
    return bool(token and _TG_TOKEN_RE.fullmatch(token.strip()))


def _backup_config_file(file_path: str) -> Optional[str]:
    if not file_path or not os.path.exists(file_path):
        return None
    backup_path = f"{file_path}.bak"
    try:
        shutil.copy2(file_path, backup_path)
        return backup_path
    except Exception as exc:
        logger.debug("Backup failed for %s: %s", file_path, safe_error(exc))
        return None


def _save_json_file(file_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_json_file(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, dict) else {}


def _prompt_group_trigger(current: str = "at") -> str:
    print("请选择群聊触发机器人回复的方式：")
    print("  [1] 仅 @机器人 或 /指令 时回复（推荐，防群内刷屏）")
    print("  [2] 响应群内所有消息")
    print("  [3] 仅响应 /指令")
    default_num = {"at": "1", "all": "2", "prefix": "3"}.get(current, "1")
    while True:
        choice = _prompt_input("请输入编号 (1/2/3)", default=default_num).strip().lower()
        if choice in ("1", "at"):
            return "at"
        if choice in ("2", "all"):
            return "all"
        if choice in ("3", "prefix"):
            return "prefix"
        print("❌ 请输入 1、2 或 3。")


def _parse_session_bindings_input(raw: str) -> Optional[Dict[str, str]]:
    text = raw.strip()
    if not text:
        return None
    if text.lower() in ("clear", "none", "{}"):
        return {}
    result: Dict[str, str] = {}
    for item in re.split(r"[,，\s]+", text):
        if "=" in item:
            k, _, v = item.partition("=")
            if k.strip() and v.strip():
                result[k.strip()] = v.strip()
    return result


# ----------------------------------------------------------------------
# 终端宽字符对齐与全量字段元信息注册表
# ----------------------------------------------------------------------

def _display_width(text: str) -> int:
    """计算字符串在等宽终端中的显示列宽（中文及全角符号占 2 列）。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ord(ch) in (0x200D, 0xFE0E, 0xFE0F):
            continue
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def _pad_right(text: str, target_width: int) -> str:
    pad = max(0, target_width - _display_width(text))
    return text + (" " * pad)


def _format_channel_row(choice: Optional[str], label: str, status: str, *, label_width: int = 24) -> str:
    padded = _pad_right(label, label_width)
    if choice is None:
        return f"  {padded} : {status}"
    return f"  [{choice}] {padded} {status}"


def _format_menu_row(
    choice: str,
    label: str,
    value: str,
    *,
    idx_width: int = 4,
    label_width: int = 46,
) -> str:
    tag = _pad_right(f"[{choice}]", idx_width)
    left = _pad_right(label, label_width)
    return f"  {tag} {left} : {value}"


def _format_value_repr(
    val: Any,
    *,
    kind: str = "str",
    secret: bool = False,
    enabled_style: bool = False,
) -> str:
    if enabled_style:
        return "已启用" if bool(val) else "已禁用"
    if kind == "bool":
        return "是" if bool(val) else "否"
    if secret:
        return _mask_secret(str(val or ""))
    if kind == "proxy":
        return str(val) if val else "直连无代理"
    if kind == "nullable_str":
        return str(val) if val else "无（禁用）"
    if kind == "bytes":
        if isinstance(val, int) and val >= 1024 * 1024 and val % (1024 * 1024) == 0:
            return f"{val} ({val // (1024 * 1024)}MB)"
        return str(val)
    if kind == "str":
        return str(val) if val != "" else "(空)"
    return str(val)


def _format_field_state(
    raw: Optional[dict],
    key: str,
    default_val: Any,
    *,
    kind: str = "str",
    secret: bool = False,
    enabled_style: bool = False,
    hint: str = "",
) -> str:
    """区分已配置与未配置，未配置时明确标出默认值。"""
    is_set = isinstance(raw, dict) and key in raw
    if not is_set:
        def_repr = _format_value_repr(default_val, kind=kind, secret=False, enabled_style=enabled_style)
        text = f"未配置（默认: {def_repr}）"
    else:
        text = _format_value_repr(raw[key], kind=kind, secret=secret, enabled_style=enabled_style)
    if hint:
        return f"{text} {hint}"
    return text


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str
    group: str = ""
    choices: Tuple[str, ...] = ()
    hint: str = ""
    min_val: Optional[int] = None
    max_val: Optional[int] = None


WORKER_SPECS: Tuple[FieldSpec, ...] = (
    FieldSpec("base", "base (Worker HTTPS 域名)", "str", "Worker 中继"),
    FieldSpec("ws", "ws (WebSocket 走 Worker)", "bool", "Worker 中继"),
    FieldSpec("http", "http (普通 HTTP 走 Worker)", "bool", "Worker 中继"),
    FieldSpec("login", "login (登录流程走 Worker)", "bool", "Worker 中继"),
    FieldSpec("login_origin", "login_origin (登录 Origin 模式)", "choice", "Worker 中继", choices=("real", "worker"), hint="[real/worker]"),
    FieldSpec("ws_permessage_deflate", "ws_permessage_deflate (WS 压缩)", "bool", "Worker 中继"),
)

GATEWAY_FEED_SPECS: Tuple[FieldSpec, ...] = (
    FieldSpec("enabled", "enabled (启用网关动态同步)", "bool", "动态同步"),
    FieldSpec("targets", "targets (目标平台与模式映射)", "feed_targets", "动态同步", hint="如 onebot=auto,wechat=chat"),
    FieldSpec("poll_interval_s", "poll_interval_s (轮询间隔秒数)", "pos_int", "动态同步"),
    FieldSpec("max_age_s", "max_age_s (动态最大年龄秒数)", "pos_int", "动态同步"),
    FieldSpec("scan_limit", "scan_limit (单次扫描上限)", "pos_int", "动态同步", min_val=1, max_val=100, hint="[1-100]"),
    FieldSpec("max_per_scan", "max_per_scan (单次最多发布条数)", "pos_int", "动态同步"),
    FieldSpec("min_publish_interval_s", "min_publish_interval_s (最小发布间隔秒)", "pos_int", "动态同步"),
    FieldSpec("publish_window_s", "publish_window_s (限流窗口秒数)", "pos_int", "动态同步"),
    FieldSpec("max_per_window", "max_per_window (窗口最多发布条数)", "pos_int", "动态同步"),
)

CHAT_EXTRA_SPECS: Tuple[FieldSpec, ...] = (
    FieldSpec("seed_main_session", "seed_main_session (自动绑定主会话)", "bool", "权限与会话"),
    FieldSpec("unbound_push_to_admin", "unbound_push_to_admin (未绑定推送转管理员)", "bool", "权限与会话"),
    FieldSpec("group_sender_info", "group_sender_info (群消息附带发送者)", "bool", "权限与会话"),
)

CONVERSATION_ADVANCED_SPECS: Tuple[FieldSpec, ...] = (
    FieldSpec("credentials_in_place", "credentials_in_place (就地填写凭证)", "bool", "交互与补发"),
    FieldSpec("approvals_in_place", "approvals_in_place (就地处理审批)", "bool", "交互与补发"),
    FieldSpec("reaction_quote", "reaction_quote (表情反应引用原消息)", "bool", "交互与补发"),
    FieldSpec("catch_up_enabled", "catch_up_enabled (启动补发离线消息)", "bool", "交互与补发"),
    FieldSpec("catch_up_window_s", "catch_up_window_s (离线补发回溯秒数)", "pos_int", "交互与补发"),
    FieldSpec("catch_up_max_messages", "catch_up_max_messages (离线补发最大条数)", "pos_int", "交互与补发"),
    FieldSpec("session_hot_window_s", "session_hot_window_s (活跃会话高频窗口秒)", "pos_int", "会话与重试"),
    FieldSpec("session_sweep_s", "session_sweep_s (全量会话巡检周期秒)", "pos_int", "会话与重试"),
    FieldSpec("recheck_sleep_s", "recheck_sleep_s (重检等待秒数)", "pos_int", "会话与重试"),
    FieldSpec("reconnect_max_retries", "reconnect_max_retries (断线最大重试次数)", "nonneg_int", "会话与重试"),
    FieldSpec("in_flight_ttl_s", "in_flight_ttl_s (在途消息超时秒数)", "pos_int", "会话与重试"),
    FieldSpec("processing_ttl_s", "processing_ttl_s (处理中状态超时秒数)", "pos_int", "会话与重试"),
    FieldSpec("chunk_chars", "chunk_chars (单段文字字符上限)", "pos_int", "分块与媒体限制"),
    FieldSpec("max_chunks", "max_chunks (单条回复最大分段数)", "pos_int", "分块与媒体限制"),
    FieldSpec("max_image_bytes", "max_image_bytes (图片大小上限字节)", "bytes", "分块与媒体限制"),
    FieldSpec("max_file_bytes", "max_file_bytes (文件大小上限字节)", "bytes", "分块与媒体限制"),
    FieldSpec("max_video_bytes", "max_video_bytes (视频下载上限字节)", "bytes", "分块与媒体限制"),
    FieldSpec("max_video_upload_bytes", "max_video_upload_bytes (视频上传上限字节)", "bytes", "分块与媒体限制"),
    FieldSpec("file_ref_threshold_bytes", "file_ref_threshold_bytes (大文件引用阈值字节)", "bytes", "分块与媒体限制"),
    FieldSpec("gateway_dedup_s", "gateway_dedup_s (消息去重窗口秒数)", "pos_int", "分块与媒体限制"),
    FieldSpec("download_timeout_s", "download_timeout_s (附件下载超时秒数)", "pos_int", "分块与媒体限制"),
    FieldSpec("delivered_file", "delivered_file (已投递记录文件路径)", "str", "状态文件与调试"),
    FieldSpec("session_map_file", "session_map_file (会话映射文件路径)", "str", "状态文件与调试"),
    FieldSpec("push_state_file", "push_state_file (推送水位文件路径)", "str", "状态文件与调试"),
    FieldSpec("routes_file", "routes_file (路由/游标文件路径)", "str", "状态文件与调试"),
    FieldSpec("debug", "debug (启用适配器调试模式)", "bool", "状态文件与调试"),
)

QQ_EXTRA_SPECS: Tuple[FieldSpec, ...] = (
    FieldSpec("proxy_url", "proxy_url (QQ 专用代理地址)", "proxy", "QQ 协议与轮询"),
    FieldSpec("download_proxy", "download_proxy (QQ 媒体下载代理)", "str", "QQ 协议与轮询"),
    FieldSpec("max_active_chunks", "max_active_chunks (主动发送最大分段)", "pos_int", "QQ 协议与轮询"),
    FieldSpec("ws_guid", "ws_guid (QQ WebSocket GUID)", "str", "QQ 协议与轮询"),
    FieldSpec("user_agent", "user_agent (QQ 请求 User-Agent)", "str", "QQ 协议与轮询"),
    FieldSpec("push_timeout_s", "push_timeout_s (推送等待超时秒数)", "pos_int", "QQ 协议与轮询"),
    FieldSpec("push_grace_s", "push_grace_s (推送宽限秒数)", "pos_int", "QQ 协议与轮询"),
    FieldSpec("poll_timeout_s", "poll_timeout_s (轮询总超时秒数)", "pos_int", "QQ 协议与轮询"),
    FieldSpec("poll_interval_s", "poll_interval_s (轮询间隔秒数)", "pos_int", "QQ 协议与轮询"),
    FieldSpec("poll_quiet_polls", "poll_quiet_polls (静默轮询判定次数)", "pos_int", "QQ 协议与轮询"),
)


def _prompt_spec_value(
    spec: FieldSpec,
    current_val: Any,
    default_val: Any,
    *,
    configured: bool = False,
) -> Tuple[bool, Any]:
    """按 FieldSpec 类型引导用户输入并校验，直接回车不修改，返回 (changed, value)。"""
    cur_disp = (
        _format_value_repr(current_val, kind=spec.kind)
        if configured
        else f"未配置，默认: {_format_value_repr(default_val, kind=spec.kind)}"
    )
    if spec.kind == "bool":
        return _prompt_edit_bool(spec.label, cur_disp)
    if spec.kind == "choice":
        return _prompt_edit_choice(spec.label, list(spec.choices), cur_disp)
    if spec.kind == "proxy":
        return _prompt_edit_proxy(spec.label, cur_disp)
    if spec.kind == "nullable_str":
        return _prompt_edit_nullable_str(spec.label, cur_disp)
    if spec.kind in ("int", "pos_int", "nonneg_int", "bytes"):
        min_v = spec.min_val
        if min_v is None:
            if spec.kind in ("pos_int", "bytes"):
                min_v = 1
            elif spec.kind == "nonneg_int":
                min_v = 0
        return _prompt_edit_int(spec.label, cur_disp, min_val=min_v, max_val=spec.max_val)
    if spec.kind == "float":
        raw = _prompt_input(f"{spec.label}（当前: {cur_disp}，直接回车不修改）", default="").strip()
        if not raw:
            return False, None
        try:
            return True, float(raw)
        except ValueError:
            print("❌ 请输入有效数字。")
            return False, None
    if spec.kind == "feed_targets":
        print("格式示例：onebot=auto, wechat=chat, qq=chat（可选平台 qq/onebot/wechat，模式 auto/native/chat；输入 clear 清空；直接回车不修改）")
        raw = _prompt_input(f"{spec.label}（当前: {cur_disp}）", default="").strip()
        if not raw:
            return False, None
        if raw.lower() in ("clear", "none", "{}"):
            return True, {}
        parsed: Dict[str, str] = {}
        for item in re.split(r"[,，\s]+", raw):
            if "=" not in item:
                print("❌ 格式错误，请使用 平台=模式，例如 onebot=auto。")
                return False, None
            k, _, v = item.partition("=")
            parsed[k.strip()] = v.strip()
        return True, parsed
    return _prompt_edit_str(spec.label, cur_disp, allow_clear=True)


def _format_group_whitelist(wl: Optional[dict]) -> str:
    if not isinstance(wl, dict):
        return "未配置（默认: {}）"
    groups = {k: v for k, v in wl.items() if k != "private"}
    if not groups:
        return "未配置（默认: {}）"
    return str(groups)


def _prompt_group_whitelist(wl: dict) -> bool:
    """编辑非 private 的群白名单映射，返回是否有改动。"""
    current = {k: v for k, v in wl.items() if k != "private"}
    print(f"当前群白名单: {current or '{}'}")
    print("输入格式：群ID=* 或 群ID=用户1|用户2，多项用逗号分隔；输入 clear 清空群白名单；回车保持不变。")
    raw = _prompt_input("请输入新群白名单", default="").strip()
    if not raw:
        return False
    for key in list(wl.keys()):
        if key != "private":
            wl.pop(key, None)
    if raw.lower() in ("clear", "none", "{}"):
        return True
    for item in re.split(r"[,，\s]+", raw):
        if "=" in item:
            gid, _, rule = item.partition("=")
            gid = gid.strip()
            rule = rule.strip()
            if not gid:
                continue
            if rule == "*":
                wl[gid] = "*"
            else:
                wl[gid] = [u.strip() for u in rule.split("|") if u.strip()]
        elif item.strip():
            wl[item.strip()] = "*"
    return True


def _build_extra_spec_map(start_index: int, specs: List[FieldSpec]) -> Dict[str, FieldSpec]:
    return {str(start_index + i): spec for i, spec in enumerate(specs)}


def get_registered_config_fields() -> Dict[str, set]:
    """返回 FullConfigEditor 覆盖的全部配置字段名，供校验与测试使用。"""
    conv_keys = {s.key for s in CONVERSATION_ADVANCED_SPECS}
    chat_keys = {
        "enabled", "admin_id", "admin_only", "seed_main_session",
        "unbound_push_to_admin", "allow_all_private", "allow_all_group",
        "group_sender_info", "access_requests", "whitelist", "session_bindings",
    } | conv_keys
    return {
        "MuseConfig": {
            "session_id", "cookies_file", "mint_file", "host", "client_ip",
            "timezone", "proxy_url", "vm_id", "direct", "approval", "worker",
        },
        "ApprovalConfig": {"mode", "notify", "sync_interval_s"},
        "WorkerConfig": {s.key for s in WORKER_SPECS},
        "GatewayConfig": {"login_through_adapter", "background_approval_push", "feed"},
        "GatewayFeedConfig": {s.key for s in GATEWAY_FEED_SPECS},
        "QQConfig": chat_keys | {"app_id", "app_secret"} | {s.key for s in QQ_EXTRA_SPECS},
        "OneBotConfig": chat_keys | {"base_url", "access_token", "ws_url", "group_trigger", "typing_enabled", "qzone_feed"},
        "QzoneFeedConfig": {f.name for f in dataclasses.fields(QzoneFeedConfig)},
        "TelegramConfig": chat_keys | {"bot_token", "proxy_url", "group_trigger", "poll_timeout_s"},
        "WeChatConfig": conv_keys | {
            "enabled", "state_dir", "typing_enabled", "session_bindings",
            "polling_timeout_ms", "http_timeout_s", "channel_version", "route_tag",
        },
        "LoggingConfig": {"level", "console", "file", "trace", "trace_file", "payload_limit", "max_bytes", "backup_count"},
    }


# ----------------------------------------------------------------------
# 渠道连通性即时探针（向导校验与日常一键体检共用）
# ----------------------------------------------------------------------

def _probe_qq(app_id: str, app_secret: str) -> Tuple[bool, str]:
    if not app_id or not app_secret:
        return False, "缺少 AppID 或 AppSecret"
    try:
        from muse_bridge.adapters.qq.client import QQClient

        client = QQClient(
            app_id=app_id,
            app_secret=app_secret,
            user_agent=constants.DEFAULT_USER_AGENT,
        )
        client.get_access_token()
        return True, "QQ 凭证校验通过"
    except Exception as exc:
        return False, f"QQ 校验失败: {safe_error(exc)}"


async def _probe_onebot_ws_async(ws_url: str, access_token: str, timeout_s: float = 5.0) -> Tuple[bool, str]:
    try:
        import websockets
    except ImportError:
        return True, "未安装 websockets，跳过 WS 探测"

    from muse_bridge.adapters.onebot.gateway import connect_url

    url = connect_url(ws_url, access_token)
    ssl_ctx = None
    if url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=timeout_s):
            return True, "WS 握手成功"
    except Exception as exc:
        return False, safe_error(exc)


def _probe_onebot(base_url: str, access_token: str, ws_url: str) -> Tuple[bool, str, dict]:
    if not base_url:
        return False, "缺少 OneBot HTTP Base URL", {}
    info: dict = {}
    http_ok = False
    http_msg = ""
    try:
        from muse_bridge.adapters.onebot.client import OneBotClient

        client = OneBotClient(base_url=base_url, access_token=access_token)
        login_info = client.get_login_info()
        bot_id = str(login_info.get("user_id") or "")
        nickname = str(login_info.get("nickname") or "")
        info = {"bot_id": bot_id, "nickname": nickname}
        http_ok = True
        http_msg = f"账号 {bot_id}" + (f" ({nickname})" if nickname else "")
    except Exception as exc:
        http_msg = safe_error(exc)

    ws_ok = False
    ws_msg = ""
    if ws_url:
        try:
            ws_ok, ws_msg = asyncio.run(_probe_onebot_ws_async(ws_url, access_token))
        except Exception as exc:
            ws_ok, ws_msg = False, safe_error(exc)
    else:
        ws_msg = "未配置 ws_url"

    if http_ok and ws_ok:
        return True, f"HTTP 与 WS 均正常 ({http_msg})", info
    if http_ok and not ws_ok:
        return False, f"HTTP 正常 ({http_msg})，但 WebSocket 连接失败 ({ws_msg})；请确认 NapCat 已开启正向 WS 且端口/Token 一致", info
    if not http_ok and ws_ok:
        return True, f"WS 连接正常（HTTP 探测未返回: {http_msg}）", info
    return False, f"HTTP 失败 ({http_msg})；WS 失败 ({ws_msg})", info


async def _probe_telegram_async(bot_token: str, proxy_url: Optional[str]) -> Tuple[bool, str, dict]:
    from muse_bridge.adapters.telegram.client import TelegramClient

    cfg = TelegramConfig(enabled=True, bot_token=bot_token, proxy_url=proxy_url)
    client = TelegramClient(cfg)
    try:
        bot = await client.call("getMe", timeout=15)
        if not isinstance(bot, dict) or not bot.get("username"):
            return False, "Telegram getMe 未返回有效机器人信息", {}
        bot_id = str(bot.get("id") or "")
        username = str(bot.get("username") or "")
        return True, f"已连接 @{username} (ID: {bot_id})", {"bot_id": bot_id, "username": username}
    except Exception as exc:
        return False, f"Telegram 连接失败（请检查 Bot Token 或代理设置）: {safe_error(exc)}", {}
    finally:
        await client.close()


def _probe_telegram(bot_token: str, proxy_url: Optional[str] = None) -> Tuple[bool, str, dict]:
    if not _validate_telegram_token(bot_token):
        return False, "Bot Token 格式无效（应为 <数字>:<密钥>）", {}
    try:
        return asyncio.run(_probe_telegram_async(bot_token, proxy_url))
    except Exception as exc:
        return False, f"Telegram 校验失败: {safe_error(exc)}", {}


def _wechat_local_status_text(wechat_raw: dict) -> str:
    """Offline-only status summary for WeChat iLink; never contacts WeChat."""
    try:
        from muse_bridge.adapters.wechat.state import WeChatState

        cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": wechat_raw}})
        status = WeChatState(cfg.state_dir).status()
        if status.get("logged_in") or status.get("active") or status.get("has_token"):
            return "本地已登录"
        return "本地未登录"
    except Exception:
        return "本地状态未知"


# ----------------------------------------------------------------------
# 统一发消息认领引擎（支持单渠道与多渠道并行监听）
# ----------------------------------------------------------------------

async def _listen_and_claim_channels(
    channels: List[str],
    channel_configs: Dict[str, dict],
) -> Dict[str, Tuple[str, str, str, Optional[str]]]:
    """启动指定渠道的轻量监听，逐渠道认领管理员身份并返回认领结果。

    返回: {channel: (user_id, chat_type, target_id, msg_id)}
    允许用户按 Ctrl+C 中断剩余等待，已认领渠道会被完整保留。
    """
    claim_targets = [ch for ch in channels if ch in CLAIMABLE_CHANNELS]
    if not claim_targets:
        return {}

    configure_logging(LoggingConfig(level="WARNING", console=True, file=None))

    captured: Dict[str, Tuple[str, str, str, Optional[str]]] = {}
    all_done = asyncio.Event()
    gateways: List[Any] = []
    tasks: List[asyncio.Task] = []
    clients: Dict[str, Any] = {}
    tg_clients: List[Any] = []
    active_channels: List[str] = []

    def _print_progress() -> None:
        print("\n当前认领进度：")
        for ch in active_channels:
            label = CHANNEL_LABELS[ch]
            if ch in captured:
                uid, ctype, tid, _ = captured[ch]
                print(f"  ✅ {label}: 已认领 (用户 {uid} / {ctype}:{tid})")
            else:
                print(f"  ⏳ {label}: 等待向机器人发送任意消息...")

    async def _send_claim_ack(ch: str, user_id: str, chat_type: str, target_id: str, msg_id: Optional[str]) -> None:
        reply_text = (
            f"【Muse Bridge 控制台】\n"
            f"✅ 身份认领成功！（渠道: {ch.upper()}）\n"
            f"已将您（ID: {user_id}）识别为管理员并记录当前对话。"
        )
        loop = asyncio.get_running_loop()
        try:
            if ch == "qq":
                qq_c = clients.get("qq")
                if qq_c:
                    await loop.run_in_executor(
                        None,
                        qq_c.send_message,
                        chat_type,
                        target_id,
                        reply_text,
                        reply_text,
                        None,
                        msg_id,
                        1 if msg_id else None,
                    )
            elif ch == "onebot":
                ob_c = clients.get("onebot")
                if ob_c:
                    await loop.run_in_executor(
                        None,
                        ob_c.send_msg,
                        chat_type,
                        target_id,
                        [{"type": "text", "data": {"text": reply_text}}],
                    )
            elif ch == "telegram":
                tg_c = clients.get("telegram")
                if tg_c:
                    await tg_c.call("sendMessage", {"chat_id": int(target_id), "text": reply_text})
        except Exception as exc:
            logger.debug("发送确认回复遇到异常 (%s): %s", ch, safe_error(exc))

    def _record_claim(ch: str, user_id: str, chat_type: str, target_id: str, msg_id: Optional[str]) -> None:
        if ch in captured:
            return
        captured[ch] = (user_id, chat_type, target_id, msg_id)
        print(f"\n🎉 [{CHANNEL_LABELS[ch]}] 捕获消息！用户: {user_id} ({chat_type}: {target_id})")
        asyncio.create_task(_send_claim_ack(ch, user_id, chat_type, target_id, msg_id))
        _print_progress()
        if all(c in captured for c in active_channels):
            all_done.set()

    # 1. QQ 网关
    if "qq" in claim_targets:
        try:
            from muse_bridge.adapters.qq.client import QQClient
            from muse_bridge.adapters.qq.gateway import QQGateway

            qq_cfg = channel_configs.get("qq", {})
            qq_client = QQClient(
                app_id=qq_cfg.get("app_id", ""),
                app_secret=qq_cfg.get("app_secret", ""),
                user_agent=constants.DEFAULT_USER_AGENT,
            )
            clients["qq"] = qq_client
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, qq_client.get_access_token)

            async def _on_qq_event(event_type: str, d: dict):
                if "qq" in captured:
                    return
                if event_type in (
                    "C2C_MESSAGE_CREATE",
                    "GROUP_AT_MESSAGE_CREATE",
                    "GROUP_MESSAGE_CREATE",
                    "AT_MESSAGE_CREATE",
                ):
                    author = d.get("author") or {}
                    user_openid = author.get("user_openid") or author.get("member_openid") or author.get("id") or ""
                    group_openid = d.get("group_openid") or ""
                    msg_id = d.get("id")
                    if user_openid:
                        chat_type = "group" if group_openid else "private"
                        target_id = group_openid if group_openid else user_openid
                        _record_claim("qq", str(user_openid), chat_type, str(target_id), msg_id)

            qq_gw = QQGateway(
                client=qq_client,
                on_event=_on_qq_event,
                user_agent=constants.DEFAULT_USER_AGENT,
            )
            gateways.append(qq_gw)
            active_channels.append("qq")
            tasks.append(asyncio.create_task(qq_gw.start()))
        except Exception as exc:
            print(f"⚠️  [QQ] 启动监听失败，已跳过实时认领（稍后可手动输入 ID）: {safe_error(exc)}")

    # 2. OneBot 网关
    if "onebot" in claim_targets:
        try:
            from muse_bridge.adapters.onebot.client import OneBotClient
            from muse_bridge.adapters.onebot.gateway import OneBotGateway

            ob_cfg = channel_configs.get("onebot", {})
            ob_client = OneBotClient(
                base_url=ob_cfg.get("base_url", ""),
                access_token=ob_cfg.get("access_token", ""),
            )
            clients["onebot"] = ob_client
            loop = asyncio.get_running_loop()
            try:
                info = await loop.run_in_executor(None, ob_client.get_login_info)
                bot_id = str(info.get("user_id") or "")
            except Exception:
                bot_id = ""

            async def _on_ob_message(event: dict):
                if "onebot" in captured:
                    return
                if event.get("post_type") == "message":
                    msg_type = event.get("message_type") or "private"
                    user_id = str(event.get("user_id") or "")
                    if bot_id and user_id == bot_id:
                        return
                    target_id = str(event.get("group_id") if msg_type == "group" else user_id)
                    msg_id = str(event.get("message_id") or "")
                    if user_id:
                        _record_claim("onebot", user_id, msg_type, target_id, msg_id)

            ob_gw = OneBotGateway(
                ws_url=ob_cfg.get("ws_url", ""),
                access_token=ob_cfg.get("access_token", ""),
                on_message=_on_ob_message,
            )
            gateways.append(ob_gw)
            active_channels.append("onebot")
            tasks.append(asyncio.create_task(ob_gw.start()))
        except Exception as exc:
            print(f"⚠️  [OneBot] 启动监听失败，已跳过实时认领（稍后可手动输入 ID）: {safe_error(exc)}")

    # 3. Telegram 轻量轮询
    if "telegram" in claim_targets:
        try:
            from muse_bridge.adapters.telegram.client import TelegramClient

            tg_raw = channel_configs.get("telegram", {})
            tg_cfg = TelegramConfig(
                enabled=True,
                bot_token=tg_raw.get("bot_token", ""),
                proxy_url=tg_raw.get("proxy_url") or None,
            )
            tg_client = TelegramClient(tg_cfg)
            tg_clients.append(tg_client)
            clients["telegram"] = tg_client

            async def _poll_telegram():
                offset: Optional[int] = None
                # 先丢弃启动前的历史未消费更新，只捕获用户现在发的新消息
                try:
                    old_updates = await tg_client.updates(None, poll_timeout=0)
                    if old_updates:
                        offset = old_updates[-1]["update_id"] + 1
                except Exception:
                    pass

                while "telegram" not in captured:
                    try:
                        rows = await tg_client.updates(offset, poll_timeout=10)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(2)
                        continue
                    for row in rows:
                        offset = row["update_id"] + 1
                        msg = row.get("message")
                        if not isinstance(msg, dict):
                            continue
                        sender = msg.get("from") or {}
                        chat = msg.get("chat") or {}
                        if sender.get("is_bot"):
                            continue
                        user_id = str(sender.get("id") or "")
                        chat_id = str(chat.get("id") or "")
                        raw_chat_type = str(chat.get("type") or "")
                        chat_type = "private" if raw_chat_type == "private" else "group"
                        msg_id = str(msg.get("message_id") or "")
                        if user_id and chat_id:
                            _record_claim("telegram", user_id, chat_type, chat_id, msg_id)
                            return

            active_channels.append("telegram")
            tasks.append(asyncio.create_task(_poll_telegram()))
        except Exception as exc:
            print(f"⚠️  [Telegram] 启动监听失败，已跳过实时认领（稍后可手动输入 ID）: {safe_error(exc)}")

    if not active_channels:
        print("⚠️  没有可用于实时监听的渠道，将进入手动认领/跳过流程。")
        return captured

    print("\n" + "*" * 60)
    print("📡 实时认领监听已启动！请在下列已启用的聊天机器人中发送任意消息（私聊或群聊均可）：")
    _print_progress()
    print("💡 提示：如某渠道暂时不方便发消息，可按 Ctrl+C 停止等待，已认领的渠道会自动保留。")
    print("*" * 60)

    try:
        await all_done.wait()
        # 给确认回执一小段发出时间
        await asyncio.sleep(0.3)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[!] 已结束实时等待，将继续处理未认领的渠道。")
    finally:
        for gw in gateways:
            try:
                await gw.stop()
            except Exception:
                pass
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for tg_c in tg_clients:
            try:
                await tg_c.close()
            except Exception:
                pass

    return captured


# ----------------------------------------------------------------------
# 全量配置交互修改器（保留所有历史编号兼容性 + 补齐 Telegram）
# ----------------------------------------------------------------------

class FullConfigEditor:
    """提供按大类层级交互式修改 config.json 全量设置。"""

    def __init__(self, config_data: dict, file_path: str = "config.json", *, auto_save: bool = False):
        self.data = config_data
        self.file_path = file_path
        self.auto_save = auto_save
        self._dirty = False

        # 确保关键根节点存在
        self.data.setdefault("muse", {})
        self.data.setdefault("adapters", {})
        self.data.setdefault("gateway", {})
        self.data.setdefault("logging", {})
        self.data.setdefault("overrides", {})

    @property
    def _modified(self) -> bool:
        return self._dirty

    @_modified.setter
    def _modified(self, value: bool) -> None:
        self._dirty = bool(value)
        if self._dirty and self.auto_save:
            self.save(realtime=True)

    def save(self, *, realtime: bool = False) -> None:
        try:
            _save_json_file(self.file_path, self.data)
            if realtime:
                print(f"💾 已实时保存至: {self.file_path}")
            else:
                print(f"✅ 配置文件已成功保存至: {self.file_path}")
            self._dirty = False
        except Exception as e:
            print(f"❌ 保存配置文件失败: {safe_error(e)}")

    def run_menu(self) -> None:
        while True:
            print("\n" + "=" * 64)
            print("          ⚙️  Muse IM Bridge 全量配置分类设置")
            print("=" * 64)
            print("请选择要修改的配置分类：")
            print("  [1]  🧠 Muse 核心配置   (会话ID / Cookie / 域名 / 代理 / 审批 / Worker)")
            print("  [2]  🚪 网关配置        (登录通道 / 后台审批推送 / 网关动态同步)")
            print("  [3]  🐧 QQ 官方机器人   (全量字段：凭据 / 权限 / 协议 / 媒体 / 状态文件)")
            print("  [4]  🤖 OneBot (NapCat) (全量字段：地址 / 触发 / 白名单 / Qzone / 媒体)")
            print("  [5]  📝 日志与监控      (级别 / 控制台 / 文件 / Trace / 截断与轮转)")
            print("  [6]  ⚡ 全局覆盖参数    (分块字符 / 消息去重 / 下载超时 / User-Agent)")
            print("  [7]  📄 预览当前完整 JSON 配置")
            print("  [8]  💾 保存修改到文件")
            print("  [9]  💬 微信 iLink      (全量字段：本地登录 / 私聊设置 / 超时与分块)")
            print("  [10] ✈️  Telegram Bot   (全量字段：Token / 代理 / 触发 / 权限 / 轮询)")
            print("  [0]  保存并返回")
            print("-" * 64)

            choice = _prompt_input("请输入分类编号 (0-10)", default="0")
            if choice == "0":
                if self._modified:
                    self.save()
                break
            elif choice == "1":
                self._edit_muse_category()
            elif choice == "2":
                self._edit_gateway_category()
            elif choice == "3":
                self._edit_qq_category()
            elif choice == "4":
                self._edit_onebot_category()
            elif choice == "5":
                self._edit_logging_category()
            elif choice == "6":
                self._edit_overrides_category()
            elif choice == "7":
                print("\n" + "=" * 64)
                print(json.dumps(self.data, indent=2, ensure_ascii=False))
                print("=" * 64)
            elif choice == "8":
                self.save()
            elif choice == "9":
                self._edit_wechat_category()
            elif choice == "10":
                self._edit_telegram_category()
            else:
                print("❌ 无效的选项，请重新输入。")

    @staticmethod
    def _print_extra_specs(
        extra_map: Dict[str, FieldSpec],
        raw: Optional[dict],
        defaults_dict: dict,
        *,
        label_width: int = 46,
    ) -> None:
        last_group = None
        for idx, spec in extra_map.items():
            if spec.group and spec.group != last_group:
                print(f"  ── {spec.group} ──")
                last_group = spec.group
            val_text = _format_field_state(
                raw,
                spec.key,
                defaults_dict[spec.key],
                kind=spec.kind,
                hint=spec.hint,
            )
            print(_format_menu_row(idx, spec.label, val_text, label_width=label_width))

    def _edit_extra_spec_field(
        self,
        spec: FieldSpec,
        target: dict,
        defaults_dict: dict,
        *,
        validator: Optional[Callable[[dict], None]] = None,
    ) -> None:
        default_val = defaults_dict[spec.key]
        configured = spec.key in target
        current_val = target.get(spec.key, default_val)
        changed, new_val = _prompt_spec_value(spec, current_val, default_val, configured=configured)
        if not changed:
            print("ℹ️  未修改，保持原样。")
            return
        if validator is not None:
            candidate = {**target, spec.key: new_val}
            try:
                validator(candidate)
            except (ValueError, TypeError) as exc:
                print(f"❌ 配置未修改: {safe_error(exc)}")
                return
        target[spec.key] = new_val
        self._modified = True

    @staticmethod
    def _submenu_summary(
        raw: Optional[dict],
        specs: List[FieldSpec],
        *,
        extra_configured: int = 0,
        extra_total: int = 0,
    ) -> str:
        total = len(specs) + extra_total
        custom = extra_configured
        if isinstance(raw, dict):
            custom += sum(1 for s in specs if s.key in raw)
        if custom == 0:
            return f"均使用默认（共 {total} 项）"
        return f"已自定义 {custom}/{total} 项"

    def _run_spec_submenu(
        self,
        title: str,
        specs: List[FieldSpec],
        get_raw: Callable[[], dict],
        get_mut: Callable[[], dict],
        defaults_dict: dict,
        *,
        validator: Optional[Callable[[dict], None]] = None,
        extra_items: Optional[List[Tuple[str, Callable[[], str], Callable[[], None], Callable[[], bool]]]] = None,
        wechat_sparse: bool = False,
    ) -> None:
        """通用二级字段分组子菜单：精简屏显 + 未配置默认值 + 直接回车不修改 + [r] 重置。"""
        extras = extra_items or []
        spec_map = _build_extra_spec_map(len(extras) + 1, specs)
        max_idx = len(extras) + len(specs)

        while True:
            raw = get_raw()
            print(f"\n--- {title}（直接回车不修改原值）---")
            for i, (label, val_fn, _edit_fn, _reset_fn) in enumerate(extras, 1):
                print(_format_menu_row(str(i), label, val_fn()))
            self._print_extra_specs(spec_map, raw, defaults_dict)
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input(f"请选择要修改的项编号 (0-{max_idx}/r)", default="0").strip().lower()
            if c == "0":
                return
            if c == "r":
                resetters: Dict[str, Callable[[], bool]] = {}
                for i, (_label, _val_fn, _edit_fn, reset_fn) in enumerate(extras, 1):
                    resetters[str(i)] = reset_fn
                for idx, spec in spec_map.items():
                    resetters[idx] = lambda key=spec.key: self._pop_key(get_raw(), key)
                self._reset_field_prompt(resetters)
                continue
            if c.isdigit() and 1 <= int(c) <= len(extras):
                extras[int(c) - 1][2]()
                continue
            if c in spec_map:
                spec = spec_map[c]
                if wechat_sparse:
                    current = get_raw()
                    configured = spec.key in current
                    cur_val = current.get(spec.key, defaults_dict[spec.key])
                    changed, new_val = _prompt_spec_value(spec, cur_val, defaults_dict[spec.key], configured=configured)
                    if not changed:
                        print("ℹ️  未修改，保持原样。")
                        continue
                    candidate = {**current, spec.key: new_val}
                    try:
                        cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": candidate}})
                    except (ValueError, TypeError) as exc:
                        print(f"微信配置未修改: {safe_error(exc)}")
                        continue
                    normalized = asdict(cfg)
                    updated = {name: normalized[name] for name in candidate if name in normalized}
                    if updated != current:
                        self.data.setdefault("adapters", {})["wechat"] = updated
                        self._modified = True
                else:
                    self._edit_extra_spec_field(spec, get_mut(), defaults_dict, validator=validator)
                continue
            print("❌ 无效选项。")

    def _reset_field_prompt(self, resetters: Dict[str, Callable[[], bool]]) -> None:
        code = _prompt_input("请输入要重置为未配置（恢复默认）的项编号（留空取消）", default="").strip()
        if not code:
            return
        fn = resetters.get(code)
        if fn is None:
            print("❌ 该编号不支持重置或不存在。")
            return
        if fn():
            self._modified = True
            print(f"✅ 已将项 [{code}] 重置为未配置（使用默认值）。")
        else:
            print(f"ℹ️  项 [{code}] 当前本就未配置。")

    @staticmethod
    def _pop_key(container: Optional[dict], key: str) -> bool:
        if isinstance(container, dict) and key in container:
            container.pop(key, None)
            return True
        return False

    # ---------------- 1. Muse 核心分类 ----------------
    def _edit_muse_category(self) -> None:
        m_def = asdict(MuseConfig())
        appr_def = asdict(ApprovalConfig())
        while True:
            m = self.data.get("muse") if isinstance(self.data.get("muse"), dict) else {}
            appr = m.get("approval") if isinstance(m.get("approval"), dict) else {}
            worker_raw = m.get("worker") if isinstance(m.get("worker"), dict) else {}
            worker_summary = self._submenu_summary(worker_raw, list(WORKER_SPECS))

            print("\n--- [1] 🧠 Muse 核心配置（直接回车不修改原值）---")
            print(_format_menu_row("1", "session_id (主会话 ID)", _format_field_state(m, "session_id", m_def["session_id"])))
            print(_format_menu_row("2", "cookies_file (Cookie 文件)", _format_field_state(m, "cookies_file", m_def["cookies_file"])))
            print(_format_menu_row("3", "mint_file (Token 凭证文件)", _format_field_state(m, "mint_file", m_def["mint_file"])))
            print(_format_menu_row("4", "host (Muse 域名)", _format_field_state(m, "host", m_def["host"])))
            print(_format_menu_row("5", "client_ip (公网出口 IP)", _format_field_state(m, "client_ip", m_def["client_ip"], hint="[留空自动检测]")))
            print(_format_menu_row("6", "timezone (时区)", _format_field_state(m, "timezone", m_def["timezone"])))
            print(_format_menu_row("7", "proxy_url (Muse 专用代理)", _format_field_state(m, "proxy_url", m_def["proxy_url"], kind="proxy")))
            print("  ── 审批与传输 ──")
            print(_format_menu_row("8", "approval.mode (审批决策模式)", _format_field_state(appr, "mode", appr_def["mode"], hint="[manual/allow_all/deny_all]")))
            print(_format_menu_row("9", "approval.notify (审批时通知)", _format_field_state(appr, "notify", appr_def["notify"], kind="bool")))
            print(_format_menu_row("10", "vm_id (指定 VM ID)", _format_field_state(m, "vm_id", m_def["vm_id"], hint="[留空自动派生]")))
            print(_format_menu_row("11", "direct (直连忽略系统代理)", _format_field_state(m, "direct", m_def["direct"], kind="bool")))
            print(_format_menu_row("12", "approval.sync_interval_s (审批同步秒)", _format_field_state(appr, "sync_interval_s", appr_def["sync_interval_s"], kind="pos_int")))
            print(_format_menu_row("13", "▸ worker (Cloudflare Worker 中继)", worker_summary))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-13/r)", default="0").strip().lower()
            if c == "0":
                break
            elif c == "r":
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(self.data.get("muse"), "session_id"),
                    "2": lambda: self._pop_key(self.data.get("muse"), "cookies_file"),
                    "3": lambda: self._pop_key(self.data.get("muse"), "mint_file"),
                    "4": lambda: self._pop_key(self.data.get("muse"), "host"),
                    "5": lambda: self._pop_key(self.data.get("muse"), "client_ip"),
                    "6": lambda: self._pop_key(self.data.get("muse"), "timezone"),
                    "7": lambda: self._pop_key(self.data.get("muse"), "proxy_url"),
                    "8": lambda: self._pop_key((self.data.get("muse") or {}).get("approval"), "mode"),
                    "9": lambda: self._pop_key((self.data.get("muse") or {}).get("approval"), "notify"),
                    "10": lambda: self._pop_key(self.data.get("muse"), "vm_id"),
                    "11": lambda: self._pop_key(self.data.get("muse"), "direct"),
                    "12": lambda: self._pop_key((self.data.get("muse") or {}).get("approval"), "sync_interval_s"),
                    "13": lambda: self._pop_key(self.data.get("muse"), "worker"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_str("主会话 Session ID", _format_field_state(m, "session_id", m_def["session_id"]), allow_clear=True)
                if ok:
                    self.data.setdefault("muse", {})["session_id"] = val
                    self._modified = True
            elif c == "2":
                ok, val = _prompt_edit_str("Cookie 文件保存路径", _format_field_state(m, "cookies_file", m_def["cookies_file"]))
                if ok:
                    self.data.setdefault("muse", {})["cookies_file"] = val
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_str("Token 凭据文件保存路径", _format_field_state(m, "mint_file", m_def["mint_file"]))
                if ok:
                    self.data.setdefault("muse", {})["mint_file"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_str("Muse 服务端域名", _format_field_state(m, "host", m_def["host"]))
                if ok:
                    self.data.setdefault("muse", {})["host"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_str("公网出口 IP", _format_field_state(m, "client_ip", m_def["client_ip"]), allow_clear=True)
                if ok:
                    self.data.setdefault("muse", {})["client_ip"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_str("时区", _format_field_state(m, "timezone", m_def["timezone"]))
                if ok:
                    self.data.setdefault("muse", {})["timezone"] = val
                    self._modified = True
            elif c == "7":
                ok, val = _prompt_edit_proxy("Muse 专用代理地址", _format_field_state(m, "proxy_url", m_def["proxy_url"], kind="proxy"))
                if ok:
                    self.data.setdefault("muse", {})["proxy_url"] = val
                    self._modified = True
            elif c == "8":
                ok, val = _prompt_edit_choice("审批模式", ["manual", "allow_all", "deny_all"], _format_field_state(appr, "mode", appr_def["mode"]))
                if ok:
                    self.data.setdefault("muse", {}).setdefault("approval", {})["mode"] = val
                    self._modified = True
            elif c == "9":
                ok, val = _prompt_edit_bool("审批发生时是否向聊天发送通知卡片", _format_field_state(appr, "notify", appr_def["notify"], kind="bool"))
                if ok:
                    self.data.setdefault("muse", {}).setdefault("approval", {})["notify"] = val
                    self._modified = True
            elif c == "10":
                ok, val = _prompt_edit_str("VM ID", _format_field_state(m, "vm_id", m_def["vm_id"]), allow_clear=True)
                if ok:
                    self.data.setdefault("muse", {})["vm_id"] = val
                    self._modified = True
            elif c == "11":
                ok, val = _prompt_edit_bool("是否直连 Muse（忽略系统环境代理）", _format_field_state(m, "direct", m_def["direct"], kind="bool"))
                if ok:
                    self.data.setdefault("muse", {})["direct"] = val
                    self._modified = True
            elif c == "12":
                ok, val = _prompt_edit_int("审批队列后台同步间隔（秒）", _format_field_state(appr, "sync_interval_s", appr_def["sync_interval_s"]), min_val=1)
                if ok:
                    self.data.setdefault("muse", {}).setdefault("approval", {})["sync_interval_s"] = val
                    self._modified = True
            elif c == "13":
                self._edit_worker_config()

    def _edit_worker_config(self) -> None:
        w_def = asdict(WorkerConfig())
        self._run_spec_submenu(
            "Muse Cloudflare Worker 中继配置",
            list(WORKER_SPECS),
            get_raw=lambda: (self.data.get("muse") or {}).get("worker") if isinstance((self.data.get("muse") or {}).get("worker"), dict) else {},
            get_mut=lambda: self.data.setdefault("muse", {}).setdefault("worker", {}),
            defaults_dict=w_def,
            validator=lambda cand: WorkerConfig(**cand),
        )

    # ---------------- 2. Gateway 分类 ----------------
    def _edit_gateway_category(self) -> None:
        gw_def = GatewayConfig()
        while True:
            gw = self.data.get("gateway") if isinstance(self.data.get("gateway"), dict) else {}
            feed_raw = gw.get("feed") if isinstance(gw.get("feed"), dict) else {}
            feed_summary = self._submenu_summary(feed_raw, list(GATEWAY_FEED_SPECS))
            print("\n--- [2] 🚪 网关配置（直接回车不修改原值）---")
            print(_format_menu_row("1", "login_through_adapter (登录通知适配器)", _format_field_state(gw, "login_through_adapter", gw_def.login_through_adapter, hint="[qq/onebot/telegram/wechat/clear]")))
            print(_format_menu_row("2", "background_approval_push (后台审批推送)", _format_field_state(gw, "background_approval_push", gw_def.background_approval_push, hint="[qq:<ID> / onebot:<QQ> / 留空自动]")))
            print(_format_menu_row("3", "▸ feed (网关动态同步配置)", feed_summary))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请输入选项 (0-3/r)", default="0").strip().lower()
            if c == "0":
                break
            elif c == "r":
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(self.data.get("gateway"), "login_through_adapter"),
                    "2": lambda: self._pop_key(self.data.get("gateway"), "background_approval_push"),
                    "3": lambda: self._pop_key(self.data.get("gateway"), "feed"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_choice(
                    "请选择登录通知适配器",
                    ["qq", "onebot", "telegram", "wechat", ""],
                    _format_field_state(gw, "login_through_adapter", gw_def.login_through_adapter),
                )
                if ok:
                    self.data.setdefault("gateway", {})["login_through_adapter"] = val
                    self._modified = True
            elif c == "2":
                value = _prompt_input("后台审批推送目标（留空恢复最近活跃管理员私聊）")
                try:
                    value = GatewayConfig(background_approval_push=value).background_approval_push
                except ValueError as exc:
                    print(f"❌ {exc}")
                    continue
                self.data.setdefault("gateway", {})["background_approval_push"] = value
                self._modified = True
            elif c == "3":
                self._edit_gateway_feed()

    def _edit_gateway_feed(self) -> None:
        f_def = asdict(GatewayFeedConfig())
        self._run_spec_submenu(
            "网关 Muse 动态同步配置 (gateway.feed)",
            list(GATEWAY_FEED_SPECS),
            get_raw=lambda: (self.data.get("gateway") or {}).get("feed") if isinstance((self.data.get("gateway") or {}).get("feed"), dict) else {},
            get_mut=lambda: self.data.setdefault("gateway", {}).setdefault("feed", {}),
            defaults_dict=f_def,
            validator=lambda cand: GatewayFeedConfig(**cand),
        )

    # ---------------- 3. QQ 适配器分类 ----------------
    def _edit_qq_category(self) -> None:
        defaults = asdict(QQConfig())
        acc_def = asdict(AccessRequestsConfig())
        qq_proto_specs = list(QQ_EXTRA_SPECS)
        qq_session_specs = [
            s for s in (CHAT_EXTRA_SPECS + CONVERSATION_ADVANCED_SPECS)
            if s.group in ("权限与会话", "交互与补发", "会话与重试")
            and s.key not in {"unbound_push_to_admin", "credentials_in_place", "catch_up_enabled", "group_sender_info"}
        ]
        qq_media_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "分块与媒体限制"]
        qq_state_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "状态文件与调试"]

        def _get_qq() -> dict:
            adapters = self.data.get("adapters")
            if isinstance(adapters, dict) and isinstance(adapters.get("qq"), dict):
                return adapters["qq"]
            return {}

        def _mut_qq() -> dict:
            return self.data.setdefault("adapters", {}).setdefault("qq", {})

        while True:
            qq = _get_qq()
            acc = qq.get("access_requests") if isinstance(qq.get("access_requests"), dict) else {}
            wl = qq.get("whitelist") if isinstance(qq.get("whitelist"), dict) else {}

            acc_disp = (
                f"开启: {_format_field_state(acc, 'enabled', acc_def['enabled'], kind='bool')}, "
                f"自动通过: {_format_field_state(acc, 'auto_approve', acc_def['auto_approve'], kind='bool')}"
            )
            wl_extra_custom = int(any(k != "private" for k in wl)) + int("state_file" in acc)

            print("\n--- [3] 🐧 QQ 官方机器人适配器（直接回车不修改原值）---")
            print("  ── 常用凭据与权限 ──")
            print(_format_menu_row("1", "enabled (是否启用)", _format_field_state(qq, "enabled", defaults["enabled"], enabled_style=True)))
            print(_format_menu_row("2", "app_id (Bot AppID)", _format_field_state(qq, "app_id", defaults["app_id"])))
            print(_format_menu_row("3", "app_secret (Bot AppSecret)", _format_field_state(qq, "app_secret", defaults["app_secret"], secret=True)))
            print(_format_menu_row("4", "admin_id (超级管理员 OpenID 列表)", _format_field_state(qq, "admin_id", defaults["admin_id"])))
            print(_format_menu_row("5", "admin_only (管理指令仅限管理员私聊)", _format_field_state(qq, "admin_only", defaults["admin_only"], kind="bool")))
            print(_format_menu_row("6", "unbound_push_to_admin (未绑定消息推管理员)", _format_field_state(qq, "unbound_push_to_admin", defaults["unbound_push_to_admin"], kind="bool")))
            print(_format_menu_row("7", "credentials_in_place (群内填凭证)", _format_field_state(qq, "credentials_in_place", defaults["credentials_in_place"], kind="bool")))
            print(_format_menu_row("8", "allow_all_private (私聊全员免白名单)", _format_field_state(qq, "allow_all_private", defaults["allow_all_private"], kind="bool")))
            print(_format_menu_row("9", "allow_all_group (群聊全员免白名单)", _format_field_state(qq, "allow_all_group", defaults["allow_all_group"], kind="bool")))
            print(_format_menu_row("10", "access_requests (白名单申请功能)", acc_disp))
            print(_format_menu_row("11", "whitelist.private (私聊白名单用户)", _format_field_state(wl, "private", [])))
            print(_format_menu_row("12", "catch_up_enabled (启动历史消息补发)", _format_field_state(qq, "catch_up_enabled", defaults["catch_up_enabled"], kind="bool")))
            print(_format_menu_row("13", "session_bindings (会话固定映射字典)", _format_field_state(qq, "session_bindings", defaults["session_bindings"])))
            print(_format_menu_row("14", "group_sender_info (群消息附带发送者)", _format_field_state(qq, "group_sender_info", defaults["group_sender_info"], kind="bool")))
            print("  ── 高级分组子菜单（含全部未配置默认项）──")
            print(_format_menu_row("15", "▸ 群白名单与申请状态文件", self._submenu_summary(qq, [], extra_configured=wl_extra_custom, extra_total=2)))
            print(_format_menu_row("16", "▸ QQ 协议与轮询参数", self._submenu_summary(qq, qq_proto_specs)))
            print(_format_menu_row("17", "▸ 权限、交互与会话重试参数", self._submenu_summary(qq, qq_session_specs)))
            print(_format_menu_row("18", "▸ 分块与媒体大小限制", self._submenu_summary(qq, qq_media_specs)))
            print(_format_menu_row("19", "▸ 状态文件路径与调试", self._submenu_summary(qq, qq_state_specs)))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项或子菜单 (0-19/r)", default="0").strip().lower()
            if c == "0":
                break
            if c == "r":
                qq_ref = _get_qq()
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(qq_ref, "enabled"),
                    "2": lambda: self._pop_key(qq_ref, "app_id"),
                    "3": lambda: self._pop_key(qq_ref, "app_secret"),
                    "4": lambda: self._pop_key(qq_ref, "admin_id"),
                    "5": lambda: self._pop_key(qq_ref, "admin_only"),
                    "6": lambda: self._pop_key(qq_ref, "unbound_push_to_admin"),
                    "7": lambda: self._pop_key(qq_ref, "credentials_in_place"),
                    "8": lambda: self._pop_key(qq_ref, "allow_all_private"),
                    "9": lambda: self._pop_key(qq_ref, "allow_all_group"),
                    "10": lambda: self._pop_key(qq_ref, "access_requests"),
                    "11": lambda: self._pop_key(qq_ref.get("whitelist"), "private"),
                    "12": lambda: self._pop_key(qq_ref, "catch_up_enabled"),
                    "13": lambda: self._pop_key(qq_ref, "session_bindings"),
                    "14": lambda: self._pop_key(qq_ref, "group_sender_info"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_bool("是否启用 QQ 适配器", _format_field_state(qq, "enabled", defaults["enabled"], enabled_style=True))
                if ok:
                    _mut_qq()["enabled"] = val
                    self._modified = True
            elif c == "2":
                ok, val = _prompt_edit_str("QQ Bot AppID", _format_field_state(qq, "app_id", defaults["app_id"]), allow_clear=True)
                if ok:
                    _mut_qq()["app_id"] = val
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_str("QQ Bot AppSecret", _format_field_state(qq, "app_secret", defaults["app_secret"], secret=True), allow_clear=True)
                if ok:
                    _mut_qq()["app_secret"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_str_list("管理员 OpenID 列表", qq.get("admin_id", []))
                if ok:
                    _mut_qq()["admin_id"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_bool("管理指令(/switch, /new, /delete等)是否仅限管理员私聊执行", _format_field_state(qq, "admin_only", defaults["admin_only"], kind="bool"))
                if ok:
                    _mut_qq()["admin_only"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_bool("未绑定聊天的主动推送是否转交首位管理员私聊", _format_field_state(qq, "unbound_push_to_admin", defaults["unbound_push_to_admin"], kind="bool"))
                if ok:
                    _mut_qq()["unbound_push_to_admin"] = val
                    self._modified = True
            elif c == "7":
                ok, val = _prompt_edit_bool("是否允许在群聊中直接填写凭证卡片", _format_field_state(qq, "credentials_in_place", defaults["credentials_in_place"], kind="bool"))
                if ok:
                    _mut_qq()["credentials_in_place"] = val
                    self._modified = True
            elif c == "8":
                ok, val = _prompt_edit_bool("是否开放所有私聊（无需白名单）", _format_field_state(qq, "allow_all_private", defaults["allow_all_private"], kind="bool"))
                if ok:
                    _mut_qq()["allow_all_private"] = val
                    self._modified = True
            elif c == "9":
                ok, val = _prompt_edit_bool("是否开放所有群聊（无需白名单）", _format_field_state(qq, "allow_all_group", defaults["allow_all_group"], kind="bool"))
                if ok:
                    _mut_qq()["allow_all_group"] = val
                    self._modified = True
            elif c == "10":
                ok1, v1 = _prompt_edit_bool("是否开放 /申请 白名单指令", _format_field_state(acc, "enabled", acc_def["enabled"], kind="bool"))
                ok2, v2 = _prompt_edit_bool("普通用户发送 /申请 时是否自动通过", _format_field_state(acc, "auto_approve", acc_def["auto_approve"], kind="bool"))
                if ok1 or ok2:
                    acc_mut = _mut_qq().setdefault("access_requests", {})
                    if ok1:
                        acc_mut["enabled"] = v1
                    if ok2:
                        acc_mut["auto_approve"] = v2
                    self._modified = True
            elif c == "11":
                ok, val = _prompt_edit_str_list("私聊白名单 OpenID 列表", wl.get("private", []))
                if ok:
                    _mut_qq().setdefault("whitelist", {})["private"] = val
                    self._modified = True
            elif c == "12":
                ok, val = _prompt_edit_bool("服务启动时是否追赶补发离线期间的消息", _format_field_state(qq, "catch_up_enabled", defaults["catch_up_enabled"], kind="bool"))
                if ok:
                    _mut_qq()["catch_up_enabled"] = val
                    self._modified = True
            elif c == "13":
                print("会话预绑定示例格式：private:OPENID_1=SESSION_ID_1, group:GID_1=SESSION_ID_2")
                print(f"当前预绑定: {qq.get('session_bindings', {})}")
                raw_b = _prompt_input("输入映射(输入 clear 清空，直接回车不修改)", default="")
                parsed = _parse_session_bindings_input(raw_b)
                if parsed is not None:
                    _mut_qq()["session_bindings"] = parsed
                    self._modified = True
            elif c == "14":
                ok, val = _prompt_edit_bool("群消息是否附带发送者信息", _format_field_state(qq, "group_sender_info", defaults["group_sender_info"], kind="bool"))
                if ok:
                    _mut_qq()["group_sender_info"] = val
                    self._modified = True
            elif c == "15":
                def _edit_qq_wl():
                    if _prompt_group_whitelist(_mut_qq().setdefault("whitelist", {})):
                        self._modified = True
                def _reset_qq_wl() -> bool:
                    wl_cur = _get_qq().get("whitelist")
                    if not isinstance(wl_cur, dict):
                        return False
                    removed = [k for k in wl_cur if k != "private"]
                    for k in removed:
                        wl_cur.pop(k, None)
                    return bool(removed)
                def _edit_qq_acc_file():
                    acc_cur = _get_qq().get("access_requests") if isinstance(_get_qq().get("access_requests"), dict) else {}
                    ok, val = _prompt_edit_str("白名单申请状态文件路径", _format_field_state(acc_cur, "state_file", acc_def["state_file"]))
                    if ok:
                        _mut_qq().setdefault("access_requests", {})["state_file"] = val
                        self._modified = True
                self._run_spec_submenu(
                    "QQ 群白名单与申请状态文件",
                    [],
                    get_raw=_get_qq,
                    get_mut=_mut_qq,
                    defaults_dict=defaults,
                    extra_items=[
                        ("whitelist.<group> (群白名单映射)", lambda: _format_group_whitelist(_get_qq().get("whitelist")), _edit_qq_wl, _reset_qq_wl),
                        ("access_requests.state_file (申请状态文件)", lambda: _format_field_state(_get_qq().get("access_requests"), "state_file", acc_def["state_file"]), _edit_qq_acc_file, lambda: self._pop_key(_get_qq().get("access_requests"), "state_file")),
                    ],
                )
            elif c == "16":
                self._run_spec_submenu("QQ 协议与轮询参数", qq_proto_specs, _get_qq, _mut_qq, defaults)
            elif c == "17":
                self._run_spec_submenu("QQ 权限、交互与会话重试参数", qq_session_specs, _get_qq, _mut_qq, defaults)
            elif c == "18":
                self._run_spec_submenu("QQ 分块与媒体大小限制", qq_media_specs, _get_qq, _mut_qq, defaults)
            elif c == "19":
                self._run_spec_submenu("QQ 状态文件路径与调试", qq_state_specs, _get_qq, _mut_qq, defaults)
            else:
                print("❌ 无效选项。")

    # ---------------- 4. OneBot 适配器分类 ----------------
    def _edit_onebot_category(self) -> None:
        defaults = asdict(OneBotConfig())
        acc_def = defaults["access_requests"]
        ob_perm_specs = [
            s for s in (CHAT_EXTRA_SPECS + CONVERSATION_ADVANCED_SPECS)
            if s.group in ("权限与会话", "交互与补发") and s.key != "group_sender_info"
        ]
        ob_session_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "会话与重试"]
        ob_media_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "分块与媒体限制"]
        ob_state_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "状态文件与调试"]

        def _get_ob() -> dict:
            adapters = self.data.get("adapters")
            if isinstance(adapters, dict) and isinstance(adapters.get("onebot"), dict):
                return adapters["onebot"]
            return {}

        def _mut_ob() -> dict:
            return self.data.setdefault("adapters", {}).setdefault("onebot", {})

        while True:
            ob = _get_ob()
            acc = ob.get("access_requests") if isinstance(ob.get("access_requests"), dict) else {}
            wl = ob.get("whitelist") if isinstance(ob.get("whitelist"), dict) else {}
            qz = ob.get("qzone_feed") if isinstance(ob.get("qzone_feed"), dict) else {}

            acc_disp = (
                f"开启: {_format_field_state(acc, 'enabled', acc_def['enabled'], kind='bool')}, "
                f"自动通过: {_format_field_state(acc, 'auto_approve', acc_def['auto_approve'], kind='bool')}"
            )
            qz_disp = _format_field_state(qz, "enabled", QzoneFeedConfig().enabled, enabled_style=True) if qz else "未配置（默认: 已禁用）"

            print("\n--- [4] 🤖 OneBot (NapCat) 适配器（直接回车不修改原值）---")
            print("  ── 常用连接与权限 ──")
            print(_format_menu_row("1", "enabled (是否启用)", _format_field_state(ob, "enabled", defaults["enabled"], enabled_style=True)))
            print(_format_menu_row("2", "base_url (NapCat HTTP 接口地址)", _format_field_state(ob, "base_url", defaults["base_url"])))
            print(_format_menu_row("3", "access_token (访问 Token 鉴权)", _format_field_state(ob, "access_token", defaults["access_token"], secret=True)))
            print(_format_menu_row("4", "ws_url (正向 WebSocket 地址)", _format_field_state(ob, "ws_url", defaults["ws_url"])))
            print(_format_menu_row("5", "group_trigger (群消息触发策略)", _format_field_state(ob, "group_trigger", defaults["group_trigger"], hint="[all/at/prefix]")))
            print(_format_menu_row("6", "admin_id (超级管理员 QQ 号列表)", _format_field_state(ob, "admin_id", defaults["admin_id"])))
            print(_format_menu_row("7", "admin_only (管理指令仅限管理员私聊)", _format_field_state(ob, "admin_only", defaults["admin_only"], kind="bool")))
            print(_format_menu_row("8", "allow_all_private (私聊全员免白名单)", _format_field_state(ob, "allow_all_private", defaults["allow_all_private"], kind="bool")))
            print(_format_menu_row("9", "allow_all_group (群聊全员免白名单)", _format_field_state(ob, "allow_all_group", defaults["allow_all_group"], kind="bool")))
            print(_format_menu_row("10", "access_requests (白名单申请功能)", acc_disp))
            print(_format_menu_row("11", "whitelist.private (私聊白名单 QQ)", _format_field_state(wl, "private", [])))
            print(_format_menu_row("12", "whitelist.groups (群白名单 群号)", _format_field_state(wl, "groups", [])))
            print(_format_menu_row("13", "session_bindings (会话预绑定映射)", _format_field_state(ob, "session_bindings", defaults["session_bindings"])))
            print(_format_menu_row("14", "group_sender_info (群消息附带发送者)", _format_field_state(ob, "group_sender_info", defaults["group_sender_info"], kind="bool")))
            print(_format_menu_row("15", "qzone_feed (个人动态同步到 QQ空间)", qz_disp))
            print(_format_menu_row("16", "typing_enabled (私聊输入状态提示)", _format_field_state(ob, "typing_enabled", defaults["typing_enabled"], kind="bool")))
            print("  ── 高级分组子菜单（含全部未配置默认项）──")
            print(_format_menu_row("17", "▸ 权限、交互与离线补发参数", self._submenu_summary(ob, ob_perm_specs, extra_configured=int("state_file" in acc), extra_total=1)))
            print(_format_menu_row("18", "▸ 会话与重试超时参数", self._submenu_summary(ob, ob_session_specs)))
            print(_format_menu_row("19", "▸ 分块与媒体大小限制", self._submenu_summary(ob, ob_media_specs)))
            print(_format_menu_row("20", "▸ 状态文件路径与调试", self._submenu_summary(ob, ob_state_specs)))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项或子菜单 (0-20/r)", default="0").strip().lower()
            if c == "0":
                break
            if c == "r":
                ob_ref = _get_ob()
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(ob_ref, "enabled"),
                    "2": lambda: self._pop_key(ob_ref, "base_url"),
                    "3": lambda: self._pop_key(ob_ref, "access_token"),
                    "4": lambda: self._pop_key(ob_ref, "ws_url"),
                    "5": lambda: self._pop_key(ob_ref, "group_trigger"),
                    "6": lambda: self._pop_key(ob_ref, "admin_id"),
                    "7": lambda: self._pop_key(ob_ref, "admin_only"),
                    "8": lambda: self._pop_key(ob_ref, "allow_all_private"),
                    "9": lambda: self._pop_key(ob_ref, "allow_all_group"),
                    "10": lambda: self._pop_key(ob_ref, "access_requests"),
                    "11": lambda: self._pop_key(ob_ref.get("whitelist"), "private"),
                    "12": lambda: self._pop_key(ob_ref.get("whitelist"), "groups"),
                    "13": lambda: self._pop_key(ob_ref, "session_bindings"),
                    "14": lambda: self._pop_key(ob_ref, "group_sender_info"),
                    "15": lambda: self._pop_key(ob_ref, "qzone_feed"),
                    "16": lambda: self._pop_key(ob_ref, "typing_enabled"),
                })
                continue

            if c == "16":
                ok, val = _prompt_edit_bool("私聊回复期间是否展示输入状态 (NapCat set_input_status)", _format_field_state(ob, "typing_enabled", defaults["typing_enabled"], kind="bool"))
                if ok:
                    _mut_ob()["typing_enabled"] = val
                    self._modified = True
            elif c == "15":
                self._edit_qzone_feed(_mut_ob())
            elif c == "14":
                ok, val = _prompt_edit_bool("群消息是否附带发送者信息", _format_field_state(ob, "group_sender_info", defaults["group_sender_info"], kind="bool"))
                if ok:
                    _mut_ob()["group_sender_info"] = val
                    self._modified = True
            elif c == "1":
                ok, val = _prompt_edit_bool("是否启用 OneBot 适配器", _format_field_state(ob, "enabled", defaults["enabled"], enabled_style=True))
                if ok:
                    _mut_ob()["enabled"] = val
                    self._modified = True
            elif c == "2":
                ok, url = _prompt_edit_str("NapCat HTTP Base URL (如 http://127.0.0.1:3000)", _format_field_state(ob, "base_url", defaults["base_url"]), allow_clear=True)
                if ok:
                    url = _normalize_http_url(url or "")
                    ob_mut = _mut_ob()
                    ob_mut["base_url"] = url
                    if not ob_mut.get("ws_url") and url:
                        ob_mut["ws_url"] = _default_ws_url(url)
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_str("Access Token", _format_field_state(ob, "access_token", defaults["access_token"], secret=True), allow_clear=True)
                if ok:
                    _mut_ob()["access_token"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_str("正向 WebSocket URL", _format_field_state(ob, "ws_url", defaults["ws_url"]), allow_clear=True)
                if ok:
                    _mut_ob()["ws_url"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_choice("群聊响应规则", ["all", "at", "prefix"], _format_field_state(ob, "group_trigger", defaults["group_trigger"]))
                if ok:
                    _mut_ob()["group_trigger"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_str_list("管理员 QQ 号列表", ob.get("admin_id", []))
                if ok:
                    _mut_ob()["admin_id"] = val
                    self._modified = True
            elif c == "7":
                ok, val = _prompt_edit_bool("管理指令是否仅限管理员在私聊执行", _format_field_state(ob, "admin_only", defaults["admin_only"], kind="bool"))
                if ok:
                    _mut_ob()["admin_only"] = val
                    self._modified = True
            elif c == "8":
                ok, val = _prompt_edit_bool("是否开放所有私聊免白名单", _format_field_state(ob, "allow_all_private", defaults["allow_all_private"], kind="bool"))
                if ok:
                    _mut_ob()["allow_all_private"] = val
                    self._modified = True
            elif c == "9":
                ok, val = _prompt_edit_bool("是否开放所有群聊免白名单", _format_field_state(ob, "allow_all_group", defaults["allow_all_group"], kind="bool"))
                if ok:
                    _mut_ob()["allow_all_group"] = val
                    self._modified = True
            elif c == "10":
                ok1, v1 = _prompt_edit_bool("是否开放 /申请 白名单指令", _format_field_state(acc, "enabled", acc_def["enabled"], kind="bool"))
                ok2, v2 = _prompt_edit_bool("发送 /申请 时是否自动通过", _format_field_state(acc, "auto_approve", acc_def["auto_approve"], kind="bool"))
                if ok1 or ok2:
                    acc_mut = _mut_ob().setdefault("access_requests", {})
                    if ok1:
                        acc_mut["enabled"] = v1
                    if ok2:
                        acc_mut["auto_approve"] = v2
                    self._modified = True
            elif c == "11":
                ok, val = _prompt_edit_str_list("私聊白名单 QQ 号列表", wl.get("private", []))
                if ok:
                    _mut_ob().setdefault("whitelist", {})["private"] = val
                    self._modified = True
            elif c == "12":
                ok, raw_groups = _prompt_edit_str_list("白名单开放群号列表 (数字群号)", wl.get("groups", []))
                if ok and raw_groups is not None:
                    int_groups = []
                    for g in raw_groups:
                        try:
                            int_groups.append(int(g))
                        except ValueError:
                            int_groups.append(g)
                    _mut_ob().setdefault("whitelist", {})["groups"] = int_groups
                    self._modified = True
            elif c == "13":
                print(f"当前预绑定: {ob.get('session_bindings', {})}")
                raw_b = _prompt_input("输入映射(如 private:12345=SESS_1, 输入 clear 清空，直接回车不修改)", default="")
                parsed = _parse_session_bindings_input(raw_b)
                if parsed is not None:
                    _mut_ob()["session_bindings"] = parsed
                    self._modified = True
            elif c == "17":
                def _edit_ob_acc_file():
                    acc_cur = _get_ob().get("access_requests") if isinstance(_get_ob().get("access_requests"), dict) else {}
                    ok, val = _prompt_edit_str("白名单申请状态文件路径", _format_field_state(acc_cur, "state_file", acc_def["state_file"]))
                    if ok:
                        _mut_ob().setdefault("access_requests", {})["state_file"] = val
                        self._modified = True
                self._run_spec_submenu(
                    "OneBot 权限、交互与离线补发参数",
                    ob_perm_specs,
                    get_raw=_get_ob,
                    get_mut=_mut_ob,
                    defaults_dict=defaults,
                    extra_items=[
                        ("access_requests.state_file (申请状态文件)", lambda: _format_field_state(_get_ob().get("access_requests"), "state_file", acc_def["state_file"]), _edit_ob_acc_file, lambda: self._pop_key(_get_ob().get("access_requests"), "state_file")),
                    ],
                )
            elif c == "18":
                self._run_spec_submenu("OneBot 会话与重试超时参数", ob_session_specs, _get_ob, _mut_ob, defaults)
            elif c == "19":
                self._run_spec_submenu("OneBot 分块与媒体大小限制", ob_media_specs, _get_ob, _mut_ob, defaults)
            elif c == "20":
                self._run_spec_submenu("OneBot 状态文件路径与调试", ob_state_specs, _get_ob, _mut_ob, defaults)
            else:
                print("❌ 无效选项。")

    # ---------------- 9. 微信 iLink 分类 ----------------
    def _edit_wechat_category(self) -> None:
        """Edit only the selected setting; login never walks through config fields."""
        global _LAST_INPUT_EMPTY
        defaults = asdict(WeChatConfig())
        fields = {
            "1": ("enabled", "启用微信适配器"),
            "4": ("state_dir", "本地账号目录"),
            "5": ("typing_enabled", "输入状态提示"),
            "6": ("catch_up_enabled", "启动时补发历史消息"),
            "7": ("chunk_chars", "每段文字字符数（最多 4000）"),
            "8": ("max_chunks", "最多文字分段数"),
            "9": ("polling_timeout_ms", "长轮询超时（毫秒）"),
            "10": ("http_timeout_s", "HTTP 超时（秒）"),
            "11": ("session_bindings", "会话预绑定"),
            "12": ("channel_version", "协议客户端版本"),
            "13": ("route_tag", "协议路由标签"),
        }
        wx_interact_specs = [
            s for s in CONVERSATION_ADVANCED_SPECS
            if s.group == "交互与补发" and s.key != "catch_up_enabled"
        ]
        wx_runtime_specs = [
            s for s in CONVERSATION_ADVANCED_SPECS
            if s.group in ("会话与重试", "分块与媒体限制") and s.key not in {"chunk_chars", "max_chunks"}
        ]
        wx_state_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "状态文件与调试"]

        def _get_wx() -> dict:
            adapters = self.data.get("adapters")
            if isinstance(adapters, dict) and isinstance(adapters.get("wechat"), dict):
                return adapters["wechat"]
            return {}

        while True:
            current = _get_wx()
            settings = {**defaults, **current}
            print("\n--- [9] 💬 微信 iLink（个人私信，直接回车不修改原值）---")
            print("  ── 基础设置与登录 ──")
            print(_format_menu_row("1", "enabled (启用微信适配器)", _format_field_state(current, "enabled", defaults["enabled"], enabled_style=True)))
            print(_format_menu_row("2", "扫码登录 / 重新登录", "连接微信，凭据保存为本地明文 JSON"))
            print(_format_menu_row("3", "查看本地登录状态", "仅读本地状态，不连接微信"))
            for choice, (key, title) in fields.items():
                if choice != "1":
                    kind = "bool" if type(defaults[key]) is bool else "str"
                    print(_format_menu_row(choice, f"{key} ({title})", _format_field_state(current, key, defaults[key], kind=kind)))
            print("  ── 高级分组子菜单（含全部未配置默认项）──")
            print(_format_menu_row("14", "▸ 交互与离线补发参数", self._submenu_summary(current, wx_interact_specs)))
            print(_format_menu_row("15", "▸ 会话、重试与媒体大小限制", self._submenu_summary(current, wx_runtime_specs)))
            print(_format_menu_row("16", "▸ 状态文件路径与调试", self._submenu_summary(current, wx_state_specs)))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")
            choice = _prompt_input("请选择要修改的项或子菜单 (0-16/r)", default="0").strip().lower()
            if choice == "0":
                return
            if choice == "r":
                wx_ref = _get_wx()
                resetters: Dict[str, Callable[[], bool]] = {
                    idx: (lambda key=k: self._pop_key(wx_ref, key))
                    for idx, (k, _t) in fields.items()
                }
                self._reset_field_prompt(resetters)
                continue
            if choice in ("2", "3"):
                from muse_bridge.adapters.wechat.cli import run_login, run_status
                try:
                    config = ConfigLoader._wechat_from_raw({"adapters": {"wechat": current}})
                    (run_login if choice == "2" else run_status)(config)
                except Exception:
                    print("微信操作失败；请检查配置或稍后重试，未输出认证详情。")
                continue
            if choice == "14":
                self._run_spec_submenu("微信交互与离线补发参数", wx_interact_specs, _get_wx, _get_wx, defaults, wechat_sparse=True)
                continue
            if choice == "15":
                self._run_spec_submenu("微信会话、重试与媒体大小限制", wx_runtime_specs, _get_wx, _get_wx, defaults, wechat_sparse=True)
                continue
            if choice == "16":
                self._run_spec_submenu("微信状态文件路径与调试", wx_state_specs, _get_wx, _get_wx, defaults, wechat_sparse=True)
                continue
            if choice not in fields:
                print("请选择菜单中的编号。")
                continue

            key, title = fields[choice]
            value = settings[key]
            if type(defaults[key]) is bool:
                _LAST_INPUT_EMPTY = False
                value = _prompt_bool(title, value)
                if _LAST_INPUT_EMPTY:
                    print("ℹ️  未修改，保持原样。")
                    continue
            elif key == "http_timeout_s":
                raw = _prompt_input(f"{title}（直接回车不修改）", default="").strip()
                if not raw:
                    print("ℹ️  未修改，保持原样。")
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    print("请输入有效的超时秒数。")
                    continue
            elif key in ("chunk_chars", "max_chunks", "polling_timeout_ms"):
                cancelled = False
                while True:
                    _LAST_INPUT_EMPTY = False
                    value = _prompt_int(title, value, defaults[key])
                    if _LAST_INPUT_EMPTY:
                        cancelled = True
                        break
                    if value > 0 and (key != "chunk_chars" or value <= 4000):
                        break
                    print("请输入正整数，文字每段不能超过 4000 字符。")
                if cancelled:
                    print("ℹ️  未修改，保持原样。")
                    continue
            elif key == "session_bindings":
                raw = _prompt_input("输入 private:<用户ID>=<Muse会话ID>（clear 清空，直接回车不修改）")
                if not raw:
                    continue
                if raw.lower() == "clear":
                    value = {}
                else:
                    pairs = re.split(r"[,，\s]+", raw)
                    if any("=" not in item for item in pairs):
                        print("格式错误；未修改会话预绑定。")
                        continue
                    value = dict(item.split("=", 1) for item in pairs)
            else:
                ok, new_str = _prompt_edit_str(title, _format_field_state(current, key, defaults[key]), allow_clear=(key == "route_tag"))
                if not ok:
                    print("ℹ️  未修改，保持原样。")
                    continue
                value = new_str

            candidate = {**current, key: value}
            try:
                config = ConfigLoader._wechat_from_raw({"adapters": {"wechat": candidate}})
            except (ValueError, TypeError) as exc:
                print(f"微信配置未修改: {safe_error(exc)}")
                continue
            # Preserve sparse configuration and unrelated settings. Removed legacy
            # policy fields are not written back when a setting is changed.
            normalized = asdict(config)
            updated = {name: normalized[name] for name in candidate if name in normalized}
            if updated != current:
                self.data["adapters"]["wechat"] = updated
                self._modified = True

    # ---------------- 10. Telegram 适配器分类 ----------------
    def _edit_telegram_category(self) -> None:
        defaults = asdict(TelegramConfig())
        acc_def = defaults["access_requests"]
        tg_perm_specs = [
            s for s in (CHAT_EXTRA_SPECS + CONVERSATION_ADVANCED_SPECS)
            if s.group in ("权限与会话", "交互与补发") and s.key != "reaction_quote"
        ]
        tg_session_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "会话与重试"]
        tg_media_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "分块与媒体限制"]
        tg_state_specs = [s for s in CONVERSATION_ADVANCED_SPECS if s.group == "状态文件与调试"]

        def _get_tg() -> dict:
            adapters = self.data.get("adapters")
            if isinstance(adapters, dict) and isinstance(adapters.get("telegram"), dict):
                return adapters["telegram"]
            return {}

        def _mut_tg() -> dict:
            return self.data.setdefault("adapters", {}).setdefault("telegram", {})

        while True:
            tg = _get_tg()
            acc = tg.get("access_requests") if isinstance(tg.get("access_requests"), dict) else {}
            wl = tg.get("whitelist") if isinstance(tg.get("whitelist"), dict) else {}

            acc_disp = (
                f"开启: {_format_field_state(acc, 'enabled', acc_def['enabled'], kind='bool')}, "
                f"自动通过: {_format_field_state(acc, 'auto_approve', acc_def['auto_approve'], kind='bool')}"
            )
            perm_extra_custom = int(any(k != "private" for k in wl)) + int("state_file" in acc)

            print("\n--- [10] ✈️  Telegram Bot 适配器（直接回车不修改原值）---")
            print("  ── 常用凭据与权限 ──")
            print(_format_menu_row("1", "enabled (是否启用)", _format_field_state(tg, "enabled", defaults["enabled"], enabled_style=True)))
            print(_format_menu_row("2", "bot_token (Bot Token)", _format_field_state(tg, "bot_token", defaults["bot_token"], secret=True)))
            print(_format_menu_row("3", "proxy_url (HTTP/HTTPS 代理)", _format_field_state(tg, "proxy_url", defaults["proxy_url"], kind="proxy")))
            print(_format_menu_row("4", "group_trigger (群消息触发策略)", _format_field_state(tg, "group_trigger", defaults["group_trigger"], hint="[at/all/prefix]")))
            print(_format_menu_row("5", "admin_id (管理员 User ID 列表)", _format_field_state(tg, "admin_id", defaults["admin_id"])))
            print(_format_menu_row("6", "admin_only (管理指令仅限管理员私聊)", _format_field_state(tg, "admin_only", defaults["admin_only"], kind="bool")))
            print(_format_menu_row("7", "allow_all_private (私聊全员免白名单)", _format_field_state(tg, "allow_all_private", defaults["allow_all_private"], kind="bool")))
            print(_format_menu_row("8", "allow_all_group (群聊全员免白名单)", _format_field_state(tg, "allow_all_group", defaults["allow_all_group"], kind="bool")))
            print(_format_menu_row("9", "access_requests (白名单申请功能)", acc_disp))
            print(_format_menu_row("10", "whitelist.private (私聊白名单)", _format_field_state(wl, "private", [])))
            print(_format_menu_row("11", "session_bindings (会话预绑定)", _format_field_state(tg, "session_bindings", defaults["session_bindings"])))
            print(_format_menu_row("12", "poll_timeout_s (长轮询超时秒数)", _format_field_state(tg, "poll_timeout_s", defaults["poll_timeout_s"], hint="[1-50]")))
            print(_format_menu_row("13", "reaction_quote (表情反应引用)", _format_field_state(tg, "reaction_quote", defaults["reaction_quote"], kind="bool")))
            print("  ── 高级分组子菜单（含全部未配置默认项）──")
            print(_format_menu_row("14", "▸ 群白名单、权限与交互参数", self._submenu_summary(tg, tg_perm_specs, extra_configured=perm_extra_custom, extra_total=2)))
            print(_format_menu_row("15", "▸ 会话与重试超时参数", self._submenu_summary(tg, tg_session_specs)))
            print(_format_menu_row("16", "▸ 分块与媒体大小限制", self._submenu_summary(tg, tg_media_specs)))
            print(_format_menu_row("17", "▸ 状态文件路径与调试", self._submenu_summary(tg, tg_state_specs)))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项或子菜单 (0-17/r)", default="0").strip().lower()
            if c == "0":
                break
            if c == "r":
                tg_ref = _get_tg()
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(tg_ref, "enabled"),
                    "2": lambda: self._pop_key(tg_ref, "bot_token"),
                    "3": lambda: self._pop_key(tg_ref, "proxy_url"),
                    "4": lambda: self._pop_key(tg_ref, "group_trigger"),
                    "5": lambda: self._pop_key(tg_ref, "admin_id"),
                    "6": lambda: self._pop_key(tg_ref, "admin_only"),
                    "7": lambda: self._pop_key(tg_ref, "allow_all_private"),
                    "8": lambda: self._pop_key(tg_ref, "allow_all_group"),
                    "9": lambda: self._pop_key(tg_ref, "access_requests"),
                    "10": lambda: self._pop_key(tg_ref.get("whitelist"), "private"),
                    "11": lambda: self._pop_key(tg_ref, "session_bindings"),
                    "12": lambda: self._pop_key(tg_ref, "poll_timeout_s"),
                    "13": lambda: self._pop_key(tg_ref, "reaction_quote"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_bool("是否启用 Telegram 适配器", _format_field_state(tg, "enabled", defaults["enabled"], enabled_style=True))
                if ok:
                    _mut_tg()["enabled"] = val
                    self._modified = True
            elif c == "2":
                ok, tok = _prompt_edit_str("Telegram Bot Token", _format_field_state(tg, "bot_token", defaults["bot_token"], secret=True), allow_clear=True)
                if ok:
                    tok = (tok or "").strip()
                    if tok and not _validate_telegram_token(tok):
                        print("❌ Bot Token 格式不正确（应为 <数字>:<密钥>），未修改。")
                        continue
                    _mut_tg()["bot_token"] = tok
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_proxy("HTTP(S) 代理地址", _format_field_state(tg, "proxy_url", defaults["proxy_url"], kind="proxy"))
                if ok:
                    _mut_tg()["proxy_url"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_choice("群聊响应规则", ["at", "all", "prefix"], _format_field_state(tg, "group_trigger", defaults["group_trigger"]))
                if ok:
                    _mut_tg()["group_trigger"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_str_list("管理员 Telegram User ID 列表", tg.get("admin_id", []))
                if ok:
                    _mut_tg()["admin_id"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_bool("管理指令是否仅限管理员在私聊执行", _format_field_state(tg, "admin_only", defaults["admin_only"], kind="bool"))
                if ok:
                    _mut_tg()["admin_only"] = val
                    self._modified = True
            elif c == "7":
                ok, val = _prompt_edit_bool("是否开放所有私聊免白名单", _format_field_state(tg, "allow_all_private", defaults["allow_all_private"], kind="bool"))
                if ok:
                    _mut_tg()["allow_all_private"] = val
                    self._modified = True
            elif c == "8":
                ok, val = _prompt_edit_bool("是否开放所有群聊免白名单", _format_field_state(tg, "allow_all_group", defaults["allow_all_group"], kind="bool"))
                if ok:
                    _mut_tg()["allow_all_group"] = val
                    self._modified = True
            elif c == "9":
                ok1, v1 = _prompt_edit_bool("是否开放 /申请 白名单指令", _format_field_state(acc, "enabled", acc_def["enabled"], kind="bool"))
                ok2, v2 = _prompt_edit_bool("发送 /申请 时是否自动通过", _format_field_state(acc, "auto_approve", acc_def["auto_approve"], kind="bool"))
                if ok1 or ok2:
                    acc_mut = _mut_tg().setdefault("access_requests", {})
                    if ok1:
                        acc_mut["enabled"] = v1
                    if ok2:
                        acc_mut["auto_approve"] = v2
                    self._modified = True
            elif c == "10":
                ok, val = _prompt_edit_str_list("私聊白名单 User ID 列表", wl.get("private", []))
                if ok:
                    _mut_tg().setdefault("whitelist", {})["private"] = val
                    self._modified = True
            elif c == "11":
                print(f"当前预绑定: {tg.get('session_bindings', {})}")
                raw_b = _prompt_input("输入映射(如 private:123456=SESS_1, 输入 clear 清空，直接回车不修改)", default="")
                parsed = _parse_session_bindings_input(raw_b)
                if parsed is not None:
                    _mut_tg()["session_bindings"] = parsed
                    self._modified = True
            elif c == "12":
                ok, val = _prompt_edit_int("长轮询超时秒数 (1-50)", _format_field_state(tg, "poll_timeout_s", defaults["poll_timeout_s"]), min_val=1, max_val=50)
                if ok:
                    _mut_tg()["poll_timeout_s"] = val
                    self._modified = True
            elif c == "13":
                ok, val = _prompt_edit_bool("收到表情反应时是否引用原消息", _format_field_state(tg, "reaction_quote", defaults["reaction_quote"], kind="bool"))
                if ok:
                    _mut_tg()["reaction_quote"] = val
                    self._modified = True
            elif c == "14":
                def _edit_tg_wl():
                    if _prompt_group_whitelist(_mut_tg().setdefault("whitelist", {})):
                        self._modified = True
                def _reset_tg_wl() -> bool:
                    wl_cur = _get_tg().get("whitelist")
                    if not isinstance(wl_cur, dict):
                        return False
                    removed = [k for k in wl_cur if k != "private"]
                    for k in removed:
                        wl_cur.pop(k, None)
                    return bool(removed)
                def _edit_tg_acc_file():
                    acc_cur = _get_tg().get("access_requests") if isinstance(_get_tg().get("access_requests"), dict) else {}
                    ok, val = _prompt_edit_str("白名单申请状态文件路径", _format_field_state(acc_cur, "state_file", acc_def["state_file"]))
                    if ok:
                        _mut_tg().setdefault("access_requests", {})["state_file"] = val
                        self._modified = True
                self._run_spec_submenu(
                    "Telegram 群白名单、权限与交互参数",
                    tg_perm_specs,
                    get_raw=_get_tg,
                    get_mut=_mut_tg,
                    defaults_dict=defaults,
                    extra_items=[
                        ("whitelist.<group> (群白名单映射)", lambda: _format_group_whitelist(_get_tg().get("whitelist")), _edit_tg_wl, _reset_tg_wl),
                        ("access_requests.state_file (申请状态文件)", lambda: _format_field_state(_get_tg().get("access_requests"), "state_file", acc_def["state_file"]), _edit_tg_acc_file, lambda: self._pop_key(_get_tg().get("access_requests"), "state_file")),
                    ],
                )
            elif c == "15":
                self._run_spec_submenu("Telegram 会话与重试超时参数", tg_session_specs, _get_tg, _mut_tg, defaults)
            elif c == "16":
                self._run_spec_submenu("Telegram 分块与媒体大小限制", tg_media_specs, _get_tg, _mut_tg, defaults)
            elif c == "17":
                self._run_spec_submenu("Telegram 状态文件路径与调试", tg_state_specs, _get_tg, _mut_tg, defaults)
            else:
                print("❌ 无效选项。")

    def _edit_qzone_feed(self, ob: dict) -> None:
        defaults = asdict(QzoneFeedConfig())
        settings = {**defaults, **ob.get("qzone_feed", {})}
        print("\n--- 个人动态同步到 QQ空间 ---")
        print("启用后，运行网桥时会将个人 Muse feed 动态的文字和图片发布到 QQ空间。默认关闭。")
        print("此处仅修改配置，不会立即发送或发布动态。")
        settings["enabled"] = _prompt_bool("是否启用个人动态发布到 QQ空间", settings["enabled"])
        for key, title in (
            ("poll_interval_s", "轮询间隔（秒）"),
            ("max_age_s", "动态最大年龄（秒）"),
            ("scan_limit", "每次扫描条数上限"),
            ("max_per_scan", "每次扫描最多发布条数"),
            ("min_publish_interval_s", "最小发布间隔（秒）"),
            ("publish_window_s", "发布限流窗口（秒）"),
            ("max_per_window", "每个窗口最多发布条数"),
        ):
            while True:
                value = _prompt_int(title, settings[key], defaults[key])
                upper = 100 if key == "scan_limit" else settings["scan_limit"] if key == "max_per_scan" else None
                if value > 0 and (upper is None or value <= upper):
                    settings[key] = value
                    break
                print(f"请输入 1–{upper} 之间的整数。" if upper else "请输入大于零的整数。")
        settings["ugc_right"] = int(_prompt_choice(
            "QQ空间可见范围：1 所有人可见，4 好友可见，64 仅自己可见",
            ["1", "4", "64"], str(settings["ugc_right"]),
        ))
        settings["state_file"] = _prompt_str("动态同步状态文件路径", settings["state_file"], defaults["state_file"])
        ob["qzone_feed"] = settings
        self._modified = True

    # ---------------- 5. 日志与追踪分类 ----------------
    def _edit_logging_category(self) -> None:
        l_def = asdict(LoggingConfig())
        while True:
            log = self.data.get("logging") if isinstance(self.data.get("logging"), dict) else {}
            print("\n--- [5] 📝 日志与追踪配置（直接回车不修改原值）---")
            print(_format_menu_row("1", "level (日志输出级别)", _format_field_state(log, "level", l_def["level"], hint="[TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL/OFF]")))
            print(_format_menu_row("2", "console (是否输出到控制台)", _format_field_state(log, "console", l_def["console"], kind="bool")))
            print(_format_menu_row("3", "file (日志文件输出路径)", _format_field_state(log, "file", l_def["file"], kind="nullable_str")))
            print(_format_menu_row("4", "trace (是否开启诊断追踪)", _format_field_state(log, "trace", l_def["trace"], kind="bool")))
            print(_format_menu_row("5", "trace_file (追踪日志路径)", _format_field_state(log, "trace_file", l_def["trace_file"], kind="nullable_str")))
            print(_format_menu_row("6", "max_bytes (单日志文件大小)", _format_field_state(log, "max_bytes", l_def["max_bytes"], kind="bytes")))
            print(_format_menu_row("7", "backup_count (备份轮转数)", _format_field_state(log, "backup_count", l_def["backup_count"])))
            print(_format_menu_row("8", "payload_limit (追踪报文截断字节)", _format_field_state(log, "payload_limit", l_def["payload_limit"])))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-8/r)", default="0").strip().lower()
            if c == "0":
                break
            elif c == "r":
                log_ref = self.data.get("logging")
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(log_ref, "level"),
                    "2": lambda: self._pop_key(log_ref, "console"),
                    "3": lambda: self._pop_key(log_ref, "file"),
                    "4": lambda: self._pop_key(log_ref, "trace"),
                    "5": lambda: self._pop_key(log_ref, "trace_file"),
                    "6": lambda: self._pop_key(log_ref, "max_bytes"),
                    "7": lambda: self._pop_key(log_ref, "backup_count"),
                    "8": lambda: self._pop_key(log_ref, "payload_limit"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_choice(
                    "日志等级",
                    ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"],
                    _format_field_state(log, "level", l_def["level"]),
                )
                if ok:
                    self.data.setdefault("logging", {})["level"] = val
                    self._modified = True
            elif c == "2":
                ok, val = _prompt_edit_bool("是否输出控制台日志", _format_field_state(log, "console", l_def["console"], kind="bool"))
                if ok:
                    self.data.setdefault("logging", {})["console"] = val
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_nullable_str("日志文件路径", _format_field_state(log, "file", l_def["file"], kind="nullable_str"))
                if ok:
                    self.data.setdefault("logging", {})["file"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_bool("是否开启结构化诊断追踪 trace.jsonl", _format_field_state(log, "trace", l_def["trace"], kind="bool"))
                if ok:
                    self.data.setdefault("logging", {})["trace"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_nullable_str("追踪日志路径", _format_field_state(log, "trace_file", l_def["trace_file"], kind="nullable_str"))
                if ok:
                    self.data.setdefault("logging", {})["trace_file"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_int("单日志最大字节数", _format_field_state(log, "max_bytes", l_def["max_bytes"], kind="bytes"), min_val=1)
                if ok:
                    self.data.setdefault("logging", {})["max_bytes"] = val
                    self._modified = True
            elif c == "7":
                ok, val = _prompt_edit_int("日志文件备份保留数量", _format_field_state(log, "backup_count", l_def["backup_count"]), min_val=1)
                if ok:
                    self.data.setdefault("logging", {})["backup_count"] = val
                    self._modified = True
            elif c == "8":
                ok, val = _prompt_edit_int("追踪报文截断字节数 (>=0)", _format_field_state(log, "payload_limit", l_def["payload_limit"]), min_val=0)
                if ok:
                    self.data.setdefault("logging", {})["payload_limit"] = val
                    self._modified = True

    # ---------------- 6. 高级性能与覆盖参数 ----------------
    def _edit_overrides_category(self) -> None:
        while True:
            ov = self.data.get("overrides") if isinstance(self.data.get("overrides"), dict) else {}
            print("\n--- [6] ⚡ 高级性能与全局覆盖参数（直接回车不修改原值）---")
            print(_format_menu_row("1", "chunk_chars (长文本单段字符上限)", _format_field_state(ov, "chunk_chars", constants.DEFAULT_CHUNK_CHARS)))
            print(_format_menu_row("2", "max_chunks (单条回复最多拆分段数)", _format_field_state(ov, "max_chunks", constants.DEFAULT_MAX_CHUNKS)))
            print(_format_menu_row("3", "max_active_chunks (主动发送限额)", _format_field_state(ov, "max_active_chunks", constants.DEFAULT_MAX_ACTIVE_CHUNKS)))
            print(_format_menu_row("4", "gateway_dedup_s (网关消息去重窗口秒数)", _format_field_state(ov, "gateway_dedup_s", constants.DEFAULT_GATEWAY_DEDUP_S)))
            print(_format_menu_row("5", "download_timeout_s (文件下载超时秒数)", _format_field_state(ov, "download_timeout_s", constants.DEFAULT_DOWNLOAD_TIMEOUT_S)))
            print(_format_menu_row("6", "user_agent (全局请求 User-Agent)", _format_field_state(ov, "user_agent", constants.DEFAULT_USER_AGENT[:40] + "...")))
            print("  [r]  重置指定字段为未配置（恢复默认）")
            print("  [0]  返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-6/r)", default="0").strip().lower()
            if c == "0":
                break
            elif c == "r":
                ov_ref = self.data.get("overrides")
                self._reset_field_prompt({
                    "1": lambda: self._pop_key(ov_ref, "chunk_chars"),
                    "2": lambda: self._pop_key(ov_ref, "max_chunks"),
                    "3": lambda: self._pop_key(ov_ref, "max_active_chunks"),
                    "4": lambda: self._pop_key(ov_ref, "gateway_dedup_s"),
                    "5": lambda: self._pop_key(ov_ref, "download_timeout_s"),
                    "6": lambda: self._pop_key(ov_ref, "user_agent"),
                })
                continue

            if c == "1":
                ok, val = _prompt_edit_int("长文本单段字符上限", _format_field_state(ov, "chunk_chars", constants.DEFAULT_CHUNK_CHARS), min_val=1)
                if ok:
                    self.data.setdefault("overrides", {})["chunk_chars"] = val
                    self._modified = True
            elif c == "2":
                ok, val = _prompt_edit_int("单条回复最多拆分段数", _format_field_state(ov, "max_chunks", constants.DEFAULT_MAX_CHUNKS), min_val=1)
                if ok:
                    self.data.setdefault("overrides", {})["max_chunks"] = val
                    self._modified = True
            elif c == "3":
                ok, val = _prompt_edit_int("主动消息分段上限", _format_field_state(ov, "max_active_chunks", constants.DEFAULT_MAX_ACTIVE_CHUNKS), min_val=1)
                if ok:
                    self.data.setdefault("overrides", {})["max_active_chunks"] = val
                    self._modified = True
            elif c == "4":
                ok, val = _prompt_edit_int("消息防重复去重窗口时长 (秒)", _format_field_state(ov, "gateway_dedup_s", constants.DEFAULT_GATEWAY_DEDUP_S), min_val=1)
                if ok:
                    self.data.setdefault("overrides", {})["gateway_dedup_s"] = val
                    self._modified = True
            elif c == "5":
                ok, val = _prompt_edit_int("媒体附件下载超时时长 (秒)", _format_field_state(ov, "download_timeout_s", constants.DEFAULT_DOWNLOAD_TIMEOUT_S), min_val=1)
                if ok:
                    self.data.setdefault("overrides", {})["download_timeout_s"] = val
                    self._modified = True
            elif c == "6":
                ok, val = _prompt_edit_str("全局 User-Agent", _format_field_state(ov, "user_agent", constants.DEFAULT_USER_AGENT[:40] + "..."))
                if ok:
                    self.data.setdefault("overrides", {})["user_agent"] = val
                    self._modified = True


# ----------------------------------------------------------------------
# 单渠道引导配置组件（向导与日常渠道管理复用）
# ----------------------------------------------------------------------

def _configure_qq_interactive(existing: Optional[dict] = None) -> Tuple[str, Optional[dict], str]:
    """引导配置 QQ 并即时校验。返回 (status: 'ready'|'unverified'|'cancel', cfg, note)。"""
    prev = existing or {}
    while True:
        print("\n--- 配置 QQ 官方机器人 ---")
        if prev.get("app_id"):
            print(f"当前 AppID: {prev['app_id']}")
        app_id = _prompt_input("请输入 QQ Bot AppID（直接回车取消）", default="").strip()
        if not app_id:
            print("已取消配置 QQ 官方机器人。")
            return "cancel", None, ""

        if prev.get("app_secret"):
            app_secret = _prompt_input("请输入 QQ Bot AppSecret（直接回车保持原值）", default="").strip() or prev["app_secret"]
        else:
            app_secret = _prompt_input("请输入 QQ Bot AppSecret（直接回车取消）", default="").strip()
            if not app_secret:
                print("已取消配置 QQ 官方机器人。")
                return "cancel", None, ""

        print("🔍 正在校验 QQ AppID 与 AppSecret...")
        ok, msg = _probe_qq(app_id, app_secret)
        if ok:
            print(f"✅ {msg}")
            return "ready", {"app_id": app_id, "app_secret": app_secret}, f"AppID: {app_id}"

        print(f"❌ {msg}")
        print("  [1] 重新输入凭据")
        print("  [2] 暂不校验，先保留配置")
        print("  [3] 放弃并返回")
        action = _prompt_input("请选择后续操作 (1/2/3)", default="1").strip()
        if action == "2":
            return "unverified", {"app_id": app_id, "app_secret": app_secret}, f"AppID: {app_id} (未校验)"
        if action == "3":
            return "cancel", None, ""
        prev = {"app_id": app_id, "app_secret": app_secret}


def _configure_onebot_interactive(existing: Optional[dict] = None) -> Tuple[str, Optional[dict], str]:
    """引导配置 OneBot (NapCat) 并即时探测 HTTP + WS。"""
    prev = existing or {}
    while True:
        print("\n--- 配置 OneBot v11 (NapCat / LLOneBot) ---")
        if prev.get("base_url"):
            print(f"当前 HTTP 地址: {prev['base_url']}")
        raw_base = _prompt_input(
            "请输入 OneBot HTTP 地址（如 3000 或 127.0.0.1:3000，直接回车取消）",
            default="",
        ).strip()
        if not raw_base:
            print("已取消配置 OneBot。")
            return "cancel", None, ""
        base_url = _normalize_http_url(raw_base)

        access_token = _prompt_str(
            "请输入 OneBot Access Token（未设置鉴权直接回车留空）",
            prev.get("access_token", ""),
        ).strip()
        default_ws = prev.get("ws_url") or _default_ws_url(base_url)
        ws_url = _prompt_str("请输入 OneBot 正向 WebSocket URL", default_ws).strip()
        if not ws_url:
            ws_url = _default_ws_url(base_url)

        print(f"🔍 正在探测 OneBot HTTP ({base_url}) 与 WebSocket ({ws_url})...")
        ok, msg, info = _probe_onebot(base_url, access_token, ws_url)
        cfg = {
            "base_url": base_url,
            "access_token": access_token,
            "ws_url": ws_url,
        }
        if ok:
            print(f"✅ {msg}")
            bot_tag = f"账号 {info.get('bot_id')}" if info.get("bot_id") else base_url
            return "ready", cfg, bot_tag

        print(f"⚠️  {msg}")
        print("  [1] 重新输入地址或 Token")
        print("  [2] 暂不校验，先保留配置（稍后启动 NapCat 后可用）")
        print("  [3] 放弃并返回")
        action = _prompt_input("请选择后续操作 (1/2/3)", default="1").strip()
        if action == "2":
            return "unverified", cfg, f"{base_url} (未校验)"
        if action == "3":
            return "cancel", None, ""
        prev = cfg


def _configure_telegram_interactive(
    existing: Optional[dict] = None,
    default_proxy: Optional[str] = None,
) -> Tuple[str, Optional[dict], str]:
    """引导配置 Telegram Bot（含代理询问与 getMe 即时校验）。"""
    prev = existing or {}
    while True:
        print("\n--- 配置 Telegram Bot ---")
        if prev.get("bot_token"):
            print(f"当前 Bot Token: {_mask_secret(prev['bot_token'])}")
        token = _prompt_input(
            "请输入 @BotFather 颁发的 Bot Token（直接回车取消）",
            default="",
        ).strip()
        if not token:
            print("已取消配置 Telegram。")
            return "cancel", None, ""
        if not _validate_telegram_token(token):
            print("❌ Token 格式不正确，应类似 123456789:AAHxxx...")
            continue

        suggested_proxy = prev.get("proxy_url") or default_proxy or ""
        while True:
            raw_proxy = _prompt_str(
                "HTTP(S) 代理地址（如 http://127.0.0.1:7890；直连留空，输入 clear 清除代理）",
                suggested_proxy,
            )
            try:
                proxy_url = _validate_proxy_url(raw_proxy)
                break
            except ValueError as exc:
                print(f"❌ {exc}")

        print("🔍 正在调用 Telegram getMe 校验连接...")
        ok, msg, info = _probe_telegram(token, proxy_url)
        cfg = {
            "bot_token": token,
            "proxy_url": proxy_url,
            "group_trigger": prev.get("group_trigger", "at"),
        }
        if ok:
            print(f"✅ {msg}")
            username = info.get("username")
            return "ready", cfg, (f"@{username}" if username else "已连接")

        print(f"❌ {msg}")
        print("  [1] 重新修改 Token 或代理")
        print("  [2] 暂不校验，先保留配置")
        print("  [3] 放弃并返回")
        action = _prompt_input("请选择后续操作 (1/2/3)", default="1").strip()
        if action == "2":
            return "unverified", cfg, "已填写 (未校验)"
        if action == "3":
            return "cancel", None, ""
        prev = cfg


def _configure_wechat_guided(existing: Optional[dict] = None) -> Tuple[str, Optional[dict], str]:
    """微信精简引导子菜单：直接扫码或进入精简设置，不直接展示 13 项底层参数。"""
    current = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    logged_in = False
    while True:
        enabled = bool(current.get("enabled", False))
        local_status = "本次已登录" if logged_in else _wechat_local_status_text(current)
        print("\n--- 配置微信 iLink（个人私信）---")
        print(f"当前状态: {'已启用' if enabled else '未启用'} | 账号: {local_status}")
        print("  [1] 立即扫码登录微信（推荐，扫码后自动启用）")
        print("  [2] 引导式基础设置（输入状态提示 / 离线消息补发）")
        print("  [3] 查看本地登录状态（不连接微信）")
        print("  [4] 仅启用微信，稍后再扫码")
        print("  [5] 进入完整微信高级参数（分块 / 超时 / 路由标签等）")
        print("  [0] 完成并返回")

        choice = _prompt_input("请选择操作 (0-5)", default="1" if not logged_in and not enabled else "0").strip()
        if choice == "0":
            if not current.get("enabled"):
                return "cancel", None, ""
            note = "已扫码登录" if (logged_in or local_status == "本地已登录") else "已启用（待扫码）"
            return "ready", current, note
        elif choice == "1":
            from muse_bridge.adapters.wechat.cli import run_login

            current["enabled"] = True
            try:
                cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": current}})
                run_login(cfg)
                logged_in = True
                print("✅ 微信扫码登录完成并已启用。")
                return "ready", current, "已扫码登录"
            except Exception:
                print("❌ 微信登录失败；请检查网络或稍后重试，未输出认证详情。")
        elif choice == "2":
            print("\n--- 微信基础设置引导 ---")
            print("说明：分块长度、超时和协议版本已使用安全默认值，此处仅需按个人习惯确认 2 项：")
            current["enabled"] = True
            current["typing_enabled"] = _prompt_bool(
                "回复生成期间是否向微信发送「对方正在输入...」提示",
                current.get("typing_enabled", False),
            )
            current["catch_up_enabled"] = _prompt_bool(
                "重启网桥后是否自动补发离线期间漏掉的私信",
                current.get("catch_up_enabled", False),
            )
            print("✅ 微信基础设置已更新（如需调整分块或超时，随时可在高级设置中修改）。")
        elif choice == "3":
            from muse_bridge.adapters.wechat.cli import run_status

            try:
                cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": current}})
                run_status(cfg)
            except Exception:
                print("❌ 无法读取本地微信状态。")
        elif choice == "4":
            current["enabled"] = True
            print("ℹ️  已启用微信适配器，之后可随时在控制台的「账号登录」或运行 python main.py --wechat-login 扫码。")
            return "ready", current, "已启用（待扫码）"
        elif choice == "5":
            editor = FullConfigEditor({"adapters": {"wechat": current}})
            editor._edit_wechat_category()
            current = editor.data["adapters"].get("wechat", current)
        else:
            print("❌ 请输入 0-5 之间的编号。")


# ----------------------------------------------------------------------
# 首次启动 / 重新初始化引导向导
# ----------------------------------------------------------------------

class FirstRunWizard:
    def __init__(
        self,
        output_file: str = "config.json",
        *,
        existing_data: Optional[dict] = None,
        preserve_muse: bool = False,
    ):
        self.output_file = output_file
        self.existing_data: dict = copy.deepcopy(existing_data) if isinstance(existing_data, dict) else {}
        self.preserve_muse = preserve_muse

        self.selected_channels: List[str] = []
        self.channel_configs: Dict[str, dict] = {}
        self.channel_notes: Dict[str, str] = {}
        self.claimed_events: Dict[str, Tuple[str, str, str, Optional[str]]] = {}
        self.manual_admins: Dict[str, str] = {}
        self.group_settings: Dict[str, dict] = {}
        self.first_message_event: Optional[Tuple[str, str, str, str, Optional[str]]] = None
        self.muse_proxy_url: Optional[str] = (
            self.existing_data.get("muse", {}).get("proxy_url") if preserve_muse else None
        )
        self.login_through_adapter: Optional[str] = None
        self.wechat_login_notifications: bool = False
        self.completed: bool = False
        self._captured_event = asyncio.Event()

    def run_interactive(self, *, offer_muse_login: bool = True) -> bool:
        print("\n" + "=" * 60)
        print("     🚀 Muse IM Bridge 引导配置向导")
        print("=" * 60)
        print("接下来只需 3 步即可完成基础配置：")
        print("  1. 在渠道列表中自由选择并配置需要启用的平台（当场测通）")
        print("  2. 为需要管理员权限的平台发一条消息认领身份（可跳过或手填）")
        print("  3. 生成干净配置并衔接 Muse 账号登录\n")

        # 1. 自由渠道配置列表（选哪个进哪个，配完回到列表，最后选下一步）
        if not self._run_channel_setup_loop():
            print("已取消引导向导，未写入任何修改。")
            return False

        # 2. 逐渠道认领管理员（仅 QQ / OneBot / Telegram 需要，微信不进入监听）
        claimable = [ch for ch in self.selected_channels if ch in CLAIMABLE_CHANNELS]
        if claimable:
            print("\n" + "=" * 60)
            print("步骤 2/3：逐渠道认领管理员身份")
            print("=" * 60)
            asyncio.run(self._listen_for_first_message())
            self._handle_unclaimed_and_group_options(claimable)

        # 3. 选择登录通知通道
        self._choose_login_notification_channel()

        # 4. 生成并写入配置文件 + 可读摘要
        self._generate_and_save_config()
        self.completed = True

        # 5. 无缝衔接 Muse 登录（仅当尚未登录时询问）
        if offer_muse_login:
            self._offer_muse_login()

        return True

    def _channel_status_badge(self, ch: str) -> str:
        if ch not in self.selected_channels:
            return "[未配置]"
        note = self.channel_notes.get(ch, "已配置")
        return f"[✅ 已启用: {note}]"

    def _run_channel_setup_loop(self) -> bool:
        """交互式渠道配置列表：用户选哪个进哪个，配完返回列表，最后按 n 进入下一步。"""
        while True:
            print("\n--- 步骤 1/3：渠道配置列表（可多选，配好后输入 n 进入下一步）---")
            for idx, ch in enumerate(CHANNEL_ORDER, 1):
                print(_format_channel_row(str(idx), CHANNEL_LABELS[ch], self._channel_status_badge(ch)))
            proxy_disp = self.muse_proxy_url or "直连无代理"
            print(_format_channel_row("5", "Muse 网络/代理设置", f"[{proxy_disp}]"))
            if self.selected_channels:
                enabled_names = "、".join(CHANNEL_LABELS[c] for c in self.selected_channels)
                print(f"  [n] ➡️  下一步：进入认领与生成配置（已选：{enabled_names}）")
            else:
                print("  [n] ➡️  下一步（请先至少配置 1 个渠道）")
            print("  [0] 取消退出")

            default_choice = "n" if self.selected_channels else "1"
            raw = _prompt_input("请选择编号（支持逗号/空格一次输入多个，如 2,4；完成输入 n）", default=default_choice).strip().lower()
            if raw in ("0", "q", "quit", "exit"):
                return False
            if raw in ("n", "next", "done"):
                if not self.selected_channels:
                    print("⚠️  请至少先配置并启用 1 个聊天渠道。")
                    continue
                return True

            tokens = [t for t in re.split(r"[,，\s]+", raw) if t]
            valid_map = {"1": "qq", "2": "onebot", "3": "telegram", "4": "wechat", "5": "proxy"}
            unknown = [t for t in tokens if t not in valid_map]
            if not tokens or unknown:
                print("❌ 请输入 1-5 选择对应项，输入 n 进入下一步，或输入 0 取消。")
                continue

            for token in tokens:
                target = valid_map[token]
                if target == "proxy":
                    self._configure_muse_proxy()
                else:
                    self._configure_single_channel_in_list(target)

    def _configure_muse_proxy(self) -> None:
        print("\n--- Muse 网络与代理设置 ---")
        print("如果您的服务器访问 muse.ai 需要代理，请在此填写 HTTP(S) 代理；否则直接回车保持直连。")
        while True:
            raw = _prompt_str("Muse 代理地址 (如 http://127.0.0.1:7890，输入 clear 恢复直连)", self.muse_proxy_url or "")
            try:
                self.muse_proxy_url = _validate_proxy_url(raw)
                print(f"✅ Muse 网络设置已更新：{self.muse_proxy_url or '直连无代理'}")
                return
            except ValueError as exc:
                print(f"❌ {exc}")

    def _configure_single_channel_in_list(self, ch: str) -> None:
        # 如果已经配置过，允许用户选择修改或移除
        if ch in self.selected_channels:
            print(f"\n[{CHANNEL_LABELS[ch]}] 当前已启用（{self.channel_notes.get(ch, '已配置')}）。")
            print("  [1] 修改配置 / 重新校验")
            print("  [2] 取消启用此渠道")
            print("  [0] 返回列表")
            act = _prompt_input("请选择操作 (0/1/2)", default="1").strip()
            if act == "0":
                return
            if act == "2":
                self.selected_channels = [c for c in self.selected_channels if c != ch]
                self.channel_configs.pop(ch, None)
                self.channel_notes.pop(ch, None)
                print(f"已取消启用 {CHANNEL_LABELS[ch]}。")
                return

        if ch == "qq":
            self._configure_qq()
        elif ch == "onebot":
            self._configure_onebot()
        elif ch == "telegram":
            self._configure_telegram()
        elif ch == "wechat":
            self._configure_wechat()

    def _mark_channel_enabled(self, ch: str, cfg: dict, note: str) -> None:
        if ch not in self.selected_channels:
            self.selected_channels.append(ch)
        self.channel_configs[ch] = cfg
        self.channel_notes[ch] = note

    def _configure_qq(self) -> None:
        status, cfg, note = _configure_qq_interactive(self.channel_configs.get("qq"))
        if status in ("ready", "unverified") and cfg is not None:
            self._mark_channel_enabled("qq", cfg, note)

    def _configure_onebot(self) -> None:
        status, cfg, note = _configure_onebot_interactive(self.channel_configs.get("onebot"))
        if status in ("ready", "unverified") and cfg is not None:
            self._mark_channel_enabled("onebot", cfg, note)

    def _configure_telegram(self) -> None:
        status, cfg, note = _configure_telegram_interactive(
            self.channel_configs.get("telegram"),
            default_proxy=self.muse_proxy_url,
        )
        if status in ("ready", "unverified") and cfg is not None:
            self._mark_channel_enabled("telegram", cfg, note)
            tg_proxy = cfg.get("proxy_url")
            if tg_proxy and not self.muse_proxy_url:
                if _prompt_bool(f"是否同步将该代理 ({tg_proxy}) 也用于 Muse 核心连接", False):
                    self.muse_proxy_url = tg_proxy

    def _configure_wechat(self) -> None:
        status, cfg, note = _configure_wechat_guided(self.channel_configs.get("wechat"))
        if status in ("ready", "unverified") and cfg is not None:
            self._mark_channel_enabled("wechat", cfg, note)

    async def _listen_for_first_message(self) -> None:
        """启动选定渠道的轻量监听，逐渠道捕获管理员身份。"""
        claimable = [ch for ch in self.selected_channels if ch in CLAIMABLE_CHANNELS]
        if not claimable:
            return
        results = await _listen_and_claim_channels(claimable, self.channel_configs)
        for ch, event in results.items():
            self.claimed_events[ch] = event
            uid, ctype, tid, mid = event
            if self.first_message_event is None:
                self._on_message_captured(ch, uid, ctype, tid, mid)

    def _on_message_captured(
        self,
        channel: str,
        user_id: str,
        chat_type: str,
        target_id: str,
        msg_id: Optional[str],
    ) -> None:
        self.claimed_events[channel] = (user_id, chat_type, target_id, msg_id)
        if not self._captured_event.is_set():
            self.first_message_event = (channel, user_id, chat_type, target_id, msg_id)
            self._captured_event.set()

    def _handle_unclaimed_and_group_options(self, claimable: List[str]) -> None:
        for ch in claimable:
            if ch not in self.claimed_events:
                print(f"\n⚠️  [{CHANNEL_LABELS[ch]}] 尚未通过消息认领管理员。")
                print("  [1] 手动输入管理员 ID")
                print("  [2] 暂时跳过（管理员留空，后续可在控制台认领或通过 /申请 配置）")
                act = _prompt_input("请选择操作 (1/2)", default="2").strip()
                if act == "1":
                    id_hint = {
                        "qq": "QQ 管理员 OpenID",
                        "onebot": "管理员 QQ 号",
                        "telegram": "管理员 Telegram 数字 User ID",
                    }[ch]
                    admin_val = _prompt_input(f"请输入 {id_hint}（留空跳过）", default="").strip()
                    if admin_val:
                        self.manual_admins[ch] = admin_val
                        print(f"✅ 已设置 {CHANNEL_LABELS[ch]} 管理员 ID: {admin_val}")
            else:
                uid, ctype, tid, _ = self.claimed_events[ch]
                if ctype == "group":
                    print(f"\n检测到 [{CHANNEL_LABELS[ch]}] 的认领消息来自群聊 ({tid})：")
                    settings: dict = {}
                    if ch in ("onebot", "telegram"):
                        settings["group_trigger"] = _prompt_group_trigger("at")
                    settings["bind_main_to_group"] = _prompt_bool(
                        "是否将 Muse 主会话直接绑定到该群聊？（选 N 则绑定到您的私聊）",
                        False,
                    )
                    self.group_settings[ch] = settings

    def _choose_login_notification_channel(self) -> None:
        if not self.selected_channels:
            self.login_through_adapter = ""
            return
        if len(self.selected_channels) == 1:
            only = self.selected_channels[0]
            if only == "wechat":
                use_wx = _prompt_bool("将微信设为 Muse 登录通知渠道（Cookie 失效时发给当前扫码用户）", False)
                self.wechat_login_notifications = use_wx
                self.login_through_adapter = "wechat" if use_wx else ""
            else:
                self.login_through_adapter = only
            return

        print("\n--- 选择 Muse 登录通知渠道 ---")
        print("说明：当 Muse Cookie 失效需要重新登录时，通过哪个渠道私聊提醒您？")
        default_ch = self.first_message_event[0] if self.first_message_event else self.selected_channels[0]
        for idx, ch in enumerate(self.selected_channels, 1):
            tag = "（默认）" if ch == default_ch else ""
            print(f"  [{idx}] {CHANNEL_LABELS[ch]} ({ch}) {tag}")
        print("  [0] 暂不指定（留空）")

        default_idx = str(self.selected_channels.index(default_ch) + 1)
        while True:
            raw = _prompt_input(f"请输入编号 (0-{len(self.selected_channels)})", default=default_idx).strip()
            if raw == "0":
                self.login_through_adapter = ""
                return
            if raw.isdigit() and 1 <= int(raw) <= len(self.selected_channels):
                picked = self.selected_channels[int(raw) - 1]
                self.login_through_adapter = picked
                self.wechat_login_notifications = picked == "wechat"
                return
            print("❌ 无效选项，请重新输入。")

    @staticmethod
    def _save_session_map(map_file_path: str, chat_key: str, session_id: str) -> None:
        """预先将私聊或群聊会话绑定写入持久化文件。"""
        try:
            current_map = {}
            if os.path.exists(map_file_path):
                try:
                    current_map = _load_json_file(map_file_path)
                except Exception:
                    current_map = {}
            current_map[chat_key] = session_id
            _save_json_file(map_file_path, current_map)
        except Exception as e:
            logger.debug("Failed to write initial session map %s: %s", map_file_path, safe_error(e))

    def _resolve_channel_claim(self, ch: str) -> Optional[Tuple[str, str, str, Optional[str]]]:
        if ch in self.claimed_events:
            return self.claimed_events[ch]
        if self.first_message_event and self.first_message_event[0] == ch:
            _, uid, ctype, tid, mid = self.first_message_event
            return (uid, ctype, tid, mid)
        return None

    def _generate_and_save_config(self) -> None:
        if not self.selected_channels:
            print("⚠️ 未选择任何渠道，未生成配置。")
            return

        # 若目标已存在，先自动备份
        backup_path = _backup_config_file(self.output_file)
        if backup_path:
            print(f"📦 已将原有配置文件备份至: {backup_path}")

        # 确定主会话与 Muse 节点（保留已有非向导字段）
        existing_muse = self.existing_data.get("muse", {}) if self.preserve_muse else {}
        main_session_id = existing_muse.get("session_id") or str(uuid.uuid4())
        muse_section: Dict[str, Any] = {
            "cookies_file": existing_muse.get("cookies_file") or "cookies.json",
            "mint_file": existing_muse.get("mint_file") or ".mint.json",
            "session_id": main_session_id,
        }
        for keep_key in ("host", "client_ip", "timezone", "approval", "worker"):
            if keep_key in existing_muse:
                muse_section[keep_key] = copy.deepcopy(existing_muse[keep_key])
        if self.muse_proxy_url:
            muse_section["proxy_url"] = self.muse_proxy_url

        if self.login_through_adapter is not None:
            notification_channel = self.login_through_adapter
        elif self.wechat_login_notifications:
            notification_channel = "wechat"
        elif self.first_message_event:
            notification_channel = self.first_message_event[0]
        else:
            notification_channel = ""

        config_data: Dict[str, Any] = {}
        # 仅当选择保留旧设置时，继承已有配置文件里的无关顶级段（如 logging / overrides）
        if self.preserve_muse:
            for preserve_root in ("logging", "overrides"):
                if preserve_root in self.existing_data:
                    config_data[preserve_root] = copy.deepcopy(self.existing_data[preserve_root])

        config_data["muse"] = muse_section
        config_data["adapters"] = {}
        existing_gw = self.existing_data.get("gateway", {}) if self.preserve_muse else {}
        gw_section = copy.deepcopy(existing_gw) if isinstance(existing_gw, dict) else {}
        gw_section["login_through_adapter"] = notification_channel
        config_data["gateway"] = gw_section

        # 1. QQ 适配器配置（未启用 QQ 时必须显式写 enabled: False，防止 QQConfig 默认开启）
        if "qq" in self.selected_channels:
            qq_raw = self.channel_configs["qq"]
            claim = self._resolve_channel_claim("qq")
            manual_id = self.manual_admins.get("qq", "")
            qq_admin_id: List[str] = []
            qq_whitelist: Dict[str, Any] = {"private": []}
            qq_bindings: Dict[str, str] = {}

            if claim:
                uid, ctype, tid, _ = claim
                qq_admin_id = [uid]
                qq_whitelist["private"] = [uid]
                if ctype == "group":
                    qq_whitelist[tid] = "*"
                bind_group = bool(self.group_settings.get("qq", {}).get("bind_main_to_group", False))
                chat_key = f"group:{tid}" if (ctype == "group" and bind_group) else f"private:{uid}"
                qq_bindings[chat_key] = main_session_id
                self._save_session_map("data/qq_session_map.json", chat_key, main_session_id)
            elif manual_id:
                qq_admin_id = [manual_id]
                qq_whitelist["private"] = [manual_id]
                chat_key = f"private:{manual_id}"
                qq_bindings[chat_key] = main_session_id
                self._save_session_map("data/qq_session_map.json", chat_key, main_session_id)

            qq_entry: Dict[str, Any] = {
                "enabled": True,
                "app_id": qq_raw["app_id"],
                "app_secret": qq_raw["app_secret"],
                "admin_id": qq_admin_id,
                "admin_only": True,
                "whitelist": qq_whitelist,
                "user_agent": constants.DEFAULT_USER_AGENT,
                "credentials_in_place": True,
            }
            if qq_bindings:
                qq_entry["session_bindings"] = qq_bindings
            config_data["adapters"]["qq"] = qq_entry
        else:
            # QQConfig.enabled defaults to True when omitted; explicitly disable it.
            config_data["adapters"]["qq"] = {"enabled": False}

        # 2. OneBot 适配器配置（不写 123456789 占位符）
        if "onebot" in self.selected_channels:
            ob_raw = self.channel_configs["onebot"]
            claim = self._resolve_channel_claim("onebot")
            manual_id = self.manual_admins.get("onebot", "")
            ob_whitelist: Dict[str, Any] = {"private": [], "groups": []}
            ob_entry: Dict[str, Any] = {
                "enabled": True,
                "base_url": ob_raw["base_url"],
                "access_token": ob_raw["access_token"],
                "ws_url": ob_raw["ws_url"],
                "group_trigger": self.group_settings.get("onebot", {}).get(
                    "group_trigger", ob_raw.get("group_trigger", "at")
                ),
                "admin_id": [],
                "qzone_feed": asdict(QzoneFeedConfig()),
            }

            if claim:
                uid, ctype, tid, _ = claim
                ob_entry["admin_id"] = [uid]
                ob_whitelist["private"] = [uid]
                if ctype == "group":
                    try:
                        gid_int = int(tid)
                        ob_entry["target_group_id"] = gid_int
                        ob_whitelist["groups"] = [gid_int]
                    except ValueError:
                        ob_whitelist[tid] = "*"
                bind_group = bool(self.group_settings.get("onebot", {}).get("bind_main_to_group", False))
                chat_key = f"group:{tid}" if (ctype == "group" and bind_group) else f"private:{uid}"
                ob_entry["session_bindings"] = {chat_key: main_session_id}
                self._save_session_map("data/onebot_session_map.json", chat_key, main_session_id)
            elif manual_id:
                ob_entry["admin_id"] = [manual_id]
                ob_whitelist["private"] = [manual_id]
                chat_key = f"private:{manual_id}"
                ob_entry["session_bindings"] = {chat_key: main_session_id}
                self._save_session_map("data/onebot_session_map.json", chat_key, main_session_id)

            ob_entry["whitelist"] = ob_whitelist
            config_data["adapters"]["onebot"] = ob_entry

        # 3. Telegram 适配器配置
        if "telegram" in self.selected_channels:
            tg_raw = self.channel_configs["telegram"]
            claim = self._resolve_channel_claim("telegram")
            manual_id = self.manual_admins.get("telegram", "")
            tg_whitelist: Dict[str, Any] = {"private": []}
            tg_entry: Dict[str, Any] = {
                "enabled": True,
                "bot_token": tg_raw["bot_token"],
                "group_trigger": self.group_settings.get("telegram", {}).get(
                    "group_trigger", tg_raw.get("group_trigger", "at")
                ),
                "admin_id": [],
            }
            if tg_raw.get("proxy_url"):
                tg_entry["proxy_url"] = tg_raw["proxy_url"]

            if claim:
                uid, ctype, tid, _ = claim
                tg_entry["admin_id"] = [uid]
                tg_whitelist["private"] = [uid]
                if ctype == "group":
                    tg_whitelist[tid] = "*"
                bind_group = bool(self.group_settings.get("telegram", {}).get("bind_main_to_group", False))
                chat_key = f"group:{tid}" if (ctype == "group" and bind_group) else f"private:{uid}"
                tg_entry["session_bindings"] = {chat_key: main_session_id}
                self._save_session_map("data/telegram_session_map.json", chat_key, main_session_id)
            elif manual_id:
                tg_entry["admin_id"] = [manual_id]
                tg_whitelist["private"] = [manual_id]
                chat_key = f"private:{manual_id}"
                tg_entry["session_bindings"] = {chat_key: main_session_id}
                self._save_session_map("data/telegram_session_map.json", chat_key, main_session_id)

            tg_entry["whitelist"] = tg_whitelist
            config_data["adapters"]["telegram"] = tg_entry

        # 4. 微信 iLink 适配器配置（保持稀疏且无管理员/白名单字段）
        if "wechat" in self.selected_channels:
            wx_raw = dict(self.channel_configs["wechat"])
            wx_raw["enabled"] = True
            config_data["adapters"]["wechat"] = wx_raw

        _save_json_file(self.output_file, config_data)
        self._print_readable_summary(config_data)

    def _print_readable_summary(self, config_data: dict) -> None:
        print("\n" + "=" * 60)
        print(f"🎉 配置文件已生成并保存至: {self.output_file}")
        print("=" * 60)
        muse = config_data.get("muse", {})
        gw = config_data.get("gateway", {})
        print(f"  • Muse 主会话 : {muse.get('session_id')}")
        print(f"  • Muse 代理   : {muse.get('proxy_url') or '直连无代理'}")
        print(f"  • 登录通知渠道: {gw.get('login_through_adapter') or '(未指定)'}")
        adapters = config_data.get("adapters", {})
        for ch in self.selected_channels:
            entry = adapters.get(ch, {})
            if ch == "wechat":
                print(f"  • {CHANNEL_LABELS[ch]}: 已启用（{_wechat_local_status_text(entry)}）")
            else:
                admins = entry.get("admin_id") or []
                extra = f", 群触发: {entry.get('group_trigger')}" if "group_trigger" in entry else ""
                admin_desc = ", ".join(admins) if admins else "未认领（可后续在控制台补设）"
                print(f"  • {CHANNEL_LABELS[ch]}: 已启用 | 管理员: {admin_desc}{extra}")
        print("=" * 60)

    def _offer_muse_login(self) -> None:
        cookies_file = (
            self.existing_data.get("muse", {}).get("cookies_file")
            if self.preserve_muse
            else "cookies.json"
        ) or "cookies.json"
        if os.path.exists(cookies_file):
            print(f"\n✅ 已检测到现有 Muse 登录凭据: {cookies_file}")
            return
        print(f"\n--- 步骤 3/3：登录 Muse 账号 ---")
        if not _prompt_bool(f"未检测到 {cookies_file}，是否现在通过邮箱验证码登录 Muse", True):
            print("ℹ️  已跳过 Muse 登录；稍后可随时在控制台的「账号登录」或执行 python main.py --login 完成。")
            return
        try:
            from muse_bridge.muse.login import run_protocol_login

            run_protocol_login(output_path=cookies_file, proxy_url=self.muse_proxy_url)
        except Exception as exc:
            print(f"❌ Muse 登录失败: {safe_error(exc)}")
            print("ℹ️  配置已保存，稍后可在控制台「账号登录」重试。")


# ----------------------------------------------------------------------
# 日常运维与配置控制台
# ----------------------------------------------------------------------

def _missing_required_credentials(ch: str, entry: Optional[dict]) -> List[str]:
    """检查某渠道启用前缺失的必要凭证或登录条件。"""
    cfg = entry if isinstance(entry, dict) else {}
    if ch == "qq":
        missing = []
        if not str(cfg.get("app_id") or "").strip():
            missing.append("app_id")
        if not str(cfg.get("app_secret") or "").strip():
            missing.append("app_secret")
        return missing
    if ch == "onebot":
        missing = []
        if not str(cfg.get("base_url") or "").strip():
            missing.append("base_url")
        if not str(cfg.get("ws_url") or "").strip():
            missing.append("ws_url")
        return missing
    if ch == "telegram":
        if not _validate_telegram_token(str(cfg.get("bot_token") or "")):
            return ["bot_token"]
        return []
    if ch == "wechat":
        if _wechat_local_status_text(cfg) != "本地已登录":
            return ["微信扫码登录"]
        return []
    return []


class BridgeConsole:
    """面向日常使用的统一运维与配置控制台（所有修改实时自动保存）。"""

    def __init__(self, file_path: str = "config.json", *, auto_save: bool = True):
        self.file_path = file_path
        self.auto_save = auto_save
        self.data: dict = {}
        self._dirty: bool = False
        self.request_start_bridge: bool = False

    @property
    def _modified(self) -> bool:
        return self._dirty

    @_modified.setter
    def _modified(self, value: bool) -> None:
        self._dirty = bool(value)
        if self._dirty and self.auto_save:
            self.save(realtime=True)

    def load(self) -> bool:
        if not os.path.exists(self.file_path):
            self.data = {}
            self._dirty = False
            return False
        try:
            self.data = _load_json_file(self.file_path)
            self._dirty = False
            return True
        except Exception as exc:
            print(f"⚠️  读取配置文件 {self.file_path} 出错: {safe_error(exc)}")
            self.data = {}
            return False

    def save(self, *, realtime: bool = False) -> None:
        try:
            _save_json_file(self.file_path, self.data)
            if realtime:
                print(f"💾 已实时保存至: {self.file_path}")
            else:
                print(f"✅ 配置已保存至: {self.file_path}")
            self._dirty = False
        except Exception as exc:
            print(f"❌ 保存配置失败: {safe_error(exc)}")

    def _is_channel_enabled(self, ch: str) -> bool:
        adapters = self.data.get("adapters", {})
        if not isinstance(adapters, dict):
            return False
        entry = adapters.get(ch)
        if not isinstance(entry, dict):
            return False
        if ch == "qq":
            return bool(entry.get("enabled", True))
        return bool(entry.get("enabled", False))

    def _channel_dashboard_line(self, ch: str) -> str:
        adapters = self.data.get("adapters", {})
        entry = adapters.get(ch, {}) if isinstance(adapters, dict) else {}
        enabled = self._is_channel_enabled(ch)
        status = "✅ 已启用" if enabled else "⚪ 未启用"
        if not enabled:
            return status
        if ch == "qq":
            admins = entry.get("admin_id") or []
            app_id = entry.get("app_id") or "未填 AppID"
            return f"{status} | AppID: {app_id} | 管理员 {len(admins)} 人"
        if ch == "onebot":
            admins = entry.get("admin_id") or []
            base = entry.get("base_url") or "未填地址"
            trig = entry.get("group_trigger", constants.DEFAULT_ONEBOT_GROUP_TRIGGER)
            return f"{status} | {base} | 管理员 {len(admins)} 人 | 群模式: {trig}"
        if ch == "telegram":
            admins = entry.get("admin_id") or []
            trig = entry.get("group_trigger", "at")
            proxy = "代理" if entry.get("proxy_url") else "直连"
            return f"{status} | {proxy} | 管理员 {len(admins)} 人 | 群模式: {trig}"
        if ch == "wechat":
            return f"{status} | {_wechat_local_status_text(entry)}"
        return status

    def _print_dashboard(self) -> None:
        muse = self.data.get("muse", {}) if isinstance(self.data.get("muse"), dict) else {}
        gw = self.data.get("gateway", {}) if isinstance(self.data.get("gateway"), dict) else {}
        cookies_file = muse.get("cookies_file") or "cookies.json"
        cookie_state = "✅ 已登录" if os.path.exists(cookies_file) else "⚠️ 未登录"
        proxy_state = muse.get("proxy_url") or "直连"
        notify_ch = gw.get("login_through_adapter") or "(未设置)"
        dirty = " *未保存*" if self._modified else ""

        print("\n" + "=" * 68)
        print(f"  🛠️  Muse IM Bridge 控制台 ({self.file_path}){dirty}")
        print("=" * 68)
        print(_format_channel_row(None, "Muse 账号", f"{cookie_state} ({cookies_file}) | 代理: {proxy_state}"))
        print(_format_channel_row(None, "登录通知渠道", notify_ch))
        print("-" * 68)
        for ch in CHANNEL_ORDER:
            print(_format_channel_row(None, CHANNEL_LABELS[ch], self._channel_dashboard_line(ch)))
        print("=" * 68)

    def run(self) -> bool:
        """运行主控制台循环。返回 True 表示用户选择立即启动网桥。"""
        if not self.load():
            print(f"💡 未检测到配置文件: {self.file_path}，即将进入首次引导配置。")
            wizard = FirstRunWizard(output_file=self.file_path)
            if not wizard.run_interactive(offer_muse_login=True):
                return False
            self.load()
            if _prompt_bool("初始化已完成！是否立即启动网桥服务（选 N 进入控制台微调）", True):
                self.request_start_bridge = True
                return True

        while True:
            self._print_dashboard()
            print("请选择操作（所有修改均实时保存，随时可 Ctrl+C 直接退出）：")
            print("  [1] 渠道管理（配置 / 开关 / 发消息认领 / 单渠道测通）")
            print("  [2] 账号登录（Muse 邮箱登录 / 微信扫码与状态 / 通知渠道）")
            print("  [3] 一键连通性测试（检查全部已启用渠道）")
            print("  [4] 高级设置（Muse 核心 / 审批 / 网关 / 日志 / 分块 / Qzone）")
            print("  [5] 查看当前完整配置 JSON")
            print("  [6] 启动网桥")
            print("  [7] 重新初始化（从头引导配置）")
            print("  [0] 退出")

            choice = _prompt_input("请输入操作编号 (0-7)", default="1").strip()
            if choice == "0":
                if self._modified:
                    self.save()
                print("已退出控制台。")
                return False
            elif choice == "1":
                self._channel_manager_menu()
            elif choice == "2":
                self._account_login_menu()
            elif choice == "3":
                self._run_connectivity_check()
            elif choice == "4":
                editor = FullConfigEditor(self.data, self.file_path, auto_save=self.auto_save)
                editor.run_menu()
                if editor._modified:
                    self._modified = True
            elif choice == "5":
                print("\n" + "=" * 60)
                print(json.dumps(self.data, indent=2, ensure_ascii=False))
                print("=" * 60)
            elif choice == "6":
                if self._modified or not os.path.exists(self.file_path):
                    self.save()
                self.request_start_bridge = True
                return True
            elif choice == "7":
                self._reinitialize_flow()
            else:
                print("❌ 无效选项，请输入 0-7。")

    # ---------------- 1. 渠道管理 ----------------
    def _channel_manager_menu(self) -> None:
        while True:
            print("\n--- 渠道管理（选择平台进行配置、开关、认领或测通）---")
            for idx, ch in enumerate(CHANNEL_ORDER, 1):
                print(_format_channel_row(str(idx), CHANNEL_LABELS[ch], self._channel_dashboard_line(ch)))
            print("  [0] 返回主菜单")
            choice = _prompt_input("请选择渠道 (0-4)", default="0").strip()
            if choice == "0":
                return
            if choice in ("1", "2", "3", "4"):
                self._manage_single_channel(CHANNEL_ORDER[int(choice) - 1])
            else:
                print("❌ 请输入 0-4。")

    def _ensure_qq_explicit_state(self) -> None:
        adapters = self.data.setdefault("adapters", {})
        if "qq" not in adapters:
            adapters["qq"] = {"enabled": False}

    def _enable_channel_flow(self, ch: str) -> None:
        """启用渠道前校验必要凭证，并询问用户是否进入引导配置。"""
        adapters = self.data.setdefault("adapters", {})
        entry = adapters.get(ch, {}) if isinstance(adapters.get(ch), dict) else {}
        missing = _missing_required_credentials(ch, entry)

        if ch == "wechat":
            if missing:
                print("⚠️  微信尚未检测到本地登录账号。")
                if _prompt_bool("是否现在进入微信配置 / 扫码登录？（选 N 取消启用）", True):
                    status, cfg, _ = _configure_wechat_guided(entry)
                    if status in ("ready", "unverified") and cfg is not None:
                        adapters["wechat"] = cfg
                        self._ensure_qq_explicit_state()
                        self._modified = True
                        print("✅ 微信适配器已启用。")
                    else:
                        print("ℹ️  未完成微信启用，保持未启用状态。")
                else:
                    print("已取消启用微信适配器。")
                return
            if _prompt_bool("已检测到微信本地登录态，启用前是否需要重新配置或重新扫码", False):
                status, cfg, _ = _configure_wechat_guided(entry)
                if status in ("ready", "unverified") and cfg is not None:
                    adapters["wechat"] = cfg
                    self._ensure_qq_explicit_state()
                    self._modified = True
                    print("✅ 微信适配器已更新并启用。")
                return
            wx = adapters.setdefault("wechat", {})
            wx["enabled"] = True
            self._ensure_qq_explicit_state()
            self._modified = True
            print("✅ 已启用微信适配器。")
            return

        if missing:
            print(f"⚠️  {CHANNEL_LABELS[ch]} 尚未填写必要凭证（缺少: {'、'.join(missing)}），不能直接启用。")
            if _prompt_bool("是否现在进入引导配置输入必要凭证并测通？（选 N 取消启用）", True):
                self._guided_update_channel(ch)
            else:
                print(f"已取消启用 {CHANNEL_LABELS[ch]}。")
            return

        print(f"ℹ️  检测到 {CHANNEL_LABELS[ch]} 已有凭证配置。")
        if _prompt_bool("启用前是否需要重新配置或修改凭证", False):
            self._guided_update_channel(ch)
            return
        target = adapters.setdefault(ch, {})
        target["enabled"] = True
        self._ensure_qq_explicit_state()
        self._modified = True
        print(f"✅ 已使用现有凭证启用 {CHANNEL_LABELS[ch]}。")
        if not target.get("admin_id") and _prompt_bool("该渠道尚未配置管理员，是否现在通过发消息认领", True):
            self._daily_claim_for_channel(ch)

    def _manage_single_channel(self, ch: str) -> None:
        adapters = self.data.setdefault("adapters", {})
        while True:
            entry = adapters.get(ch, {}) if isinstance(adapters.get(ch), dict) else {}
            enabled = self._is_channel_enabled(ch)
            print(f"\n--- 渠道管理：{CHANNEL_LABELS[ch]} ---")
            print(f"当前状态: {self._channel_dashboard_line(ch)}")
            if ch == "wechat":
                print(f"  [1] {'停用' if enabled else '启用'}微信适配器")
                print("  [2] 扫码登录 / 重新登录微信")
                print("  [3] 查看本地登录状态（不连接微信）")
                print("  [4] 引导式基础设置（输入状态提示 / 离线补发）")
                print("  [5] 编辑微信全部高级参数")
                print("  [0] 返回渠道列表")
                act = _prompt_input("请选择操作 (0-5)", default="0").strip()
                if act == "0":
                    return
                elif act == "1":
                    if enabled:
                        wx = adapters.setdefault("wechat", {})
                        wx["enabled"] = False
                        self._ensure_qq_explicit_state()
                        self._modified = True
                        print("✅ 已停用微信适配器。")
                    else:
                        self._enable_channel_flow("wechat")
                elif act == "2":
                    from muse_bridge.adapters.wechat.cli import run_login

                    wx = adapters.setdefault("wechat", {})
                    try:
                        cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": wx}})
                        run_login(cfg)
                        if not wx.get("enabled"):
                            wx["enabled"] = True
                            self._ensure_qq_explicit_state()
                            self._modified = True
                    except Exception:
                        print("❌ 微信扫码登录失败；未输出认证详情。")
                elif act == "3":
                    from muse_bridge.adapters.wechat.cli import run_status

                    try:
                        cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": entry}})
                        run_status(cfg)
                    except Exception:
                        print("❌ 无法读取本地微信状态。")
                elif act == "4":
                    status, cfg, _ = _configure_wechat_guided(entry)
                    if status in ("ready", "unverified") and cfg is not None:
                        adapters["wechat"] = cfg
                        self._ensure_qq_explicit_state()
                        self._modified = True
                elif act == "5":
                    editor = FullConfigEditor(self.data, self.file_path, auto_save=self.auto_save)
                    editor._edit_wechat_category()
                    if editor._modified:
                        self._modified = True
                else:
                    print("❌ 无效选项。")
            else:
                print(f"  [1] {'停用' if enabled else '启用'}此渠道")
                print("  [2] 引导配置凭据并立即测通")
                print("  [3] 通过发消息新增管理员 / 绑定群聊")
                print("  [4] 立即测通此渠道")
                print("  [5] 编辑此渠道全部详细字段")
                print("  [0] 返回渠道列表")
                act = _prompt_input("请选择操作 (0-5)", default="0").strip()
                if act == "0":
                    return
                elif act == "1":
                    if enabled:
                        target = adapters.setdefault(ch, {})
                        target["enabled"] = False
                        self._ensure_qq_explicit_state()
                        self._modified = True
                        print(f"✅ 已停用 {CHANNEL_LABELS[ch]}。")
                    else:
                        self._enable_channel_flow(ch)
                elif act == "2":
                    self._guided_update_channel(ch)
                elif act == "3":
                    self._daily_claim_for_channel(ch)
                elif act == "4":
                    self._probe_single_channel(ch)
                elif act == "5":
                    editor = FullConfigEditor(self.data, self.file_path, auto_save=self.auto_save)
                    if ch == "qq":
                        editor._edit_qq_category()
                    elif ch == "onebot":
                        editor._edit_onebot_category()
                    elif ch == "telegram":
                        editor._edit_telegram_category()
                    if editor._modified:
                        self._modified = True
                else:
                    print("❌ 无效选项。")

    def _guided_update_channel(self, ch: str) -> None:
        adapters = self.data.setdefault("adapters", {})
        existing = adapters.get(ch, {}) if isinstance(adapters.get(ch), dict) else {}
        if ch == "qq":
            status, cfg, _ = _configure_qq_interactive(existing)
        elif ch == "onebot":
            status, cfg, _ = _configure_onebot_interactive(existing)
        elif ch == "telegram":
            muse_proxy = (self.data.get("muse") or {}).get("proxy_url")
            status, cfg, _ = _configure_telegram_interactive(existing, default_proxy=muse_proxy)
        else:
            return
        if status not in ("ready", "unverified") or cfg is None:
            return
        target = adapters.setdefault(ch, {})
        target.update(cfg)
        target["enabled"] = True
        if ch == "onebot" and "qzone_feed" not in target:
            target["qzone_feed"] = asdict(QzoneFeedConfig())
        self._ensure_qq_explicit_state()
        self._modified = True
        print(f"✅ {CHANNEL_LABELS[ch]} 凭据已更新并启用。")
        if not target.get("admin_id") and _prompt_bool("该渠道尚未配置管理员，是否现在通过发消息认领", True):
            self._daily_claim_for_channel(ch)

    def _daily_claim_for_channel(self, ch: str) -> None:
        adapters = self.data.setdefault("adapters", {})
        entry = adapters.get(ch, {})
        if not isinstance(entry, dict):
            print("⚠️  请先配置该渠道凭据。")
            return
        results = asyncio.run(_listen_and_claim_channels([ch], {ch: entry}))
        if ch not in results:
            print("ℹ️  未收到认领消息，配置未更改。")
            return
        uid, ctype, tid, _ = results[ch]
        admins = list(entry.get("admin_id") or [])
        if uid not in admins and _prompt_bool(f"是否将用户 {uid} 添加为 {CHANNEL_LABELS[ch]} 管理员", True):
            admins.append(uid)
            entry["admin_id"] = admins
            self._modified = True

        wl = entry.setdefault("whitelist", {})
        priv = list(wl.get("private") or [])
        if uid not in priv:
            priv.append(uid)
            wl["private"] = priv
            self._modified = True

        if ctype == "group":
            if _prompt_bool(f"是否将群 {tid} 加入该渠道白名单", True):
                if ch == "onebot":
                    groups = list(wl.get("groups") or [])
                    try:
                        gid_val: Any = int(tid)
                    except ValueError:
                        gid_val = tid
                    if gid_val not in groups:
                        groups.append(gid_val)
                    wl["groups"] = groups
                else:
                    wl[tid] = "*"
                self._modified = True
            if ch in ("onebot", "telegram"):
                if _prompt_bool(f"是否调整群触发模式（当前: {entry.get('group_trigger', 'at')}）", False):
                    entry["group_trigger"] = _prompt_group_trigger(entry.get("group_trigger", "at"))
                    self._modified = True
        print(f"✅ 已完成 {CHANNEL_LABELS[ch]} 的身份与白名单更新。")

    def _probe_single_channel(self, ch: str) -> bool:
        adapters = self.data.get("adapters", {})
        entry = adapters.get(ch, {}) if isinstance(adapters, dict) else {}
        if ch == "qq":
            ok, msg = _probe_qq(entry.get("app_id", ""), entry.get("app_secret", ""))
        elif ch == "onebot":
            ok, msg, _ = _probe_onebot(
                entry.get("base_url", ""),
                entry.get("access_token", ""),
                entry.get("ws_url", ""),
            )
        elif ch == "telegram":
            ok, msg, _ = _probe_telegram(entry.get("bot_token", ""), entry.get("proxy_url"))
        elif ch == "wechat":
            status_text = _wechat_local_status_text(entry)
            ok = status_text == "本地已登录"
            msg = f"微信离线状态检查（不连接微信）: {status_text}"
        else:
            ok, msg = False, "未知渠道"
        icon = "✅" if ok else "❌"
        print(f"  {icon} [{CHANNEL_LABELS[ch]}] {msg}")
        return ok

    # ---------------- 2. 账号登录 ----------------
    def _account_login_menu(self) -> None:
        while True:
            muse = self.data.setdefault("muse", {})
            gw = self.data.setdefault("gateway", {})
            cookies_file = muse.get("cookies_file") or "cookies.json"
            cookie_state = "已存在" if os.path.exists(cookies_file) else "缺失"
            wx_entry = (self.data.get("adapters") or {}).get("wechat", {})
            print("\n--- 账号登录与通知通道 ---")
            print(f"  [1] Muse 邮箱验证码登录（当前 {cookies_file}: {cookie_state}）")
            print(f"  [2] 微信 iLink 扫码登录（当前: {_wechat_local_status_text(wx_entry)}）")
            print("  [3] 查看微信本地登录状态（不连接微信）")
            print(f"  [4] 设置默认登录通知渠道（当前: {gw.get('login_through_adapter') or '未设置'}）")
            print("  [0] 返回主菜单")
            act = _prompt_input("请选择操作 (0-4)", default="0").strip()
            if act == "0":
                return
            elif act == "1":
                from muse_bridge.muse.login import run_protocol_login
                from muse_bridge.config.models import WorkerConfig

                worker_raw = muse.get("worker") if isinstance(muse.get("worker"), dict) else {}
                try:
                    worker_cfg = WorkerConfig(**worker_raw) if worker_raw else None
                    run_protocol_login(
                        output_path=cookies_file,
                        worker=worker_cfg,
                        proxy_url=muse.get("proxy_url"),
                    )
                except Exception as exc:
                    print(f"❌ Muse 登录失败: {safe_error(exc)}")
            elif act == "2":
                from muse_bridge.adapters.wechat.cli import run_login

                try:
                    cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": wx_entry}})
                    run_login(cfg)
                except Exception:
                    print("❌ 微信扫码登录失败；未输出认证详情。")
            elif act == "3":
                from muse_bridge.adapters.wechat.cli import run_status

                try:
                    cfg = ConfigLoader._wechat_from_raw({"adapters": {"wechat": wx_entry}})
                    run_status(cfg)
                except Exception:
                    print("❌ 无法读取本地微信状态。")
            elif act == "4":
                ok, val = _prompt_edit_choice(
                    "请选择登录通知适配器",
                    ["qq", "onebot", "telegram", "wechat", ""],
                    _format_field_state(gw, "login_through_adapter", ""),
                )
                if ok:
                    gw["login_through_adapter"] = val
                    self._modified = True
            else:
                print("❌ 无效选项。")

    # ---------------- 3. 一键连通性测试 ----------------
    def _run_connectivity_check(self) -> None:
        print("\n" + "=" * 60)
        print("🩺 正在执行一键配置与连通性体检...")
        print("=" * 60)
        muse = self.data.get("muse", {}) if isinstance(self.data.get("muse"), dict) else {}
        cookies_file = muse.get("cookies_file") or "cookies.json"
        if os.path.exists(cookies_file):
            print(f"  ✅ [Muse] 登录凭据文件存在 ({cookies_file})")
        else:
            print(f"  ⚠️  [Muse] 尚未登录，缺少 {cookies_file}（可在「账号登录」中完成）")

        enabled_channels = [ch for ch in CHANNEL_ORDER if self._is_channel_enabled(ch)]
        if not enabled_channels:
            print("  ⚠️  当前未启用任何聊天渠道，请进入「渠道管理」启用至少一个渠道。")
        else:
            for ch in enabled_channels:
                self._probe_single_channel(ch)

        if self._modified:
            print("  ℹ️  检测到内存中有未保存更改，建议先保存配置。")
        print("=" * 60)

    # ---------------- 7. 重新初始化 ----------------
    def _reinitialize_flow(self) -> None:
        print("\n⚠️  即将重新初始化配置：")
        print(f"   • 原配置文件会自动备份为 {self.file_path}.bak")
        print("   • 之后会从头走一遍渠道选择、即时校验与管理员认领引导")
        if not _prompt_bool("确认要重新初始化配置吗", False):
            print("已取消重新初始化。")
            return
        preserve_muse = _prompt_bool("是否保留现有的 Muse 会话 ID、代理与日志/高级设置", True)
        wizard = FirstRunWizard(
            output_file=self.file_path,
            existing_data=self.data,
            preserve_muse=preserve_muse,
        )
        if wizard.run_interactive(offer_muse_login=True):
            self.load()


# ----------------------------------------------------------------------
# 脚本主程序入口
# ----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target_file = "config.json"
    if args and not args[0].startswith("-"):
        target_file = args[0]

    console = BridgeConsole(file_path=target_file)
    start_bridge = console.run()
    if start_bridge:
        import main as bridge_main

        print("\n🚀 正在启动 Muse IM Bridge...")
        return bridge_main.main(["--config", target_file])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 用户中断，退出。")
        sys.exit(0)
