"""Local interaction lifecycle, independent of IM and Muse transport."""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import threading
import time
from typing import Any, Optional


def interaction_owner(kind, target, user, platform="", account=""):
    """Keep legacy chat/user positions; append a platform/account namespace."""
    return (kind, target, user, str(platform), str(account)) if platform else (kind, target, user)


def interaction_target(owner):
    """Extract a target from either a legacy or namespaced owner tuple."""
    return (*owner[:2], *owner[3:])


@dataclasses.dataclass
class Interaction:
    id: str
    kind: str
    state: str
    owner: Optional[tuple[str, ...]] = None
    session_id: str = ""
    remote_id: str = ""
    expires_at: float = 0
    context: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state in {"completed", "cancelled", "expired", "approved", "denied", "timed_out"}


class InteractionManager:
    """Keep pending work separate from input focus and approval gates.

    Contexts (including temporary secrets) stay in memory, never on disk.
    Expired focus remains a tombstone until an explicit new interaction or
    /chat command, so a late secret cannot fall through into model chat.
    """

    def __init__(self):
        self.items: dict[str, Interaction] = {}
        self.focus: dict[tuple[str, ...], str] = {}
        self._remote: dict[tuple[str, str], str] = {}
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._guard = threading.RLock()
        self.managed_sessions: set[str] = set()
        self.approvals_ready = False
        self.credential_messages: dict[tuple, str] = {}
        self._namespaces: dict[object, tuple[str, str]] = {}

    def register_namespace(self, platform, account, *, owner=None):
        namespace = (str(platform), str(account))
        self._namespaces[owner if owner is not None else namespace] = namespace

    def target_key(self, target):
        if len(target) > 2:
            return tuple(target)
        namespace = ()
        # Legacy callers remain usable with one gateway, but never guess an
        # account in a shared service. Explicit keys always stay isolated.
        namespaces = set(self._namespaces.values())
        if not namespace and len(namespaces) == 1:
            namespace = next(iter(namespaces))
        return (*target, *namespace)

    def owner_key(self, owner):
        target = self.target_key(interaction_target(owner))
        return (*owner[:3], *target[2:])

    def remember_credential_message(self, target_type, target_id, body, item, *, target=None):
        if not isinstance(body, dict) or item.kind != "credential":
            return
        info = body.get("ext_info") or {}
        references = {"id": body.get("id"),
                      "ref": info.get("ref_idx") if isinstance(info, dict) else None}
        target = self.target_key(target or (target_type, target_id))
        with self._guard:
            for kind, value in references.items():
                if isinstance(value, str) and value:
                    self.credential_messages[(*target, kind, value)] = item.id
            while len(self.credential_messages) > 4096:
                self.credential_messages.pop(next(iter(self.credential_messages)))

    def referenced_credential(self, target_type, target_id, references, *, target=None):
        target = self.target_key(target or (target_type, target_id))
        with self._guard:
            keys = {self.credential_messages.get((*target, kind, value))
                    for kind, value in references}
            return self.items.get(next(iter(keys))) if len(keys) == 1 and None not in keys else None

    def owner_lock(self, owner: tuple[str, ...]) -> asyncio.Lock:
        return self._locks.setdefault(self.owner_key(owner), asyncio.Lock())

    def add(self, kind: str, *, owner=None, session_id="", remote_id="",
            state="available", ttl=300, context=None) -> Interaction:
        if owner is not None:
            owner = self.owner_key(owner)
        with self._guard:
            if remote_id:
                existing = self.by_remote(kind, remote_id)
                if existing is not None:
                    return existing
            prefix = {"login": "L", "credential": "C", "approval": "A"}[kind]
            while True:
                key = prefix + secrets.token_hex(3).upper()
                if key not in self.items:
                    break
            item = Interaction(key, kind, state, owner, session_id, remote_id,
                               time.time() + ttl if ttl else 0, context or {})
            self.items[key] = item
            if remote_id:
                self._remote[(kind, remote_id)] = key
            if session_id:
                self.managed_sessions.add(session_id)
            return item

    def by_remote(self, kind: str, remote_id: str) -> Optional[Interaction]:
        key = self._remote.get((kind, remote_id))
        return self.items.get(key) if key else None

    def activate(self, item: Interaction, owner: tuple[str, ...]) -> None:
        owner = self.owner_key(owner)
        with self._guard:
            current = self.current(owner)
            if current and not current.terminal and current.id != item.id:
                raise ValueError("请先结束当前输入流程。")
            if item.owner is not None and item.owner != owner:
                raise ValueError("此交互不属于当前用户。")
            item.owner = owner
            self.focus[owner] = item.id

    def current(self, owner: tuple[str, ...]) -> Optional[Interaction]:
        with self._guard:
            key = self.focus.get(self.owner_key(owner))
            item = self.items.get(key) if key else None
            if item and not item.terminal and item.expires_at and time.time() >= item.expires_at:
                self.finish(item, "expired")
            return item

    def finish(self, item: Interaction, state="completed") -> None:
        with self._guard:
            auth = item.context.pop("auth", None)
            if auth is not None:
                try:
                    auth.client.close()
                except Exception:
                    pass
            item.context.pop("fields", None)
            item.context.pop("password", None)
            item.context.pop("username", None)
            item.context.pop("capture_authorization", None)
            item.state = state

    def release(self, owner: tuple[str, ...]) -> None:
        with self._guard:
            self.focus.pop(self.owner_key(owner), None)

    def pending_approvals(self, session_id: str = "") -> list[Interaction]:
        with self._guard:
            return [i for i in self.items.values() if i.kind == "approval"
                    and not i.terminal and (not session_id or i.session_id == session_id)]

    def close(self) -> None:
        with self._guard:
            for item in self.items.values():
                if item.kind != "approval":
                    self.finish(item, "cancelled")
