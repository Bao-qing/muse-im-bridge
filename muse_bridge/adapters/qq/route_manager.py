"""Route manager for asynchronous user message routing.

Maps Muse user message IDs to QQ outbound destinations (group/private and msg_id).
Coordinates execution IDs (root_message_execution_id) so multi-part replies
(e.g. text + generated images + generated files) in the same conversational turn
stay with the group, while subsequent events (like delayed reminders with a new
execution ID) strictly route to the configured private chat.
Persists to disk (qq_routes.json) so routes survive process restarts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from typing import Dict, Optional


@dataclasses.dataclass
class RouteInfo:
    user_mid: str
    target_type: str  # "group" or "private"
    target_id: str
    msg_id: Optional[str] = None
    root_exec_id: Optional[str] = None
    seq: int = 1
    timestamp: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RouteInfo:
        return cls(**data)


class RouteManager:
    def __init__(self, state_file: Optional[str] = "qq_routes.json", ttl_s: float = 600.0):
        self.state_file = state_file
        self.ttl_s = ttl_s
        self._routes: Dict[str, RouteInfo] = {}  # user_mid -> RouteInfo
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                for k, v in data.items():
                    route = RouteInfo.from_dict(v)
                    if now - route.timestamp <= self.ttl_s:
                        self._routes[k] = route
        except Exception:
            pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        tmp = f"{self.state_file}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                data = {k: v.to_dict() for k, v in self._routes.items()}
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def register(
        self,
        user_mid: str,
        target_type: str,
        target_id: str,
        msg_id: Optional[str] = None,
    ) -> RouteInfo:
        self.cleanup()
        route = RouteInfo(
            user_mid=user_mid,
            target_type=target_type,
            target_id=target_id,
            msg_id=msg_id,
            timestamp=time.time(),
        )
        if user_mid:
            self._routes[user_mid] = route
            self._save_state()
        return route

    def get(self, user_mid: Optional[str]) -> Optional[RouteInfo]:
        if not user_mid:
            return None
        return self._routes.get(user_mid)

    def next_seq(self, user_mid: Optional[str]) -> int:
        """Get and increment the passive reply sequence number for user_mid."""
        if not user_mid or user_mid not in self._routes:
            return 1
        s = self._routes[user_mid].seq
        self._routes[user_mid].seq += 1
        self._save_state()
        return s

    def match_route(
        self,
        user_mid: Optional[str],
        root_exec_id: Optional[str] = None,
    ) -> Optional[RouteInfo]:
        """Match route for assistant event.

        Keeps the route active for all assistant events sharing the same turn (same root_exec_id
        or immediate presentation following the turn), but treats subsequent events with a
        different root_exec_id (such as a delayed reminder) as unrouted proactive pushes.
        """
        if not user_mid:
            return None
        route = self._routes.get(user_mid)
        if not route:
            return None

        # First assistant event in the turn binds its root_exec_id
        if not route.root_exec_id and root_exec_id:
            route.root_exec_id = root_exec_id
            self._save_state()
            return route

        # Events sharing the same root_exec_id or presentations without one belong to this turn
        if not root_exec_id or route.root_exec_id == root_exec_id:
            return route

        # Different root_exec_id means a separate turn / delayed reminder!
        return None

    def finish_turn(self, user_mid: Optional[str]) -> None:
        """Clear route when a subsequent user message arrives."""
        if user_mid and user_mid in self._routes:
            self._routes.pop(user_mid, None)
            self._save_state()

    def pop(self, user_mid: Optional[str]) -> Optional[RouteInfo]:
        """Consume and remove route for user_mid."""
        if not user_mid:
            return None
        route = self._routes.pop(user_mid, None)
        if route is not None:
            self._save_state()
        return route

    def cleanup(self) -> None:
        now = time.time()
        to_del = [k for k, v in self._routes.items() if now - v.timestamp > self.ttl_s]
        if to_del:
            for k in to_del:
                del self._routes[k]
            self._save_state()
