"""Offline safety and scheduling contracts for the opt-in Qzone feed worker."""
import asyncio
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from muse_bridge.adapters.onebot.qzone import QzoneFeedWorker
from muse_bridge.config.models import ConfigLoader, QzoneFeedConfig
from muse_bridge.muse.feed import FeedPage, FeedPost


class TestQzoneFeedWorker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = 10000.0
        self.config = QzoneFeedConfig(enabled=True, state_file=str(self.path))
        self.client = mock.Mock()
        self.client.get_login_info.return_value = {"user_id": 123}
        self.service = mock.Mock()
        self.service.list_feed.return_value = FeedPage("source", [])
        self.service.prepare_feed_images.side_effect = lambda post, limit: post.images
        self.worker = self.make_worker()

    def make_worker(self):
        worker = QzoneFeedWorker(self.config, self.client, self.service,
                                 clock=lambda: self.now)

        async def advance(seconds):
            self.now += seconds
            return True

        worker._wait = mock.AsyncMock(side_effect=advance)
        return worker

    def posts(self, *posts):
        self.service.list_feed.return_value = FeedPage("source", list(posts))

    def post(self, key, age=0, **kwargs):
        return FeedPost(key, self.now - age, kwargs.pop("text", key), **kwargs)

    def sent(self):
        return [call.args[0] for call in self.client.send_qzone_msg.call_args_list]

    async def test_disabled_has_no_calls_or_state_io(self):
        self.config.enabled = False
        with mock.patch.object(self.worker, "_load") as load, \
                mock.patch.object(self.worker, "_save") as save:
            self.assertEqual(await self.worker.scan_once(), 0)
            await self.worker.run()
        self.client.assert_not_called()
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.service.mock_calls, [])
        load.assert_not_called()
        save.assert_not_called()
        self.worker._wait.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_startup_scans_immediately_and_shutdown_interrupts_poll(self):
        worker = QzoneFeedWorker(self.config, self.client, self.service)
        entered_wait = asyncio.Event()
        original_wait = worker._wait

        async def wait(seconds):
            entered_wait.set()
            return await original_wait(seconds)

        worker._wait = wait
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            self.service.list_feed.assert_called_once_with(self.config.scan_limit)
            await worker.stop()
            await asyncio.wait_for(task, timeout=2)
            self.assertEqual(self.service.list_feed.call_count, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_scan_window_rejects_stale_future_and_blank(self):
        self.posts(self.post("stale", self.config.max_age_s + 1),
                   self.post("future", -1), self.post("blank", text=" \n"),
                   self.post("boundary", self.config.max_age_s))
        self.assertEqual(await self.worker.scan_once(), 1)
        self.assertEqual(self.sent(), ["boundary"])

    async def test_newest_bounded_selection_is_chronological(self):
        self.posts(*(self.post(str(i), age=100 - i) for i in (2, 0, 4, 1, 3)))
        self.assertEqual(await self.worker.scan_once(), 3)
        self.assertEqual(self.sent(), ["2", "3", "4"])
        self.service.list_feed.assert_called_once_with(50)

    async def test_duplicate_ids_send_once_and_forward_images_permissions(self):
        post = self.post("same", images=["https://example.test/photo.jpg"])
        self.posts(post, post)
        self.assertEqual(await self.worker.scan_once(), 1)
        self.client.send_qzone_msg.assert_called_once_with(
            "same", images=post.images, ugc_right=4)

    async def test_durable_dedup_survives_restart(self):
        self.posts(self.post("one"))
        self.assertEqual(await self.worker.scan_once(), 1)
        self.assertTrue(self.path.exists())
        self.assertEqual(await self.make_worker().scan_once(), 0)
        self.assertEqual(self.sent(), ["one"])

    async def test_minimum_spacing_across_scans_and_restart(self):
        timestamps = []
        self.client.send_qzone_msg.side_effect = lambda *a, **k: timestamps.append(self.now)
        for key, worker in (("a", self.worker), ("b", self.worker),
                            ("c", self.make_worker())):
            self.posts(self.post(key))
            self.assertEqual(await worker.scan_once(), 1)
        self.assertEqual(timestamps, [10000, 10060, 10120])
        self.worker._wait.assert_awaited_once_with(60)

    async def test_rolling_quota_across_scans_restart_and_expiry(self):
        self.config.max_per_window = 2
        self.posts(self.post("a"))
        await self.worker.scan_once()
        self.posts(self.post("b"))
        await self.worker.scan_once()
        self.posts(self.post("c"))
        self.assertEqual(await self.worker.scan_once(), 0)
        restarted = self.make_worker()
        self.assertEqual(await restarted.scan_once(), 0)
        self.now = 10000 + self.config.publish_window_s
        self.posts(self.post("c"))
        self.assertEqual(await restarted.scan_once(), 1)
        self.assertEqual(self.sent(), ["a", "b", "c"])

    async def test_corrupt_and_foreign_state_fail_closed(self):
        identity = {"onebot": "123", "muse": "source"}
        bad_states = ["not json", "[]", json.dumps({"version": 2, "identity": identity, "attempts": {}})]
        for foreign in ({"onebot": "456", "muse": "source"},
                        {"onebot": "123", "muse": "other"}):
            bad_states.append(json.dumps({"version": 1, "identity": foreign, "attempts": {}}))
        for attempts in ([], {"": 1}, {"x": True}, {"x": -1},
                         {"x": "100"}, {"x": float("nan")}, {"x": float("inf")}):
            bad_states.append(json.dumps({"version": 1, "identity": identity, "attempts": attempts}))
        self.posts(self.post("one"))
        for contents in bad_states:
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    await self.make_worker().scan_once()
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)
        self.client.send_qzone_msg.assert_not_called()

    async def test_persistence_failure_prevents_send(self):
        self.posts(self.post("one"))
        with mock.patch.object(self.worker, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.worker.scan_once()
        self.client.send_qzone_msg.assert_not_called()

    async def test_send_failure_or_uncertain_outcome_is_reserved_after_restart(self):
        self.posts(self.post("one"))
        for error in (RuntimeError("rejected"), TimeoutError("outcome unknown")):
            with self.subTest(error=error):
                self.path.unlink(missing_ok=True)
                self.client.send_qzone_msg.reset_mock()
                self.client.send_qzone_msg.side_effect = error
                with self.assertRaises(type(error)):
                    await self.make_worker().scan_once()
                self.assertIn("one", json.loads(self.path.read_text())["attempts"])
                self.client.send_qzone_msg.side_effect = None
                self.assertEqual(await self.make_worker().scan_once(), 0)
                self.assertEqual(self.client.send_qzone_msg.call_count, 1)

    async def test_image_preparation_failure_does_not_publish_partial_post(self):
        self.posts(self.post("with-image", images=["/feed/units/with-image/media/a"]))
        self.service.prepare_feed_images.side_effect = TimeoutError("image unavailable")
        with self.assertRaises(TimeoutError):
            await self.worker.scan_once()
        self.client.send_qzone_msg.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_stop_before_worker_scheduled_does_not_clear_stop(self):
        await self.worker.stop()
        await self.worker.run()
        self.service.list_feed.assert_not_called()

    async def test_stop_during_image_preparation_prevents_publish(self):
        self.posts(self.post("with-image", images=["/feed/units/with-image/media/a"]))
        self.service.prepare_feed_images.side_effect = lambda *a: self.worker._stop.set() or []
        self.assertEqual(await self.worker.scan_once(), 0)
        self.client.send_qzone_msg.assert_not_called()
        self.assertFalse(self.path.exists())

    async def test_service_failure_never_sends_or_writes_state(self):
        self.service.list_feed.side_effect = RuntimeError("feed unavailable")
        with self.assertRaises(RuntimeError):
            await self.worker.scan_once()
        self.client.send_qzone_msg.assert_not_called()
        self.assertFalse(self.path.exists())


class TestQzoneFeedConfig(unittest.TestCase):
    def test_defaults_are_opt_in_and_bounded(self):
        self.assertEqual(dataclasses.asdict(QzoneFeedConfig()), {
            "enabled": False, "poll_interval_s": 300, "max_age_s": 3600,
            "scan_limit": 50, "max_per_scan": 3, "min_publish_interval_s": 60,
            "publish_window_s": 3600, "max_per_window": 5, "ugc_right": 4,
            "state_file": "data/onebot_qzone_feed.local.json",
        })

    def test_enabled_is_strict_boolean(self):
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                QzoneFeedConfig(enabled=value)
        for value in (True, False):
            self.assertIs(QzoneFeedConfig(enabled=value).enabled, value)

    def test_positive_integers_are_strict(self):
        for name in ("poll_interval_s", "max_age_s", "scan_limit", "max_per_scan",
                     "min_publish_interval_s", "publish_window_s", "max_per_window"):
            for value in (True, False, 0, -1, 1.0, "1", None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    QzoneFeedConfig(**{name: value})

    def test_bounds_permissions_and_state_path(self):
        for kwargs in ({"scan_limit": 101}, {"scan_limit": 2, "max_per_scan": 3},
                       *({"ugc_right": v} for v in (True, 1.0, "4", 0, 16, 128)),
                       *({"state_file": v} for v in (None, "", " \n", 123))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                QzoneFeedConfig(**kwargs)
        for permission in (1, 4, 64):
            self.assertEqual(QzoneFeedConfig(ugc_right=permission).ugc_right, permission)

    def test_loader_accepts_fields_and_rejects_unknown_or_coerced_values(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("muse_bridge.config.models._detect_egress_ip",
                           side_effect=AssertionError("must not detect egress")) as detect:
            path = Path(directory) / "config.json"
            # Isolate the optional cwd-relative const.json without changing cwd.
            original_exists = __import__("os").path.exists
            with mock.patch("muse_bridge.config.models.os.path.exists",
                            side_effect=lambda p: False if p == "const.json" else original_exists(p)):
                for feed in ({}, {"enabled": True, "max_per_scan": 2},
                             {"unknown_field": 1}, {"enabled": "false"},
                             {"scan_limit": "50"}, {"max_per_window": True}, [], None):
                    with self.subTest(feed=feed):
                        path.write_text(json.dumps({"muse": {"client_ip": "127.0.0.1"},
                            "adapters": {"onebot": {"qzone_feed": feed}}}), encoding="utf-8")
                        if feed == {} or (isinstance(feed, dict) and feed.get("enabled") is True):
                            loaded = ConfigLoader.load(str(path)).onebot.qzone_feed
                            self.assertEqual(loaded, QzoneFeedConfig(**feed))
                        else:
                            with self.assertRaises(ValueError):
                                ConfigLoader.load(str(path))
            detect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
