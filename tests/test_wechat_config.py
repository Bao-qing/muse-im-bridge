"""Strict WeChat config and account isolation, entirely offline."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from muse_bridge.config import WeChatConfig
from muse_bridge.config.models import AppConfig, ConfigLoader


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

    def test_defaults(self):
        cfg = WeChatConfig()
        self.assertFalse(cfg.enabled)
        for name in ("admin_only", "allow_all_private", "allow_all_group", "unbound_push_to_admin", "typing_enabled"):
            self.assertFalse(getattr(cfg, name))
        self.assertEqual(cfg.state_dir, "data/wechat")
        self.assertEqual(cfg.channel_version, "2.4.6")
        self.assertEqual(cfg.chunk_chars, 4000)
        self.assertEqual(cfg.polling_timeout_ms, 35000)
        self.assertEqual(cfg.http_timeout_s, 35)
        self.assertEqual(AppConfig().wechat, cfg)
        self.assertFalse(cfg.is_admin("scanner"))

    def test_offline_loader_ignores_unrelated_muse_and_logging(self):
        cfg = self.load({"enabled": True}, muse={"approval": "bad"}, logging="bad")
        self.assertTrue(cfg.enabled)
        self.detect.assert_not_called()

    def test_full_loader_wires_wechat(self):
        self.load({"enabled": True, "admin_id": ["explicit"]})
        cfg = ConfigLoader.load(str(Path(self.tmp.name) / "config.json"))
        self.assertTrue(cfg.wechat.enabled)
        self.assertTrue(cfg.wechat.is_admin("explicit"))
        self.assertFalse(cfg.wechat.is_admin("scanner"))
        self.assertEqual(cfg.qq.admin_only, True)

    def test_const_merge_and_overrides_are_consistent(self):
        Path("const.json").write_text(json.dumps({"adapters": {"wechat": {
            "channel_version": "base", "chunk_chars": 1200, "admin_only": True}}}), encoding="utf-8")
        cfg = self.load({"channel_version": "user"}, overrides={"chunk_chars": 3000})
        self.assertEqual(cfg.channel_version, "user")
        self.assertEqual(cfg.chunk_chars, 3000)
        self.assertTrue(cfg.admin_only)
        self.detect.assert_not_called()
        self.assertEqual(ConfigLoader.load(str(Path(self.tmp.name) / "config.json")).wechat, cfg)

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
        for bad in (True, "35", float("inf"), float("nan"), 0, -1, None):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                self.load({"http_timeout_s": bad})
        self.assertEqual(self.load({"http_timeout_s": 1.5}).http_timeout_s, 1.5)

    def test_overrides_never_coerce_invalid_values(self):
        for name, value in (("admin_only", "false"), ("chunk_chars", True), ("max_chunks", "8"),
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

    def test_groups_rejected(self):
        for raw in ({"allow_all_group": True}, {"allow_all_groups": True},
                    {"whitelist": {"groups": []}}, {"whitelist": {"123": "*"}},
                    {"session_bindings": {"group:123": "session"}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.load(raw)

    def test_account_paths_and_explicit_presets(self):
        original = WeChatConfig(state_dir=str(Path(self.tmp.name) / "wx"), admin_id=["explicit"],
                                session_bindings={"private:user": "session"}, routes_file="outside/routes.json")
        before = dataclasses.asdict(original)
        first, second = original.for_account("bot/a"), original.for_account("bot/b")
        expected = Path(original.state_dir) / "accounts" / hashlib.sha256(b"bot/a").hexdigest()[:24]
        for key in ("delivered_file", "session_map_file", "push_state_file", "routes_file"):
            self.assertEqual(Path(getattr(first, key)).parent, expected)
            self.assertNotEqual(getattr(first, key), getattr(second, key))
        self.assertEqual(Path(first.access_requests.state_file).parent, expected)
        self.assertEqual(first.admin_id, ["explicit"])
        self.assertEqual(first.session_bindings, {"private:user": "session"})
        first.admin_id.append("new")
        first.access_requests.enabled = False
        self.assertEqual(dataclasses.asdict(original), before)
        self.assertFalse(expected.exists())
        for bad in ("", " ", None):
            with self.assertRaises(ValueError):
                original.for_account(bad)

    def test_nested_access_and_ids_validation(self):
        for raw in ({"access_requests": []}, {"access_requests": {"auto_approve": "true"}},
                    {"whitelist": {"private": [True]}}, {"admin_id": [False]},
                    {"session_bindings": {"private:": "x"}}, {"typing_enabled": "false"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.load(raw)
        self.assertEqual(self.load({"admin_id": [123]}).admin_id, ["123"])


if __name__ == "__main__":
    unittest.main()
