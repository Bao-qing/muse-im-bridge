"""Unified message abstractions and markdown-aware text splitters."""

from __future__ import annotations

import dataclasses
import re
from typing import Any, List, Optional

FENCE_RE = re.compile(r"^([`~]{3,})(.*)$")


@dataclasses.dataclass
class Attachment:
    type: str  # "image", "file", "video"
    filename: str = ""
    content_type: str = ""
    url: Optional[str] = None
    data: Optional[bytes] = None
    file_info: Optional[str] = None  # Platform-specific token / id after upload
    workspace_path: Optional[str] = None


@dataclasses.dataclass
class MessageSource:
    platform: str  # "qq", "onebot", "wechat"
    chat_type: str  # "private", "group"
    target_id: str  # For private: user openid/id; For group: group openid/id
    sender_id: str = ""
    sender_name: str = ""
    raw_event: Optional[Any] = None
    sender_card: str = ""

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


@dataclasses.dataclass
class InboundMessage:
    id: str
    source: MessageSource
    text: str = ""
    attachments: List[Attachment] = dataclasses.field(default_factory=list)
    timestamp: float = 0.0
    references: List[tuple[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class OutboundMessage:
    text: str = ""
    attachments: List[Attachment] = dataclasses.field(default_factory=list)
    reply_to_id: Optional[str] = None
    msg_seq: int = 1


@dataclasses.dataclass(frozen=True)
class DeliveryReference:
    session_id: str
    muse_id: str
    excerpt: str = ""
    reply_to_id: Optional[str] = None
    reply_target: Optional[dict] = None
    plain_text: bool = False
    approval_binding: Optional[Any] = None
    purpose: Optional[str] = None


def _fence_open(line: str) -> Optional[dict]:
    m = FENCE_RE.match(line)
    if not m:
        return None
    return {
        "open": line,
        "close": m.group(1),
        "char": m.group(1)[0],
        "width": len(m.group(1)),
    }


def _is_fence_close(line: str, fence: dict) -> bool:
    m = FENCE_RE.match(line)
    return bool(
        m
        and m.group(1)[0] == fence["char"]
        and len(m.group(1)) >= fence["width"]
        and not m.group(2).strip()
    )


def split_markdown(text: str, limit: int) -> List[str]:
    """Split text into chunks of at most `limit` characters without breaking code fences."""
    if not text:
        return []
    chunks: List[str] = []
    cur: List[str] = []
    fence: Optional[dict] = None

    def flush():
        nonlocal cur
        if not cur:
            return
        out = cur + [fence["close"]] if fence else cur
        chunks.append("\n".join(out))
        cur = [fence["open"]] if fence else []

    for raw in text.split("\n"):
        line_len = len(raw)
        if fence is not None:
            if _is_fence_close(raw, fence):
                cur.append(raw)
                fence = None
                continue
        else:
            opened = _fence_open(raw)
            if opened:
                fence = opened

        overhead = len(fence["close"]) + 1 if fence else 0
        current_len = sum(len(l) for l in cur) + len(cur)
        if current_len + line_len + overhead > limit and cur:
            flush()

        if len(raw) + overhead > limit:
            sub_limit = max(10, limit - overhead)
            start = 0
            while start < len(raw):
                part = raw[start : start + sub_limit]
                cur.append(part)
                if start + sub_limit < len(raw):
                    flush()
                start += sub_limit
        else:
            cur.append(raw)

    if cur:
        flush()

    return chunks or [text]


def chunk_text(text: str, chunk_chars: int, max_chunks: int) -> List[str]:
    """Split markdown text and cap total chunks, appending a truncation notice if needed."""
    parts = split_markdown(text, chunk_chars)
    if len(parts) <= max_chunks:
        return parts
    kept = parts[:max_chunks]
    notice = f"\n\n[内容已截断，仅展示前 {max_chunks} 段]"
    kept[-1] = kept[-1][: max(0, chunk_chars - len(notice))] + notice
    return kept
