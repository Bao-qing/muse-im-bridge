"""Real connector cards use authd registration, not credentials/capture."""

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import test_credential_replies as replies
import test_interactions as fixtures
from muse_bridge.config.models import MuseConfig
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.connector_keys import connector_key_context


def connector_event(session="s1", **params):
    spec = {"version": 2, "connector": {
        "name": "custom.fictional-mcp", "mode": "api_key", "hosts": ["mcp.fictional.test"],
        "token_proxy": [{"field": "access_token", "placement": "bearer_header"}],
    }}
    encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode().rstrip("=")
    query = {"service": "fictional-mcp", "spec": encoded, "session_id": session, **params}
    return {"event_name": "delta.presentation", "message_id": "connector-message", "role": "assistant",
            "payload": {"presentation_id": "connector-card", "kind": "internal_link_action_card",
                        "data": {"title": "Matlab Mcp", "subtitle": "连接器", "action": {
                            "type": "connector", "connectorId": "fictional-mcp",
                            "url": "https://agent.meta.ai/connect/api-key?" + urlencode(query)}},
                        "state": {"version": 0, "data": {}}}}


class ConnectorProtocolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = MuseConfig(cookies_file=str(Path(self.tmp.name) / "cookies.json"))
        self.service = MuseService(self.config, MagicMock())
        self.protocol = self.service.interaction_protocol
        self.bodies = []
        self.protocol.request = MagicMock(side_effect=lambda *a, **kw: self.bodies.append(copy.deepcopy((a, kw))) or {})

    def test_real_v2_body_and_durable_replay_receipt(self):
        event = connector_event()
        item = self.protocol.register_credential(event, "s1")
        self.assertTrue(item.id.startswith("K"))
        self.protocol.capture_credential(item, "  fictional-single-key  ")
        (method, path, body), kwargs = self.bodies[0]
        self.assertEqual((method, path, kwargs), ("POST", "/v1/custom-connectors/register", {"service": "authd"}))
        self.assertEqual(body, {
            "connector": {"name": "custom.fictional-mcp", "mode": "api_key", "hosts": ["mcp.fictional.test"],
                          "token_proxy": [{"field": "access_token", "placement": "bearer_header"}]},
            "api_key": {"key": "fictional-single-key"}, "replace_existing": True, "session_id": "s1"})
        self.assertNotIn("fictional-single-key", json.dumps(item.context))
        self.assertNotIn("fictional-single-key", Path(self.protocol._receipt_path).read_text())
        restored = MuseService(self.config, MagicMock()).interaction_protocol
        self.assertTrue(restored.register_credential(event, "s1").terminal)

    def test_raw_backend_connector_validates_agent_but_submits_chat_session(self):
        event = connector_event(session="agent-123")
        data = event["payload"]["data"]
        action = data.pop("action")
        data.update(type="connector", data={"connector_id": action["connectorId"], "url": action["url"]})
        event["payload"]["agent_id"] = "agent-123"
        event["payload"]["session_id"] = "s1"
        item = self.protocol.register_credential(event, "s1")
        self.assertIsNotNone(item)
        self.assertEqual(item.session_id, "s1")
        self.protocol.capture_credential(item, "fictional-key")
        self.assertEqual(self.bodies[0][0][2]["session_id"], "s1")
        event["payload"]["agent_id"] = "unrelated-agent"
        self.assertIsNone(self.protocol.register_credential(event, "s1"))

    def test_builtin_agent_link_also_submits_owning_chat_session(self):
        event = connector_event()
        event["agent_id"] = "agent-123"
        event["payload"]["data"]["action"]["url"] = (
            "https://agent.meta.ai/connect/api-key?service=fictional-mcp&session_id=agent-123")
        item = self.protocol.register_credential(event, "s1")
        self.assertIsNotNone(item)
        self.protocol.capture_credential(item, "fictional-key")
        self.assertEqual(self.bodies[0], (("POST", "/api_key_auth/callback", {
            "provider": "fictional-mcp", "key": "fictional-key", "session_id": "s1"}), {"service": "authd"}))

    def test_reject_empty_or_multiline_at_protocol_boundary(self):
        item = self.protocol.register_credential(connector_event(), "s1")
        for value in ("", " ", "one\ntwo", "one\rtwo", "key\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.protocol.capture_credential(item, value)
        self.protocol.request.assert_not_called()

    def test_uncertain_submit_only_retries_same_trimmed_key(self):
        item = self.protocol.register_credential(connector_event(), "s1")
        self.protocol.request.side_effect = OSError("fictional failure")
        with self.assertRaises(OSError):
            self.protocol.capture_credential(item, "fictional-key")
        with self.assertRaises(ValueError):
            self.protocol.capture_credential(item, "different")
        self.assertEqual(self.protocol.request.call_count, 1)
        self.assertFalse(Path(self.protocol._receipt_path).exists())
        self.protocol.request.side_effect = None
        self.protocol.request.return_value = {}
        self.protocol.capture_credential(item, " fictional-key ")
        self.assertTrue(Path(self.protocol._receipt_path).exists())

    def test_invalid_urls_specs_and_session_mismatch_are_not_local_inputs(self):
        action = connector_event()["payload"]["data"]["action"]
        for url in (action["url"].replace("agent.meta.ai", "evil.test"),
                    action["url"] + "&service=other", action["url"].replace("session_id=s1", "session_id=s2"),
                    action["url"].replace("https:", "http:")):
            self.assertIsNone(connector_key_context({**action, "url": url}, "s1"))
        for spec in ("bad-base64!", "e30", "W10", "bnVsbA"):
            self.assertIsNone(self.protocol.register_credential(connector_event(spec=spec), "s1"))
        self.assertIsNone(self.protocol.register_credential(connector_event(), "s2"))
        self.protocol.request.assert_not_called()

    def test_explicit_rejection_allows_corrected_key(self):
        from muse_bridge.muse.client.errors import RPCError
        from muse_bridge.muse.connector_keys import ConnectorKeyRejected
        item = self.protocol.register_credential(connector_event(), "s1")
        self.protocol.request.side_effect = RPCError("POST", "/v1/custom-connectors/register", 400)
        with self.assertRaises(ConnectorKeyRejected):
            self.protocol.capture_credential(item, "wrong-key")
        self.assertNotIn("submitted_digest", item.context)
        self.assertFalse(Path(self.protocol._receipt_path).exists())
        self.protocol.request.side_effect = None
        self.protocol.request.return_value = {}
        self.protocol.capture_credential(item, "corrected-key")
        self.assertEqual(self.protocol.request.call_count, 2)

    def test_revised_destination_keeps_old_quote_and_focus_as_tombstones(self):
        event = connector_event()
        old = self.protocol.register_credential(event, "s1")
        owner = ("private", "ADMIN", "ADMIN")
        self.service.interactions.activate(old, owner)
        self.service.interactions.remember_credential_message("private", "ADMIN", {"id": "OLD"}, old)
        changed = connector_event(spec=base64.urlsafe_b64encode(json.dumps({"version": 2, "connector": {
            "name": "custom.new", "mode": "api_key", "hosts": ["new.test"],
            "token_proxy": [{"field": "access_token", "placement": "bearer_header"}]}}).encode()).decode())
        changed["payload"]["state"]["version"] = 1
        new = self.protocol.register_credential(changed, "s1")
        self.assertIsNot(old, new)
        self.assertTrue(old.terminal)
        self.assertIs(self.service.interactions.current(owner), old)
        self.assertIs(self.service.interactions.referenced_credential("private", "ADMIN", [("id", "OLD")]), old)
        self.protocol.capture_credential(new, "fictional-key")
        self.assertEqual(self.bodies[0][0][2]["connector"]["hosts"], ["new.test"])
        self.assertIs(self.protocol.register_credential(event, "s1"), new)

    def test_deeply_nested_spec_falls_back_without_aborting_dispatch(self):
        spec = base64.urlsafe_b64encode(("[" * 2000 + "0" + "]" * 2000).encode()).decode()
        self.assertIsNone(self.protocol.register_credential(connector_event(spec=spec), "s1"))

    def test_builtin_callback_is_separate_from_vault(self):
        event = connector_event()
        event["payload"]["data"]["action"]["url"] = "https://agent.meta.ai/connect/api-key?service=fictional-mcp"
        item = self.protocol.register_credential(event, "s1")
        self.protocol.capture_credential(item, "fictional-key")
        self.assertEqual(self.bodies[0], (("POST", "/api_key_auth/callback", {
            "provider": "fictional-mcp", "key": "fictional-key", "session_id": "s1"}), {"service": "authd"}))


class ConnectorChatCase(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TestQQInteractions.setUp
    tearDown = fixtures.TestQQInteractions.tearDown
    send = fixtures.TestQQInteractions.send
    text = fixtures.TestQQInteractions.text
    quoted = replies.TestCredentialReplies.quoted
    delivered = replies.TestCredentialReplies.delivered

    def card(self):
        return self.service.interaction_protocol.register_credential(connector_event(), "s1")

    async def test_quote_single_key_saves_without_model_chat(self):
        item = self.card()
        self.delivered(item)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("fictional-key")
        capture.assert_called_once_with(item, "fictional-key")
        self.assertEqual(self.text(), "【密钥已保存】")
        self.assertTrue(item.terminal)
        self.service.post_message.assert_not_called()

    async def test_command_multiline_rejected_then_single_line_succeeds(self):
        item = self.card()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/密钥 {item.id}")
            self.assertIn("单行", self.text())
            await self.send("account\npassword")
            self.assertIn("只接受一行", self.text())
            capture.assert_not_called()
            await self.send("fictional-key")
            capture.assert_called_once_with(item, "fictional-key")
        self.service.post_message.assert_not_called()

    async def test_key_alias_inline_and_separate_credential_selection(self):
        item = self.card()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/凭证 {item.id}")
            capture.assert_not_called()
            await self.send(f"/key {item.id} fictional-key")
            capture.assert_called_once_with(item, "fictional-key")
        self.service.post_message.assert_not_called()

    async def test_quoted_selection_command_is_never_submitted_as_key(self):
        item = self.card()
        self.delivered(item)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted(f"/密钥 {item.id}")
            capture.assert_not_called()
            self.assertIn("单行", self.text())
            await self.send("fictional-key")
            capture.assert_called_once_with(item, "fictional-key")
        self.service.post_message.assert_not_called()

    async def test_inline_trailing_newline_is_rejected(self):
        item = self.card()
        with patch.object(self.service, "capture_credential") as capture:
            await self.send(f"/密钥 {item.id} fictional-key\n")
            capture.assert_not_called()
            self.assertIn("只接受一行", self.text())
        self.service.post_message.assert_not_called()

    async def test_definitive_rejection_prompts_for_correction(self):
        from muse_bridge.muse.connector_keys import ConnectorKeyRejected
        item = self.card()
        await self.send(f"/密钥 {item.id}")
        with patch.object(self.service, "capture_credential", side_effect=ConnectorKeyRejected()):
            await self.send("fictional-key")
        self.assertIn("请检查后重新输入", self.text())
        self.assertFalse(item.terminal)
        self.service.post_message.assert_not_called()

    async def test_render_prefix_and_real_quote_reference(self):
        pusher = self.adapter.outbound_pusher
        pusher.client = self.adapter.client
        self.adapter.client.send_message.return_value = (200, {"id": "KEY_CARD"})
        event = connector_event()
        data = event["payload"]["data"]
        action = data.pop("action")
        data.update(type="connector", data={"connector_id": action["connectorId"], "url": action["url"]})
        await pusher._scan_and_dispatch([event], session_id="s1")
        item = self.service.interactions.by_remote("credential", "connector-card")
        self.assertTrue(self.text().startswith("【Matlab Mcp提示】连接器（需要输入单行密钥/授权凭据）"))
        self.assertIn("/密钥 " + item.id, self.text())
        self.assertNotIn("网页端", self.text())
        self.assertIs(self.service.interactions.referenced_credential("private", "ADMIN", [("id", "KEY_CARD")]), item)

    async def test_nonadmin_and_cancelled_quote_do_not_leak_key(self):
        item = self.card()
        self.delivered(item)
        self.delivered(item, target_id="OTHER")
        with patch.object(self.service, "capture_credential") as capture:
            await self.quoted("fictional-key", sender="OTHER")
            capture.assert_not_called()
            await self.send(f"/密钥 {item.id}")
            await self.send("/取消")
            await self.quoted("fictional-key")
            capture.assert_not_called()
        self.service.post_message.assert_not_called()
