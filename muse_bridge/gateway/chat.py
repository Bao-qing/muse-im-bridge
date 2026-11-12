"""Platform-neutral chat gateway between Muse and one chat platform.

The gateway owns everything that does not depend on the platform:
per-chat Muse sessions, deduplication, whitelist applications, slash
commands, login / credential / approval flows, and the outbound dispatcher.
A platform adapter (QQ, OneBot, ...) subclasses it and only translates:
raw platform events -> ``InboundMessage`` -> ``handle_inbound``, and the
send / download hooks below -> platform API calls.

Inbound flow (identical for every platform):
access command -> whitelist -> interaction input (OTP, passwords, approval
numbers) -> login gate -> local slash commands -> per-chat session ->
``post_message`` (returns at once) -> route registration. The reply comes
back through the outbound dispatcher, never through this call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional, Tuple

from ..adapters.base import BaseAdapter
from ..config.models import ChatConfig
from ..core.commands import CHINESE_COMMANDS, GATEWAY_COMMANDS, CommandError, CommandHandler
from ..core.deduplicator import MessageDeduplicator
from ..core.message import Attachment, InboundMessage
from ..core.interactions import interaction_owner
from ..core.session_map import SessionMapper
from ..muse.client.errors import LoginRequired, is_runtime_auth_error
from ..muse.interactions import ApprovalBlocked
from ..muse.service import MuseService
from ..observability import get_logger, safe_error, trace_event
from .access import ChatAccessRequests
from .interactions import ChatInteractions
from .outbound import OutboundDispatcher
from .routes import RouteManager

logger = get_logger(__name__)

# Commands that change which session a chat uses, or the account itself.
ADMIN_COMMANDS = frozenset({"login", "switch", "delete", "rename", "archive", "unarchive", "new"})


class ChatGateway(BaseAdapter):
    """Base of every chat adapter; see the module docstring for the split."""

    def __init__(
        self,
        config: ChatConfig,
        muse_service: MuseService,
        deduplicator: Optional[MessageDeduplicator] = None,
    ):
        self.config = config
        self.muse_service = muse_service
        self.deduplicator = deduplicator or MessageDeduplicator(
            persistence_file=self.config.delivered_file,
            in_flight_ttl_s=self.config.in_flight_ttl_s,
            processing_ttl_s=self.config.processing_ttl_s,
            gateway_dedup_s=self.config.gateway_dedup_s,
        )
        self.route_manager = RouteManager(state_file=self.config.routes_file, ttl_s=600.0)

        # Chat -> Muse session mapping: each group / private user has its own session
        self.session_mapper = SessionMapper(
            state_file=self.config.session_map_file,
            bindings=self.config.session_bindings,
        )

        # Bind configured main session to admin's private chat if not already bound
        main_sid = self.muse_service.config.session_id
        admin_target = self.config.admin_target()
        if main_sid and admin_target and not self.session_mapper.current("private", admin_target):
            self.session_mapper.bind("private", admin_target, main_sid)

        self.muse_service.interactions.managed_sessions.update(self.session_mapper.session_ids())

        # Sole exit to the chat for both replies & proactive pushes
        self.outbound_pusher = self.create_outbound()

        self._in_login_fallback: bool = False
        self.interaction_router = ChatInteractions(self)
        self.access_requests = ChatAccessRequests(self)
        self._chat_send_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------
    def create_outbound(self) -> OutboundDispatcher:
        raise NotImplementedError

    def access_identity(self) -> str:
        """Bot identity stored with whitelist grants (refuses a foreign state file)."""
        raise NotImplementedError

    def account_identity(self) -> str:
        """Stable local account namespace; platforms may override (e.g. bot_id)."""
        return self.outbound_pusher.account_identity()

    def interaction_owner(self, kind, target, user):
        return interaction_owner(kind, target, user, self.name, self.account_identity())

    def render_command_output(self, text):
        return text

    def can_recreate_session(self, error) -> bool:
        """Only a confirmed missing-session rejection permits one new-session POST.

        Platforms requiring conservative acceptance (e.g. WeChat) may return False.
        A bare 404, a missing quote, or a transport error is not confirmation.
        """
        message = str(error).lower()
        return "status 404" in message and "session not found" in message

    async def send_reply(self, target_type: str, target_id: str, text: str,
                         msg_id: Optional[str] = None) -> dict:
        """Reply in a chat; raises when it was not delivered.

        Returns the sent message body; credential prompts remember it so a
        later quote of that message selects the credential.
        """
        raise NotImplementedError

    async def send_command_output(self, target_type: str, target_id: str, text: str,
                                  msg_id: Optional[str] = None) -> None:
        """Deliver the output of a local slash command (best effort)."""
        try:
            await self.send_reply(target_type, target_id, self.render_command_output(text), msg_id)
        except Exception as e:
            logger.warning('Command reply to %s:%s failed: %s', target_type, target_id, safe_error(e))

    async def send_notice(self, target_id: str, text: str) -> None:
        """Send a system notice to a private chat (admin login alerts)."""
        await self.send_reply("private", target_id, text)

    async def download_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        raise NotImplementedError

    @staticmethod
    def message_references(event) -> List[Tuple[str, str]]:
        """``[(kind, value)]`` of messages the raw event quotes (credential replies)."""
        return []

    # ------------------------------------------------------------------
    # Engine-facing API
    # ------------------------------------------------------------------
    @property
    def is_enabled(self) -> bool:
        return bool(self.config.enabled)

    async def notify_interaction(self, item, text):
        return await self.outbound_pusher.notify_interaction(item, text)

    def owns_session(self, session_id: str) -> bool:
        """Whether a chat of this adapter is bound to ``session_id``."""
        return bool(session_id and self.session_mapper.owners(session_id))

    def enter_login_mode(self, admin_target: Optional[str] = None) -> None:
        self._in_login_fallback = True

    def exit_login_mode(self) -> None:
        self._in_login_fallback = False
        if self.outbound_pusher:
            self.outbound_pusher.reset_login_alert()

    async def _notify_admin_login(self, admin_target: str) -> None:
        try:
            await self.send_notice(
                admin_target,
                "【系统告警】向 Muse 发送消息时检测到凭据失效（401 Unauthorized）。\n已自动进入登录回落流程，请直接在此私聊发送：\n/登录 <您的邮箱>\n即可获取验证码重新登录恢复服务 ♪",
            )
        except Exception as e:
            logger.debug('Failed to notify admin for login: %s', safe_error(e))

    # ------------------------------------------------------------------
    # Inbound pipeline
    # ------------------------------------------------------------------
    async def handle_inbound(self, msg: InboundMessage) -> str:
        """Process one parsed chat message (ids already validated, mentions stripped)."""
        kind = msg.source.chat_type
        target = msg.source.target_id
        user = msg.source.sender_id
        event = msg.source.raw_event or {}
        msg_id = msg.id or None
        content = msg.text
        attachments = msg.attachments

        if self.access_requests.is_command(content):
            owner = self.interaction_owner(kind, target, user)
            async with self.muse_service.interactions.owner_lock(owner):
                current = self.muse_service.interactions.current(owner)
                if (current and not current.terminal) or self.interaction_router.message_references(event):
                    await self.send_reply(kind, target, "请先结束当前填写，再单独发送申请命令。", msg_id)
                    return "handled"
                await self.access_requests.handle(kind, target, user, content, msg_id)
            return "handled"
        if not self.access_requests.allows(kind, target, user):
            logger.debug('Ignored %s message: sender not in whitelist', kind)
            return "handled"

        if not content and not attachments and not self.interaction_router.message_references(event):
            return "handled"

        target_type, target_id = kind, target
        is_admin = self.config.is_admin(user)
        owner = self.interaction_owner(target_type, target_id, user)
        session_id = self.session_mapper.current(target_type, target_id) or ""

        # Consume sensitive input before any raw message logging.
        if await self.interaction_router.handle(owner, content, msg_id, is_admin, session_id, event=event):
            logger.info('Local interaction from %s:%s: [redacted]', target_type, target_id)
            return "handled"

        available = await asyncio.to_thread(self.muse_service.token_manager.has_credentials)
        if available and self._in_login_fallback:
            self.exit_login_mode()
        needs_login = not available
        if needs_login and not content.startswith("/"):
            text = "Muse 认证不可用，请管理员私聊发送 /登录 或 /登录 <邮箱>。"
            await self.send_reply(target_type, target_id, text, msg_id)
            return "handled"

        logger.info("Inbound platform=%s target_type=%s target_id=%s msg_id=%s attachments=%s",
                    self.name, target_type, target_id, msg_id, len(attachments))

        # 1. Local Slash Commands
        parsed = CommandHandler.parse_command(content)
        if parsed is not None:
            await self._run_command(parsed, target_type, target_id, msg_id, is_admin)
            return "handled"

        # Muse understands its native English commands, not their Chinese labels.
        parts = content.split(None, 1)
        if parts and parts[0].startswith("/"):
            native = CHINESE_COMMANDS.get(parts[0][1:])
            if native in GATEWAY_COMMANDS:
                args = parts[1] if len(parts) > 1 else ""
                if native == "usage":
                    args = {"开启": "on", "关闭": "off"}.get(args, args)
                content = "/" + native + ((" " + args) if args else "")

        trace_event(f"{self.name}.adapter", "message.received", payload={"content": content},
                    session_id=session_id, target_type=target_type, target_id=target_id,
                    msg_id=msg_id, attachment_count=len(attachments))

        # 2. Sequential Inbound Ingestion (Serialized per chat so slow files are never overtaken by later text!)
        chat_lock = self._chat_send_locks.setdefault((target_type, target_id), asyncio.Lock())
        async with chat_lock:
            references = msg.references or self.interaction_router.message_references(event)
            if kind == "group" and self.config.group_sender_info and not content.startswith("/"):
                info = {"用户ID": user}
                for label, value in (("昵称", msg.source.sender_name), ("群名片", msg.source.sender_card)):
                    if isinstance(value, str) and value.strip():
                        info[label] = value.strip()
                content = "【群消息发送者 " + json.dumps(info, ensure_ascii=False) + "】\n" + content
            return await self._forward(target_type, target_id, content, attachments, msg_id, is_admin, references, event)

    async def _run_command(self, parsed, target_type, target_id, msg_id, is_admin) -> None:
        cmd_name, cmd_args = parsed

        # Check admin-only permission switch
        if self.config.admin_only and cmd_name in ADMIN_COMMANDS and not is_admin:
            await self.send_reply(
                target_type,
                target_id,
                "权限不足：管理指令仅限管理员在私聊中执行（如需在群聊中使用，可在配置中设置 admin_only: false）。",
                msg_id,
            )
            return

        loop = asyncio.get_running_loop()
        client = None
        # Session this chat is currently bound to.
        chat_session_id = self.session_mapper.current(target_type, target_id)
        if not chat_session_id:
            chat_session_id = "（尚未创建，发送第一条消息后自动建立）"
        try:
            if cmd_name not in ("help", "__help__", "__usage_hint__", "new"):
                client = await asyncio.to_thread(self.muse_service.create_client)
                await asyncio.to_thread(client.connect)
            # Persistence is handled by SessionMapper.bind
            reply, new_sid = await loop.run_in_executor(
                None,
                CommandHandler.execute,
                cmd_name,
                cmd_args,
                client,
                chat_session_id,
                self.muse_service.config.session_id,
            )
            if new_sid != chat_session_id:
                self.session_mapper.bind(target_type, target_id, new_sid)
                if cmd_name == "new":
                    self.outbound_pusher.track_session(new_sid, fresh=True)
                else:
                    # Bound to an existing session: never replay its old history.
                    await self.outbound_pusher.ensure_baseline(new_sid)
        except CommandError as e:
            reply = str(e)
        except ApprovalBlocked as e:
            await self.send_reply(target_type, target_id, str(e), msg_id)
            return
        except Exception as e:
            if isinstance(e, LoginRequired):
                self.enter_login_mode()
                reply = "Muse Cookie 会话已失效，请管理员私聊 /登录。"
            else:
                if is_runtime_auth_error(e) and client is not None:
                    await asyncio.to_thread(self.muse_service.token_manager.invalidate_mint, client.token, client.notary_token)
                reply = "命令结果未确认，请稍后核对；不会自动重复执行。"
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

        # Reply command output directly
        await self.send_command_output(target_type, target_id, reply, msg_id)

    async def _forward(self, target_type, target_id, content, attachments, msg_id, is_admin, references=(), event=None) -> str:
        """handled: consumed; unknown: POST may have run; retryable: no POST attempted."""
        post_started = False
        session_id = ""
        loop = asyncio.get_running_loop()
        quote = {}

        own_references = [("id", msg_id)] if msg_id else []
        scene = (event or {}).get("message_scene") or {}
        entries = scene.get("ext") if isinstance(scene, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str) and entry.startswith("msg_idx="):
                own_references.append(("ref", entry.partition("=")[2]))

        # Download attachments if any
        for att in attachments:
            if (att.url or att.file_info) and not att.data:
                max_cap = (
                    self.config.max_video_bytes if att.type == "video"
                    else self.config.max_file_bytes if att.type == "file"
                    else self.config.max_image_bytes
                )
                try:
                    att.data = await self.download_attachment(att, max_cap)
                except Exception as e:
                    logger.warning('Attachment download failed: %s', safe_error(e))
                    content += f"\n[{att.type}下载失败]"

        try:
            # Bind configured main session to admin's private chat if not yet bound
            if target_type == "private" and is_admin and self.muse_service.config.session_id:
                if not self.session_mapper.current("private", target_id):
                    self.session_mapper.bind("private", target_id, self.muse_service.config.session_id)

            # Resolve which Muse session this chat talks to
            session_id, created = self.session_mapper.resolve(target_type, target_id)
            if created:
                self.outbound_pusher.track_session(session_id, fresh=True)
                logger.info('New isolated Muse session %s for %s:%s', session_id, target_type, target_id)
            else:
                # Pre-bound / restored session: baseline once so old history is never replayed
                await self.outbound_pusher.ensure_baseline(session_id)

            reference = self.outbound_pusher.reference_store.resolve_channel(
                session_id, target_type, target_id, references)
            quote = {}
            if reference:
                if reference.reply_target:
                    quote["reply_to_target"] = reference.reply_target
                else:
                    quote["in_reply_to"] = reference.muse_id
            elif references:
                content = "【引用消息未关联到当前会话】" + chr(10) + content

            # Post message to Muse in strict chronological order
            post_started = True
            sent_mid = await asyncio.to_thread(
                self.muse_service.post_message, session_id, content, attachments,
                self.config.file_ref_threshold_bytes, **quote)
            if sent_mid and msg_id:
                self.outbound_pusher.reference_store.remember(
                    session_id, target_type, target_id, sent_mid, own_references, content)
            self.outbound_pusher.track_session(session_id)

            # Register route for outbound pusher
            self.route_manager.register(sent_mid, target_type, target_id, msg_id)
            logger.debug('Message delivered to Muse (mid=%s), route registered for %s:%s', sent_mid, target_type, target_id)
            trace_event(f"{self.name}.adapter", "route.registered", session_id=session_id, sent_mid=sent_mid,
                        target_type=target_type, target_id=target_id, msg_id=msg_id)
            return "handled" if sent_mid else "unknown"
        except ApprovalBlocked as e:
            await self.send_reply(target_type, target_id, str(e), msg_id)
            return "handled"
        except Exception as e:
            if isinstance(e, LoginRequired):
                logger.warning('Cookie 会话不可用，需要管理员登录。 error=%s', safe_error(e))
                self.enter_login_mode()
                admin_target = self.config.admin_target()
                if target_type == "private" and is_admin:
                    reply = "Muse 会话凭据已失效，请发送 /登录 或 /登录 <邮箱>。"
                else:
                    reply = "Muse 会话凭据已失效，已通知管理员重新登录。"
                    if admin_target and (target_type != "private" or target_id != admin_target):
                        loop.call_soon(asyncio.create_task, self._notify_admin_login(admin_target))
                await self.send_reply(target_type, target_id, reply, msg_id)
                return "unknown" if post_started else "retryable"
            if post_started and not quote and self.can_recreate_session(e):
                # Preserve legacy recovery only for an explicit session rejection.
                # A failed recovery POST is never replayed again.
                try:
                    new_sid = self.session_mapper.new_session(target_type, target_id)
                    self.outbound_pusher.track_session(new_sid, fresh=True)
                    sent_mid = await asyncio.to_thread(
                        self.muse_service.post_message, new_sid, content, attachments,
                        self.config.file_ref_threshold_bytes)
                    if sent_mid and msg_id:
                        self.outbound_pusher.reference_store.remember(
                            new_sid, target_type, target_id, sent_mid, own_references, content)
                    self.outbound_pusher.track_session(new_sid)
                    self.route_manager.register(sent_mid, target_type, target_id, msg_id)
                    return "handled" if sent_mid else "unknown"
                except Exception as recovery_error:
                    logger.error('Missing-session recovery unconfirmed: %s', safe_error(recovery_error))
                    await self.send_command_output(target_type, target_id,
                        "会话重建后发送结果未确认；未自动重发，请稍后核对。", msg_id)
                    return "unknown"
            # Transport failures and unclassified errors never replay user input.
            logger.error('Failed to post message to Muse: %s', safe_error(e))
            await self.send_reply(target_type, target_id,
                "连接暂不可用，消息结果未确认；未自动重发，请稍后核对。" if post_started
                else "连接暂不可用，消息尚未发送，请稍后重试。", msg_id)
            return "unknown" if post_started else "retryable"
