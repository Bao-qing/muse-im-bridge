"""Offline sender, parsing and shared-gateway integration."""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from muse_bridge.adapters.wechat.state import WeChatState


def saved_state(root):
    state = WeChatState(root)
    state.save_account({"bot_id": "bot@im.bot", "user_id": "owner", "bot_token": "test-token",
                        "baseurl": "https://ilinkai.weixin.qq.com"})
    return state


def config():
    return SimpleNamespace(chunk_chars=4000, max_chunks=8, max_image_bytes=1024,
                           max_file_bytes=1024, max_video_upload_bytes=1024)


def event(mid="1", user="owner", text="hello", token="TA", **fields):
    return {"message_type": 1, "message_id": mid, "from_user_id": user,
            "to_user_id": "bot@im.bot", "context_token": token,
            "item_list": [{"type": 1, "text_item": {"text": text}}], **fields}


class TestWeChatSender(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_vs_latest(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            state.remember_message("owner", "B", "TB")
            client = mock.Mock(send_message=mock.AsyncMock(return_value={"ret": 0}))
            sender = WeChatSender(client, state, config(), mock.Mock())
            self.assertTrue(await sender.send("owner", "reply", purpose="reply", msg_id="A", logical_id="A"))
            self.assertTrue(await sender.send("owner", "answer", logical_id="answer"))
            self.assertTrue(await sender.send("owner", "notice", purpose="notification", logical_id="notice"))
            messages = [call.args[0] for call in client.send_message.await_args_list]
            self.assertEqual([m["context_token"] for m in messages], ["TA", "TB", "TB"])
            self.assertTrue(all(len(m["item_list"]) == 1 for m in messages))
            self.assertFalse(await sender.send("stranger", "no context"))
            self.assertEqual(client.send_message.await_count, 3)

    async def test_immediate_reply_never_falls_back_to_latest_context(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            state.remember_message("owner", "B", "")
            client = mock.Mock(send_message=mock.AsyncMock(return_value={"ret": 0}))
            sender = WeChatSender(client, state, config(), mock.Mock())
            self.assertFalse(await sender.send("owner", "reply", purpose="reply", msg_id="B"))
            client.send_message.assert_not_awaited()
            self.assertEqual(state.context("owner"), "TA")

    async def test_chunks_success_and_repeat_suppression(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            client = mock.Mock(send_message=mock.AsyncMock(return_value={"ret": 0}))
            sender = WeChatSender(client, state, config(), mock.Mock())
            text = "```python\n" + "中😀" * 4100 + "\n```"
            self.assertTrue(await sender.send("owner", text, logical_id="long"))
            count = client.send_message.await_count
            self.assertGreater(count, 2)
            for call in client.send_message.await_args_list:
                self.assertLessEqual(len(call.args[0]["item_list"][0]["text_item"]["text"]), 4000)
            self.assertTrue(await sender.send("owner", text, logical_id="long"))
            self.assertEqual(client.send_message.await_count, count)

    async def test_uncertain_send_not_replayed_even_after_restart(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            client = mock.Mock(send_message=mock.AsyncMock(side_effect=TimeoutError("unknown")))
            sender = WeChatSender(client, state, config(), mock.Mock())
            self.assertFalse(await sender.send("owner", "answer", logical_id="same"))
            sender = WeChatSender(client, WeChatState(root), config(), mock.Mock())
            self.assertFalse(await sender.send("owner", "answer", logical_id="same"))
            self.assertEqual(client.send_message.await_count, 1)
            self.assertEqual(state.status()["uncertain_sends"], 1)

    async def test_partial_media_failure_is_not_complete(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            client = mock.Mock(send_message=mock.AsyncMock(return_value={"ret": 0}))
            media = mock.Mock(upload=mock.AsyncMock(side_effect=ValueError("upload failed")))
            sender = WeChatSender(client, state, config(), media)
            self.assertFalse(await sender.send("owner", "caption", images=[{"data": b"image"}], logical_id="pic"))
            self.assertEqual(client.send_message.await_count, 1)
            media.upload.side_effect = None
            media.upload.return_value = {"type": 2, "image_item": {}}
            self.assertTrue(await sender.send("owner", "caption", images=[{"data": b"image"}], logical_id="pic"))
            self.assertEqual(client.send_message.await_count, 2)
            self.assertEqual(client.send_message.call_args.args[0]["item_list"][0]["type"], 2)

    async def test_send_messages_collects_server_message_ids(self):
        from muse_bridge.adapters.wechat.sender import WeChatSender
        with tempfile.TemporaryDirectory() as root:
            state = saved_state(root)
            state.remember_message("owner", "A", "TA")
            client = mock.Mock(send_message=mock.AsyncMock(
                side_effect=[{"message_id": 101}, {"message_id": "102"}]))
            sender = WeChatSender(client, state, config(), mock.Mock())
            ok, ids = await sender.send_messages("owner", "a" * 4001, logical_id="multi")
            self.assertTrue(ok)
            self.assertEqual(ids, ["101", "102"])

class TestWeChatParsing(unittest.TestCase):
    def test_no_secret_raw_event_or_media_filename_path(self):
        from muse_bridge.adapters.wechat.adapter import parse_event
        raw = event()
        raw["item_list"].append({"type": 4, "file_item": {
            "file_name": "../../secret.txt", "media": {"aes_key": "sensitive"}}})
        message, private_media, token = parse_event(raw, "bot@im.bot")
        self.assertEqual(token, "TA")
        self.assertEqual(message.source.raw_event, {})
        self.assertEqual(message.attachments[0].filename, "secret.txt")
        self.assertNotIn("sensitive", repr(message))
        self.assertEqual(len(private_media), 1)
        self.assertIsNone(parse_event(event(group_id="group"), "bot@im.bot"))
        self.assertIsNone(parse_event(event(message_type=2), "bot@im.bot"))
        self.assertIsNone(parse_event(event(to_user_id="foreign"), "bot@im.bot"))

    def test_malformed_items_and_extreme_timestamp_do_not_poison_batch(self):
        from muse_bridge.adapters.wechat.adapter import parse_event
        raw = event(create_time_ms=10**400)
        raw["item_list"].extend([{"type": []}, {"type": {}}, {"type": True}])
        message, _, _ = parse_event(raw, "bot@im.bot")
        self.assertEqual(message.timestamp, 0)
        self.assertEqual(message.text, "hello")
        self.assertIsNone(parse_event(event(message_type=True), "bot@im.bot"))

    def test_voice_transcription_and_multiline_text(self):
        from muse_bridge.adapters.wechat.adapter import parse_event
        raw = event(text="line one\nline two")
        raw["item_list"].append({"type": 3, "voice_item": {"text": "transcribed"}})
        message, media, _ = parse_event(raw, "bot@im.bot")
        self.assertEqual(message.text, "line one\nline two\ntranscribed")
        self.assertFalse(message.attachments)

    def test_ref_msg_id_parsed_without_secrets(self):
        from muse_bridge.adapters.wechat.adapter import parse_event
        raw = event(mid="20", text="引用上一条消息")
        raw["item_list"][0]["ref_msg"] = {
            "message_item": {
                "type": 0,
                "create_time_ms": 1794623884000,
                "msg_id": "7527198144967907592",
            }
        }
        message, _, _ = parse_event(raw, "bot@im.bot")
        self.assertEqual(message.text, "引用上一条消息")
        self.assertEqual(message.references, [("id", "7527198144967907592")])
        self.assertEqual(message.source.raw_event, {"references": [("id", "7527198144967907592")]})

        raw_int = event(mid="21", text="int ref")
        raw_int["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": 123456}}
        message_int, _, _ = parse_event(raw_int, "bot@im.bot")
        self.assertEqual(message_int.references, [("id", "123456")])

    def test_ref_msg_fallback_text_when_no_msg_id(self):
        from muse_bridge.adapters.wechat.adapter import parse_event
        raw = event(mid="22", text="回复")
        raw["item_list"][0]["ref_msg"] = {"title": "摘要内容"}
        message, _, _ = parse_event(raw, "bot@im.bot")
        self.assertEqual(message.references, [])
        self.assertEqual(message.source.raw_event, {})
        self.assertEqual(message.text, "[引用: 摘要内容]\n回复")


def make_adapter(root, state=None, *, muse=None):
    from muse_bridge.adapters.wechat.adapter import WeChatAdapter
    from muse_bridge.config.models import MuseConfig, WeChatConfig
    from muse_bridge.core.interactions import InteractionManager
    state = state or saved_state(root)
    if muse is None:
        muse = mock.Mock()
        muse.config = MuseConfig(session_id="", cookies_file=str(Path(root) / "cookies.json"))
        muse.interactions = InteractionManager()
        muse.interaction_protocol.approval_queue.return_value = []
        muse.interaction_protocol.queue_heads.return_value = []
        muse.token_manager.has_credentials.return_value = True
        muse.post_message.side_effect = lambda sid, text, *a, **kw: "mid-" + text
    client = mock.Mock(send_message=mock.AsyncMock(return_value={"ret": 0}), aclose=mock.AsyncMock())
    adapter = WeChatAdapter(WeChatConfig(enabled=True, state_dir=root), muse, state=state, client=client)
    adapter.outbound_pusher.ensure_baseline = mock.AsyncMock()
    return adapter, muse, client


class TestWeChatIntake(unittest.IsolatedAsyncioTestCase):
    async def test_batch_latest_before_business_and_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            contexts = []
            async def handle(message):
                contexts.append((adapter.state.context("owner", message.id), adapter.state.context("owner")))
                return "handled"
            adapter.handle_inbound = handle
            batch = [event("A", token="TA"), event("B", token="TB")]
            await adapter.handle_batch(batch)
            self.assertEqual(contexts, [("TA", "TB"), ("TB", "TB")])
            await adapter.handle_batch(batch)
            self.assertEqual(len(contexts), 2)

    async def test_any_personal_sender_can_chat_without_access_state(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch("muse_bridge.gateway.chat.ChatAccessRequests", side_effect=AssertionError("No access policy")):
                adapter, muse, client = make_adapter(root)
            self.assertIsNone(adapter.access_requests)
            await adapter.handle_batch([event("A", "stranger", "first", "TA"),
                                        event("B", "other", "second", "TB")])
            self.assertEqual([c.args[1] for c in muse.post_message.call_args_list], ["first", "second"])
            self.assertNotEqual(muse.post_message.call_args_list[0].args[0], muse.post_message.call_args_list[1].args[0])
            self.assertEqual(adapter.state.context("stranger"), "TA")
            self.assertEqual(adapter.state.context("other"), "TB")
            self.assertFalse(list(Path(root).rglob("*access*")))
            self.assertFalse(hasattr(adapter, "_transient_contexts"))

    async def test_typing_ticket_cache_and_failure_backoff(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            client.get_config = mock.AsyncMock(return_value={"typing_ticket": "ticket"})
            first = await adapter._typing_ticket("owner", "A")
            second = await adapter._typing_ticket("owner", "B")
            self.assertEqual((first, second), ("ticket", "ticket"))
            self.assertEqual(client.get_config.await_count, 1)
            client.get_config.side_effect = TimeoutError("typing unavailable")
            self.assertIsNone(await adapter._typing_ticket("other", "C"))
            self.assertIsNone(await adapter._typing_ticket("other", "D"))
            self.assertEqual(client.get_config.await_count, 2)

    async def test_typing_cancellation_stops_keepalive(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.config.typing_enabled = True
            client.get_config = mock.AsyncMock(return_value={"typing_ticket": "ticket"})
            started = asyncio.Event()
            async def send_typing(user, ticket, status):
                if status == 1:
                    started.set()
                return {"ret": 0}
            client.send_typing = mock.AsyncMock(side_effect=send_typing)
            task = asyncio.create_task(adapter._typing("owner", "A"))
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual([call.args[2] for call in client.send_typing.await_args_list], [1, 2])

    async def test_group_events_never_reach_shared_gateway(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            await adapter.handle_batch([event("A", group_id="unexpected-group")])
            muse.post_message.assert_not_called()
            client.send_message.assert_not_awaited()
            self.assertIsNone(adapter.state.context("owner"))

    async def test_local_help_and_two_turn_muse_chat(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            await adapter.handle_batch([event("help", text="/帮助")])
            self.assertTrue(client.send_message.await_count)
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TA")
            help_text = client.send_message.call_args.args[0]["item_list"][0]["text_item"]["text"]
            for obsolete in ("管理员", "申请", "白名单", "鉴权", "群"):
                self.assertNotIn(obsolete, help_text)
            self.assertIn("引用凭证卡", help_text)
            for retained in ("/登录", "/凭证", "/审批", "/新建"):
                self.assertIn(retained, help_text)
            arbitrary = "用户原文：管理员申请群聊；如需在群聊中使用"
            self.assertEqual(adapter.render_command_output(arbitrary), arbitrary)
            await adapter.handle_batch([event("A", text="first", token="TA")])
            await adapter.handle_batch([event("B", text="second", token="TB")])
            self.assertEqual(muse.post_message.call_count, 2)
            self.assertEqual(muse.post_message.call_args_list[0].args[0], muse.post_message.call_args_list[1].args[0])
            sid = muse.post_message.call_args.args[0]
            with mock.patch.object(adapter.route_manager, "match_route") as route:
                target = adapter.outbound_pusher.resolve_delivery_target(sid, "wrong-mid", "unknown-execution")
            self.assertEqual(target, ("private", "owner", None, "proactive"))
            route.assert_not_called()
            self.assertEqual(adapter.state.message_status("owner", "A"), "handled")

    async def test_poll_batch_personal_messages_keep_order_and_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            client.get_updates = mock.AsyncMock(return_value={"ret": 0, "get_updates_buf": "next",
                "msgs": [event("first", "stranger", "first chat", "TA"),
                         event("second", "stranger", "second chat", "TB")]})
            async def intake(messages):
                await adapter.handle_batch(messages)
                self.assertEqual(muse.post_message.call_count, 2)
                adapter.gateway._stop.set()
            adapter.gateway.on_batch = intake
            await adapter.gateway.start()
            self.assertEqual(adapter.state.cursor, "next")
            self.assertEqual([c.args[1] for c in muse.post_message.call_args_list], ["first chat", "second chat"])
            self.assertEqual(adapter.state.context("stranger"), "TB")

    async def test_notifications_only_bound_chat_without_scanner_copy(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            await adapter.handle_batch([event("A", "stranger", "chat", "TA")])
            adapter.state.remember_message("owner", "B", "TB")
            sid = adapter.session_mapper.current("private", "stranger")
            for kind in ("credential", "approval"):
                item = muse.interactions.add(kind, session_id=sid, context={"fingerprint": "current"})
                self.assertTrue(await adapter.notify_interaction(item, "card-" + kind))
            self.assertEqual([c.args[0]["to_user_id"] for c in client.send_message.await_args_list],
                             ["stranger", "stranger"])
            self.assertFalse(await adapter.notify_interaction(
                muse.interactions.add("credential", session_id="unbound"), "unbound"))
            self.assertEqual(client.send_message.await_count, 2)
            self.assertEqual(adapter.notification_target(), "owner")
            self.assertEqual(adapter.outbound_pusher.notification_target(), "owner")

    async def test_complete_dispatcher_to_sender_and_reaction_revisions(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            await adapter.handle_batch([event("A", text="first", token="TA"),
                                        event("B", text="second", token="TB")])
            sid = muse.post_message.call_args.args[0]
            pusher = adapter.outbound_pusher
            history = [{"role": "user", "message_id": "mid-first", "seq": 1},
                       {"role": "user", "message_id": "mid-second", "seq": 2},
                       {"role": "assistant", "message_id": "answer", "seq": 3,
                        "event_name": "message.assistant", "display_text": "async answer",
                        "payload": {"status": "completed"}}]
            await pusher._scan_and_dispatch(history, sid)
            self.assertEqual(client.send_message.await_count, 1)
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TB")
            self.assertEqual(client.send_message.call_args.args[0]["item_list"][0]["text_item"]["text"], "async answer")
            await pusher._scan_and_dispatch(history, sid)
            self.assertEqual(client.send_message.await_count, 1)
            for seq, emojis in ((10, ["like"]), (11, ["heart"]), (12, []), (13, ["like"])):
                reaction = {"event_name": "reactions.updated", "seq": seq,
                            "payload": {"message_id": "answer", "reactions": emojis}}
                await pusher._dispatch_reaction(reaction, sid)
                await pusher._dispatch_reaction(reaction, sid)
            self.assertEqual(client.send_message.await_count, 5)
            self.assertEqual(pusher.reference_store.reaction_state(sid, "answer"), (13, ["like"]))

    async def test_unknown_intake_and_restart_do_not_repeat_business(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.handle_inbound = mock.AsyncMock(side_effect=TimeoutError("business outcome unknown"))
            with self.assertRaises(TimeoutError):
                await adapter.handle_batch([event("A")])
            self.assertEqual(adapter.state.message_status("owner", "A"), "unknown")
            await adapter.handle_batch([event("A")])
            self.assertEqual(adapter.handle_inbound.await_count, 1)
            state = adapter.state
            state.remember_message("owner", "B", "TB")
            restarted, muse, client = make_adapter(root, WeChatState(root))
            restarted.handle_inbound = mock.AsyncMock()
            await restarted.handle_batch([event("B")])
            restarted.handle_inbound.assert_not_called()

    async def test_retryable_intake_keeps_cursor_and_unknown_post_never_replays(self):
        from muse_bridge.adapters.wechat.adapter import IntakeRetryable
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.handle_inbound = mock.AsyncMock(side_effect=["retryable", "handled"])
            with self.assertRaises(IntakeRetryable):
                await adapter.handle_batch([event("A")])
            self.assertEqual(adapter.state.message_status("owner", "A"), "accepted")
            await adapter.handle_batch([event("A")])
            self.assertEqual(adapter.state.message_status("owner", "A"), "handled")
        for error in (TimeoutError("unconfirmed"), RuntimeError("status 404: session not found")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as root:
                adapter, muse, client = make_adapter(root)
                muse.post_message.side_effect = error
                await adapter.handle_batch([event("B")])
                self.assertEqual(adapter.state.message_status("owner", "B"), "unknown")
                await adapter.handle_batch([event("B")])
                muse.post_message.assert_called_once()

    async def test_notification_ignores_old_route_and_receipt_has_no_id(self):
        from muse_bridge.core.message import DeliveryReference
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.state.remember_message("owner", "A", "TA")
            adapter.state.remember_message("owner", "B", "TB")
            delivery = DeliveryReference("session", "notice", purpose="notification")
            self.assertTrue(await adapter.outbound_pusher._deliver("private", "owner", "notice", [], [], [],
                                                                  route=SimpleNamespace(msg_id="A"), delivery=delivery))
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TB")
            body = await adapter.send_reply("private", "owner", "immediate", "A")
            self.assertEqual(body, {})
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TA")
            client.send_message.return_value = {"message_id": 7527198718193190536}
            body_with_id = await adapter.send_reply("private", "owner", "with-id", "A")
            self.assertEqual(body_with_id, {"id": "7527198718193190536"})

    async def test_inbound_quote_resolves_to_muse_in_reply_to_and_outbound_drops_quote(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            client.send_message.side_effect = [{"message_id": 2001}]
            await adapter.handle_batch([event("1001", text="first", token="TA")])
            sid = muse.post_message.call_args.args[0]
            history = [
                {"role": "user", "message_id": "mid-first", "seq": 1},
                {"role": "assistant", "message_id": "muse-2001", "seq": 2,
                 "reply_to_id": "mid-first", "event_name": "message.assistant",
                 "display_text": "bot reply", "payload": {"status": "completed"}},
            ]
            await adapter.outbound_pusher._scan_and_dispatch(history, sid)
            sent_msg = client.send_message.call_args.args[0]
            self.assertEqual(sent_msg["item_list"][0]["text_item"]["text"], "bot reply")
            self.assertNotIn("ref_msg", sent_msg["item_list"][0])

            quote_ev = event("1002", text="follow up", token="TB")
            quote_ev["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": "2001"}}
            await adapter.handle_batch([quote_ev])
            self.assertEqual(muse.post_message.call_args.args[1], "follow up")
            self.assertEqual(muse.post_message.call_args.kwargs.get("in_reply_to"), "muse-2001")


class TestWeChatMuseInteractions(unittest.IsolatedAsyncioTestCase):
    def make(self, root, main_session=""):
        from muse_bridge.config.models import MuseConfig
        from muse_bridge.muse.service import MuseService
        token = mock.MagicMock()
        token.has_credentials.return_value = True
        service = MuseService(MuseConfig(session_id=main_session,
            cookies_file=str(Path(root) / "cookies.json"), mint_file=str(Path(root) / "mint.json")), token)
        self.addCleanup(service.interactions.close)
        service.post_message = mock.Mock(return_value="posted")
        return make_adapter(root, muse=service)

    def texts(self, client):
        return [c.args[0]["item_list"][0]["text_item"]["text"] for c in client.send_message.await_args_list]

    def assert_no_access_copy(self, client):
        for text in self.texts(client):
            for obsolete in ("管理员", "白名单", "申请", "鉴权", "群聊"):
                self.assertNotIn(obsolete, text)

    async def test_personal_commands_and_main_session_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root, "main-session")
            self.assertEqual(adapter.session_mapper.current("private", "owner"), "main-session")
            await adapter.handle_batch([event("chat", "stranger", "hello")])
            self.assertNotEqual(service.post_message.call_args.args[0], "main-session")
            await adapter.handle_batch([event("new", "stranger", "/新建")])
            sid = adapter.session_mapper.current("private", "stranger")
            self.assertNotEqual(sid, "main-session")
            remote = mock.Mock()
            remote.list_sessions.return_value = [{"session_id": "chosen-session"}]
            with mock.patch.object(service, "create_client", return_value=remote):
                await adapter.handle_batch([event("switch", "stranger", "/切换 chosen-session"),
                                            event("delete", "stranger", "/删除 chosen-session")])
            self.assertEqual(adapter.session_mapper.current("private", "stranger"), "chosen-session")
            remote.delete_session.assert_called_once_with("chosen-session")
            self.assert_no_access_copy(client)

    async def test_muse_login_email_and_otp_need_no_platform_role(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            service.token_manager.has_credentials.return_value = False
            await adapter.handle_batch([event("no-login", "stranger", "hello")])
            self.assertIn("请发送 /登录", self.texts(client)[-1])
            with mock.patch("muse_bridge.gateway.interactions.MuseProtocolLogin") as login:
                auth = login.return_value
                auth.extract_cookies.return_value = {"cookie": "hatch_sess=synthetic-session"}
                await adapter.handle_batch([event("login", "stranger", "/登录 person@example.invalid")])
                owner = adapter.interaction_owner("private", "stranger", "stranger")
                self.assertEqual(service.interactions.current(owner).state, "wait_otp")
                await adapter.handle_batch([event("otp", "stranger", "123456")])
                auth.send_otp.assert_called_once_with("person@example.invalid")
                auth.confirm_otp.assert_called_once_with("123456")
            service.token_manager.refresh_from_cookies.assert_called_once_with(False)
            self.assertIsNone(service.interactions.current(owner))
            self.assertTrue(Path(service.config.cookies_file).is_file())
            service.post_message.assert_not_called()
            self.assert_no_access_copy(client)

    async def test_credential_selection_and_multiline_input_without_admin(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            adapter.session_mapper.bind("private", "stranger", "s1")
            service.interactions.managed_sessions.add("s1")
            card = service.interactions.add("credential", session_id="s1",
                context={"page_url": "https://example.invalid/login"})
            adapter.config.credentials_in_place = False  # Group-only restriction is irrelevant here.
            hint = adapter.outbound_pusher.render_credential_hint(card)
            self.assertIn("/凭证 " + card.id, hint)
            self.assertNotIn("管理员", hint)
            usernames = []
            with mock.patch.object(service, "capture_credential",
                                   side_effect=lambda item, secret: usernames.append(item.context["username"])) as capture:
                await adapter.handle_batch([event("select", "stranger", f"/凭证 {card.id} 一次"),
                                            event("secret", "stranger", "synthetic-user\nsynthetic-secret")])
            capture.assert_called_once_with(card, "synthetic-secret")
            self.assertEqual(usernames, ["synthetic-user"])
            self.assertTrue(card.terminal)
            service.post_message.assert_not_called()
            self.assert_no_access_copy(client)

    async def test_muse_approval_card_gate_and_numeric_decision_without_admin(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            adapter.session_mapper.bind("private", "stranger", "s1")
            service.interactions.managed_sessions.add("s1")
            row = {"approval_id": "remote-approval", "status": "pending", "scope": {"thread_id": "s1"},
                   "requested_at_ms": 1, "display": {"summary_title": "Continue operation"},
                   "decision_options": [{"kind": "allow_once", "label": {"kind": "allow_once"},
                                         "label_text": "允许一次"}]}
            decisions = []
            def request(method, path, body=None, **kwargs):
                if path == "/approvals":
                    return {"pending": [copy.deepcopy(row)] if row["status"] == "pending" else [], "recent": []}
                if method == "POST":
                    decisions.append(body)
                    row["status"] = "approved"
                return copy.deepcopy(row)
            service.interaction_protocol.request = mock.Mock(side_effect=request)
            service.interaction_protocol.fetch_approvals()
            await adapter.handle_batch([event("gate", "stranger", "please continue")])
            service.post_message.assert_not_called()
            self.assertIn("【审批", self.texts(client)[-1])
            await adapter.handle_batch([event("decision", "stranger", "1")])
            self.assertEqual(decisions, [{"decision": "allow_once", "notification_channel": "ui"}])
            self.assertIn("审批已全部完成", self.texts(client)[-1])
            await adapter.handle_batch([event("chat", "stranger", "continue")])
            service.post_message.assert_called_once()
            self.assert_no_access_copy(client)

    async def test_quote_credential_card_captures_input_without_posting_to_chat(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            client.send_message.return_value = {"message_id": 888001}
            await adapter.handle_batch([event("seed", "stranger", "hi", "TA")])
            sid = adapter.session_mapper.current("private", "stranger")
            service.post_message.reset_mock()

            history = [{
                "role": "assistant",
                "message_id": "pres-cred-1",
                "seq": 1,
                "event_name": "delta.presentation",
                "payload": {
                    "presentation_id": "pres-cred-1",
                    "data": {
                        "action": {
                            "type": "credentials_vault",
                            "data": {
                                "url": "https://muse.ai/connect/browser-credentials"
                                       "#page_url=https%3A%2F%2Fexample.invalid%2Flogin"
                                       "&capture_authorization=cap-auth-1",
                            },
                        }
                    },
                },
            }]
            await adapter.outbound_pusher._scan_and_dispatch(history, sid)
            sent_text = client.send_message.call_args.args[0]["item_list"][0]["text_item"]["text"]
            self.assertIn("可引用此消息", sent_text)
            card = service.interactions.by_remote("credential", "pres-cred-1")
            self.assertIsNotNone(card)

            quote_ev = event("cred-reply", "stranger", "alice\nsecret-pw", "TB")
            quote_ev["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": "888001"}}
            usernames = []
            with mock.patch.object(service, "capture_credential",
                                   side_effect=lambda item, secret: usernames.append(item.context["username"])) as capture:
                await adapter.handle_batch([quote_ev])
            capture.assert_called_once_with(card, "secret-pw")
            self.assertEqual(usernames, ["alice"])
            self.assertTrue(card.terminal)
            service.post_message.assert_not_called()
            self.assert_no_access_copy(client)

    async def test_quote_approval_card_submits_decision(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            client.send_message.return_value = {"message_id": 999001}
            await adapter.handle_batch([event("seed", "stranger", "hi", "TA")])
            sid = adapter.session_mapper.current("private", "stranger")
            service.interactions.managed_sessions.add(sid)
            row = {"approval_id": "remote-approval", "status": "pending", "scope": {"thread_id": sid},
                   "requested_at_ms": 1, "display": {"summary_title": "Continue operation"},
                   "decision_options": [{"kind": "allow_once", "label": {"kind": "allow_once"},
                                         "label_text": "允许一次"}]}
            decisions = []
            def request(method, path, body=None, **kwargs):
                if path == "/approvals":
                    return {"pending": [copy.deepcopy(row)] if row["status"] == "pending" else [], "recent": []}
                if method == "POST":
                    decisions.append(body)
                    row["status"] = "approved"
                return copy.deepcopy(row)
            service.interaction_protocol.request = mock.Mock(side_effect=request)
            service.interaction_protocol.fetch_approvals()
            item = service.interactions.by_remote("approval", "remote-approval")
            await service.interaction_protocol.announce(
                item, lambda it, text: adapter.outbound_pusher.notify_interaction(it, text))

            quote_ev = event("approve-quote", "stranger", "1", "TB")
            quote_ev["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": "999001"}}
            await adapter.handle_batch([quote_ev])
            self.assertEqual(decisions, [{"decision": "allow_once", "notification_channel": "ui"}])
            self.assert_no_access_copy(client)

    async def test_login_required_runtime_hints_and_notice_target(self):
        from muse_bridge.muse.client.errors import LoginRequired
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            service.post_message.side_effect = LoginRequired("synthetic-expiry")
            await adapter.handle_batch([event("chat", "stranger", "hello")])
            self.assertIn("请发送 /登录", self.texts(client)[-1])
            self.assertTrue(adapter._in_login_fallback)
            remote = mock.Mock()
            remote.connect.side_effect = LoginRequired("synthetic-expiry")
            with mock.patch.object(service, "create_client", return_value=remote):
                await adapter.handle_batch([event("command", "stranger", "/会话")])
            self.assertIn("请发送 /登录", self.texts(client)[-1])
            adapter.state.remember_message("owner", "seed", "TO")
            await adapter.outbound_pusher._notify_login_required()
            self.assertEqual(client.send_message.call_args.args[0]["to_user_id"], "owner")
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TO")
            self.assert_no_access_copy(client)

    async def test_engine_start_uses_notification_target_without_admin_config(self):
        from muse_bridge.engine import BridgeEngine
        with tempfile.TemporaryDirectory() as root:
            adapter, service, client = self.make(root)
            adapter.start = mock.AsyncMock()
            service.verify_connection = mock.Mock(return_value=False)
            service.interaction_protocol.run = mock.AsyncMock()
            engine = object.__new__(BridgeEngine)
            engine.config = SimpleNamespace(gateway=SimpleNamespace(login_through_adapter="wechat"))
            engine.adapters = [adapter]
            engine.muse_service = service
            with mock.patch.object(engine, "_notify_admin_login", new_callable=mock.AsyncMock) as notice:
                await engine.start()
                await asyncio.sleep(0)
                notice.assert_awaited_once_with(adapter, "owner")


if __name__ == "__main__":
    unittest.main()
