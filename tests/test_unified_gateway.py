"""Behavioral contracts for one business gateway hosting multiple platform drivers."""
import asyncio
import ast
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from muse_bridge.adapters.base import BaseAdapter
from muse_bridge.config.models import AccessRequestsConfig, MuseConfig, QQConfig
from muse_bridge.core.delivery import DeliveryEnvelope, DeliveryReceipt, DeliveryResult, DeliveryStatus
from muse_bridge.core.message import Attachment, InboundMessage, MessageSource
from muse_bridge.gateway.muse_events import MuseEventPump
from muse_bridge.gateway.runtime import UnifiedGateway
from muse_bridge.muse.service import MuseService


class Driver(BaseAdapter):
    def __init__(self, config, platform="qq", account="account"):
        self.config = config
        self.platform, self.account = platform, account
        self.sent = []
        self.signals = []
        self.download_started = asyncio.Event()
        self.download_release = asyncio.Event()
        self.status = DeliveryStatus.CONFIRMED

    @property
    def name(self):
        return self.platform

    @property
    def is_enabled(self):
        return True

    def account_identity(self):
        return self.account

    async def start(self):
        pass

    async def stop(self):
        pass

    async def download_attachment(self, attachment, max_bytes):
        self.download_started.set()
        await self.download_release.wait()
        return b"media"

    async def deliver(self, envelope):
        self.sent.append(envelope)
        return DeliveryResult(self.status, [DeliveryReceipt(
            envelope.target_type, envelope.target_id, (("id", f"receipt-{len(self.sent)}"),))])

    async def activity(self, target, token, state):
        self.signals.append((target, token, state))


class UnifiedGatewayCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        token = MagicMock()
        token.has_credentials.return_value = True
        self.service = MuseService(MuseConfig(cookies_file=str(self.root / "cookies")), token)
        self.service.interactions.approvals_ready = True
        self.service.post_message = MagicMock(side_effect=lambda *args, **kwargs: "m" + str(self.service.post_message.call_count))
        self.gateway = UnifiedGateway(self.service)
        self.addAsyncCleanup(self.gateway.stop)

    def driver(self, label="qq", account="account"):
        directory = self.root / label
        cfg = QQConfig(app_id=account, app_secret="test", admin_id=["u"],
            allow_all_group=True, allow_all_private=True,
            routes_file=str(directory / "routes.json"), session_map_file=str(directory / "sessions.json"),
            push_state_file=str(directory / "push.json"), delivered_file=str(directory / "delivered.jsonl"),
            access_requests=AccessRequestsConfig(state_file=str(directory / "access.json")))
        driver = Driver(cfg, label, account)
        self.gateway.mount(driver)
        ctx = self.gateway.contexts[driver]
        ctx.outbound_pusher.ensure_baseline = AsyncMock()
        return driver, ctx

    def message(self, platform="qq", mid="input", text="hello", kind="private", target="u", attachments=()):
        return InboundMessage(mid, MessageSource(platform, kind, target, "u"), text, list(attachments))

    async def test_text_and_command_cannot_overtake_slow_image(self):
        driver, ctx = self.driver()
        first = asyncio.create_task(driver.emit(self.message(mid="image", text="first", attachments=[Attachment("image", url="https://example.invalid/image")])) )
        await driver.download_started.wait()
        second = asyncio.create_task(driver.emit(self.message(mid="text", text="second")))
        command = asyncio.create_task(driver.emit(self.message(mid="command", text="/帮助")))
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.assertFalse(command.done())
        self.assertEqual(self.service.post_message.call_count, 0)
        driver.download_release.set()
        await asyncio.gather(first, second, command)
        self.assertEqual([c.args[1] for c in self.service.post_message.call_args_list], ["first", "second"])
        self.assertTrue(driver.sent)

    async def test_slow_video_blocks_shared_session_but_not_other_platform(self):
        first, ctx1 = self.driver()
        second, ctx2 = self.driver("onebot", "other")
        ctx1.session_mapper.bind("private", "u", "shared")
        ctx2.session_mapper.bind("private", "u", "shared")
        task = asyncio.create_task(first.emit(self.message(mid="video", text="video", attachments=[Attachment("video", url="https://example.invalid/video")])))
        await first.download_started.wait()
        shared = asyncio.create_task(second.emit(self.message("onebot", "shared-input", "shared-text")))
        independent = asyncio.create_task(second.emit(self.message("onebot", "independent", "independent", target="other-user")))
        await independent
        self.assertFalse(shared.done())
        first.download_release.set()
        await asyncio.gather(task, shared)
        self.assertEqual([c.args[1] for c in self.service.post_message.call_args_list], ["independent", "video", "shared-text"])

    async def test_same_ids_on_different_platforms_do_not_collide(self):
        a, ca = self.driver()
        b, cb = self.driver("onebot", "account")
        await asyncio.gather(a.emit(self.message()), b.emit(self.message("onebot")))
        self.assertNotEqual(ca.session_mapper.current("private", "u"), cb.session_mapper.current("private", "u"))
        self.assertEqual(self.service.post_message.call_count, 2)

    async def test_foreground_credential_blocks_chat_without_capturing_it(self):
        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")
        self.service.interactions.add("credential", session_id="session", remote_id="card", context={"foreground_blocking": True})
        await driver.emit(self.message(text="ordinary input, not a secret"))
        self.service.post_message.assert_not_called()
        self.assertIn("等待填写凭证", driver.sent[-1].text)
        self.assertIsNone(self.service.interactions.current(ctx.interaction_owner("private", "u", "u")))

    async def test_background_credential_does_not_acquire_input(self):
        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")
        self.service.interactions.add("credential", session_id="session", remote_id="background", context={"background_credential": True})
        await driver.emit(self.message(text="42"))
        self.service.post_message.assert_called_once()
        self.assertEqual(self.service.post_message.call_args.args[1], "42")

    async def test_background_approval_push_does_not_acquire_input(self):
        driver, ctx = self.driver()
        item = self.service.interactions.add("approval", remote_id="background", context={"background_approval": True})
        await driver.emit(self.message(text="42"))
        self.service.post_message.assert_called_once()
        self.assertIsNone(self.service.interactions.current(ctx.interaction_owner("private", "u", "u")))

    async def test_credential_card_requires_positive_foreground_evidence(self):
        from unittest.mock import patch
        from urllib.parse import urlencode
        from muse_bridge.gateway.outbound import OutboundDispatcher

        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")
        fields = {"page_url": "https://example.invalid/", "password_label": "SSH key",
                  "capture_authorization": "fictional-authorization"}
        card = {"event_name": "delta.presentation", "message_id": "card-mid", "seq": 2,
                "payload": {"presentation_id": "card-id", "data": {"type": "credentials_vault",
                    "data": {"capture": "private_key", **fields,
                             "url": "https://muse.ai/connect/browser-credentials#" + urlencode(fields)}}}}
        with patch.object(OutboundDispatcher, "_scan_and_dispatch", new=AsyncMock()):
            await ctx.outbound_pusher._scan_and_dispatch([card], "session")
            item = self.service.interactions.by_remote("credential", "card-id")
            self.assertFalse(item.context.get("foreground_blocking"))
            ctx.route_manager.register("user-mid", "private", "u", "input-mid")
            history = [{"role": "user", "message_id": "user-mid", "seq": 1}, card]
            await ctx.outbound_pusher._scan_and_dispatch(history, "session")
            self.assertTrue(item.context.get("foreground_blocking"))
            # Explicit scheduler evidence overrides the earlier causal route.
            card["payload"]["scope_type"] = "schedule"
            await ctx.outbound_pusher._scan_and_dispatch(history, "session")
            self.assertTrue(item.context.get("background_credential"))
            self.assertFalse(item.context.get("foreground_blocking"))
        await driver.emit(self.message(text="ordinary background chat"))
        self.service.post_message.assert_called_once()

    async def test_unconfirmed_delivery_is_logged_with_reason_not_content(self):
        driver, ctx = self.driver()
        driver.status = DeliveryStatus.REJECTED
        secret = "private-message-text-must-not-be-logged"
        with self.assertLogs("muse_bridge.gateway.delivery", level="WARNING") as logs:
            confirmed = await ctx.outbound_pusher._deliver("private", "u", secret, [], [], [{"data": b"x"}])
        self.assertFalse(confirmed)
        joined = "\n".join(logs.output)
        self.assertIn("private:u rejected", joined)
        self.assertNotIn(secret, joined)

    async def test_duplicate_inbound_is_consumed_once(self):
        driver, ctx = self.driver()
        await driver.emit(self.message())
        await driver.emit(self.message())
        self.assertEqual(self.service.post_message.call_count, 1)

    async def test_unknown_delivery_not_repeated_after_context_recreation(self):
        driver, ctx = self.driver()
        driver.status = DeliveryStatus.UNKNOWN
        envelope = DeliveryEnvelope("stable", "private", "u", "probe")
        self.assertFalse(await ctx.outbound_pusher.send_envelope(envelope))
        from muse_bridge.gateway.chat import ChannelContext
        restored = ChannelContext(driver, self.gateway)
        self.assertFalse(await restored.outbound_pusher.send_envelope(envelope))
        self.assertEqual(len(driver.sent), 1)

    async def test_partial_delivery_not_repeated(self):
        driver, ctx = self.driver()
        driver.status = DeliveryStatus.PARTIAL
        envelope = DeliveryEnvelope("partial", "private", "u", "probe")
        await ctx.outbound_pusher.send_envelope(envelope)
        await ctx.outbound_pusher.send_envelope(envelope)
        self.assertEqual(len(driver.sent), 1)

    async def test_one_event_pump_unions_platform_sessions(self):
        a, ca = self.driver()
        b, cb = self.driver("onebot", "other")
        ca.session_mapper.bind("private", "u", "shared")
        cb.session_mapper.bind("private", "u", "shared")
        cb.session_mapper.bind("group", "g", "other")
        pump = MuseEventPump(self.gateway)
        self.assertEqual(set(pump.session_mapper.session_ids()), {"shared", "other"})
        self.assertEqual(len(pump.session_mapper.owners("shared")), 2)
        with self.assertRaises(RuntimeError):
            await ca.outbound_pusher.start()

    async def test_shared_session_reply_has_one_origin_channel(self):
        a, ca = self.driver()
        b, cb = self.driver("onebot", "other")
        ca.session_mapper.bind("private", "u", "shared")
        cb.session_mapper.bind("private", "u", "shared")
        ca.route_manager.register("user-mid", "private", "u", "input-mid")
        self.assertEqual(ca.outbound_pusher.resolve_delivery_target("shared", "user-mid", "execution")[1], "u")
        self.assertEqual(cb.outbound_pusher.resolve_delivery_target("shared", "user-mid", "execution")[1], "")

    async def test_account_change_uses_new_repository_shard(self):
        self.service.config.session_id = "main-from-old-account"
        driver, old = self.driver()
        old.session_mapper.bind("group", "g", "old-session")
        driver.account = "replacement"
        self.gateway.identity_changed(driver)
        new = self.gateway.contexts[driver]
        self.assertEqual(new.session_mapper.current("group", "g"), "")
        self.assertEqual(old.session_mapper.current("group", "g"), "old-session")
        self.assertNotEqual(new.config.routes_file, old.config.routes_file)
        self.assertFalse(new.seed_main)
        self.assertEqual(new.session_mapper.current("private", "u"), "")
        new.outbound_pusher.ensure_baseline = AsyncMock()
        await driver.emit(self.message(mid="new-account-input"))
        self.assertNotEqual(self.service.post_message.call_args.args[0], "main-from-old-account")

    async def test_unverified_legacy_onebot_state_is_preserved_not_adopted(self):
        from muse_bridge.config.models import OneBotConfig
        import json
        root = self.root / "legacy-onebot"
        root.mkdir()
        sessions = root / "sessions.json"
        sessions.write_text(json.dumps({"private:u": "old-account-session"}), encoding="utf-8")
        cfg = OneBotConfig(base_url="https://napcat.example.invalid", admin_id=["u"],
            routes_file=str(root / "routes.json"), session_map_file=str(sessions),
            push_state_file=str(root / "push.json"), delivered_file=str(root / "delivered.jsonl"),
            access_requests=AccessRequestsConfig(state_file=str(root / "access.json")))
        driver = Driver(cfg, "onebot", "actual-new-login")
        self.gateway.mount(driver)
        ctx = self.gateway.contexts[driver]
        self.assertEqual(ctx.session_mapper.current("private", "u"), "")
        self.assertFalse(ctx.seed_main)
        self.assertEqual(json.loads(sessions.read_text(encoding="utf-8")), {"private:u": "old-account-session"})
        self.assertNotEqual(ctx.config.session_map_file, str(sessions))

    async def test_contexts_cannot_share_mutable_state_path(self):
        driver, ctx = self.driver()
        other = Driver(driver.config, "onebot", "different")
        with self.assertRaises(ValueError):
            self.gateway.mount(other)

    async def test_retired_origin_does_not_replay_into_other_channel_after_restore(self):
        from muse_bridge.gateway.delivery import GatewayOutbound
        a, ca = self.driver()
        b, cb = self.driver("onebot", "other")
        ca.session_mapper.bind("private", "u", "shared")
        cb.session_mapper.bind("group", "B", "shared")
        cb.route_manager.register("u1", "group", "B", "input-B")
        history = [
            {"role": "user", "message_id": "u1", "seq": 1},
            {"role": "assistant", "message_id": "a1", "seq": 2,
             "event_name": "message.assistant", "display_text": "B-only-answer",
             "payload": {"status": "completed", "metadata": {"root_message_execution_id": "root1"}}},
            {"role": "user", "message_id": "u2", "seq": 3},
        ]
        # The original worker retires u1 before the other worker's first scan.
        await cb.outbound_pusher._scan_and_dispatch(history, "shared")
        self.assertIsNone(cb.route_manager.get("u1"))
        self.assertEqual([e.target_id for e in b.sent], ["B"])
        await ca.outbound_pusher._scan_and_dispatch(history, "shared")
        self.assertFalse(a.sent)
        # Restore the causal ledger, deliberately discard seen IDs to prove that
        # privacy does not depend on a bounded per-worker history cache.
        cb.outbound_pusher = GatewayOutbound(cb)
        ca.outbound_pusher = GatewayOutbound(ca)
        ca.outbound_pusher._seen_ids.clear()
        await ca.outbound_pusher._scan_and_dispatch(history, "shared")
        await cb.outbound_pusher._scan_and_dispatch(history, "shared")
        self.assertFalse(a.sent)
        self.assertEqual(len(b.sent), 1)

    async def test_first_secondary_admin_waits_for_main_session_route_registration(self):
        import threading
        self.service.config.session_id = "main"
        driver, ctx = self.driver()
        ctx.config.admin_id.append("secondary")
        ctx.outbound_pusher._gated.add("main")
        started, release = threading.Event(), threading.Event()

        def post(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("Test barrier was not released")
            return "pending-user-mid"

        self.service.post_message.side_effect = post
        msg = InboundMessage("secondary-input", MessageSource("qq", "private", "secondary", "secondary"), "hello")
        submit = asyncio.create_task(driver.emit(msg))
        pump = MuseEventPump(self.gateway)
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            self.assertTrue(self.gateway.session_lock("main").locked())
            await pump._hydrate_and_dispatch("main", [
                {"role": "user", "message_id": "pending-user-mid", "seq": 1},
                {"role": "assistant", "message_id": "early-reply", "seq": 2,
                 "event_name": "message.assistant", "display_text": "reply-for-secondary",
                 "payload": {"status": "completed"}},
            ], False)
            await asyncio.sleep(0)
            self.assertFalse(driver.sent)
            release.set()
            await submit
            await asyncio.wait_for(pump._queues[ctx].join(), 3)
            self.assertEqual([e.target_id for e in driver.sent], ["secondary"])
        finally:
            release.set()
            await asyncio.gather(submit, return_exceptions=True)
            for task in pump._workers.values():
                task.cancel()
            await asyncio.gather(*pump._workers.values(), return_exceptions=True)

    async def test_failed_startup_login_notice_does_not_stop_gateway(self):
        driver, ctx = self.driver()
        self.gateway.config.login_through_adapter = "qq"
        self.service.verify_connection = MagicMock(return_value=False)
        self.service.token_manager.has_credentials.return_value = False
        attempted = asyncio.Event()

        async def hold(*args, **kwargs):
            await asyncio.Event().wait()

        async def refuse(envelope):
            attempted.set()
            return DeliveryResult(DeliveryStatus.REJECTED)

        driver.start = AsyncMock(side_effect=hold)
        driver.deliver = refuse
        self.gateway._run_events = AsyncMock(side_effect=hold)
        self.service.interaction_protocol.run = AsyncMock(side_effect=hold)
        running = asyncio.create_task(self.gateway.start())
        try:
            await asyncio.wait_for(attempted.wait(), 3)
            await asyncio.sleep(0)
            self.assertFalse(running.done())
            self.assertTrue(self.gateway._running)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_login_notice_unknown_or_exception_is_best_effort(self):
        driver, ctx = self.driver()
        self.gateway.config.login_through_adapter = "qq"
        for failure in (DeliveryStatus.UNKNOWN, OSError("test transport")):
            with self.subTest(failure=failure):
                if isinstance(failure, Exception):
                    ctx.send_notice = AsyncMock(side_effect=failure)
                else:
                    ctx.send_notice = AsyncMock(side_effect=RuntimeError(failure.value))
                await self.gateway.login_required()
                ctx.send_notice.assert_awaited_once()

    async def test_distinct_routes_cannot_share_another_mutable_repository(self):
        driver, ctx = self.driver()
        other = Driver(dataclasses.replace(driver.config, routes_file=str(self.root / "different-routes.json")),
                       "onebot", "other")
        with self.assertRaises(ValueError):
            self.gateway.mount(other)

    async def test_oversized_workspace_video_skips_fs_read_and_sends_user_notice(self):
        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")
        ctx.outbound_pusher._gated.add("session")
        pump = MuseEventPump(self.gateway)
        video_event = {
            "role": "assistant",
            "event_name": "delta.presentation",
            "message_id": "vid-msg-1",
            "seq": 2,
            "display_text": "跳舞视频",
            "payload": {
                "kind": "video",
                "data": {
                    "fallback_text": "![跳舞视频](sandbox://workspace/imagine_media/dance.mp4)",
                    "videos": [{
                        "byte_len": 11093552,
                        "label": "跳舞视频",
                        "mime": "video/mp4",
                        "missing": False,
                        "path": "sandbox://workspace/imagine_media/dance.mp4",
                    }],
                },
            },
        }
        client = MagicMock()
        client.get_history.return_value = [video_event]
        client.read_file_bounded = MagicMock(side_effect=AssertionError("Should skip /fs/read when byte_len > cap"))
        hydrated = pump._fetch_session(client, "session")
        client.read_file_bounded.assert_not_called()
        with self.assertLogs("muse_bridge.gateway.outbound", level="WARNING") as logs:
            await ctx.outbound_pusher._scan_and_dispatch(hydrated, "session")
        self.assertIn("Skipping oversized video", "\n".join(logs.output))
        self.assertEqual(len(driver.sent), 1)
        self.assertEqual(driver.sent[0].videos, [])
        self.assertEqual(
            driver.sent[0].text,
            "跳舞视频\n【提示】视频大小（10.6MB）超过当前渠道发送上限（10MB），无法直接发送。",
        )
        # Subsequent syncs must neither re-read nor re-send the handled event.
        hydrated_again = pump._fetch_session(client, "session")
        await ctx.outbound_pusher._scan_and_dispatch(hydrated_again, "session")
        self.assertEqual(len(driver.sent), 1)

    async def test_event_pump_does_not_reread_already_delivered_workspace_media(self):
        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")
        ctx.outbound_pusher._gated.add("session")
        pump = MuseEventPump(self.gateway)
        img_event = {
            "role": "assistant",
            "event_name": "delta.presentation",
            "message_id": "img-msg-1",
            "seq": 1,
            "display_text": "cat.webp",
            "payload": {"kind": "image", "image_path": "workspace/imagine_media/cat.webp"},
        }
        client = MagicMock()
        client.get_history.return_value = [dict(img_event)]
        client.read_file_bounded = MagicMock(return_value=(b"webp-bytes", 10, False))
        hydrated = pump._fetch_session(client, "session")
        self.assertEqual(client.read_file_bounded.call_count, 1)
        await ctx.outbound_pusher._scan_and_dispatch(hydrated, "session")
        self.assertEqual(len(driver.sent), 1)
        self.assertEqual(len(driver.sent[0].images), 1)

        # Next turn includes the old image event in history plus a new text message.
        text_event = {
            "role": "assistant",
            "event_name": "message.assistant",
            "message_id": "txt-msg-2",
            "seq": 2,
            "display_text": "done",
            "payload": {"status": "completed"},
        }
        client.get_history.return_value = [dict(img_event), text_event]
        hydrated2 = pump._fetch_session(client, "session")
        self.assertEqual(client.read_file_bounded.call_count, 1)
        await ctx.outbound_pusher._scan_and_dispatch(hydrated2, "session")
        self.assertEqual(len(driver.sent), 2)
        self.assertEqual(driver.sent[1].text, "done")

    async def test_read_file_bounded_stops_early_and_sends_limit_notice(self):
        import base64
        from muse_bridge.muse.client.client import MuseClient

        raw_client = MuseClient.__new__(MuseClient)
        chunk = b"x" * 600 * 1024
        calls = []

        def fake_fs_call(path, body):
            calls.append(body["offset"])
            return {"len": len(chunk), "data_base64": base64.b64encode(chunk).decode(), "eof": False}

        raw_client._fs_call = fake_fs_call
        data, exact_size, exceeded = raw_client.read_file_bounded("workspace/big.mp4", 1024 * 1024, chunk_size=len(chunk))
        self.assertIsNone(data)
        self.assertIsNone(exact_size)
        self.assertTrue(exceeded)
        # Stopped immediately after crossing 1 MiB on chunk 2 instead of reading more chunks.
        self.assertEqual(len(calls), 2)

        driver, ctx = self.driver()
        ctx.config.max_video_upload_bytes = 1024 * 1024
        ctx.session_mapper.bind("private", "u", "session")
        ctx.outbound_pusher._gated.add("session")
        pump = MuseEventPump(self.gateway)
        md_video = {
            "role": "assistant",
            "event_name": "message.assistant",
            "message_id": "md-vid-1",
            "seq": 1,
            "display_text": "![clip](sandbox://workspace/big.mp4)",
            "payload": {"status": "completed"},
        }
        raw_client.get_history = MagicMock(return_value=[md_video])
        hydrated = pump._fetch_session(raw_client, "session")
        await ctx.outbound_pusher._scan_and_dispatch(hydrated, "session")
        self.assertEqual(len(driver.sent), 1)
        self.assertIn("【提示】视频超过当前渠道发送上限（1MB），无法直接发送。", driver.sent[0].text)

    async def test_url_media_exceeding_upload_limit_at_download_sends_fallback_notice(self):
        driver, ctx = self.driver()
        ctx.session_mapper.bind("private", "u", "session")

        async def deliver_side_effect(envelope):
            driver.sent.append(envelope)
            if envelope.videos:
                return DeliveryResult(
                    DeliveryStatus.PARTIAL,
                    [DeliveryReceipt("private", "u", (("id", "r-text"),))],
                    "QQ video exceeds upload limit (>10485760 bytes)",
                )
            return DeliveryResult(
                DeliveryStatus.CONFIRMED,
                [DeliveryReceipt("private", "u", (("id", "r-notice"),))],
            )

        driver.deliver = deliver_side_effect
        url_video_event = {
            "role": "assistant",
            "event_name": "message.assistant",
            "message_id": "url-vid-1",
            "seq": 1,
            "display_text": "生成结果如下",
            "payload": {"status": "completed"},
            "_extracted_videos": [{"url": "https://cdn.example.invalid/huge.mp4", "filename": "huge.mp4"}],
        }
        await ctx.outbound_pusher._scan_and_dispatch([url_video_event], "session")
        self.assertEqual(len(driver.sent), 2)
        self.assertEqual(driver.sent[1].text, "【提示】视频超过当前渠道发送上限（10MB），无法直接发送。")
        self.assertTrue(ctx.deduplicator.is_delivered("url-vid-1"))

    def test_platform_modules_do_not_import_business_services(self):
        root = Path(__file__).resolve().parents[1] / "muse_bridge" / "adapters"
        for platform in ("qq", "onebot", "wechat"):
            for name in ("adapter.py", "outbound.py"):
                tree = ast.parse((root / platform / name).read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        self.assertNotIn("gateway.chat", module)
                        self.assertNotIn("gateway.outbound", module)
                        self.assertNotIn("muse.service", module)
                        self.assertNotIn("core.session_map", module)
                        self.assertNotIn("core.interactions", module)
