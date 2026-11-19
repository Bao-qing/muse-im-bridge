"""Offline tests for the OneBot (NapCat) adapter on the chat gateway."""

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muse_bridge.adapters.onebot import adapter as ob_adapter
from muse_bridge.adapters.onebot.adapter import parse_segments
from gateway_helpers import OneBotAdapter
from muse_bridge.adapters.onebot.client import OneBotAPIError, OneBotClient
from muse_bridge.adapters.onebot.gateway import OneBotGateway, connect_url
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, OneBotConfig
from muse_bridge.core.interactions import InteractionManager
from muse_bridge.gateway.routes import RouteManager
from muse_bridge.muse.interactions import ApprovalBlocked

SELF = "300000001"


def _response(payload):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


class TestOneBotConfig(unittest.TestCase):
    def test_legacy_whitelist_and_numeric_ids_become_strings(self):
        cfg = OneBotConfig(whitelist={"private": [123, "456"], "groups": [300000002]}, admin_id=[300000003])
        self.assertEqual(cfg.whitelist, {"private": ["123", "456"], "300000002": "*"})
        self.assertEqual(cfg.admin_id, ["300000003"])
        self.assertTrue(cfg.is_admin("300000003"))

    def test_group_trigger_and_booleans_are_validated(self):
        self.assertFalse(OneBotConfig().typing_enabled)
        with self.assertRaisesRegex(ValueError, "adapters.onebot.group_trigger"):
            OneBotConfig(group_trigger="sometimes")
        with self.assertRaisesRegex(ValueError, "adapters.onebot.typing_enabled"):
            OneBotConfig(typing_enabled="yes")
        with self.assertRaisesRegex(ValueError, "adapters.onebot.typing_enabled"):
            OneBotConfig(typing_enabled=1)
        with self.assertRaisesRegex(ValueError, "adapters.onebot.approvals_in_place"):
            OneBotConfig(approvals_in_place="yes")
        with self.assertRaisesRegex(ValueError, "adapters.onebot.allow_all_private"):
            OneBotConfig(allow_all_private="yes")
        with self.assertRaisesRegex(ValueError, "adapters.onebot.allow_all_group"):
            OneBotConfig(allow_all_group="yes")

    def test_loader_loads_typing_enabled_and_wizard_toggles_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({
                "muse": {"client_ip": "198.51.100.1"},
                "adapters": {"qq": {"enabled": False}, "onebot": {"enabled": True, "typing_enabled": True}},
            }), encoding="utf-8")
            self.assertTrue(ConfigLoader.load(str(path)).onebot.typing_enabled)
        from manage import FullConfigEditor
        editor = FullConfigEditor({"adapters": {"onebot": {}}})
        with mock.patch("manage._prompt_input", side_effect=["16", "y", "0"]), mock.patch("builtins.print"):
            editor._edit_onebot_category()
        self.assertTrue(editor.data["adapters"]["onebot"]["typing_enabled"])

    def test_loader_uses_onebot_state_files_and_env_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"muse": {"client_ip": "198.51.100.1"}, "adapters": {
                "qq": {"enabled": False},
                "onebot": {"enabled": True, "base_url": "http://127.0.0.1:3000", "admin_id": 42,
                           "group_trigger": "at", "whitelist": {"groups": [7]}}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ONEBOT_ACCESS_TOKEN": "env-token"}):
                cfg = ConfigLoader.load(str(path)).onebot
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.access_token, "env-token")
        self.assertEqual(cfg.admin_id, ["42"])
        self.assertEqual(cfg.group_trigger, "at")
        self.assertEqual(cfg.whitelist, {"7": "*"})
        self.assertFalse(cfg.allow_all_private)
        self.assertFalse(cfg.allow_all_group)
        self.assertEqual(cfg.session_map_file, "data/onebot_session_map.json")
        self.assertEqual(cfg.routes_file, "data/onebot_routes.json")
        self.assertEqual(cfg.access_requests.state_file, "data/onebot_access.local.json")


