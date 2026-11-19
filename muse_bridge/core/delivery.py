"""Platform delivery contracts. No Muse protocol or gateway state crosses this port."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeliveryStatus(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NativeQuote:
    references: tuple[tuple[str, str], ...] = ()
    excerpt: str = ""


@dataclass
class DeliveryEnvelope:
    delivery_id: str
    target_type: str
    target_id: str
    text: str = ""
    images: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    purpose: str = "proactive"  # reply uses this input's snapshot; other sends use latest
    input_id: str | None = None
    passive_id: str | None = None
    quote: NativeQuote | None = None
    quote_fallback: str = ""
    plain_text: bool = False
    fallback_target: str | None = None  # explicitly authorized by gateway policy


@dataclass(frozen=True)
class ReactionEnvelope:
    delivery_id: str
    target_type: str
    target_id: str
    message_ref: tuple[str, str]
    reactions: tuple[str, ...]  # Complete desired state, including empty to clear.
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    quote: NativeQuote | None = None
    quote_fallback: str = ""
    passive_id: str | None = None
    fallback_target: str | None = None


def reaction_as_delivery(envelope: ReactionEnvelope, *, quote: bool = False) -> DeliveryEnvelope:
    """Render a ReactionEnvelope as a plain-text message for chat platforms.

    When ``quote`` is True, keep the original message quote (or its text
    fallback). When ``quote`` is False (default), omit quoting so fallback
    reactions keep the chat list clean.
    """
    parts = []
    if envelope.added:
        parts.append(" ".join(envelope.added))
    if envelope.removed:
        parts.append(" ".join(envelope.removed))
    if not parts and envelope.reactions:
        parts.append(" ".join(envelope.reactions))
    native_quote = None
    quote_fallback = ""
    if quote:
        native_quote = envelope.quote
        if native_quote is None and envelope.message_ref and envelope.message_ref[0] and envelope.message_ref[1]:
            native_quote = NativeQuote((envelope.message_ref,))
        quote_fallback = envelope.quote_fallback
    return DeliveryEnvelope(
        delivery_id=envelope.delivery_id,
        target_type=envelope.target_type,
        target_id=envelope.target_id,
        text="\n".join(parts),
        purpose="notification",
        passive_id=envelope.passive_id,
        quote=native_quote,
        quote_fallback=quote_fallback,
        plain_text=True,
        fallback_target=envelope.fallback_target if quote else None,
    )


@dataclass
class FeedEnvelope:
    post_id: str
    text: str
    images: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass(frozen=True)
class DeliveryReceipt:
    target_type: str
    target_id: str
    references: tuple[tuple[str, str], ...] = ()

    def as_body(self) -> dict:
        values = dict(self.references)
        body = {"id": values["id"]} if "id" in values else {}
        if "ref" in values:
            body["ext_info"] = {"ref_idx": values["ref"]}
        return body

    @classmethod
    def from_body(cls, kind: str, target: str, body: Any) -> DeliveryReceipt:
        body = body if isinstance(body, dict) else {}
        info = body.get("ext_info") or {}
        values = (("id", body.get("id") or body.get("message_id")),
                  ("ref", info.get("ref_idx") if isinstance(info, dict) else None))
        return cls(kind, str(target), tuple((k, str(v)) for k, v in values if v is not None and str(v)))


@dataclass
class DeliveryResult:
    status: DeliveryStatus
    receipts: list[DeliveryReceipt] = field(default_factory=list)
    reason: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == DeliveryStatus.CONFIRMED

    def __bool__(self) -> bool:
        return self.confirmed
