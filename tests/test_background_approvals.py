"""Offline background approval routing through real gateway/interaction boundaries."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from muse_bridge.adapters.onebot.adapter import OneBotAdapter
from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.config.models import (
    AccessRequestsConfig, AppConfig, ApprovalConfig, ConfigLoader, GatewayConfig,
    MuseConfig, OneBotConfig, QQConfig,
)
from muse_bridge.engine import BridgeEngine
from muse_bridge.gateway.admin_activity import AdminPrivateActivity
from muse_bridge.muse.interactions import ApprovalBlocked
from muse_bridge.muse.service import MuseService


def background(remote_id="scheduled-1", **overrides):
    return {
        "approval_id": remote_id, "status": "pending", "requested_at_ms": 1000,
        "scope": {"scope_type": "schedule", "schedule_id": "test-email",
                  "session_scope_id": "worker-submission", "requester_source": "scheduler.cron",
                  "runtime_request_trace": {"thread_id": None}},
        "display": {"attention_mode": "background", "summary_title": "发送测试邮件",
                    "purpose_summary": "这是预定任务测试邮件。"},
        "is_exclusive": True, "message_revision": 0,
        "decision_options": [
            {"kind": "allow_once", "label": {"kind": "allow_once"}, "label_text": "发送"},
            {"kind": "allow_session", "label": {"kind": "allow_for_this_scheduled_task"},
             "label_text": "始终允许这项定时任务"},
            {"kind": "deny", "label": {"kind": "deny"}, "label_text": "不发送"},
        ], **overrides,
    }


class Remote:
    def __init__(self, *rows):
        self.rows = {r["approval_id"]: copy.deepcopy(r) for r in rows}
        self.posts = []

    def __call__(self, method, path, body=None, **kwargs):
        if path == "/approvals":
            return {"pending_approvals": [copy.deepcopy(r) for r in self.rows.values()
                                          if r["status"] == "pending"], "recent_approvals": []}
        key = path.split("/")[2]
        if method == "POST":
            self.posts.append((key, copy.deepcopy(body)))
            self.rows[key]["status"] = "denied" if body["decision"] == "deny" else "approved"
        return {"approval": copy.deepcopy(self.rows[key])}


class BackgroundCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = MuseService(MuseConfig(cookies_file=str(self.root / "cookies.json")), MagicMock())
        self.service.token_manager.has_credentials.return_value = True
        self.service.post_message = MagicMock(return_value="message")
        self.addCleanup(self.service.interactions.close)
        self.p = self.service.interaction_protocol
        self.remote = Remote(background())
        self.p.request = MagicMock(side_effect=self.remote)
        self.qq = QQAdapter(self.config(QQConfig, "qq", ["ADMIN", "SECOND"]), self.service)
        self.ob = OneBotAdapter(self.config(OneBotConfig, "onebot", ["12345"]), self.service)
        self.qq.client = MagicMock()
        self.qq.client.send_message.return_value = (200, {"id": "sent-card"})
        self.qq.outbound_pusher.client = self.qq.client
        self.ob.outbound_pusher._deliver = AsyncMock(return_value=True)
        self.engine = BridgeEngine.__new__(BridgeEngine)
        self.engine.config = AppConfig(qq=self.qq.config, onebot=self.ob.config)
        self.engine.adapters = [self.qq, self.ob]
        self.engine.muse_service = self.service

    def config(self, cls, name, admins):
        credentials = ({"app_id": "test-app", "app_secret": "test-secret"} if cls is QQConfig
                       else {"base_url": "http://onebot.invalid"})
        return cls(**credentials, admin_id=admins, enabled=True,
                   whitelist={"private": admins + ["OTHER"], "G": "*"},
                   routes_file=str(self.root / (name + "-routes.json")),
                   access_requests=AccessRequestsConfig(state_file=str(self.root / (name + "-access.json"))),
                   delivered_file=str(self.root / (name + "-delivered.jsonl")),
                   session_map_file=str(self.root / (name + "-sessions.json")),
                   push_state_file=str(self.root / (name + "-push.json")))

    def load(self):
        self.p.fetch_approvals()
        return self.service.interactions.by_remote("approval", "scheduled-1")

    async def send(self, text, user="ADMIN", group=False, quote=None):
        event = {"id": uuid.uuid4().hex, "content": text,
                 "author": {"member_openid" if group else "user_openid": user}}
        if quote:
            event["message_reference"] = {"message_id": quote}
        if group:
            event["group_openid"] = "G"
        await self.qq.handle_event("GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE", event)

    async def tick(self, notify=None):
        # Policy/routing tests drive one completed synchronization, without
        # starting the separately tested account-wide socket worker.
        pending = await self.p.reconcile()
        await self.p._notify_pending(notify or self.engine._notify_interaction, pending)

    def test_schedule_is_independent_of_every_chat_gate(self):
        item = self.load()
        self.assertTrue(item.context["background_approval"])
        self.assertEqual(item.session_id, "")
        self.assertNotIn("worker-submission", self.service.interactions.managed_sessions)
        self.assertEqual(self.p.queue_heads(), [item])
        for sid in ("s1", "s2", "worker-submission"):
            self.service.check_approval_gate(sid)
            self.assertEqual(self.p.approval_queue(sid), [])
        self.assertIn(f"【后台审批 {item.id}】", self.p.format_approval(item))
        self.assertNotIn("后台任务等待审批", self.p.format_approval(item))
        self.assertTrue(self.service.interactions.approvals_ready)

    def test_schedule_with_unmanaged_thread_still_enters_background_queue(self):
        self.remote.rows["scheduled-1"]["scope"]["thread_id"] = "not-managed"
        self.assertIsNotNone(self.load())
        self.assertEqual(self.p.approval_queue("not-managed"), [])

    def test_snapshot_and_legacy_detail_both_supply_background_evidence(self):
        for source in ("display", "scope", "detail"):
            with self.subTest(source=source):
                service = MuseService(self.service.config, MagicMock())
                self.addCleanup(service.interactions.close)
                row = background()
                detail = copy.deepcopy(row)
                if source == "display":
                    row["scope"] = None
                    detail = {"approval_id": row["approval_id"], "status": "pending", "scope": None}
                elif source == "scope":
                    row["display"] = {}
                    detail = {"approval_id": row["approval_id"], "status": "pending", "decision_options": ["allow_once"]}
                else:
                    row["scope"] = None
                    row["display"] = {}
                    detail["scope_type"] = "schedule"
                    detail["scope"] = None
                service.interaction_protocol.request = MagicMock(side_effect=[
                    {"pending": [row], "recent": []}, {"approval": detail}])
                items = service.interaction_protocol.fetch_approvals()
                self.assertEqual(len(items), 1)
                self.assertTrue(items[0].context["background_approval"])
                self.assertEqual(items[0].context["options"][1]["decision"], "allow_session")

    def test_unknown_unscoped_approval_still_fails_closed(self):
        self.remote.rows["scheduled-1"] = background(scope=None, display={})
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")
        self.assertFalse(self.service.interactions.approvals_ready)

    def test_foreground_gate_and_background_queue_remain_separate(self):
        self.service.interactions.managed_sessions.add("s1")
        self.remote.rows["foreground"] = background("foreground", scope={"thread_id": "s1"}, display={})
        self.load()
        self.assertEqual(len(self.p.queue_heads()), 2)
        self.assertEqual(len(self.p.approval_queue("s1")), 1)
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")
        self.service.check_approval_gate("s2")

    def test_web_decision_ends_local_background_wait(self):
        item = self.load()
        self.remote.rows["scheduled-1"]["status"] = "approved"
        self.p.fetch_approvals()
        self.assertTrue(item.terminal)
        self.assertEqual(self.p.queue_heads(), [])
        self.assertEqual(self.remote.posts, [])

    def test_pending_restored_without_any_chat_binding(self):
        self.load()
        restored = MuseService(self.service.config, MagicMock())
        self.addCleanup(restored.interactions.close)
        restored.interaction_protocol.request = MagicMock(side_effect=self.remote)
        self.assertEqual(len(restored.interaction_protocol.fetch_approvals()), 1)
        self.assertEqual(restored.interactions.managed_sessions, set())

    async def test_no_admin_activity_retains_pending_without_broadcast(self):
        item = self.load()
        self.assertFalse(await self.p.announce(item, self.engine._notify_interaction))
        self.qq.client.send_message.assert_not_called()
        self.ob.outbound_pusher._deliver.assert_not_awaited()
        self.assertFalse(item.context.get("notified", False))
        await self.send("/聊天")
        self.assertTrue(await self.p.announce(item, self.engine._notify_interaction))
        self.assertEqual(self.qq.client.send_message.call_args.args[:2], ("private", "ADMIN"))
        self.assertFalse(self.qq.session_mapper.current("private", "ADMIN"))

    async def test_explicit_target_needs_no_activity_or_session_binding(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:SECOND")
        self.qq.session_mapper.bind("group", "G", "unrelated")
        item = self.load()
        self.assertTrue(await self.p.announce(item, self.engine._notify_interaction))
        self.assertEqual(self.qq.client.send_message.call_args.args[:2], ("private", "SECOND"))
        self.ob.outbound_pusher._deliver.assert_not_awaited()
        self.assertEqual(self.qq.session_mapper.snapshot(), {"group:G": "unrelated"})

    async def test_selects_one_latest_admin_across_gateways(self):
        await self.send("/聊天")
        with patch("muse_bridge.gateway.admin_activity.time.time", return_value=4000000000):
            self.ob.admin_private_activity.record(self.ob._admin_activity_identity(), "12345", ["12345"])
        self.qq.client.send_message.reset_mock()
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        self.qq.client.send_message.assert_not_called()
        self.ob.outbound_pusher._deliver.assert_awaited_once()
        self.assertEqual(self.ob.outbound_pusher._deliver.await_args.args[:2], ("private", "12345"))

    async def test_failed_send_keeps_selected_inbox_for_retry(self):
        await self.send("/聊天")
        item = self.load()
        self.qq.outbound_pusher._send_to_qq = AsyncMock(return_value=False)
        self.assertFalse(await self.p.announce(item, self.engine._notify_interaction))
        await self.send("/聊天", user="SECOND")
        self.qq.outbound_pusher._send_to_qq.return_value = True
        self.assertTrue(await self.p.announce(item, self.engine._notify_interaction))
        self.assertEqual([c.args[:2] for c in self.qq.outbound_pusher._send_to_qq.await_args_list],
                         [("private", "ADMIN"), ("private", "ADMIN")])

    async def test_background_push_and_listing_never_acquire_chat_input(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        item = self.load()
        self.qq.session_mapper.bind("private", "ADMIN", "s1")
        self.qq.outbound_pusher.ensure_baseline = AsyncMock()
        await self.p.announce(item, self.engine._notify_interaction)
        for listed in (False, True):
            if listed:
                await self.send("/审批")
            for text in ("1", "99", "1 all", "全部 2", "取消", "name\npassword", "普通消息"):
                with self.subTest(listed=listed, text=text):
                    self.service.post_message.reset_mock()
                    self.assertFalse(self.qq.interaction_router.is_credential_input(
                        ("private", "ADMIN", "ADMIN"), text, {}))
                    await self.send(text)
                    self.service.post_message.assert_called_once()
                    self.assertEqual(self.service.post_message.call_args.args[:2], ("s1", text))
                    self.assertEqual(self.remote.posts, [])
                    self.assertFalse(item.terminal)
        hint = self.p.format_approval(item)
        self.assertIn("必须引用本卡回复数字", hint)
        self.assertNotIn("直接发送数字仍作为普通聊天转发", hint)
        self.assertNotIn("可直接回复数字审批", hint)

    async def test_private_quoted_choice_uses_background_queue(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        await self.send("1", quote="sent-card")
        self.assertTrue(item.terminal)
        self.assertEqual(self.remote.posts[0][1]["decision"], "allow_once")
        self.service.post_message.assert_not_called()

    async def test_unrelated_quote_does_not_enter_background_approval_flow(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        self.load()
        await self.p.announce(self.load(), self.engine._notify_interaction)
        router = self.qq.interaction_router
        event = {"message_reference": {"message_id": "ordinary-message"}}
        self.assertFalse(router.is_credential_input(("private", "ADMIN", "ADMIN"), "1", event))
        self.assertFalse(await router.handle(("private", "ADMIN", "ADMIN"), "1", "in", True, "", event))
        self.assertEqual(self.remote.posts, [])

    async def test_quoted_card_invalid_choice_stays_in_explicit_quote_flow(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        await self.send("不是选项", quote="sent-card")
        self.assertEqual(self.remote.posts, [])
        self.service.post_message.assert_not_called()
        self.assertIn("选项数字", self.qq.client.send_message.call_args.args[2])

    async def test_quote_replay_cannot_decide_next_background_card(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        self.remote.rows["scheduled-2"] = background("scheduled-2", requested_at_ms=2000)
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        router = self.qq.interaction_router
        event = {"message_reference": {"message_id": "sent-card"}}
        self.qq.client.send_message.return_value = (200, {"id": "next-card"})
        for _ in range(2):
            await router.handle(("private", "ADMIN", "ADMIN"), "1", "incoming", True, "", event=event)
        self.assertEqual(len(self.remote.posts), 1)
        self.assertFalse(self.service.interactions.by_remote("approval", "scheduled-2").terminal)

    async def test_group_admin_cannot_read_or_decide_background_card(self):
        item = self.load()
        await self.send(f"/审批 {item.id} 1", group=True)
        self.assertEqual(self.remote.posts, [])
        self.assertIn("不属于当前群", self.qq.client.send_message.call_args.args[2])
        self.assertNotIn("发送测试邮件", self.qq.client.send_message.call_args.args[2])

    async def test_nonadmin_private_cannot_decide(self):
        item = self.load()
        await self.send(f"/审批 {item.id} 1", user="OTHER")
        self.assertEqual(self.remote.posts, [])
        self.assertIsNone(self.qq.latest_admin_private())

    async def test_auto_allow_and_deny_do_not_depend_on_recipient_or_notify(self):
        for mode, decision in (("allow_all", "allow_once"), ("deny_all", "deny")):
            for notify in (True, False):
                with self.subTest(mode=mode, notify=notify):
                    key = f"{mode}-{notify}"
                    self.remote.rows = {key: background(key)}
                    self.service.config.approval = ApprovalConfig(mode=mode, notify=notify)
                    await self.tick()
                    self.assertEqual(self.remote.posts[-1], (key, {
                        "decision": decision, "notification_channel": "ui", "expected_message_revision": 0}))
                    self.assertTrue(self.service.interactions.by_remote("approval", key).terminal)
        self.qq.client.send_message.assert_not_called()
        self.ob.outbound_pusher._deliver.assert_not_awaited()

    async def test_auto_result_retried_after_admin_arrives_without_redeciding(self):
        self.service.config.approval = ApprovalConfig(mode="allow_all", notify=True)
        await self.tick()
        item = self.service.interactions.by_remote("approval", "scheduled-1")
        self.assertIn("auto_notification", item.context)
        await self.send("/聊天")
        self.qq.client.send_message.reset_mock()
        await self.tick()
        self.assertEqual(len(self.remote.posts), 1)
        self.assertNotIn("auto_notification", item.context)
        self.qq.client.send_message.assert_called_once()
        self.assertIn("approved", self.qq.client.send_message.call_args.args[2])
        await self.tick()
        self.qq.client.send_message.assert_called_once()

    async def test_changed_revision_requires_new_choice(self):
        item = self.load()
        await self.send("/审批")
        self.remote.rows["scheduled-1"]["message_revision"] = 1
        await self.send(f"/审批 {item.id} 1")
        self.assertEqual(self.remote.posts, [])
        self.assertIn("变化", self.qq.client.send_message.call_args.args[2])

    async def test_background_queue_does_not_steal_foreground_gate_input(self):
        self.engine.config.gateway = GatewayConfig(background_approval_push="qq:ADMIN")
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        self.qq.session_mapper.bind("private", "ADMIN", "s1")
        self.service.interactions.managed_sessions.add("s1")
        self.remote.rows["foreground"] = background("foreground", scope={"thread_id": "s1"}, display={})
        self.p.fetch_approvals()
        await self.send("待审批")  # Show the current session's foreground gate.
        await self.send("1")
        self.assertEqual([key for key, _ in self.remote.posts], ["foreground"])
        self.assertFalse(item.terminal)

    async def test_background_batch_advances_only_background_queue(self):
        self.remote.rows["scheduled-2"] = background("scheduled-2", requested_at_ms=2000)
        item = self.load()
        await self.send("/审批")
        await self.send(f"/审批 {item.id} 1 全部")
        self.assertEqual({key for key, _ in self.remote.posts}, {"scheduled-1", "scheduled-2"})
        self.assertEqual(self.p.queue_heads(), [])

    async def test_lost_admin_privilege_and_disabled_gateway_are_not_destinations(self):
        await self.send("/聊天")
        item = self.load()
        self.qq.config.admin_id.remove("ADMIN")
        self.qq.client.send_message.reset_mock()
        self.assertFalse(await self.p.announce(item, self.engine._notify_interaction))
        self.qq.client.send_message.assert_not_called()
        await self.send("/聊天", user="SECOND")
        self.qq.config.enabled = False
        self.assertFalse(await self.p.announce(item, self.engine._notify_interaction))

    async def test_native_scheduled_task_option_is_not_replaced_by_chat_permission(self):
        item = self.load()
        await self.send("/审批")
        await self.send(f"/审批 {item.id} 2")
        self.assertEqual(self.remote.posts[0][1]["decision"], "allow_session")
        self.assertNotIn("always_scope", self.remote.posts[0][1])
        self.assertTrue(item.terminal)

    async def test_background_uncertain_decision_allows_explicit_retry(self):
        item = self.load()
        posts = []
        def uncertain(method, path, body=None, **kwargs):
            if method == "POST":
                posts.append(body)
                raise TimeoutError()
            return self.remote(method, path, body, **kwargs)
        self.p.request.side_effect = uncertain
        await self.p.submit_approval(item, "1")
        await self.p.submit_approval(item, "1")
        self.assertEqual(len(posts), 2)
        self.assertTrue(item.context["decision_uncertain"])
        self.service.check_approval_gate("unrelated-chat")

    async def test_local_commands_record_activity_but_group_and_outbound_do_not(self):
        await self.send("/聊天")
        first = self.qq.latest_admin_private()
        await self.send("/审批", user="SECOND", group=True)
        self.assertEqual(self.qq.latest_admin_private(), first)
        item = self.load()
        await self.p.announce(item, self.engine._notify_interaction)
        self.assertEqual(self.qq.latest_admin_private(), first)
        restored = AdminPrivateActivity(str(self.qq.config.routes_file) + ".admins.local.json")
        self.assertEqual(restored.latest(self.qq._admin_activity_identity(), ["ADMIN"]), first)
        self.assertIsNone(restored.latest("other-account", ["ADMIN"]))
        self.assertIsNone(restored.latest(self.qq._admin_activity_identity(), ["REMOVED"]))


class BackgroundConfigCase(unittest.TestCase):
    def test_destination_syntax_and_admin_validation(self):
        for value in (None, True, [], "qq:", "qq: OTHER", "qq:OTHER:bad", "wechat:OTHER", "group:G"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GatewayConfig(background_approval_push=value)
        for target in ("qq:OTHER", "qq:*", "onebot:ADMIN"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                AppConfig(qq=QQConfig(admin_id=["ADMIN"]),
                          gateway=GatewayConfig(background_approval_push=target))
        with self.assertRaises(ValueError):
            AppConfig(qq=QQConfig(enabled=False, admin_id=["ADMIN"]),
                      gateway=GatewayConfig(background_approval_push="qq:ADMIN"))
        self.assertEqual(GatewayConfig().background_approval_push, "")
        AppConfig(qq=QQConfig(admin_id=["ADMIN"]),
                  gateway=GatewayConfig(background_approval_push="qq:ADMIN"))

    def test_gateway_editor_can_set_clear_and_reject_invalid_destination(self):
        from init_config import FullConfigEditor
        editor = FullConfigEditor({})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with patch("builtins.input", side_effect=["2", "qq:ADMIN", "0"]):
                editor._edit_gateway_category()
            self.assertEqual(editor.data["gateway"]["background_approval_push"], "qq:ADMIN")
            with patch("builtins.input", side_effect=["2", "group:G", "0"]):
                editor._edit_gateway_category()
            self.assertEqual(editor.data["gateway"]["background_approval_push"], "qq:ADMIN")
            with patch("builtins.input", side_effect=["2", "", "0"]):
                editor._edit_gateway_category()
            self.assertEqual(editor.data["gateway"]["background_approval_push"], "")
        self.assertIn("后台审批推送", output.getvalue())

    def test_loader_accepts_destination_and_keeps_legacy_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            raw = {"muse": {"client_ip": "127.0.0.1"},
                   "adapters": {"qq": {"enabled": True, "admin_id": ["ADMIN"]}},
                   "gateway": {"background_approval_push": "qq:ADMIN"}}
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(ConfigLoader.load(str(path)).gateway.background_approval_push, "qq:ADMIN")
            raw.pop("gateway")
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(ConfigLoader.load(str(path)).gateway.background_approval_push, "")


if __name__ == "__main__":
    unittest.main()
