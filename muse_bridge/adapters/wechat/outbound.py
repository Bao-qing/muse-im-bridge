"""iLink asynchronous outputs are detached sends, not guessed native replies."""
from __future__ import annotations

import hashlib

from ...gateway.outbound import OutboundDispatcher
from ...muse.interactions import credential_input_kind, credential_secret_label


class WeChatOutboundPusher(OutboundDispatcher):
    platform = "wechat"

    def __init__(self, *args, state, sender, **kwargs):
        self.state, self.sender = state, sender
        super().__init__(*args, **kwargs)

    def account_identity(self):
        return self.state.bot_id

    def reference_identity(self):
        return "wechat:" + self.state.bot_id

    def notification_target(self):
        return self.state.credentials["user_id"]

    def resolve_delivery_target(self, session_id, last_user_mid, root_exec_id):
        # Never use last_user_mid/root_exec_id to claim a causal iLink reply.
        for kind, target in self.session_mapper.owners(session_id) if session_id else []:
            if kind == "private":
                return kind, target, None, "proactive"
        return "private", "", None, "proactive"

    def render_credential_hint(self, item):
        if item.context.get("auth_providers") or item.context.get("grant_request_type"):
            return "请在 Muse 网页完成此登录。"
        kind = credential_input_kind(item.context)
        if kind == "code":
            return f"可引用此消息发送验证码，或发送 /凭证 {item.id} [保存|一次] 验证码。"
        if kind == "secret":
            return f"可引用此消息发送{credential_secret_label(item.context)}（支持多行），或发送 /凭证 {item.id} [保存|一次] 凭证。"
        return f"可引用此消息发送账号和密码（支持多行），或发送 /凭证 {item.id} [保存|一次] [凭证]。"

    async def _send_notice(self, target_id, text):
        if not await self.sender.send(target_id, text, purpose="notification"):
            raise RuntimeError("WeChat notice was not confirmed")

    async def _deliver(self, target_type, target_id, text, images, files, videos,
                       route=None, credential=None, delivery=None):
        if target_type != "private":
            return False
        purpose = getattr(delivery, "purpose", None) or "proactive"
        # A route on an approval notification is not permission to use its old token.
        if purpose == "reply":
            purpose = "proactive"
        event_id = getattr(delivery, "muse_id", "") or ""
        if purpose == "notification" and event_id:
            # Reactions mutate the same Muse message; dedup a revision, not its source.
            event_id += f":{getattr(delivery, 'revision', None)}:" + hashlib.sha256(text.encode()).hexdigest()
        session_id = getattr(delivery, "session_id", "") or ""
        if not event_id:
            binding = getattr(delivery, "approval_binding", None)
            prefix = binding[0].id if binding else (getattr(credential, "id", "") or "notice")
            event_id = prefix + ":" + hashlib.sha256(text.encode()).hexdigest()
        # Outbound iLink sendmessage does not render native quotes; drop Muse reply_to_id here,
        # while still recording sent message_id receipts for inbound quote-replies.
        ok, sent_ids = await self.sender.send_messages(
            target_id, text, images=images, files=files, videos=videos,
            purpose=purpose, logical_id=f"muse:{session_id}:{event_id}")
        for sent_id in sent_ids:
            body = {"id": sent_id}
            self.remember_delivery(delivery, target_type, target_id, body)
            if credential is not None:
                self.muse_service.interactions.remember_credential_message(
                    target_type, target_id, body, credential,
                    target=self.interaction_target(target_type, target_id))
        return ok
