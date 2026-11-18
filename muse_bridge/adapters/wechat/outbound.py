"""Delivery port over the existing account-scoped WeChat send ledger."""
from __future__ import annotations

import asyncio
import hashlib
import json

from ...core.delivery import DeliveryEnvelope, DeliveryReceipt, DeliveryResult, DeliveryStatus


class WeChatSenderTransport:
    """Use input snapshots only for explicit replies; other purposes use latest.

    WeChatSender owns context lookup, media preparation, reservations and send
    deduplication. This port never derives an input ID from a route or quote.
    """

    def __init__(self, config, sender):
        self.config, self.sender = config, sender
        self._locks = {}

    def _part_statuses(self, envelope, text):
        # send_messages returns (bool, ids), not a typed failure. Consult its
        # existing ledger rather than guessing that False means safe to retry.
        # Keep the order/key format identical to WeChatSender.send_messages.
        try:
            kinds = (["video"] * len(envelope.videos) + ["image"] * len(envelope.images)
                     + ["file"] * len(envelope.files) + ["text"] * len(self.sender.chunks(text)))
            return [self.sender.state.part_status(hashlib.sha256(json.dumps(
                [envelope.delivery_id, envelope.target_id, envelope.purpose, index, kind]
            ).encode()).hexdigest()) for index, kind in enumerate(kinds)]
        except (AttributeError, TypeError):
            return None

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult:
        if envelope.target_type != "private" or not envelope.target_id:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="WeChat requires a private target")
        if not envelope.delivery_id:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="WeChat requires a delivery ID")
        if envelope.purpose not in {"reply", "proactive", "notification"}:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Invalid WeChat delivery purpose")
        if envelope.purpose == "reply" and not envelope.input_id:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="WeChat reply requires an input ID")
        # iLink does not support outbound native quotes; use only the supplied
        # display fallback, never a quote ID as a context token selector.
        text = envelope.quote_fallback + envelope.text
        async with self._locks.setdefault(envelope.target_id, asyncio.Lock()):
            try:
                ok, sent_ids = await self.sender.send_messages(
                    envelope.target_id, text, images=envelope.images, files=envelope.files,
                    videos=envelope.videos, purpose=envelope.purpose,
                    msg_id=envelope.input_id if envelope.purpose == "reply" else None,
                    logical_id=envelope.delivery_id)
            except Exception:
                # The ledger still suppresses a replay if the request started.
                return DeliveryResult(DeliveryStatus.UNKNOWN, reason="WeChat delivery outcome unknown")
            receipts = [DeliveryReceipt("private", envelope.target_id, (("id", str(mid)),))
                        for mid in sent_ids if mid is not None and str(mid)]
            if ok:
                return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
            states = self._part_statuses(envelope, text)
            if states is None or any(status in {"sending", "unknown"} for status in states):
                return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, "WeChat send outcome unknown; replay suppressed")
            if receipts or "confirmed" in states:
                return DeliveryResult(DeliveryStatus.PARTIAL, receipts, "WeChat delivery partially completed")
            return DeliveryResult(DeliveryStatus.REJECTED, reason="WeChat delivery refused or not prepared")
