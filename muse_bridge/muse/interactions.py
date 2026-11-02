"""Muse credential and approval protocol, separate from IM presentation."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import threading
import uuid
from urllib.parse import parse_qs, quote, urlparse

from ..observability import get_logger, safe_error, trace_event


logger = get_logger(__name__)


# Single-value captures use explicit protected fields, never names guessed from
# a card title. Legacy create_password cards use the password field.
SECRET_CAPTURE_FIELDS = {
    "create_password": "password", "password": "password", "secret": "secret",
    "api_key": "api_key", "private_key": "private_key",
    "ssh_private_key": "private_key", "otp_seed": "otp_seed",
    "totp_secret": "totp_secret",
}


def credential_input_kind(context):
    capture = context.get("capture", "sign_in")
    if capture == "verification_code":
        return "code"
    if capture in SECRET_CAPTURE_FIELDS or context.get("secret_field"):
        return "secret"
    return "sign_in"


def credential_secret_label(context):
    return context.get("password_label") or "密钥或凭证内容"


DECISIONS = {"allow_once", "allow_session", "allow_always", "deny_always", "deny"}
DURABLE_SCOPES = {"destination", "destination_domain", "entity", "connector_all_actions", "recipient"}
LABELS = {
    "allow_once": "本次允许", "allow_session": "本次会话允许",
    "allow_always": "始终允许", "deny_always": "始终拒绝", "deny": "拒绝",
    "allow_for_this_task": "本次任务允许",
    "allow_all_web_access_for_this_task": "允许本次任务的所有网页访问",
    "allow_for_this_scheduled_task": "此定时任务允许",
    "allow_for_this_artifact": "此工件允许", "allow_for_this_feed": "此信息流允许",
    "deny_for_this_task": "本次任务拒绝",
    "deny_for_this_scheduled_task": "此定时任务拒绝",
    "deny_for_this_artifact": "此工件拒绝",
}
SCOPE_LABELS = {"destination": "此目标", "destination_domain": "此目标域名",
                "entity": "此实体", "connector_all_actions": "此连接器的全部操作",
                "recipient": "此接收者"}


class ApprovalBlocked(RuntimeError):
    """A user message was not sent or retained because approval is pending."""


def unwrap_response(raw):
    value = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(value, dict) or value.get("ok") is False or "error" in value:
        raise ValueError("Interaction request rejected")
    result = value.get("result", value)
    if not isinstance(result, dict):
        raise ValueError("Invalid interaction result")
    return result


def message_revision(approval):
    revision = approval.get("message_revision")
    if revision is None and approval.get("editable_message_available") is True:
        return 0
    if revision is not None and (type(revision) is not int or not 0 <= revision <= 9007199254740991):
        raise ValueError("Invalid approval message revision")
    return revision


class MuseInteractions:
    def __init__(self, service):
        self.service = service
        self.manager = service.interactions
        self._decision_locks = {}
        self._refresh_lock = asyncio.Lock()
        self._notify_lock = asyncio.Lock()
        self._fetch_lock = threading.RLock()
        # Per-session approval rounds: ids in display order, kept until the
        # session's queue empties so positions read 1/2, 2/2 across decisions.
        self._queue_lock = threading.Lock()
        self._rounds = {}
        self._deciding = {}
        self._capture_lock = threading.RLock()
        self._receipt_path = os.fspath(service.config.cookies_file) + ".captures.local.json"
        self._capture_receipts = set()
        try:
            with open(self._receipt_path, encoding="utf-8") as handle:
                receipts = json.load(handle)
            if (not isinstance(receipts, list) or any(not isinstance(key, str) or len(key) != 64
                    or any(c not in "0123456789abcdef" for c in key) for key in receipts)):
                raise ValueError("Invalid credential receipt store")
            self._capture_receipts.update(receipts)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise ValueError("Cannot read credential receipt store; refusing to reopen captures") from None

    def _capture_key(self, session_id, remote_id, context):
        vm_id = getattr(self.service.token_manager, "vm_id", "")
        if not isinstance(vm_id, str) or not vm_id:
            vm_id = self.service.config.vm_id
        identity = [self.service.config.host, vm_id, session_id, remote_id,
                    context["page_url"], context.get("capture_authorization")]
        if context.get("secret_field") and context.get("capture") != "sign_in":
            identity.extend([context.get("capture"), context["secret_field"]])
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def _record_capture(self, key):
        with self._capture_lock:
            if key in self._capture_receipts:
                return
            receipts = self._capture_receipts | {key}
            directory = os.path.dirname(os.path.abspath(self._receipt_path))
            fd, temporary = tempfile.mkstemp(prefix=".capture-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(sorted(receipts), handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._receipt_path)
                self._capture_receipts = receipts
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def request(self, method, path, body=None, *, service="sentinel"):
        with self.service.create_client() as client:
            raw = client.request(method, path,
                                 json.dumps(body, ensure_ascii=False).encode() if body is not None else b"",
                                 service=service, include_content_type=body is not None)
        return unwrap_response(raw)

    def register_credential(self, event, session_id, owner=None):
        payload = event.get("payload", event)
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return None
        action = data.get("action", data)
        if not isinstance(action, dict) or action.get("type") != "credentials_vault":
            return None
        fields = action.get("data", action)
        if not isinstance(fields, dict):
            return None
        payload = {"message_id": event.get("message_id"), **payload}
        remote_id = next((payload.get(key) for key in ("presentation_id", "widget_id", "id", "message_id")
                          if isinstance(payload.get(key), str) and payload[key]), None)
        if not remote_id or not session_id:
            return None
        existing = self.manager.by_remote("credential", remote_id)
        if existing and existing.session_id != session_id:
            return None
        state = payload.get("state") or {}
        if not isinstance(state, dict):
            return None
        version = state.get("version", 0)
        if type(version) is not int or version < 0:
            return None
        if existing and version < existing.context.get("state_version", 0):
            return existing
        state_data = state.get("data") or {}
        if not isinstance(state_data, dict):
            return None
        if state_data.get("credential_capture_completed") is True:
            if existing:
                existing.context["state_version"] = version
                self.manager.finish(existing)
            return existing
        if existing:
            # Never resurrect a completed/cancelled capture from a replayed card.
            existing.context["state_version"] = version
            return existing
        url = fields.get("url")
        if not isinstance(url, str):
            return None
        try:
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname not in {"agent.meta.ai", "muse.ai"}
                    or parsed.path.rstrip("/") != "/connect/browser-credentials"
                    or parsed.username or parsed.password or parsed.query
                    or parsed.port not in (None, 443)):
                return None
        except ValueError:
            return None
        params = parse_qs(parsed.fragment, keep_blank_values=True)
        keys = ("page_url", "capture_authorization", "username_label", "password_label", "verification_code_label", "capture")
        if any(len(values) != 1 for values in params.values()):
            return None
        context = {}
        for key in keys:
            values = params.get(key, [])
            # Capture often exists only in the structured card, not its URL.
            value = values[0] if values else (fields.get(key, "sign_in") if key == "capture" else None)
            if key in fields and fields[key] != value:
                return None
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return None
            context[key] = value
        try:
            page = urlparse(context.get("page_url") or "")
            if page.scheme not in {"http", "https"} or not page.hostname or page.username or page.password:
                return None
        except ValueError:
            return None
        capture = context["capture"]
        if capture not in {"sign_in", "verification_code", *SECRET_CAPTURE_FIELDS}:
            return None
        secret_field = SECRET_CAPTURE_FIELDS.get(capture)
        if capture == "sign_in" and not context["username_label"] and context["password_label"]:
            secret_field = "password"
        # Authorized login/code cards keep the existing fail-closed requirement.
        # Legacy single-secret cards may omit authorization; the authenticated
        # authd capture API accepts them with the explicit session context.
        if not context["capture_authorization"] and capture != "create_password":
            return None
        if secret_field and (context["username_label"] or context["verification_code_label"]):
            return None
        providers = fields.get("auth_providers") or []
        if not isinstance(providers, list) or any(p not in {"google", "apple", "amazon", "1password"} for p in providers):
            return None
        if providers and (capture != "sign_in" or secret_field):
            return None
        context.update(capture=capture, auth_providers=providers, secret_field=secret_field,
                       grant_request_type=fields.get("grant_request_type"),
                       idempotency_key=str(uuid.uuid4()), state_version=version,
                       lifetime="on_read" if capture == "verification_code" else "persist")
        receipt_key = self._capture_key(session_id, remote_id, context)
        if receipt_key in self._capture_receipts:
            return self.manager.add("credential", owner=owner, session_id=session_id, remote_id=remote_id,
                                    state="completed", ttl=0,
                                    context={"page_url": context["page_url"], "state_version": version})
        context["receipt_key"] = receipt_key
        return self.manager.add("credential", owner=owner, session_id=session_id, remote_id=remote_id, ttl=0, context=context)

    def capture_credential(self, item, secret):
        if item.kind != "credential" or item.terminal:
            raise ValueError("Credential capture is no longer active")
        c = item.context
        if c.get("auth_providers") or c.get("grant_request_type"):
            raise ValueError("Provider capture must be completed in the web client")
        page = urlparse(c["page_url"])
        kind = credential_input_kind(c)
        verification = kind == "code"
        if kind == "secret":
            field_name = c.get("secret_field") or SECRET_CAPTURE_FIELDS[c["capture"]]
            fields = [{"name": field_name, "value": secret, "protected": True}]
        elif verification:
            fields = [{"name": "verification_code", "value": secret, "protected": True}]
        else:
            fields = [{"name": "username", "value": c["username"], "protected": True},
                      {"name": "password", "value": secret, "protected": True}]
        body = {"page_url": page._replace(path=page.path or "/").geturl(),
                "idempotency_key": c["idempotency_key"], "label": None, "fields": fields,
                "lifetime": "on_read" if verification else c["lifetime"],
                "session_id": item.session_id}
        if c.get("capture_authorization"):
            body["capture_authorization"] = c["capture_authorization"]
        digest = hashlib.sha256((c["idempotency_key"] + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        previous_digest = c.get("submitted_digest")
        if previous_digest is not None and previous_digest != digest:
            for field in fields:
                field["value"] = ""
            raise ValueError("An uncertain capture can only retry identical inputs")
        c["submitted_digest"] = digest
        try:
            result = self.request("POST", "/v1/credentials/capture", body, service="authd")
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise ValueError("Invalid capture response")
            self._record_capture(c["receipt_key"])
            trace_event("muse.interactions", "credential.captured", session_id=item.session_id,
                        interaction_id=item.id, state="completed")
            return result["id"]
        finally:
            for field in fields:
                field["value"] = ""

    @staticmethod
    def approval_session(row):
        scope = row.get("scope") or {}
        trace = scope.get("runtime_request_trace") or row.get("runtime_request_trace") or {}
        return scope.get("thread_id") or row.get("thread_id") or trace.get("thread_id") or ""

    @staticmethod
    def merge_approval(row, detail):
        full = {**row, **detail}
        # Sentinel snapshot contains scoped objects; the legacy detail endpoint
        # may contain decision strings only. Never replace scoped objects with
        # that lossy representation (including an explicitly empty snapshot).
        if "decision_options" in row:
            full["decision_options"] = row["decision_options"]
        return full

    @staticmethod
    def legacy_options(approval):
        # Source verified: UI module 11193 and parser e9/e1. Feed flags are
        # deliberately NOT aliases for the artifact-reader capability.
        payload = approval.get("payload")
        structured = isinstance(payload, dict)
        payload = payload or {}
        payload_type = payload.get("type")
        accepted = {"browser_task_confirmation", "browser_checkout", "stripe_link_checkout",
                    "shopify_checkout", "outgoing_media", "browser_action", "network", "device", "connector"}
        if structured and payload_type not in accepted:
            return []
        network = payload.get("network_payload") or {}
        connector_data = payload.get("connector_payload") or {}
        source = (network or connector_data or payload.get("device_payload") or {}) if structured else approval
        question = (approval.get("display") or {}).get("permission_question") or {}
        reader = (network.get("space_reader_grant_eligible") is True or question.get("kind") == "space_reader_web_read") if structured else approval.get("space_reader_grant_eligible") is True
        cron = question.get("kind") == "cron_web_read" if structured else approval.get("cron_web_grant_eligible") is True
        scheme = source.get("scheme") or ("connector" if structured and source.get("port") == 0 else "https")
        device = payload_type == "device" or str(scheme).lower() == "device"
        connector = not (payload_type == "browser_task_confirmation" or device) and (scheme == "connector" or source.get("port") == 0)
        scope = "entity" if reader or device or connector else "destination_domain"
        def make(kind, text, durable_scope=None):
            return {"label": text, "decision": kind, "scope": durable_scope,
                    "native": {"kind": kind, "legacy": True, "scope": durable_scope}}
        if reader:
            return [make("allow_always", "允许此工件", "entity"),
                    make("deny_always", "拒绝此工件", "entity")]
        turn = source.get("turn_grant_eligible") is True
        once_label = "允许此定时任务" if cron else ("允许本次任务的网页访问" if turn and not connector else "本次允许")
        options = [make("allow_once", once_label)]
        if not cron:
            options.append(make("allow_always", "始终允许（" + SCOPE_LABELS[scope] + "）", scope))
        options.append(make("deny", "拒绝"))
        return options

    @staticmethod
    def approval_options(approval):
        # Structured options, including empty/invalid lists, are authoritative.
        if "decision_options" not in approval:
            return MuseInteractions.legacy_options(approval)
        native = approval["decision_options"]
        if not isinstance(native, list):
            return []
        options = []
        for action in native:
            if not isinstance(action, dict) or action.get("kind") not in DECISIONS:
                return []
            label = action.get("label")
            if not isinstance(label, dict) or not isinstance(label.get("kind"), str):
                return []
            kind = action["kind"]
            scope = label.get("scope")
            if label["kind"] in {"allow_always_scoped", "deny_always_scoped"}:
                if scope not in DURABLE_SCOPES or kind not in {"allow_always", "deny_always"}:
                    return []
            elif scope is not None:
                return []
            text = action.get("label_text")
            if text is not None and not isinstance(text, str):
                return []
            if not text:
                text = LABELS.get(label["kind"])
                if not text and scope:
                    text = ("始终允许" if kind == "allow_always" else "始终拒绝") + SCOPE_LABELS[scope]
                text = text or kind
            options.append({"label": text, "decision": kind, "scope": scope, "native": action})
        return options

    @staticmethod
    def approval_fingerprint(approval):
        # Exclude timestamps/runtime trace; bind a choice to displayed content,
        # full native choices, and the server's editable-message revision.
        data = {key: approval.get(key) for key in ("display", "payload", "payload_preview", "decision_options")}
        data["message_revision"] = message_revision(approval)
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def upsert_approval(self, row, detail=None):
        if not isinstance(row, dict):
            raise ValueError("Invalid approval record")
        full = self.merge_approval(row, detail or {})
        remote_id = full.get("approval_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError("Approval identifier missing")
        item = self.manager.by_remote("approval", remote_id)
        status = full.get("status")
        if status in {"approved", "denied", "timed_out"}:
            if item:
                self.manager.finish(item, status)
            return item
        if status != "pending":
            raise ValueError("Approval status missing")
        sid = self.approval_session(full) or (item.session_id if item else "")
        if not sid or sid not in self.manager.managed_sessions:
            return None
        if item and (item.terminal or item.session_id != sid):
            return item
        item = item or self.manager.add("approval", session_id=sid, remote_id=remote_id, state="pending", ttl=0)
        fingerprint = self.approval_fingerprint(full)
        if item.context.get("fingerprint") != fingerprint:
            item.context["notified"] = False
        item.context.update(approval=full, options=self.approval_options(full), fingerprint=fingerprint)
        item.state = "pending"
        return item

    def fetch_approvals(self):
        with self._fetch_lock:
            try:
                snapshot = self.request("GET", "/approvals")
                pending = snapshot.get("pending_approvals", snapshot.get("pending"))
                recent = snapshot.get("recent_approvals", snapshot.get("recent", []))
                if not isinstance(pending, list) or not isinstance(recent, list):
                    raise ValueError("Invalid approval snapshot")
                active = set()
                for row in pending:
                    if not isinstance(row, dict):
                        raise ValueError("Invalid pending approval")
                    remote_id = row.get("approval_id")
                    sid = self.approval_session(row)
                    if sid and sid not in self.manager.managed_sessions:
                        continue
                    if not isinstance(remote_id, str) or not remote_id:
                        raise ValueError("Approval identifier missing")
                    active.add(remote_id)
                    # Establish the gate even if the detail request fails.
                    if sid and self.manager.by_remote("approval", remote_id) is None:
                        self.upsert_approval(row)
                    result = self.request("GET", f"/approvals/{quote(remote_id, safe='')}")
                    detail = result.get("approval", result)
                    item = self.upsert_approval(row, detail)
                    if not sid and item is None and not self.approval_session(detail):
                        raise ValueError("Unscoped pending approval")
                for row in recent:
                    # Recent pending rows can lag behind the active snapshot.
                    if row.get("status") in {"approved", "denied", "timed_out"}:
                        self.upsert_approval(row)
                for item in self.manager.pending_approvals():
                    if item.remote_id not in active:
                        result = self.request("GET", f"/approvals/{quote(item.remote_id, safe='')}")
                        self.upsert_approval(result.get("approval", result))
                self.manager.approvals_ready = True
                return self.manager.pending_approvals()
            except Exception:
                self.manager.approvals_ready = False
                raise

    async def reconcile(self):
        async with self._refresh_lock:
            return await asyncio.to_thread(self.fetch_approvals)

    @staticmethod
    def _requested_at(item):
        a = item.context.get("approval") or {}
        for source in (a, a.get("runtime_request_trace") or {}):
            value = source.get("requested_at_ms") if isinstance(source, dict) else None
            if type(value) is int:
                return value
        return float("inf")

    def approval_queue(self, session_id):
        """Pending approvals of one session in stable display order; the head is shown."""
        pending = sorted(self.manager.pending_approvals(session_id), key=self._requested_at)
        with self._queue_lock:
            if not pending:
                self._rounds.pop(session_id, None)
                return []
            order = self._rounds.setdefault(session_id, [])
            order.extend(i.id for i in pending if i.id not in order)
            rank = {key: n for n, key in enumerate(order)}
            return sorted(pending, key=lambda i: rank[i.id])

    def queue_heads(self):
        sessions = list(dict.fromkeys(i.session_id for i in self.manager.pending_approvals()))
        with self._queue_lock:
            for sid in [s for s in self._rounds if s not in sessions]:
                del self._rounds[sid]
        return [queue[0] for queue in map(self.approval_queue, sessions) if queue]

    def queue_position(self, item):
        """(position, round size, still pending) for the card header and hints."""
        queue = self.approval_queue(item.session_id)
        with self._queue_lock:
            order = self._rounds.get(item.session_id, [])
            if item.id not in order:
                return 1, 1, len(queue)
            return order.index(item.id) + 1, len(order), len(queue)

    def display_snapshot(self, item):
        """Taken before sending: what the user will see, to bind later choices."""
        return (item.context.get("fingerprint"),
                [(i.id, i.context.get("fingerprint")) for i in self.approval_queue(item.session_id)])

    def mark_displayed(self, item, target, snapshot):
        fingerprint, queue = snapshot
        item.context.setdefault("displayed_to", {})[target] = fingerprint
        item.context.setdefault("queue_shown", {})[target] = queue

    @contextlib.contextmanager
    def deciding(self, session_id):
        """Hold back background pushes while a chat decides and advances this queue."""
        with self._queue_lock:
            self._deciding[session_id] = self._deciding.get(session_id, 0) + 1
        try:
            yield
        finally:
            with self._queue_lock:
                left = self._deciding.get(session_id, 1) - 1
                if left:
                    self._deciding[session_id] = left
                else:
                    self._deciding.pop(session_id, None)

    async def announce(self, item, notify):
        """Push a queue head once; concurrent callers never send it twice."""
        async with self._notify_lock:
            if item.terminal or item.context.get("notified"):
                return False
            delivered = await notify(item, self.format_approval(item))
            if delivered:
                item.context["notified"] = True
            return delivered

    def format_approval(self, item):
        a = item.context.get("approval") or {}
        d = a.get("display") or {}
        position, total, waiting = self.queue_position(item)
        lines = [f"【审批 {item.id}】{position}/{total}",
                 d.get("summary_title") or a.get("approval_phrase") or "待审批操作",
                 d.get("purpose_summary") or a.get("short_explanation") or ""]
        document = d.get("request_preview_document") or {}
        for block in document.get("blocks", []):
            if block.get("type") == "fields":
                lines.extend(f"{r.get('label') or r.get('key')}: {r.get('value', '')}" for r in block.get("rows", []))
            elif block.get("type") == "text":
                lines.append(str(block.get("text", "")))
        if not document:
            connector = (a.get("payload") or {}).get("connector_payload") or {}
            lines.append(a.get("payload_preview") or connector.get("payload_preview") or "")
        options = item.context.get("options", [])
        lines.extend(f"选项 {n}｜{o['label']}" for n, o in enumerate(options, 1))
        if not options:
            lines.append("服务端未提供可验证的原生选项，请在 Muse 网页处理；")
        lines.append(f"可直接回复数字审批，也可使用 /审批 {item.id} <数字> ")
        if waiting > 1:
            lines.append(f"回复「<数字> all」或 /审批 all <数字>，对排队中的 {waiting} 项全部使用该选项")
        lines.append("完成审批以继续聊天")
        return "\n".join(line for line in lines if line)

    async def submit_approval(self, item, choice, *, expected_fingerprint=None, automatic=False):
        lock = self._decision_locks.setdefault(item.remote_id, asyncio.Lock())
        async with lock:
            if item.terminal:
                return "此审批已结束，请重新发送需要继续的消息。"
            if item.context.get("decision_uncertain"):
                result = await asyncio.to_thread(self.request, "GET", f"/approvals/{quote(item.remote_id, safe='')}")
                self.upsert_approval(result.get("approval", result))
                return ("审批已由远端确认结束。" if item.terminal else
                        "上次提交结果仍不确定，不会重复发送决定；请在 Muse 网页核对。")
            shown = expected_fingerprint or item.context.get("fingerprint")
            old_options = item.context.get("options", [])
            if str(choice).isdigit():
                index = int(choice) - 1
                option = old_options[index] if 0 <= index < len(old_options) else None
            else:
                matches = [o for o in old_options if o["decision"] == choice and not o.get("scope")]
                option = matches[0] if len(matches) == 1 else None
            if option is None:
                return "请选择当前审批卡实际提供的选项。\n" + self.format_approval(item)
            # The snapshot carries scoped option objects; detail may only carry
            # strings. Reconcile both before interpreting the displayed choice.
            await asyncio.to_thread(self.fetch_approvals)
            fresh = item.context["approval"]
            if item.terminal:
                return "审批已在其他客户端结束，请重新发送需要继续的消息。"
            if fresh.get("status") != "pending":
                raise ValueError("Approval state unconfirmed")
            if not automatic and item.context["fingerprint"] != shown:
                return "审批内容或选项已变化，请按新卡重新选择。\n" + self.format_approval(item)
            if option not in item.context["options"]:
                return "可用审批选项已变化，请重新选择。\n" + self.format_approval(item)
            body = {"decision": option["decision"], "notification_channel": "ui"}
            revision = message_revision(fresh)
            if revision is not None:
                body["expected_message_revision"] = revision
            if option["decision"] in {"allow_always", "deny_always"} and option.get("scope"):
                body["always_scope"] = option["scope"]
                if option["decision"] == "allow_always":
                    body["allow_always_scope"] = option["scope"]
            item.state = "submitting"
            try:
                result = await asyncio.to_thread(self.request, "POST", f"/approvals/{quote(item.remote_id, safe='')}/decide", body)
                self.upsert_approval(result.get("approval", result))
                if not item.terminal:
                    raise ValueError("Approval completion not confirmed")
            except Exception:
                # Never replay a decision whose response may have been lost.
                try:
                    result = await asyncio.to_thread(self.request, "GET", f"/approvals/{quote(item.remote_id, safe='')}")
                    self.upsert_approval(result.get("approval", result))
                except Exception:
                    pass
                if not item.terminal:
                    item.state = "unknown"
                    item.context["decision_uncertain"] = True
                    return "审批结果未确认，仍保持门禁；请先在 Muse 网页核对；"
            trace_event("muse.interactions", "approval.resolved", session_id=item.session_id,
                        interaction_id=item.id, state=item.state)
            return f"【审批 {item.id}】{option['label']}；服务端状态：{item.state}。"

    async def run(self, notify, running):
        """Use explicit sentinel reconciliation; snapshot stream origin is unverified."""
        while running():
            try:
                pending = await self.reconcile()
                for item in pending:
                    policy = self.service.config.approval
                    if policy.mode != "manual" and not item.context.get("decision_uncertain"):
                        choice = "allow_once" if policy.mode == "allow_all" else "deny"
                        if any(o["decision"] == choice and not o.get("scope") for o in item.context["options"]):
                            text = await self.submit_approval(item, choice, automatic=True)
                            if policy.notify and item.terminal:
                                await notify(item, text)
                            continue
                # One card per session at a time; the next is pushed once the head ends.
                for head in self.queue_heads():
                    if head.session_id not in self._deciding:
                        await self.announce(head, notify)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.manager.approvals_ready = False
                logger.warning("Approval reconciliation unavailable; pending gates retained. error=%s", safe_error(e))
            await asyncio.sleep(3)
