"""Chat -> Muse session mapping.

Every group / private user gets its own Muse session. The binding is created
lazily (fresh UUID on first message), may be pre-seeded from config
(``session_bindings``) and is persisted to disk so it survives restarts.
``/new`` and ``/switch`` only rebind the chat that issued them; several chats
may share one session by switching to it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from .state_file import ensure_parent


def chat_key(target_type: str, target_id: str) -> str:
    """Stable string key for a chat, e.g. ``group:ABC`` or ``private:XYZ``."""
    return f"{target_type}:{target_id}"


def split_chat_key(key: str) -> Tuple[str, str]:
    t, _, i = key.partition(":")
    return t, i


class SessionMapper:
    def __init__(
        self,
        state_file: Optional[str] = None,
        bindings: Optional[Dict[str, str]] = None,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        self.state_file = state_file
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self._map: Dict[str, str] = {}
        self._config_bindings: Dict[str, str] = {
            str(k): str(v) for k, v in (bindings or {}).items() if k and v
        }
        self._load()

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        if self.state_file and os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._map = {str(k): str(v) for k, v in data.items() if k and v}
            except Exception:
                self._map = {}
        # Explicit config bindings seed chats that have no runtime binding yet;
        # runtime changes (/switch, /new) win over the config afterwards.
        for k, v in self._config_bindings.items():
            self._map.setdefault(k, v)

    def _save(self) -> None:
        if not self.state_file:
            return
        tmp = f"{self.state_file}.tmp.{os.getpid()}"
        try:
            ensure_parent(self.state_file)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._map, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.state_file)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # -- resolution ----------------------------------------------------
    def resolve(self, target_type: str, target_id: str) -> Tuple[str, bool]:
        """Return ``(session_id, created)`` for a chat.

        ``created`` is True only when a brand new session binding was minted
        (first message from this chat).
        """
        key = chat_key(target_type, target_id)
        with self._lock:
            sid = self._map.get(key)
            if sid:
                return sid, False
            sid = self._id_factory()
            self._map[key] = sid
            self._save()
            return sid, True

    def current(self, target_type: str, target_id: str) -> str:
        """Session the chat is bound to *without* creating a binding."""
        with self._lock:
            return self._map.get(chat_key(target_type, target_id), "")

    def bind(self, target_type: str, target_id: str, session_id: str) -> None:
        """Rebind a chat (``/switch`` / ``/new``)."""
        with self._lock:
            self._map[chat_key(target_type, target_id)] = session_id
            self._save()

    def new_session(self, target_type: str, target_id: str) -> str:
        """Force create a brand new session ID for a chat and persist binding."""
        new_sid = self._id_factory()
        self.bind(target_type, target_id, new_sid)
        return new_sid

    # -- reverse lookups (used by the outbound pusher) ------------------
    def session_ids(self) -> List[str]:
        """All session ids that must be watched for assistant output."""
        with self._lock:
            seen: List[str] = []
            for sid in self._map.values():
                if sid not in seen:
                    seen.append(sid)
            return seen

    def owners(self, session_id: str) -> List[Tuple[str, str]]:
        """Chats bound to ``session_id`` (usually one)."""
        with self._lock:
            return [split_chat_key(k) for k, v in self._map.items() if v == session_id]

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._map)
