"""Route manager for asynchronous user message routing.

Maps Muse user message IDs to QQ outbound destinations (group/private and msg_id).
When an assistant message replies to a user message, the route is consumed (popped),
ensuring subsequent proactive events (like delayed reminders) strictly route to
the configured proactive push target (e.g. private chat) rather than hijacking the group.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Dict, Optional


@dataclasses.dataclass
class RouteInfo:
    user_mid: str
    target_type: str  # "group" or "private"
    target_id: str
    msg_id: Optional[str] = None
    timestamp: float = dataclasses.field(default_factory=time.time)


class RouteManager:
    def __init__(self, ttl_s: float = 600.0):
        self.ttl_s = ttl_s
        self._routes: Dict[str, RouteInfo] = {}  # user_mid -> RouteInfo

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
        return route

    def get(self, user_mid: Optional[str]) -> Optional[RouteInfo]:
        if not user_mid:
            return None
        return self._routes.get(user_mid)

    def pop(self, user_mid: Optional[str]) -> Optional[RouteInfo]:
        """Consume and remove the route for user_mid upon dispatching reply."""
        if not user_mid:
            return None
        return self._routes.pop(user_mid, None)

    def cleanup(self) -> None:
        now = time.time()
        to_del = [k for k, v in self._routes.items() if now - v.timestamp > self.ttl_s]
        for k in to_del:
            del self._routes[k]
