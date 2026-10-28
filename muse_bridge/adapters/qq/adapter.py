"""QQ official bot adapter.

Completely decouples Inbound message reception from Outbound delivery:
1. Inbound Router: Receives QQ messages, downloads attachments, registers route,
   and fires-and-forgets to Muse immediately (takes ~0.2s without blocking).
2. Outbound Pusher: Subscribes to Muse event stream, listens for completed assistant events,
   resolves destination via RouteManager, and sends replies / proactive pushes to QQ.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from ...config.models import QQConfig
from ...core.commands import CommandError, CommandHandler
from ...core.deduplicator import MessageDeduplicator
from ...core.message import Attachment
from ...core.session_map import SessionMapper
from ...muse.service import MuseService
from ..base import BaseAdapter
from .client import QQClient
from .gateway import QQGateway
from .outbound import QQOutboundPusher
from .route_manager import RouteManager


class QQAdapter(BaseAdapter):
    def __init__(
        self,
        config: QQConfig,
        muse_service: MuseService,
        deduplicator: Optional[MessageDeduplicator] = None,
    ):
        self.config = config
        self.muse_service = muse_service
        self.client = QQClient(
            app_id=self.config.app_id,
            app_secret=self.config.app_secret,
            proxy_url=self.config.proxy_url,
            download_proxy=self.config.download_proxy,
            user_agent=self.config.user_agent,
        )

        self.deduplicator = deduplicator or MessageDeduplicator(
            persistence_file=self.config.delivered_file,
            in_flight_ttl_s=self.config.push.in_flight_ttl_s,
            processing_ttl_s=self.config.push.processing_ttl_s,
            gateway_dedup_s=self.config.gateway_dedup_s,
        )

        self.route_manager = RouteManager(ttl_s=600.0)

        self.active_session_id = CommandHandler.load_session_id(
            self.config.session_state_file,
            self.config.session_id,
        )

        # Chat -> Muse session mapping ("global": one shared session, "per_chat": isolated)
        self.session_mapper = SessionMapper(
            mode=self.config.session_mode,
            global_session_id=self.active_session_id,
            state_file=self.config.session_map_file,
            bindings=self.config.session_bindings,
            global_state_file=self.config.session_state_file,
        )

        # 1. Inbound Gateway (receives QQ events)
        self.gateway = QQGateway(
            client=self.client,
            on_event=self.handle_event,
            proxy_url=self.config.proxy_url,
            debug=self.config.debug,
            ws_guid=self.config.ws_guid,
            user_agent=self.config.user_agent,
        )

        # 2. Outbound Dispatcher (sole exit to QQ for both replies & proactive pushes)
        self.outbound_pusher = QQOutboundPusher(
            config=self.config,
            client=self.client,
            muse_service=self.muse_service,
            deduplicator=self.deduplicator,
            route_manager=self.route_manager,
            session_id=self.active_session_id,
            session_mapper=self.session_mapper,
            on_login_required=self.enter_login_mode,
        )

        self._in_login_fallback: bool = False
        self._login_sessions: Dict[Tuple[str, str, str], dict] = {}
        self._chat_send_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._running = False

    def enter_login_mode(self, admin_target: Optional[str] = None) -> None:
        self._in_login_fallback = True

    def exit_login_mode(self) -> None:
        self._in_login_fallback = False
        if self.outbound_pusher:
            self.outbound_pusher.reset_login_alert()

    async def _notify_admin_login(self, admin_target: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self.client.send_message,
                "private",
                admin_target,
                "【系统告警】向 Muse 发送消息时检测到凭据失效（401 Unauthorized）。\n已自动进入登录回落流程，请直接在此私聊发送：\n/login <您的邮箱>\n（或直接回复您的邮箱地址）即可获取验证码重新登录恢复服务 ♪",
            )
        except Exception as e:
            if self.config.debug:
                print(f"[QQAdapter] Failed to notify admin for login: {e}")

    async def _send_command_reply(
        self,
        target_type: str,
        target_id: str,
        reply: str,
        msg_id: Optional[str] = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        code, body = await loop.run_in_executor(
            None,
            self.client.send_message,
            target_type,
            target_id,
            reply,
            reply,
            None,
            msg_id,
            1 if msg_id else None,
        )
        if not (200 <= code < 300):
            # Fall back to active send so command replies are never dropped
            await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                reply,
                reply,
            )

    @property
    def name(self) -> str:
        return "qq"

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.app_id and self.config.app_secret)

    async def start(self) -> None:
        if not self.is_enabled:
            return
        self._running = True
        print(f"[QQAdapter] Starting decoupled QQ Adapter (AppID: {self.config.app_id})...")

        tasks = [
            asyncio.create_task(self.gateway.start()),
            asyncio.create_task(self.outbound_pusher.start()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        await self.gateway.stop()
        await self.outbound_pusher.stop()

    def is_allowed(self, event_type: str, d: dict) -> bool:
        """Check sender against whitelist."""
        author = d.get("author", {})
        openid = author.get("user_openid") or author.get("id") or ""
        wl = self.config.whitelist
        if not wl:
            return False

        if event_type == "C2C_MESSAGE_CREATE":
            privates = wl.get("private")
            return isinstance(privates, list) and openid in privates

        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            gid = d.get("group_openid")
            if not gid:
                return False
            allowed = wl.get(gid)
            if allowed == "*":
                return True
            return isinstance(allowed, list) and openid in allowed

        return False

    def extract_attachments(self, d: dict) -> List[Attachment]:
        attachments = []
        raw_list = d.get("attachments") or []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url:
                continue
            ctype = str(item.get("content_type") or "").lower()
            filename = item.get("filename") or ""

            if ctype.startswith("image"):
                att_type = "image"
            elif ctype.startswith("video"):
                att_type = "video"
            else:
                att_type = "file"

            attachments.append(Attachment(
                type=att_type,
                filename=filename,
                content_type=ctype,
                url=url,
            ))
        return attachments

    async def handle_event(self, event_type: str, d: dict) -> None:
        msg_id = d.get("id")
        if msg_id and self.deduplicator.is_duplicate_gateway_msg(msg_id):
            if self.config.debug:
                print(f"[QQAdapter] Duplicate gateway message ignored: {msg_id}")
            return

        if not self.is_allowed(event_type, d):
            if self.config.debug:
                print(f"[QQAdapter] Ignored event {event_type}: sender not in whitelist")
            return

        raw_content = (d.get("content") or "").strip()
        content = re.sub(r"<@!?\w+>", "", raw_content).strip()
        attachments = self.extract_attachments(d)

        if not content and not attachments:
            return

        target_type = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
        target_id = d.get("author", {}).get("user_openid") if target_type == "private" else d.get("group_openid")

        print(f"[QQAdapter] Inbound ({event_type}) from {target_type}:{target_id}: {content}")

        author = d.get("author", {})
        user_openid = author.get("user_openid") or author.get("id") or ""
        is_admin = self.config.is_admin(user_openid)
        session_key = (target_type, target_id, user_openid)

        # 0. Ongoing Login Session (both group & private supported)
        if session_key in self._login_sessions:
            session = self._login_sessions[session_key]
            if time.time() - session["timestamp"] > 300:
                del self._login_sessions[session_key]
            else:
                if content.lower() in ("/cancel", "取消", "退出"):
                    del self._login_sessions[session_key]
                    await self._send_command_reply(target_type, target_id, "【系统提示】登录流程已取消。", msg_id)
                    return

                code_match = re.search(r"^\s*([0-9]{6})\s*$", content)
                if code_match:
                    otp_code = code_match.group(1)
                    auth = session["auth"]
                    loop = asyncio.get_running_loop()
                    try:
                        await loop.run_in_executor(None, auth.confirm_otp, otp_code)
                        cookies_data = auth.extract_cookies()
                        cookies_path = self.muse_service.config.cookies_file or "cookies.json"
                        with open(cookies_path, "w", encoding="utf-8") as f:
                            json.dump(cookies_data, f, indent=2, ensure_ascii=False)
                            f.write("\n")
                        del self._login_sessions[session_key]
                        try:
                            await loop.run_in_executor(None, self.muse_service.token_manager.refresh_from_cookies)
                            self.exit_login_mode()
                            reply = f"【登录成功】已成功更新会话 Cookies ({cookies_path}) 并刷新凭据，Muse 服务已恢复正常！"
                        except Exception as refresh_err:
                            msg_str = str(refresh_err)
                            if "cannot resolve vm_id" in msg_str or "not found in page HTML" in msg_str:
                                reply = f"【登录提示】验证码校验成功，Cookies 已保存至 {cookies_path}。\n但该账号目前处于等候名单（Waitlist），尚未分配虚拟机（VM），请等待官方审核放行。"
                            else:
                                reply = f"【登录提示】Cookies 已保存，但刷新 Token 提示：{refresh_err}"
                        await self._send_command_reply(target_type, target_id, reply, msg_id)
                    except Exception as e:
                        await self._send_command_reply(
                            target_type,
                            target_id,
                            f"【登录失败】验证码确认出错：{e}\n请重新输入收到的 6 位数验证码，或发送 /cancel 取消。",
                            msg_id,
                        )
                    return
                else:
                    await self._send_command_reply(
                        target_type,
                        target_id,
                        f"【等待验证码】当前正在登录流程中（邮箱: {session['email']}）。\n请直接回复收到的 6 位数纯数字验证码，或回复 /cancel 取消。",
                        msg_id,
                    )
                    return

        # 0.1 Direct email input or login fallback check
        needs_login = self._in_login_fallback or not self.muse_service.token_manager.has_credentials()
        email_pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
        is_email = bool(re.match(email_pattern, content.strip()))

        # If admin directly sends an email in private chat (or in group if admin_only is false):
        if (is_admin or not self.config.admin_only) and is_email:
            email = content.strip()
            from ...muse.login import MuseProtocolLogin
            auth = MuseProtocolLogin(base_url="https://muse.ai")
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, auth.send_otp, email)
                self._login_sessions[session_key] = {
                    "auth": auth,
                    "email": email,
                    "timestamp": time.time(),
                }
                reply = f"【登录流程】验证码已成功发送至 {email}。\n请直接回复收到的 6 位数验证码完成登录（或发送 /cancel 取消）："
            except Exception as e:
                reply = f"【发送验证码失败】：{e}"
            await self._send_command_reply(target_type, target_id, reply, msg_id)
            return

        # If in login fallback and user message is not a command, intercept immediately with login guidance
        if needs_login and not content.startswith("/"):
            if target_type == "private" and is_admin:
                await self._send_command_reply(
                    target_type,
                    target_id,
                    "【系统提示】Muse 会话凭据已失效，当前已回落至登录流程。\n请直接回复您的登录邮箱（如 user@example.com）或发送 /login <邮箱> 获取验证码 ♪",
                    msg_id,
                )
            else:
                await self._send_command_reply(
                    target_type,
                    target_id,
                    "【系统提示】Muse 会话凭据已失效，机器人正处于登录维护状态，正在等待管理员重新认证恢复。",
                    msg_id,
                )
            return

        # 1. Local Slash Commands
        parsed = CommandHandler.parse_command(content)
        if parsed is not None:
            cmd_name, cmd_args = parsed

            # Check admin-only permission switch
            admin_cmds = {"login", "switch", "delete", "rename", "archive", "unarchive", "new"}
            if self.config.admin_only and cmd_name in admin_cmds and not is_admin:
                await self._send_command_reply(
                    target_type,
                    target_id,
                    "权限不足：管理指令仅限管理员在私聊中执行（如需在群聊中使用，可在配置中设置 admin_only: false）。",
                    msg_id,
                )
                return

            # /login command
            if cmd_name == "login":
                email = cmd_args.strip()
                if not email or "@" not in email:
                    await self._send_command_reply(target_type, target_id, "用法：/login <邮箱地址>\n示例：/login user@example.com", msg_id)
                    return
                from ...muse.login import MuseProtocolLogin
                auth = MuseProtocolLogin(base_url="https://muse.ai")
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, auth.send_otp, email)
                    self._login_sessions[session_key] = {
                        "auth": auth,
                        "email": email,
                        "timestamp": time.time(),
                    }
                    reply = f"【登录流程】验证码已成功发送至 {email}。\n请直接回复收到的 6 位数验证码完成登录（或发送 /cancel 取消）："
                except Exception as e:
                    reply = f"【发送验证码失败】：{e}"
                await self._send_command_reply(target_type, target_id, reply, msg_id)
                return

            loop = asyncio.get_running_loop()
            client = None
            # Session this chat is currently bound to (own session in per_chat mode,
            # the shared global one otherwise).
            chat_session_id = self.session_mapper.current(target_type, target_id)
            if self.session_mapper.per_chat and not chat_session_id:
                chat_session_id = "（尚未创建，发送第一条消息后自动建立）"
            try:
                if cmd_name not in ("help", "__help__", "__usage_hint__", "new"):
                    client = self.muse_service.create_client()
                    client.connect()
                # session_state_file=None: persistence is handled by SessionMapper.bind
                reply, new_sid = await loop.run_in_executor(
                    None,
                    CommandHandler.execute,
                    cmd_name,
                    cmd_args,
                    client,
                    chat_session_id,
                    None,
                )
                if new_sid != chat_session_id:
                    self.session_mapper.bind(target_type, target_id, new_sid)
                    if self.session_mapper.per_chat:
                        if cmd_name == "new":
                            self.outbound_pusher.track_session(new_sid, fresh=True)
                        else:
                            # Bound to an existing session: never replay its old history.
                            await self.outbound_pusher.ensure_baseline(new_sid)
                    else:
                        self.active_session_id = new_sid
                        self.outbound_pusher.session_id = new_sid
            except CommandError as e:
                reply = str(e)
            except Exception as e:
                err_msg = str(e).lower()
                if any(k in err_msg for k in ("401", "403", "unauthorized", "credential", "cookie", "handshake", "hatch_vm_auth")):
                    self.muse_service.token_manager.invalidate()
                    self.enter_login_mode()
                    reply = "【系统提示】执行指令时检测到 Muse 会话凭据已失效，已进入登录回落流程。\n请管理员在此私聊发送 /login <您的邮箱>（或直接回复邮箱）进行登录认证。"
                else:
                    reply = f"命令执行出错：{e}"
            finally:
                if client:
                    try:
                        client.close()
                    except Exception:
                        pass

            # Reply command output directly
            await loop.run_in_executor(
                None,
                self.client.send_message,
                target_type,
                target_id,
                reply,
                reply,
                None,
                msg_id,
            )
            return

        # 2. Sequential Inbound Ingestion (Serialized per chat so slow files are never overtaken by later text!)
        chat_key = (target_type, target_id)
        chat_lock = self._chat_send_locks.setdefault(chat_key, asyncio.Lock())

        async with chat_lock:
            loop = asyncio.get_running_loop()

            # Download attachments if any
            for att in attachments:
                if att.url and not att.data:
                    max_cap = (
                        self.config.max_video_bytes if att.type == "video"
                        else self.config.max_file_bytes if att.type == "file"
                        else self.config.max_image_bytes
                    )
                    try:
                        att.data = await loop.run_in_executor(
                            None, self.client.download_bytes, att.url, max_cap, self.config.download_timeout_s
                        )
                    except Exception as e:
                        content += f"\n[{att.type}下载失败：{e}]"

            try:
                # Resolve which Muse session this chat talks to
                session_id, created = self.session_mapper.resolve(target_type, target_id)
                if self.session_mapper.per_chat:
                    if created:
                        self.outbound_pusher.track_session(session_id, fresh=True)
                        print(f"[QQAdapter] New isolated Muse session {session_id} for {target_type}:{target_id}")
                    else:
                        # Pre-bound / restored session: baseline once so old history is never replayed
                        await self.outbound_pusher.ensure_baseline(session_id)

                # Post message to Muse in strict chronological order
                sent_mid = await loop.run_in_executor(
                    None,
                    self.muse_service.post_message,
                    session_id,
                    content,
                    attachments,
                    self.config.file_ref_threshold_bytes,
                )
                self.outbound_pusher.track_session(session_id)

                # Register route for outbound pusher
                self.route_manager.register(sent_mid, target_type, target_id, msg_id)
                if self.config.debug:
                    print(f"[QQAdapter] Message delivered to Muse (mid={sent_mid}), route registered for {target_type}:{target_id}")
            except Exception as e:
                err_msg = str(e).lower()
                if any(k in err_msg for k in ("401", "403", "unauthorized", "credential", "cookie", "handshake", "hatch_vm_auth")):
                    print(f"[QQAdapter] Credential expired while sending message to Muse: {e}")
                    self.muse_service.token_manager.invalidate()
                    self.enter_login_mode()
                    admin_target = self.config.get_default_private_target()
                    if target_type == "private" and is_admin:
                        reply = "【系统提示】发送消息时检测到 Muse 会话凭据已失效，已进入登录回落流程。\n请直接回复您的登录邮箱（如 user@example.com）以获取验证码 ♪"
                    else:
                        reply = "【系统提示】Muse 会话凭据已失效，服务已暂停并进入登录维护状态，已通知管理员重新登录。"
                        if admin_target and (target_type != "private" or target_id != admin_target):
                            loop.call_soon(
                                asyncio.create_task,
                                self._notify_admin_login(admin_target),
                            )
                    await self._send_command_reply(target_type, target_id, reply, msg_id)
                    return
                print(f"[QQAdapter] Failed to post message to Muse: {e}")
                await self._send_command_reply(target_type, target_id, f"转接出错了：{e}", msg_id)
