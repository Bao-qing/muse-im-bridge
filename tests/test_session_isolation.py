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
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, QQConfig, QQPushConfig
from muse_bridge.core.session_map import MODE_GLOBAL, MODE_PER_CHAT, SessionMapper, chat_key


class TestSessionMapper(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.map_file = os.path.join(self._tmp.name, "map.json")
        self.global_file = os.path.join(self._tmp.name, "global.json")

    def _ids(self):
        counter = iter(range(1, 100))
        return lambda: f"sid-{next(counter)}"

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            SessionMapper(mode="weird")

    def test_global_mode_shares_one_session(self):
        m = SessionMapper(mode=MODE_GLOBAL, global_session_id="G", state_file=self.map_file)
        self.assertEqual(m.resolve("group", "A"), ("G", False))
        self.assertEqual(m.resolve("private", "U"), ("G", False))
        self.assertEqual(m.session_ids(), ["G"])
        self.assertEqual(m.owners("G"), [])
        self.assertFalse(os.path.exists(self.map_file))

    def test_global_bind_persists_to_global_file(self):
        m = SessionMapper(mode=MODE_GLOBAL, global_session_id="G", global_state_file=self.global_file)
        m.bind("group", "A", "NEW")
        self.assertEqual(m.resolve("private", "U")[0], "NEW")
        with open(self.global_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["session_id"], "NEW")

    def test_per_chat_isolates_and_is_stable(self):
        m = SessionMapper(mode=MODE_PER_CHAT, global_session_id="G",
                          state_file=self.map_file, id_factory=self._ids())
        a, created_a = m.resolve("group", "A")
        b, created_b = m.resolve("group", "B")
        u, created_u = m.resolve("private", "U")
        self.assertTrue(created_a and created_b and created_u)
        self.assertEqual(len({a, b, u}), 3)
        self.assertNotIn("G", {a, b, u})
        # Stable on repeat
        self.assertEqual(m.resolve("group", "A"), (a, False))
        # Same id string in different chat types stays separate
        self.assertNotEqual(m.resolve("group", "X")[0], m.resolve("private", "X")[0])

    def test_per_chat_persists_across_restart(self):
        m1 = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file, id_factory=self._ids())
        a, _ = m1.resolve("group", "A")
        m2 = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file, id_factory=self._ids())
        self.assertEqual(m2.resolve("group", "A"), (a, False))

    def test_config_bindings_seed_but_runtime_wins(self):
        m = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file,
                          bindings={chat_key("group", "A"): "PRE"}, id_factory=self._ids())
        self.assertEqual(m.resolve("group", "A"), ("PRE", False))
        m.bind("group", "A", "SWITCHED")
        m2 = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file,
                           bindings={chat_key("group", "A"): "PRE"}, id_factory=self._ids())
        self.assertEqual(m2.resolve("group", "A")[0], "SWITCHED")

    def test_current_does_not_create(self):
        m = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file, id_factory=self._ids())
        self.assertEqual(m.current("group", "A"), "")
        self.assertEqual(m.session_ids(), [])

    def test_bind_only_affects_that_chat(self):
        m = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file, id_factory=self._ids())
        a, _ = m.resolve("group", "A")
        b, _ = m.resolve("group", "B")
        m.bind("group", "A", "OTHER")
        self.assertEqual(m.current("group", "A"), "OTHER")
        self.assertEqual(m.current("group", "B"), b)
        self.assertEqual(set(m.session_ids()), {"OTHER", b})

    def test_owners_reverse_lookup(self):
        m = SessionMapper(mode=MODE_PER_CHAT, state_file=self.map_file, id_factory=self._ids())
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

    def test_defaults_keep_legacy_global_mode(self):
        cfg = self._load({"app_id": "a"})
        self.assertEqual(cfg.qq.session_mode, "global")
        self.assertTrue(cfg.qq.proactive_to_session_owner)

    def test_per_chat_options_loaded(self):
        cfg = self._load({
            "app_id": "a",
            "session_mode": "Per_Chat",
            "session_map_file": "m.json",
            "session_bindings": {"group:G1": "S1"},
            "proactive_to_session_owner": False,
            "session_hot_window_s": 10,
        })
        self.assertEqual(cfg.qq.session_mode, "per_chat")
        self.assertEqual(cfg.qq.session_map_file, "m.json")
        self.assertEqual(cfg.qq.session_bindings, {"group:G1": "S1"})
        self.assertFalse(cfg.qq.proactive_to_session_owner)
        self.assertEqual(cfg.qq.session_hot_window_s, 10)


