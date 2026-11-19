"""Gateway-only content/routing and receipt registration, detached from platform senders."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re

from ..config.models import ChatConfig
from ..core.delivery import (
    DeliveryEnvelope,
    DeliveryReceipt,
    DeliveryResult,
    DeliveryStatus,
    NativeQuote,
    ReactionEnvelope,
)
from ..core.references import MessageReferenceStore
from ..muse.interactions import credential_input_kind
from ..core.state_file import atomic_json_write
from .outbound import OutboundDispatcher, format_oversize_notice
from ..observability import get_logger

logger = get_logger(__name__)
_OVERSIZE_CLAUSE_RE = re.compile(
    r"\b(image|video|file) exceeds upload limit \((?:(\d+) > (\d+) bytes|>(\d+) bytes)\)"
)
_MEDIA_LABELS_CN = {"image": "图片", "video": "视频", "file": "文件"}


def _oversize_fallback_notices(reason: str) -> list[str]:
    if not reason:
        return []
    clauses = [part.strip() for part in reason.split(";") if part.strip()]
    if not clauses:
        return []
    notices = []
    for clause in clauses:
        match = _OVERSIZE_CLAUSE_RE.search(clause)
        if not match:
            return []
        kind, actual_s, limit_s, cap_only_s = match.groups()
        limit = int(limit_s or cap_only_s)
        actual = int(actual_s) if actual_s is not None else None
        notices.append(format_oversize_notice(_MEDIA_LABELS_CN[kind], limit, actual))
    return notices


class GatewayOutbound(OutboundDispatcher):
    def __init__(self, context):
        self.context = context
        self.platform = context.name
        super().__init__(context.config, None, context.muse_service, context.deduplicator,
                         context.route_manager, context.session_mapper, context.enter_login_mode)
        if self.platform == "onebot" and context.account_identity():
            self.reference_store = MessageReferenceStore(
                self._reference_state_file, self.reference_identity() + ":" + context.account_identity())
        self._ledger_path = str(context.config.routes_file) + ".deliveries.local.json" if context.config.routes_file else None
        self._ledger = {}
        self._origins = {}
        if self._ledger_path and os.path.exists(self._ledger_path):
            with open(self._ledger_path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or data.get("identity") != [self.platform, self.account_identity()]:
                raise ValueError("Delivery state is corrupt or belongs to another account")
            self._ledger = data["deliveries"]
            self._origins = data.get("origins", {})
        self._delivery_locks = {}

    def account_identity(self):
        return self.context.account_identity()

    def reference_identity(self):
        if self.platform == "wechat":
            return "wechat:" + self.context.account_identity()
        return super().reference_identity()

    def notification_target(self):
        return self.context.notification_target()

    async def start(self):
        raise RuntimeError("Only UnifiedGateway may run the Muse event subscription")

    def track_session(self, session_id, fresh=False):
        super().track_session(session_id, fresh)
        pump = self.context.gateway.events
        if pump is not None:
            pump.track_session(session_id, fresh)
        if not fresh:
            self.context.gateway.turn_started(self.context, session_id)

    async def on_turn_finished(self, session_id):
        await self.context.gateway.turn_finished(self.context, session_id)

    async def on_turns_cleared(self):
        for sid in self.session_mapper.session_ids():
            await self.on_turn_finished(sid)

    async def _send_notice(self, target_id, text):
        await self.context.send_notice(target_id, text)

    def remember_origin(self, sid, mid, kind, target, root=None):
        if mid and mid not in self._origins:
            self._origins[mid] = {"session": sid, "kind": kind, "target": target, "root": root}
            self._save_ledger()

    def match_origin(self, sid, mid, root):
        origin = self._origins.get(mid)
        if not origin or origin["session"] != sid:
            return None
        if root and origin.get("root") and root != origin["root"]:
            return None
        if root and not origin.get("root"):
            origin["root"] = root
            self._save_ledger()
        return origin

    def resolve_delivery_target(self, session_id, last_user_mid, root_exec_id):
        source = self.context.gateway.route_owner(session_id, last_user_mid, root_exec_id)
        if source is not None and source is not self.context:
            return "private", "", None, None
        if source is None and self.context.gateway.notification_owner(session_id) is not self.context:
            return "private", "", None, None
        origin = self.match_origin(session_id, last_user_mid, root_exec_id) if source is self.context else None
        if origin:
            target = origin["kind"], origin["target"]
            if target not in self.session_mapper.owners(session_id):
                return "private", "", None, None
            route = self.route_manager.match_route(last_user_mid, root_exec_id)
            if self.platform == "wechat" or route is None:
                return *target, None, "proactive"
        if self.platform == "wechat":
            # Business causality does not authorize an old iLink context snapshot.
            route = self.route_manager.match_route(last_user_mid, root_exec_id)
            owners = self.session_mapper.owners(session_id)
            target = ((route.target_type, route.target_id) if route and
                      (route.target_type, route.target_id) in owners else next(iter(owners), ("private", "")))
            return *target, None, "proactive"
        return super().resolve_delivery_target(session_id, last_user_mid, root_exec_id)

    def render_credential_hint(self, item):
        if isinstance(self.config, ChatConfig):
            return super().render_credential_hint(item)
        # Personal channels allow their participant to fill credentials in place.
        kind = credential_input_kind(item.context)
        if item.context.get("auth_providers") or item.context.get("grant_request_type"):
            return "请在 Muse 网页完成此登录。"
        label = "单行密钥，无需填写账号" if kind == "key" else "凭证内容（支持多行）"
        return f"可引用此消息发送{label}，或发送 /凭证 {item.id}。"

    async def _scan_and_dispatch(self, hist, session_id=None):
        last_user = None
        suppressed = False
        for event in sorted(hist, key=lambda e: e.get("seq") or 0):
            if event.get("role") == "user":
                last_user = event.get("message_id")
            elif event.get("role") == "assistant":
                root = ((event.get("payload") or {}).get("metadata") or {}).get("root_message_execution_id")
                source = self.context.gateway.route_owner(session_id, last_user, root)
                if source is not None and source is not self.context and event.get("message_id"):
                    self._seen_ids.add(event["message_id"])
                    suppressed = True
        if suppressed:
            self._save_state()
        await super()._scan_and_dispatch(hist, session_id)

    def _save_ledger(self):
        if self._ledger_path:
            atomic_json_write(self._ledger_path, {
                "version": 1, "identity": [self.platform, self.account_identity()],
                "deliveries": self._ledger, "origins": self._origins})

    async def send_envelope(self, envelope):
        lock = self._delivery_locks.setdefault((envelope.target_type, envelope.target_id), asyncio.Lock())
        async with lock:
            saved = self._ledger.get(envelope.delivery_id)
            if saved:
                receipts = [DeliveryReceipt(r["target_type"], r["target_id"], tuple(map(tuple, r["references"])))
                            for r in saved.get("receipts", [])]
                status = saved.get("status")
                if status == "confirmed":
                    return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
                if status in {"sending", "unknown", "partial"}:
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, "Previous delivery is uncertain; not replayed")
            self._ledger[envelope.delivery_id] = {"status": "sending", "receipts": []}
            self._save_ledger()  # reserve before any non-idempotent platform call
            try:
                result = await self.context.adapter.deliver(envelope)
            except BaseException:
                self._ledger[envelope.delivery_id]["status"] = "unknown"
                self._save_ledger()
                raise
            self._ledger[envelope.delivery_id] = {"status": result.status.value,
                "receipts": [{"target_type": r.target_type, "target_id": r.target_id,
                              "references": list(r.references)} for r in result.receipts]}
            self._save_ledger()
            return result

    async def send_reaction_envelope(self, envelope: ReactionEnvelope) -> DeliveryResult:
        lock = self._delivery_locks.setdefault((envelope.target_type, envelope.target_id), asyncio.Lock())
        async with lock:
            saved = self._ledger.get(envelope.delivery_id)
            if saved:
                receipts = [DeliveryReceipt(r["target_type"], r["target_id"], tuple(map(tuple, r["references"])))
                            for r in saved.get("receipts", [])]
                status = saved.get("status")
                if status == "confirmed":
                    return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
                if status in {"sending", "unknown", "partial"}:
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, "Previous delivery is uncertain; not replayed")
            self._ledger[envelope.delivery_id] = {"status": "sending", "receipts": []}
            self._save_ledger()
            try:
                result = await self.context.adapter.deliver_reaction(envelope)
            except BaseException:
                self._ledger[envelope.delivery_id]["status"] = "unknown"
                self._save_ledger()
                raise
            self._ledger[envelope.delivery_id] = {"status": result.status.value,
                "receipts": [{"target_type": r.target_type, "target_id": r.target_id,
                              "references": list(r.references)} for r in result.receipts]}
            self._save_ledger()
            return result

    async def _deliver_reaction(self, target_type, target_id, emojis, additions, removals, delivery=None):
        reference = self.quote_for(delivery, target_type, target_id)
        sid = getattr(delivery, "session_id", "")
        mid = getattr(delivery, "muse_id", "") or ""
        if self.context.adapter.supports_native_reactions() and reference is None:
            return mid in self._seen_ids
        quote = NativeQuote(tuple(reference.references), reference.excerpt) if reference else None
        revision = getattr(delivery, "revision", None)
        parts = []
        if additions:
            parts.append(" ".join(additions))
        if removals:
            parts.append(" ".join(removals))
        digest = hashlib.sha256((chr(10).join(parts) + "|" + " ".join(emojis)).encode()).hexdigest()
        key = f"muse-reaction:{self.account_identity()}:{sid}:{mid}:{revision}:{target_type}:{target_id}:{digest}"
        fallback = (self.config.admin_target()
                    if isinstance(self.config, ChatConfig) and target_type == "group" and not quote else None)
        quote_fallback = (self.quote_text(delivery, target_type, target_id)
                          if delivery and delivery.reply_to_id and not quote else "")
        message_ref = next(
            ((k, v) for k, v in reference.references if k == "id"),
            reference.references[0] if reference and reference.references else ("", ""),
        ) if reference else ("", "")
        envelope = ReactionEnvelope(
            delivery_id=key,
            target_type=target_type,
            target_id=target_id,
            message_ref=message_ref,
            reactions=tuple(emojis),
            added=tuple(additions),
            removed=tuple(removals),
            is_removal=not additions and bool(removals),
            quote=quote,
            quote_fallback=quote_fallback,
            fallback_target=fallback,
        )
        result = await self.send_reaction_envelope(envelope)
        if result.status != DeliveryStatus.CONFIRMED:
            logger.warning("Reaction delivery to %s:%s %s: %s", target_type, target_id,
                           result.status.value, result.reason or "no reason reported")
            if (self.context.adapter.supports_native_reactions()
                    and result.status == DeliveryStatus.REJECTED
                    and "(HTTP 429)" not in (result.reason or "")):
                return True
            return False
        for receipt in result.receipts:
            self.remember_delivery(delivery, receipt.target_type, receipt.target_id, receipt.as_body())
        return True

    async def _deliver(self, target_type, target_id, text, images, files, videos,
                       route=None, credential=None, delivery=None):
        reference = self.quote_for(delivery, target_type, target_id)
        quote = NativeQuote(tuple(reference.references), reference.excerpt) if reference else None
        binding = getattr(delivery, "approval_binding", None)
        event_id = (getattr(delivery, "muse_id", None) or
                    (binding[0].id + ":" + str(binding[1][0]) if binding else None) or
                    (credential.id if credential is not None else "notice"))
        sid = getattr(delivery, "session_id", "")
        revision = getattr(delivery, "revision", None)
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = f"muse:{self.account_identity()}:{sid}:{event_id}:{revision}:{target_type}:{target_id}:{digest}"
        fallback = self.config.admin_target() if isinstance(self.config, ChatConfig) and target_type == "group" and not quote else None
        envelope = DeliveryEnvelope(key, target_type, target_id, text, images, files, videos,
            purpose=getattr(delivery, "purpose", None) or "proactive",
            passive_id=route.msg_id if route else None, quote=quote,
            quote_fallback=self.quote_text(delivery, target_type, target_id) if delivery and delivery.reply_to_id and not quote else "",
            plain_text=bool(delivery and delivery.plain_text), fallback_target=fallback)
        result = await self.send_envelope(envelope)
        if result.status != DeliveryStatus.CONFIRMED:
            # Reasons are platform-authored diagnostics; never log text, URLs or media.
            logger.warning("Delivery to %s:%s %s: %s", target_type, target_id, result.status.value,
                           result.reason or "no reason reported")
        all_receipts = list(result.receipts)
        confirmed = result.confirmed
        if result.status in {DeliveryStatus.REJECTED, DeliveryStatus.PARTIAL}:
            notices = _oversize_fallback_notices(result.reason or "")
            if notices:
                notice_text = "\n".join([*([envelope.text] if not all_receipts and envelope.text else []), *notices])
                notice_envelope = DeliveryEnvelope(
                    f"{key}:oversize-notice", target_type, target_id, notice_text,
                    purpose=envelope.purpose, input_id=envelope.input_id, passive_id=envelope.passive_id,
                    quote=quote if not all_receipts else None,
                    quote_fallback=envelope.quote_fallback if not all_receipts else "",
                    plain_text=envelope.plain_text, fallback_target=fallback,
                )
                notice_result = await self.send_envelope(notice_envelope)
                if notice_result.status != DeliveryStatus.CONFIRMED:
                    logger.warning("Oversize notice to %s:%s %s: %s", target_type, target_id,
                                   notice_result.status.value, notice_result.reason or "no reason reported")
                all_receipts.extend(notice_result.receipts)
                confirmed = notice_result.confirmed
        for receipt in all_receipts:
            body = receipt.as_body()
            self.remember_delivery(delivery, receipt.target_type, receipt.target_id, body)
            if credential is not None:
                self.muse_service.interactions.remember_credential_message(
                    receipt.target_type, receipt.target_id, body, credential,
                    target=self.interaction_target(receipt.target_type, receipt.target_id))
        return confirmed
