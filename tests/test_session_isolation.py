"""Tests for per-chat Muse session isolation."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.adapters.qq.outbound import QQOutboundPusher
from muse_bridge.adapters.qq.route_manager import RouteManager
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, QQConfig
from muse_bridge.core.session_map import SessionMapper, chat_key


class TestSessionMapper(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.map_file = os.path.join(self._tmp.name, "map.json")

    def _ids(self):
        counter = iter(range(1, 100))
        return lambda: f"sid-{next(counter)}"

    def test_per_chat_isolates_and_is_stable(self):
        m = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        a, created_a = m.resolve("group", "A")
        b, created_b = m.resolve("group", "B")
        u, created_u = m.resolve("private", "U")
        self.assertTrue(created_a and created_b and created_u)
        self.assertEqual(len({a, b, u}), 3)
        # Stable on repeat
        self.assertEqual(m.resolve("group", "A"), (a, False))
        # Same id string in different chat types stays separate
        self.assertNotEqual(m.resolve("group", "X")[0], m.resolve("private", "X")[0])

    def test_per_chat_persists_across_restart(self):
        m1 = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        a, _ = m1.resolve("group", "A")
        m2 = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        self.assertEqual(m2.resolve("group", "A"), (a, False))

    def test_config_bindings_seed_but_runtime_wins(self):
        m = SessionMapper(state_file=self.map_file,
                          bindings={chat_key("group", "A"): "PRE"}, id_factory=self._ids())
        self.assertEqual(m.resolve("group", "A"), ("PRE", False))
        m.bind("group", "A", "SWITCHED")
        m2 = SessionMapper(state_file=self.map_file,
                           bindings={chat_key("group", "A"): "PRE"}, id_factory=self._ids())
        self.assertEqual(m2.resolve("group", "A")[0], "SWITCHED")

    def test_current_does_not_create(self):
        m = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        self.assertEqual(m.current("group", "A"), "")
        self.assertEqual(m.session_ids(), [])

    def test_bind_only_affects_that_chat(self):
        m = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        a, _ = m.resolve("group", "A")
        b, _ = m.resolve("group", "B")
        m.bind("group", "A", "OTHER")
        self.assertEqual(m.current("group", "A"), "OTHER")
        self.assertEqual(m.current("group", "B"), b)
        self.assertEqual(set(m.session_ids()), {"OTHER", b})

    def test_owners_reverse_lookup(self):
        m = SessionMapper(state_file=self.map_file, id_factory=self._ids())
        a, _ = m.resolve("group", "A")
        self.assertEqual(m.owners(a), [("group", "A")])
        self.assertEqual(m.owners("nope"), [])


class TestSessionConfig(unittest.TestCase):
    def _load(self, qq: dict):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"muse": {"client_ip": "1.1.1.1"}, "adapters": {"qq": qq}}, f)
            return ConfigLoader.load(p)

    def test_session_options_loaded(self):
        cfg = self._load({
            "app_id": "a",
            "session_map_file": "m.json",
            "session_bindings": {"group:G1": "S1"},
            "session_hot_window_s": 10,
        })
        self.assertEqual(cfg.qq.session_map_file, "m.json")
        self.assertEqual(cfg.qq.session_bindings, {"group:G1": "S1"})
        self.assertEqual(cfg.qq.session_hot_window_s, 10)

    def test_push_behavior_defaults(self):
        cfg = self._load({"app_id": "a"})
        self.assertEqual(cfg.qq.push_state_file, "data/qq_push_state.json")
        self.assertFalse(cfg.qq.catch_up_enabled)
        self.assertEqual(cfg.qq.catch_up_window_s, 6 * 3600)
        self.assertEqual(cfg.qq.catch_up_max_messages, 5)
        self.assertEqual(cfg.qq.recheck_sleep_s, 5)
        self.assertEqual(cfg.qq.in_flight_ttl_s, 300)
        self.assertEqual(cfg.qq.processing_ttl_s, 300)

    def test_flat_push_keys_loaded_and_old_push_block_ignored(self):
        cfg = self._load({
            "app_id": "a",
            "push_state_file": "p.json",
            "catch_up_enabled": True,
            "catch_up_window_s": 60,
            "catch_up_max_messages": 2,
            "recheck_sleep_s": 7,
            "in_flight_ttl_s": 11,
            "processing_ttl_s": 12,
            "session_mode": "global",
            "push": {"target_type": "group", "target_id": "G", "state_file": "old.json",
                     "catch_up_enabled": False, "catch_up_window_s": 999},
        })
        q = cfg.qq
        self.assertEqual(q.push_state_file, "p.json")
        self.assertTrue(q.catch_up_enabled)
        self.assertEqual((q.catch_up_window_s, q.catch_up_max_messages), (60, 2))
        self.assertEqual((q.recheck_sleep_s, q.in_flight_ttl_s, q.processing_ttl_s), (7, 11, 12))
        self.assertFalse(hasattr(q, "push"))
        self.assertFalse(hasattr(q, "session_mode"))

    def test_admin_target_skips_wildcard_and_empty(self):
        self.assertEqual(QQConfig(admin_id=["", "*", "A1", "A2"]).admin_target(), "A1")
        self.assertIsNone(QQConfig(admin_id=["*"]).admin_target())
        self.assertIsNone(QQConfig().admin_target())

    def test_unbound_push_to_admin_flag_defaults_and_validates(self):
        self.assertTrue(QQConfig().unbound_push_to_admin)
        with self.assertRaises(ValueError):
            QQConfig(unbound_push_to_admin="false")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"adapters": {"qq": {"unbound_push_to_admin": False}}}, f)
            self.assertFalse(ConfigLoader.load(path).qq.unbound_push_to_admin)


def _make_adapter(tmp, **extra):
    cfg = QQConfig(
        app_id="1", app_secret="s",
        whitelist={"private": ["U1"], "GA": "*", "GB": "*"},
        admin_id=["U1"], admin_only=False,
        access_requests=AccessRequestsConfig(state_file=os.path.join(tmp, "access.local.json")),
        session_map_file=os.path.join(tmp, "map.json"),
        delivered_file=os.path.join(tmp, "delivered.jsonl"),
        push_state_file=os.path.join(tmp, "push.json"),
        **extra,
    )
    from muse_bridge.core.interactions import InteractionManager
    muse = mock.Mock()
    muse.interactions = InteractionManager()
    muse.post_message = mock.Mock(side_effect=lambda sid, text, atts, thr: f"mid-{sid}-{text}")
    adapter = QQAdapter(config=cfg, muse_service=muse)
    adapter.route_manager = RouteManager(state_file=None)
    adapter.outbound_pusher.route_manager = adapter.route_manager
    adapter.client = mock.Mock()
    adapter.client.send_message = mock.Mock(return_value=(200, "ok"))
    adapter.outbound_pusher.ensure_baseline = mock.AsyncMock()
    return adapter, muse


def _group_event(gid, uid, text, mid):
    return {"id": mid, "group_openid": gid, "content": text, "author": {"user_openid": uid}}


class TestInboundIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_per_chat_routes_groups_to_different_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "hi A", "m1"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GB", "y", "hi B", "m2"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "z", "again A", "m3"))
            await adapter.handle_event("C2C_MESSAGE_CREATE",
                                       {"id": "m4", "content": "hi priv", "author": {"user_openid": "U1"}})

            sids = {c.args[1]: c.args[0] for c in muse.post_message.call_args_list}
            sa1 = muse.post_message.call_args_list[0].args[0]
            sb = muse.post_message.call_args_list[1].args[0]
            sa2 = muse.post_message.call_args_list[2].args[0]
            sp = muse.post_message.call_args_list[3].args[0]
            self.assertEqual(sa1, sa2)
            self.assertEqual(len({sa1, sb, sp}), 3)
            self.assertNotIn("GLOBAL", {sa1, sb, sp})
            # routes registered per chat
            self.assertEqual(adapter.route_manager.get(f"mid-{sa1}-hi A").target_id, "GA")
            self.assertEqual(adapter.route_manager.get(f"mid-{sb}-hi B").target_id, "GB")
            self.assertEqual(adapter.route_manager.get(f"mid-{sp}-hi priv").target_type, "private")
            # outbound watches all three
            self.assertEqual(set(adapter.session_mapper.session_ids()), {sa1, sb, sp})

    async def test_switch_to_shared_session_joins_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.session_mapper.bind("group", "GA", "SHARED")
            adapter.session_mapper.bind("group", "GB", "SHARED")
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "hi A", "m1"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GB", "y", "hi B", "m2"))
            self.assertEqual([c.args[0] for c in muse.post_message.call_args_list], ["SHARED", "SHARED"])

    async def test_new_command_rebinds_only_this_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "a", "m1"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GB", "y", "b", "m2"))
            before_a = adapter.session_mapper.current("group", "GA")
            before_b = adapter.session_mapper.current("group", "GB")
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "/new", "m3"))
            self.assertNotEqual(adapter.session_mapper.current("group", "GA"), before_a)
            self.assertEqual(adapter.session_mapper.current("group", "GB"), before_b)

    async def test_switch_command_baselines_target_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            fake_client = mock.MagicMock()
            fake_client.list_sessions.return_value = [
                {"session_id": "EXIST-1111", "updated_at_ms": 5, "title": "t"},
            ]
            muse.create_client.return_value = fake_client
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "/switch 1", "m1"))
            self.assertEqual(adapter.session_mapper.current("group", "GA"), "EXIST-1111")
            adapter.outbound_pusher.ensure_baseline.assert_awaited_with("EXIST-1111")

    async def test_admin_cmd_blocked_for_non_admin_when_admin_only(self):
        """Regression: is_admin used to be an undefined name (NameError)."""
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            adapter.config.admin_only = True
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "/new", "m1"))
            text = adapter.client.send_message.call_args_list[-1].args[2]
            self.assertIn("权限不足", text)
            self.assertEqual(adapter.session_mapper.current("group", "GA"), "")


class _FakeMuseClient:
    def __init__(self, listing, hist=None, fail_list=False):
        self._listing = listing
        self._hist = hist or {}
        self.fail_list = fail_list
        self.history_calls = []

    def connect(self):
        pass

    def list_sessions(self):
        if self.fail_list:
            raise RuntimeError("boom")
        return [{"session_id": k, "updated_at_ms": v} for k, v in self._listing.items()]

    def get_history(self, sid, limit):
        self.history_calls.append(sid)
        return self._hist.get(sid, [])


def _pusher(tmp, **cfg_extra):
    cfg_extra.setdefault("admin_id", ["ADMIN"])
    cfg = QQConfig(
        app_id="1", app_secret="s",
        whitelist={"private": ["ADMIN", "WL_USER"]},
        session_hot_window_s=30, session_sweep_s=60,
        push_state_file=os.path.join(tmp, "push.json"),
        **cfg_extra,
    )
    mapper = SessionMapper(state_file=os.path.join(tmp, "map.json"))
    dedup = mock.Mock()
    dedup.is_delivered.return_value = False
    dedup.is_processing.return_value = False
    p = QQOutboundPusher(config=cfg, client=mock.Mock(), muse_service=mock.Mock(),
                         deduplicator=dedup, route_manager=RouteManager(state_file=None),
                         session_mapper=mapper)
    return p, mapper


class TestOutboundChangeDetection(unittest.TestCase):
    def test_per_chat_only_fetches_changed_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            b, _ = m.resolve("group", "B")
            p._gated.update({a, b})
            p._last_sweep = time.monotonic()  # no sweep due
            c = _FakeMuseClient({a: 100, b: 100})
            # first sight of a/b: updated > -1 so both fetched once
            self.assertEqual(set(p._sessions_to_fetch(c)), {a, b})
            # hot window keeps them for now; expire it to test pure change detection
            p._hot_until.clear()
            self.assertEqual(p._sessions_to_fetch(c), [])
            c._listing[b] = 200   # B got new activity
            self.assertEqual(p._sessions_to_fetch(c), [b])
            p._hot_until.clear()
            self.assertEqual(p._sessions_to_fetch(c), [])

    def test_not_gated_session_always_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            p._last_sweep = time.monotonic()
            self.assertEqual(p._sessions_to_fetch(_FakeMuseClient({a: 1})), [a])

    def test_hot_session_fetched_without_list_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            p._gated.add(a)
            p._last_sweep = time.monotonic()
            p._last_updated[a] = 50
            c = _FakeMuseClient({a: 50})
            self.assertEqual(p._sessions_to_fetch(c), [])
            p.track_session(a)
            self.assertEqual(p._sessions_to_fetch(c), [a])

    def test_sweep_fetches_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            b, _ = m.resolve("group", "B")
            p._gated.update({a, b})
            p._last_updated.update({a: 5, b: 5})
            p._last_sweep = 0.0
            self.assertEqual(set(p._sessions_to_fetch(_FakeMuseClient({a: 5, b: 5}))), {a, b})

    def test_list_failure_falls_back_to_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            b, _ = m.resolve("group", "B")
            p._gated.update({a, b})
            p._last_sweep = time.monotonic()
            self.assertEqual(set(p._sessions_to_fetch(_FakeMuseClient({}, fail_list=True))), {a, b})

    def test_no_sessions_no_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp)
            self.assertEqual(p._sessions_to_fetch(_FakeMuseClient({})), [])

    def test_streaming_message_keeps_session_hot(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "A")
            hist = [{"role": "assistant", "event_name": "message.assistant", "message_id": "x1",
                     "payload": {"status": None}, "display_text": "par"}]
            p._fetch_session(_FakeMuseClient({}, {a: hist}), a)
            self.assertGreater(p._hot_until.get(a, 0), time.monotonic())


class TestOutboundRouting(unittest.IsolatedAsyncioTestCase):
    def _assistant(self, mid, text, exec_id=None):
        return {"role": "assistant", "event_name": "message.assistant", "message_id": mid,
                "seq": 10, "display_text": text,
                "payload": {"status": "completed", "metadata": {"root_message_execution_id": exec_id}}}

    async def test_proactive_goes_to_session_owner_in_per_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("private", "USER_A")
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch([self._assistant("r1", "提醒")], a)
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual((kw["target_type"], kw["target_id"]), ("private", "USER_A"))

    async def test_proactive_group_with_passive_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "GA")
            p.route_manager.register("u1", "group", "GA", "msg_id_123")
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch([self._assistant("r1_grp", "群提醒")], a)
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual((kw["target_type"], kw["target_id"]), ("group", "GA"))
            self.assertEqual(kw["route"].msg_id, "msg_id_123")

    async def test_proactive_for_unbound_session_goes_to_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp)
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch([self._assistant("r3", "提醒")], "UNBOUND")
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual((kw["target_type"], kw["target_id"]), ("private", "ADMIN"))

    async def test_unbound_push_disabled_is_dropped_even_with_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp, unbound_push_to_admin=False)
            p._send_to_qq = mock.AsyncMock(return_value=True)
            with self.assertLogs("muse_bridge", level="WARNING") as logs:
                await p._scan_and_dispatch([self._assistant("r6", "提醒")], "UNBOUND")
            p._send_to_qq.assert_not_called()
            self.assertTrue(any("unbound_push_to_admin is disabled" in line for line in logs.output))

    async def test_unbound_without_admin_is_dropped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp, admin_id=[])
            p._send_to_qq = mock.AsyncMock(return_value=True)
            with self.assertLogs("muse_bridge", level="WARNING") as logs:
                await p._scan_and_dispatch([self._assistant("r5", "提醒")], "UNBOUND")
            p._send_to_qq.assert_not_called()
            p.deduplicator.mark_processing.assert_not_called()
            self.assertTrue(any("no admin" in line for line in logs.output))

    async def test_group_rejection_falls_back_to_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "GA")
            p.client.send_message = mock.Mock(side_effect=[(400, '{"code": 40034105}'), (200, "ok")])
            await p._scan_and_dispatch([self._assistant("r6", "群提醒")], a)
            calls = p.client.send_message.call_args_list
            self.assertEqual(calls[0].args[:2], ("group", "GA"))
            self.assertEqual(calls[1].args[:2], ("private", "ADMIN"))
            self.assertIn("【群消息提醒】", calls[1].args[2])
            self.assertEqual(len(calls), 2)

    async def test_interactive_reply_uses_route_not_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp)
            a, _ = m.resolve("group", "GA")
            p.route_manager.register("u1", "group", "GA", "qqmsg")
            hist = [
                {"role": "user", "message_id": "u1", "seq": 9},
                self._assistant("r4", "答复", "e1"),
            ]
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch(hist, a)
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual(kw["target_id"], "GA")
            self.assertIsNotNone(kw["route"])

    async def test_ensure_baseline_marks_seen_and_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp)
            client = _FakeMuseClient({}, {"S": [self._assistant("old1", "old")]})
            cm = mock.MagicMock()
            cm.__enter__.return_value = client
            p.muse_service.create_client.return_value = cm
            await p.ensure_baseline("S")
            self.assertIn("old1", p._seen_ids)
            self.assertIn("S", p._gated)
            # second call is a no-op
            client.history_calls.clear()
            await p.ensure_baseline("S")
            self.assertEqual(client.history_calls, [])


if __name__ == "__main__":
    unittest.main()