def _make_adapter(tmp, mode, **extra):
    cfg = QQConfig(
        app_id="1", app_secret="s",
        whitelist={"private": ["U1"], "GA": "*", "GB": "*"},
        admin_id=["U1"], admin_only=False,
        session_id="GLOBAL", session_mode=mode,
        access_requests=AccessRequestsConfig(state_file=os.path.join(tmp, "access.local.json")),
        session_state_file=os.path.join(tmp, "qq_session.json"),
        session_map_file=os.path.join(tmp, "map.json"),
        delivered_file=os.path.join(tmp, "delivered.jsonl"),
        push=QQPushConfig(state_file=os.path.join(tmp, "push.json"),
                          delivered_file=os.path.join(tmp, "delivered.jsonl")),
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
            adapter, muse = _make_adapter(tmp, "per_chat")
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

    async def test_global_mode_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, "global")
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "hi A", "m1"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GB", "y", "hi B", "m2"))
            self.assertEqual([c.args[0] for c in muse.post_message.call_args_list], ["GLOBAL", "GLOBAL"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "map.json")))

    async def test_new_command_rebinds_only_this_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, "per_chat")
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "a", "m1"))
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GB", "y", "b", "m2"))
            before_a = adapter.session_mapper.current("group", "GA")
            before_b = adapter.session_mapper.current("group", "GB")
            await adapter.handle_event("GROUP_MESSAGE_CREATE", _group_event("GA", "x", "/new", "m3"))
            self.assertNotEqual(adapter.session_mapper.current("group", "GA"), before_a)
            self.assertEqual(adapter.session_mapper.current("group", "GB"), before_b)

    async def test_switch_command_baselines_target_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, "per_chat")
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
            adapter, muse = _make_adapter(tmp, "per_chat")
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


def _pusher(tmp, mode="per_chat", **cfg_extra):
    cfg = QQConfig(
        app_id="1", app_secret="s",
        admin_id=["ADMIN"], whitelist={"private": ["ADMIN"]},
        session_mode=mode, session_hot_window_s=30, session_sweep_s=60,
        push=QQPushConfig(state_file=os.path.join(tmp, "push.json")),
        **cfg_extra,
    )
    mapper = SessionMapper(mode=mode, global_session_id="GLOBAL",
                           state_file=os.path.join(tmp, "map.json"))
    dedup = mock.Mock()
    dedup.is_delivered.return_value = False
    dedup.is_processing.return_value = False
    p = QQOutboundPusher(config=cfg, client=mock.Mock(), muse_service=mock.Mock(),
                         deduplicator=dedup, route_manager=RouteManager(state_file=None),
                         session_id="GLOBAL", session_mapper=mapper)
    return p, mapper


class TestOutboundChangeDetection(unittest.TestCase):
    def test_global_mode_fetches_single_session_every_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp, mode="global")
            c = _FakeMuseClient({})
            self.assertEqual(p._sessions_to_fetch(c), ["GLOBAL"])
            self.assertEqual(p._sessions_to_fetch(c), ["GLOBAL"])

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

    async def test_proactive_to_admin_when_owner_routing_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, m = _pusher(tmp, proactive_to_session_owner=False)
            a, _ = m.resolve("group", "GA")
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch([self._assistant("r2", "提醒")], a)
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual((kw["target_type"], kw["target_id"]), ("private", "ADMIN"))

    async def test_global_mode_proactive_still_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pusher(tmp, mode="global")
            p._send_to_qq = mock.AsyncMock(return_value=True)
            await p._scan_and_dispatch([self._assistant("r3", "提醒")], "GLOBAL")
            kw = p._send_to_qq.call_args.kwargs
            self.assertEqual((kw["target_type"], kw["target_id"]), ("private", "ADMIN"))

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
