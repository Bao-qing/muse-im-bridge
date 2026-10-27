"""Route manager for asynchronous user message routing.

Maps Muse user message IDs to QQ outbound destinations (group/private and msg_id).
When an assistant message replies to a user message, the route is consumed (popped),
ensuring subsequent proactive events (like delayed reminders) strictly route to
the configured proactive push target (e.g. private chat) rather than hijacking the group.
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

    def pop(self, user_mid: Optional[str]) -> Optional[RouteInfo]:
        """Consume and remove the route for user_mid upon dispatching reply."""
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
