"""Secret-only cards share the guarded QQ/OneBot credential filling flow."""

import contextlib
import copy
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import test_credential_replies as replies
import test_interactions as fixtures
import test_onebot_adapter as onebot
from muse_bridge.config.models import MuseConfig
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.interactions import SECRET_CAPTURE_FIELDS


KEY = "\n".join(("-----BEGIN OPENSSH PRIVATE KEY-----", "FICTIONAL-NOT-A-KEY", "-----END OPENSSH PRIVATE KEY-----"))


def secret_event(capture="private_key", *, authorization="fictional-auth", **overrides):
    fields = {"page_url": "https://example.test/", "password_label": "SSH 私钥"}
    if authorization is not None:
        fields["capture_authorization"] = authorization
    url = "https://agent.meta.ai/connect/browser-credentials#" + urlencode(fields)
    return {"event_name": "delta.presentation", "message_id": "secret-message", "role": "assistant",
            "payload": {"presentation_id": "secret-card", "kind": "internal_link_action_card",
                        "data": {"type": "credentials_vault", "title": "安全存储", "data": {
                            "url": url, "capture": capture, **fields, **overrides}},
                        "state": {"version": 1, "data": {}}}}


class SecretProtocolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = MuseService(MuseConfig(cookies_file=str(Path(self.tmp.name) / "cookies.json")), MagicMock())
        self.protocol = self.service.interaction_protocol
        self.bodies = []

        def request(method, path, body, **kwargs):
            self.assertEqual((method, path, kwargs), ("POST", "/v1/credentials/capture", {"service": "authd"}))
            self.bodies.append(copy.deepcopy(body))
            return {"id": "fictional-capture"}

        self.protocol.request = MagicMock(side_effect=request)

    def test_supported_secret_captures_submit_one_protected_field(self):
        for capture, field in SECRET_CAPTURE_FIELDS.items():
            with self.subTest(capture=capture):
                event = secret_event(capture)
                event["payload"]["presentation_id"] = capture
                item = self.protocol.register_credential(event, "s1")
                self.assertIsNotNone(item)
                self.protocol.capture_credential(item, KEY)
                body = self.bodies[-1]
                self.assertEqual(body["fields"], [{"name": field, "value": KEY, "protected": True}])
                self.assertEqual(body["session_id"], "s1")
                self.assertEqual(body["capture_authorization"], "fictional-auth")
                self.assertNotIn(KEY, json.dumps(item.context))

    def test_real_legacy_create_password_without_authorization(self):
        item = self.protocol.register_credential(secret_event("create_password", authorization=None), "s1")
        self.assertIsNotNone(item)
        item.context["lifetime"] = "on_read"
        self.protocol.capture_credential(item, KEY)
        self.assertEqual(self.bodies[0]["fields"][0]["name"], "password")
        self.assertEqual(self.bodies[0]["lifetime"], "on_read")
        self.assertNotIn("capture_authorization", self.bodies[0])

    def test_password_only_sign_in_does_not_require_username(self):
        item = self.protocol.register_credential(secret_event("sign_in"), "s1")
        self.protocol.capture_credential(item, "fictional-api-key")
        self.assertEqual(self.bodies[0]["fields"], [{"name": "password", "value": "fictional-api-key", "protected": True}])

    def test_muse_domain_and_structured_verification_capture(self):
        event = secret_event("verification_code")
        fields = event["payload"]["data"]["data"]
        fields.pop("password_label")
        context = {"page_url": "https://example.test/", "capture_authorization": "fictional-auth", "verification_code_label": "验证码"}
        fields.update(context, url="https://muse.ai/connect/browser-credentials#" + urlencode(context))
        item = self.protocol.register_credential(event, "s1")
        self.assertIsNotNone(item)
        self.protocol.capture_credential(item, "123456")
        self.assertEqual(self.bodies[0]["fields"][0]["name"], "verification_code")
        self.assertEqual(self.bodies[0]["lifetime"], "on_read")

    def test_unknown_unauthorized_or_mismatched_cards_fail_closed(self):
        for event in (secret_event("unknown_capture"), secret_event(authorization=None),
                      secret_event(page_url="https://other.test/"), secret_event(username_label="Account"),
                      secret_event(auth_providers=["google"])):
            with self.subTest(event=event):
                self.assertIsNone(self.protocol.register_credential(event, "s1"))
        for suffix in ("&password_label=Other", "&capture_authorization=", "&capture=unknown_capture"):
            event = secret_event()
            event["payload"]["data"]["data"]["url"] += suffix
            self.assertIsNone(self.protocol.register_credential(event, "s1"))

    def test_old_password_only_sign_in_receipt_still_prevents_replay(self):
        event = secret_event("sign_in")
        fields = event["payload"]["data"]["data"]
        identity = [self.service.config.host, self.service.config.vm_id, "s1", "secret-card",
                    fields["page_url"], fields["capture_authorization"]]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        Path(self.protocol._receipt_path).write_text(json.dumps([key]))
        token = MagicMock()
        token.vm_id = ""
        restored = MuseService(self.service.config, token).interaction_protocol
        self.assertTrue(restored.register_credential(event, "s1").terminal)

    def test_secret_retry_digest_and_receipt_remain_secret_free(self):
        item = self.protocol.register_credential(secret_event(), "s1")
        self.protocol.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.protocol.capture_credential(item, KEY)
        self.assertNotIn(KEY, json.dumps(item.context))
        with self.assertRaises(ValueError):
            self.protocol.capture_credential(item, "changed-key")
        self.assertEqual(self.protocol.request.call_count, 1)
        self.protocol.request.side_effect = lambda *args, **kwargs: {"id": "saved"}
        self.protocol.capture_credential(item, KEY)
        receipt = Path(self.protocol._receipt_path).read_text()
        for value in (KEY, "fictional-auth", "example.test"):
            self.assertNotIn(value, receipt)
        restored = MuseService(self.service.config, MagicMock()).interaction_protocol
        self.assertTrue(restored.register_credential(secret_event(), "s1").terminal)


