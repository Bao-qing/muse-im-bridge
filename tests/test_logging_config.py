"""Logging configuration tests; temporary files and no egress/network access."""

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muse_bridge.config import LoggingConfig
from muse_bridge.config.models import (
    AppConfig, ConfigLoader, GatewayConfig, MuseConfig, OneBotConfig, QQConfig,
)


class TestLoggingConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, original_cwd)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        detect_patch = mock.patch(
            "muse_bridge.config.models._detect_egress_ip", return_value="203.0.113.7",
        )
        self.detect = detect_patch.start()
        self.addCleanup(detect_patch.stop)

    def load(self, data):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return ConfigLoader.load(str(path))

    def test_defaults_and_public_export(self):
        self.assertEqual(dataclasses.asdict(LoggingConfig()), {
            "level": "INFO", "console": True, "file": "logs/bridge.log",
            "trace": False, "trace_file": "logs/trace.jsonl",
            "payload_limit": 65536, "max_bytes": 10485760, "backup_count": 5,
        })
        cfg = self.load({})
        self.assertEqual(cfg.logging, LoggingConfig())
        self.assertFalse(cfg.debug)
        self.assertFalse(cfg.qq.debug)
        self.assertFalse(Path("logs").exists())
        self.detect.assert_called_once_with(cfg.muse.proxy_url, cfg.muse.direct)

    def test_legacy_positional_app_config_construction(self):
        muse, qq, onebot, gateway = MuseConfig(), QQConfig(), OneBotConfig(), GatewayConfig()
        cfg = AppConfig(muse, qq, onebot, gateway, True)
        self.assertIs(cfg.muse, muse)
        self.assertIs(cfg.qq, qq)
        self.assertIs(cfg.onebot, onebot)
        self.assertIs(cfg.gateway, gateway)
        self.assertTrue(cfg.debug)
        self.assertEqual(cfg.logging, LoggingConfig())
        self.assertIsNot(AppConfig().logging, AppConfig().logging)

    def test_all_levels_and_case_normalization(self):
        for level in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"):
            with self.subTest(level=level):
                cfg = self.load({"logging": {"level": " " + level.lower() + " "}})
                self.assertEqual(cfg.logging.level, level)

    def test_top_level_legacy_debug(self):
        cfg = self.load({"debug": True})
        self.assertEqual(cfg.logging.level, "DEBUG")
        self.assertTrue(cfg.debug)
        self.assertFalse(cfg.qq.debug)

    def test_qq_legacy_debug_and_aliases(self):
        for section in ("qq", "qq_official", "adapters"):
            with self.subTest(section=section):
                raw = {"qq": {"debug": True}} if section == "adapters" else {"debug": True}
                cfg = self.load({section: raw})
                self.assertEqual(cfg.logging.level, "DEBUG")
                self.assertTrue(cfg.qq.debug)
                self.assertTrue(cfg.debug)

    def test_environment_legacy_debug(self):
        for value in ("1", "true", "yes", "on", " TRUE "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"QQ_DEBUG": value}):
                cfg = self.load({})
                self.assertEqual(cfg.logging.level, "DEBUG")
                self.assertTrue(cfg.qq.debug)
                self.assertTrue(cfg.debug)
        for value in ("", "0", "false", "off"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"QQ_DEBUG": value}):
                self.assertEqual(self.load({}).logging.level, "INFO")

    def test_explicit_level_wins_without_removing_legacy_fields(self):
        for level in ("INFO", "WARNING", "OFF"):
            with self.subTest(level=level), mock.patch.dict(os.environ, {"QQ_DEBUG": "1"}):
                cfg = self.load({"debug": True, "logging": {"level": level}})
                self.assertEqual(cfg.logging.level, level)
                self.assertTrue(cfg.debug)
                self.assertTrue(cfg.qq.debug)

    def test_partial_logging_keeps_legacy_fallback(self):
        cfg = self.load({"debug": True, "logging": {"console": False, "file": None}})
        self.assertEqual(cfg.logging.level, "DEBUG")
        self.assertFalse(cfg.logging.console)
        self.assertIsNone(cfg.logging.file)

    def test_full_logging_values(self):
        values = {
            "level": "TRACE", "console": False, "file": "runtime.log",
            "trace": True, "trace_file": "trace.jsonl",
            "payload_limit": 0, "max_bytes": 1, "backup_count": 1,
        }
        self.assertEqual(dataclasses.asdict(self.load({"logging": values}).logging), values)
        self.assertFalse(Path("runtime.log").exists())
        self.assertFalse(Path("trace.jsonl").exists())

    def test_empty_or_null_disabled_paths(self):
        for value in (None, ""):
            with self.subTest(value=value):
                cfg = self.load({"logging": {"file": value, "trace_file": value}})
                self.assertEqual(cfg.logging.file, value)
                self.assertEqual(cfg.logging.trace_file, value)
                traced = self.load({"logging": {"file": value, "trace": True}})
                self.assertTrue(traced.logging.trace)

    def test_invalid_logging_container(self):
        for value in (None, [], True, 1, "DEBUG"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "logging must be an object"):
                self.load({"logging": value})

    def test_invalid_levels(self):
        for value in (None, True, 10, [], {}, "", "WARN", "VERBOSE"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "logging.level"):
                self.load({"logging": {"level": value}})

    def test_strict_booleans(self):
        for name in ("console", "trace"):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "logging." + name):
                    self.load({"logging": {name: value}})

    def test_strict_integer_limits(self):
        for name, invalid in (
            ("payload_limit", (-1, True, False, 1.0, "65536", None)),
            ("max_bytes", (0, -1, True, False, 1.0, "10", None)),
            ("backup_count", (0, -1, True, False, 1.0, "5", None)),
        ):
            for value in invalid:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "logging." + name):
                    self.load({"logging": {name: value}})

    def test_invalid_paths(self):
        for name in ("file", "trace_file"):
            for value in (True, 12, [], {}, "   ", "bad\x00path"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "logging." + name):
                    self.load({"logging": {name: value}})

    def test_trace_requires_nonempty_separate_path(self):
        for value in (None, ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "logging.trace_file"):
                self.load({"logging": {"trace": True, "trace_file": value}})
        for value in ("logs/bridge.log", "logs/../logs/bridge.log", str(Path("logs/bridge.log").resolve())):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must differ"):
                self.load({"logging": {"trace": True, "trace_file": value}})
        self.assertFalse(self.load({"logging": {"trace_file": "logs/bridge.log"}}).logging.trace)

    def test_unknown_field_fails_without_echoing_values(self):
        with self.assertRaisesRegex(ValueError, "unknown configuration fields") as error:
            self.load({"logging": {"unexpected": "synthetic-value-not-for-output"}})
        self.assertNotIn("synthetic-value-not-for-output", str(error.exception))

    def test_const_base_merging_and_level_precedence(self):
        Path("const.json").write_text(json.dumps({
            "debug": True, "logging": {"level": "WARNING", "console": False},
        }), encoding="utf-8")
        cfg = self.load({"logging": {"file": ""}})
        self.assertEqual(cfg.logging.level, "WARNING")
        self.assertFalse(cfg.logging.console)
        self.assertEqual(cfg.logging.file, "")
        self.assertEqual(self.load({"logging": {"level": "ERROR"}}).logging.level, "ERROR")

    def test_other_business_config_unchanged_and_source_not_written(self):
        data = {
            "muse": {"session_id": "example-session", "vm_id": "example-vm",
                     "client_ip": "198.51.100.9", "proxy_url": "http://localhost:9999",
                     "direct": False, "approval": {"mode": "deny_all", "notify": False}},
            "adapters": {
                "qq": {"enabled": False, "admin_id": ["example-admin"],
                       "credentials_in_place": True, "session_mode": "per_chat",
                       "session_bindings": {"group:example": "example-session"},
                       "access_requests": {"auto_approve": False},
                       "whitelist": {"private": ["example-admin"]},
                       "push": {"enabled": False, "catch_up_enabled": True}},
                "onebot": {"enabled": True, "base_url": "http://localhost:3000", "target_group_id": 123},
            },
            "gateway": {"login_through_adapter": "qq"},
            "overrides": {"chunk_chars": 777, "timezone": "UTC"},
        }
        baseline = self.load(data)
        data["logging"] = {"level": "OFF", "file": None, "console": False}
        cfg = self.load(data)
        for name in ("muse", "qq", "onebot", "gateway", "debug"):
            self.assertEqual(getattr(cfg, name), getattr(baseline, name))
        self.assertEqual(json.loads(Path("config.json").read_text(encoding="utf-8")), data)
        self.assertEqual(cfg.qq.chunk_chars, 777)
        self.assertEqual(cfg.muse.timezone, "UTC")
        self.detect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
