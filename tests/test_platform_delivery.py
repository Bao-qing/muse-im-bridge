"""Offline contracts for resolved, platform-only delivery envelopes."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from muse_bridge.adapters.onebot.client import OneBotAPIError
from muse_bridge.adapters.onebot.outbound import OneBotSender
from muse_bridge.adapters.qq.outbound import QQSender
from muse_bridge.adapters.wechat.client import WeChatAPIError
from muse_bridge.adapters.wechat.outbound import WeChatSenderTransport
from muse_bridge.adapters.wechat.sender import WeChatSender
from muse_bridge.core.delivery import DeliveryEnvelope, DeliveryStatus, NativeQuote


def config(**overrides):
    return SimpleNamespace(**(dict(routes_file=None, chunk_chars=100, max_chunks=10,
                                   max_active_chunks=10, max_image_bytes=8, max_video_bytes=16,
                                   max_video_upload_bytes=8, max_file_bytes=8,
                                   download_timeout_s=1) | overrides))


def qq_client():
    return Mock(send_message=Mock(return_value=(200, {"id": "qq-id", "ext_info": {"ref_idx": "qq-ref"}})),
                upload_media=Mock(return_value="file-info"), download_bytes=Mock(return_value=b"image"))


def onebot_client():
    return Mock(send_msg=Mock(return_value={"message_id": 42}),
                upload_file=Mock(return_value=None), download_bytes=Mock(return_value=b"image"))


class PlatformDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_oversized_video_reports_size_without_upload_or_content(self):
        client = qq_client()
        secret_name = "private-name-must-not-appear.mp4"
        result = await QQSender(config(), client).deliver(DeliveryEnvelope(
            "oversized-video", "private", "1", videos=[{"data": b"123456789", "filename": secret_name}]))
        self.assertEqual(result.status, DeliveryStatus.REJECTED)
        self.assertIn("video exceeds upload limit (9 > 8 bytes)", result.reason)
        self.assertNotIn(secret_name, result.reason)
        client.upload_media.assert_not_called()
        client.send_message.assert_not_called()

    async def test_qq_oversized_video_with_text_is_partial_not_confirmed(self):
        result = await QQSender(config(), qq_client()).deliver(DeliveryEnvelope(
            "oversized-video-text", "private", "1", text="caption", videos=[{"data": b"123456789"}]))
        self.assertEqual(result.status, DeliveryStatus.PARTIAL)
        self.assertEqual(len(result.receipts), 1)
        self.assertIn("exceeds upload limit", result.reason)

    async def test_media_preparation_failure_is_rejected_or_partial(self):
        for factory, client_factory in ((QQSender, qq_client), (OneBotSender, onebot_client)):
            for text, expected in (("", DeliveryStatus.REJECTED), ("text", DeliveryStatus.PARTIAL)):
                with self.subTest(platform=factory.__name__, text=text):
                    client = client_factory()
                    client.download_bytes.side_effect = OSError("offline")
                    result = await factory(config(), client).deliver(DeliveryEnvelope(
                        "media-fail", "private", "1", text=text, images=[{"url": "https://unused.invalid"}]))
                    self.assertEqual(result.status, expected)
                    self.assertEqual(len(result.receipts), int(bool(text)))

    async def test_qq_media_upload_failure_is_not_success(self):
        client = qq_client()
        client.upload_media.side_effect = OSError("upload refused")
        result = await QQSender(config(), client).deliver(DeliveryEnvelope(
            "upload-fail", "private", "1", images=[{"data": b"image"}]))
        self.assertEqual(result.status, DeliveryStatus.REJECTED)
        client.send_message.assert_not_called()

    async def test_unknown_send_has_no_retry_or_fallback(self):
        for factory, client_factory, send_name in ((QQSender, qq_client, "send_message"),
                                                   (OneBotSender, onebot_client, "send_msg")):
            with self.subTest(platform=factory.__name__):
                client = client_factory()
                send = getattr(client, send_name)
                send.side_effect = TimeoutError("possibly accepted")
                sender = factory(config(), client)
                envelope = DeliveryEnvelope("uncertain", "group", "1", text="hello",
                                            passive_id="incoming", fallback_target="2")
                self.assertEqual((await sender.deliver(envelope)).status, DeliveryStatus.UNKNOWN)
                self.assertEqual((await sender.deliver(envelope)).status, DeliveryStatus.UNKNOWN)
                self.assertEqual(send.call_count, 1)

    async def test_unknown_keeps_already_confirmed_receipts(self):
        client = onebot_client()
        client.send_msg.side_effect = [{"message_id": 42}, TimeoutError()]
        result = await OneBotSender(config(chunk_chars=10), client).deliver(
            DeliveryEnvelope("partial-unknown", "group", "1", text="first\nsecond\nthird"))
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        self.assertEqual(result.receipts[0].references, (("id", "42"),))
        self.assertEqual(client.send_msg.call_count, 2)

    async def test_native_quotes_and_receipts(self):
        qq = qq_client()
        result = await QQSender(config(), qq).deliver(DeliveryEnvelope(
            "qq-quote", "group", "1", text="hello", plain_text=True,
            quote=NativeQuote((("id", "id"), ("ref", "native-ref"))), quote_fallback="not used"))
        self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
        self.assertEqual(result.receipts[0].as_body(), {"id": "qq-id", "ext_info": {"ref_idx": "qq-ref"}})
        self.assertEqual(qq.send_message.call_args.kwargs["message_reference"]["message_id"], "native-ref")
        self.assertEqual(qq.send_message.call_args.args[2:4], ("hello", None))
        ob = onebot_client()
        result = await OneBotSender(config(), ob).deliver(DeliveryEnvelope(
            "ob-quote", "group", "1", text="hello", quote=NativeQuote((("id", "23"),))))
        self.assertEqual(ob.send_msg.call_args.args[2][0], {"type": "reply", "data": {"id": "23"}})
        self.assertEqual(result.receipts[0].target_id, "1")
        self.assertEqual(result.receipts[0].references, (("id", "42"),))

    async def test_fallback_requires_authorization_and_no_native_quote(self):
        for factory, client_factory, send_name, rejection, success in (
                (QQSender, qq_client, "send_message", (403, {}), (200, {"id": "private-id"})),
                (OneBotSender, onebot_client, "send_msg", OneBotAPIError("send", 100), {"message_id": "private-id"})):
            for authorized, native in ((False, False), (True, True), (True, False)):
                with self.subTest(platform=factory.__name__, authorized=authorized, native=native):
                    client = client_factory()
                    send = getattr(client, send_name)
                    send.side_effect = [rejection, success]
                    result = await factory(config(max_active_chunks=1), client).deliver(DeliveryEnvelope(
                        "fallback", "group", "1", text="hello", fallback_target="2" if authorized else None,
                        quote=NativeQuote((("id", "native"),)) if native else None))
                    allowed = authorized and not native
                    self.assertEqual(result.status, DeliveryStatus.CONFIRMED if allowed else DeliveryStatus.REJECTED)
                    self.assertEqual(send.call_count, 2 if allowed else 1)
                    if allowed:
                        self.assertEqual((result.receipts[0].target_type, result.receipts[0].target_id), ("private", "2"))

    async def test_qq_media_fallback_uploads_for_actual_recipient(self):
        client = qq_client()
        client.send_message.side_effect = [(403, {}), (200, {"id": "private-image"})]
        result = await QQSender(config(), client).deliver(DeliveryEnvelope(
            "media-fallback", "group", "1", images=[{"data": b"image"}], fallback_target="2"))
        self.assertEqual(result.status, DeliveryStatus.CONFIRMED)
        self.assertEqual([call.args[:2] for call in client.upload_media.call_args_list], [("group", "1"), ("private", "2")])
        self.assertEqual(result.receipts[0].target_id, "2")

    async def test_qq_http_server_error_is_unknown_not_active_retry(self):
        client = qq_client()
        client.send_message.return_value = (503, {})
        result = await QQSender(config(), client).deliver(DeliveryEnvelope(
            "server-error", "group", "1", text="hello", passive_id="input", fallback_target="2"))
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        self.assertEqual(client.send_message.call_count, 1)

    async def test_qq_passive_limits_are_shared_across_deliveries(self):
        for kind, limit in (("private", 4), ("group", 5)):
            with self.subTest(kind=kind):
                client = qq_client()
                sender = QQSender(config(max_active_chunks=0), client)
                results = await asyncio.gather(*(sender.deliver(DeliveryEnvelope(
                    f"{kind}-{index}", kind, "1", text="hello", passive_id="same-input",
                    purpose="notification" if index % 2 else "reply")) for index in range(limit + 1)))
                self.assertEqual([call.args[6] for call in client.send_message.call_args_list], list(range(1, limit + 1)))
                self.assertEqual(results[-1].status, DeliveryStatus.REJECTED)

    async def test_qq_migrates_and_persists_reservations_before_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory) / "routes.json"
            original = {"old-turn": {"msg_id": "input", "seq": 2}, "other": {"msg_id": "input", "seq": 3}}
            routes.write_text(json.dumps(original), encoding="utf-8")
            cfg = config(routes_file=str(routes))
            sidecar = routes.with_suffix(".passive.local.json")
            client = qq_client()
            def send(*args, **kwargs):
                saved = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(saved["next_sequences"]["input"], args[6] + 1)
                return 200, {"id": "sent"}
            client.send_message.side_effect = send
            await QQSender(cfg, client).deliver(DeliveryEnvelope("before-restart", "private", "1", text="x", passive_id="input"))
            await QQSender(cfg, client).deliver(DeliveryEnvelope("after-restart", "private", "1", text="x", passive_id="input"))
            self.assertEqual([call.args[6] for call in client.send_message.call_args_list], [3, 4])
            self.assertEqual(json.loads(routes.read_text(encoding="utf-8")), original)

    async def test_qq_failed_reservation_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = qq_client()
            sender = QQSender(config(routes_file=str(Path(directory) / "routes.json")), client)
            with patch("muse_bridge.adapters.qq.outbound.atomic_json_write", side_effect=OSError()):
                result = await sender.deliver(DeliveryEnvelope("disk-error", "private", "1", text="x", passive_id="input"))
            self.assertEqual(result.status, DeliveryStatus.REJECTED)
            client.send_message.assert_not_called()

    async def test_onebot_malformed_response_is_unknown(self):
        client = onebot_client()
        client.send_msg.side_effect = OneBotAPIError("send", None, "malformed response")
        result = await OneBotSender(config(), client).deliver(DeliveryEnvelope(
            "malformed", "group", "1", text="hello", fallback_target="2"))
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        self.assertEqual(client.send_msg.call_count, 1)

    async def test_onebot_uploaded_file_is_partial_if_companion_refused(self):
        client = onebot_client()
        client.send_msg.side_effect = OneBotAPIError("send", 100)
        result = await OneBotSender(config(), client).deliver(DeliveryEnvelope(
            "file", "group", "1", files=[{"data": b"file", "filename": "f.txt"}], fallback_target="2"))
        self.assertEqual(result.status, DeliveryStatus.PARTIAL)
        self.assertEqual(result.receipts[0].target_id, "1")
        self.assertEqual(result.receipts[0].references, ())
        self.assertEqual(client.upload_file.call_count, 1)
        self.assertEqual(client.send_msg.call_count, 1)


class MemoryWeChatState:
    cooldown_until = 0

    def __init__(self):
        self.parts = {}
        self.context = Mock(side_effect=lambda user, mid=None: "snapshot" if mid == "input" else "latest")

    def part_status(self, key):
        return self.parts.get(key)

    def reserve_part(self, key):
        self.parts[key] = "sending"

    def finish_part(self, key, status):
        if status == "retryable":
            self.parts.pop(key, None)
        else:
            self.parts[key] = status


class WeChatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = config()
        self.state = MemoryWeChatState()
        self.client = SimpleNamespace(send_message=AsyncMock(return_value={"message_id": 7}),
                                      download_public=AsyncMock(side_effect=OSError()))
        self.media = SimpleNamespace(upload=AsyncMock(return_value={"type": 2}))
        self.sender = WeChatSender(self.client, self.state, self.cfg, self.media)
        self.transport = WeChatSenderTransport(self.cfg, self.sender)

    async def test_reply_requires_input_and_other_purposes_use_latest(self):
        result = await self.transport.deliver(DeliveryEnvelope("invalid", "private", "u", text="x", purpose="reply"))
        self.assertEqual(result.status, DeliveryStatus.REJECTED)
        self.client.send_message.assert_not_awaited()
        reply = await self.transport.deliver(DeliveryEnvelope("reply", "private", "u", text="x", purpose="reply", input_id="input"))
        self.assertEqual(reply.receipts[0].references, (("id", "7"),))
        await self.transport.deliver(DeliveryEnvelope("notice", "private", "u", text="x", purpose="notification", input_id="input", passive_id="stale"))
        self.assertEqual([call.args[0]["context_token"] for call in self.client.send_message.await_args_list], ["snapshot", "latest"])

    async def test_logical_id_preserves_ledger_and_suppresses_unknown_replay(self):
        self.client.send_message.side_effect = TimeoutError()
        envelope = DeliveryEnvelope("stable-logical-id", "private", "u", text="hello")
        for _ in range(2):
            self.assertEqual((await self.transport.deliver(envelope)).status, DeliveryStatus.UNKNOWN)
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertEqual(list(self.state.parts.values()), ["unknown"])

    async def test_media_preparation_failure_is_rejected(self):
        result = await self.transport.deliver(DeliveryEnvelope(
            "media-error", "private", "u", images=[{"url": "https://unused.invalid"}]))
        self.assertEqual(result.status, DeliveryStatus.REJECTED)
        self.client.send_message.assert_not_awaited()

    async def test_known_refusal_after_media_is_partial_with_receipt(self):
        self.client.send_message.side_effect = [{"message_id": "image-id"}, WeChatAPIError(100)]
        result = await self.transport.deliver(DeliveryEnvelope(
            "partial", "private", "u", text="caption", images=[{"data": b"image"}]))
        self.assertEqual(result.status, DeliveryStatus.PARTIAL)
        self.assertEqual(result.receipts[0].references, (("id", "image-id"),))


if __name__ == "__main__":
    unittest.main()
