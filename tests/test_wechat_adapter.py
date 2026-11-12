"""Offline sender, parsing and shared-gateway integration."""
import asyncio
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


def make_adapter(root, state=None):
    from muse_bridge.adapters.wechat.adapter import WeChatAdapter
    from muse_bridge.config.models import MuseConfig, WeChatConfig
    from muse_bridge.core.interactions import InteractionManager
    state = state or saved_state(root)
    muse = mock.Mock()
    muse.config = MuseConfig(session_id="")
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
                contexts.append((adapter._context("owner", message.id), adapter._context("owner")))
                return "handled"
            adapter.handle_inbound = handle
            batch = [event("A", token="TA"), event("B", token="TB")]
            await adapter.handle_batch(batch)
            self.assertEqual(contexts, [("TA", "TB"), ("TB", "TB")])
            await adapter.handle_batch(batch)
            self.assertEqual(len(contexts), 2)

    async def test_stranger_only_access_request_gets_transient_context(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            seen = []
            async def handle(message):
                seen.append((message.text, adapter._context("stranger", message.id)))
                return "handled"
            adapter.handle_inbound = handle
            await adapter.handle_batch([event("A", "stranger", "no access"),
                                        event("B", "stranger", "/申请", "temporary")])
            self.assertEqual(seen, [("/申请", "temporary")])
            self.assertIsNone(adapter.state.context("stranger"))
            self.assertFalse(adapter._transient_contexts)
            muse.post_message.assert_not_called()

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

    async def test_wildcard_whitelist_requires_explicit_open_switch(self):
        from muse_bridge.config.models import WeChatConfig
        with self.assertRaises(ValueError):
            WeChatConfig(whitelist={"private": "*"})

    async def test_local_help_and_two_turn_muse_chat(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            await adapter.handle_batch([event("help", text="/帮助")])
            self.assertTrue(client.send_message.await_count)
            self.assertEqual(client.send_message.call_args.args[0]["context_token"], "TA")
            self.assertNotIn("全员表示开放当前群", client.send_message.call_args.args[0]["item_list"][0]["text_item"]["text"])
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

    async def test_auto_approval_then_chat_in_same_poll_batch(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.config.access_requests.auto_approve = True
            from muse_bridge.adapters.wechat.gateway import WeChatGateway
            client.get_updates = mock.AsyncMock(return_value={"ret": 0, "get_updates_buf": "next",
                "msgs": [event("apply", "stranger", "/申请", "TR"),
                         event("chat", "stranger", "new chat", "TC")]})
            async def intake(messages):
                await adapter.handle_batch(messages)
                self.assertEqual(muse.post_message.call_count, 1)
                adapter.gateway._stop.set()
            adapter.gateway.on_batch = intake
            await adapter.gateway.start()
            self.assertEqual(adapter.state.cursor, "next")
            self.assertEqual(muse.post_message.call_args.args[1], "new chat")
            self.assertEqual(adapter.state.context("stranger"), "TC")

    async def test_pending_request_can_notify_after_restart_and_approval(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, muse, client = make_adapter(root)
            adapter.config.admin_id = ["owner"]
            adapter.state.remember_message("owner", "seed", "TO")
            await adapter.handle_batch([event("request", "stranger", "/申请", "TR")])
            self.assertIsNone(adapter.state.context("stranger"))
            rows = adapter.access_requests.store.pending()
            self.assertEqual(len(rows), 1)
            restarted, muse, client = make_adapter(root, WeChatState(root))
            restarted.config.admin_id = ["owner"]
            await restarted.handle_batch([event("approve", text=f"/申请审批 {rows[0]['id']} 同意", token="TO2")])
            self.assertTrue(restarted.access_requests.allows("private", "stranger", "stranger"))
            recipient_messages = [call.args[0] for call in client.send_message.await_args_list
                                  if call.args[0]["to_user_id"] == "stranger"]
            self.assertEqual(len(recipient_messages), 1)
            self.assertEqual(recipient_messages[0]["context_token"], "TR")
            self.assertEqual(restarted.state.context("stranger"), "TR")

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


if __name__ == "__main__":
    unittest.main()
