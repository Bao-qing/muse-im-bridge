"""QQ whitelist applications, persisted scope, and administrator-only review."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from muse_bridge.adapters.qq.access import AccessRequestStore
from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, MuseConfig, QQConfig
from muse_bridge.muse.service import MuseService


class TestAccessRequests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = QQConfig(app_id="APP", admin_id=["ADMIN"],
            whitelist={"private": ["KNOWN"], "OLD": ["KNOWN"]},
            session_map_file=str(self.root / "map.json"), delivered_file=str(self.root / "delivered.jsonl"),
            push_state_file=str(self.root / "push.json"),
            access_requests=AccessRequestsConfig(state_file=str(self.root / "access.local.json")))
        self.token = MagicMock()
        self.token.has_credentials.return_value = True
        self.service = MuseService(MuseConfig(cookies_file=str(self.root / "cookies.json")), self.token)
        self.addCleanup(self.service.interactions.close)
        self.service.post_message = MagicMock(return_value="mid")
        self.adapter = QQAdapter(self.config, self.service)
        self.adapter.client = MagicMock()
        self.adapter.client.send_message.return_value = (200, {})
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    async def send(self, text, user="NEW", group="G", event=None, **extra):
        kind = event or ("GROUP_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE")
        data = {"id": uuid.uuid4().hex, "content": text,
                "author": {"member_openid" if group else "user_openid": user}, **extra}
        if group:
            data["group_openid"] = group
        await self.adapter.handle_event(kind, data)

    def pending(self):
        return self.adapter.access_requests.store.pending()

    def allowed(self, user="NEW", group="G"):
        return self.adapter.is_allowed("GROUP_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE",
            {"author": {"member_openid" if group else "user_openid": user}, "group_openid": group})

    def texts(self):
        return [c.args[2] for c in self.adapter.client.send_message.call_args_list]

    async def test_unknown_chat_only_application_enters_local_router(self):
        await self.send("normal message")
        self.adapter.client.send_message.assert_not_called()
        await self.send("/申请", event="GROUP_AT_MESSAGE_CREATE")
        self.assertEqual(len(self.pending()), 1)
        calls = self.adapter.client.send_message.call_args_list
        self.assertEqual(calls[0].args[:2], ("private", "ADMIN"))
        self.assertIn("申请人在本群使用", calls[0].args[2])
        self.assertFalse(self.allowed())
        self.service.post_message.assert_not_called()

    async def test_admin_private_review_works_without_private_whitelist(self):
        await self.send("/申请")
        row = self.pending()[0]
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed("OTHER"))
        self.assertFalse(self.allowed(group="ELSE"))
        self.assertFalse(self.allowed(group=None))
        await self.send("hello")
        self.service.post_message.assert_called_once()
        self.assertEqual(self.config.whitelist, {"private": ["KNOWN"], "OLD": ["KNOWN"]})

    async def test_nonadmin_or_group_cannot_approve_even_when_admin_only_false(self):
        self.config.admin_only = False
        await self.send("/申请")
        row = self.pending()[0]
        await self.send(f"/申请审批 {row['id']} 同意", group=None)
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN")
        self.assertFalse(self.allowed())
        self.assertEqual(len(self.pending()), 1)
        self.service.post_message.assert_not_called()

    async def test_group_all_scope_never_opens_other_groups_or_private(self):
        await self.send("/申请 全员")
        row = self.pending()[0]
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertTrue(self.allowed("OTHER"))
        self.assertFalse(self.allowed("OTHER", "ELSE"))
        self.assertFalse(self.allowed("OTHER", None))
        self.assertFalse(self.config.is_admin("NEW"))

    async def test_existing_member_can_request_all_member_upgrade(self):
        await self.send("/申请 全员", user="KNOWN", group="OLD")
        row = self.pending()[0]
        self.assertEqual(row["scope"], "all")
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertTrue(self.allowed("OTHER", "OLD"))
        self.assertEqual(self.config.whitelist["OLD"], ["KNOWN"])

    async def test_private_application_grants_only_requester_private(self):
        await self.send("/申请", group=None)
        row = self.pending()[0]
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertTrue(self.allowed(group=None))
        self.assertFalse(self.allowed())
        self.assertFalse(self.allowed("OTHER", None))

    async def test_automatic_approval_persists_scope_and_does_not_notify_for_review(self):
        self.config.access_requests.auto_approve = True
        await self.send("/申请 全员")
        self.assertTrue(self.allowed("OTHER"))
        restored = AccessRequestStore(self.config.access_requests.state_file, "APP")
        self.assertTrue(restored.allows("group", "G", "OTHER"))
        self.assertFalse(restored.allows("private", "NEW", "NEW"))
        self.assertEqual(self.adapter.client.send_message.call_count, 1)
        self.assertIn("所有人", self.texts()[-1])

    async def test_enabling_auto_does_not_sweep_existing_pending(self):
        await self.send("/申请")
        self.config.access_requests.auto_approve = True
        await self.send("/申请")
        self.assertFalse(self.allowed())
        self.assertEqual(len(self.pending()), 1)

    async def test_duplicate_pending_never_renotifies_delivered_admins(self):
        await self.send("/申请")
        request_id = self.pending()[0]["id"]
        await self.send("/申请")
        self.assertEqual(self.pending()[0]["id"], request_id)
        notices = [c for c in self.adapter.client.send_message.call_args_list if c.args[:2] == ("private", "ADMIN")]
        self.assertEqual(len(notices), 1)

    async def test_denial_and_opposite_repeat_do_not_grant(self):
        await self.send("/申请")
        row = self.pending()[0]
        await self.send(f"/申请审批 {row['id']} 拒绝", user="ADMIN", group=None)
        await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertFalse(self.allowed())
        self.assertEqual(self.adapter.access_requests.store.get(row["id"])["status"], "denied")

    async def test_failed_save_never_publishes_automatic_grant(self):
        self.config.access_requests.auto_approve = True
        with patch("muse_bridge.gateway.access.os.replace", side_effect=OSError("disk")):
            await self.send("/申请")
        self.assertFalse(self.allowed())
        self.assertFalse(Path(self.config.access_requests.state_file).exists())
        self.assertIn("无法保存", self.texts()[-1])
        self.assertEqual(list(self.root.glob(".qq-access-*")), [])

    async def test_failed_approval_save_keeps_pending_and_no_access(self):
        await self.send("/申请")
        row = self.pending()[0]
        with patch("muse_bridge.gateway.access.os.replace", side_effect=OSError("disk")):
            await self.send(f"/申请审批 {row['id']} 同意", user="ADMIN", group=None)
        self.assertFalse(self.allowed())
        self.assertEqual(self.adapter.access_requests.store.get(row["id"])["status"], "pending")

    async def test_notification_failure_preserves_queryable_pending(self):
        self.adapter.client.send_message.side_effect = [(500, {}), (500, {}), (200, {})]
        await self.send("/申请")
        self.assertEqual(len(self.pending()), 1)
        self.assertFalse(self.allowed())
        self.assertIn("暂未通知", self.texts()[-1])
        self.adapter.client.send_message.side_effect = None
        await self.send("/申请审批", user="ADMIN", group=None)
        self.assertIn(self.pending()[0]["id"], self.texts()[-1])

    async def test_no_admin_manual_refuses_auto_still_works(self):
        self.config.admin_id.clear()
        await self.send("/申请")
        self.assertEqual(self.pending(), [])
        self.config.access_requests.auto_approve = True
        await self.send("/申请")
        self.assertTrue(self.allowed())

    async def test_disabled_requests_do_not_disable_saved_access(self):
        self.config.access_requests.auto_approve = True
        await self.send("/申请")
        self.config.access_requests.enabled = False
        await self.send("/申请", user="OTHER")
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed("OTHER"))

    async def test_malformed_targets_and_grammar_do_not_apply_or_forward(self):
        for text in ("/申请 OTHER", "/申请 全员 OTHER", "/申请 全员\nextra"):
            await self.send(text)
        await self.send("/申请 全员", group=None)
        await self.send("/申请", group="*")
        await self.send("/申请", user="")
        await self.send("/申请", event="AT_MESSAGE_CREATE")
        self.assertEqual(self.pending(), [])
        self.service.post_message.assert_not_called()

    async def test_sensitive_focus_and_quoted_application_never_notify(self):
        owner = ("group", "G", "NEW")
        item = self.service.interactions.add("credential", owner=owner, session_id="s1", state="wait_password")
        self.service.interactions.activate(item, owner)
        await self.send("/申请")
        self.service.interactions.release(owner)
        await self.send("/申请", message_scene={"ext": ["ref_msg_idx=SECRET"]})
        self.assertEqual(self.pending(), [])
        self.assertTrue(all(c.args[:2] != ("private", "ADMIN") for c in self.adapter.client.send_message.call_args_list))
        self.service.post_message.assert_not_called()

    async def test_approved_user_cannot_use_admin_credential_commands(self):
        self.config.access_requests.auto_approve = True
        await self.send("/申请")
        await self.send("/login fake@example.test")
        self.assertFalse(self.config.is_admin("NEW"))
        self.assertIn("管理员", self.texts()[-1])
        self.service.post_message.assert_not_called()

    async def test_pending_approval_gate_still_applies_to_new_member(self):
        self.config.access_requests.auto_approve = True
        await self.send("/申请")
        self.adapter.session_mapper.bind("group", "G", "s1")
        self.service.interactions.add("approval", session_id="s1", ttl=0)
        with patch.object(self.service, "format_approval", return_value="pending"):
            await self.send("hello")
        self.service.post_message.assert_not_called()
        self.assertEqual(self.texts()[-1], "pending")

    async def test_concurrent_approval_and_denial_first_commit_wins(self):
        await self.send("/申请")
        row = self.pending()[0]
        store = self.adapter.access_requests.store
        results = await asyncio.gather(
            asyncio.to_thread(store.decide, row["id"], True, "ADMIN"),
            asyncio.to_thread(store.decide, row["id"], False, "ADMIN2"))
        self.assertEqual(sum(changed for _, changed in results), 1)
        self.assertEqual(results[0][0]["status"], results[1][0]["status"])
        self.assertEqual(self.allowed(), results[0][0]["status"] == "approved")

    def test_store_expiry_capacity_cooldown_and_scope_validation(self):
        store = self.adapter.access_requests.store
        row, _ = store.apply("group", "G", "NEW", "self", now=1000)
        with self.assertRaises(ValueError):
            store.apply("group", "G2", "NEW", "self", now=1010)
        with patch.object(store, "MAX_PENDING", 1):
            with self.assertRaises(ValueError):
                store.apply("group", "G", "OTHER", "self", now=1100)
        with patch("muse_bridge.gateway.access.time.time", return_value=1000 + store.PENDING_TTL):
            self.assertEqual(store.pending(), [])
        self.assertEqual(store.get(row["id"])["status"], "expired")
        for args in (("private", "OTHER", "NEW", "self"), ("private", "NEW", "NEW", "all"),
                     ("group", "*", "NEW", "self")):
            with self.assertRaises(ValueError):
                store.apply(*args)

    def test_corrupt_or_other_application_state_is_not_overwritten(self):
        path = self.root / "bad.json"
        path.write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            AccessRequestStore(path, "APP")
        self.assertEqual(path.read_text(encoding="utf-8"), "invalid")
        store = self.adapter.access_requests.store
        store.apply("group", "G", "NEW", "all", automatic=True)
        with self.assertRaises(ValueError):
            AccessRequestStore(store.path, "OTHER_APP")

    async def test_partial_admin_delivery_retries_only_failed_recipient(self):
        self.config.admin_id.append("ADMIN2")
        self.adapter.client.send_message.side_effect = [(200, {}), (500, {}), (500, {}), (200, {})]
        await self.send("/申请")
        row = self.pending()[0]
        self.assertEqual(row["notified"], ["ADMIN"])
        self.adapter.client.send_message.side_effect = None
        with patch("muse_bridge.gateway.access.time.time", return_value=row["notice_at"] + 61):
            await self.send("/申请")
        calls = self.adapter.client.send_message.call_args_list
        self.assertEqual(sum(c.args[:2] == ("private", "ADMIN") for c in calls), 1)
        self.assertEqual(sum(c.args[:2] == ("private", "ADMIN2") for c in calls), 3)
        self.assertEqual(self.pending()[0]["notified"], ["ADMIN", "ADMIN2"])

    def test_restart_and_multiple_member_grants_preserve_wildcard(self):
        store = self.adapter.access_requests.store
        first, _ = store.apply("group", "G", "FIRST", "self")
        second, _ = store.apply("group", "G", "SECOND", "self")
        restored = AccessRequestStore(store.path, "APP")
        self.assertEqual(len(restored.pending()), 2)
        restored.decide(first["id"], True, "ADMIN")
        restored.decide(second["id"], True, "ADMIN")
        self.assertTrue(restored.allows("group", "G", "FIRST"))
        self.assertTrue(restored.allows("group", "G", "SECOND"))
        self.assertFalse(restored.allows("group", "G", "THIRD"))
        pending, _ = restored.apply("group", "G", "THIRD", "self")
        restored.apply("group", "G", "FOURTH", "all", automatic=True)
        restored.decide(pending["id"], True, "ADMIN")
        again = AccessRequestStore(store.path, "APP")
        self.assertTrue(again.allows("group", "G", "ANYONE"))
        self.assertFalse(again.allows("group", "ELSE", "ANYONE"))

    def test_processed_request_cooldown_survives_restart(self):
        store = self.adapter.access_requests.store
        row, _ = store.apply("group", "G", "NEW", "self", now=1000)
        with patch("muse_bridge.gateway.access.time.time", return_value=1100):
            store.decide(row["id"], False, "ADMIN")
        restored = AccessRequestStore(store.path, "APP")
        with self.assertRaises(ValueError):
            restored.apply("group", "G", "NEW", "self", now=1699)
        new, created = restored.apply("group", "G", "NEW", "self", now=1700)
        self.assertTrue(created)
        self.assertNotEqual(new["id"], row["id"])
        self.assertFalse(restored.allows("group", "G", "NEW"))

    async def test_admin_review_exception_does_not_allow_admin_chat(self):
        await self.send("/申请审批", user="ADMIN", group=None)
        self.assertIn("暂无", self.texts()[-1])
        self.adapter.client.send_message.reset_mock()
        await self.send("ordinary admin chat", user="ADMIN", group=None)
        self.adapter.client.send_message.assert_not_called()
        self.service.post_message.assert_not_called()
        self.assertFalse(self.adapter.is_allowed("C2C_MESSAGE_CREATE", {"author": None}))

    def test_configuration_strict_boolean_and_loader(self):
        self.assertFalse(AccessRequestsConfig().auto_approve)
        for value in ("false", 0, None):
            with self.assertRaises(ValueError):
                AccessRequestsConfig(auto_approve=value)
        for value in ("", None):
            with self.assertRaises(ValueError):
                AccessRequestsConfig(state_file=value)
        path = self.root / "config.json"
        path.write_text(json.dumps({"muse": {"client_ip": "127.0.0.1"},
            "adapters": {"qq": {"access_requests": {"auto_approve": True, "state_file": str(self.root / 'access.json')}}}}), encoding="utf-8")
        loaded = ConfigLoader.load(str(path))
        self.assertTrue(loaded.qq.access_requests.auto_approve)
        self.assertTrue(loaded.qq.access_requests.enabled)

    def test_allow_all_flags_bypass_whitelist(self):
        self.assertFalse(self.allowed(user="STRANGER", group=None))
        self.assertFalse(self.allowed(user="STRANGER", group="STRANGE_GRP"))

        self.config.allow_all_private = True
        self.assertTrue(self.allowed(user="STRANGER", group=None))
        self.assertFalse(self.allowed(user="STRANGER", group="STRANGE_GRP"))

        self.config.allow_all_private = False
        self.config.allow_all_group = True
        self.assertFalse(self.allowed(user="STRANGER", group=None))
        self.assertTrue(self.allowed(user="STRANGER", group="STRANGE_GRP"))

        self.config.allow_all_private = True
        self.assertTrue(self.allowed(user="STRANGER", group=None))
        self.assertTrue(self.allowed(user="STRANGER", group="STRANGE_GRP"))

    def test_allow_all_boolean_validation(self):
        for val in ("yes", 1, None):
            with self.assertRaises(ValueError):
                QQConfig(allow_all_private=val)
            with self.assertRaises(ValueError):
                QQConfig(allow_all_group=val)

        path = self.root / "allow_all.json"
        path.write_text(json.dumps({
            "muse": {"client_ip": "127.0.0.1"},
            "adapters": {
                "qq": {
                    "allow_all_private": True,
                    "allow_all_group": True,
                }
            }
        }), encoding="utf-8")
        loaded = ConfigLoader.load(str(path))
        self.assertTrue(loaded.qq.allow_all_private)
        self.assertTrue(loaded.qq.allow_all_group)
