"""WeChat CLI/wizard tests: fake auth, client and state; no real accounts."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import main
import init_config
from muse_bridge.config import WeChatConfig
from muse_bridge.adapters.wechat import cli


class TestWeChatCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.state = mock.Mock()
        self.state.accounts.return_value = [{"bot_id": "saved", "bot_token": "SECRET"}]
        self.state.status.return_value = {"logged_in": True, "has_token": True}
        self.state_cls = mock.Mock(return_value=self.state)
        self.client = mock.Mock(aclose=mock.AsyncMock())
        self.client_cls = mock.Mock(return_value=self.client)
        self.credentials = {"bot_token": "SECRET", "baseurl": "https://example.invalid",
                            "bot_id": "bot", "user_id": "owner"}
        self.auth = mock.Mock(login=mock.AsyncMock(return_value=self.credentials))
        self.auth_cls = mock.Mock(return_value=self.auth)
        modules = {
            "muse_bridge.adapters.wechat.state": types.SimpleNamespace(WeChatState=self.state_cls),
            "muse_bridge.adapters.wechat.client": types.SimpleNamespace(WeChatClient=self.client_cls),
            "muse_bridge.adapters.wechat.auth": types.SimpleNamespace(WeChatAuth=self.auth_cls),
        }
        patch = mock.patch.dict(sys.modules, modules)
        patch.start()
        self.addCleanup(patch.stop)
        self.output = io.StringIO()

    def test_login_saves_and_closes_without_promoting_owner(self):
        cfg = WeChatConfig(state_dir="test-state", admin_id=["configured"], http_timeout_s=10)
        with contextlib.redirect_stdout(self.output):
            result = cli.run_login(cfg)
        self.state_cls.assert_called_once_with("test-state")
        self.auth_cls.assert_called_once_with(self.client)
        self.state.save_account.assert_called_once_with(self.credentials)
        self.client.aclose.assert_awaited_once()
        self.client_cls.assert_called_once_with(channel_version="2.4.6", route_tag="", timeout=10)
        args = self.auth.login.call_args
        self.assertEqual(args.args, (self.state.accounts.return_value,))
        self.assertIs(args.kwargs["display"], cli.display_qr)
        self.assertIs(args.kwargs["verify"], cli.verify_code)
        self.assertEqual(result, self.state.status.return_value)
        self.assertEqual(cfg.admin_id, ["configured"])
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_auth_failure_closes_without_saving(self):
        self.auth.login.side_effect = RuntimeError("SECRET")
        with self.assertRaises(RuntimeError):
            cli.run_login(WeChatConfig())
        self.client.aclose.assert_awaited_once()
        self.state.save_account.assert_not_called()

    def test_status_is_offline_and_redacted(self):
        with contextlib.redirect_stdout(self.output):
            result = cli.run_status(WeChatConfig())
        self.assertEqual(result, {"logged_in": True, "has_token": True})
        self.client_cls.assert_not_called()
        self.auth_cls.assert_not_called()
        self.state.accounts.assert_not_called()
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertIn("未连接微信", self.output.getvalue())

    def test_qr_encodes_string_locally(self):
        qr = mock.Mock()
        qr_cls = mock.Mock(return_value=qr)
        with mock.patch.dict(sys.modules, {"qrcode": types.SimpleNamespace(QRCode=qr_cls)}), contextlib.redirect_stdout(self.output):
            cli.display_qr("https://example.invalid/qr?ticket=SECRET")
        qr.add_data.assert_called_once_with("https://example.invalid/qr?ticket=SECRET")
        qr.print_ascii.assert_called_once_with(invert=True)
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_verify_hidden_and_status_does_not_print_payload(self):
        with mock.patch.object(cli.getpass, "getpass", return_value=" 1234 ") as getpass_mock:
            self.assertEqual(cli.verify_code("SECRET"), "1234")
            self.assertNotIn("SECRET", getpass_mock.call_args.args[0])
        with contextlib.redirect_stdout(self.output):
            cli.show_status({"ticket": "SECRET", "status": "confirmed"})
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_main_early_dispatch_never_full_loads(self):
        for flag, method in (("--wechat-status", "run_status"), ("--wechat-login", "run_login")):
            with self.subTest(flag=flag), mock.patch.object(main.ConfigLoader, "load", side_effect=AssertionError("full loader")), mock.patch.object(main.ConfigLoader, "load_wechat", return_value=WeChatConfig()) as load, mock.patch.object(cli, method) as run:
                self.assertEqual(main.main(["--config", "dummy.json", flag]), 0)
                load.assert_called_once_with("dummy.json")
                run.assert_called_once()

    def test_cli_flags_are_mutually_exclusive(self):
        for pair in (("--wechat-login", "--wechat-status"), ("--login", "--wechat-login"), ("--login", "--wechat-status")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main.parse_args(list(pair))
        self.assertTrue(main.parse_args(["--login"]).login)

    def test_wizard_only_wechat_never_listens_for_first_message(self):
        wizard = init_config.FirstRunWizard(str(Path(self.tmp.name) / "generated.json"))
        def configure():
            wizard.channel_configs["wechat"] = {"enabled": True, "admin_id": [], "session_bindings": {}}
        with mock.patch.object(init_config, "_prompt_input", return_value="4"), mock.patch.object(wizard, "_configure_wechat", side_effect=configure), mock.patch.object(wizard, "_listen_for_first_message", new_callable=mock.AsyncMock) as listen, contextlib.redirect_stdout(self.output):
            wizard.run_interactive()
        listen.assert_not_called()
        self.assertIsNone(wizard.first_message_event)
        data = json.loads(Path(wizard.output_file).read_text(encoding="utf-8"))
        self.assertEqual(data["adapters"]["wechat"]["admin_id"], [])
        self.assertEqual(data["adapters"]["wechat"]["session_bindings"], {})
        self.assertEqual(data["gateway"]["login_through_adapter"], "")
        self.assertFalse(data["adapters"]["qq"]["enabled"])

    def test_original_channel_menu_numbers_unchanged(self):
        for choice, expected in (("1", ["qq"]), ("2", ["onebot"]), ("3", ["qq", "onebot"]),
                                 ("4", ["wechat"]), ("5", ["qq", "onebot", "wechat"])):
            wizard = init_config.FirstRunWizard()
            with mock.patch.object(init_config, "_prompt_input", return_value=choice), contextlib.redirect_stdout(self.output):
                wizard._select_channels()
            self.assertEqual(wizard.selected_channels, expected)

    def test_wizard_scanner_admin_requires_explicit_confirmation(self):
        for promote in (False, True):
            editor = init_config.FullConfigEditor({"adapters": {"wechat": {"enabled": True}}})
            def choose_bool(title, current):
                if title.startswith("现在显式"):
                    return True
                if title.startswith("将本次扫码者"):
                    self.assertFalse(current)
                    return promote
                return current
            with mock.patch.object(init_config, "_prompt_bool", side_effect=choose_bool), mock.patch.object(init_config, "_prompt_str", side_effect=lambda title, current: current), mock.patch.object(init_config, "_prompt_int", side_effect=lambda title, current, default: current), mock.patch.object(init_config, "_prompt_str_list", return_value=[]), mock.patch.object(init_config, "_prompt_input", return_value=""), mock.patch.object(cli, "run_login", return_value={"user_id": "scanner"}), contextlib.redirect_stdout(self.output):
                editor._edit_wechat_category()
            self.assertEqual(editor.data["adapters"]["wechat"]["admin_id"], ["scanner"] if promote else [])

    def test_engine_connects_wechat_without_starting_login(self):
        from muse_bridge import engine
        from muse_bridge.config.models import AppConfig, QQConfig
        adapter_cls = mock.Mock()
        module = types.SimpleNamespace(WeChatAdapter=adapter_cls)
        cfg = AppConfig(qq=QQConfig(enabled=False), wechat=WeChatConfig(enabled=True))
        with mock.patch.dict(sys.modules, {"muse_bridge.adapters.wechat.adapter": module}), mock.patch.object(engine, "MuseTokenManager"), mock.patch.object(engine, "MuseService") as service:
            instance = engine.BridgeEngine(cfg)
        adapter_cls.assert_called_once_with(config=cfg.wechat, muse_service=service.return_value)
        self.assertEqual(instance.adapters, [adapter_cls.return_value])
        self.auth_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
