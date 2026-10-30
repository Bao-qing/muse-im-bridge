"""Interaction ownership, explicit login, and approval policy regression tests."""

import contextlib
import io
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from muse_bridge.config.models import AccessRequestsConfig, ApprovalConfig, MuseConfig, QQConfig, QQPushConfig
from muse_bridge.core.interactions import InteractionManager
from muse_bridge.muse.service import MuseService
from muse_bridge.adapters.qq.adapter import QQAdapter


class TestInteractionManager(unittest.TestCase):
    def test_focus_is_owned_and_optional_cards_do_not_focus(self):
        m = InteractionManager()
        card = m.add("credential", remote_id="card1", session_id="s1")
        owner = ("private", "A", "A")
        other = ("private", "B", "B")
        self.assertIsNone(m.current(owner))
        m.activate(card, owner)
        self.assertIs(m.current(owner), card)
        self.assertIsNone(m.current(other))
        with self.assertRaises(ValueError):
            m.activate(card, other)

    def test_expiry_clears_secrets_but_keeps_input_guard(self):
        m = InteractionManager()
        owner = ("private", "A", "A")
        card = m.add("credential", owner=owner, context={"username": "private", "capture_authorization": "secret"})
        m.activate(card, owner)
        card.expires_at = time.time() - 1
        self.assertIs(m.current(owner), card)
        self.assertEqual(card.state, "expired")
        self.assertNotIn("username", card.context)
        self.assertNotIn("capture_authorization", card.context)

    def test_approval_gate_is_session_scoped_and_not_a_local_timeout(self):
        m = InteractionManager()
        a = m.add("approval", remote_id="a1", session_id="s1", state="pending", ttl=0)
        self.assertEqual(m.pending_approvals("s1"), [a])
        self.assertEqual(m.pending_approvals("s2"), [])
        self.assertIs(m.add("approval", remote_id="a1"), a)
        m.finish(a, "approved")
        self.assertEqual(m.pending_approvals("s1"), [])

    def test_policy_validation(self):
        for mode in ("manual", "allow_all", "deny_all"):
            self.assertEqual(ApprovalConfig(mode=mode).mode, mode)
        with self.assertRaises(ValueError):
            ApprovalConfig(mode="typo")
        with self.assertRaises(ValueError):
            ApprovalConfig(notify="false")


