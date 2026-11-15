"""Recent inbound administrator private chats, isolated by gateway account.

No message content or credentials are persisted. Outbound notifications never
refresh activity, and an expired passive-reply route does not erase the inbox.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from ..core.state_file import atomic_json_write
from ..observability import get_logger, safe_error

logger = get_logger(__name__)


class AdminPrivateActivity:
    def __init__(self, path):
        self.path = path
        self._identity = None
        self._activity = {}

    def _load(self, identity):
        if self._identity == identity:
            return
        self._identity = identity
        self._activity = {}
        if not self.path:
            return
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
            if data.get("identity") == identity:
                self._activity = {
                    user: timestamp for user, timestamp in data.get("activity", {}).items()
                    if isinstance(user, str) and type(timestamp) in (int, float)
                    and math.isfinite(timestamp) and timestamp > 0
                }
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Administrator activity unavailable: %s", safe_error(exc))

    def record(self, identity, user, admins):
        self._load(identity)
        if user not in admins or user == "*":
            return
        self._activity = {u: ts for u, ts in self._activity.items() if u in admins and u != "*"}
        self._activity[user] = time.time()
        if self.path:
            try:
                atomic_json_write(self.path, {"identity": identity, "activity": self._activity})
            except Exception as exc:
                logger.warning("Administrator activity write failed: %s", safe_error(exc))

    def latest(self, identity, admins):
        self._load(identity)
        candidates = [(u, ts) for u, ts in self._activity.items() if u in admins and u != "*"]
        return max(candidates, key=lambda row: row[1]) if candidates else None
