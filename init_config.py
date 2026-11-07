#!/usr/bin/env python3
"""Muse Bridge 首次启动配置向导 (First-run Setup Wizard)

功能流程：
1. 交互式选择并配置渠道（QQ 官方机器人 / OneBot NapCat，支持多选或单选）；
2. 启动对应渠道的网关 (Gateway) 监听入站消息；
3. 提示用户在对应渠道中发送一条消息；
4. 第一个发消息的渠道成为默认登录渠道 (`gateway.login_through_adapter`)；
5. 第一个发消息的用户自动成为默认管理员 (`admin_id`)，并加入白名单；
6. 自动发送回复确认，并生成精简最小配置保存至 `config.json`。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from muse_bridge.config import constants
from muse_bridge.observability import configure_logging, get_logger, safe_error
from muse_bridge.config.models import LoggingConfig

logger = get_logger("setup_wizard")


def _prompt_input(prompt: str, default: str = "", secret: bool = False) -> str:
    """获取用户输入，支持默认值显示。"""
    display_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        val = input(display_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[!] 配置已取消。")
        sys.exit(0)
    return val if val else default


def _default_ws_url(base_url: str) -> str:
    url = base_url.strip()
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    return f"ws://{url}"


class FirstRunWizard:
    def __init__(self, output_file: str = "config.json"):
        self.output_file = output_file
        self.selected_channels: List[str] = []
        self.channel_configs: Dict[str, dict] = {}

        # 捕获到的首条消息结果
        # (channel_name, user_id, chat_type, target_id, raw_msg_id)
        self.first_message_event: Optional[Tuple[str, str, str, str, Optional[str]]] = None
        self._captured_event = asyncio.Event()

    def run_interactive(self) -> None:
        print("=" * 60)
        print("     🚀 Muse IM Bridge 首次启动配置向导")
        print("=" * 60)
        print("本向导将帮助您快速配置聊天渠道，并通过发送一条消息")
        print("自动识别并绑定您的管理员身份与默认渠道。\n")

        if os.path.exists(self.output_file):
            print(f"⚠️  检测到已存在配置文件: {self.output_file}")
            override = _prompt_input("是否重新配置并覆盖该文件？(y/N)", default="n").lower()
            if override != "y":
                print("已退出向导，保留现有配置。")
                return

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
        ws_url = _prompt_input(f"请输入 OneBot 正向 WebSocket URL", default=default_ws).strip()

        self.channel_configs["onebot"] = {
            "base_url": base_url,
            "access_token": access_token,
            "ws_url": ws_url,
        }

    async def _listen_for_first_message(self) -> None:
        """启动各选定渠道的轻量网关，等待第一个发消息的用户。"""
        # 配置静默日志，避免刷屏干扰用户输入引导
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

                # 先测试一次 access_token 获取，以便提早发现 AppID/AppSecret 错误
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

                # 测试 HTTP 连通性
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
                        # 忽略机器人自己发的消息
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
        print("*" * 60 + "\n")

        # 等待第一条消息到达
        try:
            await self._captured_event.wait()
        except asyncio.CancelledError:
            pass

        # 捕获到了第一条消息，给用户发送成功回复反馈
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
            "正在为您生成最小配置文件 config.json..."
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

        # 停止所有网关
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

        import uuid
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

            # 如果第一条消息来自 QQ 群聊，自动将该群加入白名单
            if is_qq_first and chat_type == "group":
                qq_whitelist[target_id] = "*"
            else:
                # 保留示例占位群
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

            # 若首次消息来自 QQ，直接把该管理员私聊对话预先绑定到主会话
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

                # 若首次消息来自 OneBot，直接把该管理员私聊对话预先绑定到主会话
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


def main():
    wizard = FirstRunWizard()
    try:
        wizard.run_interactive()
    except KeyboardInterrupt:
        print("\n[!] 用户中断，向导退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
