"""QQ input routing for owned login, credential, and approval interactions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time

from ...muse.login import MuseProtocolLogin


class QQInteractions:
    def __init__(self, adapter):
        self.adapter = adapter
        self.manager = adapter.muse_service.interactions

    async def reply(self, owner, text, msg_id, *, item=None):
        body = await self.adapter._send_command_reply(owner[0], owner[1], text, msg_id)
        if item is not None:
            self.manager.remember_credential_message(owner[0], owner[1], body, item)

    @staticmethod
    def message_references(event):
        references = []
        scene = event.get("message_scene") or {}
        entries = scene.get("ext") if isinstance(scene, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str):
                key, _, value = entry.partition("=")
                if key == "ref_msg_idx" and value:
                    references.append(("ref", value))
        reference = event.get("message_reference") or {}
        if isinstance(reference, dict) and isinstance(reference.get("message_id"), str):
            references.append(("id", reference["message_id"]))
        return references

    def pending(self, session_id):
        # Unbound chats do not implicitly acquire every other session's gate.
        return self.manager.pending_approvals(session_id) if session_id else []

    async def display_approval(self, owner, item, msg_id):
        fingerprint = item.context.get("fingerprint")
        await self.reply(owner, self.adapter.muse_service.format_approval(item), msg_id)
        item.context.setdefault("displayed_to", {})[owner[:2]] = fingerprint

    async def handle(self, owner, content, msg_id, is_admin, session_id, event=None):
        async with self.manager.owner_lock(owner):
            parts = content.split(None, 1)
            command = parts[0].lower() if parts else ""
            args = parts[1].strip() if len(parts) > 1 else ""
            current = self.manager.current(owner)
            references = self.message_references(event or {})
            referenced = self.manager.referenced_credential(owner[0], owner[1], references)
            if references and command not in {"/cancel", "/chat"}:
                if referenced is not None:
                    await self.start_credential(referenced, owner, content, msg_id, is_admin, session_id)
                    return True
                if (current and current.kind == "credential") or self.credential_candidates(owner, session_id):
                    await self.reply(owner, "未找到对应凭证，请发送 /凭证 编号。", msg_id)
                    return True
            if command == "/login":
                if owner[0] != "private" or not is_admin:
                    await self.reply(owner, "登录仅限管理员私聊。", msg_id)
                    return True
                if current and not current.terminal:
                    await self.reply(owner, "请先 /cancel 结束当前输入流程。", msg_id)
                    return True
                item = self.manager.add("login", owner=owner, state="wait_email")
                self.manager.activate(item, owner)
                if args:
                    await self._send_otp(item, args, owner, msg_id)
                else:
                    await self.reply(owner, "【登录】请输入登录邮箱；/cancel 取消。", msg_id)
                return True

            if command in {"/cancel", "取消", "退出"}:
                if current and current.kind != "approval":
                    self.manager.finish(current, "cancelled")
                    await self.reply(owner, "输入流程已结束；/chat 返回聊天。迟到的敏感输入仍会被拦截。", msg_id)
                elif self.pending(session_id):
                    await self.show_approval(owner, session_id, msg_id)
                else:
                    await self.reply(owner, "没有活动输入流程；取消不会决定任何远端审批。", msg_id)
                return True

            if command == "/chat":
                if current and not current.terminal:
                    await self.reply(owner, "请先 /cancel 结束当前输入流程。", msg_id)
                elif self.pending(session_id):
                    await self.show_approval(owner, session_id, msg_id)
                else:
                    self.manager.release(owner)
                    await self.reply(owner, "已返回聊天，请重新发送消息。", msg_id)
                return True

            if command in {"/凭证", "/credential", "/密码", "/password", "/验证码", "/code"}:
                await self.handle_credential(owner, command, args, current, session_id, msg_id, is_admin)
                return True
            if command in {"/审批", "/approval", "/approve", "/deny"}:
                await self.handle_approval(owner, command, args, session_id, msg_id, is_admin)
                return True

            if current:
                if current.terminal:
                    await self.reply(owner, "输入流程已结束或过期，此消息未转发。使用 /chat 返回聊天，或显式开始新流程。", msg_id)
                elif current.kind == "login":
                    if not is_admin or owner[0] != "private":
                        self.manager.finish(current, "cancelled")
                        await self.reply(owner, "当前用户无权继续登录。", msg_id)
                    elif current.state == "wait_email":
                        await self._send_otp(current, content, owner, msg_id)
                    elif current.state == "wait_otp":
                        await self._confirm_otp(current, content, owner, msg_id)
                    else:
                        await self.reply(owner, "正在处理登录，请稍候。", msg_id)
                elif current.kind == "credential":
                    if owner[0] != "private" and current.session_id != session_id:
                        self.manager.finish(current, "cancelled")
                        await self.reply(owner, "聊天已切换，请重新选择凭证。", msg_id)
                    else:
                        await self.credential_input(current, owner, content, msg_id, is_admin)
                return True

            if len(content.splitlines()) == 2:
                candidates = self.credential_candidates(owner, session_id)
                if candidates:
                    if len(candidates) == 1:
                        await self.start_credential(candidates[0], owner, content, msg_id, is_admin, session_id)
                    else:
                        await self.reply(owner, "请回复要填写的凭证卡，或先发送 /凭证 编号。", msg_id)
                    return True

            pending = self.pending(session_id)
            if pending:
                if content.strip().isdigit() and len(pending) == 1:
                    await self.handle_approval(owner, "/审批", f"{pending[0].id} {content.strip()}", session_id, msg_id, is_admin)
                else:
                    await self.show_approval(owner, session_id, msg_id)
                return True
            if re.fullmatch(r"[0-9]{6}", content):
                await self.reply(owner, "没有可用的验证码输入流程，此消息未转发；请先 /login。", msg_id)
                return True
            return False

    async def _send_otp(self, item, email, owner, msg_id):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            await self.reply(owner, "请输入有效邮箱；/cancel 取消。", msg_id)
            return
        auth = MuseProtocolLogin()
        item.context["auth"] = auth
        item.context["email"] = email
        item.state = "submitting"
        try:
            await asyncio.to_thread(auth.send_otp, email)
        except Exception:
            auth.client.close()
            item.context.pop("auth", None)
            item.state = "wait_email"
            await self.reply(owner, "验证码发送失败，请重试邮箱或 /cancel；未记录认证响应。", msg_id)
            return
        item.state = "wait_otp"
        item.expires_at = time.time() + 300
        await self.reply(owner, "【登录】验证码已发送，请输入六位数字；/cancel 取消。", msg_id)

    async def _confirm_otp(self, item, content, owner, msg_id):
        if not re.fullmatch(r"[0-9]{6}", content):
            await self.reply(owner, "请回复六位数字验证码；/cancel 取消。", msg_id)
            return
        item.state = "submitting"
        auth = item.context["auth"]
        try:
            await asyncio.to_thread(auth.confirm_otp, content)
            cookies = auth.extract_cookies()
            if "hatch_sess=" not in cookies.get("cookie", ""):
                raise ValueError("missing session cookie")
            path = self.adapter.muse_service.config.cookies_file or "cookies.json"
            directory = os.path.dirname(os.path.abspath(path))
            fd, temporary = tempfile.mkstemp(prefix=".cookies-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False)
                    f.write(chr(10))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except Exception:
            item.state = "wait_otp"
            await self.reply(owner, "验证码确认失败，请重试或 /cancel；输入未转发到 Muse。", msg_id)
            return
        self.manager.finish(item)
        try:
            await asyncio.to_thread(self.adapter.muse_service.token_manager.refresh_from_cookies, False)
        except Exception:
            await self.reply(owner, "验证码通过且 Cookie 已保存，但运行凭据尚未刷新成功。请检查服务配置或账号 VM 状态。", msg_id)
            return
        self.adapter.exit_login_mode()
        await self.reply(owner, "【登录成功】Cookie 与运行凭据已更新；/chat 返回聊天。", msg_id)

    async def show_approval(self, owner, session_id, msg_id, *, all_sessions=False):
        pending = self.manager.pending_approvals() if all_sessions else self.pending(session_id)
        if not pending:
            await self.reply(owner, "当前没有待处理审批。", msg_id)
        else:
            for item in pending:
                await self.display_approval(owner, item, msg_id)

    async def handle_approval(self, owner, command, args, session_id, msg_id, is_admin):
        if not is_admin:
            await self.reply(owner, "审批仅限已配置的管理员操作。", msg_id)
            return
        if not args:
            await self.show_approval(owner, session_id, msg_id, all_sessions=owner[0] == "private")
            return
        parts = args.split()
        item = self.manager.items.get(parts[0].upper())
        if item is None or item.kind != "approval" or item.terminal:
            await self.reply(owner, "该审批不存在或已结束；消息未转发。", msg_id)
            return
        # Admin private chat is the explicit account-wide control surface.
        # A group may only act on its own currently bound Muse session.
        if owner[0] != "private" and item.session_id != session_id:
            await self.reply(owner, "此审批不属于当前群绑定的 Muse 会话。", msg_id)
            return
        choice = parts[1] if len(parts) > 1 else None
        if command == "/approve":
            choice = "allow_once"
        elif command == "/deny":
            choice = "deny"
        fingerprint = item.context.get("displayed_to", {}).get(owner[:2])
        if choice is None or not fingerprint:
            await self.display_approval(owner, item, msg_id)
            return
        try:
            text = await self.adapter.muse_service.submit_approval(item, choice, expected_fingerprint=fingerprint)
        except Exception:
            text = "审批结果暂未确认，未放行、未补发任何消息，请重新查看审批。"
        await self.reply(owner, text, msg_id)
        if not item.terminal:
            # Re-display authoritative state; never reinterpret an old numeric choice.
            await self.display_approval(owner, item, msg_id)

    def credential_candidates(self, owner, session_id):
        return [item for item in list(self.manager.items.values())
                if item.kind == "credential" and not item.terminal
                and item.owner in (None, owner)
                and item.session_id in self.manager.managed_sessions
                and (owner[0] == "private" or item.session_id == session_id)]

    def can_fill_credential(self, owner, is_admin):
        return is_admin and (owner[0] == "private" or self.adapter.config.credentials_in_place)

    async def start_credential(self, item, owner, content, msg_id, is_admin, session_id, lifetime=None):
        if not self.can_fill_credential(owner, is_admin):
            text = "仅管理员可以填写凭证。" if not is_admin else "请私聊填写此凭证。"
            await self.reply(owner, text, msg_id)
            return
        if item not in self.credential_candidates(owner, session_id):
            await self.reply(owner, "此凭证已失效或不属于当前聊天。", msg_id)
            return
        current = self.manager.current(owner)
        if current and not current.terminal and current is not item:
            await self.reply(owner, "请先 /cancel 结束当前填写。", msg_id)
            return
        if item.context.get("auth_providers") or item.context.get("grant_request_type"):
            await self.reply(owner, "请在 Muse 网页完成此登录。", msg_id)
            return
        try:
            self.manager.activate(item, owner)
        except ValueError:
            await self.reply(owner, "此凭证正在由其他人填写。", msg_id)
            return
        if item.state not in {"wait_username", "wait_password", "wait_code"}:
            verification = item.context.get("capture") == "verification_code"
            item.context["lifetime"] = "on_read" if verification else (lifetime or "persist")
            item.state = "wait_code" if verification else "wait_username"
            item.expires_at = time.time() + 300
        elif lifetime is not None and lifetime != item.context.get("lifetime"):
            await self.reply(owner, "请先 /cancel，再更改保存方式。", msg_id)
            return
        if content:
            await self.credential_input(item, owner, content, msg_id, is_admin)
        else:
            prompt = "请输入验证码。" if item.state == "wait_code" else ("请输入密码。" if item.state == "wait_password" else "请发送账号和密码，每项一行；也可以先发账号。")
            await self.reply(owner, prompt, msg_id, item=item)

    async def handle_credential(self, owner, command, args, current, session_id, msg_id, is_admin):
        if not self.can_fill_credential(owner, is_admin):
            await self.reply(owner, "仅管理员可以填写凭证。" if not is_admin else "请私聊填写此凭证。", msg_id)
            return
        if command in {"/密码", "/password", "/验证码", "/code"}:
            parts = args.split(None, 1)
            item = self.manager.items.get(parts[0].upper()) if parts else None
            expected = "wait_code" if command in {"/验证码", "/code"} else "wait_password"
            if (not item or item.kind != "credential" or item.terminal or item.owner != owner
                    or item.state != expected or current is not item
                    or (owner[0] != "private" and item.session_id != session_id)):
                await self.reply(owner, "请先选择要填写的凭证。", msg_id)
                return
            await self._capture(item, owner, parts[1] if len(parts) > 1 else "", msg_id)
            return
        header, _, supplied = args.partition("\n")
        parts = header.split()
        candidates = self.credential_candidates(owner, session_id)
        item = self.manager.items.get(parts[0].upper()) if parts else (candidates[0] if len(candidates) == 1 else None)
        if item not in candidates:
            listing = chr(10).join(f"{i.id}：{i.context.get('page_url', '')}" for i in candidates)
            await self.reply(owner, (listing + chr(10) + "发送 /凭证 编号，或回复凭证卡开始填写。") if listing else "暂无待填写的凭证。", msg_id)
            return
        lifetime = parts[1] if len(parts) > 1 else None
        if lifetime not in {None, "保存", "一次", "persist", "on_read"} or len(parts) > 2:
            await self.reply(owner, "发送 /凭证 编号 [保存|一次]。", msg_id)
            return
        selected = None if lifetime is None else ("on_read" if lifetime in {"一次", "on_read"} else "persist")
        await self.start_credential(item, owner, supplied, msg_id, is_admin, session_id, selected)

    async def credential_input(self, item, owner, content, msg_id, is_admin):
        if not self.can_fill_credential(owner, is_admin):
            self.manager.finish(item, "cancelled")
            await self.reply(owner, "无权继续填写此凭证。", msg_id)
            return
        lines = content.replace("\r\n", "\n").split("\n")
        if item.state == "wait_username" or (item.state == "wait_password" and len(lines) == 2):
            if (item.context.get("submitted_digest") is not None
                    and lines[0].strip() != item.context.get("username")):
                await self.reply(owner, "请用相同内容重试，或 /cancel 取消。", msg_id, item=item)
                return
            if len(lines) > 2:
                await self.reply(owner, "请分两行发送：第一行账号，第二行密码。", msg_id, item=item)
                return
            username = lines[0].strip()
            if not username:
                await self.reply(owner, "请输入账号。", msg_id, item=item)
                return
            item.context["username"] = username
            item.state = "wait_password"
            if len(lines) == 2 and lines[1]:
                await self._capture(item, owner, lines[1], msg_id)
            else:
                await self.reply(owner, "请输入密码。", msg_id, item=item)
        elif item.state in {"wait_password", "wait_code"}:
            if len(lines) != 1:
                await self.reply(owner, "请只发送密码。" if item.state == "wait_password" else "请只发送验证码。", msg_id, item=item)
                return
            await self._capture(item, owner, content, msg_id)
        else:
            await self.reply(owner, "正在保存，请稍候。", msg_id)

    async def _capture(self, item, owner, secret, msg_id):
        if not secret:
            await self.reply(owner, "请输入验证码。" if item.state == "wait_code" else "请输入密码。", msg_id, item=item)
            return
        if item.expires_at and time.time() >= item.expires_at:
            self.manager.finish(item, "expired")
            await self.reply(owner, "填写已过期，请重新选择凭证。", msg_id)
            return
        previous = item.state
        item.state = "submitting"
        try:
            await asyncio.to_thread(self.adapter.muse_service.capture_credential, item, secret)
        except Exception:
            if not item.terminal:
                item.state = previous
            await self.reply(owner, "保存结果未确认，请用相同内容重试，或 /cancel 取消。", msg_id, item=item)
            return
        self.manager.finish(item)
        self.manager.release(owner)
        await self.reply(owner, "【凭证已保存】", msg_id)
