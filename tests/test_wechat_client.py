"""Offline HTTP and URL-policy contracts for iLink."""
import base64
import json
import time
import unittest
from unittest import mock

import httpx

from muse_bridge.adapters.wechat.client import (
    DEFAULT_API, DeliveryUnknownError, SessionCoolingError,
    WeChatAPIError, WeChatClient, WeChatError,
)
from muse_bridge.adapters.wechat.security import (
    DEFAULT_CDN, PinnedTransport, WeChatSecurityError, redirect_base,
    validate_url,
)


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123"
        yield b"456"


class TestWeChatClient(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, **kwargs):
        client = WeChatClient(transport=httpx.MockTransport(handler), **kwargs)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_headers_dynamic_uin_and_base_info(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b'{"ret":0}', headers={"content-type": "application/octet-stream"})
        client = self.client(handler, token="secret-token", channel_version="3.5.7", route_tag="region")
        with mock.patch("muse_bridge.adapters.wechat.client.secrets.randbits", side_effect=[0, 4294967295]):
            await client.get_config("user", "context-secret")
            await client.notify(True)
        for request in requests:
            self.assertEqual(request.headers["authorization"], "Bearer secret-token")
            self.assertEqual(request.headers["content-type"], "application/json")
            self.assertEqual(request.headers["authorizationtype"], "ilink_bot_token")
            self.assertEqual(request.headers["ilink-app-clientversion"], str((3 << 16) | (5 << 8) | 7))
            self.assertEqual(request.headers["skrouteTag"], "region")
            self.assertEqual(json.loads(request.content)["base_info"]["channel_version"], "3.5.7")
        self.assertEqual([base64.b64decode(r.headers["x-wechat-uin"]) for r in requests], [b"0", b"4294967295"])
        self.assertEqual(json.loads(requests[0].content)["context_token"], "context-secret")
        self.assertTrue(requests[1].url.path.endswith("msg/notifystart"))

    async def test_all_business_bodies_and_no_mutation(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ret": 0})
        client = self.client(handler)
        body = {"filekey": "key", "base_info": {"channel_version": "wrong"}}
        msg = {"to_user_id": "user", "context_token": "ctx", "item_list": []}
        await client.get_upload_url(body)
        await client.send_message(msg)
        await client.send_typing("user", "ticket", 2)
        await client.get_updates("cursor", timeout_ms=47000)
        await client.get_config("user")
        await client.notify(False)
        self.assertEqual(body["base_info"]["channel_version"], "wrong")
        self.assertNotIn("base_info", msg)
        self.assertEqual(json.loads(requests[1].content)["msg"], msg)
        self.assertEqual(json.loads(requests[2].content)["typing_ticket"], "ticket")
        self.assertEqual(requests[3].extensions["timeout"]["read"], 47)
        self.assertNotIn("context_token", json.loads(requests[4].content))
        for request in requests:
            self.assertEqual(json.loads(request.content)["base_info"]["channel_version"], "2.4.6")
            self.assertNotIn("SKRouteTag", request.headers)

    async def test_login_is_fixed_entry_and_unauthenticated(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={})
        client = self.client(handler, token="secret")
        await client.get_qrcode([str(i) for i in range(15)])
        await client.get_qrcode_status("qr&secret", "12?34")
        self.assertEqual(requests[0].url.host, "ilinkai.weixin.qq.com")
        self.assertEqual(requests[0].url.params["bot_type"], "3")
        self.assertEqual(len(json.loads(requests[0].content)["local_token_list"]), 10)
        self.assertEqual(requests[1].url.params["qrcode"], "qr&secret")
        self.assertEqual(requests[1].url.params["verify_code"], "12?34")
        for request in requests:
            self.assertNotIn("authorization", request.headers)
        self.assertNotIn("x-wechat-uin", requests[1].headers)

    async def test_error_codes_and_secret_free_errors(self):
        debug_lines = []
        for body, expected in [({"ret": 8}, 8), ({"errcode": 9}, 9),
                               ({"ret": 0, "errcode": 3}, 3),
                               ({"ret": "remote-secret"}, None), ({"ret": False}, None)]:
            with self.subTest(body=body):
                client = self.client(lambda r: httpx.Response(200, json={**body, "errmsg": "remote-secret"}))
                with self.assertRaises(WeChatAPIError) as caught:
                    await client.get_config("user")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("remote-secret", repr(caught.exception))
        debug_client = self.client(lambda r: httpx.Response(503, json={"ret": 8, "errmsg": "gateway busy"}),
                                   on_debug=debug_lines.append)
        with self.assertRaises(WeChatAPIError):
            await debug_client.get_config("user")
        self.assertTrue(any("errmsg='gateway busy'" in line for line in debug_lines))

    async def test_invalid_session_priority_callback_and_cooling(self):
        for body in ({"ret": -14}, {"ret": 7, "errcode": -14}):
            callback = mock.Mock()
            client = self.client(lambda r: httpx.Response(200, json=body), on_session_invalid=callback)
            with self.assertRaises(SessionCoolingError) as caught:
                await client.send_message({})
            self.assertEqual(caught.exception.code, -14)
            callback.assert_called_once_with()
        handler = mock.Mock()
        client = self.client(handler, cooling_until=lambda: time.time() + 3600)
        with self.assertRaises(SessionCoolingError):
            await client.get_updates("cursor")
        handler.assert_not_called()

    async def test_callback_failure_does_not_mask_session_invalidity(self):
        client = self.client(lambda r: httpx.Response(200, json={"ret": -14}),
                             on_session_invalid=mock.Mock(side_effect=ValueError("callback-secret")))
        with self.assertRaises(SessionCoolingError) as caught:
            await client.get_config("user")
        self.assertNotIn("callback-secret", str(caught.exception))

    async def test_persistence_failure_still_cools_every_network_path_in_memory(self):
        handler = mock.Mock(side_effect=[httpx.Response(200, json={"ret": -14}),
                                         httpx.Response(200, json={"ret": 0})])
        callback = mock.Mock(side_effect=OSError("persistence-secret"))
        client = self.client(handler, cooling_until=lambda: 0, on_session_invalid=callback)
        with mock.patch("muse_bridge.adapters.wechat.client.time.time", return_value=1000):
            with self.assertRaises(SessionCoolingError):
                await client.get_updates("cursor")
            for operation in (
                client.get_updates("cursor"), client.send_message({}), client.get_upload_url({}),
                client.get_config("user"), client.send_typing("user", "ticket", 1), client.notify(True),
                client.get_qrcode([]), client.get_qrcode_status("secret"),
                client.cdn_download(DEFAULT_CDN + "/download", 1),
                client.download_public("https://example.com/media", 1),
                client.cdn_upload(DEFAULT_CDN + "/upload", b"x"),
            ):
                with self.assertRaises(SessionCoolingError):
                    await operation
            self.assertEqual(handler.call_count, 1)
            callback.assert_called_once_with()
        with mock.patch("muse_bridge.adapters.wechat.client.time.time", return_value=4599):
            with self.assertRaises(SessionCoolingError):
                await client.get_updates("cursor")
            self.assertEqual(handler.call_count, 1)
        with mock.patch("muse_bridge.adapters.wechat.client.time.time", return_value=4601):
            self.assertEqual(await client.get_config("user"), {"ret": 0})
            self.assertEqual(handler.call_count, 2)

    async def test_longpoll_timeout_is_empty_success(self):
        def handler(request):
            raise httpx.ReadTimeout("secret-url-token", request=request)
        client = self.client(handler)
        self.assertEqual(await client.get_updates("old"), {"ret": 0, "msgs": [], "get_updates_buf": "old"})
        self.assertEqual(await client.get_qrcode_status("secret"), {"status": "wait"})
        with self.assertRaises(WeChatError) as caught:
            await client.get_config("user")
        self.assertNotIn("secret", repr(caught.exception))

    async def test_send_accepts_empty_ok_and_unknown_never_retries(self):
        for body in ({}, {"ret": 0}, {"errmsg": ""}):
            client = self.client(lambda r, b=body: httpx.Response(200, json=b))
            self.assertEqual(await client.send_message({}), body)
        for exception in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout, httpx.ReadError):
            calls = []
            def handler(request):
                calls.append(request)
                raise exception("secret", request=request)
            client = self.client(handler)
            with self.assertRaises(DeliveryUnknownError):
                await client.send_message({})
            self.assertEqual(len(calls), 1)
        for response in (httpx.Response(502, json={}), httpx.Response(200, content=b"secret"),
                         httpx.Response(502, content=b"secret"), httpx.Response(200, json=[])):
            client = self.client(lambda r, resp=response: resp)
            with self.assertRaises(DeliveryUnknownError):
                await client.send_message({})

    async def test_download_public_and_cdn_have_no_auth_or_cookies(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b"ok", headers={"set-cookie": "secret=secret"})
        client = self.client(handler, token="secret", route_tag="private-route")
        self.assertEqual(await client.download_public("https://example.com/media", 2), b"ok")
        self.assertEqual(await client.cdn_download(DEFAULT_CDN + "/download?q=secret", 2), b"ok")
        for request in requests:
            for name in ("authorization", "authorizationtype", "x-wechat-uin", "ilink-app-id", "skrouteTag", "cookie"):
                self.assertNotIn(name, request.headers)
        with self.assertRaises(WeChatSecurityError):
            await client.cdn_download("https://example.com/media", 2)

    async def test_size_limits_streamed_and_declared(self):
        for response in (httpx.Response(200, stream=Chunks()),
                         httpx.Response(200, content=b"123456"),
                         httpx.Response(200, content=b"", headers={"content-length": "-1"}),
                         httpx.Response(200, content=b"", headers={"content-length": "bad"}),
                         httpx.Response(200, content=b"", headers={"content-encoding": "gzip"})):
            client = self.client(lambda r: response)
            with self.assertRaises(WeChatSecurityError):
                await client.cdn_download(DEFAULT_CDN + "/download", 5)

    async def test_redirects_are_never_followed(self):
        handler = mock.Mock(return_value=httpx.Response(302, headers={"location": "https://evil.test/secret"}))
        client = self.client(handler)
        for operation in (client.get_config("u"), client.cdn_download(DEFAULT_CDN + "/download", 20),
                          client.download_public("https://example.com/media", 20),
                          client.cdn_upload(DEFAULT_CDN + "/upload", b"x")):
            with self.assertRaises(WeChatError):
                await operation
        self.assertEqual(handler.call_count, 4)

    async def test_no_httpx_url_logs(self):
        client = self.client(lambda r: httpx.Response(200, json={}))
        with self.assertNoLogs("httpx", level="DEBUG"):
            await client.get_qrcode_status("qr-secret", "code-secret")

    async def test_non2xx_json_still_invalidates_session(self):
        callback = mock.Mock()
        client = self.client(lambda r: httpx.Response(401, json={"ret": -14, "errmsg": "secret"}),
                             on_session_invalid=callback)
        with self.assertRaises(SessionCoolingError):
            await client.get_config("user")
        callback.assert_called_once_with()

    async def test_response_close_failure_cannot_mask_confirmed_delivery(self):
        response = httpx.Response(200, json={"ret": 0})
        response.aclose = mock.AsyncMock(side_effect=httpx.ReadError("secret"))
        client = self.client(lambda r: response)
        self.assertEqual(await client.send_message({}), {"ret": 0})

    async def test_production_transport_cannot_be_replaced(self):
        with self.assertRaises(WeChatSecurityError):
            WeChatClient(transport=object())


