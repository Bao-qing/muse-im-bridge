"""Private, account-scoped iLink state. JSON is atomic, not encrypted.

A persisted sending reservation is intentionally not replayed after a crash:
the remote side may have accepted it. This is best-effort, not exactly-once.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from ...core.state_file import atomic_json_write


class WeChatStateError(ValueError):
    """A safe error: never contains the offending credentials or response."""


def account_key(bot_id: str) -> str:
    return hashlib.sha256(bot_id.encode()).hexdigest()[:24]


def _id(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(c.isspace() or ord(c) < 32 for c in value))


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 65536


def _credentials(value):
    if not isinstance(value, dict):
        raise WeChatStateError("Invalid WeChat account state")
    if not all(_id(value.get(k)) for k in ("bot_id", "user_id")) or not _text(value.get("bot_token")):
        raise WeChatStateError("Incomplete WeChat account state")
    try:
        url = urlsplit(value.get("baseurl", ""))
        host = (url.hostname or "").lower()
        valid = (url.scheme == "https" and host.endswith(".weixin.qq.com")
                 and not url.username and not url.password and url.port in (None, 443)
                 and url.path in ("", "/") and not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise WeChatStateError("Untrusted WeChat API origin")
    return {k: value[k] for k in ("bot_id", "user_id", "bot_token", "baseurl")}


def _read(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        raise WeChatStateError("Cannot read WeChat state; refusing to reuse it") from None


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


class WeChatState:
    MAX_MESSAGES = 4096
    MAX_SNAPSHOTS = 2048
    MAX_PARTS = 8192
    MAX_USERS = 2048

    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self._lock = threading.RLock()
        self.credentials = None
        self.bot_id = ""
        self.account_dir = None
        self._data = {}
        active = _read(self.root / "active.local.json")
        if active is not None:
            if not isinstance(active, dict) or active.get("version") != 1 or not _id(active.get("bot_id")):
                raise WeChatStateError("Invalid WeChat active account")
            self._load_account(active["bot_id"])

    def _load_account(self, bot_id):
        directory = self.root / "accounts" / account_key(bot_id)
        saved = _read(directory / "credentials.local.json")
        if not isinstance(saved, dict) or saved.get("version") != 1:
            raise WeChatStateError("Missing WeChat credentials; run --wechat-login")
        credentials = _credentials(saved)
        if credentials["bot_id"] != bot_id:
            raise WeChatStateError("Foreign WeChat account state")
        data = _read(directory / "protocol.local.json")
        if data is None:
            data = {"version": 1, "bot_id": bot_id, "cursor": "", "cooldown_until": 0,
                    "latest": {}, "snapshots": {}, "messages": {}, "parts": {}}
        if not isinstance(data, dict):
            raise WeChatStateError("Invalid WeChat protocol state")
        data.pop("requests", None)  # Obsolete access requests never become chat contexts.
        self._validate(data, bot_id)
        self.credentials, self.bot_id, self.account_dir, self._data = credentials, bot_id, directory, data

    @classmethod
    def _validate(cls, data, bot_id):
        try:
            valid = (isinstance(data, dict) and data["version"] == 1 and data["bot_id"] == bot_id
                     and isinstance(data["cursor"], str) and len(data["cursor"]) <= 1048576)
            until = data["cooldown_until"]
            valid = valid and type(until) in (float, int) and math.isfinite(until) and until >= 0
            for key, limit in (("latest", cls.MAX_USERS), ("snapshots", cls.MAX_SNAPSHOTS),
                               ("messages", cls.MAX_MESSAGES), ("parts", cls.MAX_PARTS)):
                valid = valid and isinstance(data[key], dict) and len(data[key]) <= limit
            valid = valid and all(_id(k) and _text(v) for k, v in data["latest"].items())
            valid = valid and all(isinstance(k, str) and _text(v) for k, v in data["snapshots"].items())
            valid = valid and all(isinstance(k, str) and v in {"accepted", "handled", "unknown", "ignored"}
                                  for k, v in data["messages"].items())
            valid = valid and all(isinstance(k, str) and v in {"sending", "confirmed", "unknown"}
                                  for k, v in data["parts"].items())
            if not valid:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise WeChatStateError("Invalid WeChat protocol state") from None

    def save_account(self, credentials):
        credentials = _credentials(credentials)
        with self._lock:
            directory = self.root / "accounts" / account_key(credentials["bot_id"])
            # Validate existing state before changing either credential or active pointer.
            existing = _read(directory / "protocol.local.json")
            if existing is not None:
                self._validate(existing, credentials["bot_id"])
            atomic_json_write(directory / "credentials.local.json", {"version": 1, **credentials})
            atomic_json_write(self.root / "active.local.json", {"version": 1, "bot_id": credentials["bot_id"]})
            self._load_account(credentials["bot_id"])

    def accounts(self):
        result = []
        for path in sorted((self.root / "accounts").glob("*/credentials.local.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            saved = _read(path)
            if not isinstance(saved, dict) or saved.get("version") != 1:
                raise WeChatStateError("Invalid saved WeChat account")
            credentials = _credentials(saved)
            if path.parent.name != account_key(credentials["bot_id"]):
                raise WeChatStateError("Foreign WeChat account directory")
            result.append(credentials)
        return result[:10]

    def _commit(self, change):
        with self._lock:
            if not self.credentials:
                raise WeChatStateError("WeChat is not logged in; run --wechat-login")
            data = copy.deepcopy(self._data)
            change(data)
            self._validate(data, self.bot_id)
            atomic_json_write(self.account_dir / "protocol.local.json", data)
            self._data = data

    @property
    def cursor(self):
        return self._data.get("cursor", "")

    def set_cursor(self, cursor):
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 1048576:
                raise WeChatStateError("Invalid WeChat cursor")
            self._commit(lambda d: d.update(cursor=cursor))

    @property
    def cooldown_until(self):
        return self._data.get("cooldown_until", 0)

    def pause(self):
        self._commit(lambda d: d.update(cooldown_until=max(d["cooldown_until"], time.time() + 3600)))

    @staticmethod
    def _trim(table, limit, removable):
        for key in list(table):
            if len(table) < limit:
                break
            if table[key] in removable:
                del table[key]
        if len(table) >= limit:
            raise WeChatStateError("WeChat state capacity reached; inspect uncertain deliveries")

    def message_status(self, user, msg_id):
        return self._data.get("messages", {}).get(_key(user, msg_id))

    def remember_message(self, user, msg_id, token):
        if not _id(user) or not _id(msg_id):
            raise WeChatStateError("Invalid WeChat message identity")
        key = _key(user, msg_id)
        def change(d):
            if key in d["messages"]:
                return  # Replaying an old batch must never move latest backwards.
            self._trim(d["messages"], self.MAX_MESSAGES, {"handled", "ignored"})
            d["messages"][key] = "accepted"
            if token:
                if not _text(token):
                    raise WeChatStateError("Invalid WeChat reply context")
                if user not in d["latest"] and len(d["latest"]) >= self.MAX_USERS:
                    raise WeChatStateError("Too many WeChat context owners")
                d["latest"][user] = token
                d["snapshots"][key] = token
                while len(d["snapshots"]) > self.MAX_SNAPSHOTS:
                    del d["snapshots"][next(iter(d["snapshots"]))]
        self._commit(change)

    def finish_message(self, user, msg_id, status="handled"):
        if status not in {"handled", "ignored", "unknown"}:
            raise WeChatStateError("Invalid WeChat processing result")
        self._commit(lambda d: d["messages"].update({_key(user, msg_id): status}))

    def recover_inputs(self):
        """Do not replay an input which might have been POSTed before a crash."""
        if any(v == "accepted" for v in self._data.get("messages", {}).values()):
            self._commit(lambda d: d.update(messages={k: "unknown" if v == "accepted" else v
                                                     for k, v in d["messages"].items()}))

    def retry_message(self, user, msg_id):
        self._commit(lambda d: d["messages"].update({_key(user, msg_id): "accepted"}))

    def context(self, user, msg_id=None):
        if msg_id is not None:
            return self._data.get("snapshots", {}).get(_key(user, msg_id))
        return self._data.get("latest", {}).get(user)

    def part_status(self, key):
        return self._data.get("parts", {}).get(key)

    def reserve_part(self, key):
        def change(d):
            if key in d["parts"]:
                raise WeChatStateError("WeChat delivery already attempted")
            self._trim(d["parts"], self.MAX_PARTS, {"confirmed"})
            d["parts"][key] = "sending"
        self._commit(change)

    def finish_part(self, key, status):
        if status not in {"confirmed", "unknown", "retryable"}:
            raise WeChatStateError("Invalid WeChat delivery result")
        def change(d):
            if status == "retryable":
                d["parts"].pop(key, None)
            else:
                d["parts"][key] = status
        self._commit(change)

    def status(self):
        return {"logged_in": bool(self.credentials), "bot_id": self.bot_id,
                "user_id": (self.credentials or {}).get("user_id", ""),
                "has_token": bool((self.credentials or {}).get("bot_token")),
                "has_cursor": bool(self.cursor), "context_users": len(self._data.get("latest", {})),
                "cooldown_until": self.cooldown_until,
                "uncertain_sends": sum(v in {"sending", "unknown"} for v in self._data.get("parts", {}).values()),
                "uncertain_inputs": sum(v in {"accepted", "unknown"} for v in self._data.get("messages", {}).values())}
