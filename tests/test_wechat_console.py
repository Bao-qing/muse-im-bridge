"""Offline coverage for the standalone console; only synthetic accounts/media.

Run: python -m unittest discover -s tests -p test_wechat_console.py -v
No tests package, real config/cookie/token or network is needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SPEC = importlib.util.spec_from_file_location("wechat_manual_console", Path(__file__).with_name("wechat_cli.py"))
console = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = console
_SPEC.loader.exec_module(console)

from muse_bridge.config import WeChatConfig
from muse_bridge.adapters.wechat.media import encrypt
from muse_bridge.adapters.wechat.state import WeChatState

ACCOUNT = {"bot_id": "test-bot", "user_id": "test-owner", "bot_token": "SYNTHETIC-BOT-SECRET",
           "baseurl": "https://ilinkai.weixin.qq.com"}


def event(mid="m1", *, user="test-owner", text="hello", items=None):
    return {"message_type": 1, "from_user_id": user, "to_user_id": "test-bot", "message_id": mid,
            "context_token": "SYNTHETIC-CONTEXT-SECRET", "item_list": items if items is not None else [
                {"type": 1, "text_item": {"text": text}}]}


class QueueInput:
    def __init__(self, *lines):
        self.queue = asyncio.Queue()
        for line in lines:
            self.queue.put_nowait(line)
        self.closed = False
        self.reading = asyncio.Event()

    async def read_line(self, prompt=""):
        self.reading.set()
        return await self.queue.get()

    async def verify(self):
        return await self.read_line()

    def feed(self, line):
        self.queue.put_nowait(line)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.updates = asyncio.Queue()
        self.sent = []
        self.polling = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.delivered = asyncio.Event()
        self.closed = 0
        self.active = 0
        self.max_active = 0
        self.cursors = []

    async def get_updates(self, cursor, timeout_ms):
        self.cursors.append(cursor)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.polling.set()
        try:
            return await self.updates.get()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1

    async def send_message(self, message):
        self.sent.append(message)
        self.delivered.set()
        return {"ret": 0}

    async def aclose(self):
        self.closed += 1


class ConsoleFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = WeChatConfig(state_dir=str(self.root / "state"), max_image_bytes=64,
                                max_file_bytes=128, max_video_bytes=256,
                                max_video_upload_bytes=192, download_timeout_s=1)
        self.state = WeChatState(self.cfg.state_dir)
        self.state.save_account(ACCOUNT)
        self.client = FakeClient()
        self.media = mock.Mock(download=mock.AsyncMock(return_value=b"media"))
        self.sender = mock.Mock(send=mock.AsyncMock(return_value=True))
        self.lines = []
        self.cli = console.WeChatConsole(self.cfg, self.state, self.client, self.media, self.sender,
                                         self.root / "downloads", output=self.lines.append)
        self.reader = QueueInput()
        # A real DNS lookup is a test failure, even if a future edit accidentally
        # replaces a fake. All state/config paths in these tests are temporary.
        network = mock.patch("socket.getaddrinfo", side_effect=AssertionError("Network forbidden in offline tests"))
        network.start()
        self.addCleanup(network.stop)

    def text(self):
        return "\n".join(self.lines)

    def args(self, *extra):
        return console.parse_args(["--config", str(self.root / "fake.json"),
                                   "--download-dir", str(self.root / "downloads"), *extra])

    async def test_parser_envelope_rejects_wrong_account_group_and_invalid_ids(self):
        for change in ({"message_type": 2}, {"message_type": True}, {"group_id": "group"},
                       {"to_user_id": "other-bot"}, {"from_user_id": "has space"},
                       {"message_id": None}, {"context_token": {}}, {"item_list": "bad"}):
            value = event()
            value.update(change)
            self.assertIsNone(console.parse_incoming(value, "test-bot"))
        parsed = console.parse_incoming(event(mid=42), "test-bot")
        self.assertEqual(parsed.message_id, "42")
        self.assertNotIn("SYNTHETIC-CONTEXT-SECRET", repr(parsed))

    async def test_concurrent_receive_and_send_and_duplicate_suppression(self):
        run = asyncio.create_task(self.cli.run(self.reader))
        try:
            await asyncio.wait_for(self.client.polling.wait(), 1)
            self.client.updates.put_nowait({"msgs": [event(), event()], "get_updates_buf": "next-cursor"})
            # The poller starts its second request only after the full batch and
            # cursor commit, without waiting for a line of console input.
            await self.wait_for(lambda: len(self.client.cursors) >= 2)
            self.assertEqual(self.state.cursor, "next-cursor")
            self.assertIn("sender=test-owner message_id=m1", self.text())
            self.assertEqual(self.text().count("[收到]"), 1)
            self.assertEqual(self.state.context("test-owner"), "SYNTHETIC-CONTEXT-SECRET")
            self.reader.feed("from console")
            await self.wait_for(lambda: self.sender.send.await_count == 1)
            self.sender.send.assert_awaited_once_with("test-owner", "from console", purpose="proactive")
            self.assertEqual(self.client.active, 1)
            self.reader.feed("/quit")
            await asyncio.wait_for(run, 1)
            self.assertTrue(self.client.cancelled.is_set())
            self.assertEqual(self.client.active, 0)
            self.assertEqual(self.client.max_active, 1)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_paths_with_spaces_image_file_video_and_windows_backslashes(self):
        await self.cli.handle_batch([event()])
        image = self.root / "image with spaces.png"
        image.write_bytes(b"PNG")
        document = self.root / "document with spaces.txt"
        document.write_bytes(b"FILE")
        video = self.root / "video with spaces.mp4"
        video.write_bytes(b"VIDEO")
        await self.cli.command(f'/image "{image}"')
        await self.cli.command(f"/file '{document}'")
        await self.cli.command(f"/video {video}")
        self.assertEqual(self.sender.send.await_count, 3)
        for call, kind, data, path in zip(self.sender.send.await_args_list, ("images", "files", "videos"),
                                           (b"PNG", b"FILE", b"VIDEO"), (image, document, video)):
            self.assertEqual(call.kwargs[kind], [{"data": data, "filename": path.name}])
            self.assertEqual(call.kwargs["purpose"], "proactive")
        self.assertEqual(str(console.path_argument(r'"C:\my files\a.png"')), str(Path(r"C:\my files\a.png")))
        for command in ("/image", '/file "unfinished', "/video \x00"):
            await self.cli.command(command)
        self.assertEqual(self.sender.send.await_count, 3)

    async def test_send_needs_context_no_permissions_and_to_peers(self):
        await self.cli.command("hello without context")
        self.assertIn("缺少 context", self.text())
        self.sender.send.assert_not_awaited()
        # Not an owner/admin/whitelisted user: still an ordinary protocol peer.
        await self.cli.handle_batch([event(user="unrestricted-user")])
        await self.cli.command("/to unrestricted-user")
        await self.cli.command("/to invalid user")
        self.assertEqual(self.cli.target, "unrestricted-user")
        await self.cli.command("/peers")
        self.assertIn("user_id=unrestricted-user message_id=m1 context=有", self.text())
        await self.cli.command("allowed")
        self.sender.send.assert_awaited_once_with("unrestricted-user", "allowed", purpose="proactive")

    async def test_default_target_falls_back_to_first_incoming(self):
        self.cli.target = ""
        await self.cli.handle_batch([event(user="first-peer")])
        self.assertEqual(self.cli.target, "first-peer")

    async def test_auto_download_images_files_video_and_transcribed_voice(self):
        items = [
            {"type": 2, "image_item": {"media": {"full_url": "https://fake.invalid/?signature=SECRET"}}},
            {"type": 4, "file_item": {"file_name": "../../CON.txt", "media": {"aes_key": "AES-SECRET"}}},
            {"type": 5, "video_item": {}},
            {"type": 3, "voice_item": {"text": "spoken words", "media": {"aes_key": "VOICE-SECRET"}}},
        ]
        incoming = event(items=items)
        await self.cli.handle_batch([incoming, incoming])
        self.assertEqual(self.media.download.await_count, 4)
        self.assertEqual([call.args[1] for call in self.media.download.await_args_list], [64, 128, 256, 128])
        self.assertEqual([call.kwargs["image"] for call in self.media.download.await_args_list], [True, False, False, False])
        paths = list(self.cli.download_dir.iterdir())
        self.assertEqual(len(paths), 4)
        for path in paths:
            self.assertEqual(path.parent, self.cli.download_dir)
            self.assertEqual(path.read_bytes(), b"media")
        self.assertTrue(any(path.name.endswith("_CON.txt") for path in paths))
        self.assertIn("spoken words", self.text())
        self.assertIn("kind=voice path=", self.text())
        self.assertNotIn("signature=SECRET", self.text())
        self.assertNotIn("AES-SECRET", self.text())
        self.sender.send.assert_not_awaited()

    async def test_failed_download_is_visible_terminal_no_repeat_and_batch_advances(self):
        self.media.download.side_effect = [RuntimeError("AES-SECRET https://fake.invalid/?token=SECRET"), b"success"]
        first = event(items=[{"type": 2, "image_item": {}}])
        second = event("m2", items=[{"type": 4, "file_item": {"file_name": "ok.txt"}}])
        await self.cli.handle_batch([first, second, first])
        self.assertEqual(self.state.message_status("test-owner", "m1"), "unknown")
        self.assertEqual(self.state.message_status("test-owner", "m2"), "handled")
        self.assertEqual(self.media.download.await_count, 2)
        self.assertIn("附件下载/保存失败", self.text())
        self.assertIn("RuntimeError", self.text())
        self.assertNotIn("AES-SECRET", self.text())
        self.assertNotIn("?token=SECRET", self.text())
        # Same failed message through the gateway is skipped, but the cursor can advance.
        self.client.updates.put_nowait({"msgs": [first], "get_updates_buf": "after-failure"})
        run = asyncio.create_task(self.cli.run(self.reader))
        try:
            await self.wait_for(lambda: self.state.cursor == "after-failure")
            self.assertEqual(self.media.download.await_count, 2)
        finally:
            self.reader.feed("/quit")
            await asyncio.wait_for(run, 1)

    async def test_save_limits_and_no_overwrite_even_on_collision(self):
        directory = self.root / "safe"
        directory.mkdir()
        existing = directory / ("same_" + console.safe_filename(r"..\NUL.txt"))
        existing.write_bytes(b"original")
        with mock.patch.object(console.secrets, "token_hex", side_effect=["same", "unique"]):
            saved = console.save_attachment(directory, r"..\NUL.txt", b"new", 3)
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(saved.read_bytes(), b"new")
        self.assertEqual(saved.parent, directory.resolve())
        for name in ("../../", "..\\..\\CON", "C:\\unsafe\\LPT1.txt", "COM¹.dat", "a:b?.txt", "x\x1by", ". ", "界" * 300):
            safe = console.safe_filename(name)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertNotIn(":", safe)
            self.assertNotIn("?", safe)
            self.assertFalse(safe.endswith((".", " ")))
            self.assertLessEqual(len(safe.encode("utf-8")), 161)
            self.assertNotIn(safe.split(".", 1)[0].upper(), console._RESERVED)
        with self.assertRaises(ValueError):
            console.save_attachment(directory, "bad", b"too large", 1)
        self.assertEqual(len(list(directory.iterdir())), 2)

    async def test_incoming_oversize_bytes_and_outgoing_limit(self):
        self.media.download.return_value = b"x" * 65
        await self.cli.handle_batch([event(items=[{"type": 2, "image_item": {}}])])
        self.assertFalse(self.cli.download_dir.exists())
        self.assertEqual(self.state.message_status("test-owner", "m1"), "unknown")
        path = self.root / "oversize.png"
        path.write_bytes(b"x" * 65)
        await self.cli.command(f'/image "{path}"')
        self.sender.send.assert_not_awaited()
        self.assertIn("发送错误", self.text())

    async def test_media_timeout_is_terminal_but_quit_cancels_inflight_media(self):
        started = asyncio.Event()

        async def stalled(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.media.download.side_effect = stalled
        self.cfg.download_timeout_s = 0.02
        incoming = event(items=[{"type": 2, "image_item": {}}])
        await self.cli.handle_batch([incoming])
        self.assertEqual(self.state.message_status("test-owner", "m1"), "unknown")
        self.assertIn("TimeoutError", self.text())
        self.cfg.download_timeout_s = 100
        started.clear()
        self.client.updates.put_nowait({"msgs": [event("m2", items=[{"type": 2, "image_item": {}}])],
                                         "get_updates_buf": "must-not-commit"})
        run = asyncio.create_task(self.cli.run(self.reader))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.reader.feed("/quit")
            await asyncio.wait_for(run, 1)
            self.assertEqual(self.state.cursor, "")
            self.assertEqual(self.state.message_status("test-owner", "m2"), "accepted")
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    async def test_save_failure_is_terminal_and_other_media_still_saved(self):
        with mock.patch.object(console, "save_attachment", side_effect=[PermissionError("SECRET"), self.root / "ok"]):
            await self.cli.handle_batch([event(items=[{"type": 2, "image_item": {}}, {"type": 4, "file_item": {}}])])
        self.assertEqual(self.media.download.await_count, 2)
        self.assertIn("PermissionError", self.text())
        self.assertNotIn("SECRET", self.text())
        self.assertEqual(self.state.message_status("test-owner", "m1"), "unknown")

    async def test_crash_marker_and_reloaded_state_skip_download(self):
        self.state.remember_message("test-owner", "m1", "SYNTHETIC-CONTEXT-SECRET")
        self.state.recover_inputs()
        reloaded = WeChatState(self.cfg.state_dir)
        self.cli.state = reloaded
        await self.cli.handle_batch([event(items=[{"type": 2, "image_item": {}}])])
        self.media.download.assert_not_awaited()
        self.assertEqual(reloaded.message_status("test-owner", "m1"), "unknown")

    async def test_cursor_waits_for_media_and_persistence_error_does_not_commit(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            started.set()
            await release.wait()
            return b"media"

        self.media.download.side_effect = download
        self.client.updates.put_nowait({"msgs": [event(items=[{"type": 2, "image_item": {}}])],
                                         "get_updates_buf": "not-yet"})
        run = asyncio.create_task(self.cli.run(self.reader))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(self.state.cursor, "")
            self.assertEqual(self.state.message_status("test-owner", "m1"), "accepted")
            with mock.patch.object(self.state, "finish_message", side_effect=OSError("SECRET")):
                release.set()
                await self.wait_for(lambda: self.cli.gateway.phase == "backoff")
            self.assertEqual(self.state.cursor, "")
            self.assertIn("保留游标", self.text())
            self.assertNotIn("SECRET", self.text())
        finally:
            self.reader.feed("/quit")
            await asyncio.wait_for(run, 1)

    async def test_status_redaction_unknown_send_and_runtime_login_is_only_hint(self):
        media_url = "https://fake.invalid/media?signature=SECRET"
        body_url = "https://example.com/read?topic=wechat"
        raw = f"SYNTHETIC-BOT-SECRET SYNTHETIC-CONTEXT-SECRET AES-SECRET {media_url} {body_url}\x1b[2J"
        await self.cli.handle_batch([event(text=raw, items=[
            {"type": 1, "text_item": {"text": raw}},
            {"type": 2, "image_item": {"aeskey": "AES-SECRET", "media": {"full_url": media_url}}}])])
        self.sender.send.return_value = False
        await self.cli.command("send")
        await self.cli.command("/status")
        await self.cli.command("/login")
        await self.cli.command("/登录")
        self.assertIn("发送未确认", self.text())
        self.assertIn("--login 重启", self.text())
        for value in ("SYNTHETIC-BOT-SECRET", "SYNTHETIC-CONTEXT-SECRET", "AES-SECRET", "signature=SECRET", "\x1b"):
            self.assertNotIn(value, self.text())
        self.assertIn(body_url, self.text())
        self.assertIn('"has_token": true', self.text())
        self.assertIn('"user_id": "test-owner"', self.text())

    async def test_saved_choice_and_eof_quit_close_client_and_cancel_poll(self):
        for ending in (None, "/quit"):
            with self.subTest(ending=ending):
                client = FakeClient()
                reader = QueueInput("1")
                factory = mock.Mock(return_value=client)
                run = asyncio.create_task(console.async_main(self.args(), reader=reader, output=self.lines.append,
                                          config_loader=lambda path: self.cfg, client_factory=factory))
                try:
                    await asyncio.wait_for(client.polling.wait(), 1)
                    reader.feed(ending)
                    self.assertEqual(await asyncio.wait_for(run, 1), 0)
                    self.assertTrue(client.cancelled.is_set())
                    self.assertEqual(client.closed, 1)
                    self.assertTrue(reader.closed)
                    self.assertEqual(client.active, 0)
                    self.assertEqual(factory.call_args.args, (ACCOUNT["bot_token"], ACCOUNT["baseurl"]))
                    self.assertEqual(factory.call_args.kwargs["cooling_until"](), 0)
                    self.assertTrue(callable(factory.call_args.kwargs["on_session_invalid"]))
                finally:
                    run.cancel()
                    await asyncio.gather(run, return_exceptions=True)

    async def test_external_cancellation_stops_poll_and_closes_client(self):
        reader = QueueInput("1")
        run = asyncio.create_task(console.async_main(self.args(), reader=reader, output=self.lines.append,
                                  config_loader=lambda _: self.cfg, client_factory=lambda *a, **k: self.client))
        await asyncio.wait_for(self.client.polling.wait(), 1)
        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run, 1)
        self.assertEqual(self.client.closed, 1)
        self.assertTrue(reader.closed)
        self.assertTrue(self.client.cancelled.is_set())

    async def test_scan_login_closes_unauthenticated_client_before_poll_client(self):
        login_client = FakeClient()
        clients = []

        def factory(*args, **kwargs):
            clients.append((args, kwargs))
            if not args:
                return login_client
            self.assertEqual(login_client.closed, 1)
            return self.client

        async def login(accounts, *, display, verify, on_status):
            self.assertEqual(accounts[0]["bot_id"], "test-bot")
            on_status({"raw_token": "SECRET"})
            self.assertEqual(await verify(), "1234")
            return ACCOUNT.copy()

        auth = mock.Mock(login=mock.AsyncMock(side_effect=login))
        reader = QueueInput("1234", "/quit")
        result = await console.async_main(self.args("--login"), reader=reader, output=self.lines.append,
                                         config_loader=lambda _: self.cfg, client_factory=factory,
                                         auth_factory=lambda _: auth)
        self.assertEqual(result, 0)
        self.assertEqual(len(clients), 2)
        self.assertEqual(login_client.closed, 1)
        self.assertEqual(self.client.closed, 1)
        self.assertNotIn("SECRET", self.text())
        self.assertTrue(reader.closed)

    async def test_menu_missing_saved_login_then_scan_or_exit(self):
        state = WeChatState(self.root / "empty-state")
        login_client = FakeClient()
        auth = mock.Mock(login=mock.AsyncMock(return_value=ACCOUNT.copy()))
        self.assertTrue(await console.select_account(
            self.cfg, state, QueueInput("wrong", "1", "2"), output=self.lines.append,
            client_factory=lambda **_: login_client, auth_factory=lambda _: auth, display=mock.Mock()))
        self.assertEqual(state.bot_id, "test-bot")
        self.assertEqual(login_client.closed, 1)
        self.assertIn("没有已保存登录", self.text())
        for choice in ("0", None):
            factory = mock.Mock()
            self.assertFalse(await console.select_account(self.cfg, state, QueueInput(choice),
                             output=self.lines.append, client_factory=factory))
            factory.assert_not_called()

    async def test_login_failure_preserves_saved_account_and_closes(self):
        auth = mock.Mock(login=mock.AsyncMock(side_effect=RuntimeError("SYNTHETIC-BOT-SECRET")))
        reader = QueueInput()
        result = await console.async_main(self.args("--login"), reader=reader, output=self.lines.append,
                                         config_loader=lambda _: self.cfg, client_factory=lambda **_: self.client,
                                         auth_factory=lambda _: auth)
        self.assertEqual(result, 1)
        self.assertEqual(self.client.closed, 1)
        self.assertEqual(self.state.credentials, ACCOUNT)
        self.assertTrue(reader.closed)
        self.assertNotIn("SYNTHETIC-BOT-SECRET", self.text())

    async def test_real_sender_client_and_media_with_mock_transport(self):
        requests = []
        audio_key = b"0123456789abcdef"

        def transport(request):
            requests.append(request)
            if request.url.path.endswith("getupdates"):
                return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": ""})
            if request.url.host == "novac2c.cdn.weixin.qq.com":
                if request.method == "POST":
                    return httpx.Response(200, headers={"x-encrypted-param": "SYNTHETIC-CDN-PARAM"})
                return httpx.Response(200, content=encrypt(b"original voice", audio_key))
            if request.url.path.endswith("getuploadurl"):
                return httpx.Response(200, json={"ret": 0, "upload_full_url":
                                                 "https://novac2c.cdn.weixin.qq.com/upload?sig=fake"})
            if request.url.path.endswith("sendmessage"):
                return httpx.Response(200, json={"ret": 0})
            raise AssertionError("Unexpected mock request")

        client = console.WeChatClient(ACCOUNT["bot_token"], ACCOUNT["baseurl"], transport=httpx.MockTransport(transport))
        media = console.WeChatMedia(client)
        sender = console.WeChatSender(client, self.state, self.cfg, media)
        cli = console.WeChatConsole(self.cfg, self.state, client, media, sender,
                                     self.root / "wire-downloads", output=self.lines.append)
        try:
            await cli.handle_batch([event(items=[{"type": 3, "voice_item": {
                "text": "transcription", "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/download?sig=fake",
                                                      "aes_key": audio_key.hex()}}}])])
            self.assertEqual(next(cli.download_dir.iterdir()).read_bytes(), b"original voice")
            await cli.command("text over real sender")
            sent = json.loads(requests[-1].content)["msg"]
            self.assertEqual(sent["context_token"], "SYNTHETIC-CONTEXT-SECRET")
            self.assertEqual(sent["to_user_id"], "test-owner")
            self.assertEqual(sent["item_list"][0]["text_item"]["text"], "text over real sender")
            image, document = self.root / "wire image.png", self.root / "wire file.txt"
            image.write_bytes(b"PNG")
            document.write_bytes(b"file bytes")
            await cli.command(f'/image "{image}"')
            await cli.command(f'/file "{document}"')
            messages = [json.loads(request.content)["msg"] for request in requests
                        if request.url.path.endswith("sendmessage")]
            self.assertEqual([m["item_list"][0]["type"] for m in messages], [1, 2, 4])
            self.assertEqual(messages[-1]["item_list"][0]["file_item"]["file_name"], "wire file.txt")
            self.assertEqual(sum(request.method == "POST" and request.url.host == "novac2c.cdn.weixin.qq.com"
                                 for request in requests), 2)
            self.assertIn("已发送", self.text())
            self.assertNotIn(audio_key.hex(), self.text())
            self.assertNotIn("sig=fake", self.text())
            self.assertNotIn("SYNTHETIC-CDN-PARAM", self.text())
        finally:
            await client.aclose()

    async def test_input_worker_is_daemon_no_prefetch_and_no_default_executor(self):
        class CountingStream(io.StringIO):
            reads = 0

            def readline(self):
                self.reads += 1
                return super().readline()

        stream = CountingStream("/quit\nnext application line\n")
        reader = console.ConsoleInput(output=self.lines.append, stream=stream)
        with mock.patch.object(asyncio.get_running_loop(), "run_in_executor", side_effect=AssertionError("No executor")):
            self.assertEqual(await asyncio.wait_for(reader.read_line(), 1), "/quit")
            self.assertTrue(reader._thread.daemon)
            reader.close()
        reader._thread.join(timeout=1)
        self.assertEqual(stream.reads, 1)
        self.assertEqual(stream.readline(), "next application line\n")
        self.assertFalse(reader._thread.is_alive())

    async def test_input_worker_eof_hidden_verify_and_cancellable_blocked_read(self):
        reader = console.ConsoleInput(output=self.lines.append, stream=io.StringIO(""))
        try:
            self.assertIsNone(await asyncio.wait_for(reader.read_line(), 1))
            with mock.patch.object(console, "verify_code", return_value="1234"):
                self.assertEqual(await asyncio.wait_for(reader.verify(), 1), "1234")
        finally:
            reader.close()
            reader._thread.join(timeout=1)

        entered, release = threading.Event(), threading.Event()

        class BlockedStream:
            def readline(self):
                entered.set()
                release.wait(3)
                return "unused\n"

        reader = console.ConsoleInput(output=self.lines.append, stream=BlockedStream())
        read = asyncio.create_task(reader.read_line())
        try:
            await self.wait_for(entered.is_set)
            read.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(read, 1)
            reader.close()
            self.assertTrue(reader._thread.daemon)
            self.assertIsNone(await reader.read_line())
        finally:
            release.set()
            reader._thread.join(timeout=1)


class ConfigAndEntrypointTests(unittest.TestCase):
    def test_only_offline_wechat_loader_is_used_from_synthetic_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps({"adapters": {"wechat": {"state_dir": str(Path(directory) / "state"),
                                                               "channel_version": "2.4.7"}}}), encoding="utf-8")
            # The loader intentionally supports const.json in cwd. Change into a
            # fresh directory so the test cannot read any real local config.
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with mock.patch.object(console.ConfigLoader, "load", side_effect=AssertionError("Full loader forbidden")), \
                     mock.patch("socket.getaddrinfo", side_effect=AssertionError("No network")):
                    cfg = console.ConfigLoader.load_wechat(str(path))
                    self.assertEqual(cfg.channel_version, "2.4.7")
                    reader = QueueInput("0")
                    client = mock.Mock(side_effect=AssertionError("No client on exit"))
                    with mock.patch.object(console.ConfigLoader, "load_wechat", return_value=cfg) as load:
                        result = asyncio.run(console.async_main(console.parse_args(["--config", str(path)]),
                                             reader=reader, output=lambda _: None, client_factory=client))
                    load.assert_called_once_with(str(path))
                    self.assertEqual(result, 0)
                    self.assertTrue(reader.closed)
                    client.assert_not_called()
            finally:
                os.chdir(previous)

    def test_direct_script_help_works_without_tests_package_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("wechat_cli.py")), "--help"],
                                    cwd=directory, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn(b"--config", result.stdout)
        self.assertIn(b"--login", result.stdout)
        self.assertIn(b"--download-dir", result.stdout)

    def test_daemon_blocked_input_does_not_hang_asyncio_run_or_process_exit(self):
        code = """
