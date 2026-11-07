"""Verified Muse interaction protocol and safe failure behavior."""

import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

from muse_bridge.config.models import ApprovalConfig, MuseConfig
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.interactions import ApprovalBlocked, MuseInteractions, message_revision, unwrap_response
from muse_bridge.muse.client.protocol import http_request_envelope, pb_parse


def option(kind, scope=None, label=None):
    return {"kind": kind, "label": {"kind": "allow_always_scoped" if scope else kind,
                                       **({"scope": scope} if scope else {})},
            "label_text": label or kind}


def approval(**overrides):
    return {"approval_id": "remote-1", "status": "pending", "scope": {"thread_id": "s1"},
            "display": {"summary_title": "Send test"}, "payload_preview": "test body",
            "decision_options": [option("allow_once"), option("allow_always", "recipient"),
                                 option("allow_always", "connector_all_actions"), option("deny")],
            **overrides}


def credential_event(version=1, completed=False, capture="sign_in", **fields):
    context = {"page_url": "https://example.test", "capture_authorization": "fictional-authorization",
               "username_label": "Account", "password_label": "Password", "capture": capture}
    url = "https://agent.meta.ai/connect/browser-credentials#" + urlencode(context)
    return {"event_name": "delta.presentation", "message_id": "message-1", "payload": {
        "presentation_id": "card-1", "kind": "internal_link_action_card",
        "data": {"type": "credentials_vault", "title": "Sign in", "data": {"url": url, **fields}},
        "state": {"version": version, "data": {"credential_capture_completed": completed}}}}


class ProtocolCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = MuseConfig(session_id="s1", cookies_file=str(Path(temporary.name) / "cookies.json"))
        self.service = MuseService(self.config, MagicMock())
        self.p = self.service.interaction_protocol
        self.p.request = MagicMock()

    def test_unwrap(self):
        self.assertEqual(unwrap_response(b'{"ok":true,"result":{"id":"capture-id"}}'), {"id": "capture-id"})
        self.assertEqual(unwrap_response({"ok": True}), {"ok": True})
        for raw in ({"ok": False}, {"error": "denied"}, [], b"not json"):
            with self.assertRaises((ValueError, TypeError)):
                unwrap_response(raw)

    def test_service_selector_preserves_default_wire(self):
        for name, value in (("daemon", 0), ("sentinel", 1), ("authd", 3)):
            envelope = pb_parse(http_request_envelope(7, "GET", "/approvals", service=name))
            self.assertEqual(envelope.get(1, [(0, 0)])[0][1], value)
            frame = pb_parse(envelope[2][0][1])
            self.assertEqual(frame[1][0][1], 7)
        with self.assertRaises(ValueError):
            http_request_envelope(1, "GET", "/", service="invented")

    def test_native_order_and_same_kind_scopes_are_preserved(self):
        native = approval()["decision_options"]
        choices = self.p.approval_options({"decision_options": native})
        self.assertEqual([x["native"] for x in choices], native)
        self.assertEqual([x["scope"] for x in choices], [None, "recipient", "connector_all_actions", None])

    def test_approval_numbers_are_literal_not_markdown_lists(self):
        item = self.p.upsert_approval(approval())
        text = self.p.format_approval(item)
        self.assertIn("选项 1｜allow_once", text)
        self.assertIn("选项 2｜allow_always", text)
        self.assertIn("选项 4｜deny", text)
        self.assertLess(text.index("选项 2｜"), text.index("选项 3｜"))

    def test_empty_native_choices_never_invent_permissions(self):
        for extra in ({"decision_options": []}, {"decision_options": "bad"}):
            row = {"recipient_send_prepared": True, "always_scope": "recipient", "turn_grant_eligible": True, **extra}
            self.assertEqual(self.p.approval_options(row), [])

    def test_legacy_fallback_matches_web_scope_not_recipient_flag(self):
        row = {"scheme": "connector", "port": 0, "recipient_send_prepared": True}
        choices = self.p.approval_options(row)
        self.assertEqual([o["decision"] for o in choices], ["allow_once", "allow_always", "deny"])
        self.assertEqual(choices[1]["scope"], "entity")
        row = {"payload": {"type": "network", "network_payload": {"space_reader_grant_eligible": True}}}
        self.assertEqual([o["decision"] for o in self.p.approval_options(row)], ["allow_always", "deny_always"])
        row["decision_options"] = []
        self.assertEqual(self.p.approval_options(row), [])

    def test_snapshot_scopes_survive_lossy_detail_strings(self):
        row = approval()
        detail = {"approval_id": "remote-1", "status": "pending",
                  "decision_options": ["allow_once", "allow_always", "deny"]}
        item = self.p.upsert_approval(row, detail)
        self.assertEqual(item.context["options"][1]["scope"], "recipient")
        self.assertEqual(item.context["options"][2]["scope"], "connector_all_actions")

    def test_live_card_default_capture_can_be_omitted_from_url(self):
        e = credential_event(capture="sign_in")
        fields = e["payload"]["data"]["data"]
        fields["capture"] = "sign_in"
        fields["url"] = fields["url"].replace("&capture=sign_in", "")
        self.assertIsNotNone(self.p.register_credential(e, "s1"))

    def test_invalid_scope_rejects_entire_native_option_set(self):
        native = [option("allow_once"), option("allow_always", "global")]
        self.assertEqual(self.p.approval_options({"decision_options": native}), [])

    def test_label_kind_does_not_replace_decision_kind(self):
        row = {"decision_options": [{"kind": "allow_session", "label": {"kind": "allow_for_this_task"}}]}
        self.assertEqual(self.p.approval_options(row)[0]["decision"], "allow_session")

    def test_revision_validation(self):
        self.assertIsNone(message_revision({}))
        self.assertEqual(message_revision({"editable_message_available": True}), 0)
        for value in (-1, True, 1.5, "1", 9007199254740992):
            with self.assertRaises(ValueError):
                message_revision({"message_revision": value})

    def test_reconcile_retains_unknown_pending_not_in_bounded_history(self):
        item = self.p.upsert_approval(approval())
        self.p.request.side_effect = [{"pending": [], "recent": []}, approval()]
        self.p.fetch_approvals()
        self.assertFalse(item.terminal)
        self.assertEqual(self.p.request.call_args.args[:2], ("GET", "/approvals/remote-1"))

    def test_reconcile_restores_pending_after_restart(self):
        row = approval()
        self.p.request.side_effect = [{"pending": [row], "recent": []}, row]
        self.p.fetch_approvals()
        self.assertEqual(len(self.service.interactions.pending_approvals("s1")), 1)
        self.assertTrue(self.service.interactions.approvals_ready)

    def test_other_session_not_imported(self):
        row = approval(scope={"thread_id": "unmanaged"})
        self.p.request.return_value = {"pending": [row], "recent": []}
        self.assertEqual(self.p.fetch_approvals(), [])

    def test_readiness_failure_does_not_clear_existing_gate(self):
        item = self.p.upsert_approval(approval())
        self.p.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.p.fetch_approvals()
        self.assertFalse(self.service.interactions.approvals_ready)
        self.assertFalse(item.terminal)

    def test_credential_raw_card_dedup_and_no_focus(self):
        item = self.p.register_credential(credential_event(), "s1")
        self.assertIsNotNone(item)
        key = item.context["idempotency_key"]
        self.assertIs(self.p.register_credential(credential_event(), "s1"), item)
        self.assertEqual(key, item.context["idempotency_key"])
        self.assertEqual(self.service.interactions.focus, {})
        self.assertIsNone(self.p.register_credential(credential_event(), "s2"))

    def test_credential_versioned_completion_and_empty_url(self):
        item = self.p.register_credential(credential_event(), "s1")
        e = credential_event(version=2, completed=True, url="")
        self.p.register_credential(e, "s1")
        self.assertTrue(item.terminal)
        self.assertNotIn("capture_authorization", item.context)
        self.p.register_credential(credential_event(version=1), "s1")
        self.assertTrue(item.terminal)
        self.assertEqual(item.context["state_version"], 2)

    def test_mismatched_credential_context_rejected(self):
        e = credential_event(page_url="https://unrelated.test/")
        self.assertIsNone(self.p.register_credential(e, "s1"))

    def test_capture_routes_authd_and_retains_idempotency(self):
        item = self.p.register_credential(credential_event(), "s1")
        item.context["username"] = "fictional-user"
        item.context["lifetime"] = "on_read"
        bodies = []
        def request(method, path, body, **kwargs):
            bodies.append(copy.deepcopy(body))
            self.assertEqual(kwargs["service"], "authd")
            return {"id": "capture-id-not-card-id"}
        self.p.request.side_effect = request
        self.p.capture_credential(item, "fictional-secret")
        self.p.capture_credential(item, "fictional-secret")
        self.assertEqual(bodies[0]["idempotency_key"], bodies[1]["idempotency_key"])
        self.assertEqual(bodies[0]["page_url"], "https://example.test/")
        self.assertEqual(bodies[0]["lifetime"], "on_read")
        self.assertNotIn("password", item.context)

    def test_capture_receipt_survives_restart_without_secrets(self):
        item = self.p.register_credential(credential_event(), "s1")
        item.context["username"] = "fictional-user"
        self.p.request.return_value = {"id": "capture-id"}
        self.p.capture_credential(item, "fictional-secret")
        saved = Path(self.p._receipt_path).read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved), [item.context["receipt_key"]])
        for secret in ("fictional-user", "fictional-secret", "fictional-authorization", "capture-id"):
            self.assertNotIn(secret, saved)
        restored = MuseService(self.config, MagicMock()).interaction_protocol
        restored_item = restored.register_credential(credential_event(), "s1")
        self.assertEqual(restored_item.state, "completed")
        self.assertNotIn("capture_authorization", restored_item.context)
        renewed = credential_event()
        fields = renewed["payload"]["data"]["data"]
        fields["url"] = fields["url"].replace("fictional-authorization", "new-authorization")
        another_restart = MuseService(self.config, MagicMock()).interaction_protocol
        self.assertFalse(another_restart.register_credential(renewed, "s1").terminal)

    def test_failed_capture_does_not_record_completion(self):
        item = self.p.register_credential(credential_event(), "s1")
        item.context["username"] = "fictional-user"
        self.p.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.p.capture_credential(item, "fictional-secret")
        self.assertFalse(Path(self.p._receipt_path).exists())
        restored = MuseService(self.config, MagicMock()).interaction_protocol
        self.assertFalse(restored.register_credential(credential_event(), "s1").terminal)

    def test_receipt_write_failure_is_not_reported_as_success(self):
        item = self.p.register_credential(credential_event(), "s1")
        item.context["username"] = "fictional-user"
        self.p.request.return_value = {"id": "capture-id"}
        with unittest.mock.patch("muse_bridge.muse.interactions.os.replace", side_effect=OSError()):
            with self.assertRaises(OSError):
                self.p.capture_credential(item, "fictional-secret")
        self.assertFalse(self.p._capture_receipts)
        self.assertFalse(Path(self.p._receipt_path).exists())
        self.assertFalse(list(Path(self.p._receipt_path).parent.glob(".capture-*.tmp")))

    def test_corrupt_receipt_store_does_not_reopen_cards(self):
        Path(self.p._receipt_path).write_text("invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read credential receipt store"):
            MuseService(self.config, MagicMock())

    def test_verification_code_is_always_on_read(self):
        item = self.p.register_credential(credential_event(capture="verification_code"), "s1")
        bodies = []
        def request(method, path, body, **kwargs):
            bodies.append(copy.deepcopy(body)); return {"id": "capture-2"}
        self.p.request.side_effect = request
        self.p.capture_credential(item, "123456")
        self.assertEqual(bodies[0]["lifetime"], "on_read")
        self.assertEqual(bodies[0]["fields"][0]["name"], "verification_code")

    def test_provider_not_treated_as_password_capture(self):
        item = self.p.register_credential(credential_event(auth_providers=["google"]), "s1")
        with self.assertRaises(ValueError):
            self.p.capture_credential(item, "not-a-provider-token")
        self.p.request.assert_not_called()

    def test_service_gate_prevents_send_and_fails_closed(self):
        self.p.upsert_approval(approval())
        self.p.fetch_approvals = MagicMock()
        client = MagicMock()
        client.__enter__.return_value = client
        self.service.create_client = MagicMock(return_value=client)
        for send in (self.service.post_message, self.service.send_turn):
            with self.assertRaises(ApprovalBlocked):
                send("s1", "must not send")
        client.send_message.assert_not_called()
        self.p.fetch_approvals.side_effect = TimeoutError()
        with self.assertRaises(ApprovalBlocked):
            self.service.post_message("s2", "must not send")
        client.send_message.assert_not_called()


class DecisionCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = MuseConfig(session_id="s1", cookies_file=str(Path(temporary.name) / "cookies.json"))
        self.service = MuseService(self.config, MagicMock())
        self.p = self.service.interaction_protocol
        self.row = approval(message_revision=3)
        self.item = self.p.upsert_approval(self.row)
        self.calls = []
        def request(method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            if path == "/approvals":
                return {"pending": [copy.deepcopy(self.row)], "recent": []}
            if method == "POST":
                return approval(status="approved")
            return copy.deepcopy(self.row)
        self.p.request = MagicMock(side_effect=request)

    async def test_scoped_allow_sends_both_fields(self):
        await self.p.submit_approval(self.item, "2")
        body = [c[2] for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(body, {"decision": "allow_always", "notification_channel": "ui",
                                "always_scope": "recipient", "allow_always_scope": "recipient",
                                "expected_message_revision": 3})

    async def test_changed_revision_requires_new_display(self):
        self.row["message_revision"] = 4
        result = await self.p.submit_approval(self.item, "1")
        self.assertIn("变化", result)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        self.assertFalse(self.item.terminal)

    async def test_simultaneous_admins_submit_once(self):
        await asyncio.gather(self.p.submit_approval(self.item, "1"), self.p.submit_approval(self.item, "1"))
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    async def test_timeout_reconciles_without_replaying(self):
        count = 0
        def request(method, path, body=None, **kwargs):
            nonlocal count
            if path == '/approvals':
                return {"pending": [copy.deepcopy(self.row)], "recent": []}
            if method == "POST":
                count += 1
                raise TimeoutError()
            return approval(status="approved") if count else copy.deepcopy(self.row)
        self.p.request.side_effect = request
        await self.p.submit_approval(self.item, "1")
        self.assertEqual(count, 1)
        self.assertEqual(self.item.state, "approved")

    async def test_uncertain_decision_never_reposts_on_user_retry(self):
        posts = []
        def request(method, path, body=None, **kwargs):
            if path == "/approvals":
                return {"pending": [copy.deepcopy(self.row)], "recent": []}
            if method == "POST":
                posts.append(body)
                raise TimeoutError()
            return copy.deepcopy(self.row)
        self.p.request.side_effect = request
        await self.p.submit_approval(self.item, "1")
        await self.p.submit_approval(self.item, "1")
        self.assertEqual(len(posts), 1)
        self.assertFalse(self.item.terminal)

    async def test_deny_always_sends_no_allow_scope(self):
        native = {"kind": "deny_always", "label": {"kind": "deny_always_scoped", "scope": "entity"}}
        self.row["decision_options"] = [native]
        self.p.upsert_approval(self.row)
        await self.p.submit_approval(self.item, "1")
        body = [c[2] for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(body["always_scope"], "entity")
        self.assertNotIn("allow_always_scope", body)

    async def test_auto_allow_is_not_durable_permission(self):
        self.service.config.approval = ApprovalConfig(mode="allow_all", notify=False)
        self.p.reconcile = AsyncMock(return_value=[self.item])
        running = iter([True, False])
        notify = AsyncMock()
        with unittest.mock.patch("muse_bridge.muse.interactions.asyncio.sleep", new=AsyncMock()):
            await self.p.run(notify, lambda: next(running))
        body = [c[2] for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(body["decision"], "allow_once")
        self.assertNotIn("always_scope", body)
        notify.assert_not_called()

    async def test_auto_deny_sends_deny(self):
        self.service.config.approval = ApprovalConfig(mode="deny_all")
        self.p.reconcile = AsyncMock(return_value=[self.item])
        running = iter([True, False])
        with unittest.mock.patch("muse_bridge.muse.interactions.asyncio.sleep", new=AsyncMock()):
            await self.p.run(AsyncMock(), lambda: next(running))
        body = [c[2] for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(body["decision"], "deny")

    async def test_run_pauses_quietly_when_credentials_missing(self):
        self.service.token_manager.has_credentials.return_value = False
        self.p.reconcile = AsyncMock()
        running = iter([True, False])
        with unittest.mock.patch("muse_bridge.muse.interactions.asyncio.sleep", new=AsyncMock()) as mock_sleep, \
             unittest.mock.patch("muse_bridge.muse.interactions.logger.warning") as mock_warn:
            await self.p.run(AsyncMock(), lambda: next(running))
        self.p.reconcile.assert_not_called()
        mock_warn.assert_not_called()
        self.assertFalse(self.p.manager.approvals_ready)
        mock_sleep.assert_called_with(3)


if __name__ == "__main__":
    unittest.main()
