"""Gateway-owned account context, not a platform base class or a running gateway.

Repositories stay physically sharded for compatibility. Only UnifiedGateway runs
business pipelines/subscriptions; adapters never inherit or inspect this context.
"""
from __future__ import annotations

import hashlib
import uuid
from ..config.models import ChatConfig
from ..core.commands import HELP_TEXT
from ..core.deduplicator import MessageDeduplicator
from ..core.delivery import DeliveryEnvelope, DeliveryStatus
from ..core.identity import AdapterIdentity
from ..core.interactions import interaction_owner
from ..core.session_map import SessionMapper
from .access import ChatAccessRequests
from .admin_activity import AdminPrivateActivity
from .interactions import ChatInteractions
from .routes import RouteManager


class ChannelContext:
    def __init__(self, adapter, gateway, *, config=None, deduplicator=None, seed_main=True):
        self.adapter = adapter
        self.seed_main = seed_main
        self.gateway = gateway
        self.config = config or adapter.config
        self.muse_service = gateway.muse_service
        self.name = adapter.name
        self.identity = AdapterIdentity(adapter.name, adapter.account_identity())
        self.deduplicator = deduplicator or MessageDeduplicator(
            self.config.delivered_file, self.config.in_flight_ttl_s,
            self.config.processing_ttl_s, self.config.gateway_dedup_s)
        self.route_manager = RouteManager(self.config.routes_file, ttl_s=600)
        self.session_mapper = SessionMapper(self.config.session_map_file, self.config.session_bindings)
        self.admin_private_activity = AdminPrivateActivity(
            str(self.config.routes_file) + ".admins.local.json" if self.config.routes_file else None)
        self._in_login_fallback = False
        from .delivery import GatewayOutbound
        self.outbound_pusher = GatewayOutbound(self)
        self.interaction_router = ChatInteractions(self)
        self.access_requests = (ChatAccessRequests(self)
                                if getattr(self.config, "access_requests", None) is not None else None)
        main = self.muse_service.config.session_id
        target = self.notification_target()
        if self.seed_main and main and target and not self.session_mapper.current("private", target):
            self.session_mapper.bind("private", target, main)
        self.muse_service.interactions.managed_sessions.update(self.session_mapper.session_ids())

    @property
    def is_enabled(self):
        return self.adapter.is_enabled

    def account_identity(self):
        return self.identity.account

    def access_identity(self):
        # Legacy grant schema. Account changes are isolated by the runtime's state namespace.
        return self.config.app_id if hasattr(self.config, "app_id") else self.name

    def notification_target(self):
        return (self.config.admin_target() if isinstance(self.config, ChatConfig)
                else self.adapter.notification_target())

    def can_manage(self, user):
        return self.access_requests is None or self.config.is_admin(user)

    def _admin_commands_only(self):
        return isinstance(self.config, ChatConfig) and self.config.admin_only

    def _login_instruction(self):
        return ("请管理员在私聊中发送 /登录 或 /登录 <邮箱> 重新登录。" if self.access_requests
                else "请发送 /登录 或 /登录 <邮箱> 重新登录。")

    def interaction_owner(self, kind, target, user):
        return interaction_owner(kind, target, user, self.name, self.account_identity())

    @staticmethod
    def message_references(event):
        return list((event or {}).get("references", ()))

    def render_command_output(self, text):
        if isinstance(self.config, ChatConfig) or text != HELP_TEXT:
            return text
        lines = []
        for line in text.splitlines():
            if line.startswith("/申请"):
                continue
            if line.startswith("/登录 "):
                line = "/登录 [邮箱] — 登录 Muse"
            elif line.startswith("/凭证 "):
                line = "/凭证 [编号] [保存|一次] — 填写凭证，也可引用凭证卡回复"
            lines.append(line)
        return "\n".join(lines)

    def can_recreate_session(self, error):
        # Personal channels retain conservative, no-replay intake semantics.
        message = str(error).lower()
        return isinstance(self.config, ChatConfig) and "status 404" in message and "session not found" in message

    async def send_reply(self, kind, target, text, msg_id=None):
        text = self.render_command_output(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        envelope = DeliveryEnvelope(
            f"input:{self.account_identity()}:{kind}:{target}:{msg_id or uuid.uuid4().hex}:{digest}", kind, target, text,
            purpose="reply" if msg_id else "notification", input_id=msg_id, passive_id=msg_id)
        result = await self.outbound_pusher.send_envelope(envelope)
        if result.status != DeliveryStatus.CONFIRMED:
            raise RuntimeError("Chat reply delivery was not confirmed")
        return result.receipts[-1].as_body() if result.receipts else {}

    async def send_command_output(self, kind, target, text, msg_id=None):
        try:
            await self.send_reply(kind, target, text, msg_id)
        except Exception:
            from ..observability import get_logger
            get_logger(__name__).warning("Command output delivery was not confirmed")

    async def send_notice(self, target, text):
        await self.send_reply("private", target, text)

    async def download_attachment(self, attachment, max_bytes):
        return await self.adapter.download_attachment(attachment, max_bytes)

    def owns_session(self, sid):
        return bool(sid and self.session_mapper.owners(sid))

    def enter_login_mode(self, admin_target=None):
        self._in_login_fallback = True

    def exit_login_mode(self):
        self._in_login_fallback = False
        self.outbound_pusher.reset_login_alert()

    async def _notify_admin_login(self, target):
        from ..muse.interactions import LOGIN_RELOGIN_NOTICE
        await self.send_notice(target, "【系统告警】" + LOGIN_RELOGIN_NOTICE)

    def latest_admin_private(self):
        if not isinstance(self.config, ChatConfig):
            return None
        identity = self.outbound_pusher.reference_identity() + ":" + self.account_identity()
        return self.admin_private_activity.latest(identity, self.config.admin_id)

    def _admin_activity_identity(self):
        return self.outbound_pusher.reference_identity() + ":" + self.account_identity()

    async def notify_interaction(self, item, text):
        return await self.outbound_pusher.notify_interaction(item, text)

    async def notify_background_approval(self, item, text, target):
        return await self.outbound_pusher.notify_background_approval(item, text, target)
