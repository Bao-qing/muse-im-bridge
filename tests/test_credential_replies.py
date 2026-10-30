"""QQ credential entry, reply correlation, and automatic chat return."""

import contextlib
import io
import json
import time
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import test_interactions as fixtures
from muse_bridge.config.models import ConfigLoader, QQConfig


class TestCredentialReplies(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TestQQInteractions.setUp
    tearDown = fixtures.TestQQInteractions.tearDown
    send = fixtures.TestQQInteractions.send
    text = fixtures.TestQQInteractions.text

    def card(self, session_id="s1", **context):
        return self.service.interactions.add("credential", session_id=session_id, ttl=0,
            context={"page_url": "https://example.test/", **context})

    async def group(self, text, sender="ADMIN", **extra):
        await self.adapter.handle_event("GROUP_MESSAGE_CREATE", {
            "id": str(time.time_ns()), "content": text, "group_openid": "G",
            "author": {"member_openid": sender}, **extra})

    async def quoted(self, text, ref="REFIDX_CARD==", sender="ADMIN", group=False):
        event = {"message_type": 103, "message_scene": {"ext": [
            "ref_msg_idx=" + ref, "msg_idx=REFIDX_INCOMING==", "auth_token=unused"]}}
        if group:
            await self.group(text, sender=sender, **event)
        else:
            await self.adapter.handle_event("C2C_MESSAGE_CREATE", {
                "id": str(time.time_ns()), "content": text,
                "author": {"user_openid": sender}, **event})

    def delivered(self, card, target_type="private", target_id="ADMIN", ref="REFIDX_CARD=="):
        self.service.interactions.remember_credential_message(target_type, target_id,
            {"id": "ROBOT_CARD", "ext_info": {"ref_idx": ref}}, card)

    async def test_combined_input_saves_and_next_message_is_chat(self):
        card = self.card()
        observed = []
        self.service.capture_credential = lambda item, secret: observed.append((item.context["username"], secret))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await self.send("fictional-account\nfictional-password")
        self.assertEqual(observed, [("fictional-account", "fictional-password")])
        self.assertNotIn("fictional-account", output.getvalue())
        self.assertNotIn("fictional-password", output.getvalue())
        self.assertIsNone(self.service.interactions.current(("private", "ADMIN", "ADMIN")))
        self.assertTrue(card.terminal)
        self.assertEqual(self.text(), "凭证已保存，可以继续聊天。")
        self.service.post_message.assert_not_called()
        await self.send("继续聊")
        self.service.post_message.assert_called_once()

    async def test_partial_input_only_asks_for_missing_password(self):
        card = self.card()
        self.service.capture_credential = capture = MagicMock()
        await self.send(f"/凭证 {card.id} 一次")
        await self.send("fictional-account")
        self.assertEqual(self.text(), "请输入密码。")
        capture.assert_not_called()
        await self.send("fictional-password")
        capture.assert_called_once_with(card, "fictional-password")
        self.assertEqual(card.context["lifetime"], "on_read")
        self.service.post_message.assert_not_called()

    async def test_command_can_include_two_lines(self):
        card = self.card()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send(f"/凭证 {card.id} 一次\naccount\npassword")
        capture.assert_called_once_with(card, "password")
        self.assertTrue(card.terminal)

    async def test_quote_selects_right_card_among_multiple(self):
        card = self.card()
        other = self.card()
        self.delivered(card)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("account\npassword")
        capture.assert_called_once_with(card, "password")
        self.assertFalse(other.terminal)
        self.service.post_message.assert_not_called()

    async def test_empty_quote_starts_then_plain_input_completes(self):
        card = self.card()
        self.delivered(card)
        await self.quoted("")
        self.assertEqual(card.state, "wait_username")
        self.assertIn("每项一行", self.text())
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.send("account\npassword")
        capture.assert_called_once_with(card, "password")

    async def test_quote_username_then_quote_prompt_password(self):
        card = self.card()
        self.delivered(card)
        self.adapter.client.send_message.return_value = (200, {"ext_info": {"ref_idx": "PROMPT=="}})
        await self.quoted("account")
        self.assertEqual(self.text(), "请输入密码。")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("password", ref="PROMPT==")
        capture.assert_called_once_with(card, "password")

    async def test_group_setting_default_denies_but_explicit_true_allows(self):
        card = self.card()
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.group(f"/凭证 {card.id}\naccount\npassword")
            capture.assert_not_called()
            self.assertEqual(self.text(), "请私聊填写此凭证。")
            self.adapter.config.credentials_in_place = True
            await self.group(f"/凭证 {card.id}\naccount\npassword")
            capture.assert_called_once_with(card, "password")
        self.service.post_message.assert_not_called()

    async def test_group_quote_obeys_setting_and_member_admin(self):
        card = self.card()
        self.delivered(card, "group", "G")
        self.adapter.config.credentials_in_place = True
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("account\npassword", group=True, sender="OTHER")
            capture.assert_not_called()
            await self.quoted("account\npassword", group=True)
            capture.assert_called_once_with(card, "password")
        self.service.post_message.assert_not_called()

    async def test_group_cannot_access_another_session_card(self):
        card = self.card("elsewhere")
        self.adapter.config.credentials_in_place = True
        self.delivered(card, "group", "G")
        with patch.object(self.service, "capture_credential") as capture:
            await self.quoted("account\npassword", group=True)
        capture.assert_not_called()
        self.service.post_message.assert_not_called()

    async def test_multiple_cards_do_not_guess_from_two_lines(self):
        self.card()
        self.card()
        with patch.object(self.service, "capture_credential") as capture:
            await self.send("account\npassword")
        capture.assert_not_called()
        self.service.post_message.assert_not_called()
        self.assertIn("回复", self.text())

    async def test_unknown_quote_never_becomes_chat(self):
        self.card()
        with patch.object(self.service, "capture_credential") as capture:
            await self.quoted("account\npassword", ref="TMP_unknown")
        capture.assert_not_called()
        self.service.post_message.assert_not_called()

    async def test_completed_card_quote_never_becomes_chat(self):
        card = self.card()
        self.delivered(card)
        self.service.interactions.finish(card)
        await self.quoted("late-password")
        self.service.post_message.assert_not_called()

    async def test_verification_reply_needs_only_one_value(self):
        card = self.card(capture="verification_code")
        self.delivered(card)
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("123456")
        capture.assert_called_once_with(card, "123456")
        self.assertEqual(card.context["lifetime"], "on_read")
        self.assertIsNone(self.service.interactions.current(("private", "ADMIN", "ADMIN")))

    async def test_failed_capture_keeps_focus_for_same_input_retry(self):
        card = self.card()
        with patch.object(self.service, "capture_credential", side_effect=TimeoutError()):
            await self.send("account\npassword")
        self.assertEqual(card.state, "wait_password")
        self.assertIs(self.service.interactions.current(("private", "ADMIN", "ADMIN")), card)
        self.service.post_message.assert_not_called()

    def test_reference_index_keeps_padding_and_is_chat_scoped(self):
        card = self.card()
        self.delivered(card)
        router = self.adapter.interaction_router
        refs = router.message_references({"message_scene": {"ext": ["msg_idx=OTHER", "ref_msg_idx=REFIDX_CARD=="]}})
        self.assertEqual(refs, [("ref", "REFIDX_CARD==")])
        self.assertIs(self.service.interactions.referenced_credential("private", "ADMIN", refs), card)
        self.assertIsNone(self.service.interactions.referenced_credential("group", "G", refs))

    async def test_group_focus_cannot_follow_session_switch(self):
        card = self.card()
        self.adapter.config.credentials_in_place = True
        await self.group(f"/凭证 {card.id}")
        await self.group("account")
        self.adapter.session_mapper.bind("group", "G", "s2")
        with patch.object(self.service, "capture_credential") as capture:
            await self.group("password")
        capture.assert_not_called()
        self.assertEqual(card.state, "cancelled")
        self.service.post_message.assert_not_called()

    async def test_second_admin_cannot_take_owned_card(self):
        card = self.card()
        self.adapter.config.admin_id.append("OTHER")
        self.adapter.config.credentials_in_place = True
        self.delivered(card, "group", "G")
        await self.quoted("account", group=True)
        with patch.object(self.service, "capture_credential") as capture:
            await self.quoted("other-account\npassword", group=True, sender="OTHER")
        capture.assert_not_called()
        self.assertEqual(card.owner, ("group", "G", "ADMIN"))
        self.service.post_message.assert_not_called()

    def test_conflicting_or_partial_references_fail_closed(self):
        first, second = self.card(), self.card()
        self.delivered(first)
        self.service.interactions.remember_credential_message(
            "private", "ADMIN", {"id": "SECOND"}, second)
        for refs in ([('ref', 'REFIDX_CARD=='), ('id', 'SECOND')],
                     [('ref', 'REFIDX_CARD=='), ('id', 'unknown')]):
            self.assertIsNone(self.service.interactions.referenced_credential(
                "private", "ADMIN", refs))
        router = self.adapter.interaction_router
        for value in (None, {}, 1, "ref_msg_idx=wrong"):
            self.assertEqual(router.message_references({"message_scene": {"ext": value}}), [])

    async def test_success_still_respects_pending_approval(self):
        card = self.card()
        self.service.interactions.add("approval", session_id="s1", ttl=0)
        with patch.object(self.service, "capture_credential", return_value="saved"):
            await self.send("account\npassword")
        self.assertTrue(card.terminal)
        self.assertIsNone(self.service.interactions.current(("private", "ADMIN", "ADMIN")))
        with patch.object(self.service, "format_approval", return_value="待审批"):
            await self.send("继续聊")
        self.service.post_message.assert_not_called()
        self.assertEqual(self.text(), "待审批")

    async def test_cancelled_and_expired_focus_do_not_release(self):
        card = self.card()
        await self.send(f"/凭证 {card.id}")
        await self.send("/cancel")
        await self.send("late-password")
        self.assertIs(self.service.interactions.current(("private", "ADMIN", "ADMIN")), card)
        next_card = self.card()
        await self.send(f"/凭证 {next_card.id}")
        next_card.expires_at = time.time() - 1
        await self.send("late-password")
        self.assertEqual(next_card.state, "expired")
        self.service.post_message.assert_not_called()

    async def test_outbound_records_only_successful_actual_destination(self):
        card = self.card()
        pusher = self.adapter.outbound_pusher
        pusher.client = self.adapter.client
        self.adapter.client.send_message.return_value = (200, {"id": "SENT", "ext_info": {"ref_idx": "DELIVERED=="}})
        self.assertTrue(await pusher._send_to_qq("group", "G", "card", [], [], [], credential=card))
        self.assertIs(self.service.interactions.referenced_credential("group", "G", [("ref", "DELIVERED==")]), card)
        self.adapter.client.send_message.side_effect = [(403, {"code": 40034105}), (200, {"ext_info": {"ref_idx": "FALLBACK=="}})]
        with patch.object(self.adapter.config, "resolve_push_target", return_value=("private", "ADMIN")):
            self.assertTrue(await pusher._send_to_qq("group", "G", "card", [], [], [], credential=card))
        self.assertIs(self.service.interactions.referenced_credential("private", "ADMIN", [("ref", "FALLBACK==")]), card)
        self.assertIsNone(self.service.interactions.referenced_credential("group", "G", [("ref", "FALLBACK==")]))
        self.adapter.client.send_message.side_effect = None
        self.adapter.client.send_message.return_value = (500, {"id": "FAILED"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await pusher._send_to_qq("group", "G", "card", [], [], [], credential=card))
        self.assertIsNone(self.service.interactions.referenced_credential("group", "G", [("id", "FAILED")]))

    async def test_failed_prompt_delivery_does_not_record_reference(self):
        card = self.card()
        self.adapter.client.send_message.return_value = (500, {"id": "FAILED"})
        with self.assertRaises(RuntimeError):
            await self.send(f"/凭证 {card.id}")
        self.assertFalse(self.service.interactions.credential_messages)

    async def test_full_two_line_answer_also_works_after_account_prompt(self):
        card = self.card()
        self.delivered(card)
        await self.quoted("account")
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("account\npassword")
        capture.assert_called_once_with(card, "password")
        self.assertTrue(card.terminal)

    async def test_two_line_retry_keeps_original_account_after_uncertain_save(self):
        card = self.card()
        self.delivered(card)
        await self.quoted("account")
        card.context["submitted_digest"] = "uncertain"
        with patch.object(self.service, "capture_credential", return_value="saved") as capture:
            await self.quoted("changed-account\npassword")
            capture.assert_not_called()
            self.assertEqual(card.context["username"], "account")
            await self.quoted("account\npassword")
            capture.assert_called_once_with(card, "password")
        self.service.post_message.assert_not_called()

    def test_config_requires_real_boolean(self):
        self.assertFalse(QQConfig().credentials_in_place)
        for value in ("false", 0, None):
            with self.assertRaises(ValueError):
                QQConfig(credentials_in_place=value)
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({"muse": {"client_ip": "127.0.0.1"},
            "adapters": {"qq": {"credentials_in_place": True}}}), encoding="utf-8")
        self.assertTrue(ConfigLoader.load(str(path)).qq.credentials_in_place)
