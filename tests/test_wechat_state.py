"""Offline account isolation and best-effort delivery recovery contracts."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muse_bridge.adapters.wechat.state import WeChatState, WeChatStateError


def credentials(bot="bot@im.bot", user="owner@im.wechat"):
    return {"bot_id": bot, "user_id": user, "bot_token": "synthetic-bot-token",
            "baseurl": "https://ilinkai.weixin.qq.com"}


class TestWeChatState(unittest.TestCase):
    def test_empty_login_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            self.assertFalse(state.status()["logged_in"])
            state.save_account(credentials())
            state.set_cursor("cursor-secret")
            state.remember_message("owner@im.wechat", "1", "context-secret")
            state.pause()
            restored = WeChatState(root)
            self.assertEqual(restored.credentials, credentials())
            self.assertEqual(restored.cursor, "cursor-secret")
            self.assertEqual(restored.context("owner@im.wechat"), "context-secret")
            self.assertEqual(restored.cooldown_until, state.cooldown_until)
            self.assertNotIn("secret", json.dumps(restored.status()))
            self.assertNotIn("bot_token", restored.status())

    def test_latest_snapshot_and_replay(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.remember_message("owner", "A", "TA")
            state.remember_message("owner", "B", "TB")
            state.remember_message("owner", "A", "TA")
            state.remember_message("owner", "C", "")
            self.assertEqual(state.context("owner", "A"), "TA")
            self.assertEqual(state.context("owner"), "TB")
            self.assertIsNone(state.context("other"))
            state.set_cursor("one")
            state.set_cursor("")
            self.assertEqual(state.cursor, "one")

    def test_switch_account_does_not_reuse_context_or_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.set_cursor("old")
            state.remember_message("owner", "A", "TA")
            old_dir = state.account_dir
            state.save_account(credentials("other@im.bot", "other@im.wechat"))
            self.assertNotEqual(state.account_dir, old_dir)
            self.assertEqual(state.cursor, "")
            self.assertIsNone(state.context("owner"))
            self.assertEqual(len(state.accounts()), 2)
            state.save_account(credentials())
            self.assertEqual(state.cursor, "old")

    def test_write_failure_does_not_publish_new_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.set_cursor("old")
            with mock.patch("muse_bridge.adapters.wechat.state.atomic_json_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    state.set_cursor("new")
            self.assertEqual(state.cursor, "old")
            self.assertEqual(WeChatState(root).cursor, "old")

    def test_corrupt_foreign_and_unsafe_account_refused(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            for origin in ("http://ilinkai.weixin.qq.com", "https://localhost", "https://evil.example",
                           "https://user@ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com:444"):
                with self.assertRaises(WeChatStateError):
                    state.save_account({**credentials(), "baseurl": origin})
            state.save_account(credentials())
            path = state.account_dir / "protocol.local.json"
            path.write_text('{"version": 1, "bot_id": "foreign"}', encoding="utf-8")
            with self.assertRaises(WeChatStateError):
                WeChatState(root)
            path.write_text('[]', encoding="utf-8")
            with self.assertRaises(WeChatStateError):
                WeChatState(root)

    def test_request_context_not_proactive_until_authorized_promotion(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.remember_request("new-user", "TR")
            self.assertIsNone(state.context("new-user"))
            state = WeChatState(root)
            state.promote_request("new-user")
            self.assertEqual(state.context("new-user"), "TR")
            state.remember_request("new-user", "old-request")
            state.remember_message("new-user", "new-message", "new-context")
            state.promote_request("new-user")
            self.assertEqual(state.context("new-user"), "new-context")
            self.assertNotIn("old-request", json.dumps(state.status()))

    def test_recover_inputs_and_sending_reservation_not_replayed(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.remember_message("owner", "A", "TA")
            state.reserve_part("part")
            state = WeChatState(root)
            state.recover_inputs()
            self.assertEqual(state.message_status("owner", "A"), "unknown")
            self.assertEqual(state.part_status("part"), "sending")
            with self.assertRaises(WeChatStateError):
                state.reserve_part("part")
            state.finish_part("part", "retryable")
            state.reserve_part("part")
            state.finish_part("part", "confirmed")
            self.assertEqual(WeChatState(root).part_status("part"), "confirmed")

    def test_unresolved_capacity_is_not_silently_evicted(self):
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.MAX_PARTS = 1
            state.reserve_part("unknown")
            with self.assertRaises(WeChatStateError):
                state.reserve_part("new")
            self.assertEqual(state.part_status("unknown"), "sending")
            self.assertIsNone(state.part_status("new"))


class TestWeChatPolling(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_only_after_intake(self):
        from muse_bridge.adapters.wechat.gateway import WeChatGateway
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            state.set_cursor("old")
            client = mock.Mock()
            client.get_updates = mock.AsyncMock(return_value={"msgs": [], "get_updates_buf": "new"})
            intake = mock.AsyncMock(side_effect=OSError("intake unavailable"))
            gateway = WeChatGateway(client, state, intake)
            async def end(_):
                gateway._stop.set()
            gateway._wait = end
            await gateway.start()
            self.assertEqual(state.cursor, "old")
            self.assertEqual(client.get_updates.await_count, 1)

    async def test_stop_cancels_long_poll(self):
        from muse_bridge.adapters.wechat.gateway import WeChatGateway
        with tempfile.TemporaryDirectory() as root:
            state = WeChatState(root)
            state.save_account(credentials())
            entered = asyncio.Event()
            async def poll(*_):
                entered.set()
                await asyncio.Event().wait()
            client = mock.Mock(get_updates=poll)
            gateway = WeChatGateway(client, state, mock.AsyncMock())
            task = asyncio.create_task(gateway.start())
            await entered.wait()
            await gateway.stop()
            self.assertTrue(task.done())
            self.assertEqual(gateway.phase, "stopped")


if __name__ == "__main__":
    unittest.main()
