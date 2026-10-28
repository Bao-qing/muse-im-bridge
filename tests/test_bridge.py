"""Comprehensive test suite for the new Muse Bridge OO architecture."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from muse_bridge.config.models import (
    AppConfig,
    ConfigLoader,
    MuseConfig,
    OneBotConfig,
    QQConfig,
    QQPushConfig,
)
from muse_bridge.core.commands import CommandError, CommandHandler
from muse_bridge.core.deduplicator import MessageDeduplicator
from muse_bridge.core.message import (
    Attachment,
    InboundMessage,
    MessageSource,
    OutboundMessage,
    chunk_text,
    split_markdown,
)
from muse_bridge.muse.token_manager import MuseTokenManager
from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.adapters.onebot.client import OneBotClient


class TestConfig(unittest.TestCase):
    def test_default_config_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "muse": {"session_id": "sess-123"},
                    "adapters": {
                        "qq": {
                            "app_id": "test_app",
                            "app_secret": "test_secret",
                            "whitelist": {
                                "private": ["PRIV_1", "PRIV_2"],
                                "GRP_1": "*"
                            }
                        }
                    }
                }, f)

            cfg = ConfigLoader.load(cfg_path)
            self.assertEqual(cfg.muse.session_id, "sess-123")
            self.assertEqual(cfg.qq.app_id, "test_app")
            self.assertEqual(cfg.qq.app_secret, "test_secret")
            # Push defaults to first private
            target_type, target_id = cfg.qq.resolve_push_target()
            self.assertEqual(target_type, "private")
            self.assertEqual(target_id, "PRIV_1")

    def test_admin_id_and_push_default_to_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "adapters": {
                        "qq": {
                            "app_id": "app",
                            "admin_id": ["ADMIN_OPENID_1", "ADMIN_OPENID_2"],
                            "whitelist": {"private": ["OTHER_USER"], "GRP_1": "*"},
                            "push": {"enabled": True, "target_id": None}
                        }
                    },
                    "gateway": {
                        "login_through_adapter": "qq"
                    }
                }, f)
            cfg = ConfigLoader.load(cfg_path)
            self.assertEqual(cfg.qq.admin_id, ["ADMIN_OPENID_1", "ADMIN_OPENID_2"])
            self.assertTrue(cfg.qq.is_admin("ADMIN_OPENID_1"))
            self.assertFalse(cfg.qq.is_admin("OTHER_USER"))
            # When push.target_id is not specified, defaults to admin!
            target_type, target_id = cfg.qq.resolve_push_target()
            self.assertEqual(target_type, "private")
            self.assertEqual(target_id, "ADMIN_OPENID_1")
            self.assertEqual(cfg.gateway.login_through_adapter, "qq")
            self.assertTrue(cfg.qq.admin_only)

    def test_admin_only_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "adapters": {
                        "qq": {
                            "app_id": "app",
                            "admin_id": ["ADMIN_1"],
                            "admin_only": False
                        }
                    }
                }, f)
            cfg = ConfigLoader.load(cfg_path)
            self.assertFalse(cfg.qq.admin_only)

    def test_explicit_push_target_override(self):
        qq = QQConfig(
            app_id="app",
            whitelist={"private": ["PRIV_1"], "GRP_1": "*"},
            push=QQPushConfig(target_type="group", target_id="GRP_CUSTOM")
        )
        target_type, target_id = qq.resolve_push_target()
        self.assertEqual(target_type, "group")
        self.assertEqual(target_id, "GRP_CUSTOM")

    def test_constants_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "overrides": {
                        "chunk_chars": 500,
                        "timezone": "UTC",
                        "ws_guid": "CUSTOM_GUID_123",
                        "user_agent": "CustomBot/1.0"
                    }
                }, f)
            cfg = ConfigLoader.load(cfg_path)
            self.assertEqual(cfg.qq.chunk_chars, 500)
            self.assertEqual(cfg.muse.timezone, "UTC")
            self.assertEqual(cfg.qq.ws_guid, "CUSTOM_GUID_123")
            self.assertEqual(cfg.qq.user_agent, "CustomBot/1.0")


class TestMessageAndSplitter(unittest.TestCase):
    def test_split_markdown_fences_preserved(self):
        code = "```python\n" + "\n".join(f"print({i})" for i in range(100)) + "\n```"
        parts = split_markdown(code, limit=200)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertTrue(p.startswith("```python"))
            self.assertTrue(p.rstrip().endswith("```"))

    def test_chunk_text_truncation(self):
        long_text = "\n\n".join(f"Paragraph {i} " + "A" * 200 for i in range(20))
        chunks = chunk_text(long_text, chunk_chars=300, max_chunks=3)
        self.assertEqual(len(chunks), 3)
        self.assertIn("[内容已截断，仅展示前 3 段]", chunks[-1])

    def test_attachment_model(self):
        att = Attachment(type="image", filename="pic.png", data=b"PNGDATA")
        self.assertEqual(att.type, "image")
        self.assertEqual(att.data, b"PNGDATA")


class TestMediaExtraction(unittest.TestCase):
    def test_extract_media_descriptors(self):
        from muse_bridge.adapters.qq.outbound import _extract_media_descriptors, _clean_text
        event = {
            "role": "assistant",
            "message_id": "mid_media",
            "event_name": "message.assistant",
            "display_text": "这是图片：![小猫](sandbox://workspace/imagine_media/cat.webp)\n这是代码：[test.py](sandbox://workspace/user/files/test.py)\n这是视频：[demo.mp4](sandbox://workspace/user/videos/demo.mp4)",
            "payload": {}
        }
        images, files, videos = _extract_media_descriptors(event, event["display_text"])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["workspace_path"], "workspace/imagine_media/cat.webp")
        self.assertEqual(images[0]["filename"], "小猫")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["workspace_path"], "workspace/user/files/test.py")
        self.assertEqual(files[0]["filename"], "test.py")

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["workspace_path"], "workspace/user/videos/demo.mp4")
        self.assertEqual(videos[0]["filename"], "demo.mp4")

        cleaned = _clean_text(event["display_text"])
        self.assertNotIn("sandbox://", cleaned)
        self.assertIn("这是图片：", cleaned)
        self.assertIn("这是代码：", cleaned)
        self.assertIn("这是视频：", cleaned)


class TestDeduplicator(unittest.TestCase):
    def test_three_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "delivered.jsonl")
            dedup = MessageDeduplicator(persistence_file=state_file, processing_ttl_s=300)

            mid = "mid_test_1"
            self.assertFalse(dedup.is_delivered(mid))
            self.assertFalse(dedup.is_processing(mid))

            # Mark processing
            dedup.mark_processing([mid])
            self.assertTrue(dedup.is_processing(mid))
            self.assertFalse(dedup.is_delivered(mid))

            # Rollback processing
            dedup.rollback_processing([mid])
            self.assertFalse(dedup.is_processing(mid))
            self.assertFalse(dedup.is_delivered(mid))

            # Re-mark and deliver
            dedup.mark_processing([mid])
            dedup.mark_delivered([mid])
            self.assertTrue(dedup.is_delivered(mid))
            self.assertFalse(dedup.is_processing(mid))

            # New instance reading from same persistence file
            dedup2 = MessageDeduplicator(persistence_file=state_file)
            self.assertTrue(dedup2.is_delivered(mid))

    def test_processing_expiry(self):
        dedup = MessageDeduplicator(persistence_file=None, processing_ttl_s=10)
        mid = "mid_expire"
        dedup.mark_processing([mid])
        self.assertTrue(dedup.is_processing(mid, now=time.time()))
        # Fast forward past TTL
        self.assertFalse(dedup.is_processing(mid, now=time.time() + 15))

    def test_gateway_duplicate(self):
        dedup = MessageDeduplicator(persistence_file=None, gateway_dedup_s=5)
        self.assertFalse(dedup.is_duplicate_gateway_msg("MSG_1"))
        self.assertTrue(dedup.is_duplicate_gateway_msg("MSG_1"))


class TestCommandHandler(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(CommandHandler.parse_command("/help"), ("help", ""))
        self.assertEqual(CommandHandler.parse_command("/sessions 2"), ("sessions", "2"))
        self.assertEqual(CommandHandler.parse_command("/new"), ("new", ""))
        self.assertEqual(CommandHandler.parse_command("/switch 1"), ("switch", "1"))
        # Gateway commands pass through (return None)
        self.assertIsNone(CommandHandler.parse_command("/status"))
        self.assertIsNone(CommandHandler.parse_command("/clear"))
        self.assertIsNone(CommandHandler.parse_command("normal text"))

    def test_execute_new_and_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "session.json")
            reply, new_id = CommandHandler.execute("new", "", None, "old-id", state_file)
            self.assertIn("已开启新会话", reply)
            self.assertNotEqual(new_id, "old-id")
            # Verify persistence
            loaded = CommandHandler.load_session_id(state_file)
            self.assertEqual(loaded, new_id)

            reply2, cur_id = CommandHandler.execute("current", "", None, new_id, state_file)
            self.assertIn(new_id, reply2)
            self.assertEqual(cur_id, new_id)


class TestQQAdapterLogic(unittest.TestCase):
    def test_whitelist_matching(self):
        adapter = QQAdapter(
            config=QQConfig(
                app_id="123",
                app_secret="sec",
                whitelist={
                    "private": ["ALLOWED_USER"],
                    "GRP_OPEN": "*",
                    "GRP_RESTRICTED": ["MEMBER_1"]
                }
            ),
            muse_service=mock.Mock(),
            deduplicator=mock.Mock(),
        )

        # Private
        self.assertTrue(adapter.is_allowed("C2C_MESSAGE_CREATE", {"author": {"user_openid": "ALLOWED_USER"}}))
        self.assertFalse(adapter.is_allowed("C2C_MESSAGE_CREATE", {"author": {"user_openid": "STRANGER"}}))

        # Open Group
        self.assertTrue(adapter.is_allowed("GROUP_AT_MESSAGE_CREATE", {"group_openid": "GRP_OPEN", "author": {"user_openid": "ANYONE"}}))

        # Restricted Group
        self.assertTrue(adapter.is_allowed("GROUP_AT_MESSAGE_CREATE", {"group_openid": "GRP_RESTRICTED", "author": {"user_openid": "MEMBER_1"}}))
        self.assertFalse(adapter.is_allowed("GROUP_AT_MESSAGE_CREATE", {"group_openid": "GRP_RESTRICTED", "author": {"user_openid": "MEMBER_2"}}))


class TestRouteManager(unittest.TestCase):
    def test_register_and_lookup_and_pop(self):
        from muse_bridge.adapters.qq.route_manager import RouteManager
        rm = RouteManager(ttl_s=100)
        route = rm.register("mid_123", "group", "GRP_1", "msg_qq_1")
        self.assertEqual(route.user_mid, "mid_123")
        found = rm.get("mid_123")
        self.assertIsNotNone(found)
        self.assertEqual(found.target_type, "group")
        self.assertEqual(found.target_id, "GRP_1")
        self.assertEqual(found.msg_id, "msg_qq_1")

        # Test pop consumes the route
        popped = rm.pop("mid_123")
        self.assertEqual(popped.user_mid, "mid_123")
        self.assertIsNone(rm.get("mid_123"))
        self.assertIsNone(rm.pop("mid_123"))

    def test_match_route_multi_part_turn(self):
        from muse_bridge.adapters.qq.route_manager import RouteManager
        rm = RouteManager()
        rm.register("user_turn_1", "group", "GRP_1", "msg_qq_1")

        # 1. First event in turn binds root_exec_id
        r1 = rm.match_route("user_turn_1", "exec_A")
        self.assertIsNotNone(r1)
        self.assertEqual(r1.target_type, "group")

        # 2. Subsequent media event in SAME turn matches!
        r2 = rm.match_route("user_turn_1", None)
        self.assertIsNotNone(r2)
        self.assertEqual(r2.target_type, "group")

        # 3. Subsequent text event in SAME turn matches!
        r3 = rm.match_route("user_turn_1", "exec_A")
        self.assertIsNotNone(r3)
        self.assertEqual(r3.target_type, "group")

        # 4. Delayed reminder with DIFFERENT exec_id -> returns None (routes to private)!
        r4 = rm.match_route("user_turn_1", "exec_B_delayed")
        self.assertIsNone(r4)


class TestGateCatchUp(unittest.TestCase):
    def test_baseline_when_disabled(self):
        from muse_bridge.adapters.qq.outbound import gate_catch_up
        hist = [
            {"role": "assistant", "message_id": "m1", "seq": 1},
            {"role": "assistant", "message_id": "m2", "seq": 2},
        ]
        seen_ids, action, kept, skipped, max_seq, reason = gate_catch_up(
            hist=hist,
            seen_ids=set(),
            last_ts=time.time(),
            now=time.time(),
            enabled=False,
            window_s=3600,
            max_messages=5,
        )
        self.assertEqual(action, "baseline")
        self.assertEqual(kept, 0)
        self.assertEqual(skipped, 2)
        self.assertIn("m1", seen_ids)
        self.assertIn("m2", seen_ids)

    def test_catch_up_within_window(self):
        from muse_bridge.adapters.qq.outbound import gate_catch_up
        hist = [
            {"role": "assistant", "message_id": f"m{i}", "seq": i} for i in range(1, 10)
        ]
        seen_ids, action, kept, skipped, max_seq, reason = gate_catch_up(
            hist=hist,
            seen_ids=set(),
            last_ts=time.time() - 60,
            now=time.time(),
            enabled=True,
            window_s=3600,
            max_messages=3,
        )
        self.assertEqual(action, "catch_up")
        self.assertEqual(kept, 3)
        self.assertEqual(skipped, 6)
        self.assertNotIn("m7", seen_ids)
        self.assertNotIn("m8", seen_ids)
        self.assertNotIn("m9", seen_ids)
        for i in range(1, 7):
            self.assertIn(f"m{i}", seen_ids)


class TestOneBotClient(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_send_group_msg_array_format(self, mock_urlopen):
        mock_resp = mock.Mock()
        mock_resp.read.return_value = json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 999}}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        client = OneBotClient(base_url="http://127.0.0.1:3000", access_token="test_tok")
        res = client.send_group_msg(group_id=10001, text="Hello NapCat!")
        self.assertEqual(res["retcode"], 0)

        # Inspect request payload
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer test_tok")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["group_id"], 10001)
        self.assertEqual(payload["message"], [{"type": "text", "data": {"text": "Hello NapCat!"}}])


if __name__ == "__main__":
    unittest.main()
