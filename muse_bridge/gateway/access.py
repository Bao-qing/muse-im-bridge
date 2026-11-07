"""Local whitelist applications, independent of Muse approvals and of the chat platform."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import secrets
import tempfile
import threading
import time

from ..core.state_file import ensure_parent


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and value != "*" and not any(c.isspace() for c in value))


class AccessRequestStore:
    PENDING_TTL = 7 * 86400
    MAX_PENDING = 1000
    MAX_HISTORY = 1000
    MAX_GRANTS = 10000

    def __init__(self, path, app_id):
        # ``app_id`` names the bot identity the grants belong to (QQ app id, "onebot").
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self.data = {"version": 1, "app_id": app_id, "private": [], "groups": {}, "requests": []}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            self._validate(data, app_id)
            self.data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("Cannot read access request state; refusing dynamic authorization") from None

    @staticmethod
    def _validate(data, app_id):
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("app_id") != app_id
                or not isinstance(data.get("private"), list)
                or not all(valid_id(v) for v in data["private"])
                or not isinstance(data.get("groups"), dict)
                or not isinstance(data.get("requests"), list)):
            raise ValueError("Invalid access state")
        for group, members in data["groups"].items():
            if not valid_id(group) or not (members == "*" or
                    isinstance(members, list) and all(valid_id(v) for v in members)):
                raise ValueError("Invalid group grant")
        ids = set()
        for row in data["requests"]:
            if (not isinstance(row, dict) or not valid_id(row.get("id"))
                    or row["id"] in ids
                    or row.get("kind") not in {"private", "group"}
                    or not valid_id(row.get("target_id")) or not valid_id(row.get("requester"))
                    or row.get("scope") not in {"self", "all"}
                    or row.get("status") not in {"pending", "approved", "denied", "expired"}
                    or not isinstance(row.get("notified"), list)
                    or not all(valid_id(v) for v in row["notified"])):
                raise ValueError("Invalid application")
            if row["kind"] == "private" and (row["scope"] != "self" or row["target_id"] != row["requester"]):
                raise ValueError("Invalid private application")
            for field in ("created_at", "updated_at", "notice_at"):
                value = row.get(field)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid application time")
            ids.add(row["id"])

    def _commit(self, data):
        ensure_parent(self.path)
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, temporary = tempfile.mkstemp(prefix=".access-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write(chr(10))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.data = data
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _allows(data, kind, target, user, scope="self"):
        if kind == "private":
            return user in data["private"]
        members = data["groups"].get(target, [])
        return members == "*" or scope == "self" and user in members

    def allows(self, kind, target, user, scope="self"):
        with self._lock:
            return self._allows(self.data, kind, target, user, scope)

    @staticmethod
    def _prune(data, now):
        for row in data["requests"]:
            if row["status"] == "pending" and now - row["created_at"] >= AccessRequestStore.PENDING_TTL:
                row.update(status="expired", updated_at=now)
        pending = [r for r in data["requests"] if r["status"] == "pending"]
        history = sorted((r for r in data["requests"] if r["status"] != "pending"),
                         key=lambda r: r["updated_at"])[-AccessRequestStore.MAX_HISTORY:]
        data["requests"] = history + pending

    @staticmethod
    def _grant(data, row):
        count = len(data["private"]) + sum(1 if v == "*" else len(v) for v in data["groups"].values())
        if count >= AccessRequestStore.MAX_GRANTS and not AccessRequestStore._allows(
                data, row["kind"], row["target_id"], row["requester"], row["scope"]):
            raise ValueError("白名单已满，请联系管理员。")
        if row["kind"] == "private":
            if row["requester"] not in data["private"]:
                data["private"].append(row["requester"])
        elif row["scope"] == "all":
            data["groups"][row["target_id"]] = "*"
        elif data["groups"].get(row["target_id"]) != "*":
            members = data["groups"].setdefault(row["target_id"], [])
            if row["requester"] not in members:
                members.append(row["requester"])

    def apply(self, kind, target, user, scope, *, automatic=False, now=None):
        if (kind not in {"private", "group"} or not valid_id(target) or not valid_id(user)
                or scope not in {"self", "all"}
                or kind == "private" and (scope != "self" or target != user)):
            raise ValueError("申请信息无效。")
        now = time.time() if now is None else now
        with self._lock:
            data = copy.deepcopy(self.data)
            self._prune(data, now)
            key = (kind, target, user, scope)
            matches = [r for r in data["requests"]
                       if (r["kind"], r["target_id"], r["requester"], r["scope"]) == key]
            pending = next((r for r in matches if r["status"] == "pending"), None)
            if pending:
                return copy.deepcopy(pending), False
            if sum(r["status"] == "pending" for r in data["requests"]) >= self.MAX_PENDING:
                raise ValueError("待处理申请较多，请稍后再试。")
            row = {"id": "W" + secrets.token_hex(4).upper(), "kind": kind,
                   "target_id": target, "requester": user, "scope": scope,
                   "status": "approved" if automatic else "pending",
                   "created_at": now, "updated_at": now, "notified": [], "notice_at": 0}
            while any(r["id"] == row["id"] for r in data["requests"]):
                row["id"] = "W" + secrets.token_hex(4).upper()
            if automatic:
                self._grant(data, row)
            data["requests"].append(row)
            self._prune(data, now)
            self._commit(data)
            return copy.deepcopy(row), True

    def get(self, request_id):
        with self._lock:
            return copy.deepcopy(next((r for r in self.data["requests"] if r["id"] == request_id), None))

    def pending(self):
        with self._lock:
            data = copy.deepcopy(self.data)
            self._prune(data, time.time())
            if data != self.data:
                self._commit(data)
            return copy.deepcopy([r for r in data["requests"] if r["status"] == "pending"])

    def decide(self, request_id, approve, admin):
        with self._lock:
            data = copy.deepcopy(self.data)
            self._prune(data, time.time())
            row = next((r for r in data["requests"] if r["id"] == request_id), None)
            if row is None:
                raise ValueError("未找到此申请。")
            changed = row["status"] == "pending"
            if changed:
                if approve:
                    self._grant(data, row)
                row.update(status="approved" if approve else "denied",
                           updated_at=time.time(), decided_by=admin)
            if data != self.data:
                self._commit(data)
            return copy.deepcopy(row), changed

    def notice(self, request_id, admin=None):
        with self._lock:
            data = copy.deepcopy(self.data)
            row = next((r for r in data["requests"] if r["id"] == request_id), None)
            if row is None or row["status"] != "pending":
                return None
            if admin is None:
                if time.time() - row["notice_at"] < 60:
                    return None
                row["notice_at"] = time.time()
            elif admin not in row["notified"]:
                row["notified"].append(admin)
            self._commit(data)
            return copy.deepcopy(row)


class ChatAccessRequests:
    def __init__(self, adapter):
        self.adapter = adapter
        self.config = adapter.config.access_requests
        self.store = AccessRequestStore(self.config.state_file, adapter.access_identity())
        self._notification_lock = asyncio.Lock()

    @staticmethod
    def is_command(content):
        parts = content.split()
        return bool(parts) and parts[0] in {"/申请", "/申请审批"}

    def allows(self, kind, target, user, scope="self"):
        if kind == "private" and getattr(self.adapter.config, "allow_all_private", False):
            return True
        if kind == "group" and getattr(self.adapter.config, "allow_all_group", False):
            return True
        static = self.adapter.config.whitelist
        members = static.get("private" if kind == "private" else target, [])
        allowed = (user in members if kind == "private" and isinstance(members, list) else
                   kind == "group" and (members == "*" or scope == "self"
                   and isinstance(members, list) and user in members))
        return allowed or self.store.allows(kind, target, user, scope)

    @staticmethod
    def scope_text(row):
        return "本群全员" if row["scope"] == "all" else ("私聊访问" if row["kind"] == "private" else "申请人在本群使用")

    async def _reply(self, kind, target, text, msg_id=None):
        try:
            await self.adapter.send_reply(kind, target, text, msg_id)
            return True
        except Exception:
            return False

    async def _notify(self, row, admins):
        async with self._notification_lock:
            row = await asyncio.to_thread(self.store.notice, row["id"])
            if row is None:
                return False
            for admin in admins:
                if admin in row["notified"]:
                    continue
                text = (f"【白名单申请 {row['id']}】\n"
                        f"聊天：{row['target_id']}\n申请人：{row['requester']}\n"
                        f"范围：{self.scope_text(row)}\n"
                        f"/申请审批 {row['id']} 同意\n/申请审批 {row['id']} 拒绝")
                if await self._reply("private", admin, text):
                    row = await asyncio.to_thread(self.store.notice, row["id"], admin)
                    if row is None:
                        break
            return bool(row and row["notified"])

    async def handle(self, kind, target, user, content, msg_id):
        parts = content.split()
        if not parts or parts[0] not in {"/申请", "/申请审批"}:
            return False
        if not valid_id(user) or not valid_id(target):
            return True
        if parts[0] == "/申请审批":
            if kind != "private" or not self.adapter.config.is_admin(user):
                await self._reply(kind, target, "请由管理员私聊审批。", msg_id)
                return True
            await self._review(target, user, parts[1:], msg_id)
            return True
        if not self.config.enabled:
            await self._reply(kind, target, "暂未开放申请。", msg_id)
            return True
        if len(parts) > 2 or len(parts) == 2 and (parts[1] != "全员" or kind != "group"):
            await self._reply(kind, target, "发送 /申请；群内开放给所有人请发送 /申请 全员。", msg_id)
            return True
        scope = "all" if len(parts) == 2 else "self"
        if self.allows(kind, target, user, scope):
            await self._reply(kind, target, "本群已对所有人开放。" if scope == "all" else "你已可以使用，无需申请。", msg_id)
            return True
        admins = list(dict.fromkeys(a for a in self.adapter.config.admin_id if valid_id(a)))
        if not self.config.auto_approve and not admins:
            await self._reply(kind, target, "暂时无法申请，请联系管理员。", msg_id)
            return True
        try:
            row, _ = await asyncio.to_thread(self.store.apply, kind, target, user, scope,
                                             automatic=self.config.auto_approve)
            if row["status"] == "pending":
                await self._notify(row, admins)
                row = await asyncio.to_thread(self.store.get, row["id"]) or row
            if row["status"] == "approved":
                text = "本群已对所有人开放。" if scope == "all" else "已开通，可以开始聊天。"
            elif row["status"] == "pending":
                text = (f"申请 {row['id']} 已提交，等待管理员处理。" if row["notified"] else
                        f"申请 {row['id']} 已保存，暂未通知到管理员，请稍后重试。")
            else:
                text = "申请未通过。" if row["status"] == "denied" else "申请已过期，请重新申请。"
            await self._reply(kind, target, text, msg_id)
        except ValueError as exc:
            await self._reply(kind, target, str(exc), msg_id)
        except (OSError, TypeError):
            await self._reply(kind, target, "申请暂时无法保存，请稍后重试。", msg_id)
        return True

    async def _review(self, target, admin, args, msg_id):
        try:
            if not args:
                rows = await asyncio.to_thread(self.store.pending)
                lines = [f"{r['id']}｜{r['target_id']}｜{r['requester']}｜{self.scope_text(r)}" for r in rows[:20]]
                text = ("待审批申请：\n" + chr(10).join(lines) +
                        "\n发送 /申请审批 编号 同意 或 拒绝。") if lines else "暂无待审批申请。"
                await self._reply("private", target, text, msg_id)
                return
            if len(args) != 2 or args[1] not in {"同意", "拒绝"}:
                await self._reply("private", target, "发送 /申请审批 编号 同意 或 拒绝。", msg_id)
                return
            row, changed = await asyncio.to_thread(self.store.decide, args[0].upper(), args[1] == "同意", admin)
            status = {"approved": "已通过", "denied": "未通过", "expired": "已过期"}[row["status"]]
            await self._reply("private", target, f"申请 {row['id']} {status}。", msg_id)
            if changed:
                text = ("本群已对所有人开放。" if row["scope"] == "all" else "申请已通过，可以开始聊天。") if row["status"] == "approved" else "申请未通过。"
                await self._reply(row["kind"], row["target_id"], f"{row['id']}：{text}")
        except ValueError as exc:
            await self._reply("private", target, str(exc), msg_id)
        except (OSError, TypeError):
            await self._reply("private", target, "审批暂时无法保存，请稍后重试。", msg_id)
