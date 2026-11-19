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
import manage
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

    def test_login_saves_and_closes_without_role_configuration(self):
        cfg = WeChatConfig(state_dir="test-state", http_timeout_s=10)
        with contextlib.redirect_stdout(self.output):
            result = cli.run_login(cfg)
        self.state_cls.assert_called_once_with("test-state")
        self.auth_cls.assert_called_once_with(self.client)
        self.state.save_account.assert_called_once_with(self.credentials)
        self.client.aclose.assert_awaited_once()
        from muse_bridge.network import DIRECT_ROUTE
        self.client_cls.assert_called_once_with(channel_version="2.4.6", route_tag="", timeout=10, route=DIRECT_ROUTE)
        args = self.auth.login.call_args
        self.assertEqual(args.args, (self.state.accounts.return_value,))
        self.assertIs(args.kwargs["display"], cli.display_qr)
        self.assertIs(args.kwargs["verify"], cli.verify_code)
        self.assertEqual(result, self.state.status.return_value)
        self.assertFalse(hasattr(cfg, "admin_id"))
        self.assertNotIn("管理员", self.output.getvalue())
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
        wizard = manage.FirstRunWizard(str(Path(self.tmp.name) / "generated.json"))
        def configure():
            wizard._mark_channel_enabled("wechat", {"enabled": True, "session_bindings": {}}, "已启用")
        with mock.patch.object(manage, "_prompt_input", side_effect=["4", "n"]), \
             mock.patch.object(wizard, "_configure_wechat", side_effect=configure), \
             mock.patch.object(wizard, "_listen_for_first_message", new_callable=mock.AsyncMock) as listen, \
             contextlib.redirect_stdout(self.output):
            wizard.run_interactive(offer_muse_login=False)
        listen.assert_not_called()
        self.assertIsNone(wizard.first_message_event)
        data = json.loads(Path(wizard.output_file).read_text(encoding="utf-8"))
        self.assertNotIn("admin_id", data["adapters"]["wechat"])
        self.assertEqual(data["adapters"]["wechat"]["session_bindings"], {})
        self.assertNotIn("login_through_adapter", data["gateway"])
        self.assertFalse(data["adapters"]["qq"]["enabled"])

    def test_channel_list_allows_free_combinations_including_wechat_and_telegram(self):
        for inputs, expected in (
            (["1", "n"], ["qq"]),
            (["2", "n"], ["onebot"]),
            (["1,2", "n"], ["qq", "onebot"]),
            (["3", "n"], ["telegram"]),
            (["4", "n"], ["wechat"]),
            (["2,4", "n"], ["onebot", "wechat"]),
            (["1,2,3,4", "n"], ["qq", "onebot", "telegram", "wechat"]),
        ):
            wizard = manage.FirstRunWizard()
            with mock.patch.object(manage, "_prompt_input", side_effect=inputs), \
                 mock.patch.object(wizard, "_configure_qq", side_effect=lambda w=wizard: w._mark_channel_enabled("qq", {"app_id": "a", "app_secret": "s"}, "ok")), \
                 mock.patch.object(wizard, "_configure_onebot", side_effect=lambda w=wizard: w._mark_channel_enabled("onebot", {"base_url": "http://127.0.0.1:3000", "access_token": "", "ws_url": "ws://127.0.0.1:3000"}, "ok")), \
                 mock.patch.object(wizard, "_configure_telegram", side_effect=lambda w=wizard: w._mark_channel_enabled("telegram", {"bot_token": "123:abc", "proxy_url": None}, "ok")), \
                 mock.patch.object(wizard, "_configure_wechat", side_effect=lambda w=wizard: w._mark_channel_enabled("wechat", {"enabled": True}, "ok")), \
                 contextlib.redirect_stdout(self.output):
                self.assertTrue(wizard._run_channel_setup_loop())
            self.assertEqual(wizard.selected_channels, expected)

    def test_wechat_menu_back_does_not_modify_configuration(self):
        for settings in (None, {"enabled": True, "chunk_chars": 3000}):
            data = {"adapters": {} if settings is None else {"wechat": settings.copy()}}
            editor = manage.FullConfigEditor(data)
            before = json.loads(json.dumps(editor.data))
            with mock.patch.object(manage, "_prompt_input", return_value="0") as prompt, contextlib.redirect_stdout(self.output):
                editor._edit_wechat_category()
            prompt.assert_called_once()
            self.assertEqual(editor.data, before)
            self.assertFalse(editor._modified)
        for removed in ("管理员", "白名单", "申请"):
            self.assertNotIn(removed, self.output.getvalue())

    def test_wechat_menu_can_login_immediately_without_editing_settings(self):
        settings = {"enabled": True, "state_dir": "chosen-state", "chunk_chars": 3000}
        editor = manage.FullConfigEditor({"adapters": {"wechat": settings.copy()}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["2", "0"]) as prompt, mock.patch.object(manage, "_prompt_bool", side_effect=AssertionError("unrelated setting")), mock.patch.object(cli, "run_login", return_value={"user_id": "scanner"}) as login, contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        self.assertEqual(prompt.call_count, 2)
        login.assert_called_once()
        self.assertEqual(login.call_args.args[0].state_dir, "chosen-state")
        self.assertEqual(editor.data["adapters"]["wechat"], settings)
        self.assertFalse(editor._modified)

    def test_wechat_menu_status_is_independent_from_login_and_settings(self):
        editor = manage.FullConfigEditor({"adapters": {"wechat": {"enabled": True}}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["3", "0"]), mock.patch.object(cli, "run_status") as status, mock.patch.object(cli, "run_login") as login, contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        status.assert_called_once()
        login.assert_not_called()
        self.assertFalse(editor._modified)

    def test_wechat_menu_changes_only_selected_field(self):
        original = {"enabled": True, "state_dir": "same", "chunk_chars": 3000, "max_chunks": 4}
        editor = manage.FullConfigEditor({"adapters": {"wechat": original.copy()}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["5", "0"]), mock.patch.object(manage, "_prompt_bool", return_value=True) as boolean, contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        boolean.assert_called_once_with("输入状态提示", False)
        self.assertEqual(editor.data["adapters"]["wechat"], {**original, "typing_enabled": True})
        self.assertTrue(editor._modified)

    def test_wechat_menu_login_failure_can_return_without_changes(self):
        editor = manage.FullConfigEditor({"adapters": {"wechat": {"enabled": True}}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["2", "0"]), mock.patch.object(cli, "run_login", side_effect=RuntimeError("SECRET")), contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        self.assertEqual(editor.data["adapters"]["wechat"], {"enabled": True})
        self.assertFalse(editor._modified)
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_wechat_menu_numeric_validation_stays_on_selected_field(self):
        editor = manage.FullConfigEditor({"adapters": {"wechat": {"enabled": True}}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["7", "0"]), mock.patch.object(manage, "_prompt_int", side_effect=[4001, 2000]) as number, contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        self.assertEqual(number.call_count, 2)
        self.assertEqual(editor.data["adapters"]["wechat"], {"enabled": True, "chunk_chars": 2000})

    def test_wechat_menu_preserves_fractional_timeout_and_rejects_nonfinite(self):
        editor = manage.FullConfigEditor({"adapters": {"wechat": {"enabled": True, "http_timeout_s": 0.5}}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["10", "nan", "10", "0.5", "0"]), contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        self.assertEqual(editor.data["adapters"]["wechat"], {"enabled": True, "http_timeout_s": 0.5})
        self.assertFalse(editor._modified)

    def test_wechat_menu_session_bindings_rejects_malformed_input(self):
        editor = manage.FullConfigEditor({"adapters": {"wechat": {"enabled": True}}})
        with mock.patch.object(manage, "_prompt_input", side_effect=["11", "invalid", "11", "private:owner=session", "0"]), contextlib.redirect_stdout(self.output):
            editor._edit_wechat_category()
        self.assertEqual(editor.data["adapters"]["wechat"], {"enabled": True, "session_bindings": {"private:owner": "session"}})

    def test_engine_connects_wechat_without_starting_login(self):
        from muse_bridge import engine
        from muse_bridge.config.models import AppConfig, MuseConfig, QQConfig
        adapter_cls = mock.Mock()
        module = types.SimpleNamespace(WeChatAdapter=adapter_cls)
        cfg = AppConfig(muse=MuseConfig(proxy="none"), qq=QQConfig(enabled=False), wechat=WeChatConfig(enabled=True))
        with mock.patch.dict(sys.modules, {"muse_bridge.adapters.wechat.adapter": module}), mock.patch.object(engine, "MuseTokenManager"), mock.patch.object(engine, "MuseService") as service, mock.patch.object(engine, "UnifiedGateway") as gateway:
            gateway.return_value.mount.side_effect = lambda adapter: adapter
            instance = engine.BridgeEngine(cfg)
        adapter_cls.assert_called_once_with(cfg.wechat, network=cfg.network)
        gateway.assert_called_once_with(service.return_value, cfg.gateway)
        gateway.return_value.mount.assert_called_once_with(adapter_cls.return_value)
        self.assertEqual(instance.adapters, [adapter_cls.return_value])
        self.auth_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
