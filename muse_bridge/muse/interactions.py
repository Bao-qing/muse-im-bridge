"""Muse credential and approval protocol, separate from IM presentation."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, quote, urlparse

from ..core.state_file import atomic_json_write
from .client.errors import LoginRequired, RPCError
from .connector_keys import ConnectorKeyRejected, connector_key_context
from ..observability import get_logger, safe_error, trace_event


logger = get_logger(__name__)


# The one admin-facing login notice, shared by the engine and both gateway
# paths so the wording never drifts apart.
LOGIN_RELOGIN_NOTICE = "Muse 登录已失效。请在此私聊发送：/登录 <您的邮箱>，按提示输入验证码即可恢复。"


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
    if capture == "connector_key":
        return "key"
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
        # Exact sent-card bindings, including terminal tombstones. Never let a
        # replayed quote select a new queue head or a newer card revision.
        self._approval_messages = {}
        self._refresh_lock = asyncio.Lock()
        self._notify_lock = asyncio.Lock()
        self._fetch_lock = threading.RLock()
        # Never hold this state lock across network I/O. Newer events win over
        # an older in-flight Sentinel read, including its failures.
        self._state_lock = threading.RLock()
        self._state_version = 0
        self._stream_generation = 0
        self._stream_seq = -1
        self._stream_emitted_ms = -1
        self._pending_snapshot = {}
        self._last_fetch = None
        self._reconcile_retry_at = 0.0
        self._needs_reconcile = threading.Event()
        self._wake_callback = None
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
            atomic_json_write(self._receipt_path, sorted(receipts))
            self._capture_receipts = receipts

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
        if not isinstance(action, dict):
            return None
        if action.get("type") == "connector":
            return self._register_connector_key(event, payload, data, action, session_id, owner)
        if action.get("type") != "credentials_vault":
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

    def _register_connector_key(self, event, payload, data, action, session_id, owner):
        if not session_id:
            return None
        fields = action.get("data", action)
        if not isinstance(fields, dict):
            return None
        context = connector_key_context(fields, session_id,
                                        agent_id=payload.get("agent_id") or event.get("agent_id"))
        if context is None:
            return None
        remote_id = next((payload.get(key) for key in
                          ("presentation_id", "widget_id", "id", "message_id", "host_message_id")
                          if isinstance(payload.get(key), str) and payload[key]), None)
        remote_id = remote_id or event.get("message_id")
        if not isinstance(remote_id, str) or not remote_id:
            return None
        state = payload.get("state") or {}
        if not isinstance(state, dict):
            return None
        version = state.get("version", 0)
        if type(version) is not int or version < 0:
            return None
        context.update(state_version=version, title=data.get("title") or "连接器",
                       subtitle=data.get("subtitle") or "连接器")
        context["receipt_key"] = self._capture_key(session_id, remote_id, context)
        completed = context["receipt_key"] in self._capture_receipts
        existing = self.manager.by_remote("credential", remote_id)
        if existing:
            if existing.session_id != session_id or existing.context.get("capture") != "connector_key":
                return None
            if existing.terminal or version < existing.context.get("state_version", 0):
                return existing
            changed = any(existing.context.get(key) != context.get(key) for key in
                          ("key_request", "key_endpoint", "key_session_id", "service_name"))
            if changed:
                if version <= existing.context.get("state_version", 0):
                    return existing
                return self.manager.supersede_credential(existing, context,
                                                        state="completed" if completed else "available")
            existing.context["state_version"] = version
            return existing
        return self.manager.add("credential", owner=owner, session_id=session_id,
                                remote_id=remote_id, ttl=0, context=context,
                                state="completed" if completed else "available")

    def register_connector_key(self, item, secret):
        if item.kind != "credential" or item.context.get("capture") != "connector_key" or item.terminal:
            raise ValueError("Connector key input is no longer active")
        if not isinstance(secret, str) or not secret.strip() or "\n" in secret or "\r" in secret:
            raise ValueError("Connector key must be one non-empty line")
        c = item.context
        body = {**c["key_request"], "api_key": {"key": secret.strip()},
                "replace_existing": True}
        if c.get("key_endpoint") == "/api_key_auth/callback":
            body = {"provider": c["service_name"], "key": secret.strip(),
                    "session_id": c["key_session_id"]}
        else:
            body["session_id"] = c["key_session_id"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        try:
            if c.get("submitted_digest") not in (None, digest):
                raise ValueError("An uncertain connector-key submission can only retry identical input")
            c["submitted_digest"] = digest
            # The web client only awaits this RPC; unlike vault capture, its
            # successful response does not require a capture id.
            try:
                self.request("POST", c["key_endpoint"], body, service="authd")
            except RPCError as exc:
                if exc.status in {400, 401, 403, 404, 409, 422}:
                    c.pop("submitted_digest", None)
                    raise ConnectorKeyRejected("Connector key registration rejected") from exc
                raise
            self._record_capture(c["receipt_key"])
            return c["service_name"]
        finally:
            if "api_key" in body:
                body["api_key"]["key"] = ""
            else:
                body["key"] = ""

    def capture_credential(self, item, secret):
        if item.kind != "credential" or item.terminal:
            raise ValueError("Credential capture is no longer active")
        c = item.context
        if credential_input_kind(c) == "key":
            return self.register_connector_key(item, secret)
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
    def is_background_approval(row):
        """Positive server evidence, never guess from a missing thread alone."""
        scope = row.get("scope") or {}
        trace = scope.get("runtime_request_trace") or row.get("runtime_request_trace") or {}
        return ((row.get("display") or {}).get("attention_mode") == "background"
                or (row.get("presentation") or {}).get("attention_mode") == "background"
                or (scope.get("scope_type") or row.get("scope_type")) == "schedule"
                or (scope.get("requester_source") or row.get("requester_source")
                    or trace.get("requester_source")) == "scheduler.cron")

    BACKGROUND_QUEUE = "background:approvals"

    @classmethod
    def queue_id(cls, item):
        # A local queue key, not a Muse thread or an IM session binding.
        return cls.BACKGROUND_QUEUE if item.context.get("background_approval") else item.session_id

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
    def _approval_display_content(display):
        """Normalize only the source annotation observed on push snapshots."""
        if not isinstance(display, dict):
            return display
        purpose, base = display.get("purpose_summary"), display.get("short_explanation")
        if isinstance(base, str) and base and isinstance(purpose, str) and purpose.startswith(base):
            # The list endpoint returns the base explanation; the push endpoint
            # may append this side-chat attribution. Keep every other display
            # field and arbitrary purpose changes in the choice fingerprint.
            suffix = purpose[len(base):]
            if re.fullmatch(r'\. 此任务来自旁聊“[^\r\n]+”。', suffix):
                return {**display, "purpose_summary": base}
        return display

    @staticmethod
    def approval_fingerprint(approval):
        # Bind choices to request/display content and full native options, not
        # the push-only source annotation or discovery of initial revision 0.
        data = {key: approval.get(key) for key in ("display", "payload", "payload_preview", "decision_options")}
        data["display"] = MuseInteractions._approval_display_content(data["display"])
        data["message_revision"] = message_revision(approval) or 0
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
        background = (self.is_background_approval(row) or self.is_background_approval(full)
                      or bool(item and item.context.get("background_approval")))
        sid = "" if background else self.approval_session(full) or (item.session_id if item else "")
        if not background and (not sid or sid not in self.manager.managed_sessions):
            return None
        if item and (item.terminal or item.session_id != sid):
            return item
        item = item or self.manager.add("approval", session_id=sid, remote_id=remote_id, state="pending", ttl=0)
        previous = item.context.get("approval") or {}
        # Snapshot records can omit the version learned from a detail response.
        # Absence is not a version reset; explicit changes still invalidate the
        # displayed choice, and POST keeps the last known server revision.
        known_revision = message_revision(previous)
        if message_revision(full) is None and known_revision is not None:
            full["message_revision"] = known_revision
        old_display, new_display = previous.get("display"), full.get("display")
        old_content = self._approval_display_content(old_display)
        new_content = self._approval_display_content(new_display)
        if (isinstance(old_display, dict) and isinstance(new_display, dict)
                and old_content == new_content and old_display != old_content and new_display == new_content):
            # A less enriched source must not erase the already displayed
            # side-chat hint when all actual display content is identical.
            full["display"] = {**new_display, "purpose_summary": old_display["purpose_summary"]}
        item.context["background_approval"] = background
        fingerprint = self.approval_fingerprint(full)
        if item.context.get("fingerprint") != fingerprint:
            item.context["notified"] = False
        item.context.update(approval=full, options=self.approval_options(full), fingerprint=fingerprint)
        item.state = "pending"
        return item

    def _wake(self):
        if self._wake_callback is not None:
            self._wake_callback()

    def invalidate_approval_state(self):
        with self._state_lock:
            self.manager.approvals_ready = False
            self._state_version += 1
            self._needs_reconcile.set()
        self._wake()

    def begin_approval_stream(self):
        with self._state_lock:
            self._stream_generation += 1
            self._stream_seq = self._stream_emitted_ms = -1
            self.invalidate_approval_state()
            return self._stream_generation

    def end_approval_stream(self, generation):
        with self._state_lock:
            if generation == self._stream_generation:
                self._stream_generation += 1
                self.invalidate_approval_state()

    def check_local_gate(self, session_id):
        """An in-memory read only; unknown state never causes a network send."""
        with self._state_lock:
            self.manager.managed_sessions.add(session_id)
            if not self.manager.approvals_ready:
                raise ApprovalBlocked("暂时无法确认审批状态，这条消息没有发送出去，请稍后重发。")
            # A binding may be created after startup. Keep account-wide rows so
            # a previously unmanaged session cannot miss its existing gate.
            for row in self._pending_snapshot.values():
                if self.approval_session(row) == session_id:
                    self.upsert_approval(row)
            return self.approval_queue(session_id)

    @staticmethod
    def _snapshot_lists(snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid approval snapshot")
        pending = snapshot.get("pending_approvals", snapshot.get("pending"))
        recent = snapshot.get("recent_approvals", snapshot.get("recent", []))
        if not isinstance(pending, list) or not isinstance(recent, list):
            raise ValueError("Invalid approval snapshot")
        ids = set()
        for row in pending + recent:
            if (not isinstance(row, dict) or not isinstance(row.get("approval_id"), str)
                    or not row["approval_id"] or row.get("status") not in
                    {"pending", "approved", "denied", "timed_out"}):
                raise ValueError("Invalid approval record")
        for row in pending:
            if row["approval_id"] in ids:
                raise ValueError("Duplicate pending approval")
            ids.add(row["approval_id"])
        # Some Sentinel responses retain resolved rows in the pending array.
        # Their explicit terminal status is authoritative, not their container.
        recent = [*recent, *(row for row in pending if row["status"] != "pending")]
        return [row for row in pending if row["status"] == "pending"], recent

    def _commit_approval_snapshot(self, pending, recent):
        """Caller holds the state lock. Never guess a missing row's decision."""
        for row in pending + recent:
            sid = self.approval_session(row)
            if not isinstance(sid, str):
                raise ValueError("Invalid approval session")
            if row["status"] == "pending" and not sid and not self.is_background_approval(row):
                raise ValueError("Unscoped pending approval")
            self.approval_options(row)
            self.approval_fingerprint(row)
        self._pending_snapshot = {row["approval_id"]: copy.deepcopy(row) for row in pending}
        active = set(self._pending_snapshot)
        for row in pending:
            self.upsert_approval(row)
        for row in recent:
            if row["approval_id"] not in active and row["status"] != "pending":
                self.upsert_approval(row)
        # The recent list is bounded. Missing != approved: retain that gate and
        # ask the reconciliation worker for its detail, not the inbound sender.
        if any(item.remote_id not in active for item in self.manager.pending_approvals()):
            self._needs_reconcile.set()
        self.manager.approvals_ready = True
        self._state_version += 1

    def accept_approval_event(self, event, generation):
        if not isinstance(event, dict) or event.get("event") != "approvals.snapshot":
            return False
        with self._state_lock:
            if generation != self._stream_generation:
                return False
            try:
                payload = event.get("payload")
                if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                        or payload["schema_version"] != 1):
                    raise ValueError("Unsupported approval snapshot")
                seq, emitted = event.get("seq"), payload.get("emitted_at_ms")
                if type(seq) is not int or seq < 0 or type(emitted) is not int or emitted < 0:
                    raise ValueError("Invalid approval event ordering")
                if seq <= self._stream_seq or emitted < self._stream_emitted_ms:
                    return False
                pending, recent = self._snapshot_lists(payload)
                self._commit_approval_snapshot(pending, recent)
                self._stream_seq, self._stream_emitted_ms = seq, emitted
            except (ValueError, TypeError, AttributeError):
                self.invalidate_approval_state()
                logger.warning("Invalid approval snapshot; waiting for reconciliation.")
                return False
        self._wake()
        return True

    def fetch_approvals(self):
        with self._fetch_lock:
            with self._state_lock:
                version = self._state_version
            try:
                pending, recent = self._snapshot_lists(self.request("GET", "/approvals"))
                with self._state_lock:
                    if version != self._state_version:
                        if self.manager.approvals_ready:
                            self._last_fetch = time.monotonic()
                        return self.manager.pending_approvals()
                active = {row["approval_id"] for row in pending}
                merged = []
                for row in pending:
                    sid = self.approval_session(row)
                    if sid and sid not in self.manager.managed_sessions and not self.is_background_approval(row):
                        merged.append(row)
                        continue
                    # Establish the gate even if the detail request fails.
                    with self._state_lock:
                        if version == self._state_version and sid:
                            self.upsert_approval(row)
                    result = self.request("GET", f"/approvals/{quote(row['approval_id'], safe='')}")
                    full = self.merge_approval(row, result.get("approval", result))
                    if full.get("status") == "pending":
                        merged.append(full)
                    else:
                        recent = [*recent, full]
                for item in self.manager.pending_approvals():
                    if item.remote_id not in active:
                        result = self.request("GET", f"/approvals/{quote(item.remote_id, safe='')}")
                        row = result.get("approval", result)
                        if row.get("status") == "pending":
                            merged.append(row)
                        else:
                            recent = [*recent, row]
                # Validate even detail responses before changing local state.
                self._snapshot_lists({"pending": merged, "recent": recent})
                with self._state_lock:
                    if version == self._state_version:
                        self._commit_approval_snapshot(merged, recent)
                    if self.manager.approvals_ready:
                        self._last_fetch = time.monotonic()
                self._wake()
                return self.manager.pending_approvals()
            except Exception:
                with self._state_lock:
                    if version == self._state_version:
                        self.invalidate_approval_state()
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
        pending = sorted((i for i in self.manager.pending_approvals()
                          if self.queue_id(i) == session_id), key=self._requested_at)
        with self._queue_lock:
            if not pending:
                self._rounds.pop(session_id, None)
                return []
            order = self._rounds.setdefault(session_id, [])
            order.extend(i.id for i in pending if i.id not in order)
            rank = {key: n for n, key in enumerate(order)}
            return sorted(pending, key=lambda i: rank[i.id])

    def queue_heads(self):
        sessions = list(dict.fromkeys(self.queue_id(i) for i in self.manager.pending_approvals()))
        with self._queue_lock:
            for sid in [s for s in self._rounds if s not in sessions]:
                del self._rounds[sid]
        return [queue[0] for queue in map(self.approval_queue, sessions) if queue]

    def queue_position(self, item):
        """(position, round size, still pending) for the card header and hints."""
        queue = self.approval_queue(self.queue_id(item))
        with self._queue_lock:
            order = self._rounds.get(self.queue_id(item), [])
            if item.id not in order:
                return 1, 1, len(queue)
            return order.index(item.id) + 1, len(order), len(queue)

    def display_snapshot(self, item):
        """Taken before sending: what the user will see, to bind later choices."""
        return (item.context.get("fingerprint"),
                [(i.id, i.context.get("fingerprint")) for i in self.approval_queue(self.queue_id(item))])

    def mark_displayed(self, item, target, snapshot):
        target = self.manager.target_key(target)
        fingerprint, queue = snapshot
        item.context.setdefault("displayed_to", {})[target] = fingerprint
        item.context.setdefault("queue_shown", {})[target] = queue

    def remember_approval_message(self, target, body, items, snapshots):
        target = self.manager.target_key(target)
        if not isinstance(body, dict):
            return
        info = body.get("ext_info") or {}
        message_id = body.get("id") or body.get("message_id")
        if type(message_id) is int:
            message_id = str(message_id)
        references = {"id": message_id,
                      "ref": info.get("ref_idx") if isinstance(info, dict) else None}
        bindings = [(item.id, snapshot) for item, snapshot in zip(items, snapshots)]
        for kind, value in references.items():
            if isinstance(value, str) and value:
                self._approval_messages[(*target, kind, value)] = bindings
        while len(self._approval_messages) > 4096:
            self._approval_messages.pop(next(iter(self._approval_messages)))

    def referenced_approvals(self, target, references):
        target = self.manager.target_key(target)
        bindings = [self._approval_messages.get((*target, kind, value))
                    for kind, value in references]
        if not bindings or any(b is None or b != bindings[0] for b in bindings):
            return None
        return bindings[0]

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

    @staticmethod
    def _approval_title(item):
        return f"【后台审批 {item.id}】" if item.context.get("background_approval") else f"【审批 {item.id}】"

    @staticmethod
    def _state_label(state):
        return {"approved": "已允许", "denied": "已拒绝", "expired": "已过期",
                "timed_out": "已过期"}.get(state, state)

    def format_approval(self, item):
        a = item.context.get("approval") or {}
        d = a.get("display") or {}
        position, total, waiting = self.queue_position(item)
        lines = [f"{self._approval_title(item)}{position}/{total}",
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
            lines.append("此审批没有可选项，请在 Muse 网页中处理。")
        if item.context.get("background_approval"):
            lines.append(f"必须引用本卡回复数字，或使用 /审批 {item.id} <数字>")
            if waiting > 1:
                lines.append(f"引用本卡回复「<数字> all」，或发送 /审批 {item.id} <数字> 全部，可一次处理排队中的 {waiting} 项。")
        else:
            lines.append(f"当前会话等待审批时可直接回复数字；其他聊天请引用本卡回复数字，或使用 /审批 {item.id} <数字>")
            if waiting > 1:
                lines.append(f"直接回复「<数字> all」，或发送 /审批 {item.id} <数字> 全部，可一次处理排队中的 {waiting} 项。")
        if not item.context.get("background_approval"):
            lines.append("完成审批以继续聊天")
        return "\n".join(line for line in lines if line)

    async def submit_approval(self, item, choice, *, expected_fingerprint=None, automatic=False):
        lock = self._decision_locks.setdefault(item.remote_id, asyncio.Lock())
        async with lock:
            if item.terminal:
                return f"此审批已结束（{self._state_label(item.state)}），无需重复操作。"
            if automatic and item.context.get("decision_uncertain"):
                return "上次的选择结果未能确认，请回复选项数字重试。"
            shown = expected_fingerprint or item.context.get("fingerprint")
            old_options = item.context.get("options", [])
            if str(choice).isdigit():
                index = int(choice) - 1
                option = old_options[index] if 0 <= index < len(old_options) else None
            else:
                matches = [o for o in old_options if o["decision"] == choice and not o.get("scope")]
                option = matches[0] if len(matches) == 1 else None
            if option is None:
                return "请选择卡片上有的选项。\n" + self.format_approval(item)
            # The snapshot carries scoped option objects; detail may only carry
            # strings. Reconcile both before interpreting the displayed choice.
            await asyncio.to_thread(self.fetch_approvals)
            fresh = item.context["approval"]
            if item.terminal:
                return f"此审批已在其他设备上处理完（{self._state_label(item.state)}），本次选择未提交。"
            if fresh.get("status") != "pending":
                raise ValueError("Approval state unconfirmed")
            if not automatic and item.context["fingerprint"] != shown:
                return "审批内容已变化，请按新卡重新选择。\n" + self.format_approval(item)
            if option not in item.context["options"]:
                return "审批内容已变化，请按新卡重新选择。\n" + self.format_approval(item)
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
                self.upsert_approval(fresh, result.get("approval", result))
                if not item.terminal:
                    raise ValueError("Approval completion not confirmed")
            except Exception as exc:
                logger.warning("Approval decision unconfirmed. interaction_id=%s error=%s", item.id, safe_error(exc))
                # Reconcile once, preserving the snapshot's display and native
                # options when the detail endpoint returns a lossy legacy row.
                # A later explicit user choice may resubmit; the server owns
                # duplicate decisions. Never replay automatically in this call.
                try:
                    result = await asyncio.to_thread(self.request, "GET", f"/approvals/{quote(item.remote_id, safe='')}")
                    self.upsert_approval(fresh, result.get("approval", result))
                except Exception as reconcile_exc:
                    logger.warning("Approval decision reconciliation unavailable. interaction_id=%s error=%s",
                                   item.id, safe_error(reconcile_exc))
                if not item.terminal:
                    item.state = "unknown"
                    item.context["decision_uncertain"] = True
                    return "审批结果未能确认，可再次回复选项数字重试。"
            item.context.pop("decision_uncertain", None)
            trace_event("muse.interactions", "approval.resolved", session_id=item.session_id,
                        interaction_id=item.id, state=item.state)
            return f"{self._approval_title(item)}{option['label']}：状态：{self._state_label(item.state)}，服务端：{item.state}。"

    def reconciliation_due(self):
        return (self._last_fetch is None or self._needs_reconcile.is_set()
                or time.monotonic() - self._last_fetch >= self.service.config.approval.sync_interval_s)

    async def _wait_for_update(self, changed):
        # Notifications retry if no administrator is reachable. This wakeup is
        # NOT the Sentinel polling interval: only reconciliation_due() fetches.
        try:
            await asyncio.wait_for(changed.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
        changed.clear()

    async def _notify_pending(self, notify, pending):
        for item in pending:
            policy = self.service.config.approval
            if policy.mode != "manual" and not item.context.get("decision_uncertain"):
                choice = "allow_once" if policy.mode == "allow_all" else "deny"
                if any(o["decision"] == choice and not o.get("scope") for o in item.context["options"]):
                    text = await self.submit_approval(item, choice, automatic=True)
                    if policy.notify and item.terminal:
                        item.context["auto_notification"] = text
        # Keep results pending if no administrator is reachable yet.
        for item in list(self.manager.items.values()):
            text = item.context.get("auto_notification")
            if text and self.service.config.approval.notify and await notify(item, text):
                item.context.pop("auto_notification", None)
        for head in self.queue_heads():
            if self.queue_id(head) not in self._deciding:
                await self.announce(head, notify)

    async def _run_updates(self, notify, running, changed):
        reconciliation = None
        try:
            while running():
                if not await asyncio.to_thread(self.service.token_manager.has_credentials):
                    if self.manager.approvals_ready:
                        self.invalidate_approval_state()
                    await asyncio.sleep(3)
                    continue
                # Reconcile in a separate task: a slow full snapshot must not
                # delay notifications from a newer event on the owner thread.
                if reconciliation is not None and reconciliation.done():
                    try:
                        reconciliation.result()
                        self._reconcile_retry_at = 0.0
                    except Exception as exc:
                        self._reconcile_retry_at = time.monotonic() + 3
                        logger.warning("Approval reconciliation unavailable; pending gates retained. error=%s", safe_error(exc))
                    reconciliation = None
                if (reconciliation is None and self.reconciliation_due()
                        and time.monotonic() >= self._reconcile_retry_at):
                    self._needs_reconcile.clear()
                    reconciliation = asyncio.create_task(self.reconcile())
                    reconciliation.add_done_callback(lambda _: changed.set())
                try:
                    if self.manager.approvals_ready:
                        await self._notify_pending(notify, self.manager.pending_approvals())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Notification failures do not revoke newer event authority.
                    logger.warning("Approval notification unavailable. error=%s", safe_error(exc))
                await self._wait_for_update(changed)
        finally:
            if reconciliation is not None:
                # to_thread cancellation cannot cancel a synchronous RPC. Drain
                # it before closing the protocol rather than leaking a worker.
                with contextlib.suppress(Exception):
                    await asyncio.shield(reconciliation)

    async def run(self, notify, running):
        """Stream-driven updates plus startup/recovery/periodic Sentinel reads."""
        from .approval_stream import ApprovalEventStream

        loop = asyncio.get_running_loop()
        changed = asyncio.Event()
        self._wake_callback = lambda: loop.call_soon_threadsafe(changed.set)
        stream = ApprovalEventStream(self)
        listener = asyncio.create_task(asyncio.to_thread(stream.run))
        try:
            await self._run_updates(notify, running, changed)
        finally:
            stream.stop.set()
            # Do not cancel to_thread and abandon a live socket. The owner
            # thread closes its client after its bounded read/connect returns.
            try:
                await asyncio.shield(listener)
            finally:
                self._wake_callback = None