class TestOneBotClient(unittest.TestCase):
    def setUp(self):
        self.client = OneBotClient("https://onebot.test", "tok")

    def test_call_raises_on_refused_action_with_first_line_only(self):
        refused = {"status": "failed", "retcode": 1200, "message": "send failed\n at stack (napcat.mjs:1)"}
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", return_value=_response(refused)):
            with self.assertRaises(OneBotAPIError) as raised:
                self.client.send_msg("group", "1", [{"type": "text", "data": {"text": "x"}}])
        self.assertEqual(raised.exception.retcode, 1200)
        self.assertNotIn("napcat.mjs", str(raised.exception))

    def test_send_msg_payload_user_agent_and_message_id(self):
        ok = {"status": "ok", "retcode": 0, "data": {"message_id": 99}}
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", return_value=_response(ok)) as open_:
            data = self.client.send_msg("private", "42", [{"type": "text", "data": {"text": "hi"}}])
        req = open_.call_args.args[0]
        self.assertEqual(req.full_url, "https://onebot.test/send_private_msg")
        self.assertEqual(json.loads(req.data)["user_id"], 42)
        self.assertNotIn("Python-urllib", req.get_header("User-agent"))
        self.assertEqual(data, {"message_id": 99})

    def test_legacy_send_group_msg_raises_instead_of_reporting_success(self):
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", return_value=_response({"status": "failed", "retcode": 100})):
            with self.assertRaises(OneBotAPIError):
                self.client.send_group_msg(1, "x")

    def test_set_input_status_validates_params_and_stays_quiet_on_error(self):
        ok = {"status": "ok", "retcode": 0, "data": {}}
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", return_value=_response(ok)) as open_:
            self.client.set_input_status("42", 1)
        req = open_.call_args.args[0]
        self.assertEqual(req.full_url, "https://onebot.test/set_input_status")
        self.assertEqual(json.loads(req.data), {"user_id": 42, "event_type": 1})
        for bad_uid in (0, -5, "abc"):
            with self.assertRaises(ValueError):
                self.client.set_input_status(bad_uid, 1)
        for bad_type in (3, -1, True, "1"):
            with self.assertRaises(ValueError):
                self.client.set_input_status(42, bad_type)
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", side_effect=OSError("offline")), \
             mock.patch("muse_bridge.adapters.onebot.client.logger.error") as err_log:
            with self.assertRaises(RuntimeError):
                self.client.set_input_status(42, 1)
            err_log.assert_not_called()

    def test_file_source_prefers_base64_then_http_url_and_skips_local_paths(self):
        answers = [{"status": "ok", "retcode": 0, "data": {"url": "/app/temp/a.txt"}},
                   {"status": "ok", "retcode": 0, "data": {"url": "https://cdn.test/a.txt"}}]
        with mock.patch("muse_bridge.network.NetworkRoute.open_url", side_effect=[_response(a) for a in answers]):
            self.assertEqual(self.client.file_source("/fid", group_id="7"), {"url": "https://cdn.test/a.txt"})
        with mock.patch("muse_bridge.network.NetworkRoute.open_url",
                        return_value=_response({"status": "ok", "retcode": 0, "data": {"base64": "QQ=="}})):
            self.assertEqual(self.client.file_source("/fid"), {"base64": "QQ=="})
        with mock.patch("muse_bridge.network.NetworkRoute.open_url",
                        return_value=_response({"status": "ok", "retcode": 0, "data": {"url": "/app/a"}})):
            self.assertEqual(self.client.file_source("/fid"), {})


class TestOneBotGateway(unittest.IsolatedAsyncioTestCase):
    def test_connect_url_keeps_path_and_query_and_replaces_token(self):
        self.assertEqual(connect_url("wss://h/ws?sig=a&access_token=old", "new"),
                         "wss://h/ws?sig=a&access_token=new")
        self.assertEqual(connect_url("ws://h:3000", "t"), "ws://h:3000/?access_token=t")
        self.assertEqual(connect_url("ws://h:3000", ""), "ws://h:3000")

    async def test_lifecycle_sets_self_id_and_message_sent_is_not_dispatched(self):
        callback = mock.AsyncMock()
        gateway = OneBotGateway("wss://h", "t", callback)
        frames = [{"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": 5},
                  {"post_type": "message_sent", "message_type": "group"},
                  {"post_type": "message", "message_type": "group"}]

        async def recv():
            if frames:
                return json.dumps(frames.pop(0))
            gateway._running = False
            raise RuntimeError("closed")

        ws = mock.Mock(recv=mock.AsyncMock(side_effect=recv))
        cm = mock.MagicMock()
        cm.__aenter__ = mock.AsyncMock(return_value=ws)
        cm.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch("muse_bridge.adapters.onebot.gateway.websockets",
                        mock.Mock(connect=mock.Mock(return_value=cm))):
            await gateway.start()
        await asyncio.sleep(0)
        self.assertEqual(gateway.self_id, "5")
        callback.assert_awaited_once_with({"post_type": "message", "message_type": "group"})

    async def test_stop_closes_the_socket(self):
        gateway = OneBotGateway("wss://h", "t", mock.AsyncMock())
        gateway._ws = mock.Mock(close=mock.AsyncMock())
        gateway._running = True
        await gateway.stop()
        self.assertFalse(gateway._running)
        gateway._ws.close.assert_awaited_once()