class TestWeChatURLPolicy(unittest.IsolatedAsyncioTestCase):
    def test_api_url_negative_cases(self):
        for url in (
            "http://ilinkai.weixin.qq.com", "https://evil.qq.com", "https://ilinkai.weixin.qq.com.evil.test",
            "https://u:secret@ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com:444",
            "https://127.0.0.1", "https://[::1]", "https://ilinkai.weixin.qq.com/#secret",
            "https://ilinkai.weixin.qq.com\\@evil.test", "https://ilinkai.weixin.qq.com\n",
            "https://ilinkai.weixin.qq.com/%0a", "https://ilinkai.weixin.qq.com/?secret=1",
        ):
            with self.subTest(url=url), self.assertRaises(WeChatSecurityError) as caught:
                validate_url(url, base=True)
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(validate_url(DEFAULT_API + ":443/", base=True), DEFAULT_API)
        self.assertEqual(redirect_base("ilinkai.weixin.qq.com"), DEFAULT_API)
        for host in ("unknown.weixin.qq.com", "evil.qq.com", "user@ilinkai.weixin.qq.com", "ilinkai.weixin.qq.com/path", None):
            with self.assertRaises(WeChatSecurityError):
                redirect_base(host)

    def test_public_url_negative_cases(self):
        for url in ("http://example.com", "file:///tmp/x",
                    "https://user:secret@example.com", "https://example.com:8443/x"):
            with self.subTest(url=url), self.assertRaises(WeChatSecurityError):
                validate_url(url, public=True)

    async def test_transport_validates_url_and_uses_standard_https(self):
        transport = PinnedTransport()
        pool = mock.Mock()
        pool.handle_async_request = mock.AsyncMock(return_value=httpx.Response(200))
        pool.aclose = mock.AsyncMock()
        with mock.patch("muse_bridge.adapters.wechat.security.httpx.AsyncHTTPTransport", return_value=pool) as factory:
            req = httpx.Request("GET", DEFAULT_API + "/path?q=secret")
            await transport.handle_async_request(req)
            sent = pool.handle_async_request.call_args.args[0]
            self.assertEqual(sent.url.host, "ilinkai.weixin.qq.com")
            self.assertEqual(sent.url.params["q"], "secret")
            self.assertTrue(factory.call_args.kwargs["verify"])
            self.assertTrue(factory.call_args.kwargs["trust_env"])
            await transport.aclose()
        pool.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
