"""Offline invariants for observational QQ/OneBot transport logging."""

import asyncio
import base64
import io
import json
import logging
import queue
import threading
import types
import unittest
import urllib.error
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from muse_bridge import observability as obs
from muse_bridge.adapters.qq import client as qq_client
from muse_bridge.adapters.qq import gateway as qq_gateway
from muse_bridge.adapters.qq import ws_transport as transport
from muse_bridge.adapters.onebot import client as ob_client
from muse_bridge.adapters.onebot import gateway as ob_gateway


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []
        self.runtime = []

    def emit(self, record):
        if hasattr(record, "trace_data"):
            self.events.append(record.trace_data)
        else:
            self.runtime.append(record.getMessage())


@contextmanager
def capture(enabled):
    handler = _Capture()
    root = logging.getLogger("muse_bridge")
    old_level, old_disabled = root.level, root.disabled
    root.setLevel(obs.TRACE)
    root.disabled = False
    root.addHandler(handler)
    try:
        with patch.object(obs, "_trace_enabled", enabled):
            yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        root.disabled = old_disabled


class Response:
    def __init__(self, raw, status=200, headers=None):
        self.raw = raw
        self.status = status
        self.headers = headers or {"Content-Type": "application/json",
                                   "Set-Cookie": "private-cookie-value",
                                   "X-Untrusted": "private-header-value"}
        self.read = Mock(return_value=raw)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def cached_qq():
    client = qq_client.QQClient("app-id", "private-client-secret")
    client._token = "private-access-token"
    client._token_expires_at = float("inf")
    return client


def wire_request(call):
    request = call.args[0]
    return request.full_url, request.get_method(), request.data, request.header_items(), call.kwargs