class TestQQInteractions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        token = MagicMock()
        token.has_credentials.return_value = True
        self.service = MuseService(MuseConfig(cookies_file=str(root / "cookies.json")), token)
        cfg = QQConfig(app_id="app", admin_id=["ADMIN"], whitelist={"private": ["ADMIN", "OTHER"], "G": "*"},
                       access_requests=AccessRequestsConfig(state_file=str(root / "access.local.json")),
                       session_id="s1", session_mode="global",
                       session_state_file=str(root / "session.json"),
                       session_map_file=str(root / "map.json"), delivered_file=str(root / "delivered.jsonl"),
                       push=QQPushConfig(state_file=str(root / "push.json")))
        self.adapter = QQAdapter(cfg, self.service)
        self.adapter.client = MagicMock()
        self.adapter.client.send_message.return_value = (200, {})
        self.service.post_message = MagicMock(return_value="mid")

    def tearDown(self):
        self.service.interactions.close()
        self.tmp.cleanup()

    async def send(self, text, sender="ADMIN", mid=None):
        await self.adapter.handle_event("C2C_MESSAGE_CREATE", {"id": mid or uuid.uuid4().hex, "content": text, "author": {"user_openid": sender}})

    def text(self):
        return self.adapter.client.send_message.call_args.args[2]

    async def test_bare_email_is_chat_not_login(self):
        with patch("muse_bridge.muse.login.MuseProtocolLogin.send_otp") as otp:
            await self.send("user@example.com")
            otp.assert_not_called()
        self.service.post_message.assert_called_once()

    async def test_fallback_bare_email_does_not_send_otp(self):
        self.adapter.enter_login_mode()
        with patch("muse_bridge.muse.login.MuseProtocolLogin.send_otp") as otp:
            await self.send("user@example.com")
            otp.assert_not_called()
        self.service.post_message.assert_not_called()
        self.assertIn("/login", self.text())

    async def test_explicit_login_prompts_then_captures_email(self):
        with patch("muse_bridge.muse.login.MuseProtocolLogin.send_otp") as otp:
            await self.send("/login")
            otp.assert_not_called()
            await self.send("user@example.com")
            otp.assert_called_once_with("user@example.com")
        item = self.service.interactions.current(("private", "ADMIN", "ADMIN"))
        self.assertEqual(item.state, "wait_otp")
        self.service.post_message.assert_not_called()

    async def test_nonadmin_cannot_login(self):
        with patch("muse_bridge.muse.login.MuseProtocolLogin.send_otp") as otp:
            await self.send("/login user@example.com", "OTHER")
            otp.assert_not_called()
        self.assertIn("管理员", self.text())

    async def test_late_and_restart_password_commands_are_redacted(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.send("/密码 CUNKNOWN secret-for-test-only")
        self.assertNotIn("secret-for-test-only", out.getvalue())
        self.service.post_message.assert_not_called()

    async def test_expired_focus_does_not_forward_late_input(self):
        owner = ("private", "ADMIN", "ADMIN")
        item = self.service.interactions.add("login", owner=owner, state="wait_otp")
        self.service.interactions.activate(item, owner)
        item.expires_at = time.time() - 1
        await self.send("123456")
        self.service.post_message.assert_not_called()
        self.assertIn("过期", self.text())

    async def test_inactive_credential_does_not_block_chat(self):
        self.service.interactions.add("credential", session_id="s1", context={"page_url":"https://example.test/"})
        await self.send("继续聊天")
        self.service.post_message.assert_called_once()

    async def test_pending_approval_redisplays_and_drops_message(self):
        self.service.interactions.add("approval", state="pending", session_id="s1", ttl=0)
        self.service.format_approval = MagicMock(return_value="审批原生卡片")
        await self.send("不要转发此消息")
        self.assertEqual(self.text(), "审批原生卡片")
        self.service.post_message.assert_not_called()

    async def test_unbound_chat_does_not_gate_on_other_session(self):
        self.service.interactions.add("approval", state="pending", session_id="elsewhere", ttl=0)
        consumed = await self.adapter.interaction_router.handle(("private", "ADMIN", "ADMIN"), "normal", "id-unbound", True, "")
        self.assertFalse(consumed)

    async def test_group_cannot_decide_other_sessions_approval(self):
        from unittest.mock import AsyncMock
        item = self.service.interaction_protocol.upsert_approval({"approval_id": "approval-test", "status": "pending",
            "scope": {"thread_id": "s1"}, "decision_options": []})
        self.service.submit_approval = AsyncMock()
        await self.adapter.interaction_router.handle(("group", "G", "ADMIN"), f"/审批 {item.id} 1", "id-group", True, "s2")
        self.service.submit_approval.assert_not_called()
        self.assertIn("不属于", self.text())

    async def test_admin_must_see_current_card_before_deciding(self):
        from unittest.mock import AsyncMock
        item = self.service.interaction_protocol.upsert_approval({"approval_id": "approval-test", "status": "pending",
            "scope": {"thread_id": "s1"}, "decision_options": []})
        self.service.submit_approval = AsyncMock(return_value="decision test")
        await self.send(f"/审批 {item.id} 1")
        self.service.submit_approval.assert_not_called()
        self.assertIn("审批", self.text())
        await self.send(f"/审批 {item.id} 1")
        self.service.submit_approval.assert_awaited_once_with(item, "1", expected_fingerprint=item.context["fingerprint"])

    async def test_inactive_card_lifetime_selection_is_explicit(self):
        item = self.service.interactions.add("credential", session_id="s1", context={"page_url": "https://example.test/"})
        await self.send(f"/凭证 {item.id} 一次")
        self.assertEqual(item.context["lifetime"], "on_read")
        self.assertEqual(item.state, "wait_username")
        self.service.post_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
