"""Approval queue: one card at a time, batch decisions, in-place push."""

import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from muse_bridge.adapters.onebot.adapter import OneBotAdapter
from muse_bridge.config.models import OneBotConfig
from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, MuseConfig, QQConfig
from muse_bridge.muse.service import MuseService


def option(kind, scope=None, label=None):
    return {"kind": kind, "label": {"kind": "allow_always_scoped" if scope else kind,
                                       **({"scope": scope} if scope else {})},
            "label_text": label or kind}


def row(remote_id, requested_at, session="s1", options=None, title=None):
    return {"approval_id": remote_id, "status": "pending", "scope": {"thread_id": session},
            "requested_at_ms": requested_at,
            "display": {"summary_title": title or f"Share with {remote_id}"},
            "decision_options": options if options is not None else
            [option("allow_once", label="允许一次"), option("allow_always", "destination", "总是允许此站点"),
             option("deny", label="拒绝")]}


class FakeRemote:
    """Sentinel approval API: decisions mark rows approved/denied."""

    def __init__(self, rows):
        self.rows = {r["approval_id"]: copy.deepcopy(r) for r in rows}
        self.posts = []

    def __call__(self, method, path, body=None, **kwargs):
        if path == "/approvals":
            pending = [copy.deepcopy(r) for r in self.rows.values() if r["status"] == "pending"]
            return {"pending": pending, "recent": []}
        remote_id = path.split("/")[2]
        if method == "POST":
            self.posts.append((remote_id, body))
            self.rows[remote_id]["status"] = "denied" if body["decision"].startswith("deny") else "approved"
        return copy.deepcopy(self.rows[remote_id])


class QueueCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        token = MagicMock()
        token.has_credentials.return_value = True
        self.service = MuseService(MuseConfig(session_id="s1", cookies_file=str(root / "cookies.json")), token)
        cfg = QQConfig(app_id="app", admin_id=["ADMIN"], whitelist={"private": ["ADMIN"], "G": "*"},
                       access_requests=AccessRequestsConfig(state_file=str(root / "access.local.json")),
                       session_bindings={"private:ADMIN": "s1"},
                       session_map_file=str(root / "map.json"), delivered_file=str(root / "delivered.jsonl"),
                       push_state_file=str(root / "push.json"))
        self.adapter = QQAdapter(cfg, self.service)
        self.adapter.client = MagicMock()
        self.adapter.client.send_message.return_value = (200, {})
        self.adapter.outbound_pusher.client = self.adapter.client
        self.service.post_message = MagicMock(return_value="mid")
        self.protocol = self.service.interaction_protocol
        self.addCleanup(self.service.interactions.close)

    def load(self, *rows):
        self.remote = FakeRemote(rows)
        self.protocol.request = MagicMock(side_effect=self.remote)
        self.protocol.fetch_approvals()
        return self.protocol.approval_queue("s1")

    async def send(self, text, sender="ADMIN"):
        await self.adapter.handle_event("C2C_MESSAGE_CREATE", {"id": uuid.uuid4().hex, "content": text,
                                                              "author": {"user_openid": sender}})

    def texts(self):
        return [c.args[2] for c in self.adapter.client.send_message.call_args_list]

    def test_queue_follows_request_time_and_numbers_the_round(self):
        late, early = row("remote-b", 2000), row("remote-a", 1000)
        queue = self.load(late, early)
        self.assertEqual([i.remote_id for i in queue], ["remote-a", "remote-b"])
        text = self.protocol.format_approval(queue[0])
        self.assertTrue(text.startswith(f"【审批 {queue[0].id}】1/2\nShare with remote-a"))
        self.assertIn("选项 2｜总是允许此站点", text)
        self.assertIn(f"/审批 {queue[0].id} <数字>", text)
        self.assertIn("排队中的 2 项", text)
        self.assertTrue(text.endswith("完成审批以继续聊天"))
        self.assertNotIn("Muse 会话", text)
        self.assertIn("2/2", self.protocol.format_approval(queue[1]))

    async def test_background_push_sends_only_the_queue_head(self):
        queue = self.load(row("remote-a", 1000), row("remote-b", 2000))
        notify = AsyncMock(return_value=True)
        for head in self.protocol.queue_heads():
            await self.protocol.announce(head, notify)
        for head in self.protocol.queue_heads():
            await self.protocol.announce(head, notify)
        notify.assert_awaited_once()
        self.assertIs(notify.await_args.args[0], queue[0])

    async def test_numeric_reply_decides_head_then_shows_next_card(self):
        first, second = self.load(row("remote-a", 1000), row("remote-b", 2000))
        await self.send("聊天")  # Gate shows the head card only.
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("1/2", self.texts()[0])
        self.assertNotIn(second.id, self.texts()[0])
        await self.send("1")
        self.assertEqual(self.remote.posts, [("remote-a", {"decision": "allow_once", "notification_channel": "ui"})])
        self.assertTrue(first.terminal)
        reply = self.texts()[-1]
        self.assertIn(f"【审批 {first.id}】允许一次", reply)
        self.assertIn(f"【审批 {second.id}】2/2", reply)
        self.assertEqual(len(self.texts()), 2)  # Result and next card arrive as one message.
        await self.send("3")
        self.assertEqual(self.remote.posts[-1][0], "remote-b")
        self.assertIn("审批已全部完成", self.texts()[-1])
        await self.send("你好")
        self.service.post_message.assert_called_once()

    async def test_number_all_applies_option_to_whole_queue_by_meaning(self):
        reordered = [option("deny", label="拒绝"), option("allow_once", label="允许一次")]
        self.load(row("remote-a", 1000), row("remote-b", 2000, options=reordered), row("remote-c", 3000))
        await self.send("聊天")
        await self.send("1 all")
        decided = {remote_id: body["decision"] for remote_id, body in self.remote.posts}
        self.assertEqual(decided, {"remote-a": "allow_once", "remote-b": "allow_once", "remote-c": "allow_once"})
        self.assertIn("【批量审批】「允许一次」", self.texts()[-1])
        self.assertIn("审批已全部完成", self.texts()[-1])

    async def test_all_skips_cards_without_the_same_option(self):
        no_scope = [option("allow_once", label="允许一次"), option("deny", label="拒绝")]
        first, second = self.load(row("remote-a", 1000), row("remote-b", 2000, options=no_scope))
        await self.send("聊天")
        await self.send("/审批 all 2")  # "总是允许此站点" exists only on the first card.
        self.assertEqual([r for r, _ in self.remote.posts], ["remote-a"])
        self.assertFalse(second.terminal)
        reply = self.texts()[-1]
        self.assertIn(f"{second.id}：无相同选项，保留待审批", reply)
        self.assertIn(f"【审批 {second.id}】", reply)

    async def test_all_covers_only_cards_queued_when_shown(self):
        self.load(row("remote-a", 1000), row("remote-b", 2000))
        await self.send("聊天")
        self.remote.rows["remote-c"] = row("remote-c", 3000)
        await self.send("all 1")
        self.assertEqual(sorted(r for r, _ in self.remote.posts), ["remote-a", "remote-b"])
        self.assertFalse(self.protocol.manager.by_remote("approval", "remote-c").terminal)
        self.assertIn("3/3", self.texts()[-1])  # The late card joins the round still in progress.

    async def test_all_needs_the_card_to_have_been_seen(self):
        self.load(row("remote-a", 1000), row("remote-b", 2000))
        await self.send("2 all")
        self.assertEqual(self.remote.posts, [])
        self.assertIn("请先查看当前审批卡", self.texts()[-1])

    async def test_digits_without_pending_approval_are_still_chat(self):
        self.load()
        await self.send("2 all")
        await self.send("1")
        self.assertEqual(self.service.post_message.call_count, 2)

    async def test_non_admin_cannot_decide_queue(self):
        self.adapter.config.whitelist["private"].append("OTHER")
        self.adapter.session_mapper.bind("private", "OTHER", "s1")  # Shares the session with the queue.
        self.load(row("remote-a", 1000), row("remote-b", 2000))
        await self.send("1 all", sender="OTHER")
        self.assertEqual(self.remote.posts, [])
        self.assertIn("仅限", self.texts()[-1])

    async def test_admin_private_answers_queue_pushed_from_other_session(self):
        self.service.interactions.managed_sessions.add("s2")
        self.adapter.session_mapper.bind("group", "G", "s2")
        self.adapter.config.approvals_in_place = False
        self.load(row("remote-a", 1000, session="s2"), row("remote-b", 2000, session="s2"))
        queue = self.protocol.approval_queue("s2")
        self.assertEqual(len(queue), 2)
        await self.adapter.notify_interaction(queue[0], self.protocol.format_approval(queue[0]))
        self.adapter.session_mapper.bind("private", "ADMIN", "s9")
        await self.send("1 all")
        self.assertEqual(sorted(r for r, _ in self.remote.posts), ["remote-a", "remote-b"])

    def bind_group_only(self):
        # s1 lives in group G; the admin's private chat is on another session.
        self.adapter.session_mapper.bind("private", "ADMIN", "s9")
        self.adapter.session_mapper.bind("group", "G", "s1")

    async def test_in_place_push_skips_admin_copy(self):
        self.bind_group_only()
        self.adapter.config.approvals_in_place = True
        queue = self.load(row("remote-a", 1000))
        pusher = self.adapter.outbound_pusher
        pusher._send_to_qq = AsyncMock(return_value=True)
        await pusher.notify_interaction(queue[0], "card")
        self.assertEqual([c.args[:2] for c in pusher._send_to_qq.await_args_list], [("group", "G")])

    async def test_in_place_push_falls_back_to_admin_when_chat_fails(self):
        self.bind_group_only()
        self.adapter.config.approvals_in_place = True
        queue = self.load(row("remote-a", 1000))
        pusher = self.adapter.outbound_pusher
        pusher._send_to_qq = AsyncMock(side_effect=lambda t, i, *a, **k: t == "private")
        self.assertTrue(await pusher.notify_interaction(queue[0], "card"))
        self.assertEqual([c.args[:2] for c in pusher._send_to_qq.await_args_list],
                         [("group", "G"), ("private", "ADMIN")])

    async def test_copies_admin_when_in_place_disabled(self):
        self.bind_group_only()
        self.adapter.config.approvals_in_place = False
        queue = self.load(row("remote-a", 1000))
        pusher = self.adapter.outbound_pusher
        pusher._send_to_qq = AsyncMock(return_value=True)
        await pusher.notify_interaction(queue[0], "card")
        self.assertEqual([c.args[:2] for c in pusher._send_to_qq.await_args_list],
                         [("group", "G"), ("private", "ADMIN")])

    async def test_push_skips_chat_that_already_shows_current_card(self):
        queue = self.load(row("remote-a", 1000), row("remote-b", 2000))
        await self.send("聊天")
        await self.send("1")  # Advances to remote-b in this chat.
        pusher = self.adapter.outbound_pusher
        pusher._send_to_qq = AsyncMock(return_value=True)
        self.assertTrue(await pusher.notify_interaction(queue[1], "card"))
        pusher._send_to_qq.assert_not_awaited()

    async def quoted(self, text, message_id="card-1", *, owner=None, session="s1", admin=True):
        owner = owner or ("private", "ADMIN", "ADMIN")
        return await self.adapter.interaction_router.handle(
            owner, text, uuid.uuid4().hex, admin, session,
            event={"message_reference": {"message_id": message_id}})

    async def show_quoted_card(self, item):
        self.adapter.client.send_message.return_value = (200, {"id": "card-1"})
        await self.adapter.interaction_router.display_approval(
            ("private", "ADMIN", "ADMIN"), item, "incoming")

    async def test_quoted_success_and_replay_do_not_decide_next_card(self):
        first, second = self.load(row("remote-a", 1), row("remote-b", 2))
        await self.show_quoted_card(first)
        # New replies have different receipts; the old quote stays a tombstone.
        self.adapter.client.send_message.return_value = (200, {"id": "card-2"})
        self.assertTrue(await self.quoted("1"))
        self.assertIn("服务端状态：approved", self.texts()[-1])
        self.assertIn(second.id, self.texts()[-1])
        await self.quoted("1")
        self.assertIn("已结束", self.texts()[-1])
        self.assertEqual(len(self.remote.posts), 1)
        self.assertFalse(second.terminal)
        self.service.post_message.assert_not_called()

    async def test_quoted_timed_out_card_has_feedback_without_post(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        self.remote.rows["remote-a"]["status"] = "timed_out"
        await self.quoted("1")
        self.assertIn("其他客户端结束", self.texts()[-1])
        self.assertIn("审批已全部完成", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])

    async def test_batch_expiry_is_not_reported_as_successful_choice(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        self.remote.rows["remote-a"]["status"] = "timed_out"
        await self.send("1 all")
        self.assertIn("已过期", self.texts()[-1])
        self.assertNotIn(f"{item.id}：已完成", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])

    async def test_quoted_already_expired_card_has_explicit_feedback(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        self.service.interactions.finish(item, "timed_out")
        await self.quoted("1")
        self.assertIn("已过期", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])
        self.service.post_message.assert_not_called()

    async def test_quoted_refresh_failure_reports_unconfirmed_without_post(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        self.protocol.request.side_effect = TimeoutError("refresh failed")
        await self.quoted("1")
        self.assertIn("结果暂未确认", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])
        self.assertFalse(item.terminal)

    def test_receipt_reference_kinds_are_scoped_and_accept_onebot_numeric_ids(self):
        item, = self.load(row("remote-a", 1))
        snapshot = self.protocol.display_snapshot(item)
        self.protocol.remember_approval_message(("group", "7"),
            {"message_id": 123, "ext_info": {"ref_idx": "ref-1"}}, [item], [snapshot])
        expected = [(item.id, snapshot)]
        self.assertEqual(self.protocol.referenced_approvals(("group", "7"), [("id", "123")]), expected)
        self.assertEqual(self.protocol.referenced_approvals(("group", "7"), [("ref", "ref-1")]), expected)
        self.assertIsNone(self.protocol.referenced_approvals(("group", "8"), [("id", "123")]))
        self.assertIsNone(self.protocol.referenced_approvals(("group", "7"), [("id", "123"), ("id", "other")]))

    async def test_quoted_uncertain_failure_never_reposts(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        def uncertain(method, path, body=None, **kwargs):
            if method == "POST":
                self.remote.posts.append(("remote-a", body))
                raise TimeoutError("response lost")
            return self.remote(method, path, body, **kwargs)
        self.protocol.request.side_effect = uncertain
        await self.quoted("1")
        self.assertIn("结果未确认", self.texts()[-1])
        await self.quoted("1")
        self.assertIn("不会重复发送决定", self.texts()[-1])
        self.assertEqual(len(self.remote.posts), 1)
        self.assertFalse(item.terminal)

    async def test_quoted_changed_revision_does_not_reinterpret_old_choice(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        self.remote.rows["remote-a"]["decision_options"].reverse()
        self.adapter.client.send_message.return_value = (200, {"id": "new-card"})
        await self.quoted("1")
        self.assertIn("已变化", self.texts()[-1])
        await self.quoted("1")
        self.assertIn("已变化", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])

    async def test_quoted_permissions_and_group_binding_are_preserved(self):
        item, = self.load(row("remote-a", 1))
        await self.show_quoted_card(item)
        await self.quoted("1", admin=False)
        self.assertIn("仅限", self.texts()[-1])
        owner = ("group", "G", "ADMIN")
        self.protocol.remember_approval_message(owner[:2], {"id": "card-1"},
                                              [item], [self.protocol.display_snapshot(item)])
        await self.quoted("1", owner=owner, session="different")
        self.assertIn("不属于当前群", self.texts()[-1])
        self.assertEqual(self.remote.posts, [])

    async def test_unverified_background_quote_redisplays_without_decision(self):
        item, = self.load(row("remote-a", 1))
        await self.adapter.notify_interaction(item, self.protocol.format_approval(item))
        self.assertTrue(await self.quoted("1", "background-card"))
        self.assertIn("无法验证", self.texts()[-1])
        self.assertIn(item.id, self.texts()[-1])
        self.assertEqual(self.remote.posts, [])
        self.service.post_message.assert_not_called()

    async def test_onebot_ordinary_approvals_report_results_for_every_group_trigger(self):
        for trigger in ("all", "at", "prefix"):
            for mode in ("number", "batch", "command"):
                with self.subTest(trigger=trigger, mode=mode):
                    root = Path(self.tmp.name) / f"{trigger}-{mode}"
                    root.mkdir()
                    cfg = OneBotConfig(admin_id=["42"], group_trigger=trigger, whitelist={"7": "*"},
                        session_bindings={"group:7": "s1"},
                        access_requests=AccessRequestsConfig(state_file=str(root / "access.json")),
                        session_map_file=str(root / "map.json"), delivered_file=str(root / "delivered.jsonl"),
                        push_state_file=str(root / "push.json"))
                    adapter = OneBotAdapter(cfg, self.service)
                    adapter.client = MagicMock()
                    adapter.client.send_msg.return_value = {"message_id": 100}
                    adapter.outbound_pusher.client = adapter.client
                    item, = self.load(row(f"{trigger}-{mode}", 1))
                    await adapter.notify_interaction(item, self.protocol.format_approval(item))
                    text = {"number": "1", "batch": "1 all", "command": f"/审批 {item.id} 1"}[mode]
                    await adapter.handle_event({"post_type": "message", "message_type": "group", "group_id": 7,
                        "user_id": 42, "self_id": 99, "message_id": 1,
                        "message": [{"type": "text", "data": {"text": text}}]})
                    self.assertEqual(len(self.remote.posts), 1)
                    self.assertIn("审批已全部完成", str(adapter.client.send_msg.call_args))
                    self.assertIn("已完成" if mode == "batch" else "服务端状态：approved",
                                  str(adapter.client.send_msg.call_args))
                    self.service.post_message.assert_not_called()

    async def test_ordinary_command_and_batch_refresh_failures_report_unconfirmed(self):
        for mode in ("command", "batch"):
            with self.subTest(mode=mode):
                item, = self.load(row(mode, 1))
                await self.show_quoted_card(item)
                self.protocol.request.side_effect = TimeoutError("refresh failed")
                text = f"/审批 {item.id} 1" if mode == "command" else "1 all"
                await self.send(text)
                self.assertIn("未确认", self.texts()[-1])
                self.assertEqual(self.remote.posts, [])
                self.assertFalse(item.terminal)
                self.service.interactions.finish(item, "cancelled")

    async def test_onebot_group_quote_bypasses_at_trigger_and_reports_result(self):
        root = Path(self.tmp.name)
        cfg = OneBotConfig(admin_id=["42"], group_trigger="at", whitelist={"7": "*"},
                           session_bindings={"group:7": "s1"},
                           access_requests=AccessRequestsConfig(state_file=str(root / "ob-access.json")),
                           session_map_file=str(root / "ob-map.json"),
                           delivered_file=str(root / "ob-delivered.jsonl"),
                           push_state_file=str(root / "ob-push.json"))
        adapter = OneBotAdapter(cfg, self.service)
        adapter.client = MagicMock()
        adapter.client.send_msg.return_value = {"message_id": 100}
        adapter.outbound_pusher.client = adapter.client
        item, = self.load(row("remote-a", 1))
        await adapter.notify_interaction(item, self.protocol.format_approval(item))
        event = {"post_type": "message", "message_type": "group", "group_id": 7,
                 "user_id": 42, "self_id": 99, "message_id": 1,
                 "message": [{"type": "reply", "data": {"id": "100"}},
                             {"type": "text", "data": {"text": "1"}}]}
        adapter.client.send_msg.return_value = {"message_id": 101}
        await adapter.handle_event(event)
        self.assertEqual(len(self.remote.posts), 1)
        self.assertIn("服务端状态：approved", str(adapter.client.send_msg.call_args))
        calls = adapter.client.send_msg.call_count
        await adapter.handle_event(event)  # Same incoming message is deduplicated.
        self.assertEqual(adapter.client.send_msg.call_count, calls)
        self.service.post_message.assert_not_called()

    async def test_quoted_all_uses_original_queue_snapshot(self):
        self.load(row("remote-a", 1), row("remote-b", 2))
        await self.show_quoted_card(self.protocol.approval_queue("s1")[0])
        self.remote.rows["remote-c"] = row("remote-c", 3)
        await self.quoted("1 all")
        self.assertEqual([key for key, _ in self.remote.posts], ["remote-a", "remote-b"])
        self.assertIn("批量审批", self.texts()[-1])

    def test_approvals_in_place_config(self):
        self.assertTrue(QQConfig().approvals_in_place)
        with self.assertRaises(ValueError):
            QQConfig(approvals_in_place="yes")
        path = Path(self.tmp.name) / "config.json"
        path.write_text('{"muse": {}, "adapters": {"qq": {"approvals_in_place": false}}}', encoding="utf-8")
        self.assertFalse(ConfigLoader.load(str(path)).qq.approvals_in_place)


if __name__ == "__main__":
    unittest.main()