class TestSegments(unittest.TestCase):
    def test_text_mentions_reply_and_media(self):
        message = [
            {"type": "reply", "data": {"id": "1289753793"}},
            {"type": "at", "data": {"qq": SELF}},
            {"type": "text", "data": {"text": " 你好 "}},
            {"type": "at", "data": {"qq": "123", "name": "Bob"}},
            {"type": "image", "data": {"file": "ABC.png", "url": "https://multimedia.nt.qq.com.cn/x?rkey=1"}},
            {"type": "file", "data": {"file": "probe.txt", "file_id": "/fid", "file_size": "21"}},
            {"type": "face", "data": {"id": "1"}},
        ]
        text, mentioned, atts, refs = parse_segments(message, SELF)
        self.assertEqual(text, "你好 @Bob")
        self.assertTrue(mentioned)
        self.assertEqual(refs, [("id", "1289753793")])
        self.assertEqual([(a.type, a.filename, a.url, a.file_info) for a in atts], [
            ("image", "ABC.png", "https://multimedia.nt.qq.com.cn/x?rkey=1", None),
            ("file", "probe.txt", None, "/fid")])
        self.assertEqual(atts[0].content_type, "image/png")

    def test_cq_string_message(self):
        text, mentioned, atts, refs = parse_segments(
            f"[CQ:reply,id=9][CQ:at,qq={SELF}] /help [CQ:image,file=a.png]", SELF)
        self.assertEqual((text, mentioned, atts, refs), ("/help", True, [], [("id", "9")]))


def _make_adapter(tmp, **extra):
    fields = dict(
        enabled=True, base_url="http://127.0.0.1:3000", access_token="tok",
        whitelist={"private": ["A1", "100"], "G1": "*", "G2": "*"},
        admin_id=["A1"], admin_only=False,
        access_requests=AccessRequestsConfig(state_file=os.path.join(tmp, "access.local.json")),
        session_map_file=os.path.join(tmp, "map.json"),
        delivered_file=os.path.join(tmp, "delivered.jsonl"),
        push_state_file=os.path.join(tmp, "push.json"),
        routes_file=os.path.join(tmp, "routes.json"),
    )
    fields.update(extra)
    cfg = OneBotConfig(**fields)
    muse = mock.Mock()
    muse.interactions = InteractionManager()
    muse.interaction_protocol.referenced_approvals.return_value = None
    muse.post_message = mock.Mock(side_effect=lambda sid, text, atts, thr: f"mid-{sid}-{text}")
    adapter = OneBotAdapter(config=cfg, muse_service=muse)
    adapter.self_id = SELF
    adapter.host.identity_changed(adapter.adapter)
    adapter.route_manager = RouteManager(state_file=None)
    adapter.outbound_pusher.route_manager = adapter.route_manager
    adapter.client = mock.Mock()
    adapter.client.send_msg = mock.Mock(return_value={"message_id": 777})
    adapter.outbound_pusher.client = adapter.client
    adapter.outbound_pusher.ensure_baseline = mock.AsyncMock()
    return adapter, muse


def _group(gid, uid, text, mid, *, at=False):
    message = ([{"type": "at", "data": {"qq": SELF}}] if at else []) + [{"type": "text", "data": {"text": text}}]
    return {"post_type": "message", "message_type": "group", "group_id": int(gid) if gid.isdigit() else gid,
            "user_id": uid, "self_id": int(SELF), "message_id": mid, "message": message}


