"""Offline Worker routing invariants: no live Muse/Cloudflare traffic."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from muse_bridge.config.models import ConfigLoader, MuseConfig, WorkerConfig
from muse_bridge.engine import BridgeEngine
from muse_bridge.muse.client.client import MuseClient
from muse_bridge.muse.client.errors import CredentialRefreshError, HandshakeError
from muse_bridge.muse.client.transport import WS
from muse_bridge.muse.login import MuseProtocolLogin
from muse_bridge.muse.routing import auth_url, cookie_domain, http_base, worker_http_options
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.token_manager import MuseTokenManager

BASE = "https://your-worker.example.com"
PREFIX = BASE + "/https/muse.ai"
VM = "01234567-89ab-cdef-0123-456789abcdef"


class RoutingTests(unittest.TestCase):
    def test_bases_and_independent_switches(self):
        for ws in (False, True):
            for http in (False, True):
                for login in (False, True):
                    worker = WorkerConfig(ws=ws, http=http, login=login)
                    self.assertEqual(http_base("https://muse.ai/", worker),
                                     PREFIX if http else "https://muse.ai")
                    self.assertEqual(http_base("https://muse.ai", worker, login=True),
                                     PREFIX if login else "https://muse.ai")

    def test_auth_urls_and_switchable_origin(self):
        direct = "https://auth.muse.ai/aymh/?origin=https://muse.ai"
        self.assertEqual(auth_url("https://muse.ai", WorkerConfig()), direct)
        self.assertEqual(auth_url("https://muse.ai", WorkerConfig(http=True)), direct)
        self.assertEqual(auth_url("https://muse.ai", WorkerConfig(login=True)),
                         BASE + "/https/auth.muse.ai/aymh/?origin=https://muse.ai")
        self.assertEqual(auth_url("https://muse.ai", WorkerConfig(login=True, login_origin="worker")),
                         BASE + "/https/auth.muse.ai/aymh/?origin=" + PREFIX)
        self.assertEqual(auth_url("https://example.test", WorkerConfig()),
                         "https://auth.example.test/aymh/?origin=https://example.test")

    def test_cookie_domain_boundaries(self):
        for worker in (WorkerConfig(), WorkerConfig(login=True)):
            for domain in ("muse.ai", ".muse.ai", "auth.muse.ai", ".AUTH.MUSE.AI"):
                self.assertEqual(cookie_domain(domain, worker), "muse.ai")
            for domain in ("evil-muse.ai", "muse.ai.evil.test", "", "eu.org", "other.test"):
                self.assertIsNone(cookie_domain(domain, worker))
        self.assertIsNone(cookie_domain("your-worker.example.com", WorkerConfig()))
        self.assertEqual(cookie_domain(".your-worker.example.com", WorkerConfig(login=True)), "muse.ai")
        self.assertIsNone(cookie_domain("evil.your-worker.example.com", WorkerConfig(login=True)))

    def test_invalid_worker_config_fails(self):
        for value in ("http://worker.test", "https://worker.test/prefix", "https://worker.test?q=1",
                      "https://user:pw@worker.test", "https://worker.test:444", "https://worker.test/#x", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkerConfig(base=value)
        for key in ("ws", "http", "login", "ws_permessage_deflate"):
            with self.assertRaises(ValueError):
                WorkerConfig(**{key: "false"})
        with self.assertRaises(ValueError):
            WorkerConfig(login_origin="unknown")

    def test_egress_detection_is_never_called_in_any_worker_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for channel in ("ws", "http", "login"):
                path.write_text(json.dumps({"muse": {"worker": {channel: True}}}), encoding="utf-8")
                with patch("muse_bridge.config.models._detect_egress_ip") as detect:
                    config = ConfigLoader.load(str(path))
                detect.assert_not_called()
                self.assertEqual(config.muse.client_ip, "")
            path.write_text(json.dumps({"muse": {"worker": {"ws": True}, "client_ip": "203.0.113.4"}}))
            with patch("muse_bridge.config.models._detect_egress_ip") as detect:
                self.assertEqual(ConfigLoader.load(str(path)).muse.client_ip, "203.0.113.4")
            detect.assert_not_called()
            path.write_text("{}")
            with patch("muse_bridge.config.models._detect_egress_ip", return_value="203.0.113.5") as detect:
                config = ConfigLoader.load(str(path))
            detect.assert_called_once()
            self.assertEqual(config.muse.client_ip, "203.0.113.5")

    def test_engine_and_service_pass_worker_config(self):
        worker = WorkerConfig(ws=True, http=True, login=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"muse": {"mint_file": "", "worker": {"ws": True, "http": True}},
                                        "adapters": {"qq": {"enabled": False}}}))
            engine = BridgeEngine(ConfigLoader.load(str(path)))
            self.addCleanup(engine.muse_service.interactions.close)
            self.assertEqual(engine.token_manager.base_url, PREFIX)
        manager = MagicMock()
        manager.get_credentials.return_value = ("t", "n", "vm")
        service = MuseService(MuseConfig(worker=worker), manager)
        self.addCleanup(service.interactions.close)
        self.assertIs(service.create_client().worker, worker)


class WebSocketTests(unittest.TestCase):
    def test_dial_url_preserves_query_and_direct_route(self):
        for enabled in (False, True):
            worker = WorkerConfig(ws=enabled)
            client = MuseClient("a+/=", "b+/=", "vm", "", direct=False,
                                proxy_url="http://local:8080", worker=worker)
            with patch("muse_bridge.muse.client.client.uuid.uuid4", return_value=MagicMock(hex="id", __str__=lambda _: "request")), \
                 patch("muse_bridge.muse.client.client.WS") as dial, \
                 patch("muse_bridge.muse.client.client.noise_handshake", return_value=("send", "recv")):
                client.connect()
            query = "vm_id=vm&auth_token=a%2B%2F%3D&notary_token=b%2B%2F%3D&app_id=hatch-web&request_id=request"
            expected = ("wss://your-worker.example.com/wss/hatch.metaaivm.com" if enabled
                        else "wss://hatch.metaaivm.com") + "/v1/noise?" + query
            self.assertEqual(dial.call_args.args, (expected,))
            self.assertEqual(dial.call_args.kwargs,
                             {"direct": True} if enabled else {"direct": False, "proxy_url": "http://local:8080"})

    def test_disable_compression_only_on_worker_and_no_fallback(self):
        for enabled in (False, True):
            client = MuseClient("t", "n", "vm", "", direct=True,
                                worker=WorkerConfig(ws=enabled, ws_permessage_deflate=False))
            with patch("muse_bridge.muse.client.client.WS", side_effect=OSError("unavailable")) as dial:
                with self.assertRaises(HandshakeError):
                    client.connect()
            dial.assert_called_once()
            self.assertEqual(dial.call_args.kwargs.get("permessage_deflate"), False if enabled else None)

    def test_wire_extension_toggle_leaves_other_headers_identical(self):
        requests = []
        for advertise in (True, False):
            raw_sock = MagicMock()
            tls_sock = MagicMock()
            context = MagicMock()
            context.wrap_socket.return_value = tls_sock
            with patch("muse_bridge.muse.client.transport.socket.create_connection", return_value=raw_sock) as connect, \
                 patch("muse_bridge.muse.client.transport.ssl.create_default_context", return_value=context), \
                 patch("muse_bridge.muse.client.transport.os.urandom", return_value=b"a" * 16), \
                 patch.object(WS, "_read_until", return_value=b"HTTP/1.1 101 Switching Protocols\r\n\r\n"):
                ws = WS("wss://worker.test/wss/hatch.metaaivm.com/v1/noise?x=1", direct=True,
                        permessage_deflate=advertise)
            connect.assert_called_once_with(("worker.test", 443), timeout=15)
            context.wrap_socket.assert_called_once_with(raw_sock, server_hostname="worker.test")
            requests.append(tls_sock.sendall.call_args.args[0])
            ws.close()
        extension = b"Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
        self.assertIn(extension, requests[0])
        self.assertNotIn(extension, requests[1])
        self.assertEqual(requests[0].replace(extension, b""), requests[1])
        self.assertIn(b"Host: worker.test\r\n", requests[0])
        self.assertIn(b"Origin: https://muse.ai\r\n", requests[0])


class HTTPTests(unittest.TestCase):
    def login_client(self, worker, handler):
        auth = MuseProtocolLogin(worker=worker)
        auth.client.close()
        auth.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                                   **worker_http_options(worker, login=True))
        self.addCleanup(auth.client.close)
        return auth

    def test_follow_prefixed_redirect_and_accept_cookie_then_send_to_api(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "your-worker.example.com" and request.url.path.endswith("/aymh/"):
                return httpx.Response(302, headers={"Location": PREFIX + "/?aymh_complete=1",
                    "Set-Cookie": "hatch_sess=synthetic; Domain=your-worker.example.com; Path=/https/; Secure; HttpOnly"})
            if request.url.path.endswith("/restart"):
                return httpx.Response(201, json={"csrf_token": "csrf"})
            if request.url.path.endswith("/send-otp"):
                return httpx.Response(201, json={"ok": True})
            return httpx.Response(200)
        auth = self.login_client(WorkerConfig(login=True), handler)
        auth.send_otp("test@example.test")
        self.assertEqual(len(requests), 4)
        self.assertTrue(all(r.url.host == "your-worker.example.com" for r in requests))
        self.assertEqual(requests[-1].headers["Cookie"], "hatch_sess=synthetic")
        self.assertEqual(requests[-1].headers["Origin"], "https://muse.ai")
        saved = auth.extract_cookies()
        self.assertEqual(saved["cookie"], "hatch_sess=synthetic")
        self.assertEqual(saved["cookies"][0]["domain"], "muse.ai")
        self.assertEqual(saved["cookies"][0]["path"], "/")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MUSE_COOKIE": ""}):
            path = Path(tmp) / "cookies.json"
            for data in (saved, {"cookies": saved["cookies"]}, saved["cookies"]):
                path.write_text(json.dumps(data), encoding="utf-8")
                for worker in (WorkerConfig(), WorkerConfig(http=True)):
                    manager = MuseTokenManager(mint_file="", cookies_file=str(path), worker=worker)
                    self.assertEqual(manager.load_cookie_string(), "hatch_sess=synthetic")

    def test_redirect_escape_and_http_error_fail_without_landing_fallback(self):
        for status, headers in ((302, {"Location": "https://muse.ai/"}), (503, {})):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, headers=headers)
            auth = self.login_client(WorkerConfig(login=True), handler)
            with self.assertRaises(RuntimeError):
                auth.initialize_session()
            self.assertEqual(len(requests), 1)

    def test_worker_http_guard_both_channels_and_no_environment_proxy(self):
        self.assertEqual(worker_http_options(WorkerConfig()), {})
        for login in (False, True):
            options = worker_http_options(WorkerConfig(http=not login, login=login), login=login)
            self.assertFalse(options["trust_env"])
            guard = options["event_hooks"]["request"][0]
            guard(httpx.Request("GET", PREFIX + "/"))
            guard(httpx.Request("GET", BASE + "/http/auth.muse.ai/redirect"))
            for url in ("https://muse.ai/", BASE + "/evil", "https://other.test/https/muse.ai/", BASE + ":444/https/muse.ai/"):
                with self.assertRaises(RuntimeError):
                    guard(httpx.Request("GET", url))

    def test_direct_login_unchanged(self):
        with patch("muse_bridge.muse.login.httpx.Client") as client:
            auth = MuseProtocolLogin()
        client.assert_called_once_with(http2=True, follow_redirects=True, timeout=25.0)
        auth.client.cookies.jar = []
        self.assertEqual(auth.extract_cookies(), {"cookie": ""})

    def test_token_http_calls_all_route_through_worker_but_vm_address_does_not(self):
        manager = MuseTokenManager(mint_file="", worker=WorkerConfig(http=True))
        seen = []
        def handler(request):
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=f"{VM}.metaaivm.com")
            return httpx.Response(200, json={"token": "token", "notary_token": "notary"})
        factory = httpx.Client
        def make_client(**kwargs):
            return factory(transport=httpx.MockTransport(handler), **kwargs)
        with patch("muse_bridge.muse.token_manager.httpx.Client", side_effect=make_client), \
             patch.object(manager, "load_cookie_string", return_value="hatch_sess=synthetic"):
            self.assertEqual(manager.refresh_from_cookies(), ("token", "notary", VM))
        self.assertEqual([str(r.url) for r in seen], [PREFIX + "/", PREFIX + "/api/hatch/token"])
        self.assertEqual(json.loads(seen[-1].content)["vmAddress"], f"wss://{VM}.metaaivm.com/")
        self.assertEqual(seen[-1].headers["Origin"], "https://muse.ai")

    def test_worker_vm_failure_does_not_retry_direct(self):
        manager = MuseTokenManager(mint_file="", worker=WorkerConfig(http=True))
        factory = httpx.Client
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://muse.ai/"})
        with patch("muse_bridge.muse.token_manager.httpx.Client",
                   side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            with self.assertRaises(CredentialRefreshError):
                manager.vm_id_from_cookies("hatch_sess=synthetic")
        self.assertEqual(len(seen), 1)

    def test_netscape_and_json_cookie_domains(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MUSE_COOKIE": ""}):
            path = Path(tmp) / "cookies.txt"
            path.write_text(".muse.ai\tTRUE\t/\tTRUE\t0\ta\treal\n"
                            "your-worker.example.com\tFALSE\t/\tTRUE\t0\tb\tproxy\n"
                            "muse.ai.evil.test\tFALSE\t/\tTRUE\t0\tc\tevil\n")
            for worker, expected in ((WorkerConfig(), "a=real"), (WorkerConfig(login=True), "a=real; b=proxy")):
                manager = MuseTokenManager(mint_file="", cookies_file=str(path), worker=worker)
                self.assertEqual(manager.load_cookie_string(), expected)


if __name__ == "__main__":
    unittest.main()
