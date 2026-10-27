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
                        "timezone": "UTC"
                    }
                }, f)
            cfg = ConfigLoader.load(cfg_path)
            self.assertEqual(cfg.qq.chunk_chars, 500)
            self.assertEqual(cfg.muse.timezone, "UTC")


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


class TestOneBotClient(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_send_group_msg_array_format(self, mock_urlopen):
        mock_resp = mock.Mock()
        mock_resp.read.return_value = json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 999}}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        client = OneBotClient(base_url="https://bot.baoqing.eu.org:81", access_token="test_tok")
        res = client.send_group_msg(group_id=836401578, text="Hello NapCat!")
        self.assertEqual(res["retcode"], 0)

        # Inspect request payload
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer test_tok")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["group_id"], 836401578)
        self.assertEqual(payload["message"], [{"type": "text", "data": {"text": "Hello NapCat!"}}])


if __name__ == "__main__":
    unittest.main()