class HTTPLoggingTests(unittest.TestCase):
    def assert_correlated(self, events, component):
        events = [event for event in events if event["component"] == component]
        self.assertTrue(events)
        ids = {event["request_id"] for event in events}
        self.assertEqual(len(ids), 1)
        self.assertTrue(all(event["duration_ms"] >= 0 for event in events))
        self.assertTrue(all("url" in event and "method" in event for event in events))
        return events

    def assert_hidden(self, cap, *values):
        logs = json.dumps(cap.events, ensure_ascii=False) + " ".join(cap.runtime)
        for value in values:
            self.assertNotIn(value, logs)

    def test_qq_rest_toggle_preserves_wire_read_return_and_redaction(self):
        body = {"ok": True, "file_info": "private-file-info",
                "resolved": {"attachments": [{"url": "https://cdn.test/a?sig=private-signature"}]},
                "extra": {"unrecognized": "private-otp-314159"}}
        payload = {"content": "private-chat", "media": {"file_info": "private-file-info"}, "msg_seq": 7}
        calls = []
        for enabled in (False, True):
            response = Response(json.dumps(body).encode())
            with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", return_value=response) as open_:
                result = cached_qq()._http_json("POST", "https://api.test/messages?sig=private-query", payload, timeout=27)
            self.assertEqual(result, (200, body))
            response.read.assert_called_once_with()
            open_.assert_called_once()
            calls.append(wire_request(open_.call_args))
            if enabled:
                events = self.assert_correlated(cap.events, "qq.http")
                self.assertEqual([e["event"] for e in events], ["start", "response"])
                self.assertEqual(events[-1]["status"], 200)
                self.assertEqual(events[0]["payload"]["msg_seq"], 7)
                self.assert_hidden(cap, "private-access-token", "private-file-info", "private-chat",
                                   "private-signature", "private-query", "private-cookie-value",
                                   "private-header-value", "private-otp-314159")
            else:
                self.assertEqual(cap.events, [])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(payload["media"]["file_info"], "private-file-info")

    def test_qq_http_error_is_read_once_and_returned_unchanged(self):
        for enabled in (False, True):
            for raw, expected in ((b'{"code":401,"message":"private-error-body"}',
                                   {"code": 401, "message": "private-error-body"}),
                                  (b'private-non-json-error', {"raw": "private-non-json-error"})):
                error = urllib.error.HTTPError("https://api.test/", 401, "private-error-reason",
                                               {"Set-Cookie": "private-cookie-value"}, io.BytesIO(raw))
                error.read = Mock(wraps=error.read)
                with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", side_effect=error) as open_:
                    result = cached_qq()._http_json("POST", "https://api.test/", {"content": "private-chat"})
                self.assertEqual(result, (401, expected))
                error.read.assert_called_once_with()
                open_.assert_called_once()
                if enabled:
                    events = self.assert_correlated(cap.events, "qq.http")
                    self.assertEqual(events[-1]["status"], 401)
                    self.assertEqual(events[-1]["event"], "error")
                    self.assert_hidden(cap, "private-error-body", "private-non-json-error", "private-error-reason")

    def test_token_sensitive_both_ways_cache_and_failure_do_not_read_extra(self):
        for enabled in (False, True):
            response = Response(b'{"access_token":"private-new-token","expires_in":7200}')
            client = cached_qq()
            with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", return_value=response) as open_:
                self.assertEqual(client.get_access_token(force_refresh=True), "private-new-token")
                self.assertEqual(client.get_access_token(), "private-new-token")
            response.read.assert_called_once_with()
            open_.assert_called_once()
            self.assertEqual(open_.call_args.kwargs, {"timeout": 15})
            self.assertEqual(json.loads(open_.call_args.args[0].data)["clientSecret"], "private-client-secret")
            if enabled:
                events = self.assert_correlated(cap.events, "qq.http")
                self.assertEqual([e["payload"] for e in events], ["[REDACTED]", "[REDACTED]"])
                self.assert_hidden(cap, "private-new-token", "private-client-secret")
            stream = io.BytesIO(b'private-auth-error-body')
            error = urllib.error.HTTPError("https://bots.qq.com/app/getAppAccessToken", 403, "denied", {}, stream)
            error.read = Mock(wraps=error.read)
            with capture(enabled), patch.object(qq_client.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "failed to acquire QQ access token: HTTP Error 403: denied"):
                    client.get_access_token(force_refresh=True)
            error.read.assert_not_called()

    def test_qq_retry_keeps_two_posts_and_one_token_refresh(self):
        for enabled in (False, True):
            responses = [Response(b'{"message":"expired"}', 401),
                         Response(b'{"access_token":"refreshed","expires_in":7200}'),
                         Response(b'{"id":"reply-id"}')]
            with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", side_effect=responses) as open_:
                self.assertEqual(cached_qq().post_with_retry("https://api.test/messages", {"content": "private-chat"}, timeout=29),
                                 (200, {"id": "reply-id"}))
            self.assertEqual(open_.call_count, 3)
            self.assertEqual([c.kwargs["timeout"] for c in open_.call_args_list], [29, 15, 29])
            requests = [c.args[0] for c in open_.call_args_list]
            self.assertEqual(requests[0].data, requests[2].data)
            self.assertEqual(requests[0].get_header("Authorization"), "QQBot private-access-token")
            self.assertEqual(requests[2].get_header("Authorization"), "QQBot refreshed")
            for response in responses:
                response.read.assert_called_once_with()
            if enabled:
                starts = [e for e in cap.events if e["event"] == "start"]
                self.assertEqual(len({e["request_id"] for e in starts}), 3)

    def test_qq_gateway_lookup_preserves_success_and_fallback(self):
        for enabled in (False, True):
            response = Response(b'{"url":"wss://gateway.test/ws?sign=private-ws-sign"}')
            with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", return_value=response) as open_:
                self.assertEqual(cached_qq().get_gateway_url(), "wss://gateway.test/ws?sign=private-ws-sign")
            response.read.assert_called_once_with()
            self.assertEqual(open_.call_args.kwargs, {"timeout": 15})
            self.assert_hidden(cap, "private-ws-sign")
            error = urllib.error.HTTPError("https://api.test/", 403, "denied", {}, io.BytesIO(b'private-body'))
            error.read = Mock(wraps=error.read)
            with capture(enabled), patch.object(qq_client.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(cached_qq().get_gateway_url(), "wss://api.sgroup.qq.com/websocket")
            error.read.assert_not_called()

    def test_upload_metadata_no_base64_or_file_info_in_trace(self):
        binary = b'private-upload-binary'
        encoded = base64.b64encode(binary).decode()
        for enabled in (False, True):
            response = Response(b'{"file_info":"private-file-info"}')
            with capture(enabled) as cap, patch.object(qq_client.urllib.request, "urlopen", return_value=response) as open_:
                result = cached_qq().upload_media("private", "target", binary, file_type=4, file_name="private-name.txt")
            self.assertEqual(result, "private-file-info")
            self.assertEqual(json.loads(open_.call_args.args[0].data)["file_data"], encoded)
            self.assertEqual(open_.call_args.kwargs, {"timeout": 120})
            response.read.assert_called_once_with()
            self.assert_hidden(cap, encoded, "private-upload-binary", "private-file-info", "private-name.txt")
            if enabled:
                self.assertEqual(cap.events[0]["payload"]["encoded_length"], len(encoded))

    def test_download_preserves_read_limit_headers_return_and_close(self):
        outcomes = []
        for enabled in (False, True):
            response = Response(b'abc', headers={"Content-Length": "3"})
            response.getheader = Mock(return_value="3")
            conn = Mock()
            conn.getresponse.return_value = response
            with capture(enabled) as cap, patch.object(qq_client.http.client, "HTTPSConnection", return_value=conn) as cls:
                client = cached_qq()
                result = client.download_bytes("https://cdn.test/file?sig=private-signed-url", max_bytes=16, timeout=41)
            self.assertEqual(result, b'abc')
            response.read.assert_called_once_with(17)
            response.getheader.assert_called_once_with("Content-Length")
            conn.request.assert_called_once_with("GET", "/file?sig=private-signed-url",
                                                 headers={"User-Agent": client.user_agent, "Accept": "*/*"})
            conn.getresponse.assert_called_once_with()
            conn.close.assert_called_once_with()
            self.assertEqual(cls.call_args.args, ("cdn.test", 443))
            self.assertEqual(cls.call_args.kwargs["timeout"], 41)
            outcomes.append(conn.mock_calls)
            self.assert_hidden(cap, "private-signed-url")
            if enabled:
                events = self.assert_correlated(cap.events, "qq.http")
                self.assertEqual(len({e["connection_id"] for e in events}), 1)
                self.assertEqual(events[2]["payload"]["bytes"], 3)
        self.assertEqual(outcomes[0], outcomes[1])

    def test_download_invalid_status_does_not_read_and_preserves_exception(self):
        for enabled in (False, True):
            response = Response(b'private-body', 403)
            response.reason = "denied"
            conn = Mock()
            conn.getresponse.return_value = response
            with capture(enabled), patch.object(qq_client.http.client, "HTTPConnection", return_value=conn):
                with self.assertRaisesRegex(RuntimeError, "HTTP 403 denied"):
                    cached_qq().download_bytes("http://cdn.test/file")
            response.read.assert_not_called()
            conn.close.assert_called_once_with()

    def test_onebot_toggle_preserves_arguments_stream_and_result(self):
        calls = []
        for enabled in (False, True):
            response = Response(b'{"retcode":0,"data":{"message":"private-history","resolved":"private-quote"}}')
            client = ob_client.OneBotClient("https://onebot.test", "private-ob-token")
            with capture(enabled) as cap, patch.object(ob_client.urllib.request, "urlopen", return_value=response) as open_:
                result = client._post("/send_private_msg", {"user_id": 42, "message": [{"data": {"text": "private-chat"}}]}, timeout=31)
            self.assertEqual(result["retcode"], 0)
            response.read.assert_called_once_with()
            self.assertIs(open_.call_args.kwargs["context"], client._ssl_ctx)
            call = wire_request(open_.call_args)
            calls.append(call[:-1] + ({"timeout": call[-1]["timeout"]},))
            if enabled:
                events = self.assert_correlated(cap.events, "onebot.http")
                self.assertEqual(events[0]["payload"]["user_id"], 42)
                self.assert_hidden(cap, "private-ob-token", "private-chat", "private-history",
                                   "private-quote", "private-cookie-value", "private-header-value")
            else:
                self.assertEqual(cap.events, [])
        self.assertEqual(calls[0], calls[1])

    def test_onebot_http_error_is_not_consumed_or_retried(self):
        for enabled in (False, True):
            error = urllib.error.HTTPError("https://onebot.test/", 403, "denied", {}, io.BytesIO(b'private-error-body'))
            error.read = Mock(wraps=error.read)
            with capture(enabled) as cap, patch.object(ob_client.urllib.request, "urlopen", side_effect=error) as open_:
                with self.assertRaisesRegex(RuntimeError, "OneBot API request to /action failed: HTTP Error 403: denied") as raised:
                    ob_client.OneBotClient("https://onebot.test", "private-ob-token")._post("/action", {})
            self.assertIs(raised.exception.__context__, error)
            error.read.assert_not_called()
            open_.assert_called_once()
            self.assert_hidden(cap, "private-error-body")

    def test_trace_sink_failure_cannot_change_qq_or_onebot_http(self):
        for module, client, invoke in (
                (qq_client, cached_qq(), lambda c: c._http_json("GET", "https://api.test/")),
                (ob_client, ob_client.OneBotClient("https://onebot.test", "token"), lambda c: c._post("/action", {}))):
            response = Response(b'{}')
            with patch.object(module, "trace_event", side_effect=OSError("log destination failed")), \
                    patch.object(module.urllib.request, "urlopen", return_value=response) as open_:
                invoke(client)
            open_.assert_called_once()
            response.read.assert_called_once_with()


class GatewayLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_identify_resume_heartbeat_and_unknown_events_are_metadata_only(self):
        for enabled in (False, True):
            for resume in (False, True):
                client = Mock()
                client.get_access_token.return_value = "private-identify-token"
                callback = AsyncMock()
                gateway = qq_gateway.QQGateway(client, callback, debug=True)
                gateway._running = True
                gateway._connection_id = "connection-local"
                if resume:
                    gateway._session_id = "private-session-id"
                    gateway._last_seq = 4
                frames = [
                    {"op": 10, "d": {"heartbeat_interval": 45000}},
                    {"op": 0, "t": "READY", "s": 5,
                     "d": {"session_id": "private-session-id", "user": {"username": "private-username"}}},
                    {"op": 0, "t": "private-unknown-event", "d": {"raw_message": "private-otp-123456"}},
                    {"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 6,
                     "d": {"id": "message-id", "content": "private-chat",
                           "resolved": {"attachments": [{"extra": "private-quote"}]}}},
                    {"op": 1, "d": None},
                    {"op": 7, "d": None},
                ]
                ws = Mock()
                ws.recv = AsyncMock(side_effect=[json.dumps(frame) for frame in frames])
                ws.send_text = AsyncMock()
                gateway._ws = ws
                with capture(enabled) as cap:
                    await gateway._session_loop()
                    gateway._running = False
                    await asyncio.sleep(0)
                    await gateway._heartbeat_task
                self.assertEqual(ws.recv.await_count, len(frames))
                self.assertEqual(ws.send_text.await_count, 2)
                first, heartbeat = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
                self.assertEqual(first["op"], 6 if resume else 2)
                self.assertEqual(first["d"]["token"], "QQBot private-identify-token")
                if resume:
                    self.assertEqual(first["d"]["session_id"], "private-session-id")
                    self.assertEqual(first["d"]["seq"], 4)
                self.assertEqual(heartbeat, {"op": 1, "d": 6})
                client.get_access_token.assert_called_once_with()
                callback.assert_awaited_once_with("C2C_MESSAGE_CREATE", frames[3]["d"])
                logs = json.dumps(cap.events) + str(cap.runtime)
                for secret in ("private-identify-token", "private-session-id", "private-username",
                               "private-chat", "private-quote", "private-unknown-event", "private-otp-123456"):
                    self.assertNotIn(secret, logs)
                if enabled:
                    self.assertTrue(cap.events)
                    self.assertTrue(all(e["connection_id"] == "connection-local" for e in cap.events))
                    self.assertFalse(any("payload" in e for e in cap.events))

    async def test_qq_heartbeat_loop_sends_once_without_extra_token_or_network_calls(self):
        for enabled in (False, True):
            gateway = qq_gateway.QQGateway(Mock(), AsyncMock())
            gateway._connection_id = "heartbeat-connection"
            gateway._running = True
            gateway._last_seq = 8
            ws = Mock()
            async def send(text):
                gateway._running = False
            ws.send_text = AsyncMock(side_effect=send)
            gateway._ws = ws
            with capture(enabled), patch.object(qq_gateway.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                await gateway._heartbeat_loop(3.25)
            sleep.assert_awaited_once_with(3.25)
            ws.send_text.assert_awaited_once_with(json.dumps({"op": 1, "d": 8}))
            gateway.client.get_access_token.assert_not_called()

    async def test_onebot_reconnect_ids_dispatch_and_private_payloads(self):
        for enabled in (False, True):
            callback = AsyncMock()
            gateway = ob_gateway.OneBotGateway("wss://onebot.test/ws?sig=private-signature",
                                              "private-ob-token", callback)
            message = {"post_type": "message", "message": [{"data": {"text": "private-chat"}}],
                       "raw_message": "private-otp-123456",
                       "resolved": {"attachment": "private-quote"},
                       "extra": ["private-unknown"]}
            ws = Mock()
            recv_count = 0
            async def recv():
                nonlocal recv_count
                recv_count += 1
                if recv_count == 1:
                    return json.dumps(message)
                gateway._running = False
                raise RuntimeError("private-websocket-error")
            ws.recv = AsyncMock(side_effect=recv)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=[RuntimeError("private-connect-error"), ws])
            cm.__aexit__ = AsyncMock(return_value=False)
            connect = Mock(return_value=cm)
            with capture(enabled) as cap, patch.object(ob_gateway, "websockets", types.SimpleNamespace(connect=connect)), \
                    patch.object(ob_gateway.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                await gateway.start()
                await asyncio.sleep(0)
            # Let the existing create_task dispatch run after the patched sleep scope.
            await asyncio.sleep(0)
            callback.assert_awaited_once_with(message)
            self.assertEqual(connect.call_count, 2)
            expected = "wss://onebot.test/ws?sig=private-signature/&access_token=private-ob-token"
            self.assertEqual([c.args for c in connect.call_args_list], [(expected,), (expected,)])
            self.assertEqual(ws.recv.await_count, 2)
            self.assertEqual(sleep.await_args_list[0].args, (5.0,))
            logs = json.dumps(cap.events) + str(cap.runtime)
            for secret in ("private-chat", "private-otp-123456", "private-quote", "private-unknown",
                           "private-ob-token", "private-signature", "private-websocket-error", "private-connect-error"):
                self.assertNotIn(secret, logs)
            if enabled:
                starts = [e for e in cap.events if e["event"] == "connect.start"]
                self.assertEqual(len({e["connection_id"] for e in starts}), 2)
                self.assertTrue(all("payload" not in e for e in cap.events))

    async def test_qq_reconnect_backoff_and_connection_arguments_are_unchanged(self):
        for enabled in (False, True):
            client = Mock()
            client.get_gateway_url.return_value = "wss://gateway.test/ws?sig=private-signature"
            gateway = qq_gateway.QQGateway(client, AsyncMock(), proxy_url="http://proxy.test", debug=True)
            delays = []
            async def sleep(delay):
                delays.append(delay)
                if len(delays) == 3:
                    gateway._running = False
            with capture(enabled) as cap, patch.object(qq_gateway.QQWebSocket, "connect", new_callable=AsyncMock,
                    side_effect=[transport.QQWSClosed("private-closed"), RuntimeError("private-error"),
                                 transport.QQWSClosed("private-closed")]) as connect, \
                    patch.object(qq_gateway.asyncio, "sleep", side_effect=sleep):
                await gateway.start()
            self.assertEqual(delays, [1.0, 2.0, 4.0])
            self.assertEqual(client.get_gateway_url.call_count, 3)
            self.assertEqual(connect.await_args_list, [unittest.mock.call(
                "wss://gateway.test/ws?sig=private-signature", proxy_url="http://proxy.test",
                ws_guid=gateway.ws_guid, user_agent=gateway.user_agent)] * 3)
            logs = json.dumps(cap.events) + str(cap.runtime)
            for secret in ("private-closed", "private-error", "private-signature"):
                self.assertNotIn(secret, logs)
            if enabled:
                starts = [e for e in cap.events if e["event"] == "connect.start"]
                self.assertEqual(len({e["connection_id"] for e in starts}), 3)

    async def test_gateway_dispatch_exceptions_still_swallowed_without_secret_logs(self):
        for gateway, method, args in (
                (qq_gateway.QQGateway(Mock(), AsyncMock(side_effect=ValueError("private-handler"))),
                 "_safe_dispatch", ("UNKNOWN", {"content": "private-chat"})),
                (ob_gateway.OneBotGateway("wss://test", "token", AsyncMock(side_effect=ValueError("private-handler"))),
                 "_safe_dispatch", ({"message": "private-chat"},))):
            with capture(True) as cap:
                await getattr(gateway, method)(*args)
            self.assertNotIn("private-handler", str(cap.runtime))
            self.assertNotIn("private-chat", str(cap.runtime))


class SocketLoggingTests(unittest.TestCase):
    def test_handshake_toggle_preserves_proxy_tls_reads_sends_and_leftover(self):
        wire = []
        for enabled in (False, True):
            sock = Mock()
            key = base64.b64encode(b'k' * 16).decode()
            accept = transport._expected_accept(key)
            response = ("HTTP/1.1 101 Switching Protocols\r\nConnection: close\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\nSet-Cookie: private-cookie-value\r\n\r\n").encode()
            sock.recv.side_effect = [b'HTTP/1.1 200 OK\r\n\r\n', response[:20], response[20:] + b'private-leftover-otp']
            ctx = Mock()
            ctx.wrap_socket.return_value = sock
            with capture(enabled) as cap, patch.object(transport.socket, "create_connection", return_value=sock) as connect, \
                    patch.object(transport.ssl, "create_default_context", return_value=ctx), \
                    patch.object(transport.os, "urandom", return_value=b'k' * 16) as random, \
                    patch.object(transport, "new_id", side_effect=["handshake-connection", "handshake-request"]):
                result = transport._blocking_handshake("gateway.test", 443, "/ws?sig=private-signed-query",
                                                       "http://private-user:private-password@proxy.test:3129")
            self.assertEqual(result, (sock, b'private-leftover-otp'))
            connect.assert_called_once_with(("proxy.test", 3129), timeout=20)
            ctx.set_alpn_protocols.assert_called_once_with(["http/1.1"])
            ctx.wrap_socket.assert_called_once_with(sock, server_hostname="gateway.test")
            random.assert_called_once_with(16)
            self.assertEqual(sock.recv.call_args_list, [unittest.mock.call(4096)] * 3)
            self.assertEqual(sock.sendall.call_count, 2)
            self.assertIn(b'Proxy-Authorization: Basic ', sock.sendall.call_args_list[0].args[0])
            self.assertIn(b'GET /ws?sig=private-signed-query HTTP/1.1', sock.sendall.call_args_list[1].args[0])
            wire.append(sock.mock_calls)
            logs = json.dumps(cap.events) + str(cap.runtime)
            for secret in ("private-user", "private-password", "private-signed-query",
                           "private-cookie-value", "private-leftover-otp",
                           base64.b64encode(b'private-user:private-password').decode()):
                self.assertNotIn(secret, logs)
            if enabled:
                self.assertEqual([e["event"] for e in cap.events],
                                 ["handshake.start", "handshake.response", "handshake.end"])
                self.assertEqual(len({e["request_id"] for e in cap.events}), 1)
                self.assertEqual(len({e["connection_id"] for e in cap.events}), 1)
        self.assertEqual(wire[0], wire[1])

    def test_handshake_failure_keeps_exception_and_does_not_retry_or_close(self):
        for enabled in (False, True):
            sock = Mock()
            sock.recv.return_value = b'HTTP/1.1 403 private-reason\r\n\r\nprivate-body'
            ctx = Mock()
            ctx.wrap_socket.return_value = sock
            with capture(enabled) as cap, patch.object(transport.socket, "create_connection", return_value=sock) as connect, \
                    patch.object(transport.ssl, "create_default_context", return_value=ctx):
                with self.assertRaisesRegex(transport.QQWSError, "handshake: expected 101"):
                    transport._blocking_handshake("gateway.test", 443, "/ws")
            connect.assert_called_once_with(("gateway.test", 443), timeout=20)
            sock.recv.assert_called_once_with(4096)
            sock.sendall.assert_called_once()
            sock.close.assert_not_called()
            self.assertNotIn("private-reason", json.dumps(cap.events))
            self.assertNotIn("private-body", json.dumps(cap.events))
            if enabled:
                self.assertEqual(cap.events[-1]["event"], "handshake.error")

    def test_frame_loop_toggle_preserves_fragment_ping_pong_binary_and_reads(self):
        outcomes = []
        def frame(opcode, payload, fin=True):
            return bytes([(0x80 if fin else 0) | opcode, len(payload)]) + payload
        incoming = (frame(1, b'private-otp-', False) + frame(0, b'123456') +
                    frame(2, b'private-binary') + frame(9, b'private-ping') +
                    frame(10, b'private-pong') + frame(8, b'private-close-reason'))
        for enabled in (False, True):
            ws = object.__new__(transport.QQWebSocket)
            sock = Mock()
            sock.recv.return_value = incoming
            ws._connection_id = "frame-connection"
            ws._sock = sock
            ws._send_q = queue.Queue()
            ws._recv_q = queue.Queue()
            ws._closed = threading.Event()
            ws._close_reason = ""
            ws._loop = Mock()
            ws._loop.call_soon_threadsafe.side_effect = lambda callback, value: callback(value)
            ws._send_q.put(b'\x81\x80mask')
            with capture(enabled) as cap, patch.object(transport.select, "select", return_value=([sock], [sock], [])) as select_, \
                    patch.object(transport.os, "urandom", return_value=b'mask') as random:
                ws._io_loop(sock, b'')
            select_.assert_called_once_with([sock], [sock], [], 0.5)
            sock.setblocking.assert_called_once_with(False)
            sock.recv.assert_called_once_with(65536)
            sock.sendall.assert_called_once_with(b'\x81\x80mask')
            sock.close.assert_called_once_with()
            random.assert_called_once_with(4)
            items = [ws._recv_q.get_nowait() for _ in range(3)]
            self.assertEqual(items[:2], ["private-otp-123456", str(b'private-binary')])
            self.assertIsInstance(items[2], transport.QQWSClosed)
            self.assertTrue(ws._recv_q.empty())
            pong = ws._send_q.get_nowait()
            self.assertEqual(pong[0] & 0x0F, 10)
            decoded = bytes(b ^ pong[2:6][i % 4] for i, b in enumerate(pong[6:]))
            self.assertEqual(decoded, b'private-ping')
            self.assertTrue(ws._send_q.empty())
            outcomes.append((sock.mock_calls, [str(item) for item in items], pong))
            logs = json.dumps(cap.events) + str(cap.runtime)
            for secret in ("private-otp-", "123456", "private-binary", "private-ping",
                           "private-pong", "private-close-reason"):
                self.assertNotIn(secret, logs)
            if enabled:
                frames = [e for e in cap.events if e["event"] == "frame.receive"]
                self.assertEqual([e["opcode"] for e in frames], [1, 0, 2, 9, 10, 8])
                self.assertTrue(all(e["connection_id"] == "frame-connection" for e in cap.events))
                self.assertFalse(any("payload" in e for e in cap.events))
        self.assertEqual(outcomes[0], outcomes[1])


class AsyncSocketLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_recv_close_keep_queue_and_frame_encoding_unchanged(self):
        outcomes = []
        for enabled in (False, True):
            with patch.object(transport.threading, "Thread") as thread:
                ws = transport.QQWebSocket(Mock(), b'private-leftover', asyncio.get_running_loop())
                other = transport.QQWebSocket(Mock(), b'', asyncio.get_running_loop())
            self.assertNotEqual(ws._connection_id, other._connection_id)
            self.assertEqual(thread.call_count, 2)
            ws._recv_q.put_nowait("private-received-text")
            error = transport.QQWSClosed("private-close-error")
            ws._recv_q.put_nowait(error)
            with capture(enabled) as cap, patch.object(transport.os, "urandom", return_value=b'mask') as random:
                await ws.send_text("private-outgoing-token")
                self.assertEqual(await ws.recv(), "private-received-text")
                with self.assertRaises(transport.QQWSClosed) as raised:
                    await ws.recv()
                self.assertIs(raised.exception, error)
                await ws.close()
            self.assertEqual(random.call_args_list, [unittest.mock.call(4), unittest.mock.call(4)])
            ws._sock.close.assert_called_once_with()
            outcomes.append([ws._send_q.get_nowait(), ws._send_q.get_nowait()])
            self.assertTrue(ws._send_q.empty())
            logs = json.dumps(cap.events) + str(cap.runtime)
            for secret in ("private-outgoing-token", "private-received-text", "private-close-error", "private-leftover"):
                self.assertNotIn(secret, logs)
            with self.assertRaises(transport.QQWSClosed):
                await ws.send_text("not sent")
            self.assertTrue(ws._send_q.empty())
        self.assertEqual(outcomes[0], outcomes[1])


if __name__ == "__main__":
    unittest.main()