def _private(uid, text, mid):
    return {"post_type": "message", "message_type": "private", "user_id": uid, "self_id": int(SELF),
            "message_id": mid, "message": [{"type": "text", "data": {"text": text}}]}


def _sent_texts(adapter):
    return [c.args[2][0]["data"]["text"] for c in adapter.client.send_msg.call_args_list
            if c.args[2] and c.args[2][0]["type"] == "text"]


class TestOneBotInbound(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_gateway_and_interaction_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            for index, (text, native) in enumerate((("/状态", "/status"),
                    ("/用量 开启", "/usage on"), ("/停止", "/stop")), 950):
                await adapter.handle_event(_group("G1", 5, text, index))
                self.assertEqual(muse.post_message.call_args.args[1], native)
            await adapter.handle_event(_private("A1", "/登录", 960))
            self.assertIn("请输入登录邮箱", _sent_texts(adapter)[-1])
            await adapter.handle_event(_private("A1", "/取消", 961))
            self.assertIn("已结束填写", _sent_texts(adapter)[-1])

    async def test_group_sender_info_default_and_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            event = _group("G1", 123, "hello", 901)
            event["sender"] = {"nickname": "小明", "card": "群名片"}
            await adapter.handle_event(event)
            text = muse.post_message.call_args.args[1]
            self.assertIn('"用户ID": "123"', text)
            self.assertIn('"昵称": "小明"', text)
            self.assertIn('"群名片": "群名片"', text)
            self.assertTrue(text.endswith("\nhello"))
            adapter.config.group_sender_info = False
            await adapter.handle_event(_group("G1", 123, "plain", 902))
            self.assertEqual(muse.post_message.call_args.args[1], "plain")
            await adapter.handle_event(_private(100, "private", 903))
            self.assertEqual(muse.post_message.call_args.args[1], "private")

    async def test_each_chat_gets_its_own_session_and_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            await adapter.handle_event(_group("G1", 1, "hi 1", 11))
            await adapter.handle_event(_group("G2", 2, "hi 2", 12))
            await adapter.handle_event(_group("G1", 3, "again 1", 13))
            await adapter.handle_event(_private(100, "hi p", 14))
            sids = [c.args[0] for c in muse.post_message.call_args_list]
            self.assertEqual(sids[0], sids[2])
            self.assertEqual(len({sids[0], sids[1], sids[3]}), 3)
            route = adapter.route_manager.get(
                f"mid-{sids[1]}-{muse.post_message.call_args_list[1].args[1]}")
            self.assertEqual((route.target_type, route.target_id, route.msg_id), ("group", "G2", "12"))
            self.assertEqual(adapter.session_mapper.owners(sids[3]), [("private", "100")])

    async def test_self_duplicates_and_strangers_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            await adapter.handle_event(_group("G1", int(SELF), "echo", 1))
            await adapter.handle_event(_group("G1", 5, "once", 2))
            await adapter.handle_event(_group("G1", 5, "once", 2))
            await adapter.handle_event(_group("G9", 5, "stranger group", 3))
            await adapter.handle_event(_private(999, "stranger dm", 4))
            await adapter.handle_event({"post_type": "notice", "notice_type": "group_upload"})
            self.assertEqual([c.args[1] for c in muse.post_message.call_args_list], ['【群消息发送者 {"用户ID": "5"}】\nonce'])
            adapter.client.send_msg.assert_not_called()

    async def test_group_trigger_at_needs_mention_or_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, group_trigger="at")
            await adapter.handle_event(_group("G1", 5, "plain", 1))
            await adapter.handle_event(_group("G1", 5, "called", 2, at=True))
            await adapter.handle_event(_private(100, "dm needs no mention", 3))
            self.assertEqual([c.args[1] for c in muse.post_message.call_args_list],
                             ['【群消息发送者 {"用户ID": "5"}】\ncalled', "dm needs no mention"])
            await adapter.handle_event(_group("G1", 5, "/help", 4))
            self.assertIn("/会话", _sent_texts(adapter)[-1])

    async def test_admin_command_blocked_for_non_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, admin_only=True)
            await adapter.handle_event(_group("G1", 5, "/new", 1))
            self.assertIn("权限不足", _sent_texts(adapter)[-1])
            self.assertEqual(adapter.session_mapper.current("group", "G1"), "")
            await adapter.handle_event(_private("A1", "/new", 2))
            self.assertIn("已开启新会话", _sent_texts(adapter)[-1])

    async def test_approval_gate_is_reported_in_the_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            muse.post_message.side_effect = ApprovalBlocked("【审批 A1】1/1\n选项 1｜允许")
            await adapter.handle_event(_group("G1", 5, "do it", 1))
            self.assertEqual(adapter.client.send_msg.call_args.args[:2], ("group", "G1"))
            self.assertIn("【审批 A1】", _sent_texts(adapter)[-1])

    async def test_send_failure_reply_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            muse.post_message.side_effect = RuntimeError("boom")
            await adapter.handle_event(_private(100, "hello", 1))
            self.assertIn("消息可能已发送", _sent_texts(adapter)[-1])
            self.assertNotIn("boom", _sent_texts(adapter)[-1])
            muse.post_message.assert_called_once()

    async def test_image_downloaded_with_cap_and_file_via_file_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.client.download_bytes = mock.Mock(return_value=b"png-bytes")
            adapter.client.file_source = mock.Mock(return_value={"base64": base64.b64encode(b"doc").decode()})
            event = _group("G1", 5, "看看", 1)
            event["message"] += [
                {"type": "image", "data": {"file": "a.png", "url": "https://cdn.test/a?rkey=1"}},
                {"type": "file", "data": {"file": "d.txt", "file_id": "/fid"}},
            ]
            await adapter.handle_event(event)
            atts = muse.post_message.call_args.args[2]
            self.assertEqual([a.data for a in atts], [b"png-bytes", b"doc"])
            adapter.client.download_bytes.assert_called_once_with(
                "https://cdn.test/a?rkey=1", adapter.config.max_image_bytes, adapter.config.download_timeout_s)
            adapter.client.file_source.assert_called_once_with("/fid", "G1")

    async def test_undownloadable_file_is_noted_in_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.client.file_source = mock.Mock(return_value={})
            event = _private(100, "", 1)
            event["message"] = [{"type": "file", "data": {"file": "d.txt", "file_id": "/fid"}}]
            await adapter.handle_event(event)
            self.assertIn("[file下载失败]", muse.post_message.call_args.args[1])

    async def test_attachment_failure_does_not_forward_secret_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.download_attachment = mock.AsyncMock(side_effect=RuntimeError("aes_key=private-key"))
            event = _private(100, "", 1)
            event["message"] = [{"type": "file", "data": {"file": "d.txt", "file_id": "/fid"}}]
            await adapter.handle_event(event)
            text = muse.post_message.call_args.args[1]
            self.assertIn("[file下载失败]", text)
            self.assertNotIn("private-key", text)
            self.assertNotIn("aes_key", text)

    async def test_admin_fills_group_credential_card_in_place_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.session_mapper.bind("group", "G1", "s1")
            item = muse.interactions.add("credential", session_id="s1", ttl=0,
                                         context={"page_url": "https://example.test/login", "capture": "sign_in"})
            muse.interactions.remember_credential_message("group", "G1", {"id": "555"}, item)

            def quoted(uid, mid):
                event = _group("G1", uid, "account\npassword", mid)
                event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})
                return event

            await adapter.handle_event(quoted(5, 1))
            muse.capture_credential.assert_not_called()
            self.assertEqual(_sent_texts(adapter)[-1], "仅管理员可以填写凭证。")
            await adapter.handle_event(quoted("A1", 2))
            muse.capture_credential.assert_called_once_with(item, "password")
            self.assertEqual(_sent_texts(adapter)[-1], "【凭证已保存】")
            muse.post_message.assert_not_called()

    async def test_ordinary_quote_with_pending_credential_is_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.session_mapper.bind("group", "G1", "s1")
            item = muse.interactions.add("credential", session_id="s1", ttl=0)
            owner = ("group", "G1", "A1")
            mid = 0
            for active in (False, True):
                if active:
                    await adapter.handle_event(_group("G1", "A1", f"/凭证 {item.id}", 100))
                    adapter.client.send_msg.reset_mock()
                for text in ("点run all吗", "第一行\n第二行"):
                    with self.subTest(active=active, text=text):
                        mid += 1
                        muse.post_message.reset_mock()
                        event = _group("G1", "A1", text, mid)
                        event["message"].insert(0, {"type": "reply", "data": {"id": "ordinary"}})
                        self.assertFalse(adapter.interaction_router.is_credential_input(
                            owner, text, {"references": parse_segments(event["message"], SELF)[3]}))
                        await adapter.handle_event(event)
                        muse.capture_credential.assert_not_called()
                        muse.post_message.assert_called_once()
                        self.assertTrue(muse.post_message.call_args.args[1].endswith(text))
                        adapter.client.send_msg.assert_not_called()
                self.assertIs(muse.interactions.current(owner), item if active else None)

    async def test_ordinary_quote_does_not_bypass_group_at_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, group_trigger="at")
            adapter.session_mapper.bind("group", "G1", "s1")
            muse.interactions.add("credential", session_id="s1", ttl=0)
            event = _group("G1", "A1", "点run all吗", 1)
            event["message"].insert(0, {"type": "reply", "data": {"id": "ordinary"}})
            await adapter.handle_event(event)
            muse.post_message.assert_not_called()
            adapter.client.send_msg.assert_not_called()

    async def test_quoted_credential_prompt_is_found_by_reply_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            item = muse.interactions.add("credential", session_id="s1")
            body = await adapter.send_reply("private", "A1", "请输入密码。")
            muse.interactions.remember_credential_message("private", "A1", body, item)
            refs = parse_segments(
                [{"type": "reply", "data": {"id": "777"}}, {"type": "text", "data": {"text": "pw"}}], SELF)[3]
            self.assertEqual(adapter.interaction_router.message_references({"references": refs}), refs)
            self.assertIs(muse.interactions.referenced_credential("private", "A1", refs), item)


