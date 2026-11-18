"""Offline event-driven approval authority, recovery and lifecycle tests."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from muse_bridge.config.models import ApprovalConfig, ConfigLoader, MuseConfig
from muse_bridge.muse.approval_stream import ApprovalEventStream
from muse_bridge.muse.client.client import MuseClient, PushSubscription
from muse_bridge.muse.client.errors import ConnectionLost
from muse_bridge.muse.interactions import ApprovalBlocked
from muse_bridge.muse.service import MuseService


def row(key="a", sid="s1", **changes):
    return {"approval_id": key, "status": "pending", "scope": {"thread_id": sid},
            "decision_options": [], **changes}


def event(pending=(), recent=(), seq=1, emitted=None):
    return {"type": "event", "event": "approvals.snapshot", "seq": seq,
            "payload": {"schema_version": 1, "emitted_at_ms": seq if emitted is None else emitted,
                        "pending_approvals": list(pending), "recent_approvals": list(recent)}}


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = MuseService(MuseConfig(session_id="s1", cookies_file=str(
            Path(self.tmp.name) / "cookies.json")), MagicMock())
        self.addCleanup(self.service.interactions.close)
        self.service.token_manager.has_credentials.return_value = True
        self.p = self.service.interaction_protocol
        self.p.request = MagicMock(return_value={"pending": [], "recent": []})
        self.generation = self.p.begin_approval_stream()

    def accept(self, *args, **kwargs):
        return self.p.accept_approval_event(event(*args, **kwargs), self.generation)


class SnapshotTests(Fixture, unittest.TestCase):
    def test_unknown_initial_state_blocks_without_network(self):
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")
        self.p.request.assert_not_called()

    def test_foreground_updates_local_gate_and_terminal_event_releases_it(self):
        self.assertTrue(self.accept([row()]))
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")
        self.service.check_approval_gate("s2")
        self.assertTrue(self.accept(recent=[row(status="approved")], seq=2))
        self.service.check_approval_gate("s1")
        self.p.request.assert_not_called()

    def test_post_uses_only_local_approval_state(self):
        self.accept()
        client = MagicMock()
        client.__enter__.return_value = client
        client.send_message.return_value = "sent"
        self.service.create_client = MagicMock(return_value=client)
        for _ in range(3):
            self.assertEqual(self.service.post_message("s1", "hello"), "sent")
        self.p.request.assert_not_called()
        self.accept([row()], seq=2)
        with self.assertRaises(ApprovalBlocked):
            self.service.post_message("s1", "blocked")
        self.assertEqual(client.send_message.call_count, 3)

    def test_background_does_not_own_chat_or_input(self):
        self.accept([row(scope={"scope_type": "schedule", "requester_source": "scheduler.cron"})])
        self.service.check_approval_gate("s1")
        item = self.p.manager.pending_approvals()[0]
        self.assertEqual(item.session_id, "")
        self.assertTrue(item.context["background_approval"])
        self.assertEqual(self.p.manager.focus, {})

    def test_unmanaged_rows_are_projected_when_binding_arrives(self):
        self.accept([row(sid="new")])
        self.assertEqual(self.p.manager.pending_approvals(), [])
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("new")
        self.p.request.assert_not_called()

    def test_reconnect_rejects_old_generation_and_resets_order(self):
        self.accept(seq=100)
        old = self.generation
        self.generation = self.p.begin_approval_stream()
        self.assertFalse(self.p.manager.approvals_ready)
        self.assertFalse(self.p.accept_approval_event(event(seq=101), old))
        self.assertTrue(self.accept(seq=1))
        self.p.end_approval_stream(old)
        self.assertTrue(self.p.manager.approvals_ready)
        self.p.end_approval_stream(self.generation)
        self.assertFalse(self.p.manager.approvals_ready)
        self.assertFalse(self.accept(seq=2))

    def test_duplicate_or_out_of_order_snapshots_cannot_open_gate(self):
        self.accept([row()], seq=5, emitted=50)
        for seq, emitted in ((5, 50), (4, 60), (6, 49)):
            self.assertFalse(self.accept(seq=seq, emitted=emitted))
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")

    def test_malformed_snapshot_fails_closed_and_can_recover(self):
        bad_events = []
        for key, value in (("schema_version", True), ("schema_version", 2),
                           ("pending_approvals", {}), ("recent_approvals", None),
                           ("emitted_at_ms", True)):
            bad = event(seq=2)
            bad["payload"][key] = value
            bad_events.append(bad)
        bad_events.extend([event([row(scope={})], seq=2),
                           event([row(scope={"thread_id": 42})], seq=2),
                           event([row(), row()], seq=2),
                           event([row(message_revision=-1)], seq=2)])
        for bad in bad_events:
            with self.subTest(bad=bad):
                self.generation = self.p.begin_approval_stream()
                self.accept()
                self.assertFalse(self.p.accept_approval_event(bad, self.generation))
                self.assertFalse(self.p.manager.approvals_ready)
                self.assertTrue(self.p._needs_reconcile.is_set())
                self.assertTrue(self.accept(seq=3))

    def test_missing_bounded_recent_row_keeps_gate_until_detail_resolves(self):
        self.accept([row()])
        self.p._needs_reconcile.clear()
        self.accept(seq=2)
        self.assertTrue(self.p._needs_reconcile.is_set())
        with self.assertRaises(ApprovalBlocked):
            self.service.check_approval_gate("s1")
        self.p.request.side_effect = [{"pending": []}, {"approval": row(status="approved")}]
        self.p.fetch_approvals()
        self.service.check_approval_gate("s1")
        self.assertEqual(self.p.request.call_args.args, ("GET", "/approvals/a"))

    def test_partial_snapshot_never_releases_existing_gate(self):
        self.accept([row()])
        bad = event([row("new"), row("bad", message_revision=-1)],
                    [row(status="approved")], seq=2)
        self.assertFalse(self.p.accept_approval_event(bad, self.generation))
        self.assertFalse(self.p.manager.by_remote("approval", "a").terminal)
        self.assertIsNone(self.p.manager.by_remote("approval", "new"))

    def test_stale_fetch_success_or_failure_cannot_overwrite_new_event(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.generation = self.p.begin_approval_stream()
                entered, release = threading.Event(), threading.Event()
                errors = []
                def request(*args):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("test did not release read")
                    if fails:
                        raise TimeoutError("old read failed")
                    return {"pending": [], "recent": []}
                self.p.request.side_effect = request
                def fetch():
                    try:
                        self.p.fetch_approvals()
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=fetch)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(self.accept([row()]))  # must not wait for GET
                finally:
                    release.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), int(fails))
                self.assertTrue(self.p.manager.approvals_ready)
                with self.assertRaises(ApprovalBlocked):
                    self.service.check_approval_gate("s1")

    def test_fetch_failure_without_newer_event_revokes_authority(self):
        self.accept()
        self.p.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.p.fetch_approvals()
        self.assertFalse(self.p.manager.approvals_ready)

    def test_default_and_custom_period_do_not_poll_every_three_seconds(self):
        self.p._needs_reconcile.clear()
        self.p._last_fetch = 100
        with patch("muse_bridge.muse.interactions.time.monotonic", return_value=279):
            self.assertFalse(self.p.reconciliation_due())
        with patch("muse_bridge.muse.interactions.time.monotonic", return_value=280):
            self.assertTrue(self.p.reconciliation_due())
        self.service.config.approval.sync_interval_s = 20
        with patch("muse_bridge.muse.interactions.time.monotonic", return_value=120):
            self.assertTrue(self.p.reconciliation_due())
        self.p._last_fetch = None
        self.assertTrue(self.p.reconciliation_due())

    def test_events_do_not_postpone_periodic_safety_read(self):
        self.p._last_fetch = 100
        self.accept(seq=200)
        self.assertEqual(self.p._last_fetch, 100)

    def test_interval_config_defaults_validation_and_loader(self):
        self.assertEqual(ApprovalConfig().sync_interval_s, 180)
        for value in (True, False, 0, -1, 1.5, "180", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ApprovalConfig(sync_interval_s=value)
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps({"muse": {"client_ip": "127.0.0.1",
            "approval": {"sync_interval_s": 45}}}), encoding="utf-8")
        self.assertEqual(ConfigLoader.load(str(config)).muse.approval.sync_interval_s, 45)


class StreamTests(Fixture, unittest.TestCase):
    def test_owner_thread_closes_client_and_invalidates_on_end(self):
        stream = ApprovalEventStream(self.p)
        client = MagicMock()
        client.credentials_current.return_value = True
        self.service.create_client = MagicMock(return_value=client)
        def read(timeout):
            stream.stop.set()
            return [event([row()])]
        client.subscribe.return_value.read_events.side_effect = read
        stream.run()
        client.connect.assert_called_once()
        client.subscribe.assert_called_once_with()
        client.close.assert_called_once()
        self.assertFalse(self.p.manager.approvals_ready)
        self.assertEqual(len(self.p.manager.pending_approvals()), 1)

    def test_credentials_rotating_during_read_discard_snapshot(self):
        stream = ApprovalEventStream(self.p)
        client = MagicMock()
        client.credentials_current.side_effect = [True, False]
        self.service.create_client = MagicMock(return_value=client)
        def read(timeout):
            stream.stop.set()
            return [event([row()])]
        client.subscribe.return_value.read_events.side_effect = read
        stream.run()
        self.assertEqual(self.p.manager.pending_approvals(), [])
        self.assertFalse(self.p.manager.approvals_ready)
        client.close.assert_called_once()

    def test_connection_error_closes_client_and_requests_resync(self):
        stream = ApprovalEventStream(self.p)
        client = MagicMock()
        self.service.create_client = MagicMock(return_value=client)
        def connect():
            stream.stop.set()
            raise ConnectionLost("offline")
        client.connect.side_effect = connect
        stream.run()
        client.close.assert_called_once()
        self.assertFalse(self.p.manager.approvals_ready)
        self.assertTrue(self.p._needs_reconcile.is_set())


class AsyncTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_slow_periodic_read_does_not_block_event_notification(self):
        started, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def reconcile():
            started.set()
            await release.wait()
            return self.p.manager.pending_approvals()
        async def notify(item, text):
            notified.set()
            return True
        self.p.reconcile = reconcile
        changed = asyncio.Event()
        self.p._wake_callback = changed.set
        task = asyncio.create_task(self.p._run_updates(notify, lambda: True, changed))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.accept([row()])
            await asyncio.wait_for(notified.wait(), 1)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_lifecycle_cancellation_waits_for_worker_cleanup(self):
        started, closed = threading.Event(), threading.Event()
        class FakeStream:
            def __init__(self, protocol):
                self.stop = threading.Event()
            def run(self):
                started.set()
                self.stop.wait(2)
                closed.set()
        self.p._run_updates = AsyncMock(side_effect=lambda *args: None)
        # Use a genuinely blocked coroutine so cancellation takes the finally path.
        async def updates(*args):
            await asyncio.Event().wait()
        self.p._run_updates.side_effect = updates
        with patch("muse_bridge.muse.approval_stream.ApprovalEventStream", FakeStream):
            task = asyncio.create_task(self.p.run(AsyncMock(), lambda: True))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertIsNone(self.p._wake_callback)

    async def test_failed_sync_uses_backoff_instead_of_busy_retry(self):
        self.p.reconcile = AsyncMock(side_effect=TimeoutError())
        self.p._wait_for_update = AsyncMock()
        running = iter([True] * 10 + [False])
        with patch("muse_bridge.muse.interactions.time", SimpleNamespace(monotonic=lambda: 100)):
            await self.p._run_updates(AsyncMock(), lambda: next(running), asyncio.Event())
        self.p.reconcile.assert_awaited_once()
        self.assertEqual(self.p._reconcile_retry_at, 103)

    async def test_normal_update_ticks_only_fetch_at_configured_interval(self):
        self.accept()
        self.p._needs_reconcile.clear()
        self.p._last_fetch = 100
        clock = [100]
        async def tick(changed):
            clock[0] += 90
        async def sync():
            self.p._last_fetch = clock[0]
            return []
        self.p._wait_for_update = tick
        self.p.reconcile = AsyncMock(side_effect=sync)
        running = iter([True, True, True, False])
        with patch("muse_bridge.muse.interactions.time", SimpleNamespace(monotonic=lambda: clock[0])):
            await self.p._run_updates(AsyncMock(), lambda: next(running), asyncio.Event())
        self.p.reconcile.assert_awaited_once()

    async def test_startup_read_is_launched_when_no_snapshot_exists(self):
        self.p.reconcile = AsyncMock(return_value=[])
        self.p._wait_for_update = AsyncMock()
        running = iter([True, False])
        await self.p._run_updates(AsyncMock(), lambda: next(running), asyncio.Event())
        self.p.reconcile.assert_awaited_once()


class SubscriptionEndTests(unittest.TestCase):
    def test_final_chunk_event_is_delivered_once_then_connection_lost(self):
        client = MuseClient("t", "n", "vm", "")
        client._ws = MagicMock()
        client._ws.recv.return_value = (2, b"frame")
        client._recv_cs = MagicMock()
        client._reasm = MagicMock()
        sub = PushSubscription(client, 1)
        with patch("muse_bridge.muse.client.client.unwrap_service_response"), patch(
            "muse_bridge.muse.client.client.parse_service_frame", return_value=(
                1, "body_chunk", {"data": b'{"event":"approvals.snapshot"}\n', "end_body": True})):
            self.assertEqual(sub.read_events(1), [{"event": "approvals.snapshot"}])
        with self.assertRaises(ConnectionLost):
            sub.read_events(1)

    def test_multiplexed_ended_subscription_is_not_silently_polled_forever(self):
        client = MuseClient("t", "n", "vm", "")
        sub = PushSubscription(client, 1)
        client._dispatch_other_stream_frame(1, "body_chunk", {"end_body": True})
        with self.assertRaises(ConnectionLost):
            client.read_any_subscription_events()

    def test_transport_error_is_not_mistaken_for_quiet_timeout(self):
        client = MuseClient("t", "n", "vm", "")
        client._ws = MagicMock()
        client._ws.recv.side_effect = OSError("connection dropped")
        sub = PushSubscription(client, 1)
        with self.assertRaises(ConnectionLost):
            sub.read_events(1)


if __name__ == "__main__":
    unittest.main()
