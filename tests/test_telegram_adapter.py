"""Offline Bot API tests. Every request is served by MockTransport."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from muse_bridge.adapters.telegram import TelegramAdapter
from muse_bridge.adapters.telegram.client import TelegramAPIError
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, MuseConfig, TelegramConfig
from muse_bridge.core.delivery import DeliveryEnvelope, DeliveryStatus, NativeQuote
from muse_bridge.core.message import Attachment
from muse_bridge.gateway.runtime import UnifiedGateway
from muse_bridge.muse.service import MuseService


BOT_ID = 321
USER_ID = 123
GROUP_ID = -100100
TOKEN = f"{BOT_ID}:offline-fixture"


def ok(value):
    return httpx.Response(200, json={"ok": True, "result": value})


def reject(status, **extras):
    return httpx.Response(status, json={"ok": False, "error_code": status, **extras})


def update(uid, mid, text=None, *, chat_id=USER_ID, sender=USER_ID, **fields):
    message = {
        "message_id": mid, "date": 1234,
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
        "from": {"id": sender, "is_bot": False, "first_name": "Test"},
        **fields,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": uid, "message": message}


class TelegramConfigTests(unittest.TestCase):
    def test_loader_reads_env_and_uses_closed_defaults(self):
        raw = {"adapters": {"telegram": {"enabled": True, "admin_id": [USER_ID],
                                        "whitelist": {"private": [USER_ID]}}}}
        with patch.object(ConfigLoader, "_read_raw", return_value=raw), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}):
            cfg = ConfigLoader.load()
        self.assertEqual(cfg.telegram.bot_token, TOKEN)
        self.assertNotIn(TOKEN, repr(cfg))
        self.assertEqual(cfg.telegram.admin_id, [str(USER_ID)])
        self.assertEqual(cfg.telegram.whitelist["private"], [str(USER_ID)])
        self.assertFalse(cfg.telegram.allow_all_private)
        self.assertFalse(cfg.telegram.allow_all_group)
        self.assertTrue(cfg.telegram.credentials_in_place)
        self.assertFalse(cfg.telegram.seed_main_session)
        self.assertTrue(cfg.qq.seed_main_session)
        self.assertTrue(cfg.onebot.seed_main_session)

    def test_credentials_in_place_can_be_disabled_explicitly(self):
        raw = {"adapters": {"telegram": {"credentials_in_place": False}}}
        with patch.object(ConfigLoader, "_read_raw", return_value=raw):
            cfg = ConfigLoader.load()
        self.assertFalse(cfg.telegram.credentials_in_place)

    def test_enabled_adapter_requires_token_without_echoing_bad_input(self):
        with self.assertRaisesRegex(ValueError, "valid bot_token") as cm:
            TelegramConfig(enabled=True, bot_token="not-a-token-secret")
        self.assertNotIn("not-a-token-secret", str(cm.exception))


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.requests = []
        self.updates = []
        self.responses = {}
        self.next_message = 900
        self.transport = httpx.MockTransport(self.handle_request)

    async def handle_request(self, request):
        self.assertEqual(request.url.host, "api.telegram.org")
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if "application/json" in request.headers.get("content-type", "") else request.content
        self.requests.append((method, payload))
        if method in self.responses and self.responses[method]:
            override = self.responses[method].pop(0)
            if isinstance(override, BaseException):
                raise override
            return override
        if method == "getMe":
            return ok({"id": BOT_ID, "is_bot": True, "username": "MuseTestBot"})
        if method == "getUpdates":
            return ok([row for row in self.updates if row["update_id"] >= payload.get("offset", 0)])
        if method == "getFile":
            return ok({"file_path": "photos/file_1.jpg", "file_size": 4})
        if method == "file_1.jpg":
            return httpx.Response(200, content=b"data")
        if method == "sendMessage":
            self.next_message += 1
            return ok({"message_id": self.next_message, "chat": {"id": payload["chat_id"]}})
        self.fail(f"Unexpected Bot API method: {method}")

    def adapter(self, **overrides):
        fields = {key: str(self.root / key) for key in
                  ("routes_file", "delivered_file", "session_map_file", "push_state_file")}
        fields.update(enabled=True, bot_token=TOKEN, admin_id=[str(USER_ID)],
                      whitelist={"private": [str(USER_ID)], str(GROUP_ID): "*"},
                      access_requests=AccessRequestsConfig(state_file=str(self.root / "access.json")))
        fields.update(overrides)
        adapter = TelegramAdapter(TelegramConfig(**fields), transport=self.transport)
        self.addAsyncCleanup(adapter.stop)
        return adapter

    async def mounted(self, **overrides):
        token = MagicMock()
        token.has_credentials.return_value = True
        svc = MuseService(MuseConfig(session_id="production-main", cookies_file=str(self.root / "cookies")), token)
        svc.interactions.approvals_ready = True
        svc.post_message = MagicMock(side_effect=lambda *args, **kw: f"m{svc.post_message.call_count}")
        gateway = UnifiedGateway(svc)
        self.addAsyncCleanup(gateway.stop)
        adapter = gateway.mount(self.adapter(**overrides))
        await adapter.prepare()
        ctx = gateway.contexts[adapter]
        ctx.outbound_pusher.ensure_baseline = AsyncMock()
        return adapter, ctx, svc

    async def test_long_poll_through_gateway_with_plain_command_reply_and_restart(self):
        adapter, ctx, svc = await self.mounted()
        self.assertEqual(adapter.account_identity(), str(BOT_ID))
        self.assertEqual(ctx.session_mapper.current("private", str(USER_ID)), "")
        self.updates = [update(22, 6, "Can you see this?"), update(21, 5, "/help"),
                        update(23, 1, "blocked", chat_id=999, sender=999)]
        await adapter._poller.poll_once()
        self.assertEqual(svc.post_message.call_count, 1)
        sid = ctx.session_mapper.current("private", str(USER_ID))
        self.assertNotEqual(sid, "production-main")
        self.assertEqual(svc.post_message.call_args.args[:2], (sid, "Can you see this?"))
        _, data = next(req for req in self.requests if req[0] == "sendMessage")
        self.assertNotIn("reply_parameters", data)
        self.assertEqual(data["chat_id"], USER_ID)
        restarted = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        restarted.mount(host)
        await restarted.prepare()
        await restarted._poller.poll_once()
        host.accept.assert_not_awaited()
        self.assertEqual(self.requests[-1], ("getUpdates", {"timeout": 30, "allowed_updates": ["message"], "offset": 24}))

    async def test_group_mentions_foreign_commands_and_topic_events(self):
        adapter, ctx, svc = await self.mounted(group_sender_info=False)
        at = {"type": "mention", "offset": 3, "length": len("@MuseTestBot")}
        command = {"type": "bot_command", "offset": 0, "length": len("/help@MuseTestBot")}
        foreign = {**command, "length": len("/help@OtherBot")}
        self.updates = [update(1, 4, "plain", chat_id=GROUP_ID),
                        update(2, 5, "🧪 @MuseTestBot hello", chat_id=GROUP_ID, entities=[at]),
                        update(3, 6, "/help@OtherBot", chat_id=GROUP_ID, entities=[foreign]),
                        update(4, 7, "/help@MuseTestBot", chat_id=GROUP_ID, entities=[command]),
                        update(5, 8, "/help", chat_id=GROUP_ID, is_topic_message=True)]
        await adapter._poller.poll_once()
        self.assertEqual(svc.post_message.call_count, 1)
        self.assertEqual(svc.post_message.call_args.args[1], "🧪  hello")
        sent = [body for method, body in self.requests if method == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], GROUP_ID)
        self.assertNotIn("reply_parameters", sent[0])

    async def test_media_and_quote_preserve_original_input_until_gateway(self):
        adapter = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        adapter.mount(host)
        await adapter.prepare()
        self.updates = [update(1, 6, caption="  user\npass  ",
                               photo=[{"file_id": "small"}, {"file_id": "large"}],
                               reply_to_message={"message_id": 5, "chat": {"id": USER_ID}}),
                        update(2, 7, "cross chat", reply_to_message={"message_id": 5, "chat": {"id": GROUP_ID}})]
        await adapter._poller.poll_once()
        msg, other = [call.args[1] for call in host.accept.await_args_list]
        self.assertEqual(msg.text, "  user\npass  ")
        self.assertEqual(msg.references, [("id", "123:5")])
        self.assertEqual(other.references, [])
        self.assertEqual(msg.attachments, [Attachment("image", "photo.jpg", "image/jpeg", file_info="large")])
        self.assertEqual(await adapter.download_attachment(msg.attachments[0], 4), b"data")
        with self.assertRaisesRegex(ValueError, "limit"):
            await adapter.download_attachment(msg.attachments[0], 3)
        self.responses["getFile"] = [ok({"file_path": "../getUpdates"})]
        with self.assertRaisesRegex(ValueError, "file path"):
            await adapter.download_attachment(msg.attachments[0], 10)
        self.responses["getFile"] = [ok({"file_path": "photos/file_1.jpg"})]
        self.responses["file_1.jpg"] = [httpx.Response(200, content=b"xxxx", headers={"Content-Length": "1"})]
        with self.assertRaisesRegex(ValueError, "limit"):
            await adapter.download_attachment(msg.attachments[0], 3)

    async def test_retryable_holds_batch_and_cancelled_update_is_not_replayed(self):
        self.updates = [update(1, 5, "first"), update(2, 6, "second")]
        adapter = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(side_effect=["retryable", asyncio.CancelledError()])
        adapter.mount(host)
        await adapter.prepare()
        self.assertFalse(await adapter._poller.poll_once())
        self.assertEqual(host.accept.await_count, 1)
        with self.assertRaises(asyncio.CancelledError):
            await adapter._poller.poll_once()
        self.assertEqual(host.accept.await_count, 2)
        restarted = self.adapter()
        host.accept = AsyncMock(return_value="handled")
        restarted.mount(host)
        await restarted.prepare()
        await restarted._poller.poll_once()
        host.accept.assert_awaited_once()
        self.assertEqual(host.accept.await_args.args[1].text, "second")

    async def test_stale_offset_is_not_sent(self):
        adapter = self.adapter()
        await adapter.prepare()
        adapter._poller._save(9999)
        adapter._poller._state["saved_at"] = int(time.time()) - 6 * 86400
        self.updates = [update(1, 5, "smaller new ID")]
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        adapter.mount(host)
        await adapter._poller.poll_once()
        self.assertNotIn("offset", self.requests[-1][1])
        host.accept.assert_awaited_once()

    async def test_stop_cancels_in_flight_long_poll(self):
        adapter = self.adapter()
        await adapter.prepare()
        entered = asyncio.Event()

        async def blocking_poll(*args):
            entered.set()
            await asyncio.Future()

        adapter.client.updates = blocking_poll
        running = asyncio.create_task(adapter.start())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(adapter.stop(), 1)
        self.assertTrue(running.cancelled())

    async def test_conflict_is_reported_without_deleting_webhook(self):
        adapter = self.adapter()
        await adapter.prepare()
        self.responses["getUpdates"] = [reject(409, description="webhook is active")]
        with self.assertRaisesRegex(RuntimeError, "no webhook or other poller"):
            await adapter.start()
        self.assertEqual([method for method, _ in self.requests], ["getMe", "getUpdates"])

    async def test_sender_does_not_infer_quotes_from_routing_ids(self):
        adapter = self.adapter()
        for route in ({"input_id": "123:5"}, {"passive_id": "123:6"}):
            with self.subTest(route=route):
                env = DeliveryEnvelope(next(iter(route)), "private", str(USER_ID), "plain reply",
                                       purpose="reply", **route)
                self.assertTrue((await adapter.deliver(env)).confirmed)
                self.assertEqual(self.requests[-1], ("sendMessage", {"chat_id": USER_ID, "text": "plain reply"}))

    async def test_sender_rejection_and_unknown_keep_prior_receipts(self):
        adapter = self.adapter(chunk_chars=5)
        delivery = DeliveryEnvelope("key", "private", str(USER_ID), "hello!",
                                    input_id="123:5", passive_id="123:6",
                                    quote=NativeQuote((("id", "123:77"),)))
        self.responses["sendMessage"] = [ok({"message_id": 1, "chat": {"id": USER_ID}}), reject(403)]
        result = await adapter.deliver(delivery)
        self.assertEqual(result.status, DeliveryStatus.PARTIAL)
        self.assertEqual(result.receipts[0].references, (("id", "123:1"),))
        self.assertEqual(self.requests[0][1]["reply_parameters"]["message_id"], 77)
        self.assertNotIn("reply_parameters", self.requests[1][1])
        self.responses["sendMessage"] = [ok({"message_id": 2, "chat": {"id": USER_ID}}), httpx.ReadTimeout(TOKEN)]
        result = await adapter.deliver(delivery)
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        self.assertNotIn(TOKEN, result.reason)
        self.assertEqual(result.receipts[0].references, (("id", "123:2"),))
        self.assertEqual(await adapter.deliver(delivery), result)
        self.assertEqual(len(self.requests), 4)

    async def test_multipart_and_cross_chat_reference(self):
        adapter = self.adapter()
        self.responses["sendPhoto"] = [ok({"message_id": 4, "chat": {"id": USER_ID}})]
        env = DeliveryEnvelope("key", "private", str(USER_ID), images=[{"data": b"png"}],
                               quote=NativeQuote((("id", "-100100:42"),)))
        self.assertTrue((await adapter.deliver(env)).confirmed)
        method, body = self.requests[-1]
        self.assertEqual(method, "sendPhoto")
        self.assertIn(b'name="photo"', body)
        self.assertNotIn(b"reply_parameters", body)
        env.delivery_id = "new"
        env.quote = NativeQuote((("id", "123:42"),))
        self.responses["sendPhoto"] = [ok({"message_id": 5, "chat": {"id": USER_ID}})]
        self.assertTrue((await adapter.deliver(env)).confirmed)
        self.assertIn(b'"message_id": 42', self.requests[-1][1])

    async def test_ambiguous_api_error_does_not_expose_response_body(self):
        adapter = self.adapter()
        self.responses["getMe"] = [httpx.Response(503, json={"ok": False, "description": TOKEN})]
        with self.assertRaises(TelegramAPIError) as cm:
            await adapter.client.call("getMe")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertFalse(cm.exception.rejected)
