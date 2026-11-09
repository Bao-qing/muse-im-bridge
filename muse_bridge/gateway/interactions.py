"""Chat input routing for owned login, credential, and approval interactions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time

from ..muse.client.errors import LoginRequired
from ..muse.login import MuseProtocolLogin
from ..muse.interactions import credential_input_kind, credential_secret_label
from ..observability import get_logger, safe_error, trace_event


logger = get_logger(__name__)

# An expired flow intercepts one late message within this window, so a secret
# typed just after the timeout is not forwarded; later messages are chat.
LATE_INPUT_GRACE_S = 300
# "2", "2 all" or "all 2": an option number for the queued approval card.
APPROVAL_REPLY = re.compile(r"(\d{1,2})(\s*(?:all|全部))?|(?:all|全部)\s*(\d{1,2})", re.IGNORECASE)


class ChatInteractions:
    """Routes chat input for login, credential and approval flows of one adapter."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.manager = adapter.muse_service.interactions

    async def reply(self, owner, text, msg_id, *, item=None):
        body = await self.adapter.send_reply(owner[0], owner[1], text, msg_id)
        if item is not None:
            self.manager.remember_credential_message(owner[0], owner[1], body, item)

    def message_references(self, event):
        """``[(kind, value)]`` the platform event quotes; matched against sent bodies."""
        return self.adapter.message_references(event)

    def pending(self, session_id):
        # Unbound chats do not implicitly acquire every other session's gate.
        return self.manager.pending_approvals(session_id) if session_id else []

    @property
    def protocol(self):
        return self.adapter.muse_service.interaction_protocol

    def approval_queue(self, session_id):
        return self.protocol.approval_queue(session_id) if session_id else []

    def approval_targets(self, owner, session_id):
        """Sessions whose queue a bare numeric reply in this chat may answer."""
        if self.pending(session_id):
            return [session_id]
        if owner[0] != "private":
            return []
        # Admin private chat also answers queues whose current card it was shown.
        return [head.session_id for head in self.protocol.queue_heads()
                if owner[:2] in head.context.get("displayed_to", {})]

    async def display_approval(self, owner, item, msg_id, *, prefix="", items=None):
        """Send one message holding the given cards and bind later choices to it."""
        items = items or [item]
        snapshots = [self.protocol.display_snapshot(i) for i in items]
        text = "\n\n".join(self.adapter.muse_service.format_approval(i) for i in items)
        body = await self.adapter.send_reply(owner[0], owner[1], prefix + text, msg_id)
        self.protocol.remember_approval_message(owner[:2], body, items, snapshots)
        for shown, snapshot in zip(items, snapshots):
            self.protocol.mark_displayed(shown, owner[:2], snapshot)

    def is_credential_input(self, owner, content, event):
        """Keep secret whitespace and route replies past ordinary group triggers."""
        current = self.manager.current(owner)
        if current and current.kind == "credential":
            return True
        references = self.message_references(event)
        if self.manager.referenced_credential(owner[0], owner[1], references) is not None:
            return True
        session_id = self.adapter.session_mapper.current(owner[0], owner[1]) or ""
        # OneBot calls this before group triggers. Quoted approval choices must
        # reach the local router too; this does not grant approval permission.
        if APPROVAL_REPLY.fullmatch(content.strip()):
            if references and self.protocol.referenced_approvals(owner[:2], references) is not None:
                return True
            if self.approval_targets(owner, session_id) or (references and self.approval_cards(owner, session_id)):
                return True
        return bool(self.credential_candidates(owner, session_id)) and (
            bool(references) or len(content.splitlines()) >= 2)

    def approval_cards(self, owner, session_id):
        return [item for item in list(self.manager.items.values())
                if item.kind == "approval"
                and owner[:2] in item.context.get("displayed_to", {})
                and (owner[0] == "private" or item.session_id == session_id)]

    async def quoted_approval(self, owner, content, references, session_id, msg_id, is_admin):
        choice = APPROVAL_REPLY.fullmatch(content.strip())
        bindings = self.protocol.referenced_approvals(owner[:2], references)
        if not choice or (bindings is None and not self.approval_cards(owner, session_id)):
            return False
        if not is_admin:
            await self.reply(owner, "审批仅限已配置的管理员操作。", msg_id)
            return True
        if bindings is None or len(bindings) != 1:
            # Background pushes expose no sent-message receipt here. Do not
            # reinterpret an unverified/ambiguous quote as today's queue head.
            await self.show_approval(owner, session_id, msg_id, all_sessions=owner[0] == "private",
                                     prefix="无法验证所引用的审批卡，未提交决定；请按新卡重新选择。\n\n")
            return True
        key, snapshot = bindings[0]
        number = choice.group(1) or choice.group(3)
        suffix = " 全部" if choice.group(2) or choice.group(3) else ""
        await self.handle_approval(owner, "/审批", f"{key} {number}{suffix}", session_id,
                                   msg_id, is_admin, snapshot=snapshot)
        return True

    async def handle(self, owner, content, msg_id, is_admin, session_id, event=None):
        async with self.manager.owner_lock(owner):
            parts = content.split(None, 1)
            command = parts[0].lower() if parts else ""
            command = {"/login": "/登录", "/cancel": "/取消", "/chat": "/聊天",
                       "/credential": "/凭证", "/password": "/密码", "/code": "/验证码",
                       "/approval": "/审批", "/approve": "/同意", "/deny": "/拒绝"}.get(command, command)
            args = parts[1] if len(parts) > 1 else ""
            if command not in {"/凭证", "/密码", "/验证码"}:
                args = args.strip()
            current = self.manager.current(owner)
            references = self.message_references(event or {})
            referenced = self.manager.referenced_credential(owner[0], owner[1], references)
            if references and command not in {"/取消", "/聊天"}:
                if referenced is not None:
                    await self.start_credential(referenced, owner, content, msg_id, is_admin, session_id)
                    return True
                if not (current and not current.terminal) and await self.quoted_approval(
                        owner, content, references, session_id, msg_id, is_admin):
                    return True
                if (current and not current.terminal and current.kind == "credential") or self.credential_candidates(owner, session_id):
                    await self.reply(owner, "未找到对应凭证，请发送 /凭证 编号。", msg_id)
                    return True
            if command == "/登录":
                if owner[0] != "private" or not is_admin:
                    await self.reply(owner, "登录仅限管理员私聊。", msg_id)
                    return True
                if current and not current.terminal:
                    await self.reply(owner, "请先 /取消 结束当前输入流程。", msg_id)
                    return True
                item = self.manager.add("login", owner=owner, state="wait_email")
                self.manager.activate(item, owner)
                if args:
                    await self._send_otp(item, args, owner, msg_id)
                else:
                    await self.reply(owner, "【登录】请输入登录邮箱；/取消 取消。", msg_id)
                return True

            active = current is not None and not current.terminal
            # Bare words only cancel an active flow; otherwise they are chat.
            if command == "/取消" or (active and content.strip() in {"取消", "退出"}):
                if current:
                    if active:
                        self.manager.finish(current, "cancelled")
                    self.manager.release(owner)
                    await self.reply(owner, "输入流程已结束，后续消息正常转发。", msg_id)
                elif self.pending(session_id):
                    await self.show_approval(owner, session_id, msg_id)
                else:
                    await self.reply(owner, "没有活动输入流程；取消不会决定任何远端审批。", msg_id)
                return True

            if command == "/聊天":
                if current and not current.terminal:
                    await self.reply(owner, "请先 /取消 结束当前输入流程。", msg_id)
                elif self.pending(session_id):
                    await self.show_approval(owner, session_id, msg_id)
                else:
                    self.manager.release(owner)
                    await self.reply(owner, "已返回聊天，请重新发送消息。", msg_id)
                return True

            if command in {"/凭证", "/密码", "/验证码"}:
                await self.handle_credential(owner, command, args, current, session_id, msg_id, is_admin)
                return True
            if command in {"/审批", "/同意", "/拒绝"}:
                await self.handle_approval(owner, command, args, session_id, msg_id, is_admin)
                return True

            if current and current.terminal:
                self.manager.release(owner)
                if current.state == "expired" and time.time() - current.expires_at < LATE_INPUT_GRACE_S:
                    await self.reply(owner, "输入流程已过期，此消息未转发；之后的消息正常转发。", msg_id)
                    return True
                current = None

            if current:
                if current.kind == "login":
                    if not is_admin or owner[0] != "private":
                        self.manager.finish(current, "cancelled")
                        self.manager.release(owner)
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
                        self.manager.release(owner)
                        await self.reply(owner, "聊天已切换，请重新选择凭证。", msg_id)
                    else:
                        await self.credential_input(current, owner, content, msg_id, is_admin)
                return True

            # A pending credential card is an open flow: multiline input stays local.
            if len(content.splitlines()) >= 2:
                candidates = self.credential_candidates(owner, session_id)
                if candidates:
                    if len(candidates) == 1:
                        await self.start_credential(candidates[0], owner, content, msg_id, is_admin, session_id)
                    else:
                        await self.reply(owner, "请回复要填写的凭证卡，或先发送 /凭证 编号。", msg_id)
                    return True

            # A bare option number answers the card at the head of the queue.
            reply = APPROVAL_REPLY.fullmatch(content.strip())
            targets = self.approval_targets(owner, session_id) if reply else []
            if len(targets) > 1:
                await self.show_approval(owner, session_id, msg_id, all_sessions=True,
                                         prefix="多个会话有待审批，请使用 /审批 编号 <数字>。\n\n")
                return True
            if targets:
                target = targets[0]
                number = reply.group(1) or reply.group(3)
                if reply.group(2) or reply.group(3):
                    await self.handle_approval(owner, "/审批", f"all {number}", target, msg_id, is_admin)
                else:
                    head = self.approval_queue(target)[0]
                    await self.handle_approval(owner, "/审批", f"{head.id} {number}", target, msg_id, is_admin)
                return True

            # Muse refuses chat while this session has a pending approval.
            if self.pending(session_id):
                await self.show_approval(owner, session_id, msg_id)
                return True
            return False

    async def _send_otp(self, item, email, owner, msg_id):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            await self.reply(owner, "请输入有效邮箱；/取消 取消。", msg_id)
            return
        auth = MuseProtocolLogin()
        item.context["auth"] = auth
        item.context["email"] = email
        item.state = "submitting"
        try:
            await asyncio.to_thread(auth.send_otp, email)
        except Exception as exc:
            logger.warning('send_otp unavailable: %s', safe_error(exc))
            auth.client.close()
            item.context.pop("auth", None)
            item.state = "wait_email"
            await self.reply(owner, "验证码发送失败，请重试邮箱或 /取消；未记录认证响应。", msg_id)
            return
        item.state = "wait_otp"
        item.expires_at = time.time() + 300
        await self.reply(owner, "【登录】验证码已发送，请输入六位数字；/取消 取消。", msg_id)

    async def _confirm_otp(self, item, content, owner, msg_id):
        if not re.fullmatch(r"[0-9]{6}", content):
            await self.reply(owner, "请回复六位数字验证码；/取消 取消。", msg_id)
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
        except Exception as exc:
            logger.warning('confirm_otp unavailable: %s', safe_error(exc))
            item.state = "wait_otp"
            await self.reply(owner, "验证码确认失败，请重试或 /取消；输入未转发到 Muse。", msg_id)
            return
        self.manager.finish(item)
        self.manager.release(owner)
        self.adapter.exit_login_mode()
        try:
            await asyncio.to_thread(self.adapter.muse_service.token_manager.refresh_from_cookies, False)
        except LoginRequired as exc:
            logger.warning('confirm_otp unavailable: %s', safe_error(exc))
            self.adapter.enter_login_mode()
            await self.reply(owner, "登录会话未生效，请重新 /登录；输入未转发到 Muse。", msg_id)
            return
        except Exception as exc:
            logger.warning('confirm_otp unavailable: %s', safe_error(exc))
            await self.reply(owner, "Cookie 已保存，连接暂未恢复；稍后会自动重试，无需重复登录。", msg_id)
            return
        self.adapter.exit_login_mode()
        trace_event(f"{self.adapter.name}.interactions", "login.completed", interaction_id=item.id, state=item.state)
        await self.reply(owner, "【登录成功】Cookie 与运行凭据已更新，后续消息正常转发。", msg_id)

    async def show_approval(self, owner, session_id, msg_id, *, all_sessions=False, prefix=""):
        """Show only the head of each queue; the rest wait their turn."""
        heads = self.protocol.queue_heads() if all_sessions else self.approval_queue(session_id)[:1]
        if not heads:
            await self.reply(owner, prefix + "当前没有待处理审批。", msg_id)
        else:
            await self.display_approval(owner, heads[0], msg_id, prefix=prefix, items=heads)

    async def advance_queue(self, owner, session_id, msg_id, text):
        """Report a decision together with the next queued card, in one message."""
        queue = self.approval_queue(session_id)
        if not queue:
            await self.reply(owner, text + "\n审批已全部完成，可以继续聊天。", msg_id)
            return
        head = queue[0]
        # The background push then skips this chat, which already has the card.
        await self.display_approval(owner, head, msg_id, prefix=text + "\n\n")

    async def decide_all(self, owner, choice, session_id, msg_id, *, snapshot=None):
        """Apply the head card's option to every card queued when it was shown."""
        queue = self.approval_queue(session_id)
        head = queue[0] if queue else None
        shown = (snapshot[1] if snapshot is not None else
                 head.context.get("queue_shown", {}).get(owner[:2]) if head else None)
        if head and snapshot is not None and snapshot[0] != head.context.get("fingerprint"):
            await self.display_approval(owner, head, msg_id, prefix="审批内容或选项已变化，请按新卡重新选择。\n\n")
            return
        if not shown or shown[0][0] != head.id:
            if head:
                await self.display_approval(owner, head, msg_id, prefix="请先查看当前审批卡，再批量选择。\n\n")
            else:
                await self.reply(owner, "当前没有待处理审批。", msg_id)
            return
        options = head.context.get("options", [])
        index = int(choice) - 1 if choice.isdigit() else -1
        if not 0 <= index < len(options):
            await self.display_approval(owner, head, msg_id, prefix="请选择当前审批卡实际提供的选项。\n\n")
            return
        wanted = options[index]
        label = wanted["label"]
        results = []
        with self.protocol.deciding(session_id):
            for remote_key, fingerprint in shown:
                item = self.manager.items.get(remote_key)
                if item is None or item.terminal:
                    continue
                # Match by meaning, never by position: cards may order options differently.
                number = choice if item is head else next(
                    (str(n) for n, o in enumerate(item.context.get("options", []), 1)
                     if o["decision"] == wanted["decision"] and o.get("scope") == wanted.get("scope")), None)
                if number is None:
                    results.append(f"{item.id}：无相同选项，保留待审批")
                    continue
                try:
                    text = await self.adapter.muse_service.submit_approval(
                        item, number, expected_fingerprint=fingerprint)
                except Exception as exc:
                    logger.warning('decide_all unavailable: %s', safe_error(exc))
                    text = "结果暂未确认"
                status = ({"approved": "已完成（已允许）", "denied": "已完成（已拒绝）",
                           "timed_out": "已过期，未确认本次选择生效", "expired": "已过期，未确认本次选择生效"}
                          .get(item.state, f"已结束（{item.state}）") if item.terminal else text.splitlines()[0])
                results.append(f"{item.id}：{status}")
            await self.advance_queue(owner, session_id, msg_id,
                                     f"【批量审批】「{label}」\n" + "\n".join(results))

    async def handle_approval(self, owner, command, args, session_id, msg_id, is_admin, *, snapshot=None):
        if not is_admin:
            await self.reply(owner, "审批仅限已配置的管理员操作。", msg_id)
            return
        if not args:
            await self.show_approval(owner, session_id, msg_id, all_sessions=owner[0] == "private")
            return
        parts = args.split()
        if parts[0].lower() in {"all", "全部"}:
            targets = self.approval_targets(owner, session_id)
            if len(parts) != 2 or len(targets) != 1:
                text = ("当前没有待处理审批。" if not targets else
                        "多个会话有待审批，请使用 /审批 编号 <数字> 全部。" if len(targets) > 1 else
                        "发送 /审批 全部 <数字>，对当前队列全部使用该选项。")
                await self.reply(owner, text, msg_id)
                return
            await self.decide_all(owner, parts[1], targets[0], msg_id)
            return
        item = self.manager.items.get(parts[0].upper())
        if item is None or item.kind != "approval":
            await self.reply(owner, "该审批不存在或已结束；消息未转发。", msg_id)
            return
        # Admin private chat is the explicit account-wide control surface.
        # A group may only act on its own currently bound Muse session.
        if owner[0] != "private" and item.session_id != session_id:
            await self.reply(owner, "此审批不属于当前群绑定的 Muse 会话。", msg_id)
            return
        if item.terminal:
            state = {"timed_out": "已过期", "expired": "已过期", "approved": "已允许",
                     "denied": "已拒绝"}.get(item.state, item.state)
            await self.reply(owner, f"该审批已结束（{state}）；未重复提交，消息未转发。", msg_id)
            return
        choice = parts[1] if len(parts) > 1 else None
        if command == "/同意":
            choice = "allow_once"
        elif command == "/拒绝":
            choice = "deny"
        fingerprint = snapshot[0] if snapshot is not None else item.context.get("displayed_to", {}).get(owner[:2])
        if choice is None or not fingerprint:
            await self.display_approval(owner, item, msg_id)
            return
        if len(parts) == 3 and parts[2].lower() in {"all", "全部"} and item is next(iter(self.approval_queue(item.session_id)), None):
            await self.decide_all(owner, choice, item.session_id, msg_id, snapshot=snapshot)
            return
        with self.protocol.deciding(item.session_id):
            try:
                text = await self.adapter.muse_service.submit_approval(item, choice, expected_fingerprint=fingerprint)
            except Exception as exc:
                logger.warning('handle_approval unavailable: %s', safe_error(exc))
                text = "审批结果暂未确认，未放行、未补发任何消息，请重新查看审批。"
            if item.terminal:
                await self.advance_queue(owner, item.session_id, msg_id, text)
            else:
                # Re-display authoritative state; never reinterpret an old numeric choice.
                # The result's first line is the reason; the card itself follows once.
                await self.display_approval(owner, item, msg_id, prefix=text.splitlines()[0] + "\n\n")

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
            await self.reply(owner, "请先 /取消 结束当前填写。", msg_id)
            return
        if item.context.get("auth_providers") or item.context.get("grant_request_type"):
            await self.reply(owner, "请在 Muse 网页完成此登录。", msg_id)
            return
        try:
            self.manager.activate(item, owner)
        except ValueError:
            await self.reply(owner, "此凭证正在由其他人填写。", msg_id)
            return
        if item.state not in {"wait_username", "wait_password", "wait_code", "wait_secret"}:
            kind = credential_input_kind(item.context)
            item.context["lifetime"] = "on_read" if kind == "code" else (lifetime or "persist")
            item.state = {"code": "wait_code", "secret": "wait_secret", "sign_in": "wait_username"}[kind]
            item.expires_at = time.time() + 300
        elif lifetime is not None and lifetime != item.context.get("lifetime"):
            await self.reply(owner, "请先 /取消，再更改保存方式。", msg_id)
            return
        if content:
            await self.credential_input(item, owner, content, msg_id, is_admin)
        else:
            await self.reply(owner, self.credential_prompt(item), msg_id, item=item)

    async def handle_credential(self, owner, command, args, current, session_id, msg_id, is_admin):
        if not self.can_fill_credential(owner, is_admin):
            await self.reply(owner, "仅管理员可以填写凭证。" if not is_admin else "请私聊填写此凭证。", msg_id)
            return
        if command in {"/密码", "/验证码"}:
            parts = args.split(None, 1)
            item = self.manager.items.get(parts[0].upper()) if parts else None
            expected = {"wait_code"} if command in {"/验证码"} else {"wait_password", "wait_secret"}
            if (not item or item.kind != "credential" or item.terminal or item.owner != owner
                    or item.state not in expected or current is not item
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
        content = content.replace("\r\n", "\n")
        if item.state == "wait_secret":
            await self._capture(item, owner, content, msg_id)
            return
        lines = content.split("\n")
        if item.state == "wait_username" or (item.state == "wait_password" and
                len(lines) == 2):
            if (item.context.get("submitted_digest") is not None
                    and lines[0].strip() != item.context.get("username")):
                await self.reply(owner, "请用相同内容重试，或 /取消 取消。", msg_id, item=item)
                return
            username = lines[0].strip()
            if not username:
                await self.reply(owner, "请输入账号。", msg_id, item=item)
                return
            item.context["username"] = username
            item.state = "wait_password"
            if len(lines) >= 2 and any(lines[1:]):
                await self._capture(item, owner, "\n".join(lines[1:]), msg_id)
            else:
                await self.reply(owner, "请输入密码。", msg_id, item=item)
        elif item.state in {"wait_password", "wait_code"}:
            if item.state == "wait_code" and len(lines) != 1:
                await self.reply(owner, "请只发送验证码。", msg_id, item=item)
                return
            await self._capture(item, owner, content, msg_id)
        else:
            await self.reply(owner, "正在保存，请稍候。", msg_id)

    @staticmethod
    def credential_prompt(item):
        if item.state == "wait_code":
            return "请输入验证码。"
        if item.state == "wait_secret":
            return f"请发送{credential_secret_label(item.context)}；支持整段多行内容，无需填写账号。"
        if item.state == "wait_password":
            return "请输入密码。"
        return "请发送账号和密码，每项一行；也可以先发账号。"

    async def _capture(self, item, owner, secret, msg_id):
        if not secret or not secret.strip():
            await self.reply(owner, self.credential_prompt(item), msg_id, item=item)
            return
        if item.expires_at and time.time() >= item.expires_at:
            self.manager.finish(item, "expired")
            self.manager.release(owner)
            await self.reply(owner, "填写已过期，请重新选择凭证。", msg_id)
            return
        previous = item.state
        item.state = "submitting"
        try:
            await asyncio.to_thread(self.adapter.muse_service.capture_credential, item, secret)
        except Exception as exc:
            logger.warning('capture unavailable: %s', safe_error(exc))
            if not item.terminal:
                item.state = previous
            await self.reply(owner, "保存结果未确认，请用相同内容重试，或 /取消 取消。", msg_id, item=item)
            return
        self.manager.finish(item)
        self.manager.release(owner)
        await self.reply(owner, "【凭证已保存】", msg_id)
