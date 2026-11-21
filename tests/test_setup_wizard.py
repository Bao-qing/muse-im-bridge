"""Tests for first-run setup wizard (manage.py)."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from manage import FirstRunWizard, _default_ws_url


QZONE_DEFAULTS = {
    "enabled": False,
    "poll_interval_s": 300,
    "max_age_s": 3600,
    "scan_limit": 50,
    "max_per_scan": 3,
    "min_publish_interval_s": 60,
    "publish_window_s": 3600,
    "max_per_window": 5,
    "ugc_right": 4,
    "state_file": "data/onebot_qzone_feed.local.json",
}


class TestFirstRunWizard(unittest.TestCase):
    def setUp(self):
        # Config generation must not write session maps into the shared checkout.
        patcher = mock.patch.object(FirstRunWizard, "_save_session_map")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_ws_url(self):
        self.assertEqual(_default_ws_url("http://127.0.0.1:3000"), "ws://127.0.0.1:3000")
        self.assertEqual(_default_ws_url("https://onebot.example.com"), "wss://onebot.example.com")
        self.assertEqual(_default_ws_url("ws://127.0.0.1:3001"), "ws://127.0.0.1:3001")
        self.assertEqual(_default_ws_url("wss://onebot.example.com"), "wss://onebot.example.com")

    def test_generate_minimal_config_qq_only_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            wizard = FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["qq"]
            wizard.channel_configs["qq"] = {
                "app_id": "test_appid",
                "app_secret": "test_secret",
            }
            # Simulate first message captured from QQ private chat
            wizard.first_message_event = ("qq", "USER_OPENID_123", "private", "USER_OPENID_123", "msg_001")
            wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Check structure
            self.assertNotIn("login_through_adapter", data["gateway"])
            self.assertIn("qq", data["adapters"])
            self.assertNotIn("onebot", data["adapters"])  # Minimal config: omitted if not selected!

            qq = data["adapters"]["qq"]
            self.assertTrue(qq["enabled"])
            self.assertEqual(qq["app_id"], "test_appid")
            self.assertEqual(qq["app_secret"], "test_secret")
            self.assertEqual(qq["admin_id"], ["USER_OPENID_123"])
            self.assertEqual(qq["whitelist"]["private"], ["USER_OPENID_123"])
            self.assertTrue(qq["admin_only"])
            self.assertTrue(qq["credentials_in_place"])

            # Verify main_session_id generated and private chat bound to it
            main_sid = data["muse"]["session_id"]
            self.assertTrue(bool(main_sid))
            self.assertEqual(qq.get("session_bindings", {}).get("private:USER_OPENID_123"), main_sid)

    def test_generate_minimal_config_qq_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            wizard = FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["qq"]
            wizard.channel_configs["qq"] = {
                "app_id": "test_appid",
                "app_secret": "test_secret",
            }
            # Simulate first message captured from QQ group
            wizard.first_message_event = ("qq", "MEMBER_OPENID_456", "group", "GROUP_OPENID_789", "msg_002")
            wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            qq = data["adapters"]["qq"]
            self.assertEqual(qq["admin_id"], ["MEMBER_OPENID_456"])
            self.assertEqual(qq["whitelist"]["private"], ["MEMBER_OPENID_456"])
            self.assertEqual(qq["whitelist"]["GROUP_OPENID_789"], "*")

    def test_generate_minimal_config_onebot_only_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            wizard = FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["onebot"]
            wizard.channel_configs["onebot"] = {
                "base_url": "http://127.0.0.1:3000",
                "access_token": "my_token",
                "ws_url": "ws://127.0.0.1:3000",
            }
            # Simulate first message captured from OneBot group
            wizard.first_message_event = ("onebot", "10001", "group", "20002", "msg_003")
            wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertNotIn("login_through_adapter", data["gateway"])
            self.assertIn("onebot", data["adapters"])
            # QQ defaults to enabled=True when omitted; wizard must explicitly disable it.
            self.assertEqual(data["adapters"]["qq"], {"enabled": False})

            ob = data["adapters"]["onebot"]
            self.assertTrue(ob["enabled"])
            self.assertEqual(ob["qzone_feed"], QZONE_DEFAULTS)
            self.assertEqual(ob["base_url"], "http://127.0.0.1:3000")
            self.assertEqual(ob["access_token"], "my_token")
            self.assertEqual(ob["admin_id"], ["10001"])
            self.assertEqual(ob["target_group_id"], 20002)
            self.assertEqual(ob["whitelist"]["private"], ["10001"])
            self.assertEqual(ob["whitelist"]["groups"], [20002])

    def test_generate_dual_channel_config_qq_first_no_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            wizard = FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["qq", "onebot"]
            wizard.channel_configs["qq"] = {
                "app_id": "test_appid",
                "app_secret": "test_secret",
            }
            wizard.channel_configs["onebot"] = {
                "base_url": "http://127.0.0.1:3000",
                "access_token": "",
                "ws_url": "ws://127.0.0.1:3000",
            }
            wizard.first_message_event = ("qq", "QQ_ADMIN_OPENID", "private", "QQ_ADMIN_OPENID", "msg_004")
            wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertNotIn("login_through_adapter", data["gateway"])
            self.assertIn("qq", data["adapters"])
            self.assertIn("onebot", data["adapters"])

            qq = data["adapters"]["qq"]
            self.assertEqual(qq["admin_id"], ["QQ_ADMIN_OPENID"])

            ob = data["adapters"]["onebot"]
            self.assertTrue(ob["enabled"])
            self.assertEqual(ob["qzone_feed"], QZONE_DEFAULTS)
            self.assertEqual(ob["admin_id"], [])
            self.assertNotIn("target_group_id", ob)
            self.assertEqual(ob["whitelist"], {"private": [], "groups": []})

    def test_listen_and_capture_qq_message(self):
        """Test simulated gateway startup and message capture."""
        wizard = FirstRunWizard(output_file="dummy.json")
        wizard.selected_channels = ["qq"]
        wizard.channel_configs["qq"] = {
            "app_id": "10000000",
            "app_secret": "secret",
        }

        mock_qq_client = mock.MagicMock()
        mock_qq_client.get_access_token.return_value = "token_123"
        mock_qq_client.send_message.return_value = (200, {})

        captured_callback = {}

        class MockQQGateway:
            def __init__(self, client, on_event, user_agent=None):
                self.client = client
                self.on_event = on_event
                captured_callback["on_event"] = on_event

            async def start(self):
                # Simulate receiving a C2C message event after start
                await asyncio.sleep(0.01)
                await captured_callback["on_event"]("C2C_MESSAGE_CREATE", {
                    "id": "event_id_999",
                    "content": "hello",
                    "author": {"user_openid": "TEST_OPENID_888"}
                })

            async def stop(self):
                pass

        with mock.patch("muse_bridge.adapters.qq.client.QQClient", return_value=mock_qq_client), \
             mock.patch("muse_bridge.adapters.qq.gateway.QQGateway", side_effect=MockQQGateway):
            asyncio.run(wizard._listen_for_first_message())

        self.assertIsNotNone(wizard.first_message_event)
        ch, uid, ctype, tid, mid = wizard.first_message_event
        self.assertEqual(ch, "qq")
        self.assertEqual(uid, "TEST_OPENID_888")
        self.assertEqual(ctype, "private")
        self.assertEqual(tid, "TEST_OPENID_888")
        self.assertEqual(mid, "event_id_999")
        mock_qq_client.send_message.assert_called_once()


class TestFullConfigEditor(unittest.TestCase):
    def test_editor_modifies_muse_category(self):
        cfg = {"muse": {"session_id": "old_sid", "cookies_file": "old.json"}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Mock user input to change session_id (option 1) then exit category (option 0)
        with mock.patch("manage._prompt_input", side_effect=["1", "new_custom_sid", "0"]):
            editor._edit_muse_category()

        self.assertEqual(cfg["muse"]["session_id"], "new_custom_sid")
        self.assertTrue(editor._modified)

    def test_editor_modifies_gateway_category(self):
        cfg = {"gateway": {"background_approval_push": ""}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        with mock.patch("manage._prompt_input", side_effect=["1", "onebot:12345", "0"]):
            editor._edit_gateway_category()

        self.assertEqual(cfg["gateway"]["background_approval_push"], "onebot:12345")

    def test_editor_modifies_qq_category(self):
        cfg = {"adapters": {"qq": {"enabled": False, "app_id": "111"}}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Option 1: enabled -> True, Option 2: app_id -> 222, Option 0: exit
        with mock.patch("manage._prompt_input", side_effect=["1", "y", "2", "222", "0"]):
            editor._edit_qq_category()

        self.assertTrue(cfg["adapters"]["qq"]["enabled"])
        self.assertEqual(cfg["adapters"]["qq"]["app_id"], "222")

    def test_editor_modifies_onebot_category(self):
        cfg = {"adapters": {"onebot": {"enabled": True, "base_url": "http://127.0.0.1:3000"}}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Option 5: group_trigger -> at, Option 0: exit
        with mock.patch("manage._prompt_input", side_effect=["5", "at", "0"]):
            editor._edit_onebot_category()

        self.assertEqual(cfg["adapters"]["onebot"]["group_trigger"], "at")

    def test_editor_qzone_defaults_stay_disabled(self):
        from manage import FullConfigEditor
        editor = FullConfigEditor({})
        # Enter item 15, accept every default, then return to the parent.
        with mock.patch("builtins.input", side_effect=["15"] + [""] * 10 + ["0"]):
            editor._edit_onebot_category()
        self.assertEqual(editor.data["adapters"]["onebot"]["qzone_feed"], QZONE_DEFAULTS)
        self.assertTrue(editor._modified)

    def test_editor_qzone_edits_validate_and_preserve_other_settings(self):
        from manage import FullConfigEditor
        original = {**QZONE_DEFAULTS, "poll_interval_s": 900}
        ob = {"base_url": "http://localhost:3000", "qzone_feed": original}
        editor = FullConfigEditor({"adapters": {"onebot": ob}})
        with mock.patch("manage._prompt_input", side_effect=[
            "15", "y", "bad", "0", "-1", "600", "7200", "100", "7",
            "120", "1800", "9", "2", "64", "data/custom.json", "0",
        ]):
            editor._edit_onebot_category()
        self.assertEqual(ob["qzone_feed"], {
            "enabled": True, "poll_interval_s": 600, "max_age_s": 7200,
            "scan_limit": 100, "max_per_scan": 7, "min_publish_interval_s": 120,
            "publish_window_s": 1800, "max_per_window": 9, "ugc_right": 64,
            "state_file": "data/custom.json",
        })
        self.assertEqual(ob["base_url"], "http://localhost:3000")

    def test_editor_qzone_bounds_and_visibility_labels(self):
        from manage import FullConfigEditor
        editor = FullConfigEditor({})
        with mock.patch("manage._prompt_input", side_effect=[
            "15", "n", "300", "3600", "101", "2", "3", "2", "60", "3600", "5", "64", "data/custom.json", "0",
        ]) as prompted, mock.patch("builtins.print"):
            editor._edit_onebot_category()
        feed = editor.data["adapters"]["onebot"]["qzone_feed"]
        self.assertEqual(feed["scan_limit"], 2)
        self.assertEqual(feed["max_per_scan"], 2)
        self.assertEqual(feed["ugc_right"], 64)
        labels = [call.args[0] for call in prompted.call_args_list if "可见范围" in call.args[0]]
        self.assertEqual(len(labels), 1)
        self.assertIn("4 好友可见，64 仅自己可见", labels[0])
        from muse_bridge.config.models import QzoneFeedConfig
        QzoneFeedConfig(**feed)

    def test_editor_modifies_logging_and_overrides(self):
        cfg = {"logging": {"level": "INFO"}, "overrides": {"chunk_chars": 1200}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        with mock.patch("manage._prompt_input", side_effect=["1", "DEBUG", "0"]):
            editor._edit_logging_category()
        self.assertEqual(cfg["logging"]["level"], "DEBUG")

        with mock.patch("manage._prompt_input", side_effect=["1", "1500", "0"]):
            editor._edit_overrides_category()
        self.assertEqual(cfg["overrides"]["chunk_chars"], 1500)

    def test_editor_save_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test_config.json")
            from manage import FullConfigEditor
            editor = FullConfigEditor({"muse": {"session_id": "test"}}, file_path=p)
            editor.save()

            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["muse"]["session_id"], "test")

    def test_editor_modifies_telegram_category(self):
        cfg = {"adapters": {}}
        from manage import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")
        with mock.patch("manage._prompt_input", side_effect=[
            "1", "y",
            "2", "123456:AAH_valid_token",
            "3", "http",
            "4", "all",
            "13", "y",
            "0",
        ]), mock.patch("builtins.print"):
            editor._edit_telegram_category()
        tg = cfg["adapters"]["telegram"]
        self.assertTrue(tg["enabled"])
        self.assertEqual(tg["bot_token"], "123456:AAH_valid_token")
        self.assertEqual(tg["proxy"], "http")
        self.assertEqual(tg["group_trigger"], "all")
        self.assertTrue(tg["reaction_quote"])


class TestManageConsoleAndWizardV2(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(FirstRunWizard, "_save_session_map")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normalize_http_url_and_proxy_validation(self):
        import manage
        self.assertEqual(manage._normalize_http_url("3000"), "http://127.0.0.1:3000")
        self.assertEqual(manage._normalize_http_url("127.0.0.1:3001"), "http://127.0.0.1:3001")
        self.assertEqual(manage._normalize_http_url("https://napcat.example"), "https://napcat.example")
        self.assertEqual(manage._validate_proxy_url("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertIsNone(manage._validate_proxy_url("clear"))
        self.assertIsNone(manage._validate_proxy_url(""))

    def test_qq_instant_probe_retry_and_keep(self):
        import manage
        with mock.patch("manage._prompt_input", side_effect=[
            "bad_id", "bad_sec", "", "1",  # fail -> retry
            "good_id", "good_sec", "",     # succeed
        ]), mock.patch("manage._probe_qq", side_effect=[(False, "invalid"), (True, "ok")]), \
             mock.patch("builtins.print"):
            status, cfg, note = manage._configure_qq_interactive()
        self.assertEqual(status, "ready")
        self.assertEqual(cfg, {"app_id": "good_id", "app_secret": "good_sec"})
        self.assertIn("good_id", note)

    def test_telegram_guided_config_syncs_proxy_to_muse(self):
        import manage
        wizard = manage.FirstRunWizard()
        with mock.patch("manage._prompt_input", side_effect=[
            "123456:AAH_valid_token",
            "http",
            "http://127.0.0.1:7890",
        ]), mock.patch("manage._probe_telegram", return_value=(True, "ok", {"bot_id": "123456", "username": "my_bot"})), \
             mock.patch("builtins.print"):
            wizard._configure_telegram()
        self.assertEqual(wizard.selected_channels, ["telegram"])
        self.assertEqual(wizard.channel_configs["telegram"]["proxy"], "http")
        self.assertEqual(wizard.network_data["proxy_url"], "http://127.0.0.1:7890")

    def test_wechat_guided_basic_settings_and_defer(self):
        import manage
        with mock.patch("manage._prompt_input", side_effect=[
            "2", "y", "n",  # guided basic settings: typing=True, catch_up=False
            "0",            # finish and return
        ]), mock.patch("manage._wechat_local_status_text", return_value="本地未登录"), \
             mock.patch("builtins.print"):
            status, cfg, note = manage._configure_wechat_guided()
        self.assertEqual(status, "ready")
        self.assertEqual(cfg, {"enabled": True, "typing_enabled": True, "catch_up_enabled": False})
        self.assertIn("已启用", note)

    def test_telegram_only_disables_qq_and_saves_claimed_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            import manage
            wizard = manage.FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["telegram"]
            wizard.muse_proxy = "http"
            wizard.network_data["proxy_url"] = "http://127.0.0.1:7890"
            wizard.channel_configs["telegram"] = {
                "bot_token": "99999:AAH_token_xyz",
                "proxy": "http",
            }
            wizard.claimed_events["telegram"] = ("424242", "group", "-100123", "77")
            wizard.first_message_event = ("telegram", "424242", "group", "-100123", "77")
            wizard.group_settings["telegram"] = {"group_trigger": "at", "bind_main_to_group": True}
            with mock.patch("builtins.print"):
                wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(data["adapters"]["qq"], {"enabled": False})
            tg = data["adapters"]["telegram"]
            self.assertTrue(tg["enabled"])
            self.assertEqual(tg["admin_id"], ["424242"])
            self.assertEqual(tg["whitelist"]["private"], ["424242"])
            self.assertEqual(tg["whitelist"]["-100123"], "*")
            self.assertEqual(tg["group_trigger"], "at")
            self.assertEqual(tg["proxy"], "http")
            self.assertEqual(data["network"]["proxy_url"], "http://127.0.0.1:7890")
            self.assertEqual(tg["session_bindings"]["group:-100123"], data["muse"]["session_id"])

            # Generated config must load cleanly via ConfigLoader without errors
            from muse_bridge.config.models import ConfigLoader
            loaded = ConfigLoader.load(out_file)
            self.assertFalse(loaded.qq.enabled)
            self.assertTrue(loaded.telegram.enabled)

    def test_multi_channel_claiming_and_manual_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            import manage
            wizard = manage.FirstRunWizard(output_file=out_file)
            wizard.selected_channels = ["onebot", "telegram"]
            wizard.channel_configs["onebot"] = {
                "base_url": "http://127.0.0.1:3000",
                "access_token": "",
                "ws_url": "ws://127.0.0.1:3000",
            }
            wizard.channel_configs["telegram"] = {
                "bot_token": "99999:AAH_token_xyz",
                "proxy_url": None,
            }
            wizard.claimed_events["onebot"] = ("10001", "private", "10001", "m1")
            wizard.first_message_event = ("onebot", "10001", "private", "10001", "m1")

            # For unclaimed telegram: pick manual ID (1) -> "888888"
            with mock.patch("manage._prompt_input", side_effect=["1", "888888"]), \
                 mock.patch("builtins.print"):
                wizard._handle_unclaimed_and_group_options(["onebot", "telegram"])
                wizard._generate_and_save_config()

            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["adapters"]["qq"], {"enabled": False})
            self.assertEqual(data["adapters"]["onebot"]["admin_id"], ["10001"])
            self.assertEqual(data["adapters"]["telegram"]["admin_id"], ["888888"])

    def test_reinitialize_creates_backup_and_preserves_muse_and_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            original = {
                "network": {"proxy_url": "http://127.0.0.1:7890"},
                "muse": {"session_id": "keep-session-id", "proxy": "http", "cookies_file": "my_cookies.json"},
                "logging": {"level": "DEBUG"},
                "adapters": {"qq": {"enabled": True, "app_id": "old", "app_secret": "old"}},
                "gateway": {"login_through_adapter": "qq"},
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(original, f)

            import manage
            wizard = manage.FirstRunWizard(output_file=cfg_path, existing_data=original, preserve_muse=True)
            wizard.selected_channels = ["wechat"]
            wizard.channel_configs["wechat"] = {"enabled": True}
            with mock.patch("builtins.print"):
                wizard._generate_and_save_config()

            self.assertTrue(os.path.exists(f"{cfg_path}.bak"))
            with open(cfg_path, "r", encoding="utf-8") as f:
                updated = json.load(f)
            self.assertEqual(updated["muse"]["session_id"], "keep-session-id")
            self.assertEqual(updated["muse"]["proxy"], "http")
            self.assertEqual(updated["network"]["proxy_url"], "http://127.0.0.1:7890")
            self.assertEqual(updated["logging"]["level"], "DEBUG")
            self.assertNotIn("login_through_adapter", updated["gateway"])
            self.assertEqual(updated["adapters"]["qq"], {"enabled": False})
            self.assertTrue(updated["adapters"]["wechat"]["enabled"])

    def test_daily_console_toggle_and_daily_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "muse": {"session_id": "s1"},
                    "adapters": {
                        "qq": {"enabled": False},
                        "onebot": {"enabled": True, "base_url": "http://127.0.0.1:3000", "ws_url": "ws://127.0.0.1:3000", "admin_id": []},
                    },
                    "gateway": {},
                }, f)

            import manage
            console = manage.BridgeConsole(file_path=cfg_path)
            self.assertTrue(console.load())

            # Simulate daily claim for onebot receiving a group message
            fake_claim = mock.AsyncMock(return_value={"onebot": ("55555", "group", "77777", "m9")})
            with mock.patch("manage._listen_and_claim_channels", fake_claim), \
                 mock.patch("manage._prompt_input", side_effect=["y", "y", "y", "1"]), \
                 mock.patch("builtins.print"):
                console._daily_claim_for_channel("onebot")

            ob = console.data["adapters"]["onebot"]
            self.assertIn("55555", ob["admin_id"])
            self.assertIn("55555", ob["whitelist"]["private"])
            self.assertIn(77777, ob["whitelist"]["groups"])
            self.assertEqual(ob["group_trigger"], "at")
            # Real-time auto-save must have persisted changes to disk immediately
            self.assertFalse(console._modified)
            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertIn("55555", saved["adapters"]["onebot"]["admin_id"])
            self.assertIn(77777, saved["adapters"]["onebot"]["whitelist"]["groups"])

    def test_daily_console_reinit_cancel_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"muse": {"session_id": "orig"}, "adapters": {"qq": {"enabled": False}}}, f)

            import manage
            console = manage.BridgeConsole(file_path=cfg_path)
            console.load()
            with mock.patch("manage._prompt_input", return_value="n"), mock.patch("builtins.print"):
                console._reinitialize_flow()
            self.assertFalse(os.path.exists(f"{cfg_path}.bak"))
            self.assertEqual(console.data["muse"]["session_id"], "orig")

    def test_muse_protocol_login_respects_proxy_and_worker_isolation(self):
        from muse_bridge.muse.login import MuseProtocolLogin
        from muse_bridge.network import NetworkRoute

        with mock.patch("muse_bridge.network.httpx.Client") as mock_client:
            MuseProtocolLogin(route=NetworkRoute(mode="http", proxy_url="http://127.0.0.1:7890"))
            self.assertEqual(mock_client.call_args.kwargs.get("proxy"), "http://127.0.0.1:7890")

        with mock.patch("muse_bridge.network.httpx.Client") as mock_client:
            MuseProtocolLogin(
                route=NetworkRoute(
                    mode="worker",
                    proxy_url="http://127.0.0.1:7890",
                    worker_base="https://worker.example.com",
                    worker_http=True,
                    worker_ws=True,
                ),
            )
            self.assertNotIn("proxy", mock_client.call_args.kwargs)

    def test_all_model_dataclass_fields_registered_in_manage(self):
        import dataclasses
        import manage
        from muse_bridge.config import models

        registered = manage.get_registered_config_fields()
        target_classes = (
            models.MuseConfig,
            models.ApprovalConfig,
            models.WorkerConfig,
            models.GatewayConfig,
            models.GatewayFeedConfig,
            models.QQConfig,
            models.OneBotConfig,
            models.QzoneFeedConfig,
            models.TelegramConfig,
            models.WeChatConfig,
            models.LoggingConfig,
        )
        for cls in target_classes:
            expected = {f.name for f in dataclasses.fields(cls)}
            actual = registered.get(cls.__name__, set())
            missing = expected - actual
            self.assertEqual(missing, set(), f"{cls.__name__} missing fields in manage.py: {missing}")

    def test_cjk_alignment_and_unconfigured_default_markers(self):
        import manage

        rows = [
            manage._format_channel_row(str(i), manage.CHANNEL_LABELS[ch], "STATUS")
            for i, ch in enumerate(manage.CHANNEL_ORDER, 1)
        ]
        # Display column before "STATUS" must be identical across all 4 mixed CJK/ASCII rows
        status_cols = [manage._display_width(row.split("STATUS")[0]) for row in rows]
        self.assertEqual(len(set(status_cols)), 1, f"Misaligned channel rows: {rows}")

        menu_rows = [
            manage._format_menu_row("1", "enabled (是否启用)", "已启用"),
            manage._format_menu_row("10", "whitelist.private (私聊白名单)", "[]"),
            manage._format_menu_row("35", "file_ref_threshold_bytes (大文件引用阈值字节)", "未配置（默认: 2097152 (2MB)）"),
        ]
        colon_cols = [manage._display_width(row.split(" : ")[0]) for row in menu_rows]
        self.assertEqual(len(set(colon_cols)), 1, f"Misaligned menu rows: {menu_rows}")

        # Unset vs configured display markers
        self.assertEqual(
            manage._format_field_state({}, "poll_timeout_s", 30, hint="[1-50]"),
            "未配置（默认: 30） [1-50]",
        )
        self.assertEqual(
            manage._format_field_state({"poll_timeout_s": 45}, "poll_timeout_s", 30, hint="[1-50]"),
            "45 [1-50]",
        )

    def test_telegram_editor_shows_all_unset_defaults_and_supports_edit_and_reset(self):
        import contextlib
        import io
        import manage

        cfg = {"adapters": {"telegram": {"enabled": True, "bot_token": "12345:AAH_valid_tok"}}}
        editor = manage.FullConfigEditor(cfg, file_path="dummy.json")

        # Enter submenu 14 (group whitelist/perms/interact), edit item 3 (seed_main_session -> True),
        # reset item 3 via 'r', exit submenu 0, enter submenu 16 (chunk/media), exit 0, exit 0.
        buf = io.StringIO()
        with mock.patch("manage._prompt_input", side_effect=[
            "14", "3", "y", "r", "3", "0",
            "16", "0",
            "0",
        ]), contextlib.redirect_stdout(buf):
            editor._edit_telegram_category()
        out = buf.getvalue()
        self.assertIn("未配置（默认:", out)
        self.assertIn("seed_main_session", out)
        self.assertIn("file_ref_threshold_bytes", out)
        # After reset, seed_main_session is removed back to unconfigured
        self.assertNotIn("seed_main_session", cfg["adapters"]["telegram"])

    def test_muse_worker_and_gateway_feed_submenus(self):
        import manage

        cfg = {"network": {}, "gateway": {}}
        editor = manage.FullConfigEditor(cfg, file_path="dummy.json")
        # Open Network -> 2 (worker) -> 2 (ws=True) -> 0 -> 0
        with mock.patch("manage._prompt_input", side_effect=["2", "2", "y", "0", "0"]), \
             mock.patch("builtins.print"):
            editor._edit_network_category()
        self.assertTrue(cfg["network"]["worker"]["ws"])

        # Open Gateway -> 2 (feed) -> 1 (enabled=True) -> 2 (targets=onebot=auto) -> 0 -> 0
        with mock.patch("manage._prompt_input", side_effect=["2", "1", "y", "2", "onebot=auto", "0", "0"]), \
             mock.patch("builtins.print"):
            editor._edit_gateway_category()
        self.assertTrue(cfg["gateway"]["feed"]["enabled"])
        self.assertEqual(cfg["gateway"]["feed"]["targets"], {"onebot": "auto"})

    def test_wechat_editor_supports_extra_conversation_fields(self):
        import contextlib
        import io
        import manage

        cfg = {"adapters": {"wechat": {"enabled": True}}}
        editor = manage.FullConfigEditor(cfg, file_path="dummy.json")
        buf = io.StringIO()
        # Enter submenu 14 (交互与离线补发参数) -> item 4 (reaction_quote -> y) -> 0 -> 0
        with mock.patch("manage._prompt_input", side_effect=["14", "4", "y", "0", "0"]), \
             contextlib.redirect_stdout(buf):
            editor._edit_wechat_category()
        out = buf.getvalue()
        self.assertTrue(cfg["adapters"]["wechat"]["reaction_quote"])
        self.assertIn("未配置（默认:", out)
        for forbidden in ("管理员", "白名单", "申请"):
            self.assertNotIn(forbidden, out)

    def test_enter_leaves_configured_and_unconfigured_fields_unchanged(self):
        import copy
        import manage

        original = {
            "muse": {"session_id": "keep_me"},
            "gateway": {},
            "adapters": {
                "qq": {"enabled": True, "app_id": "111"},
                "onebot": {"enabled": True, "group_trigger": "at"},
                "telegram": {"enabled": True, "bot_token": "12345:AAH_valid_tok"},
                "wechat": {"enabled": True, "state_dir": "custom-wx"},
            },
            "logging": {"level": "INFO"},
            "overrides": {"chunk_chars": 1500},
        }
        data = copy.deepcopy(original)
        editor = manage.FullConfigEditor(data, file_path="dummy.json")

        # Enter fields and press Enter ("") across every category and a 2nd-level submenu
        with mock.patch("builtins.input", side_effect=[
            # Muse: 1 (configured str), 7 (unconfigured proxy), 11 (unconfigured bool), 0
            "1", "", "7", "", "11", "", "0",
            # QQ: 2 (configured str), 5 (unconfigured bool), 16 (submenu) -> 1 (proxy) -> "" -> 0, 0
            "2", "", "5", "", "16", "1", "", "0", "0",
            # Telegram: 2 (token), 3 (proxy), 4 (choice), 12 (int), 0
            "2", "", "3", "", "4", "", "12", "", "0",
            # WeChat: 4 (str), 5 (bool), 7 (int), 10 (float), 14 (submenu) -> 1 -> "" -> 0, 0
            "4", "", "5", "", "7", "", "10", "", "14", "1", "", "0", "0",
            # Logging: 1 (choice), 3 (nullable_str), 6 (int), 0
            "1", "", "3", "", "6", "", "0",
            # Overrides: 1 (int), 6 (str), 0
            "1", "", "6", "", "0",
        ]), mock.patch("builtins.print"):
            editor._edit_muse_category()
            editor._edit_qq_category()
            editor._edit_telegram_category()
            editor._edit_wechat_category()
            editor._edit_logging_category()
            editor._edit_overrides_category()

        self.assertEqual(data, original)
        self.assertFalse(editor._modified)

    def test_enable_channel_prompts_credentials_and_auto_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "muse": {"session_id": "s1"},
                    "adapters": {
                        "qq": {"enabled": False},
                        "onebot": {"enabled": False},
                        "telegram": {"enabled": False, "bot_token": "12345:AAH_valid_tok", "admin_id": ["99"]},
                    },
                }, f)

            import manage
            console = manage.BridgeConsole(file_path=cfg_path)
            console.load()

            # 1) Enabling OneBot without credentials & saying No keeps it disabled
            with mock.patch("manage._prompt_input", side_effect=["n"]), mock.patch("builtins.print"):
                console._enable_channel_flow("onebot")
            self.assertFalse(console.data["adapters"]["onebot"]["enabled"])

            # 2) Enabling OneBot without credentials & saying Yes runs guided setup and auto-saves
            with mock.patch("manage._prompt_input", side_effect=[
                "y",       # enter guided setup
                "3000",    # base_url -> http://127.0.0.1:3000
                "tok",     # access_token
                "",        # default ws_url
                "",        # default proxy mode ("none") before probe
                "n",       # skip claiming admin for now
            ]), mock.patch("manage._probe_onebot", return_value=(True, "ok", {"bot_id": "100"})), \
                 mock.patch("builtins.print"):
                console._enable_channel_flow("onebot")

            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertTrue(saved["adapters"]["onebot"]["enabled"])
            self.assertEqual(saved["adapters"]["onebot"]["base_url"], "http://127.0.0.1:3000")
            self.assertEqual(saved["adapters"]["onebot"]["access_token"], "tok")

            # 3) Enabling Telegram with existing credentials & saying No to reconfigure enables immediately
            with mock.patch("manage._prompt_input", side_effect=["n"]), mock.patch("builtins.print"):
                console._enable_channel_flow("telegram")
            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertTrue(saved["adapters"]["telegram"]["enabled"])

    def test_auto_save_persists_deep_submenu_edit_even_on_ctrl_c(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            initial = {"adapters": {"telegram": {"enabled": True, "bot_token": "12345:AAH_valid_tok"}}}
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            import manage
            editor = manage.FullConfigEditor(initial, file_path=cfg_path, auto_save=True)
            # Enter submenu 16 (chunk/media) -> item 1 (chunk_chars) -> "2000" -> then Ctrl+C (SystemExit)
            with mock.patch("builtins.input", side_effect=["16", "1", "2000", KeyboardInterrupt]), \
                 mock.patch("builtins.print"):
                with self.assertRaises(SystemExit):
                    editor._edit_telegram_category()

            # Even though user hit Ctrl+C without pressing 0, the edit was already saved to disk!
            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["adapters"]["telegram"]["chunk_chars"], 2000)

    def test_empty_enter_cancels_channel_credential_prompts(self):
        import manage

        with mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertEqual(manage._configure_qq_interactive(), ("cancel", None, ""))
            self.assertEqual(manage._configure_onebot_interactive(), ("cancel", None, ""))
            self.assertEqual(manage._configure_telegram_interactive(), ("cancel", None, ""))

    def test_wizard_defaults_muse_proxy_none_and_prompts_network_before_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "config.json")
            import manage
            from muse_bridge.config.models import ConfigLoader
            from muse_bridge.network import validate_app_network

            wizard = manage.FirstRunWizard(output_file=out_file)
            self.assertEqual(wizard.muse_proxy, "none")

            # Step 1: answer "y" to configure network before channels -> muse=http, proxy_url (worker not asked)
            # Step 2: pick "3" (telegram) -> token, Enter (defaults to http and reuses proxy_url without re-prompting!) -> "n"
            # Step 3: claim skipped (mocked) -> manual fallback "2" (skip)
            with mock.patch("manage._prompt_input", side_effect=[
                "y",                       # configure network before channels
                "http",                    # muse proxy mode
                "http://127.0.0.1:7890",   # network.proxy_url
                "3",                       # select telegram
                "123456:AAH_valid_token",  # bot_token
                "",                        # telegram proxy mode (defaults to http and reuses proxy_url directly)
                "n",                       # next step
                "2",                       # skip manual admin ID
            ]), mock.patch("manage._probe_telegram", return_value=(True, "ok", {"bot_id": "123456", "username": "bot"})) as probe_tg, \
                 mock.patch.object(wizard, "_listen_for_first_message", new_callable=mock.AsyncMock), \
                 mock.patch("builtins.print"):
                self.assertTrue(wizard.run_interactive(offer_muse_login=False))

            # Probe received the pre-configured NetworkConfig with proxy_url
            self.assertEqual(probe_tg.call_args.args[1], "http")
            self.assertEqual(probe_tg.call_args.args[2].proxy_url, "http://127.0.0.1:7890")

            cfg = ConfigLoader.load(out_file)
            self.assertEqual(cfg.muse.proxy, "http")
            self.assertEqual(cfg.telegram.proxy, "http")
            self.assertEqual(cfg.network.proxy_url, "http://127.0.0.1:7890")
            validate_app_network(cfg)

    def test_channel_probe_can_configure_network_for_qq_onebot_and_wechat(self):
        import manage

        # QQ: configure HTTP proxy right before probing
        qq_net = {}
        with mock.patch("manage._prompt_input", side_effect=[
            "app_1", "sec_1", "http", "http://127.0.0.1:7890",
        ]), mock.patch("manage._probe_qq", return_value=(True, "ok")) as probe_qq, \
             mock.patch("builtins.print"):
            status, cfg, _ = manage._configure_qq_interactive(network_data=qq_net)
        self.assertEqual(status, "ready")
        self.assertEqual(cfg["proxy"], "http")
        self.assertEqual(qq_net["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(probe_qq.call_args.args[2], "http")
        self.assertEqual(probe_qq.call_args.args[3].proxy_url, "http://127.0.0.1:7890")

        # OneBot: configure Worker right before probing
        ob_net = {}
        with mock.patch("manage._prompt_input", side_effect=[
            "https://napcat.example.com", "tok", "wss://napcat.example.com",
            "worker", "https://worker.example.com",
        ]), mock.patch("manage._probe_onebot", return_value=(True, "ok", {"bot_id": "100"})) as probe_ob, \
             mock.patch("builtins.print"):
            status, cfg, _ = manage._configure_onebot_interactive(network_data=ob_net)
        self.assertEqual(status, "ready")
        self.assertEqual(cfg["proxy"], "worker")
        self.assertEqual(ob_net["worker"]["base"], "https://worker.example.com")
        self.assertEqual(probe_ob.call_args.args[3], "worker")
        self.assertEqual(probe_ob.call_args.args[4].worker.base, "https://worker.example.com")

        # WeChat: configure HTTP proxy via option [6] then login via option [1] passes network to run_login
        wx_net = {}
        with mock.patch("manage._prompt_input", side_effect=[
            "6", "http", "http://127.0.0.1:7890",
            "1",
        ]), mock.patch("muse_bridge.adapters.wechat.cli.run_login", return_value={"logged_in": True}) as wx_login, \
             mock.patch("manage._wechat_local_status_text", return_value="本地未登录"), \
             mock.patch("builtins.print"):
            status, cfg, _ = manage._configure_wechat_guided(network_data=wx_net)
        self.assertEqual(status, "ready")
        self.assertEqual(cfg["proxy"], "http")
        self.assertEqual(wx_net["proxy_url"], "http://127.0.0.1:7890")
        wx_login.assert_called_once()
        self.assertEqual(wx_login.call_args.kwargs["network"].proxy_url, "http://127.0.0.1:7890")

    def test_wizard_validates_incomplete_network_before_leaving_channel_loop(self):
        import manage

        wizard = manage.FirstRunWizard()
        wizard.selected_channels = ["qq"]
        wizard.channel_configs["qq"] = {"app_id": "a", "app_secret": "s"}
        wizard.muse_proxy = "worker"  # missing network.worker.base!

        # Pressing "n" triggers validation -> answer "y" to fix network -> keep worker, supply base (proxy_url not asked) -> press "n"
        with mock.patch("manage._prompt_input", side_effect=[
            "n",
            "y",
            "worker",
            "https://worker.example.com",
            "n",
        ]), mock.patch("builtins.print"):
            self.assertTrue(wizard._run_channel_setup_loop())
        self.assertEqual(wizard.network_data["worker"]["base"], "https://worker.example.com")

    def test_proxy_mode_supports_1_2_3_selection(self):
        import manage

        # 1 -> none, 2 -> http, 3 -> worker
        self.assertEqual(manage._parse_proxy_mode("1"), "none")
        self.assertEqual(manage._parse_proxy_mode("2"), "http")
        self.assertEqual(manage._parse_proxy_mode("3"), "worker")

        # FullConfigEditor _prompt_edit_choice accepts 1/2/3
        cfg = {"muse": {}}
        editor = manage.FullConfigEditor(cfg, file_path="dummy.json")
        with mock.patch("manage._prompt_input", side_effect=["7", "2", "0"]), mock.patch("builtins.print"):
            editor._edit_muse_category()
        self.assertEqual(cfg["muse"]["proxy"], "http")

        # _prompt_channel_network accepts "3" for worker when missing, and reuses existing worker without re-prompting
        net = {}
        with mock.patch("manage._prompt_input", side_effect=["3", "https://worker.example.com"]), \
             mock.patch("builtins.print"):
            mode, net_cfg = manage._prompt_channel_network("Telegram ", "none", net)
        self.assertEqual(mode, "worker")
        self.assertEqual(net_cfg.worker.base, "https://worker.example.com")

        # Already configured worker.base: only asks mode (1 prompt), does not ask worker.base again
        with mock.patch("manage._prompt_input", side_effect=["3"]) as prompt_mock, \
             mock.patch("builtins.print"):
            mode, net_cfg = manage._prompt_channel_network("Telegram ", "worker", net)
        self.assertEqual(mode, "worker")
        self.assertEqual(net_cfg.worker.base, "https://worker.example.com")
        self.assertEqual(prompt_mock.call_count, 1)

        # On probe retry (allow_edit_existing=True), allows editing existing worker.base
        with mock.patch("manage._prompt_input", side_effect=["3", "https://worker2.example.com"]), \
             mock.patch("builtins.print"):
            mode, net_cfg = manage._prompt_channel_network(
                "Telegram ", "worker", net, allow_edit_existing=True,
            )
        self.assertEqual(mode, "worker")
        self.assertEqual(net_cfg.worker.base, "https://worker2.example.com")

        # FirstRunWizard._configure_muse_proxy: 2(http) only asks proxy_url; 3(worker) asks worker.base directly
        wizard = manage.FirstRunWizard()
        with mock.patch("manage._prompt_input", side_effect=["2", "http://127.0.0.1:7890"]), \
             mock.patch("builtins.print"):
            wizard._configure_muse_proxy()
        self.assertEqual(wizard.muse_proxy, "http")
        self.assertEqual(wizard.network_data["proxy_url"], "http://127.0.0.1:7890")

        wizard_w = manage.FirstRunWizard()
        with mock.patch("manage._prompt_input", side_effect=["3", "https://cfproxy.example.com"]), \
             mock.patch("builtins.print"):
            wizard_w._configure_muse_proxy()
        self.assertEqual(wizard_w.muse_proxy, "worker")
        self.assertEqual(wizard_w.network_data["worker"]["base"], "https://cfproxy.example.com")
        self.assertEqual(wizard_w.network_data["proxy_url"], "")

        # Switching from http to worker still lets user clear existing proxy_url
        with mock.patch("manage._prompt_input", side_effect=["3", "https://cfproxy.example.com", "clear"]), \
             mock.patch("builtins.print"):
            wizard._configure_muse_proxy()
        self.assertEqual(wizard.muse_proxy, "worker")
        self.assertEqual(wizard.network_data["worker"]["base"], "https://cfproxy.example.com")
        self.assertEqual(wizard.network_data["proxy_url"], "")

        # Choosing 1(none) on clean wizard allows presetting worker for other channels (e.g. Telegram)
        wizard_n = manage.FirstRunWizard()
        with mock.patch("manage._prompt_input", side_effect=["1", "3", "https://cfproxy.example.com"]), \
             mock.patch("builtins.print"):
            wizard_n._configure_muse_proxy()
        self.assertEqual(wizard_n.muse_proxy, "none")
        self.assertEqual(wizard_n.network_data["worker"]["base"], "https://cfproxy.example.com")

    def test_channel_and_network_config_can_go_back_at_any_step(self):
        import manage

        # _prompt_channel_network: pick 2(http) by mistake -> type 0 to go back to mode -> pick 3(worker)
        net = {"worker": {"base": "https://cfproxy.example.com", "ws": True, "http": True}}
        with mock.patch("manage._prompt_input", side_effect=["2", "0", "3"]), \
             mock.patch("builtins.print"):
            mode, net_cfg = manage._prompt_channel_network("Telegram ", "worker", net)
        self.assertEqual(mode, "worker")
        self.assertEqual(net_cfg.worker.base, "https://cfproxy.example.com")

        # _prompt_channel_network: type 0 directly cancels
        with mock.patch("manage._prompt_input", side_effect=["0"]), \
             mock.patch("builtins.print"):
            self.assertEqual(manage._prompt_channel_network("Telegram ", "worker", net), (None, None))

        # QQ: type 0 at AppID, AppSecret, or proxy mode returns cancel
        for seq in (["0"], ["app_1", "0"], ["app_1", "sec_1", "0"]):
            with mock.patch("manage._prompt_input", side_effect=seq), mock.patch("builtins.print"):
                self.assertEqual(manage._configure_qq_interactive(), ("cancel", None, ""))

        # OneBot: type 0 at base_url, access_token, ws_url, or proxy mode returns cancel
        for seq in (
            ["0"],
            ["127.0.0.1:3000", "0"],
            ["127.0.0.1:3000", "", "0"],
            ["127.0.0.1:3000", "", "", "0"],
        ):
            with mock.patch("manage._prompt_input", side_effect=seq), mock.patch("builtins.print"):
                self.assertEqual(manage._configure_onebot_interactive(), ("cancel", None, ""))

        # Telegram: type 0 at token or proxy mode returns cancel; Ctrl+C during probe drops to menu where 0 cancels
        for seq in (["0"], ["123456:AAH_valid_token_1234567890", "0"]):
            with mock.patch("manage._prompt_input", side_effect=seq), mock.patch("builtins.print"):
                self.assertEqual(manage._configure_telegram_interactive(), ("cancel", None, ""))

        with mock.patch("manage._prompt_input", side_effect=[
            "123456:AAH_valid_token_1234567890", "1", "0",
        ]), mock.patch("manage._probe_telegram", side_effect=KeyboardInterrupt), \
             mock.patch("builtins.print"):
            self.assertEqual(manage._configure_telegram_interactive(), ("cancel", None, ""))

        # FirstRunWizard._configure_muse_proxy: 0 at mode or address restores previous state
        wizard = manage.FirstRunWizard()
        wizard.muse_proxy = "http"
        wizard.network_data["proxy_url"] = "http://127.0.0.1:7890"
        with mock.patch("manage._prompt_input", side_effect=["3", "0"]), mock.patch("builtins.print"):
            wizard._configure_muse_proxy()
        self.assertEqual(wizard.muse_proxy, "http")
        self.assertEqual(wizard.network_data["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(wizard.network_data["worker"]["base"], "")

    def test_wechat_qr_ctrl_c_returns_to_menu_without_enabling_or_exiting(self):
        import manage

        # User picks [1] scan QR -> presses Ctrl+C -> returns to WeChat menu -> presses [0] to go back
        with mock.patch("manage._prompt_input", side_effect=["1", "0"]), \
             mock.patch("muse_bridge.adapters.wechat.cli.run_login", side_effect=KeyboardInterrupt), \
             mock.patch("manage._wechat_local_status_text", return_value="本地未登录"), \
             mock.patch("builtins.print"):
            status, cfg, note = manage._configure_wechat_guided(network_data={})
        self.assertEqual((status, cfg, note), ("cancel", None, ""))

        # CancelledError (e.g. EOF during verification) is also caught cleanly
        with mock.patch("manage._prompt_input", side_effect=["1", "0"]), \
             mock.patch("muse_bridge.adapters.wechat.cli.run_login", side_effect=asyncio.CancelledError()), \
             mock.patch("manage._wechat_local_status_text", return_value="本地未登录"), \
             mock.patch("builtins.print"):
            status, cfg, note = manage._configure_wechat_guided(network_data={})
        self.assertEqual((status, cfg, note), ("cancel", None, ""))

    def test_telegram_claim_acknowledges_update_offset_as_consumed(self):
        import manage

        fake_tg = mock.MagicMock()
        fake_tg.updates = mock.AsyncMock(side_effect=[
            [{"update_id": 100, "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 999, "type": "private"}}}],
            [{"update_id": 105, "message": {"message_id": 55, "from": {"id": 424242, "is_bot": False}, "chat": {"id": 424242, "type": "private"}}}],
            [],  # follow-up confirm call with offset=106
        ])
        fake_tg.call = mock.AsyncMock(return_value={})
        fake_tg.close = mock.AsyncMock()

        with mock.patch("muse_bridge.adapters.telegram.client.TelegramClient", return_value=fake_tg), \
             mock.patch("builtins.print"):
            captured = asyncio.run(manage._listen_and_claim_channels(
                ["telegram"],
                {"telegram": {"bot_token": "123456:AAH_valid_token_1234567890", "proxy": "none"}},
            ))

        self.assertEqual(captured["telegram"], ("424242", "private", "424242", "55"))
        self.assertEqual(fake_tg.updates.await_args_list, [
            mock.call(None, poll_timeout=0),
            mock.call(101, poll_timeout=10),
            mock.call(106, poll_timeout=0),
        ])
        fake_tg.call.assert_awaited_once()
        fake_tg.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