class SecretReplyCase(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TestQQInteractions.setUp
    tearDown = fixtures.TestQQInteractions.tearDown
    send = fixtures.TestQQInteractions.send
    text = fixtures.TestQQInteractions.text
    group = replies.TestCredentialReplies.group
    quoted = replies.TestCredentialReplies.quoted
    delivered = replies.TestCredentialReplies.delivered

    def card(self, capture="private_key", session_id="s1"):
        return self.service.interaction_protocol.register_credential(secret_event(capture), session_id)

    async def test_quote_submits_multiline_secret_without_username_or_chat(self):
        card = self.card()
        self.delivered(card)
        output = io.StringIO()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture, contextlib.redirect_stdout(output):
            await self.quoted(KEY)
        capture.assert_called_once_with(card, KEY)
        self.assertTrue(card.terminal)
        self.assertEqual(self.text(), "【凭证已保存】")
        self.assertNotIn(KEY, output.getvalue())
        self.service.post_message.assert_not_called()
        await self.send("normal chat")
        self.service.post_message.assert_called_once()

    async def test_command_then_multiline_input_and_one_time_lifetime(self):
        card = self.card("api_key")
        await self.send(f"/凭证 {card.id} 一次")
        self.assertIn("无需填写账号", self.text())
        self.assertEqual(card.state, "wait_secret")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(KEY)
        capture.assert_called_once_with(card, KEY)
        self.assertEqual(card.context["lifetime"], "on_read")
        self.service.post_message.assert_not_called()

    async def test_inline_command_preserves_secret_boundary_whitespace(self):
        card = self.card()
        value = "  " + KEY + chr(10)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/凭证 {card.id} 一次" + chr(10) + value)
        capture.assert_called_once_with(card, value)

    async def test_command_can_include_entire_multiline_secret(self):
        card = self.card()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/凭证 {card.id} 一次\n" + KEY)
        capture.assert_called_once_with(card, KEY)

    async def test_password_command_can_submit_selected_single_secret(self):
        card = self.card("api_key")
        await self.send(f"/凭证 {card.id}")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/密码 {card.id} fictional-api-key")
        capture.assert_called_once_with(card, "fictional-api-key")
        self.service.post_message.assert_not_called()

    async def test_group_permissions_and_binding_match_regular_credentials(self):
        card = self.card()
        self.delivered(card, "group", "G")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted(KEY, group=True, sender="OTHER")
            capture.assert_not_called()
            self.adapter.config.credentials_in_place = False
            await self.quoted(KEY, group=True)
            capture.assert_not_called()
            self.adapter.config.credentials_in_place = True
            self.adapter.session_mapper.bind("group", "G", "another-session")
            await self.quoted(KEY, group=True)
            capture.assert_not_called()
            self.adapter.session_mapper.bind("group", "G", "s1")
            await self.quoted(KEY, group=True)
            capture.assert_called_once_with(card, KEY)
        self.service.post_message.assert_not_called()

    async def test_failed_secret_capture_retains_focus_and_cancel_swallows_quote(self):
        card = self.card()
        self.delivered(card)
        with patch.object(self.service, "capture_credential", side_effect=TimeoutError()):
            await self.quoted(KEY)
        self.assertEqual(card.state, "wait_secret")
        await self.send("/cancel")
        await self.quoted("late secret")
        self.assertTrue(card.terminal)
        self.service.post_message.assert_not_called()

    async def test_unquoted_multiline_requires_explicit_credential_mode(self):
        card = self.card()
        with patch.object(self.service, "capture_credential") as capture:
            await self.send(KEY)
        capture.assert_not_called()
        self.service.post_message.assert_called_once()
        self.assertIsNone(self.service.interactions.current(("private", "ADMIN", "ADMIN")))
        self.assertFalse(card.terminal)

    async def test_multiple_pending_cards_do_not_intercept_multiline_chat(self):
        self.card()
        self.service.interactions.add("credential", session_id="s1", ttl=0)
        with patch.object(self.service, "capture_credential") as capture:
            await self.send(KEY)
        capture.assert_not_called()
        self.service.post_message.assert_called_once()
        self.assertIsNone(self.service.interactions.current(("private", "ADMIN", "ADMIN")))

    async def test_legacy_password_card_renders_and_records_real_quote_reference(self):
        event = secret_event("create_password", authorization=None)
        pusher = self.adapter.outbound_pusher
        pusher.client = self.adapter.client
        self.adapter.client.send_message.return_value = (200, {"id": "DELIVERED_SECRET_CARD"})
        await pusher._scan_and_dispatch([event], session_id="s1")
        card = self.service.interactions.by_remote("credential", "secret-card")
        self.assertIsNotNone(card)
        self.assertIn("无需填写账号", self.text())
        self.assertNotIn("网页端", self.text())
        self.assertIs(self.service.interactions.referenced_credential("private", "ADMIN", [("id", "DELIVERED_SECRET_CARD")]), card)

    async def test_password_prompt_accepts_private_key_without_account_prefix(self):
        card = self.service.interactions.add("credential", session_id="s1", ttl=0, context={"page_url": "https://example.test/"})
        self.delivered(card)
        await self.quoted("account")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted(KEY)
        capture.assert_called_once_with(card, KEY)
        self.assertEqual(card.context.get("username"), None)
        self.service.post_message.assert_not_called()

    async def test_quote_preserves_secret_boundary_spaces_and_final_newline(self):
        card = self.card()
        self.delivered(card)
        value = "  " + KEY + chr(10)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted(value)
        capture.assert_called_once_with(card, value)

    async def test_multiline_password_starting_with_account_is_not_truncated(self):
        card = self.service.interactions.add("credential", session_id="s1", ttl=0, context={"page_url": "https://example.test/"})
        self.delivered(card)
        await self.quoted("account")
        value = chr(10).join(("account", "second-secret-line", "third-secret-line"))
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted(value)
        capture.assert_called_once_with(card, value)

    async def test_sign_in_keeps_all_lines_after_account_as_secret(self):
        card = self.service.interactions.add("credential", session_id="s1", ttl=0, context={"page_url": "https://example.test/"})
        self.delivered(card)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("account\n" + KEY)
        capture.assert_called_once_with(card, KEY)
        self.service.post_message.assert_not_called()


class SecretOneBotCase(unittest.IsolatedAsyncioTestCase):
    async def test_secret_replies_bypass_trigger_and_preserve_exact_text(self):
        for trigger in ("all", "at", "prefix"):
            for quote in (False, True):
                with self.subTest(trigger=trigger, quote=quote), tempfile.TemporaryDirectory() as tmp:
                    adapter, muse = onebot._make_adapter(tmp)
                    adapter.config.group_trigger = trigger
                    adapter.session_mapper.bind("group", "G1", "s1")
                    item = muse.interactions.add("credential", session_id="s1", ttl=0,
                        context={"page_url": "https://example.test/", "capture": "private_key"})
                    muse.interactions.remember_credential_message("group", "G1", {"id": "555"}, item)
                    if not quote:
                        await adapter.handle_event(onebot._group("G1", "A1", f"/凭证 {item.id}", 1))
                    value = "  " + KEY + chr(10)
                    event = onebot._group("G1", "A1", value, 2)
                    if quote:
                        event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})
                    await adapter.handle_event(event)
                    muse.capture_credential.assert_called_once_with(item, value)
                    muse.post_message.assert_not_called()


    async def test_group_quote_preserves_multiline_secret_and_admin_restriction(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = onebot._make_adapter(tmp)
            adapter.session_mapper.bind("group", "G1", "s1")
            item = muse.interactions.add("credential", session_id="s1", ttl=0, context={
                "page_url": "https://example.test/", "capture": "private_key", "password_label": "SSH 私钥"})
            muse.interactions.remember_credential_message("group", "G1", {"id": "555"}, item)
            for user, mid in ((5, 1), ("A1", 2)):
                event = onebot._group("G1", user, KEY, mid)
                event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})
                await adapter.handle_event(event)
                if user == 5:
                    muse.capture_credential.assert_not_called()
            muse.capture_credential.assert_called_once_with(item, KEY)
            self.assertEqual(onebot._sent_texts(adapter)[-1], "【凭证已保存】")
            muse.post_message.assert_not_called()