class TestOneBotOutbound(unittest.IsolatedAsyncioTestCase):
    async def test_text_chunks_media_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = _make_adapter(tmp, chunk_chars=5, max_chunks=8)
            pusher = adapter.outbound_pusher
            ok = await pusher._deliver("group", "G1", "abcde\nfghij", [{"data": b"img", "filename": "a.png"}],
                                       [{"data": b"doc", "filename": "d.txt"}], [])
            self.assertTrue(ok)
            segs = [c.args[2][0] for c in adapter.client.send_msg.call_args_list]
            self.assertEqual([s["type"] for s in segs], ["text", "text", "image", "text"])
            self.assertIn("d.txt", segs[-1]["data"]["text"])
            self.assertEqual(segs[2]["data"]["file"], "base64://" + base64.b64encode(b"img").decode())
            adapter.client.upload_file.assert_called_once_with("group", "G1", b"doc", "d.txt")

    async def test_refused_group_send_falls_back_to_admin_with_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = _make_adapter(tmp)
            adapter.client.send_msg.side_effect = [OneBotAPIError("send_group_msg", 1200), {"message_id": 5}]
            ok = await adapter.outbound_pusher._deliver("group", "G1", "hello", [], [], [])
            self.assertTrue(ok)
            last = adapter.client.send_msg.call_args
            self.assertEqual(last.args[:2], ("private", "A1"))
            self.assertEqual(''.join(seg["data"]["text"] for seg in last.args[2]
                                     if seg["type"] == "text"), "【群消息提醒】\nhello")

    async def test_transport_error_is_not_retried_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = _make_adapter(tmp)
            adapter.client.send_msg.side_effect = RuntimeError("timeout")
            ok = await adapter.outbound_pusher._deliver("group", "G1", "hello", [], [], [])
            self.assertFalse(ok)
            adapter.client.send_msg.assert_called_once()

    async def test_approval_card_goes_to_the_session_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.session_mapper.bind("group", "G2", "S2")
            item = muse.interactions.add("approval", session_id="S2", remote_id="r1", context={"fingerprint": "fp"})
            muse.interaction_protocol.display_snapshot.return_value = ("fp", [])
            delivered = await adapter.notify_interaction(item, "【审批】card")
            self.assertTrue(delivered)
            self.assertEqual(adapter.client.send_msg.call_args.args[:2], ("group", "G2"))
            muse.interaction_protocol.mark_displayed.assert_called_once_with(
                item, adapter.outbound_pusher.interaction_target("group", "G2"), ("fp", []))
            self.assertTrue(adapter.owns_session("S2"))
            self.assertFalse(adapter.owns_session("other"))


