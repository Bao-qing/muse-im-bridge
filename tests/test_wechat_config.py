"""Strict personal-chat config and account isolation, entirely offline."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from muse_bridge.config import ChatConfig, ConversationConfig, WeChatConfig
from muse_bridge.config.models import AppConfig, ConfigLoader, OneBotConfig, QQConfig


REMOVED_POLICY = {"admin_id", "admin_only", "whitelist", "allow_all_private",
                  "allow_all_group", "allow_all_groups", "access_requests",
                  "unbound_push_to_admin", "group_sender_info"}


class TestWeChatConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        patch = mock.patch("muse_bridge.config.models._detect_egress_ip", return_value="")
        self.detect = patch.start()
        self.addCleanup(patch.stop)

    def load(self, settings=None, **top):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({"adapters": {"wechat": settings or {}}, **top}), encoding="utf-8")
        return ConfigLoader.load_wechat(str(path))

    def test_defaults_have_no_platform_policy(self):
        cfg = WeChatConfig()
        self.assertIsInstance(cfg, ConversationConfig)
        self.assertNotIsInstance(cfg, ChatConfig)
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.typing_enabled)
        self.assertEqual(cfg.state_dir, "data/wechat")
        self.assertEqual(cfg.channel_version, "2.4.6")
        self.assertEqual(cfg.chunk_chars, 4000)
        self.assertEqual(cfg.polling_timeout_ms, 35000)
        self.assertEqual(cfg.http_timeout_s, 35)
        self.assertEqual(cfg.reconnect_max_retries, 30)
        self.assertEqual(AppConfig().wechat, cfg)
        self.assertFalse(REMOVED_POLICY & dataclasses.asdict(cfg).keys())
        for name in REMOVED_POLICY | {"is_admin", "admin_target"}:
            with self.subTest(name=name):
                self.assertFalse(hasattr(cfg, name))

    def test_legacy_channels_keep_access_policy(self):
        for cls in (QQConfig, OneBotConfig):
            with self.subTest(channel=cls.__name__):
                cfg = cls(admin_id=["admin"], whitelist={"private": ["member"]})
                self.assertIsInstance(cfg, ConversationConfig)
                self.assertIsInstance(cfg, ChatConfig)
                self.assertTrue(cfg.admin_only)
                self.assertTrue(cfg.is_admin("admin"))
                self.assertFalse(cfg.is_admin("member"))
                self.assertEqual(cfg.admin_target(), "admin")
                self.assertTrue(cfg.access_requests.enabled)

    def test_offline_loader_ignores_unrelated_muse_and_logging(self):
        cfg = self.load({"enabled": True}, muse={"approval": "bad"}, logging="bad")
        self.assertTrue(cfg.enabled)
        self.detect.assert_not_called()

    def test_full_loader_wires_wechat_without_policy(self):
        self.load({"enabled": True, "admin_id": ["obsolete"]})
        cfg = ConfigLoader.load(str(Path(self.tmp.name) / "config.json"))
        self.assertTrue(cfg.wechat.enabled)
        self.assertFalse(REMOVED_POLICY & dataclasses.asdict(cfg.wechat).keys())
        self.assertTrue(cfg.qq.admin_only)

    def test_const_merge_and_overrides_are_consistent(self):
        Path("const.json").write_text(json.dumps({"adapters": {"wechat": {
            "channel_version": "base", "chunk_chars": 1200, "admin_only": True}}}), encoding="utf-8")
        cfg = self.load({"channel_version": "user"}, overrides={"chunk_chars": 3000, "admin_only": True})
        self.assertEqual(cfg.channel_version, "user")
        self.assertEqual(cfg.chunk_chars, 3000)
        self.assertFalse(hasattr(cfg, "admin_only"))
        self.detect.assert_not_called()
        self.assertEqual(ConfigLoader.load(str(Path(self.tmp.name) / "config.json")).wechat, cfg)

    def test_old_serialized_policy_is_explicitly_discarded(self):
        old = {"enabled": True, "admin_id": ["old-admin"], "admin_only": True,
               "whitelist": {"private": ["old-member"], "group": "*"},
               "allow_all_private": False, "allow_all_group": True, "allow_all_groups": True,
               "access_requests": {"auto_approve": True, "state_file": "do-not-read-access.json"},
               "unbound_push_to_admin": True, "group_sender_info": True}
        cfg = self.load(old)
        self.assertTrue(cfg.enabled)
        self.assertFalse(REMOVED_POLICY & dataclasses.asdict(cfg).keys())
        # Removed policy has no validation or runtime meaning, even when malformed.
        for name in REMOVED_POLICY:
            with self.subTest(name=name):
                cfg = self.load({name: [None, "obsolete"]})
                self.assertFalse(hasattr(cfg, name))
        self.assertFalse(list(Path(self.tmp.name).rglob("*access*")))

    def test_other_unknown_fields_still_fail(self):
        for name in ("admin_ids", "allow_all_friends", "unknown"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load({name: True})

    def test_strict_boolean_fields(self):
        fields = [f.name for f in dataclasses.fields(WeChatConfig) if type(getattr(WeChatConfig(), f.name)) is bool]
        for name in fields:
            for bad in (0, 1, "false", None):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    self.load({name: bad})

    def test_all_integer_limits_are_strict_positive(self):
        defaults = WeChatConfig()
        fields = [f.name for f in dataclasses.fields(defaults)
                  if type(getattr(defaults, f.name)) is int and f.name != "http_timeout_s"]
        for name in fields:
            for bad in (True, False, "1", 1.5, float("nan"), float("inf"), 0, -1, None):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    self.load({name: bad})
        self.assertEqual(self.load({"chunk_chars": 1}).chunk_chars, 1)
        with self.assertRaises(ValueError):
            self.load({"chunk_chars": 4001})
        for bad in (True, "35", float("inf"), float("nan"), 0, -1, None, 10**400):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                self.load({"http_timeout_s": bad})
        self.assertEqual(self.load({"http_timeout_s": 1.5}).http_timeout_s, 1.5)

    def test_overrides_never_coerce_invalid_values(self):
        for name, value in (("credentials_in_place", "false"), ("chunk_chars", True), ("max_chunks", "8"),
                            ("http_timeout_s", float("inf")), ("catch_up_enabled", 1)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(overrides={name: value})

    def test_legacy_configs_without_wechat_keep_old_override_semantics(self):
        path = Path(self.tmp.name) / "legacy.json"
        path.write_text(json.dumps({"overrides": {"chunk_chars": 8000}}), encoding="utf-8")
        cfg = ConfigLoader.load(str(path))
        self.assertEqual(cfg.qq.chunk_chars, 8000)
        self.assertEqual(cfg.wechat.chunk_chars, 4000)
        self.assertFalse(cfg.wechat.enabled)

    def test_only_private_session_bindings(self):
        for bindings in ({"group:123": "session"}, {"private:": "x"}, {"private:user": " "}, []):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                self.load({"session_bindings": bindings})

    def test_account_paths_without_access_files(self):
        original = WeChatConfig(state_dir=str(Path(self.tmp.name) / "wx"),
                                session_bindings={"private:user": "session"}, routes_file="outside/routes.json")
        before = dataclasses.asdict(original)
        first, second = original.for_account("bot/a"), original.for_account("bot/b")
        expected = Path(original.state_dir) / "accounts" / hashlib.sha256(b"bot/a").hexdigest()[:24]
        for key in ("delivered_file", "session_map_file", "push_state_file", "routes_file"):
            self.assertEqual(Path(getattr(first, key)).parent, expected)
            self.assertNotEqual(getattr(first, key), getattr(second, key))
        self.assertFalse(REMOVED_POLICY & dataclasses.asdict(first).keys())
        self.assertEqual(first.session_bindings, {"private:user": "session"})
        first.session_bindings["private:new"] = "new-session"
        self.assertEqual(dataclasses.asdict(original), before)
        self.assertFalse(expected.exists())
        for bad in ("", " ", None):
            with self.assertRaises(ValueError):
                original.for_account(bad)


if __name__ == "__main__":
    unittest.main()
