"""Bounded, identity-scoped message references and reaction delivery state."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
from collections import OrderedDict
from typing import Iterable, Optional

from .state_file import ensure_parent


@dataclasses.dataclass
class MessageReference:
    session_id: str
    target_type: str
    target_id: str
    muse_id: str
    references: list[tuple[str, str]]
    excerpt: str = ""
    reply_target: Optional[dict] = None


def _opaque(value) -> Optional[str]:
    # Do not parse, trim, or numerically normalize platform IDs.
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)
    return None


def _references(values: Iterable, bounded: bool = True) -> list[tuple[str, str]]:
    refs = OrderedDict()
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        kind, value = pair
        value = _opaque(value)
        if kind not in ("id", "ref") or not value:
            continue
        key = (kind, value)
        refs.pop(key, None)
        refs[key] = None
        while bounded and len(refs) > 256:
            refs.popitem(last=False)
    return list(refs)


def _reply_target(value) -> Optional[dict]:
    # Only routing metadata belongs on disk, never the original event.
    if isinstance(value, dict) and value.get("type") == "presentation":
        event_id = _opaque(value.get("presentation_event_id"))
        if event_id:
            return {"type": "presentation", "presentation_event_id": event_id}
    return None


def _emojis(values: Iterable) -> list[str]:
    result = []
    for value in values:
        if isinstance(value, str) and 0 < len(value) <= 64:
            result.append(value)
            if len(result) == 64:
                break
    return result


def _copy(record: MessageReference) -> MessageReference:
    return dataclasses.replace(
        record, references=list(record.references),
        reply_target=dict(record.reply_target) if record.reply_target else None,
    )


class MessageReferenceStore:
    """Keep the latest ``max_records`` entries in each independent store.

    IDs remain opaque strings; kinds are distinct namespaces. Lookups never
    fall back to another chat or session. Records retain at most 256 recent
    references and a 240-character excerpt. Reaction snapshots retain at most
    64 emoji strings, each at most 64 characters. Returned data is detached.
    Thread safety is per instance, not interprocess.
    """

    def __init__(self, state_file: Optional[str] = None, identity: str = "",
                 max_records=4096):
        if type(max_records) is not int or max_records < 0:
            raise ValueError("max_records must be a nonnegative integer")
        self.state_file = state_file
        self.identity = str(identity)
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: OrderedDict[tuple[str, str, str, str], MessageReference] = OrderedDict()
        self._reactions: OrderedDict[tuple[str, str], tuple[int, list[str]]] = OrderedDict()
        self._load()

    def _trim(self, entries) -> None:
        while len(entries) > self.max_records:
            entries.popitem(last=False)

    def remember(self, session_id, target_type, target_id, muse_id, references,
                 excerpt="", reply_target=None) -> MessageReference:
        key = tuple(str(value) for value in (session_id, target_type, target_id, muse_id))
        with self._lock:
            old = self._records.get(key)
            refs = _references(references)
            if old:
                refs = _references([*old.references, *refs])
            record = MessageReference(
                *key, refs,
                excerpt[:240] if isinstance(excerpt, str) and excerpt else (old.excerpt if old else ""),
                _reply_target(reply_target) or (old.reply_target if old else None),
            )
            self._records[key] = record
            self._records.move_to_end(key)
            self._trim(self._records)
            self._save()
            return _copy(record)

    def resolve_channel(self, session_id, target_type, target_id, references) -> Optional[MessageReference]:
        scope = tuple(str(value) for value in (session_id, target_type, target_id))
        refs = set(_references(references, bounded=False))
        with self._lock:
            match = None
            for key, record in self._records.items():
                if key[:3] == scope and refs.intersection(record.references):
                    # Reject ambiguous refs even when accompanied by a unique one.
                    if match is not None:
                        return None
                    match = record
            return _copy(match) if match is not None else None

    def resolve_muse(self, session_id, target_type, target_id, muse_id) -> Optional[MessageReference]:
        key = tuple(str(value) for value in (session_id, target_type, target_id, muse_id))
        with self._lock:
            record = self._records.get(key)
            if record is None:
                matches = [r for k, r in self._records.items() if k[:3] == key[:3]
                           and r.reply_target and r.reply_target.get("presentation_event_id") == key[3]]
                record = matches[0] if len(matches) == 1 else None
            return _copy(record) if record is not None else None

    def reaction_state(self, session_id, message_id) -> Optional[tuple[int, list[str]]]:
        with self._lock:
            state = self._reactions.get((str(session_id), str(message_id)))
            return (state[0], list(state[1])) if state is not None else None

    def set_reaction_state(self, session_id, message_id, seq, emojis) -> None:
        if type(seq) is not int:
            raise ValueError("reaction sequence must be an integer")
        with self._lock:
            key = (str(session_id), str(message_id))
            self._reactions[key] = (seq, _emojis(emojis))
            self._reactions.move_to_end(key)
            self._trim(self._reactions)
            self._save()

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or data.get("identity") != self.identity:
                    return
                records, reactions = data.get("records", []), data.get("reactions", [])
                if not isinstance(records, list) or not isinstance(reactions, list):
                    return
                for item in records:
                    if not isinstance(item, dict):
                        continue
                    key = tuple(item.get(name) for name in
                                ("session_id", "target_type", "target_id", "muse_id"))
                    if not all(isinstance(value, str) for value in key):
                        continue
                    refs, excerpt = item.get("references", []), item.get("excerpt", "")
                    if not isinstance(refs, list) or not isinstance(excerpt, str):
                        continue
                    old = self._records.get(key)
                    record = MessageReference(
                        *key, _references([*(old.references if old else []), *refs]),
                        excerpt[:240], _reply_target(item.get("reply_target")),
                    )
                    self._records[key] = record
                    self._records.move_to_end(key)
                    self._trim(self._records)
                for item in reactions:
                    if not isinstance(item, dict):
                        continue
                    sid, mid = item.get("session_id"), item.get("message_id")
                    seq, emojis = item.get("seq"), item.get("emojis")
                    if (not isinstance(sid, str) or not isinstance(mid, str)
                            or type(seq) is not int or not isinstance(emojis, list)):
                        continue
                    key = (sid, mid)
                    self._reactions[key] = (seq, _emojis(emojis))
                    self._reactions.move_to_end(key)
                    self._trim(self._reactions)
            except (OSError, ValueError, TypeError, RecursionError):
                self._records.clear()
                self._reactions.clear()

    def _save(self) -> None:
        if not self.state_file:
            return
        tmp = None
        try:
            if not ensure_parent(self.state_file):
                return
            data = {
                "identity": self.identity,
                "records": [dataclasses.asdict(record) for record in self._records.values()],
                "reactions": [
                    {"session_id": sid, "message_id": mid, "seq": seq, "emojis": emojis}
                    for (sid, mid), (seq, emojis) in self._reactions.items()
                ],
            }
            directory = os.path.dirname(os.path.abspath(self.state_file))
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".references-", suffix=".tmp", delete=False) as f:
                tmp = f.name
                json.dump(data, f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
