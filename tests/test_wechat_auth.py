"""Offline QR state machine tests, including safe existing-account matching."""
import asyncio
import copy
import json
import unittest
from unittest import mock

import httpx

from muse_bridge.adapters.wechat.auth import WeChatAuth, login
from muse_bridge.adapters.wechat.client import DEFAULT_API, WeChatClient, WeChatError
from muse_bridge.adapters.wechat.security import WeChatSecurityError

CONFIRMED = {
    "status": "confirmed", "bot_token": "new-token", "baseurl": DEFAULT_API,
    "ilink_bot_id": "bot@im.bot", "ilink_user_id": "user@im.wechat",
}
LOCAL = [
    {"bot_token": "old-one", "bot_id": "first", "user_id": "user-one", "baseurl": DEFAULT_API},
    {"bot_token": "old-two", "bot_id": "second", "user_id": "user-two", "baseurl": DEFAULT_API},
]


class TestWeChatAuth(unittest.IsolatedAsyncioTestCase):
    def auth(self, states):
        self.requests, self.qr_count = [], 0
        states = iter(states)
        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith("get_bot_qrcode"):
                self.qr_count += 1
                return httpx.Response(200, json={
                    "qrcode": f"qr-secret-{self.qr_count}",
                    "qrcode_img_content": f"image-content-{self.qr_count}",
                })
            return httpx.Response(200, json=next(states))
        client = WeChatClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return WeChatAuth(client)

    async def test_all_success_states_and_mixed_callbacks(self):
        statuses = ["wait", "scaned", "need_verifycode", "need_verifycode", "scaned", "scaned_but_redirect", "confirmed"]
        states = [{"status": status} for status in statuses[:-1]] + [CONFIRMED]
        states[-2]["redirect_host"] = "ilinkai.weixin.qq.com"
        auth = self.auth(states)
        display, on_status = mock.AsyncMock(), mock.Mock()
        verify = mock.Mock(side_effect=[" 1234 ", "5678"])
        with mock.patch("muse_bridge.adapters.wechat.auth.asyncio.sleep", new=mock.AsyncMock()):
            result = await auth.login(LOCAL, display=display, verify=verify, on_status=on_status)
        self.assertEqual(result, {"bot_token": "new-token", "bot_id": "bot@im.bot", "user_id": "user@im.wechat", "baseurl": DEFAULT_API})
        display.assert_awaited_once_with("image-content-1")
        self.assertEqual([call.args[0] for call in on_status.call_args_list], statuses)
        self.assertEqual(verify.call_count, 2)
        polls = [r for r in self.requests if r.method == "GET"]
        self.assertEqual(polls[3].url.params["verify_code"], "1234")
        self.assertEqual(polls[4].url.params["verify_code"], "5678")
        self.assertNotIn("verify_code", polls[5].url.params)
        self.assertEqual(json.loads(self.requests[0].content)["local_token_list"], ["old-one", "old-two"])

    async def test_blocked_and_expired_refresh_share_three_total_limit(self):
        auth = self.auth([{"status": "need_verifycode"}, {"status": "verify_code_blocked"},
                          {"status": "expired"}, {"status": "expired"}])
        display, verify, on_status = mock.Mock(), mock.AsyncMock(return_value="123"), mock.AsyncMock()
        with self.assertRaises(WeChatError) as caught:
            await auth.login(LOCAL, display=display, verify=verify, on_status=on_status)
        self.assertIn("three", str(caught.exception))
        self.assertEqual(self.qr_count, 3)
        self.assertEqual(display.call_count, 3)
        self.assertEqual(on_status.await_count, 4)
        verify.assert_awaited_once_with()
        polls = [r for r in self.requests if r.method == "GET"]
        self.assertEqual(polls[1].url.params["verify_code"], "123")
        self.assertNotIn("verify_code", polls[2].url.params)
        self.assertEqual(polls[2].url.params["qrcode"], "qr-secret-2")

    async def test_binded_matches_only_correct_token(self):
        for identity in ({"ilink_bot_id": "second"}, {"ilink_user_id": "user-two"},
                         {"ilink_bot_id": "second", "ilink_user_id": "user-two"}):
            auth = self.auth([{"status": "binded_redirect", **identity}])
            result = await auth.login(LOCAL, display=lambda s: None, verify=lambda: "")
            self.assertEqual(result["bot_token"], "old-two")
            self.assertEqual(result["bot_id"], "second")

    async def test_binded_missing_conflicting_unknown_or_ambiguous_identity_fails(self):
        for state, accounts in (
            ({}, LOCAL), ({"ilink_bot_id": "absent"}, LOCAL),
            ({"ilink_bot_id": "second", "ilink_user_id": "user-one"}, LOCAL),
            ({"ilink_user_id": "same"}, [{**a, "user_id": "same"} for a in LOCAL]),
        ):
            auth = self.auth([{"status": "binded_redirect", **state}])
            with self.assertRaises(WeChatError):
                await auth.login(accounts, display=lambda s: None, verify=lambda: "")

    async def test_missing_confirmed_botid_or_token_fails(self):
        for field in ("ilink_bot_id", "bot_token"):
            state = {k: v for k, v in CONFIRMED.items() if k != field}
            auth = self.auth([state])
            with self.assertRaises(WeChatError):
                await auth.login([], display=lambda s: None, verify=lambda: "")

    async def test_untrusted_redirect_and_confirmed_baseurl_fail_safely(self):
        for state in (
            {"status": "scaned_but_redirect", "redirect_host": "unverified.weixin.qq.com"},
            {"status": "scaned_but_redirect", "redirect_host": "user:secret@ilinkai.weixin.qq.com"},
            {"status": "scaned_but_redirect"},
            {**CONFIRMED, "baseurl": "https://evil.qq.com/secret"},
        ):
            auth = self.auth([state])
            with self.assertRaises(WeChatSecurityError) as caught:
                await auth.login([], display=lambda s: None, verify=lambda: "")
            self.assertNotIn("secret", repr(caught.exception))
            self.assertNotIn("unverified", repr(caught.exception))

    async def test_cancellation_preserves_input_credentials(self):
        accounts = copy.deepcopy(LOCAL)
        for verify in (mock.AsyncMock(side_effect=asyncio.CancelledError()), lambda: None):
            auth = self.auth([{"status": "need_verifycode"}])
            with self.assertRaises(asyncio.CancelledError):
                await auth.login(accounts, display=lambda s: None, verify=verify)
            self.assertEqual(accounts, LOCAL)

    async def test_task_cancellation_during_longpoll(self):
        entered = asyncio.Event()
        async def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"qrcode": "secret", "qrcode_img_content": "content"})
            entered.set()
            await asyncio.Event().wait()
        client = WeChatClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        task = asyncio.create_task(WeChatAuth(client).login(LOCAL, display=lambda s: None, verify=lambda: ""))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(LOCAL[0]["bot_token"], "old-one")

    async def test_latest_ten_tokens_and_mapping_input(self):
        auth = self.auth([CONFIRMED])
        accounts = {str(i): {"bot_token": str(i), "bot_id": str(i)} for i in range(19, -1, -1)}
        await auth.login(accounts, display=lambda s: None, verify=lambda: "")
        self.assertEqual(json.loads(self.requests[0].content)["local_token_list"], [str(i) for i in range(19, 9, -1)])

    async def test_wait_and_scaned_have_minimum_interval_without_extra_longpoll_delay(self):
        auth = self.auth([{"status": "wait"}, {"status": "scaned"}, CONFIRMED])
        with mock.patch("muse_bridge.adapters.wechat.auth.monotonic", side_effect=[0, .2, 1, 2.5, 3]), \
             mock.patch("muse_bridge.adapters.wechat.auth.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            await auth.login([], display=lambda s: None, verify=lambda: "")
        self.assertEqual(sleep.await_args_list, [mock.call(.8), mock.call(0)])

    async def test_cancellation_during_wait_interval(self):
        auth = self.auth([{"status": "wait"}, CONFIRMED])
        waiting = asyncio.Event()
        task = asyncio.create_task(auth.login([], display=lambda s: None, verify=lambda: "",
                                               on_status=lambda status: waiting.set()))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len([r for r in self.requests if r.method == "GET"]), 1)

    async def test_standalone_login_owns_client_cleanup(self):
        auth = self.auth([CONFIRMED])
        with mock.patch("muse_bridge.adapters.wechat.auth.WeChatClient", return_value=auth.client), \
             mock.patch.object(auth.client, "aclose", new=mock.AsyncMock()) as close:
            result = await login([], display=lambda s: None, verify=lambda: "")
        self.assertEqual(result["bot_id"], "bot@im.bot")
        close.assert_awaited_once_with()

    async def test_unknown_status_is_safe_error(self):
        for status in ("secret-status", [], {}, None):
            auth = self.auth([{"status": status}])
            with self.assertRaises(WeChatError) as caught:
                await auth.login([], display=lambda s: None, verify=lambda: "")
            self.assertNotIn("secret-status", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
