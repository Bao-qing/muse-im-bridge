"""Message deduplication and delivery state machine.

Implements the proven 3-state machine per assistant message:
  absent -> processing -> delivered
  with processing -> absent on rollback.

Also manages:
- Gateway message ID deduplication (avoiding duplicate delivery from QQ gateway).
- In-flight user messages (deferring proactive pushes while turn is processing).
- Superseded user messages (suppressing assistant replies to superseded turns).
- Persistent JSONL append-only storage surviving process restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, List, Optional, Set, Tuple

from .state_file import ensure_parent


class MessageDeduplicator:
    def __init__(
        self,
        persistence_file: Optional[str] = "data/qq_delivered.jsonl",
        in_flight_ttl_s: int = 300,
        processing_ttl_s: int = 300,
        gateway_dedup_s: int = 120,
    ):
        self.persistence_file = persistence_file
        self.in_flight_ttl_s = in_flight_ttl_s
        self.processing_ttl_s = processing_ttl_s
        self.gateway_dedup_s = gateway_dedup_s

        self._delivered_ids: Set[str] = set()
        self._superseded_user_ids: Set[str] = set()
        self._processing_ids: Dict[str, float] = {}  # mid -> timestamp
        self._in_flight_user_ids: Dict[str, float] = {}  # uid -> timestamp
        self._gateway_seen_ids: Dict[str, float] = {}  # msg_id -> timestamp

        if self.persistence_file and os.path.exists(self.persistence_file):
            self._load_from_file()

    def _load_from_file(self) -> None:
        if not self.persistence_file or not os.path.exists(self.persistence_file):
            return
        now = time.time()
        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(record, dict):
                        continue

                    # Delivered IDs
                    for mid in record.get("assistant_ids") or []:
                        if mid:
                            self._delivered_ids.add(mid)
                            self._processing_ids.pop(mid, None)

                    # Processing IDs
                    ts = record.get("ts", now)
                    for mid in record.get("processing_ids") or []:
                        if mid and mid not in self._delivered_ids:
                            self._processing_ids[mid] = ts

                    # Processing Rolled Back IDs
                    for mid in record.get("processing_rolled_back_ids") or []:
                        self._processing_ids.pop(mid, None)

                    # Superseded User IDs
                    uid = record.get("superseded_user_id")
                    if uid:
                        self._superseded_user_ids.add(uid)

                    # In-flight User IDs
                    ifid = record.get("in_flight_user_id")
                    if ifid:
                        self._in_flight_user_ids[ifid] = ts
        except Exception:
            pass

    def _append_record(self, data: dict) -> None:
        if not self.persistence_file:
            return
        data["ts"] = int(time.time())
        line = json.dumps(data, ensure_ascii=False) + "\n"
        try:
            ensure_parent(self.persistence_file)
            with open(self.persistence_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def is_delivered(self, mid: str) -> bool:
        return mid in self._delivered_ids

    def is_processing(self, mid: str, now: Optional[float] = None) -> bool:
        if mid in self._delivered_ids:
            return False
        ts = self._processing_ids.get(mid)
        if ts is None:
            return False
        curr = now if now is not None else time.time()
        if curr - ts > self.processing_ttl_s:
            # Expired mark
            self._processing_ids.pop(mid, None)
            return False
        return True

    def is_superseded(self, uid: str) -> bool:
        return uid in self._superseded_user_ids

    def is_in_flight(self, uid: str, now: Optional[float] = None) -> bool:
        ts = self._in_flight_user_ids.get(uid)
        if ts is None:
            return False
        curr = now if now is not None else time.time()
        if curr - ts > self.in_flight_ttl_s:
            self._in_flight_user_ids.pop(uid, None)
            return False
        return True

    def mark_processing(self, mids: List[str]) -> None:
        now = time.time()
        valid = [m for m in mids if m and m not in self._delivered_ids]
        if not valid:
            return
        for m in valid:
            self._processing_ids[m] = now
        self._append_record({"processing_ids": valid})

    def rollback_processing(self, mids: List[str]) -> None:
        valid = [m for m in mids if m and m in self._processing_ids]
        if not valid:
            return
        for m in valid:
            self._processing_ids.pop(m, None)
        self._append_record({"processing_rolled_back_ids": valid})

    def mark_delivered(self, mids: List[str]) -> None:
        valid = [m for m in mids if m and m not in self._delivered_ids]
        if not valid:
            return
        for m in valid:
            self._delivered_ids.add(m)
            self._processing_ids.pop(m, None)
        self._append_record({"assistant_ids": valid})

    def mark_in_flight(self, uid: str) -> None:
        if not uid:
            return
        self._in_flight_user_ids[uid] = time.time()
        self._append_record({"in_flight_user_id": uid})

    def clear_in_flight(self, uid: str) -> None:
        if uid in self._in_flight_user_ids:
            self._in_flight_user_ids.pop(uid, None)

    def mark_superseded(self, uid: str) -> None:
        if not uid:
            return
        self._superseded_user_ids.add(uid)
        self._append_record({"superseded_user_id": uid})

    def is_duplicate_gateway_msg(self, msg_id: str) -> bool:
        """Check if incoming gateway message id was already seen within gateway_dedup_s."""
        if not msg_id:
            return False
        now = time.monotonic()
        # Prune old
        to_del = [k for k, v in self._gateway_seen_ids.items() if now - v > self.gateway_dedup_s]
        for k in to_del:
            del self._gateway_seen_ids[k]

        if msg_id in self._gateway_seen_ids:
            return True
        self._gateway_seen_ids[msg_id] = now
        return False
