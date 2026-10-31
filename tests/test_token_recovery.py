"""Mint rotation must never turn a temporary gateway failure into a cookie verdict."""

import asyncio
import base64
import concurrent.futures
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from muse_bridge.config.models import MuseConfig
from muse_bridge.muse.client.errors import CredentialRefreshError, HandshakeError, LoginRequired, RPCError
from muse_bridge.muse.service import MuseService
from muse_bridge.muse.token_manager import MuseTokenManager


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def token(exp=None, vm="vm", iat=1000):
    return "header." + encoded({"env_id": vm, "iat": iat, **({"exp": exp} if exp else {})}) + ".signature"


def notary(exp):
    return "prefix.header." + encoded({"restrictions": [f"timeout:{exp}"]}) + ".signature"


class TestTokenRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cookies = self.root / "cookies.json"
        self.mint = self.root / "mint.json"
        self.cookies.write_text(json.dumps({"cookie": "hatch_sess=synthetic"}), encoding="utf-8")
        self.old_t, self.old_n = token(), notary(time.time() + 72000)
        self.mint.write_text(json.dumps({"token": self.old_t, "notary_token": self.old_n}), encoding="utf-8")
        self.manager = MuseTokenManager(str(self.mint), str(self.cookies))
        self.service = MuseService(MuseConfig(cookies_file=str(self.cookies)), self.manager)
        self.addCleanup(self.service.interactions.close)
        self.new = {"token": token(vm="vm2"), "notary_token": notary(time.time() + 72001)}

    def test_no_exp_jwt_uses_notary_and_expires_with_margin(self):
        with patch("muse_bridge.muse.token_manager.time.time", return_value=2000):
            self.manager._notary_token = notary(2061)
            self.assertTrue(self.manager.is_fresh())
            self.manager._notary_token = notary(2060)
            self.assertFalse(self.manager.is_fresh())

    def test_jwt_exp_is_used_when_earlier_than_notary(self):
        self.assertEqual(self.manager.credentials_expiry(token(2000), notary(5000)), 2000)
        self.assertEqual(self.manager.credentials_expiry(token(9000), notary(5000)), 5000)
        self.assertEqual(self.manager.credentials_expiry(token(), notary(5000)), 5000)

    def test_expired_mint_refreshes_without_cookie_change(self):
        before = self.cookies.read_bytes()
        self.manager._notary_token = notary(1000)
        with patch.object(self.manager, "_post_json", return_value=self.new) as post:
            result = self.manager.get_credentials()
        self.assertEqual(result[:2], (self.new["token"], self.new["notary_token"]))
        post.assert_called_once()
        self.assertEqual(self.cookies.read_bytes(), before)
        self.assertFalse(self.manager._invalid)

    def test_transient_failure_preserves_cookie_cache_and_recovers_after_backoff(self):
        before = self.mint.read_bytes()
        self.manager.invalidate_mint(self.old_t)
        with patch.object(self.manager, "_post_json", side_effect=TimeoutError("synthetic")) as post:
            with self.assertRaises(CredentialRefreshError):
                self.manager.get_credentials()
            with self.assertRaises(CredentialRefreshError):
                self.manager.get_credentials()
            post.assert_called_once()
        self.assertTrue(self.manager.has_credentials())
        self.assertFalse(self.manager._invalid)
        self.assertEqual(self.mint.read_bytes(), before)
        with patch("muse_bridge.muse.token_manager.time.monotonic", return_value=self.manager._retry_after + 1):
            with patch.object(self.manager, "_post_json", return_value=self.new):
                self.manager.get_credentials()
        self.assertTrue(self.manager.is_fresh())

    def test_temporary_http_errors_do_not_require_login(self):
        for status in (403, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.manager._retry_after = 0
                with patch.object(self.manager, "_post_json", side_effect=CredentialRefreshError("token", status)):
                    with self.assertRaises(CredentialRefreshError):
                        self.manager.refresh_from_cookies()
                self.assertTrue(self.manager.has_credentials())
                self.assertTrue(self.mint.exists())
                self.assertFalse(self.manager._invalid)

    def test_cookie_api_401_is_confirmed_login_required(self):
        with patch.object(self.manager, "_post_json", side_effect=CredentialRefreshError("token", 401, "unauthorized")):
            with self.assertRaises(LoginRequired):
                self.manager.refresh_from_cookies()
        self.assertFalse(self.manager.has_credentials())
        self.assertFalse(self.mint.exists())
        self.assertTrue(self.cookies.exists())

    def test_notary_cookie_endpoint_401_also_requires_login(self):
        with patch.object(self.manager, "_post_json", side_effect=[{"token": token()}, CredentialRefreshError("noise-notary-token", 401)]):
            with self.assertRaises(LoginRequired):
                self.manager.refresh_from_cookies()
        self.assertFalse(self.manager.has_credentials())

    def test_cookie_content_change_recovers_even_with_same_mtime(self):
        self.manager.invalidate()
        stamp = self.cookies.stat().st_mtime_ns
        self.cookies.write_text(json.dumps({"cookie": "hatch_sess=changed"}), encoding="utf-8")
        os.utime(self.cookies, ns=(stamp, stamp))
        self.assertTrue(self.manager.has_credentials())
        with patch.object(self.manager, "_post_json", return_value=self.new):
            with patch.object(self.manager, "vm_id_from_cookies", return_value="vm"):
                self.manager.get_credentials()
        self.assertTrue(self.manager.is_fresh())

    def test_environment_cookie_change_recovers(self):
        with patch.dict(os.environ, {"MUSE_COOKIE": "session=first"}):
            self.manager.invalidate()
            self.assertFalse(self.manager.has_credentials())
            with patch.dict(os.environ, {"MUSE_COOKIE": "session=second"}):
                self.assertTrue(self.manager.has_credentials())

    def test_vm_mismatch_rediscovers_once_and_never_requires_login(self):
        mismatch = CredentialRefreshError("token", 401, "hatch_vm_auth_no_match")
        with patch.object(self.manager, "_post_json", side_effect=[mismatch, self.new]) as post:
            with patch.object(self.manager, "vm_id_from_cookies", return_value="vm2") as resolve:
                self.manager.refresh_from_cookies()
        resolve.assert_called_once()
        self.assertEqual(post.call_args.args[1]["vmName"], "vm2")
        self.assertEqual(self.manager.vm_id, "vm2")
        self.assertFalse(self.manager._invalid)

    def test_repeated_vm_mismatch_is_recoverable_not_login(self):
        mismatch = CredentialRefreshError("token", 401, "hatch_vm_auth_no_match")
        with patch.object(self.manager, "_post_json", side_effect=mismatch) as post:
            with patch.object(self.manager, "vm_id_from_cookies", return_value="vm2"):
                with self.assertRaises(CredentialRefreshError):
                    self.manager.refresh_from_cookies()
        self.assertEqual(post.call_count, 2)
        self.assertTrue(self.manager.has_credentials())

    def test_explicit_vm_is_not_overridden(self):
        self.manager._explicit_vm = True
        self.manager.vm_id = "fixed"
        with patch.object(self.manager, "_post_json", side_effect=CredentialRefreshError("token", 401, "hatch_vm_auth_no_match")):
            with patch.object(self.manager, "vm_id_from_cookies") as resolve:
                with self.assertRaises(CredentialRefreshError):
                    self.manager.refresh_from_cookies()
        resolve.assert_not_called()
        self.assertEqual(self.manager.vm_id, "fixed")

    def test_incomplete_response_and_disk_failure_do_not_publish_or_lock(self):
        for response in ({}, {"token": token()}, self.new):
            self.manager._retry_after = 0
            with self.subTest(response=list(response)):
                with patch.object(self.manager, "_post_json", return_value=response):
                    with patch.object(self.manager, "_write_mint_atomic", side_effect=OSError("disk")):
                        with self.assertRaises(CredentialRefreshError):
                            self.manager.refresh_from_cookies()
                self.assertEqual(self.manager._token, self.old_t)
                self.assertEqual(self.manager._notary_token, self.old_n)
                self.assertFalse(self.manager._invalid)

    def test_missing_cookie_does_not_prompt_or_start_otp(self):
        self.cookies.unlink()
        self.manager.invalidate_mint()
        with patch("builtins.input") as prompt:
            with self.assertRaises(LoginRequired):
                self.manager.refresh_from_cookies(True)
        prompt.assert_not_called()

    def test_concurrent_rejected_snapshot_only_mints_once(self):
        self.manager.invalidate_mint(self.old_t)
        with patch.object(self.manager, "_post_json", return_value=self.new) as post:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.manager.refresh_from_cookies(rejected_token=self.old_t), range(6)))
        post.assert_called_once()
        self.assertEqual(len(set(results)), 1)
        self.manager.invalidate_mint(self.old_t)
        self.assertTrue(self.manager.is_fresh())

    def test_notary_only_refresh_deduplicates_same_token_and_ignores_late_failure(self):
        self.new["token"] = self.old_t
        with patch.object(self.manager, "_post_json", return_value=self.new) as post:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _: self.manager.refresh_from_cookies(
                    rejected_token=self.old_t, rejected_notary=self.old_n), range(6)))
        post.assert_called_once()
        self.manager.invalidate_mint(self.old_t, self.old_n)
        self.assertTrue(self.manager.is_fresh())

    def test_connection_snapshot_expiry_rotates_even_when_cookie_is_available(self):
        self.manager.get_credentials()
        self.manager._notary_token = notary(2000)
        with patch("muse_bridge.muse.token_manager.time.time", return_value=1940):
            self.assertTrue(self.manager.has_credentials())
            self.assertFalse(self.manager.credentials_current(self.old_t, self.manager._notary_token, "vm"))

    def test_client_snapshot_rotation_uses_own_credentials(self):
        self.manager.get_credentials()
        self.assertTrue(self.manager.credentials_current(self.old_t, self.old_n, "vm"))
        with patch.object(self.manager, "_post_json", return_value=self.new):
            self.manager.refresh_from_cookies()
        self.assertFalse(self.manager.credentials_current(self.old_t, self.old_n, "vm"))
        self.assertTrue(self.manager.credentials_current(self.new["token"], self.new["notary_token"], "vm"))

    def test_post_message_rejection_is_not_replayed(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            client = factory.return_value
            client.__enter__.return_value = client
            client.token, client.notary_token, client.vm_id = self.old_t, self.old_n, "vm"
            client.send_message.side_effect = RPCError("POST", "/chat", 401)
            with patch.object(self.service, "check_approval_gate"):
                with self.assertRaises(RPCError):
                    self.service.post_message("session", "hello")
            client.send_message.assert_called_once()
        self.assertTrue(self.manager.has_credentials())
        self.assertFalse(self.manager.is_fresh())

    def test_network_post_failure_does_not_reject_mint(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            client = factory.return_value
            client.__enter__.return_value = client
            client.connect.side_effect = HandshakeError("temporary")
            with self.assertRaises(HandshakeError):
                self.service.post_message("session", "hello")
            client.send_message.assert_not_called()
        self.assertTrue(self.manager.is_fresh())

    def test_cookie_http_error_is_safe_and_structured(self):
        response = MagicMock(status_code=401)
        response.json.return_value = {"code": "hatch_vm_auth_no_match", "secret": "DO_NOT_LOG"}
        with patch("muse_bridge.muse.token_manager.httpx.Client") as http:
            http.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaises(CredentialRefreshError) as caught:
                self.manager._post_json("https://muse.ai/api/hatch/token", {}, "COOKIE_SECRET")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, "hatch_vm_auth_no_match")
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        self.assertNotIn("COOKIE_SECRET", str(caught.exception))

    def test_connect_rejection_refreshes_once_then_succeeds(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            client = factory.return_value
            connect = client.connect
            connect.side_effect = [HandshakeError("rejected", status=401), None]
            client.token, client.notary_token, client.vm_id = self.old_t, self.old_n, "vm"
            with patch.object(self.manager, "_post_json", return_value=self.new) as post:
                actual = self.service.create_client()
                actual.connect()
            post.assert_called_once()
            self.assertEqual(client.token, self.new["token"])
            self.assertFalse(self.manager._invalid)
            self.assertEqual(connect.call_count, 2)

    def test_second_connection_failure_does_not_invalidate_cookie(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            client = factory.return_value
            client.connect.side_effect = [HandshakeError("rejected", status=401), HandshakeError("network")]
            client.token, client.notary_token, client.vm_id = self.old_t, self.old_n, "vm"
            with patch.object(self.manager, "_post_json", return_value=self.new):
                actual = self.service.create_client()
                with self.assertRaises(HandshakeError):
                    actual.connect()
        self.assertTrue(self.manager.has_credentials())
        self.assertFalse(self.manager._invalid)

    def test_network_handshake_failure_does_not_mint(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            factory.return_value.connect.side_effect = HandshakeError("network")
            with patch.object(self.manager, "_post_json") as post:
                actual = self.service.create_client()
                with self.assertRaises(HandshakeError):
                    actual.connect()
            post.assert_not_called()
        self.assertTrue(self.manager.has_credentials())

    def test_request_rejection_marks_snapshot_without_replaying_post(self):
        with patch("muse_bridge.muse.service.MuseClient") as factory:
            client = factory.return_value
            client.token, client.notary_token, client.vm_id = self.old_t, self.old_n, "vm"
            request = client.request
            request.side_effect = RPCError("POST", "/chat", 401)
            actual = self.service.create_client()
            with self.assertRaises(RPCError):
                actual.request("POST", "/chat", b"payload")
            request.assert_called_once()
        self.assertTrue(self.manager.has_credentials())
        self.assertFalse(self.manager.is_fresh())


class TestStartupRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_startup_failure_starts_adapter_without_login(self):
        from muse_bridge.engine import BridgeEngine
        from muse_bridge.adapters.qq.adapter import QQAdapter
        engine = BridgeEngine.__new__(BridgeEngine)
        engine._running = False
        engine.config = MagicMock()
        engine.muse_service = MagicMock()
        engine.muse_service.verify_connection.side_effect = HandshakeError("temporary")
        engine.muse_service.interaction_protocol.run = AsyncMock()
        adapter = MagicMock(spec=QQAdapter)
        adapter.is_enabled = True
        adapter.name = "qq"
        adapter.start = AsyncMock()
        engine.adapters = [adapter]
        with contextlib.redirect_stdout(io.StringIO()):
            await engine.start()
        adapter.start.assert_awaited_once()
        adapter.enter_login_mode.assert_not_called()