if __name__ == "__main__":
    unittest.main()


class TestGatewayRouting(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, qq_owns, onebot_owns):
        from gateway_helpers import QQAdapter
        from muse_bridge.config.models import GatewayConfig, QQConfig
        from muse_bridge.gateway.runtime import UnifiedGateway
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = mock.Mock()
        gateway = UnifiedGateway(service, GatewayConfig())
        mounted = []
        for factory, cfg_cls, name, owns in ((QQAdapter, QQConfig, "qq", qq_owns),
                                             (OneBotAdapter, OneBotConfig, "onebot", onebot_owns)):
            auth = ({"app_id": "test-app", "app_secret": "test-secret"} if name == "qq"
                    else {"base_url": "http://onebot.invalid"})
            config = cfg_cls(enabled=True, admin_id=["A1"],
                routes_file=str(Path(tmp.name) / (name + "-routes.json")),
                session_map_file=str(Path(tmp.name) / (name + "-map.json")),
                delivered_file=str(Path(tmp.name) / (name + "-delivered.jsonl")),
                push_state_file=str(Path(tmp.name) / (name + "-push.json")),
                access_requests=AccessRequestsConfig(state_file=str(Path(tmp.name) / (name + "-access.json"))), **auth)
            adapter = factory(config, service, gateway=gateway)
            adapter.client.send_message.return_value = (200, {"id": "notice"})
            adapter.client.send_msg.return_value = {"message_id": 42}
            if owns:
                adapter.session_mapper.bind("private", "A1", "S")
            adapter.ctx.notify_interaction = mock.AsyncMock(wraps=adapter.ctx.notify_interaction)
            adapter.ctx.send_notice = mock.AsyncMock(wraps=adapter.ctx.send_notice)
            mounted.append(adapter)
        self.addCleanup(service.interactions.close)
        return gateway, *mounted

    async def test_card_goes_only_to_the_adapter_owning_the_session(self):
        gateway, qq, ob = self._gateway(qq_owns=False, onebot_owns=True)
        item = gateway.muse_service.interactions.add("credential", session_id="S")
        self.assertTrue(await gateway.notify_interaction(item, "card"))
        ob.notify_interaction.assert_awaited_once_with(item, "card")
        qq.notify_interaction.assert_not_awaited()

    async def test_unowned_session_is_not_broadcast(self):
        gateway, qq, ob = self._gateway(qq_owns=False, onebot_owns=False)
        item = gateway.muse_service.interactions.add("credential", session_id="MAIN")
        self.assertFalse(await gateway.notify_interaction(item, "card"))
        qq.notify_interaction.assert_not_awaited()
        ob.notify_interaction.assert_not_awaited()
        qq.client.send_message.assert_not_called()
        ob.client.send_msg.assert_not_called()

    async def test_login_notice_broadcasts_to_all_enabled_adapters(self):
        gateway, qq, ob = self._gateway(False, False)
        await gateway.login_required()
        ob.send_notice.assert_awaited_once()
        self.assertEqual(ob.send_notice.await_args.args[0], "A1")
        qq.send_notice.assert_awaited_once()
        self.assertEqual(qq.send_notice.await_args.args[0], "A1")
        self.assertTrue(ob.ctx._in_login_fallback)
        self.assertTrue(qq.ctx._in_login_fallback)
        ob.client.send_msg.assert_called_once()
        qq.client.send_message.assert_called_once()


