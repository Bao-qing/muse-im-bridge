"""Offline unified network & Worker routing tests (no live traffic)."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx

import manage
from muse_bridge.adapters.onebot.client import OneBotClient
from muse_bridge.adapters.qq.client import QQClient
from muse_bridge.adapters.telegram.client import TelegramClient
from muse_bridge.adapters.wechat.client import DEFAULT_API, WeChatClient
from muse_bridge.config.models import (
    AppConfig,
    ConfigLoader,
    MuseConfig,
    NetworkConfig,
    OneBotConfig,
    QQConfig,
    TelegramConfig,
    WeChatConfig,
    WorkerConfig,
)
from muse_bridge.engine import BridgeEngine
from muse_bridge.muse.client.client import MuseClient
from muse_bridge.muse.client.errors import CredentialRefreshError, HandshakeError
from muse_bridge.muse.client.transport import WS
from muse_bridge.muse.login import MuseProtocolLogin
from muse_bridge.muse.routing import (
    ClientIPResolver,
    auth_url,
    cookie_domain,
    fetch_client_ip,
    http_base,
    muse_http_client,
    muse_http_hooks,
)
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.token_manager import MuseTokenManager
from muse_bridge.network import DIRECT_ROUTE, NetworkRoute, resolve_route, validate_app_network, worker_url

BASE = "https://your-worker.example.com"
MUSE_PREFIX = BASE + "/https/muse.ai"
TG_PREFIX = BASE + "/https/api.telegram.org"
VM = "01234567-89ab-cdef-0123-456789abcdef"


class NetworkConfigTests(unittest.TestCase):
    def test_defaults_and_proxy_modes(self):
        cfg = AppConfig()
        self.assertEqual(cfg.network.proxy_url, "")
        self.assertEqual(cfg.network.worker.base, "")
        self.assertTrue(cfg.network.worker.ws)
        self.assertTrue(cfg.network.worker.http)
        self.assertEqual(cfg.muse.proxy, "none")
        self.assertEqual(cfg.telegram.proxy, "worker")
        self.assertEqual(cfg.qq.proxy, "none")
        self.assertEqual(cfg.onebot.proxy, "none")
        self.assertEqual(cfg.wechat.proxy, "none")

    def test_invalid_network_and_proxy_modes_fail(self):
        for bad_base in (
            "http://worker.test",
            "https://worker.test/prefix",
            "https://worker.test?q=1",
            "https://user:pw@worker.test",
            "https://worker.test:444",
            "https://worker.test/#x",
        ):
            with self.subTest(base=bad_base), self.assertRaises(ValueError):
                WorkerConfig(base=bad_base)
        for key in ("ws", "http"):
            with self.assertRaises(ValueError):
                WorkerConfig(**{key: "false"})
        for bad_proxy in ("socks5://127.0.0.1:1080", "ftp://proxy.test", "http://:8080", "not-a-url"):
            with self.subTest(proxy=bad_proxy), self.assertRaises(ValueError):
                NetworkConfig(proxy_url=bad_proxy)
        for cls in (MuseConfig, QQConfig, OneBotConfig, TelegramConfig, WeChatConfig):
            with self.subTest(cls=cls.__name__), self.assertRaises(ValueError):
                cls(proxy="proxy_url")
        with self.assertRaises(ValueError):
            AppConfig(muse=MuseConfig(proxy="http"), network=NetworkConfig(proxy_url=""))

    def test_resolve_route_and_validate_app_network(self):
        net = NetworkConfig(
            proxy_url="http://127.0.0.1:7890",
            worker=WorkerConfig(base=BASE, ws=True, http=True),
        )
        self.assertEqual(resolve_route("none", net), DIRECT_ROUTE)
        http_route = resolve_route("http", net)
        self.assertEqual(http_route.http_proxy, "http://127.0.0.1:7890")
        self.assertEqual(http_route.ws_proxy, "http://127.0.0.1:7890")
        worker_route = resolve_route("worker", net)
        self.assertTrue(worker_route.use_worker_http)
        self.assertTrue(worker_route.use_worker_ws)
        self.assertEqual(worker_route.http_url("https://api.telegram.org/bot1:a/getMe"),
                         TG_PREFIX + "/bot1:a/getMe")
        self.assertEqual(worker_route.ws_url("wss://hatch.metaaivm.com/v1/noise?x=1"),
                         "wss://your-worker.example.com/wss/hatch.metaaivm.com/v1/noise?x=1")

        with self.assertRaisesRegex(ValueError, "network.worker.base"):
            resolve_route("worker", NetworkConfig(), section="adapters.telegram")
        with self.assertRaisesRegex(ValueError, "network.proxy_url"):
            resolve_route("http", NetworkConfig(), section="muse")

        # Disabled telegram with proxy="worker" does not require worker.base when muse is direct.
        cfg = AppConfig(
            muse=MuseConfig(proxy="none"),
            qq=QQConfig(enabled=False),
            telegram=TelegramConfig(enabled=False, proxy="worker"),
            network=NetworkConfig(),
        )
        validate_app_network(cfg)
        cfg_enabled_tg = AppConfig(
            muse=MuseConfig(proxy="none"),
            qq=QQConfig(enabled=False),
            telegram=TelegramConfig(enabled=True, bot_token="123:abc", proxy="worker"),
            network=NetworkConfig(),
        )
        with self.assertRaisesRegex(ValueError, "adapters.telegram"):
            validate_app_network(cfg_enabled_tg)

    def test_config_loader_does_not_probe_egress_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for raw in (
                {"network": {"worker": {"base": BASE}}, "muse": {"proxy": "worker"}},
                {"network": {"proxy_url": "http://127.0.0.1:7890"}, "muse": {"proxy": "http"}},
                {"muse": {"proxy": "none"}},
            ):
                with self.subTest(raw=raw):
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    cfg = ConfigLoader.load(str(path))
                    self.assertEqual(cfg.muse.client_ip, "")

    def test_manage_registered_fields_match_dataclasses(self):
        import dataclasses
        reg = manage.get_registered_config_fields()
        for cls in (NetworkConfig, WorkerConfig, MuseConfig, QQConfig, OneBotConfig, TelegramConfig, WeChatConfig):
            actual = {f.name for f in dataclasses.fields(cls)}
            self.assertEqual(reg[cls.__name__], actual, cls.__name__)


class MuseRoutingTests(unittest.TestCase):
    def test_http_base_auth_url_and_cookie_domain(self):
        worker_on = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True, worker_ws=True)
        worker_http_off = NetworkRoute(mode="worker", worker_base=BASE, worker_http=False, worker_ws=True)
        self.assertEqual(http_base("https://muse.ai/", worker_on), MUSE_PREFIX)
        self.assertEqual(http_base("https://muse.ai/", worker_http_off), "https://muse.ai")
        self.assertEqual(auth_url("https://muse.ai", DIRECT_ROUTE), "https://auth.muse.ai/aymh/?origin=https://muse.ai")
        self.assertEqual(
            auth_url("https://muse.ai", worker_on),
            BASE + "/https/auth.muse.ai/aymh/?origin=https://muse.ai",
        )
        for route in (DIRECT_ROUTE, worker_on):
            for domain in ("muse.ai", ".muse.ai", "auth.muse.ai", ".AUTH.MUSE.AI"):
                self.assertEqual(cookie_domain(domain, route), "muse.ai")
            for domain in ("evil-muse.ai", "muse.ai.evil.test", "", "eu.org"):
                self.assertIsNone(cookie_domain(domain, route))
        self.assertIsNone(cookie_domain("your-worker.example.com", DIRECT_ROUTE))
        self.assertEqual(cookie_domain(".your-worker.example.com", worker_on), "muse.ai")
        self.assertIsNone(cookie_domain("evil.your-worker.example.com", worker_on))

    def test_engine_and_service_pass_resolved_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "network": {"worker": {"base": BASE, "ws": True, "http": True}},
                "muse": {"mint_file": "", "proxy": "worker"},
                "adapters": {"qq": {"enabled": False}},
            }), encoding="utf-8")
            engine = BridgeEngine(ConfigLoader.load(str(path)))
            self.addCleanup(engine.muse_service.interactions.close)
            self.assertEqual(engine.token_manager.base_url, MUSE_PREFIX)
            self.assertEqual(engine.muse_service.route.worker_base, BASE)

        route = NetworkRoute(mode="worker", worker_base=BASE, worker_ws=True, worker_http=True)
        manager = MagicMock()
        manager.get_credentials.return_value = ("t", "n", "vm")
        service = MuseService(MuseConfig(proxy="worker"), manager, route=route)
        self.addCleanup(service.interactions.close)
        self.assertIs(service.create_client().route, route)

    def test_ws_dial_url_preserves_query_across_modes(self):
        query = "vm_id=vm&auth_token=a%2B%2F%3D&notary_token=b%2B%2F%3D&app_id=hatch-web&request_id=request"
        cases = (
            (DIRECT_ROUTE, f"wss://hatch.metaaivm.com/v1/noise?{query}", {"direct": True}),
            (
                NetworkRoute(mode="http", proxy_url="http://local:8080"),
                f"wss://hatch.metaaivm.com/v1/noise?{query}",
                {"proxy_url": "http://local:8080", "direct": False},
            ),
            (
                NetworkRoute(mode="worker", worker_base=BASE, worker_ws=True, worker_http=True),
                f"wss://your-worker.example.com/wss/hatch.metaaivm.com/v1/noise?{query}",
                {"direct": True},
            ),
            (
                NetworkRoute(mode="worker", worker_base=BASE, worker_ws=False, worker_http=True),
                f"wss://hatch.metaaivm.com/v1/noise?{query}",
                {"direct": True},
            ),
        )
        for route, expected_url, expected_kwargs in cases:
            with self.subTest(route=route):
                client = MuseClient("a+/=", "b+/=", "vm", "", route=route)
                with patch("muse_bridge.muse.client.client.uuid.uuid4", return_value=MagicMock(hex="id", __str__=lambda _: "request")), \
                     patch("muse_bridge.muse.client.client.WS") as dial, \
                     patch("muse_bridge.muse.client.client.noise_handshake", return_value=("send", "recv")):
                    client.connect()
                self.assertEqual(dial.call_args.args, (expected_url,))
                self.assertEqual(dial.call_args.kwargs, expected_kwargs)

    def test_worker_ws_failure_does_not_fallback_direct(self):
        route = NetworkRoute(mode="worker", worker_base=BASE, worker_ws=True)
        client = MuseClient("t", "n", "vm", "", route=route)
        with patch("muse_bridge.muse.client.client.WS", side_effect=OSError("unavailable")) as dial:
            with self.assertRaises(HandshakeError):
                client.connect()
        dial.assert_called_once()

    def test_worker_login_and_redirect_escape_guard(self):
        route = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True, worker_ws=True)
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/aymh/"):
                return httpx.Response(302, headers={
                    "Location": BASE + "/https/muse.ai/?aymh_complete=1",
                    "Set-Cookie": "hatch_sess=synthetic; Domain=your-worker.example.com; Path=/https/; Secure; HttpOnly",
                })
            if request.url.path.endswith("/api/auth/native/restart"):
                return httpx.Response(201, json={"csrf_token": "csrf"})
            if request.url.path.endswith("/api/auth/native/send-otp"):
                return httpx.Response(201, json={"ok": True})
            if request.url.path.endswith("/api/auth/native/confirm-otp"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, text="ok")

        auth = MuseProtocolLogin(route=route)
        auth.client.close()
        auth.client = muse_http_client(route, follow_redirects=True, transport=httpx.MockTransport(handler))
        auth.send_otp("u@example.com")
        auth.confirm_otp("123456")
        extracted = auth.extract_cookies()
        auth.client.close()
        self.assertTrue(all(r.url.host == "your-worker.example.com" for r in requests))
        self.assertEqual(extracted["cookie"], "hatch_sess=synthetic")
        self.assertEqual(extracted["cookies"][0]["domain"], "muse.ai")
        self.assertEqual(extracted["cookies"][0]["path"], "/")

        # Escape guard rejects redirect leaving Worker relay.
        hooks = muse_http_hooks(route)
        guard = hooks["request"][0]
        with self.assertRaisesRegex(RuntimeError, "escaped"):
            guard(httpx.Request("GET", "https://muse.ai/"))
        guard(httpx.Request("GET", MUSE_PREFIX + "/"))

    def test_fetch_client_ip_routes_and_validation(self):
        seen = []

        def handler(request):
            seen.append((str(request.url), request.headers.get("cookie")))
            return httpx.Response(200, json={"ip": "203.0.113.9"})

        worker_route = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True, worker_ws=True)
        self.assertEqual(
            fetch_client_ip("hatch_sess=abc", DIRECT_ROUTE, transport=httpx.MockTransport(handler)),
            "203.0.113.9",
        )
        self.assertEqual(
            fetch_client_ip("hatch_sess=abc", worker_route, transport=httpx.MockTransport(handler)),
            "203.0.113.9",
        )
        self.assertEqual(seen, [
            ("https://muse.ai/api/client-ip", "hatch_sess=abc"),
            (MUSE_PREFIX + "/api/client-ip", "hatch_sess=abc"),
        ])

        # Empty cookie skips network entirely.
        called = False

        def fail_handler(request):
            nonlocal called
            called = True
            return httpx.Response(200, json={"ip": "203.0.113.9"})

        self.assertEqual(fetch_client_ip("", DIRECT_ROUTE, transport=httpx.MockTransport(fail_handler)), "")
        self.assertFalse(called)

        # Non-200, invalid JSON, or invalid IP return "" silently.
        for resp in (
            httpx.Response(401, json={"error": "Authentication required"}),
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json={"ip": "not-an-ip"}),
            httpx.Response(200, json={}),
        ):
            with self.subTest(status=resp.status_code, body=resp.text):
                self.assertEqual(
                    fetch_client_ip("hatch_sess=abc", DIRECT_ROUTE, transport=httpx.MockTransport(lambda _: resp)),
                    "",
                )

    def test_client_ip_resolver_lazy_single_flight_and_send_message_omission(self):
        # Explicit configured IP never triggers network fetch.
        explicit_fetcher = MagicMock(return_value="203.0.113.99")
        explicit = ClientIPResolver(lambda: "hatch_sess=abc", configured_ip="198.51.100.7", fetcher=explicit_fetcher)
        self.assertEqual(explicit.get(), "198.51.100.7")
        explicit_fetcher.assert_not_called()

        # Lazy single-flight warm-up + cache reuse.
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_fetcher(cookie, route, *, base_url, timeout):
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=2.0)
            return "203.0.113.42"

        resolver = ClientIPResolver(lambda: "hatch_sess=abc", fetcher=slow_fetcher)
        client = MuseClient("t", "n", "vm", "", client_ip_provider=resolver.get)
        client.request = MagicMock(return_value=b'{"message_id":"m1"}')

        # First send is non-blocking and omits client_ip while warming cache.
        client.send_message("sid", "hello")
        first_payload = json.loads(client.request.call_args.args[2])
        self.assertNotIn("client_ip", first_payload)
        self.assertTrue(started.wait(timeout=2.0))

        # Concurrent call while in flight deduplicates without starting a second fetch.
        self.assertEqual(resolver.get(), "")
        release.set()

        # Wait briefly for background thread to store cached IP.
        for _ in range(50):
            if resolver.peek():
                break
            threading.Event().wait(0.01)
        self.assertEqual(resolver.peek(), "203.0.113.42")
        self.assertEqual(calls, 1)

        # Next send includes cached client_ip synchronously.
        client.send_message("sid", "world")
        second_payload = json.loads(client.request.call_args.args[2])
        self.assertEqual(second_payload["client_ip"], "203.0.113.42")
        self.assertEqual(calls, 1)

    def test_token_http_calls_route_through_worker_and_never_retry_direct(self):
        route = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True)
        manager = MuseTokenManager(mint_file="", route=route)
        seen = []

        def handler(request):
            seen.append((str(request.url), request.headers.get("origin")))
            if request.url.path.endswith("/https/muse.ai/"):
                return httpx.Response(200, text=f"https://{VM}.metaaivm.com/")
            return httpx.Response(200, json={"token": "tok", "notary_token": "ntok"})

        with patch("muse_bridge.muse.token_manager.muse_http_client",
                   side_effect=lambda r, **kw: muse_http_client(r, transport=httpx.MockTransport(handler), **kw)):
            vm_id = manager.vm_id_from_cookies("hatch_sess=1")
            data = manager._post_json(f"{manager.base_url}/api/hatch/token", {"vmAddress": f"wss://{VM}.metaaivm.com/"}, "hatch_sess=1")
        self.assertEqual(vm_id, VM)
        self.assertEqual(data["token"], "tok")
        self.assertEqual(seen[0], (MUSE_PREFIX + "/", "https://muse.ai"))
        self.assertEqual(seen[1], (MUSE_PREFIX + "/api/hatch/token", "https://muse.ai"))

        def failing(request):
            return httpx.Response(502, text="bad gateway")

        with patch("muse_bridge.muse.token_manager.muse_http_client",
                   side_effect=lambda r, **kw: muse_http_client(r, transport=httpx.MockTransport(failing), **kw)):
            with self.assertRaises(CredentialRefreshError):
                manager.vm_id_from_cookies("hatch_sess=1")


class AdapterNetworkRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_routes_via_worker_http_and_none(self):
        # 1. Worker mode rewrites both Bot API and file download prefixes
        net = NetworkConfig(
            proxy_url="http://127.0.0.1:7890",
            worker=WorkerConfig(base=BASE, ws=True, http=True),
        )
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "bot"}})
            if request.url.path.endswith("/getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg", "file_size": 3}})
            return httpx.Response(200, content=b"jpg")

        tg_worker = TelegramClient(
            TelegramConfig(enabled=True, bot_token="1:abc", proxy="worker"),
            network=net,
            transport=httpx.MockTransport(handler),
        )
        try:
            await tg_worker.call("getMe")
            data = await tg_worker.download("fid", 1024)
            self.assertEqual(data, b"jpg")
        finally:
            await tg_worker.close()
        self.assertEqual(seen, [
            TG_PREFIX + "/bot1:abc/getMe",
            TG_PREFIX + "/bot1:abc/getFile",
            TG_PREFIX + "/file/bot1:abc/photos/a.jpg",
        ])

        # 2. HTTP proxy mode keeps official host and sets explicit proxy with trust_env=False
        with patch("muse_bridge.network.httpx.AsyncClient") as async_cls:
            TelegramClient(
                TelegramConfig(enabled=True, bot_token="1:abc", proxy="http"),
                network=net,
            )
        self.assertEqual(async_cls.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(async_cls.call_args.kwargs["trust_env"])

        # 3. Direct mode keeps official host, no proxy, trust_env=False
        with patch("muse_bridge.network.httpx.AsyncClient") as async_cls:
            TelegramClient(
                TelegramConfig(enabled=True, bot_token="1:abc", proxy="none"),
                network=net,
            )
        self.assertNotIn("proxy", async_cls.call_args.kwargs)
        self.assertFalse(async_cls.call_args.kwargs["trust_env"])

        # 4. Missing worker.base raises clearly
        with self.assertRaisesRegex(ValueError, "network.worker.base"):
            TelegramClient(
                TelegramConfig(enabled=True, bot_token="1:abc", proxy="worker"),
                network=NetworkConfig(),
            )

    async def test_wechat_uses_unified_network_route_without_host_pinning(self):
        route = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True)
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"ret": 0})

        client = WeChatClient(token="tok", route=route, transport=httpx.MockTransport(handler))
        try:
            await client.get_config("u1")
        finally:
            await client.aclose()
        self.assertEqual(seen, [f"{BASE}/https/ilinkai.weixin.qq.com/ilink/bot/getconfig"])

        http_route = NetworkRoute(mode="http", proxy_url="http://127.0.0.1:7890")
        with patch("muse_bridge.network.httpx.AsyncHTTPTransport") as transport_cls:
            WeChatClient(token="tok", route=http_route)
        self.assertEqual(transport_cls.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(transport_cls.call_args.kwargs["trust_env"])

    def test_qq_and_onebot_route_rewriting_and_qq_download_proxy_isolation(self):
        route = NetworkRoute(mode="worker", worker_base=BASE, worker_http=True)
        qq = QQClient("app", "sec", download_proxy="https://dl.example.test", route=route)
        self.assertEqual(
            qq.proxied_url("https://multimedia.nt.qq.com.cn/path?a=1"),
            "https://dl.example.test/multimedia.nt.qq.com.cn/path?a=1",
        )
        # Verify urllib_opener never inherits env proxies in "none" mode
        import urllib.request
        with patch.dict("os.environ", {"HTTP_PROXY": "http://env-proxy:9999", "HTTPS_PROXY": "http://env-proxy:9999"}):
            opener_none = DIRECT_ROUTE.urllib_opener()
        proxy_handlers_none = [h for h in opener_none.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxy_handlers_none, [])
        opener_http = NetworkRoute(mode="http", proxy_url="http://127.0.0.1:7890").urllib_opener()
        proxy_handlers_http = [h for h in opener_http.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers_http), 1)
        self.assertEqual(proxy_handlers_http[0].proxies, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})

        ob = OneBotClient("https://onebot.test", "tok", route=route)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"retcode":0,"status":"ok","data":{}}'
        with patch.object(NetworkRoute, "open_url", return_value=resp) as open_:
            ob.get_login_info()
        self.assertEqual(open_.call_args.args[0].full_url, f"{BASE}/https/onebot.test/get_login_info")


if __name__ == "__main__":
    unittest.main()