import asyncio
import sys
import threading
sys.path.insert(0, sys.argv[1])
from wechat_cli import ConsoleInput
class BlockedStream:
    def readline(self):
        threading.Event().wait()
async def run():
    reader = ConsoleInput(output=lambda _: None, stream=BlockedStream())
    try:
        await asyncio.wait_for(reader.read_line(), 0.05)
    except TimeoutError:
        pass
    finally:
        reader.close()
asyncio.run(run())
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent)],
                                    cwd=directory, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_configuration_error_is_sanitized_and_closes_input_without_client(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                path = Path(directory) / "bad.json"
                path.write_text('{"SYNTHETIC-SECRET": invalid}', encoding="utf-8")
                reader, output = QueueInput(), []
                factory = mock.Mock(side_effect=AssertionError("No network client"))
                result = asyncio.run(console.async_main(console.parse_args(["--config", str(path)]),
                                     reader=reader, output=output.append, client_factory=factory))
                self.assertEqual(result, 1)
                self.assertTrue(reader.closed)
                self.assertNotIn("SYNTHETIC-SECRET", "\n".join(output))
                factory.assert_not_called()
            finally:
                os.chdir(previous)

    def test_ctrl_c_exit_code_without_unsanitized_traceback(self):
        def interrupted(coroutine):
            coroutine.close()
            raise KeyboardInterrupt()

        with mock.patch.object(console.asyncio, "run", side_effect=interrupted), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 130)
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
