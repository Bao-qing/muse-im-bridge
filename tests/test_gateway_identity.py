"""Offline public boundary regressions; all state lives in temporary directories."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from muse_bridge.config.models import QQConfig, OneBotConfig, MuseConfig
from muse_bridge.core.interactions import InteractionManager, interaction_owner, interaction_target
from muse_bridge.core.message import DeliveryReference, InboundMessage, MessageSource
from muse_bridge.core.state_file import atomic_json_write
from muse_bridge.gateway.chat import ChatGateway
from muse_bridge.gateway.outbound import OutboundDispatcher
from muse_bridge.muse.interactions import MuseInteractions
from muse_bridge.muse.service import MuseService
from muse_bridge.observability import _payload, observed, safe_text


class RecordingDispatcher(OutboundDispatcher):
    platform = "qq"

    async def _deliver(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return True


class MemoryGateway(ChatGateway):
    name = "qq"

    def create_outbound(self):
        dispatcher = RecordingDispatcher(self.config, None, self.muse_service,
            self.deduplicator, self.route_manager, self.session_mapper)
        dispatcher.sent = []
        dispatcher.ensure_baseline = AsyncMock()
        return dispatcher

    async def start(self):
        pass

    async def stop(self):
        pass

    def access_identity(self):
        return "test"

    async def send_reply(self, kind, target, text, msg_id=None):
        self.replies.append(text)
        return {"id": "local-reply"}


class AtomicJSONTests(unittest.TestCase):
    def test_same_directory_fsync_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "nested", "state.json")
            events = []
            replace = os.replace
            fsync = os.fsync

            def sync(fd):
                events.append("sync")
                return fsync(fd)

            def publish(source, target):
                self.assertEqual(Path(source).parent, path.parent)
                self.assertEqual(Path(target), path)
                self.assertEqual(events, ["sync"])
                self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), {"值": 1})
                events.append("replace")
                return replace(source, target)

            with patch("muse_bridge.core.state_file.os.fsync", side_effect=sync), patch(
                    "muse_bridge.core.state_file.os.replace", side_effect=publish):
                atomic_json_write(path, {"值": 1})
            self.assertEqual(events, ["sync", "replace"])
            self.assertEqual(list(path.parent.iterdir()), [path])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failures_leave_old_file_and_remove_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            atomic_json_write(path, {"old": True})
            for stage in ("fsync", "replace"):
                with self.subTest(stage=stage), patch(
                        "muse_bridge.core.state_file.os." + stage, side_effect=OSError("unavailable")):
                    with self.assertRaises(OSError):
                        atomic_json_write(path, {"new": True})
                self.assertEqual(json.loads(path.read_text()), {"old": True})
                self.assertEqual(list(Path(tmp).iterdir()), [path])
            with self.assertRaises(TypeError):
                atomic_json_write(path, {"bad": object()})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_capture_receipt_not_published_on_disk_failure(self):
        protocol = object.__new__(MuseInteractions)
        import threading
        protocol._capture_lock = threading.RLock()
        protocol._capture_receipts = {"old"}
        protocol._receipt_path = "unused"
        with patch("muse_bridge.muse.interactions.atomic_json_write", side_effect=OSError()):
            with self.assertRaises(OSError):
                protocol._record_capture("new")
        self.assertEqual(protocol._capture_receipts, {"old"})


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = InteractionManager()
        self.protocol = object.__new__(MuseInteractions)
        self.protocol.manager = self.manager
        self.protocol._approval_messages = {}

    def test_owners_focus_locks_and_remote_index(self):
        owners = [interaction_owner("private", "7", "7", p, a)
                  for p, a in (("qq", "A"), ("wechat", "A"), ("wechat", "B"))]
        for n, owner in enumerate(owners):
            self.assertEqual(owner[:2], ("private", "7"))
            item = self.manager.add("login", owner=owner, remote_id=str(n))
            self.manager.activate(item, owner)
            self.assertIs(self.manager.current(owner), item)
            self.assertIs(self.manager.by_remote("login", str(n)), item)
        self.assertEqual(len({id(self.manager.owner_lock(o)) for o in owners}), 3)
        self.assertIsNone(self.manager.current(("private", "7", "7")))

    def test_credential_and_approval_references_are_namespaced(self):
        targets = [("private", "7", p, a) for p, a in
                   (("qq", "A"), ("wechat", "A"), ("wechat", "B"))]
        cards = []
        refs = [("id", "same-remote-id")]
        for n, target in enumerate(targets):
            card = self.manager.add("credential", session_id="s")
            cards.append(card)
            self.manager.remember_credential_message(*target[:2], {"id": refs[0][1]}, card, target=target)
            snapshot = (str(n), [(card.id, str(n))])
            self.protocol.mark_displayed(card, target, snapshot)
            self.protocol.remember_approval_message(target, {"id": refs[0][1]}, [card], [snapshot])
        for target, card in zip(targets, cards):
            self.assertIs(self.manager.referenced_credential(*target[:2], refs, target=target), card)
            self.assertEqual(self.protocol.referenced_approvals(target, refs)[0][0], card.id)
            self.assertEqual(list(card.context["displayed_to"]), [target])
            self.assertEqual(list(card.context["queue_shown"]), [target])
        self.assertIsNone(self.manager.referenced_credential("private", "7", refs))
        self.assertIsNone(self.protocol.referenced_approvals(("private", "7"), refs))

    def test_legacy_single_gateway_uses_only_registered_namespace(self):
        self.manager.register_namespace("qq", "app")
        owner = ("private", "7", "7")
        item = self.manager.add("login", owner=owner)
        self.manager.activate(item, owner)
        self.assertIs(self.manager.current(owner), item)
        self.assertEqual(item.owner, (*owner, "qq", "app"))
        self.assertEqual(interaction_target(item.owner), ("private", "7", "qq", "app"))

    async def test_parallel_transport_receipts_use_explicit_namespace(self):
        async def receipt(platform, account):
            item = self.manager.add("credential")
            await asyncio.sleep(0)
            self.manager.remember_credential_message("private", "7", {"id": "same"}, item,
                target=("private", "7", platform, account))
            return item

        cards = await asyncio.gather(receipt("qq", "A"), receipt("wechat", "A"))
        for platform, card in zip(("qq", "wechat"), cards):
            self.assertIs(self.manager.referenced_credential("private", "7", [("id", "same")],
                target=("private", "7", platform, "A")), card)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.service = MuseService(MuseConfig(cookies_file=str(root / "cookies.json")), MagicMock())
        self.service.token_manager.has_credentials.return_value = True
        self.service.post_message = MagicMock(return_value="posted")
        cfg = QQConfig(app_id="app", admin_id=["7"], whitelist={"private": ["7"]},
            routes_file=str(root / "routes.json"), session_map_file=str(root / "map.json"),
            delivered_file=str(root / "delivered.jsonl"), push_state_file=str(root / "push.json"),
            session_bindings={"private:7": "s"})
        cfg.access_requests.state_file = str(root / "access.json")
        self.gateway = MemoryGateway(cfg, self.service)
        self.gateway.replies = []
        self.outbound = self.gateway.outbound_pusher
        self.addCleanup(self.service.interactions.close)

    def msg(self, text="hello"):
        return InboundMessage("m", MessageSource("qq", "private", "7", "7"), text)

    def test_default_identity_and_target_resolution(self):
        self.assertEqual(self.outbound.reference_identity(), "qq:app")
        self.assertEqual(self.gateway.interaction_owner("private", "7", "7"),
                         ("private", "7", "7", "qq", "app"))
        route = self.gateway.route_manager.register("u", "private", "7", "reply")
        self.assertEqual(self.outbound.resolve_delivery_target("s", "u", "root"),
                         ("private", "7", route, None))
        self.assertEqual(self.outbound.resolve_delivery_target("s", None, None),
                         ("private", "7", None, None))
        self.assertEqual(self.outbound.resolve_delivery_target("unbound", "u", None),
                         ("private", "", None, None))
        self.gateway.session_mapper.bind("group", "G", "group-session")
        recent = self.gateway.route_manager.register("other", "group", "G", "passive")
        self.assertEqual(self.outbound.resolve_delivery_target("group-session", None, None),
                         ("group", "G", recent, None))

    def test_onebot_reference_identity_keeps_legacy_format(self):
        self.outbound.platform = "onebot"
        self.outbound.config = OneBotConfig(base_url="http://local.test", routes_file=None,
                                           push_state_file=None)
        self.assertEqual(self.outbound.reference_identity(), "onebot:http://local.test")
        self.outbound.bind_reference_account("123")
        self.assertEqual(self.outbound.account_identity(), "123")
        self.assertEqual(self.outbound.interaction_target("private", "7"),
                         ("private", "7", "onebot", "123"))

    async def test_notification_and_reaction_purpose(self):
        card = self.service.interactions.add("credential", session_id="s")
        await self.outbound.notify_interaction(card, "notice")
        self.assertEqual(self.outbound.sent[0][1]["delivery"].purpose, "notification")
        self.outbound.sent.clear()
        await self.outbound._dispatch_reaction({"event": "reactions.updated", "seq": 1,
            "payload": {"message_id": "u", "reactions": ["+1"]}}, "s")
        delivery = self.outbound.sent[0][1]["delivery"]
        self.assertEqual(delivery.purpose, "notification")
        self.assertTrue(delivery.plain_text)
        self.assertIsNone(DeliveryReference("s", "u").purpose)

    async def test_resolution_override_propagates_purpose(self):
        event = {"role": "assistant", "message_id": "a", "seq": 1,
                 "event_name": "message.assistant", "display_text": "output",
                 "payload": {"status": "completed"}}
        with patch.object(self.outbound, "resolve_delivery_target",
                          return_value=("private", "7", None, "proactive")):
            await self.outbound._scan_and_dispatch([event], "s")
        self.assertEqual(self.outbound.sent[0][1]["delivery"].purpose, "proactive")
        self.assertIsNone(self.outbound.sent[0][1]["route"])

    async def test_success_local_empty_and_denied_are_handled(self):
        self.assertEqual(await self.gateway.handle_inbound(self.msg()), "handled")
        self.assertEqual(await self.gateway.handle_inbound(self.msg("")), "handled")
        denied = InboundMessage("m2", MessageSource("qq", "private", "other", "other"), "hello")
        self.assertEqual(await self.gateway.handle_inbound(denied), "handled")
        with patch.object(self.gateway, "_run_command", new_callable=AsyncMock):
            self.assertEqual(await self.gateway.handle_inbound(self.msg("/help")), "handled")

    async def test_uncertain_post_never_retries_or_leaks_errors(self):
        for error in (TimeoutError("aes_key=secret"), RuntimeError("status 404 secret")):
            with self.subTest(error=type(error).__name__):
                self.service.post_message.reset_mock()
                self.service.post_message.side_effect = error
                self.assertEqual(await self.gateway.handle_inbound(self.msg()), "unknown")
                self.service.post_message.assert_called_once()
                self.assertEqual(self.gateway.session_mapper.current("private", "7"), "s")
                self.assertNotIn("secret", self.gateway.replies[-1])

    async def test_confirmed_missing_session_recovers_once(self):
        self.service.post_message.side_effect = [RuntimeError("status 404: session not found"), "recovered"]
        self.assertEqual(await self.gateway.handle_inbound(self.msg()), "handled")
        self.assertEqual(self.service.post_message.call_count, 2)
        new_sid = self.gateway.session_mapper.current("private", "7")
        self.assertNotEqual(new_sid, "s")
        self.assertEqual(self.service.post_message.call_args.args[0], new_sid)

    async def test_conservative_platform_disables_missing_session_replay(self):
        self.gateway.can_recreate_session = lambda error: False
        self.service.post_message.side_effect = RuntimeError("status 404: session not found")
        self.assertEqual(await self.gateway.handle_inbound(self.msg()), "unknown")
        self.service.post_message.assert_called_once()
        self.assertEqual(self.gateway.session_mapper.current("private", "7"), "s")

    async def test_uncertain_recovery_does_not_retry_again(self):
        self.service.post_message.side_effect = [RuntimeError("status 404: session not found"), TimeoutError("secret")]
        self.assertEqual(await self.gateway.handle_inbound(self.msg()), "unknown")
        self.assertEqual(self.service.post_message.call_count, 2)
        self.assertNotIn("secret", self.gateway.replies[-1])

    async def test_pre_post_failure_is_retryable(self):
        self.outbound.ensure_baseline.side_effect = TimeoutError("secret")
        self.assertEqual(await self.gateway.handle_inbound(self.msg()), "retryable")
        self.service.post_message.assert_not_called()

    async def test_command_render_hook(self):
        self.gateway.render_command_output = lambda text: "rendered:" + text
        await self.gateway.send_command_output("private", "7", "plain")
        self.assertEqual(self.gateway.replies[-1], "rendered:plain")


class RedactionTests(unittest.TestCase):
    def test_wechat_sensitive_fields_and_key_value_text(self):
        keys = ("aes_key", "aeskey", "encrypt_query_param", "encrypted_query_param",
                "qrcode", "qrcode_img_content", "verify_code", "upload_param")
        for key in keys:
            with self.subTest(key=key):
                self.assertNotIn("privatevalue", str(_payload({key: "privatevalue"})))
                for text in (f'{key}=privatevalue', f'{key}: "privatevalue"', key + "=" + "privatevalue" * 1000):
                    self.assertNotIn("privatevalue", safe_text(text))

    def test_qrcode_auth_paths_hide_whole_request_and_response(self):
        @observed("test.transport")
        def request(path, body):
            return {"unclassified": "private-response"}

        for path in ("/ilink/bot/get_bot_qrcode", "/ilink/bot/get_qrcode_status"):
            with patch("muse_bridge.observability._trace_enabled", True), patch(
                    "muse_bridge.observability.trace_event") as trace:
                result = request(path, {"unclassified": "private-request"})
                self.assertEqual(result, {"unclassified": "private-response"})
                self.assertEqual(trace.call_count, 2)
                self.assertTrue(all(c.kwargs["sensitive"] for c in trace.call_args_list))


if __name__ == "__main__":
    unittest.main()
