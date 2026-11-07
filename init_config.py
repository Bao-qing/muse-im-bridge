#!/usr/bin/env python3
"""Muse Bridge 配置管理与首次启动向导 (Config Manager & Setup Wizard)

功能包含：
1. 【首次启动向导】：交互式配置渠道凭证、开启网关监听、自动捕获首条消息将发送者设为管理员，并将对话绑定至主会话；
2. 【全量配置分类修改器】：支持按清晰分类（Muse 核心、登录网关、QQ 适配器、OneBot 适配器、日志与追踪、高级与限制）对所有配置项进行交互式编辑、预览和保存。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from muse_bridge.config import constants
from muse_bridge.config.models import LoggingConfig
from muse_bridge.observability import configure_logging, get_logger, safe_error

logger = get_logger("setup_wizard")


# ----------------------------------------------------------------------
# 交互辅助函数
# ----------------------------------------------------------------------

def _prompt_input(prompt: str, default: str = "") -> str:
    display_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        val = input(display_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[!] 操作已取消。")
        sys.exit(0)
    return val if val else default


def _prompt_str(title: str, current_val: Any, default_val: str = "") -> str:
    curr_str = "" if current_val is None else str(current_val)
    disp = curr_str if curr_str else default_val
    val = _prompt_input(f"{title}", default=disp)
    return val


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
    disp_choices = "/".join(choices)
    while True:
        raw = _prompt_input(f"{title} ({disp_choices})", default=current_val).strip()
        if raw in choices:
            return raw
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


# ----------------------------------------------------------------------
# 全量配置交互修改器
# ----------------------------------------------------------------------

class FullConfigEditor:
    """提供按大类层级交互式修改 config.json 全量设置。"""

    def __init__(self, config_data: dict, file_path: str = "config.json"):
        self.data = config_data
        self.file_path = file_path
        self._modified = False

        # 确保关键根节点存在
        self.data.setdefault("muse", {})
        self.data.setdefault("adapters", {})
        self.data.setdefault("gateway", {})
        self.data.setdefault("logging", {})
        self.data.setdefault("overrides", {})

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"✅ 配置文件已成功保存至: {self.file_path}")
            self._modified = False
        except Exception as e:
            print(f"❌ 保存配置文件失败: {safe_error(e)}")

    def run_menu(self) -> None:
        while True:
            print("\n" + "=" * 60)
            print("          ⚙️  Muse IM Bridge 全量配置分类设置")
            print("=" * 60)
            print("请选择要修改的配置分类：")
            print("  [1] 🧠 Muse 核心配置 (会话ID / Cookie / 域名 / 审批模式)")
            print("  [2] 🚪 默认登录通道 (gateway.login_through_adapter)")
            print("  [3] 🐧 QQ 官方机器人 (凭据 / 管理员 / 权限开关 / 白名单)")
            print("  [4] 🤖 OneBot (NapCat) (地址 / Token / 触发策略 / 白名单)")
            print("  [5] 📝 日志与监控 (级别 / 控制台 / 日志文件 / 追踪 Trace)")
            print("  [6] ⚡ 高级限制与性能参数 (分块字符 / 消息去重 / 超时参数)")
            print("  [7] 📄 预览当前完整 JSON 配置")
            print("  [8] 💾 保存修改到文件")
            print("  [0] 保存并退出")
            print("-" * 60)

            choice = _prompt_input("请输入分类编号 (0-8)", default="0")
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
                print("\n" + "=" * 60)
                print(json.dumps(self.data, indent=2, ensure_ascii=False))
                print("=" * 60)
            elif choice == "8":
                self.save()
            else:
                print("❌ 无效的选项，请重新输入。")

    # ---------------- 1. Muse 核心分类 ----------------
    def _edit_muse_category(self) -> None:
        m = self.data.setdefault("muse", {})
        appr = m.setdefault("approval", {})
        while True:
            print("\n--- [1] 🧠 Muse 核心配置 ---")
            print(f"  [1] session_id (主会话 ID):      {m.get('session_id') or '(未设置)'}")
            print(f"  [2] cookies_file (Cookie 文件):    {m.get('cookies_file') or 'cookies.json'}")
            print(f"  [3] mint_file (Token 凭证文件):    {m.get('mint_file') or '.mint.json'}")
            print(f"  [4] host (Muse 域名):             {m.get('host') or constants.DEFAULT_MUSE_HOST}")
            print(f"  [5] client_ip (公网出口 IP):       {m.get('client_ip') or '(留空自动检测)'}")
            print(f"  [6] timezone (时区):              {m.get('timezone') or constants.DEFAULT_MUSE_TIMEZONE}")
            print(f"  [7] proxy_url (Muse 专用代理):     {m.get('proxy_url') or '(直连无代理)'}")
            print(f"  [8] approval.mode (审批决策模式):  {appr.get('mode') or 'manual'} [manual/allow_all/deny_all]")
            print(f"  [9] approval.notify (审批时通知):  {'是' if appr.get('notify', True) else '否'}")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-9)", default="0")
            if c == "0":
                break
            elif c == "1":
                curr = m.get("session_id", "")
                val = _prompt_str("主会话 Session ID (留空可随机生成或填已有会话 ID)", curr)
                m["session_id"] = val
                self._modified = True
            elif c == "2":
                m["cookies_file"] = _prompt_str("Cookie 文件保存路径", m.get("cookies_file", "cookies.json"))
                self._modified = True
            elif c == "3":
                m["mint_file"] = _prompt_str("Token 凭据文件保存路径", m.get("mint_file", ".mint.json"))
                self._modified = True
            elif c == "4":
                m["host"] = _prompt_str("Muse 服务端域名", m.get("host", constants.DEFAULT_MUSE_HOST))
                self._modified = True
            elif c == "5":
                val = _prompt_str("公网出口 IP (留空则在启动时自动探测)", m.get("client_ip", ""))
                m["client_ip"] = val
                self._modified = True
            elif c == "6":
                m["timezone"] = _prompt_str("时区", m.get("timezone", constants.DEFAULT_MUSE_TIMEZONE))
                self._modified = True
            elif c == "7":
                val = _prompt_str("代理地址 (如 http://127.0.0.1:7890，留空为直连)", m.get("proxy_url", ""))
                m["proxy_url"] = val if val else None
                self._modified = True
            elif c == "8":
                appr["mode"] = _prompt_choice("审批模式", ["manual", "allow_all", "deny_all"], appr.get("mode", "manual"))
                self._modified = True
            elif c == "9":
                appr["notify"] = _prompt_bool("审批发生时是否向聊天发送通知卡片", appr.get("notify", True))
                self._modified = True

    # ---------------- 2. Gateway 分类 ----------------
    def _edit_gateway_category(self) -> None:
        gw = self.data.setdefault("gateway", {})
        while True:
            curr = gw.get("login_through_adapter", "")
            print("\n--- [2] 🚪 默认登录通道配置 ---")
            print(f"当前 login_through_adapter: {curr or '(未设置)'}")
            print("说明：当 Cookie 失效进入登录回落时，系统会优先通过此适配器私聊管理员提示登录。")
            print("可选值: qq / onebot / 留空")
            print("  [1] 修改 login_through_adapter")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请输入选项 (0/1)", default="0")
            if c == "0":
                break
            elif c == "1":
                gw["login_through_adapter"] = _prompt_choice("请选择登录通知适配器", ["qq", "onebot", ""], curr)
                self._modified = True

    # ---------------- 3. QQ 适配器分类 ----------------
    def _edit_qq_category(self) -> None:
        adapters = self.data.setdefault("adapters", {})
        qq = adapters.setdefault("qq", {})
        acc = qq.setdefault("access_requests", {})
        wl = qq.setdefault("whitelist", {})
        while True:
            enabled = qq.get("enabled", False)
            print("\n--- [3] 🐧 QQ 官方机器人适配器 ---")
            print(f"  [1] enabled (是否启用):               {'已启用' if enabled else '已禁用'}")
            print(f"  [2] app_id (Bot AppID):              {qq.get('app_id') or '(未设置)'}")
            print(f"  [3] app_secret (Bot AppSecret):      {'******' if qq.get('app_secret') else '(未设置)'}")
            print(f"  [4] admin_id (超级管理员 OpenID 列表): {qq.get('admin_id') or []}")
            print(f"  [5] admin_only (管理指令仅限管理员私聊): {'是' if qq.get('admin_only', True) else '否'}")
            print(f"  [6] unbound_push_to_admin (未绑定消息推管理员): {'是' if qq.get('unbound_push_to_admin', True) else '否'}")
            print(f"  [7] credentials_in_place (群内填凭证): {'允许' if qq.get('credentials_in_place', True) else '禁止'}")
            print(f"  [8] allow_all_private (私聊全员免白名单): {'是' if qq.get('allow_all_private', False) else '否'}")
            print(f"  [9] allow_all_group (群聊全员免白名单):   {'是' if qq.get('allow_all_group', False) else '否'}")
            print(f"  [10] access_requests (白名单申请功能):  {'开启' if acc.get('enabled', True) else '关闭'}, 自动通过: {'是' if acc.get('auto_approve', False) else '否'}")
            print(f"  [11] whitelist.private (私聊白名单用户): {wl.get('private') or []}")
            print(f"  [12] catch_up_enabled (启动历史消息补发): {'是' if qq.get('catch_up_enabled', False) else '否'}")
            print(f"  [13] session_bindings (会话固定映射字典): {qq.get('session_bindings') or {}}")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-13)", default="0")
            if c == "0":
                break
            elif c == "1":
                qq["enabled"] = _prompt_bool("是否启用 QQ 适配器", qq.get("enabled", False))
                self._modified = True
            elif c == "2":
                qq["app_id"] = _prompt_str("QQ Bot AppID", qq.get("app_id", ""))
                self._modified = True
            elif c == "3":
                qq["app_secret"] = _prompt_str("QQ Bot AppSecret", qq.get("app_secret", ""))
                self._modified = True
            elif c == "4":
                qq["admin_id"] = _prompt_str_list("管理员 OpenID 列表", qq.get("admin_id", []))
                self._modified = True
            elif c == "5":
                qq["admin_only"] = _prompt_bool("管理指令(/switch, /new, /delete等)是否仅限管理员在私聊执行", qq.get("admin_only", True))
                self._modified = True
            elif c == "6":
                qq["unbound_push_to_admin"] = _prompt_bool("未绑定聊天的主动推送是否转交首位管理员私聊", qq.get("unbound_push_to_admin", True))
                self._modified = True
            elif c == "7":
                qq["credentials_in_place"] = _prompt_bool("是否允许在群聊中直接填写凭证卡片", qq.get("credentials_in_place", True))
                self._modified = True
            elif c == "8":
                qq["allow_all_private"] = _prompt_bool("是否开放所有私聊（无需加入白名单即可使用）", qq.get("allow_all_private", False))
                self._modified = True
            elif c == "9":
                qq["allow_all_group"] = _prompt_bool("是否开放所有群聊（无需加入白名单即可使用）", qq.get("allow_all_group", False))
                self._modified = True
            elif c == "10":
                acc["enabled"] = _prompt_bool("是否开放 /申请 白名单指令", acc.get("enabled", True))
                acc["auto_approve"] = _prompt_bool("普通用户发送 /申请 时是否自动通过", acc.get("auto_approve", False))
                self._modified = True
            elif c == "11":
                wl["private"] = _prompt_str_list("私聊白名单 OpenID 列表", wl.get("private", []))
                self._modified = True
            elif c == "12":
                qq["catch_up_enabled"] = _prompt_bool("服务启动时是否追赶补发离线期间的消息", qq.get("catch_up_enabled", False))
                self._modified = True
            elif c == "13":
                print("会话预绑定示例格式：private:OPENID_1=SESSION_ID_1, group:GID_1=SESSION_ID_2")
                curr_b = qq.get("session_bindings", {})
                print(f"当前预绑定: {curr_b}")
                raw_b = _prompt_input("输入映射(输入 clear 清空，回车保持)", default="")
                if raw_b.lower() in ("clear", "none", "{}"):
                    qq["session_bindings"] = {}
                    self._modified = True
                elif raw_b:
                    b_dict = {}
                    for item in re.split(r"[,，\s]+", raw_b):
                        if "=" in item:
                            k, _, v = item.partition("=")
                            b_dict[k.strip()] = v.strip()
                    qq["session_bindings"] = b_dict
                    self._modified = True

    # ---------------- 4. OneBot 适配器分类 ----------------
    def _edit_onebot_category(self) -> None:
        adapters = self.data.setdefault("adapters", {})
        ob = adapters.setdefault("onebot", {})
        acc = ob.setdefault("access_requests", {})
        wl = ob.setdefault("whitelist", {})
        while True:
            enabled = ob.get("enabled", False)
            print("\n--- [4] 🤖 OneBot (NapCat) 适配器 ---")
            print(f"  [1] enabled (是否启用):               {'已启用' if enabled else '已禁用'}")
            print(f"  [2] base_url (NapCat HTTP 接口地址): {ob.get('base_url') or '(未设置)'}")
            print(f"  [3] access_token (访问 Token 鉴权):   {ob.get('access_token') or '(未设置)'}")
            print(f"  [4] ws_url (正向 WebSocket 地址):     {ob.get('ws_url') or '(未设置)'}")
            print(f"  [5] group_trigger (群消息触发策略):    {ob.get('group_trigger', 'all')} [all全部/at艾特/prefix前缀]")
            print(f"  [6] admin_id (超级管理员 QQ 号列表):   {ob.get('admin_id') or []}")
            print(f"  [7] admin_only (管理指令仅限管理员私聊): {'是' if ob.get('admin_only', True) else '否'}")
            print(f"  [8] allow_all_private (私聊全员免白名单): {'是' if ob.get('allow_all_private', False) else '否'}")
            print(f"  [9] allow_all_group (群聊全员免白名单):   {'是' if ob.get('allow_all_group', False) else '否'}")
            print(f"  [10] access_requests (白名单申请功能):  {'开启' if acc.get('enabled', True) else '关闭'}, 自动通过: {'是' if acc.get('auto_approve', False) else '否'}")
            print(f"  [11] whitelist.private (私聊白名单 QQ): {wl.get('private') or []}")
            print(f"  [12] whitelist.groups (群白名单 群号):   {wl.get('groups') or []}")
            print(f"  [13] session_bindings (会话预绑定映射):   {ob.get('session_bindings') or {}}")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-13)", default="0")
            if c == "0":
                break
            elif c == "1":
                ob["enabled"] = _prompt_bool("是否启用 OneBot 适配器", ob.get("enabled", False))
                self._modified = True
            elif c == "2":
                url = _prompt_str("NapCat HTTP Base URL (如 http://127.0.0.1:3000)", ob.get("base_url", ""))
                if url and not url.startswith("http://") and not url.startswith("https://"):
                    url = "http://" + url
                ob["base_url"] = url
                if not ob.get("ws_url") and url:
                    ob["ws_url"] = _default_ws_url(url)
                self._modified = True
            elif c == "3":
                ob["access_token"] = _prompt_str("Access Token (若未启用 token 鉴权留空)", ob.get("access_token", ""))
                self._modified = True
            elif c == "4":
                ob["ws_url"] = _prompt_str("正向 WebSocket URL", ob.get("ws_url", _default_ws_url(ob.get("base_url", ""))))
                self._modified = True
            elif c == "5":
                ob["group_trigger"] = _prompt_choice("群聊响应规则", ["all", "at", "prefix"], ob.get("group_trigger", "all"))
                self._modified = True
            elif c == "6":
                ob["admin_id"] = _prompt_str_list("管理员 QQ 号列表", ob.get("admin_id", []))
                self._modified = True
            elif c == "7":
                ob["admin_only"] = _prompt_bool("管理指令是否仅限管理员在私聊执行", ob.get("admin_only", True))
                self._modified = True
            elif c == "8":
                ob["allow_all_private"] = _prompt_bool("是否开放所有私聊免白名单", ob.get("allow_all_private", False))
                self._modified = True
            elif c == "9":
                ob["allow_all_group"] = _prompt_bool("是否开放所有群聊免白名单", ob.get("allow_all_group", False))
                self._modified = True
            elif c == "10":
                acc["enabled"] = _prompt_bool("是否开放 /申请 白名单指令", acc.get("enabled", True))
                acc["auto_approve"] = _prompt_bool("发送 /申请 时是否自动通过", acc.get("auto_approve", False))
                self._modified = True
            elif c == "11":
                wl["private"] = _prompt_str_list("私聊白名单 QQ 号列表", wl.get("private", []))
                self._modified = True
            elif c == "12":
                raw_groups = _prompt_str_list("白名单开放群号列表 (数字群号)", wl.get("groups", []))
                # 尝试将数字串转为 int 保持规范
                int_groups = []
                for g in raw_groups:
                    try:
                        int_groups.append(int(g))
                    except ValueError:
                        int_groups.append(g)
                wl["groups"] = int_groups
                self._modified = True
            elif c == "13":
                curr_b = ob.get("session_bindings", {})
                print(f"当前预绑定: {curr_b}")
                raw_b = _prompt_input("输入映射(如 private:12345=SESS_1, 输入 clear 清空)", default="")
                if raw_b.lower() in ("clear", "none", "{}"):
                    ob["session_bindings"] = {}
                    self._modified = True
                elif raw_b:
                    b_dict = {}
                    for item in re.split(r"[,，\s]+", raw_b):
                        if "=" in item:
                            k, _, v = item.partition("=")
                            b_dict[k.strip()] = v.strip()
                    ob["session_bindings"] = b_dict
                    self._modified = True

    # ---------------- 5. 日志与追踪分类 ----------------
    def _edit_logging_category(self) -> None:
        log = self.data.setdefault("logging", {})
        while True:
            print("\n--- [5] 📝 日志与追踪配置 ---")
            print(f"  [1] level (日志输出级别):       {log.get('level', 'INFO')} [DEBUG/INFO/WARNING/ERROR]")
            print(f"  [2] console (是否输出到控制台):  {'是' if log.get('console', True) else '否'}")
            print(f"  [3] file (日志文件输出路径):     {log.get('file') or '(留空禁用写文件)'}")
            print(f"  [4] trace (是否开启诊断追踪):    {'开启' if log.get('trace', False) else '关闭'}")
            print(f"  [5] trace_file (追踪日志路径):   {log.get('trace_file') or 'logs/trace.jsonl'}")
            print(f"  [6] max_bytes (单日志文件大小):  {log.get('max_bytes', 10485760)} 字节 ({log.get('max_bytes', 10485760)//(1024*1024)}MB)")
            print(f"  [7] backup_count (备份轮转数):   {log.get('backup_count', 5)}")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-7)", default="0")
            if c == "0":
                break
            elif c == "1":
                log["level"] = _prompt_choice("日志等级", ["DEBUG", "INFO", "WARNING", "ERROR"], log.get("level", "INFO"))
                self._modified = True
            elif c == "2":
                log["console"] = _prompt_bool("是否输出控制台日志", log.get("console", True))
                self._modified = True
            elif c == "3":
                f_path = _prompt_str("日志文件路径 (留空不写入文件)", log.get("file", "logs/bridge.log"))
                log["file"] = f_path if f_path else None
                self._modified = True
            elif c == "4":
                log["trace"] = _prompt_bool("是否开启结构化诊断追踪 trace.jsonl", log.get("trace", False))
                self._modified = True
            elif c == "5":
                log["trace_file"] = _prompt_str("追踪日志路径", log.get("trace_file", "logs/trace.jsonl"))
                self._modified = True
            elif c == "6":
                log["max_bytes"] = _prompt_int("单日志最大字节数", log.get("max_bytes", 10485760))
                self._modified = True
            elif c == "7":
                log["backup_count"] = _prompt_int("日志文件备份保留数量", log.get("backup_count", 5))
                self._modified = True

    # ---------------- 6. 高级性能与覆盖参数 ----------------
    def _edit_overrides_category(self) -> None:
        ov = self.data.setdefault("overrides", {})
        while True:
            print("\n--- [6] ⚡ 高级性能与全局覆盖参数 ---")
            print(f"  [1] chunk_chars (长文本单段字符上限):        {ov.get('chunk_chars', constants.DEFAULT_CHUNK_CHARS)}")
            print(f"  [2] max_chunks (单条回复最多拆分段数):        {ov.get('max_chunks', constants.DEFAULT_MAX_CHUNKS)}")
            print(f"  [3] max_active_chunks (主动发送限额):       {ov.get('max_active_chunks', constants.DEFAULT_MAX_ACTIVE_CHUNKS)}")
            print(f"  [4] gateway_dedup_s (网关消息去重窗口秒数):   {ov.get('gateway_dedup_s', constants.DEFAULT_GATEWAY_DEDUP_S)}")
            print(f"  [5] download_timeout_s (文件下载超时秒数):   {ov.get('download_timeout_s', constants.DEFAULT_DOWNLOAD_TIMEOUT_S)}")
            print(f"  [6] user_agent (全局 HTTP/WS 请求 User-Agent): {ov.get('user_agent', constants.DEFAULT_USER_AGENT[:40])}...")
            print("  [0] 返回上级菜单")

            c = _prompt_input("请选择要修改的项编号 (0-6)", default="0")
            if c == "0":
                break
            elif c == "1":
                ov["chunk_chars"] = _prompt_int("长文本单段字符上限", ov.get("chunk_chars", constants.DEFAULT_CHUNK_CHARS))
                self._modified = True
            elif c == "2":
                ov["max_chunks"] = _prompt_int("单条回复最多拆分段数", ov.get("max_chunks", constants.DEFAULT_MAX_CHUNKS))
                self._modified = True
            elif c == "3":
                ov["max_active_chunks"] = _prompt_int("主动消息分段上限", ov.get("max_active_chunks", constants.DEFAULT_MAX_ACTIVE_CHUNKS))
                self._modified = True
            elif c == "4":
                ov["gateway_dedup_s"] = _prompt_int("消息防重复去重窗口时长 (秒)", ov.get("gateway_dedup_s", constants.DEFAULT_GATEWAY_DEDUP_S))
                self._modified = True
            elif c == "5":
                ov["download_timeout_s"] = _prompt_int("媒体附件下载超时时长 (秒)", ov.get("download_timeout_s", constants.DEFAULT_DOWNLOAD_TIMEOUT_S))
                self._modified = True
            elif c == "6":
                ov["user_agent"] = _prompt_str("全局 User-Agent", ov.get("user_agent", constants.DEFAULT_USER_AGENT))
                self._modified = True


# ----------------------------------------------------------------------
# 首次启动引导向导
# ----------------------------------------------------------------------

class FirstRunWizard:
    def __init__(self, output_file: str = "config.json"):
        self.output_file = output_file
        self.selected_channels: List[str] = []
        self.channel_configs: Dict[str, dict] = {}
        self.first_message_event: Optional[Tuple[str, str, str, str, Optional[str]]] = None
        self._captured_event = asyncio.Event()

    def run_interactive(self) -> None:
        print("=" * 60)
        print("     🚀 Muse IM Bridge 首次启动配置向导")
        print("=" * 60)
        print("本向导将帮助您配置聊天渠道，并通过发送一条消息")
        print("自动识别并绑定您的管理员身份与主会话。\n")

        # 1. 选择渠道
        self._select_channels()

        # 2. 依次输入选定渠道的基础凭证
        for ch in self.selected_channels:
            if ch == "qq":
                self._configure_qq()
            elif ch == "onebot":
                self._configure_onebot()

        # 3. 运行异步网关监听并捕获第一条消息
        print("\n" + "=" * 60)
        print("📡 正在启动网关监听，请准备发送消息...")
        print("=" * 60)
        asyncio.run(self._listen_for_first_message())

        # 4. 生成最小配置并写入文件
        self._generate_and_save_config()

    def _select_channels(self) -> None:
        print("请选择要启用的适配器渠道（可多选）：")
        print("  [1] QQ 官方机器人 (QQ Open Platform 官方协议)")
        print("  [2] OneBot v11 (NapCat / LLOneBot 等正向 WebSocket)")
        print("  [3] 全部启用 (QQ + OneBot)")

        while True:
            choice = _prompt_input("请输入选项编号 (1/2/3)", default="1").lower()
            if choice == "1":
                self.selected_channels = ["qq"]
                break
            elif choice == "2":
                self.selected_channels = ["onebot"]
                break
            elif choice in ("3", "all", "1,2", "1 2"):
                self.selected_channels = ["qq", "onebot"]
                break
            else:
                print("❌ 输入无效，请重新输入 1、2 或 3。")

    def _configure_qq(self) -> None:
        print("\n--- [1] 配置 QQ 官方机器人 ---")
        while True:
            app_id = _prompt_input("请输入 QQ Bot AppID").strip()
            if app_id:
                break
            print("❌ AppID 不能为空。")

        while True:
            app_secret = _prompt_input("请输入 QQ Bot AppSecret").strip()
            if app_secret:
                break
            print("❌ AppSecret 不能为空。")

        self.channel_configs["qq"] = {
            "app_id": app_id,
            "app_secret": app_secret,
        }

    def _configure_onebot(self) -> None:
        print("\n--- [2] 配置 OneBot (NapCat) ---")
        while True:
            base_url = _prompt_input("请输入 OneBot HTTP Base URL (如 http://127.0.0.1:3000)").strip()
            if base_url:
                if not base_url.startswith("http://") and not base_url.startswith("https://"):
                    base_url = "http://" + base_url
                break
            print("❌ Base URL 不能为空。")

        access_token = _prompt_input("请输入 OneBot Access Token (若未设置留空即可)", default="").strip()
        default_ws = _default_ws_url(base_url)
        ws_url = _prompt_input("请输入 OneBot 正向 WebSocket URL", default=default_ws).strip()

        self.channel_configs["onebot"] = {
            "base_url": base_url,
            "access_token": access_token,
            "ws_url": ws_url,
        }

    async def _listen_for_first_message(self) -> None:
        """启动各选定渠道的轻量网关，等待第一个发消息的用户。"""
        configure_logging(LoggingConfig(level="INFO", console=True, file=None))

        tasks = []
        gateways = []
        clients = {}

        # 准备 QQ 网关
        if "qq" in self.selected_channels:
            try:
                from muse_bridge.adapters.qq.client import QQClient
                from muse_bridge.adapters.qq.gateway import QQGateway

                qq_cfg = self.channel_configs["qq"]
                qq_client = QQClient(
                    app_id=qq_cfg["app_id"],
                    app_secret=qq_cfg["app_secret"],
                    user_agent=constants.DEFAULT_USER_AGENT,
                )
                clients["qq"] = qq_client

                print("[QQ] 正在校验 AppID 与 AppSecret 并获取 AccessToken...")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, qq_client.get_access_token)
                print("✅ [QQ] 凭证校验成功！")

                async def _on_qq_event(event_type: str, d: dict):
                    if self._captured_event.is_set():
                        return
                    if event_type in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
                        author = d.get("author") or {}
                        user_openid = author.get("user_openid") or author.get("member_openid") or author.get("id") or ""
                        group_openid = d.get("group_openid") or ""
                        msg_id = d.get("id")

                        if user_openid:
                            chat_type = "group" if group_openid else "private"
                            target_id = group_openid if group_openid else user_openid
                            self._on_message_captured("qq", str(user_openid), chat_type, str(target_id), msg_id)

                qq_gw = QQGateway(
                    client=qq_client,
                    on_event=_on_qq_event,
                    user_agent=constants.DEFAULT_USER_AGENT,
                )
                gateways.append(qq_gw)
                tasks.append(asyncio.create_task(qq_gw.start()))
            except Exception as e:
                print(f"❌ [QQ] 初始化失败: {safe_error(e)}")
                print("请检查网络连接及 AppID/AppSecret 是否正确。")
                return

        # 准备 OneBot 网关
        if "onebot" in self.selected_channels:
            try:
                from muse_bridge.adapters.onebot.client import OneBotClient
                from muse_bridge.adapters.onebot.gateway import OneBotGateway

                ob_cfg = self.channel_configs["onebot"]
                ob_client = OneBotClient(base_url=ob_cfg["base_url"], access_token=ob_cfg["access_token"])
                clients["onebot"] = ob_client

                print(f"[OneBot] 正在测试连接 NapCat API: {ob_cfg['base_url']} ...")
                loop = asyncio.get_running_loop()
                try:
                    info = await loop.run_in_executor(None, ob_client.get_login_info)
                    bot_id = str(info.get("user_id") or "")
                    nickname = info.get("nickname") or ""
                    print(f"✅ [OneBot] 连接成功！机器人账号: {bot_id} ({nickname})")
                except Exception as e:
                    bot_id = ""
                    print(f"⚠️  [OneBot] HTTP 探测未立即返回 (可能 NapCat 未启动或需 Token): {safe_error(e)}，将尝试连接 WebSocket...")

                async def _on_ob_message(event: dict):
                    if self._captured_event.is_set():
                        return
                    if event.get("post_type") == "message":
                        msg_type = event.get("message_type")  # "private" or "group"
                        user_id = str(event.get("user_id") or "")
                        if bot_id and user_id == bot_id:
                            return
                        target_id = str(event.get("group_id") if msg_type == "group" else user_id)
                        msg_id = str(event.get("message_id") or "")
                        if user_id:
                            self._on_message_captured("onebot", user_id, msg_type, target_id, msg_id)

                ob_gw = OneBotGateway(
                    ws_url=ob_cfg["ws_url"],
                    access_token=ob_cfg["access_token"],
                    on_message=_on_ob_message,
                )
                gateways.append(ob_gw)
                tasks.append(asyncio.create_task(ob_gw.start()))
            except Exception as e:
                print(f"❌ [OneBot] 初始化失败: {safe_error(e)}")
                return

        if not gateways:
            print("❌ 没有成功启动任何网关，退出向导。")
            return

        print("\n" + "*" * 60)
        print("💡 网关服务已连接就绪！")
        print("👉 请在您刚刚配置的机器人聊天中，发送任意一条消息（私聊或群聊发送）：")
        print("   - 第一个发消息的渠道将自动设为【默认登录渠道】")
        print("   - 您将自动成为【系统超级管理员】并加入白名单")
        print("   - 您的本次私聊对话将自动绑定至【主会话】")
        print("*" * 60 + "\n")

        try:
            await self._captured_event.wait()
        except asyncio.CancelledError:
            pass

        ch, user_id, chat_type, target_id, msg_id = self.first_message_event
        print(f"\n🎉 成功捕获首次消息！")
        print(f"  • 触发渠道: {ch.upper()}")
        print(f"  • 用户身份: {user_id}")
        print(f"  • 消息场景: {chat_type} (目标: {target_id})")

        reply_text = (
            "【Muse Bridge 初始化配置向导】\n"
            "🎉 身份识别成功！\n"
            f"已将您设为系统默认管理员（ID: {user_id}），\n"
            f"当前渠道已成为默认登录渠道（login_through_adapter: {ch}）。\n"
            "正在为您生成配置文件 config.json..."
        )

        try:
            loop = asyncio.get_running_loop()
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
            print("✅ 已向聊天渠道发送配置确认回复！")
        except Exception as e:
            logger.debug("发送确认回复遇到异常: %s", safe_error(e))

        print("正在停止配置监听网关...")
        for gw in gateways:
            try:
                await gw.stop()
            except Exception:
                pass
        for t in tasks:
            t.cancel()

    def _on_message_captured(self, channel: str, user_id: str, chat_type: str, target_id: str, msg_id: Optional[str]) -> None:
        if not self._captured_event.is_set():
            self.first_message_event = (channel, user_id, chat_type, target_id, msg_id)
            self._captured_event.set()

    @staticmethod
    def _save_session_map(map_file_path: str, chat_key: str, session_id: str) -> None:
        """预先将私聊会话绑定写入持久化文件。"""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(map_file_path)), exist_ok=True)
            current_map = {}
            if os.path.exists(map_file_path):
                try:
                    with open(map_file_path, "r", encoding="utf-8") as f:
                        current_map = json.load(f)
                except Exception:
                    current_map = {}
            current_map[chat_key] = session_id
            with open(map_file_path, "w", encoding="utf-8") as f:
                json.dump(current_map, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            logger.debug("Failed to write initial session map %s: %s", map_file_path, safe_error(e))

    def _generate_and_save_config(self) -> None:
        if not self.first_message_event:
            print("⚠️ 未收到任何消息，未生成配置。")
            return

        first_channel, admin_id, chat_type, target_id, _ = self.first_message_event

        main_session_id = str(uuid.uuid4())

        # 组装最小配置结构
        config_data: Dict[str, Any] = {
            "muse": {
                "cookies_file": "cookies.json",
                "mint_file": ".mint.json",
                "session_id": main_session_id
            },
            "adapters": {},
            "gateway": {
                "login_through_adapter": first_channel
            }
        }

        # 1. QQ 适配器配置生成
        if "qq" in self.selected_channels:
            qq_raw = self.channel_configs["qq"]
            is_qq_first = (first_channel == "qq")

            qq_admin_id = [admin_id] if is_qq_first else ["<ADMIN_ID>"]
            qq_whitelist: Dict[str, Any] = {
                "private": [admin_id] if is_qq_first else ["<USER_ID>"]
            }

            if is_qq_first and chat_type == "group":
                qq_whitelist[target_id] = "*"
            else:
                qq_whitelist["<GUILD_OR_GROUP_ID>"] = "*"
                qq_whitelist["<OPENID_OR_GROUP_HASH>"] = "*"

            qq_entry: Dict[str, Any] = {
                "enabled": True,
                "app_id": qq_raw["app_id"],
                "app_secret": qq_raw["app_secret"],
                "admin_id": qq_admin_id,
                "admin_only": True,
                "whitelist": qq_whitelist,
                "user_agent": constants.DEFAULT_USER_AGENT,
                "credentials_in_place": True
            }

            if is_qq_first:
                qq_entry["session_bindings"] = {f"private:{admin_id}": main_session_id}
                self._save_session_map("data/qq_session_map.json", f"private:{admin_id}", main_session_id)

            config_data["adapters"]["qq"] = qq_entry

        # 2. OneBot 适配器配置生成
        if "onebot" in self.selected_channels:
            ob_raw = self.channel_configs["onebot"]
            is_ob_first = (first_channel == "onebot")

            ob_whitelist: Dict[str, Any] = {
                "private": [admin_id] if is_ob_first else []
            }

            ob_entry: Dict[str, Any] = {
                "enabled": True,
                "base_url": ob_raw["base_url"],
                "access_token": ob_raw["access_token"],
                "ws_url": ob_raw["ws_url"],
            }

            if is_ob_first:
                ob_entry["admin_id"] = [admin_id]
                if chat_type == "group":
                    try:
                        gid_int = int(target_id)
                        ob_entry["target_group_id"] = gid_int
                        ob_whitelist["groups"] = [gid_int]
                    except ValueError:
                        ob_whitelist[target_id] = "*"
                else:
                    ob_whitelist["groups"] = []

                ob_entry["session_bindings"] = {f"private:{admin_id}": main_session_id}
                self._save_session_map("data/onebot_session_map.json", f"private:{admin_id}", main_session_id)
            else:
                ob_entry["target_group_id"] = 123456789
                ob_whitelist["groups"] = [123456789]

            ob_entry["whitelist"] = ob_whitelist
            config_data["adapters"]["onebot"] = ob_entry

        # 写入文件
        with open(self.output_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write("\n")

        print("\n" + "=" * 60)
        print(f"🎉 最小配置文件已成功生成并保存至: {self.output_file}")
        print(f"🔗 本次私聊对话已直接绑定至主会话 (Session ID: {main_session_id})")
        print("=" * 60)
        print(json.dumps(config_data, indent=2, ensure_ascii=False))
        print("=" * 60)
        print("\n接下来您可以：")
        print(f"1. 运行登录向导获取 Muse Cookie（如果尚未登录）：")
        print(f"   python main.py --login")
        print(f"2. 或者启动网桥服务在消息渠道进行登录：")
        print(f"   python main.py")
        print("=" * 60 + "\n")


# ----------------------------------------------------------------------
# 脚本主程序入口
# ----------------------------------------------------------------------

def main():
    target_file = "config.json"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target_file = sys.argv[1]

    print("=" * 60)
    print("      🛠️  Muse IM Bridge 配置管理控制台")
    print("=" * 60)

    # 1. 如果目标配置文件已存在，提供查看/分类修改/重新初始化的主菜单
    if os.path.exists(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ 读取现有配置文件 {target_file} 出错: {safe_error(e)}")
            data = {}

        print(f"检测到配置文件: {target_file}")
        print("请选择操作模式：")
        print("  [1] 交互式修改配置（按分类修改完整设置）")
        print("  [2] 首次启动向导（重新开启网关监听并自动认领管理员与绑定主会话）")
        print("  [3] 查看当前完整配置")
        print("  [0] 退出")

        choice = _prompt_input("请输入选项编号 (0/1/2/3)", default="1")
        if choice == "1":
            editor = FullConfigEditor(data, target_file)
            editor.run_menu()
            return
        elif choice == "2":
            wizard = FirstRunWizard(output_file=target_file)
            wizard.run_interactive()
            return
        elif choice == "3":
            print("\n" + "=" * 60)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            print("=" * 60)
            return
        elif choice == "0":
            print("退出配置管理。")
            return
        else:
            editor = FullConfigEditor(data, target_file)
            editor.run_menu()
            return

    # 2. 如果文件尚不存在，走首次启动向导生成最小配置
    print(f"💡 未检测到配置文件: {target_file}，即将进入首次配置向导。")
    wizard = FirstRunWizard(output_file=target_file)
    wizard.run_interactive()

    # 向导结束后询问是否进入分类修改器继续微调
    ans = _prompt_input("是否需要进一步微调全量详细配置？(y/N)", default="n").lower()
    if ans == "y":
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                new_data = json.load(f)
            editor = FullConfigEditor(new_data, target_file)
            editor.run_menu()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] 用户中断，退出。")
        sys.exit(0)