class TestOneBotTyping(unittest.IsolatedAsyncioTestCase):
    async def test_private_full_turn_typing_persists_without_flicker_and_skips_groups_or_strangers(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, typing_enabled=True)
            started = asyncio.Event()
            calls = []

            async def fake_send_status(user, event_type, timeout=5):
                calls.append((user, event_type))
                if event_type == 1:
                    started.set()

            adapter.adapter._send_input_status = fake_send_status

            # Unauthorized private DMs, group messages and local commands never trigger typing
            await adapter.handle_event(_private(999, "stranger", 1))
            await adapter.handle_event(_group("G1", 5, "group hello", 2))
            await adapter.handle_event(_private(100, "/help", 3))
            await asyncio.sleep(0)
            self.assertEqual(calls, [])

            # Authorized private message starts typing and binds to the resolved Muse session
            await adapter.handle_event(_private(100, "hello", 4))
            await started.wait()
            sid = adapter.session_mapper.current("private", "100")
            self.assertIn("100", adapter.adapter._typing_tasks)
            self.assertEqual(adapter.adapter._typing_sessions.get("100"), sid)
            self.assertEqual(calls, [("100", 1)])

            # Sharing the same session with a group still only sends typing to the private user
            adapter.session_mapper.bind("group", "G1", sid)
            task_before = adapter.adapter._typing_tasks["100"]
            await adapter.handle_event(_private(100, "follow up", 5))
            self.assertIs(adapter.adapter._typing_tasks["100"], task_before)
            self.assertNotIn("G1", adapter.adapter._typing_tasks)
            self.assertEqual(calls, [("100", 1)])

            # Finishing the turn stops typing for the private user only
            await adapter.outbound_pusher.on_turn_finished(sid)
            self.assertNotIn("100", adapter.adapter._typing_tasks)
            self.assertNotIn("100", adapter.adapter._typing_sessions)
            self.assertEqual(calls, [("100", 1), ("100", 2)])

    async def test_typing_stops_on_failed_post_commands_cards_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, typing_enabled=True)
            calls = []

            async def fake_send_status(user, event_type, timeout=5):
                calls.append((user, event_type))

            adapter.adapter._send_input_status = fake_send_status

            # 1. Failed post_message cleans up intake typing immediately
            muse.post_message.side_effect = RuntimeError("upstream down")
            await adapter.handle_event(_private(100, "fail", 10))
            await asyncio.sleep(0)
            self.assertNotIn("100", adapter.adapter._typing_tasks)
            if calls:
                self.assertEqual(calls[-1], ("100", 2))

            # 2. /停止 cancels active typing
            calls.clear()
            muse.post_message.side_effect = lambda sid, text, atts, thr: f"mid-{sid}-{text}"
            await adapter.handle_event(_private(100, "work", 11))
            await asyncio.sleep(0)
            self.assertIn("100", adapter.adapter._typing_tasks)
            await adapter.handle_event(_private(100, "/停止", 12))
            self.assertNotIn("100", adapter.adapter._typing_tasks)
            self.assertEqual(calls, [("100", 1), ("100", 2)])

            # 3. /新建 cancels active typing
            calls.clear()
            await adapter.handle_event(_private("A1", "work", 13))
            await asyncio.sleep(0)
            self.assertIn("A1", adapter.adapter._typing_tasks)
            await adapter.handle_event(_private("A1", "/新建", 14))
            self.assertNotIn("A1", adapter.adapter._typing_tasks)
            self.assertEqual(calls, [("A1", 1), ("A1", 2)])

            # 4. Approval pause and adapter.stop() clear active typing
            calls.clear()
            await adapter.handle_event(_private("A1", "ssh check", 15))
            await asyncio.sleep(0)
            sid = adapter.session_mapper.current("private", "A1")
            approval = muse.interactions.add("approval", session_id=sid, context={"fingerprint": "fp"})
            muse.interaction_protocol.display_snapshot.return_value = ("fp", [])
            self.assertTrue(await adapter.notify_interaction(approval, "【审批】"))
            self.assertNotIn("A1", adapter.adapter._typing_tasks)
            self.assertEqual(calls, [("A1", 1), ("A1", 2)])
            approval.state = "denied"

            calls.clear()
            await adapter.handle_event(_private("A1", "before stop", 16))
            await asyncio.sleep(0)
            self.assertIn("A1", adapter.adapter._typing_tasks)
            await adapter.adapter.stop()
            self.assertFalse(adapter.adapter._typing_tasks)
            self.assertEqual(calls, [("A1", 1), ("A1", 2)])

    async def test_typing_failure_backs_off_without_sending_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = _make_adapter(tmp, typing_enabled=True)
            calls = []

            async def failing_status(user, event_type, timeout=5):
                calls.append((user, event_type))
                raise OneBotAPIError("set_input_status", 1404, "unsupported action")

            adapter.adapter._send_input_status = failing_status
            await adapter.handle_event(_private(100, "first", 20))
            await asyncio.sleep(0)
            await adapter.handle_event(_private(100, "second", 21))
            await asyncio.sleep(0)
            self.assertEqual(calls, [("100", 1)])
            self.assertIn("100", adapter.adapter._typing_retry)
