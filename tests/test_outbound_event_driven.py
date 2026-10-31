"""Tests for the event-driven outbound pusher and WebSocket subscription flow."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from muse_bridge.adapters.qq.outbound import QQOutboundPusher
from muse_bridge.adapters.qq.route_manager import RouteManager
from muse_bridge.config.models import QQConfig, QQPushConfig
from muse_bridge.core.session_map import MODE_GLOBAL, MODE_PER_CHAT, SessionMapper
from muse_bridge.muse.client.client import MuseClient, PushSubscription
from muse_bridge.muse.client.errors import SubscribeError
from muse_bridge.muse.client.push import PushTurnTracker


class TestPushTurnTracker(unittest.TestCase):
    def test_event_session_extraction(self):
        # 1. Direct session_id in payload
        ev1 = {"event": "delta.message_done", "payload": {"session_id": "sid-1"}}
        self.assertEqual(PushTurnTracker._event_session(ev1), "sid-1")

        # 2. thread_id in payload
        ev2 = {"event": "delta.message_done", "payload": {"thread_id": "sid-2"}}
        self.assertEqual(PushTurnTracker._event_session(ev2), "sid-2")

        # 3. chat_context.chat_id in payload
        ev3 = {"event": "delta.message_done", "payload": {"chat_context": {"chat_id": "sid-3"}}}
        self.assertEqual(PushTurnTracker._event_session(ev3), "sid-3")

        # 4. Top-level session_id
        ev4 = {"event": "delta.message_done", "session_id": "sid-4"}
        self.assertEqual(PushTurnTracker._event_session(ev4), "sid-4")

    def test_turn_completion_requires_message_done_and_task_completed(self):
        tracker = PushTurnTracker("sid-1")
        self.assertFalse(tracker.done)

        # Message streaming
        tracker.feed({"event": "delta.message_start", "payload": {"session_id": "sid-1"}})
        self.assertTrue(tracker.text_streaming)
        self.assertFalse(tracker.done)

        tracker.feed({"event": "delta.text_append", "payload": {"session_id": "sid-1", "text": "hello"}})
        self.assertFalse(tracker.done)

        # Message done (assistant finished text, but root task not yet marked completed)
        tracker.feed({"event": "delta.message_done", "payload": {"session_id": "sid-1", "status": "completed"}})
        self.assertFalse(tracker.text_streaming)
        self.assertTrue(tracker.seen_message_done)
        self.assertFalse(tracker.done)

        # Subagent task completion does not finish root turn
        tracker.feed({
            "event": "task.status",
            "payload": {
                "session_id": "sid-1",
                "status": "completed",
                "task_id": "subagent-1",
                "root_agent_id": "root-agent",
            },
        })
        self.assertFalse(tracker.done)

        # Root task completed finishes the turn
        tracker.feed({
            "event": "task.status",
            "payload": {
                "session_id": "sid-1",
                "status": "completed",
                "task_id": "root-agent",
                "root_agent_id": "root-agent",
            },
        })
        self.assertTrue(tracker.done)

    def test_media_presentation_completes_presentation_turn(self):
        tracker = PushTurnTracker("sid-1")
        # Media presentation without text streaming finishes turn immediately
        tracker.feed({
            "event": "delta.presentation",
            "payload": {
                "session_id": "sid-1",
                "kind": "image",
                "data": {"type": "image"},
            },
        })
        self.assertTrue(tracker.done)


class TestPushSubscriptionAndMultiplexing(unittest.TestCase):
    def test_subscription_buffer_and_drain(self):
        client = mock.MagicMock()
        sub = PushSubscription(client, stream_id=42)
        self.assertEqual(sub.drain_events(), [])

        # Feed fragmented bytes
        sub.feed_bytes(b'{"event": "delta.message_start", "payload": {"session_id": "s1"}}\n')
        sub.feed_bytes(b'{"event": "delta.text_append", "payload":')
        events = sub.drain_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "delta.message_start")

        # Complete second event
        sub.feed_bytes(b' {"text": "hi"}}\n')
        events2 = sub.drain_events()
        self.assertEqual(len(events2), 1)
        self.assertEqual(events2[0]["event"], "delta.text_append")

        # Test reset
        sub.feed_bytes(b'{"event": "stale"}\n')
        sub.reset()
        self.assertEqual(sub.drain_events(), [])

    def test_client_subscription_dispatch(self):
        client = MuseClient(
            token="t", notary_token="nt", vm_id="vm", client_ip="127.0.0.1",
            reply_mode="push", direct=True,
        )
        sub1 = PushSubscription(client, stream_id=10)
        sub2 = PushSubscription(client, stream_id=20)

        client._register_subscription(sub1)
        client._register_subscription(sub2)

        # Dispatch frame for stream 10
        client._dispatch_other_stream_frame(10, "body_chunk", {"data": b'{"stream": 10}\n'})
        # Dispatch frame for stream 20
        client._dispatch_other_stream_frame(20, "body_chunk", {"data": b'{"stream": 20}\n'})

        evs1 = sub1.drain_events()
        evs2 = sub2.drain_events()
        self.assertEqual(len(evs1), 1)
        self.assertEqual(evs1[0]["stream"], 10)
        self.assertEqual(len(evs2), 1)
        self.assertEqual(evs2[0]["stream"], 20)


class TestOutboundEventDriven(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _make_pusher(self, mode="per_chat", **cfg_extra):
        cfg = QQConfig(
            app_id="1", app_secret="s",
            admin_id=["ADMIN"], whitelist={"private": ["ADMIN"]},
            session_mode=mode, session_hot_window_s=30, session_sweep_s=60,
            push=QQPushConfig(state_file=os.path.join(self._tmp.name, "push.json"), catch_up_enabled=False),
            **cfg_extra,
        )
        mapper = SessionMapper(mode=mode, global_session_id="GLOBAL",
                               state_file=os.path.join(self._tmp.name, "map.json"))
        dedup = mock.Mock()
        dedup.is_delivered.return_value = False
        dedup.is_processing.return_value = False

        muse_service = mock.Mock()
        muse_service.token_manager.has_credentials.return_value = True

        route_mgr = RouteManager(state_file=None)
        client = mock.Mock()
        client.send_message.return_value = (200, b"ok")

        pusher = QQOutboundPusher(
            config=cfg,
            client=client,
            muse_service=muse_service,
            deduplicator=dedup,
            route_manager=route_mgr,
            session_id="GLOBAL",
            session_mapper=mapper,
        )
        return pusher, mapper, muse_service, client

    def test_classify_push_event(self):
        # Sync triggers
        ev_done = {"event": "delta.message_done", "payload": {"session_id": "s1", "status": "completed"}}
        self.assertEqual(QQOutboundPusher._classify_push_event(ev_done), ("s1", "sync"))

        ev_pres = {"event": "delta.presentation", "payload": {"session_id": "s1", "kind": "image"}}
        self.assertEqual(QQOutboundPusher._classify_push_event(ev_pres), ("s1", "sync"))

        ev_task = {"event": "task.status", "payload": {"session_id": "s1", "status": "completed"}}
        self.assertEqual(QQOutboundPusher._classify_push_event(ev_task), ("s1", "sync"))

        # Stream triggers (keep hot, but don't force immediate sync)
        ev_append = {"event": "delta.text_append", "payload": {"session_id": "s1", "text": "a"}}
        self.assertEqual(QQOutboundPusher._classify_push_event(ev_append), ("s1", "stream"))

        # Ignored
        ev_interrupted = {"event": "delta.message_done", "payload": {"session_id": "s1", "status": "interrupted"}}
        self.assertEqual(QQOutboundPusher._classify_push_event(ev_interrupted), ("s1", None))

    async def test_lazy_session_404_handled_gracefully(self):
        pusher, mapper, muse_service, qq_client = self._make_pusher(mode="per_chat")
        sid, _ = mapper.resolve("group", "G1")

        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.subscribe.side_effect = SubscribeError("session not found")
        fake_client.get_history.side_effect = RuntimeError("status 404: session not found")
        muse_service.create_client.return_value = fake_client

        # Ensure baseline handles 404 gracefully
        await pusher.ensure_baseline(sid)
        self.assertIn(sid, pusher._gated)
        self.assertIn(sid, pusher._uncreated_sessions)

        # Calling track_session(sid) after a post marks it created
        pusher.track_session(sid, fresh=False)
        self.assertNotIn(sid, pusher._uncreated_sessions)
        self.assertIn(sid, pusher._pending_subscribe)

    async def test_event_driven_push_triggers_dispatch(self):
        pusher, mapper, muse_service, qq_client = self._make_pusher(mode="global")
        sid = "GLOBAL"

        mock_sub = mock.Mock()
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.subscribe.return_value = mock_sub

        # Route registered for u1 -> private user
        pusher.route_manager.register("u1", "private", "ADMIN", "msg-1")

        # History responses: initial setup has no assistant message; event-triggered fetch has a1
        hist_call_count = 0
        def fake_get_history(s, limit=25):
            nonlocal hist_call_count
            hist_call_count += 1
            if hist_call_count == 1:
                return [{"seq": 1, "role": "user", "message_id": "u1"}]
            return [
                {"seq": 1, "role": "user", "message_id": "u1"},
                {
                    "seq": 2,
                    "role": "assistant",
                    "message_id": "a1",
                    "event_name": "message.assistant",
                    "display_text": "Hello world from event stream!",
                    "status": "completed",
                },
            ]
        fake_client.get_history.side_effect = fake_get_history

        # Feed events: first iteration returns event then stops
        events_batch = [
            {"event": "delta.message_done", "payload": {"session_id": sid, "status": "completed"}},
            {"event": "task.status", "payload": {"session_id": sid, "status": "completed"}},
        ]

        read_call_count = 0
        def fake_read_events(timeout=1.0):
            nonlocal read_call_count
            read_call_count += 1
            if read_call_count == 1:
                return events_batch
            # After 1st batch, stop pusher loop
            pusher._running = False
            return []

        fake_client.read_any_subscription_events.side_effect = fake_read_events
        muse_service.create_client.return_value = fake_client

        pusher._running = True
        loop_task = asyncio.create_task(pusher._run_loop())
        await asyncio.wait_for(loop_task, timeout=5.0)

        # Verified that message was delivered to QQ client!
        qq_client.send_message.assert_called()
        self.assertIn("a1", pusher._seen_ids)
        self.assertTrue(pusher.deduplicator.mark_delivered.called)

    async def test_expiring_client_rotates_without_login_or_duplicate_history(self):
        pusher, mapper, service, qq = self._make_pusher(mode="global")
        first, second = mock.MagicMock(), mock.MagicMock()
        first.credentials_current.side_effect = [True, False]
        second.credentials_current.return_value = True
        for client in (first, second):
            client.get_history.return_value = [{"seq": 1, "role": "assistant", "message_id": "old", "display_text": "old"}]
        first.read_any_subscription_events.return_value = []
        def stop(**kwargs):
            pusher._running = False
            return []
        second.read_any_subscription_events.side_effect = stop
        service.create_client.side_effect = [first, second]
        pusher._running = True
        await asyncio.wait_for(pusher._run_loop(), 3)
        self.assertEqual(service.create_client.call_count, 2)
        first.close.assert_called_once()
        second.subscribe.assert_called_once_with("GLOBAL")
        qq.send_message.assert_not_called()
        service.token_manager.invalidate.assert_not_called()

    async def test_gateway_rejection_reconnects_without_login(self):
        from muse_bridge.muse.client.errors import RPCError
        pusher, mapper, service, qq = self._make_pusher(mode="global")
        first, second = mock.MagicMock(), mock.MagicMock()
        first.credentials_current.return_value = True
        second.credentials_current.return_value = True
        first.subscribe.side_effect = RPCError("GET", "/chat/subscribe", 401)
        second.get_history.return_value = []
        def stop(**kwargs):
            pusher._running = False
            return []
        second.read_any_subscription_events.side_effect = stop
        service.create_client.side_effect = [first, second]
        pusher._notify_login_required = mock.AsyncMock()
        pusher._running = True
        with mock.patch("muse_bridge.adapters.qq.outbound.asyncio.sleep", new=mock.AsyncMock()):
            await asyncio.wait_for(pusher._run_loop(), 3)
        service.token_manager.invalidate_mint.assert_called_once_with(first.token, first.notary_token)
        service.token_manager.invalidate.assert_not_called()
        pusher._notify_login_required.assert_not_called()
        first.close.assert_called_once()

    def test_widget_option_and_card_rendering(self):
        from muse_bridge.muse.client.render import (
            render_widget_to_text,
            render_presentation_card_to_text,
            render_assistant_event,
        )

        # 1. Option Choice widget
        widget_option = {
            "widget_id": "w1",
            "kind": "option",
            "data": {
                "options": ["帮我启动机器测通", "先别启动，保持暂停"],
            },
            "state": {"data": {"selectedText": "帮我启动机器测通"}},
        }
        res_opt = render_widget_to_text(widget_option)
        self.assertIn("【请选择】：", res_opt)
        self.assertIn("1. 帮我启动机器测通 (已选)", res_opt)
        self.assertIn("2. 先别启动，保持暂停", res_opt)

        # Embed into assistant message
        ev_msg = {
            "event_name": "message.assistant",
            "display_text": "保清，请做选择：[[hatch_widget:w1]]",
            "payload": {"widgets": [widget_option]},
        }
        rendered_text = render_assistant_event(ev_msg)
        self.assertIn("保清，请做选择：", rendered_text)
        self.assertIn("【请选择】：", rendered_text)

        # 2. Browser task presentation card
        ev_browser = {
            "event_name": "delta.presentation",
            "payload": {
                "kind": "browser_task",
                "data": {
                    "display_status": "已完成",
                    "latest_tab": {
                        "title": "设备已连接",
                        "url": "https://auth.instabox.sh/device/success",
                    },
                },
            },
        }
        res_browser = render_presentation_card_to_text(ev_browser)
        self.assertIn("【浏览器任务：已完成】", res_browser)
        self.assertIn("设备已连接", res_browser)
        self.assertIn("https://auth.instabox.sh/device/success", res_browser)

        # 3. Secret / Credentials Vault presentation card
        ev_vault = {
            "event_name": "delta.presentation",
            "payload": {
                "kind": "internal_link_action_card",
                "data": {
                    "type": "credentials_vault",
                    "title": "安全存储",
                    "subtitle": "登录 github.com",
                    "data": {
                        "username_label": "Username",
                        "password_label": "Password",
                    },
                },
            },
        }
        res_vault = render_presentation_card_to_text(ev_vault)
        self.assertIn("【安全存储提示】登录 github.com", res_vault)
        self.assertIn("Username/Password", res_vault)
        self.assertIn("请前往网页端完成安全输入", res_vault)

        # 4. Avatar share card
        ev_avatar = {
            "event_name": "delta.presentation",
            "payload": {
                "kind": "avatar_share",
                "data": {
                    "title": "我的新形象换好啦",
                    "subtitle": "你可以分享给朋友哦！",
                },
            },
        }
        res_avatar = render_presentation_card_to_text(ev_avatar)
        self.assertEqual("【卡片提示】我的新形象换好啦：你可以分享给朋友哦！", res_avatar)

    async def test_already_subscribed_session_skips_redundant_history_fetch_on_track(self):
        pusher, mapper, muse_service, qq_client = self._make_pusher(mode="global")
        sid = "GLOBAL"

        mock_sub = mock.Mock()
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.subscribe.return_value = mock_sub
        fake_client.get_history.return_value = [{"seq": 1, "role": "user", "message_id": "u1"}]

        read_calls = 0

        def fake_read_events(timeout=1.0):
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                # Simulate user sending a message after session is already subscribed & gated
                pusher.track_session(sid, fresh=False)
                return []
            pusher._running = False
            return []

        fake_client.read_any_subscription_events.side_effect = fake_read_events
        muse_service.create_client.return_value = fake_client

        pusher._running = True
        await asyncio.wait_for(pusher._run_loop(), timeout=5.0)

        # Initial setup calls subscribe + get_history once; subsequent track_session(sid)
        # on an already-subscribed & gated session must NOT trigger another blocking get_history!
        self.assertEqual(fake_client.subscribe.call_count, 1)
        self.assertEqual(fake_client.get_history.call_count, 1)


if __name__ == "__main__":
    unittest.main()
