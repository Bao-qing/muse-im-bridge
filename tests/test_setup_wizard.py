"""Tests for first-run setup wizard (init_config.py)."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from init_config import FirstRunWizard, _default_ws_url


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
            self.assertEqual(data["gateway"]["login_through_adapter"], "qq")
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

            self.assertEqual(data["gateway"]["login_through_adapter"], "onebot")
            self.assertIn("onebot", data["adapters"])
            self.assertNotIn("qq", data["adapters"])

            ob = data["adapters"]["onebot"]
            self.assertTrue(ob["enabled"])
            self.assertEqual(ob["qzone_feed"], QZONE_DEFAULTS)
            self.assertEqual(ob["base_url"], "http://127.0.0.1:3000")
            self.assertEqual(ob["access_token"], "my_token")
            self.assertEqual(ob["admin_id"], ["10001"])
            self.assertEqual(ob["target_group_id"], 20002)
            self.assertEqual(ob["whitelist"]["private"], ["10001"])
            self.assertEqual(ob["whitelist"]["groups"], [20002])

    def test_generate_dual_channel_config_qq_first(self):
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

            self.assertEqual(data["gateway"]["login_through_adapter"], "qq")
            self.assertIn("qq", data["adapters"])
            self.assertIn("onebot", data["adapters"])

            qq = data["adapters"]["qq"]
            self.assertEqual(qq["admin_id"], ["QQ_ADMIN_OPENID"])

            ob = data["adapters"]["onebot"]
            self.assertTrue(ob["enabled"])
            self.assertEqual(ob["qzone_feed"], QZONE_DEFAULTS)
            self.assertEqual(ob["target_group_id"], 123456789)
            self.assertEqual(ob["whitelist"]["groups"], [123456789])

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
        from init_config import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Mock user input to change session_id (option 1) then exit category (option 0)
        with mock.patch("init_config._prompt_input", side_effect=["1", "new_custom_sid", "0"]):
            editor._edit_muse_category()

        self.assertEqual(cfg["muse"]["session_id"], "new_custom_sid")
        self.assertTrue(editor._modified)

    def test_editor_modifies_gateway_category(self):
        cfg = {"gateway": {"login_through_adapter": "qq"}}
        from init_config import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        with mock.patch("init_config._prompt_input", side_effect=["1", "onebot", "0"]):
            editor._edit_gateway_category()

        self.assertEqual(cfg["gateway"]["login_through_adapter"], "onebot")

    def test_editor_modifies_qq_category(self):
        cfg = {"adapters": {"qq": {"enabled": False, "app_id": "111"}}}
        from init_config import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Option 1: enabled -> True, Option 2: app_id -> 222, Option 0: exit
        with mock.patch("init_config._prompt_input", side_effect=["1", "y", "2", "222", "0"]):
            editor._edit_qq_category()

        self.assertTrue(cfg["adapters"]["qq"]["enabled"])
        self.assertEqual(cfg["adapters"]["qq"]["app_id"], "222")

    def test_editor_modifies_onebot_category(self):
        cfg = {"adapters": {"onebot": {"enabled": True, "base_url": "http://127.0.0.1:3000"}}}
        from init_config import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        # Option 5: group_trigger -> at, Option 0: exit
        with mock.patch("init_config._prompt_input", side_effect=["5", "at", "0"]):
            editor._edit_onebot_category()

        self.assertEqual(cfg["adapters"]["onebot"]["group_trigger"], "at")

    def test_editor_qzone_defaults_stay_disabled(self):
        from init_config import FullConfigEditor
        editor = FullConfigEditor({})
        # Enter item 15, accept every default, then return to the parent.
        with mock.patch("builtins.input", side_effect=["15"] + [""] * 10 + ["0"]):
            editor._edit_onebot_category()
        self.assertEqual(editor.data["adapters"]["onebot"]["qzone_feed"], QZONE_DEFAULTS)
        self.assertTrue(editor._modified)

    def test_editor_qzone_edits_validate_and_preserve_other_settings(self):
        from init_config import FullConfigEditor
        original = {**QZONE_DEFAULTS, "poll_interval_s": 900}
        ob = {"base_url": "http://localhost:3000", "qzone_feed": original}
        editor = FullConfigEditor({"adapters": {"onebot": ob}})
        with mock.patch("init_config._prompt_input", side_effect=[
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
        from init_config import FullConfigEditor
        editor = FullConfigEditor({})
        with mock.patch("init_config._prompt_input", side_effect=[
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
        from init_config import FullConfigEditor
        editor = FullConfigEditor(cfg, file_path="dummy.json")

        with mock.patch("init_config._prompt_input", side_effect=["1", "DEBUG", "0"]):
            editor._edit_logging_category()
        self.assertEqual(cfg["logging"]["level"], "DEBUG")

        with mock.patch("init_config._prompt_input", side_effect=["1", "1500", "0"]):
            editor._edit_overrides_category()
        self.assertEqual(cfg["overrides"]["chunk_chars"], 1500)

    def test_editor_save_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test_config.json")
            from init_config import FullConfigEditor
            editor = FullConfigEditor({"muse": {"session_id": "test"}}, file_path=p)
            editor.save()

            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["muse"]["session_id"], "test")


if __name__ == "__main__":
    unittest.main()
