"""Unit tests for credential validation and automatic login fallback across all stages."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from muse_bridge.config.models import AppConfig, GatewayConfig, MuseConfig, QQConfig, QQPushConfig
from muse_bridge.muse.token_manager import MuseTokenManager
from muse_bridge.muse.service import MuseService
from muse_bridge.adapters.qq.adapter import QQAdapter
from muse_bridge.adapters.qq.outbound import QQOutboundPusher
from muse_bridge.core.deduplicator import MessageDeduplicator
from muse_bridge.adapters.qq.route_manager import RouteManager


class TestLoginFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cookies_path = os.path.join(self.tmp_dir.name, "cookies.json")
        self.mint_path = os.path.join(self.tmp_dir.name, ".mint.json")
        self.session_state_path = os.path.join(self.tmp_dir.name, "qq_session.json")
        self.session_map_path = os.path.join(self.tmp_dir.name, "qq_session_map.json")
        self.delivered_path = os.path.join(self.tmp_dir.name, "qq_delivered.jsonl")
        self.push_state_path = os.path.join(self.tmp_dir.name, "qq_push_state.json")

        with open(self.cookies_path, "w", encoding="utf-8") as f:
            json.dump({"cookie": "hatch_sess=test_sess; muse_uid=123"}, f)

        with open(self.mint_path, "w", encoding="utf-8") as f:
            json.dump({"token": "old_token", "notary_token": "old_notary"}, f)

        self.token_manager = MuseTokenManager(
            mint_file=self.mint_path,
            cookies_file=self.cookies_path,
            vm_id="test-vm",
            base_url="https://muse.ai",
        )
        self.muse_config = MuseConfig(
            cookies_file=self.cookies_path,
            mint_file=self.mint_path,
            session_id="test-session",
        )
        self.muse_service = MuseService(config=self.muse_config, token_manager=self.token_manager)

    def _make_qq_config(self, **kwargs) -> QQConfig:
        defaults = dict(
            app_id="app_123",
            admin_id=["ADMIN_OPENID"],
            whitelist={"private": ["ADMIN_OPENID"]},
            session_state_file=self.session_state_path,
            session_map_file=self.session_map_path,
            delivered_file=self.delivered_path,
            push=QQPushConfig(
                enabled=True,
                target_type="private",
                target_id="ADMIN_OPENID",
                state_file=self.push_state_path,
            ),
        )
        defaults.update(kwargs)
        return QQConfig(**defaults)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_token_manager_invalidation(self):
        """Test invalidate() clears tokens, removes mint file, and resets has_credentials."""
        self.assertTrue(os.path.exists(self.mint_path))
        self.assertTrue(self.token_manager.has_credentials())

        self.token_manager.invalidate()
        self.assertFalse(self.token_manager.has_credentials())
        self.assertFalse(self.token_manager.is_fresh())
        self.assertFalse(os.path.exists(self.mint_path))

        # Modifying cookies.json allows has_credentials() to recover
        time.sleep(0.01)
        with open(self.cookies_path, "w", encoding="utf-8") as f:
            json.dump({"cookie": "hatch_sess=new_sess"}, f)

        self.assertTrue(self.token_manager.has_credentials())

    def test_muse_service_verify_connection_failure(self):
        """Test verify_connection invalidates credentials on 401 handshake error."""
        with patch.object(self.muse_service, "create_client") as mock_create:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.connect.side_effect = RuntimeError("handshake failed: WS upgrade failed: HTTP/1.1 401 Unauthorized")
            mock_create.return_value = mock_client

            ok = self.muse_service.verify_connection()
            self.assertFalse(ok)
            self.assertFalse(self.token_manager.has_credentials())

    def test_muse_service_verify_connection_success(self):
        """Test verify_connection returns True on successful handshake."""
        with patch.object(self.muse_service, "create_client") as mock_create:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.connect.return_value = None
            mock_create.return_value = mock_client

            ok = self.muse_service.verify_connection()
            self.assertTrue(ok)
            self.assertTrue(self.token_manager.has_credentials())

    async def test_outbound_pusher_triggers_login_on_auth_failure(self):
        """Test outbound pusher detects 401, triggers login callback and notifies admin."""
        qq_config = self._make_qq_config()
        mock_qq_client = MagicMock()
        mock_qq_client.send_message.return_value = (200, {})

        login_required_called = []
        login_required = asyncio.Event()
        def on_login_req(target):
            login_required_called.append(target)
            login_required.set()

        pusher = QQOutboundPusher(
            config=qq_config,
            client=mock_qq_client,
            muse_service=self.muse_service,
            deduplicator=MessageDeduplicator(),
            route_manager=RouteManager(),
            session_id="test-session",
            on_login_required=on_login_req,
        )

        with patch.object(pusher, "_fetch_session", side_effect=RuntimeError("WS upgrade failed: HTTP/1.1 401 Unauthorized")):
            with patch.object(self.muse_service, "create_client") as mock_create:
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_create.return_value = mock_client

                # Wait for the observed callback, not executor scheduling speed.
                pusher._running = True
                task = asyncio.create_task(pusher._run_loop())
                await asyncio.wait_for(login_required.wait(), timeout=3)
                pusher._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                self.assertTrue(len(login_required_called) > 0)
                self.assertEqual(login_required_called[0], "ADMIN_OPENID")
                self.assertFalse(self.token_manager.has_credentials())
                mock_qq_client.send_message.assert_called()

    async def test_inbound_post_message_auth_failure_triggers_login_fallback(self):
        """Test sending message encountering 401 invalidates credentials and guides admin to login."""
        qq_config = self._make_qq_config(
            whitelist={"private": ["ADMIN_OPENID"], "GRP_1": "*"},
        )
        adapter = QQAdapter(config=qq_config, muse_service=self.muse_service)
        mock_qq_client = MagicMock()
        mock_qq_client.send_message.return_value = (200, {})
        adapter.client = mock_qq_client

        # Mock post_message failure with 401
        with patch.object(self.muse_service, "post_message", side_effect=RuntimeError("HTTP/1.1 401 Unauthorized")):
            event = {
                "id": "msg_001",
                "content": "Hello Muse",
                "author": {"user_openid": "ADMIN_OPENID"},
            }
            await adapter.handle_event("C2C_MESSAGE_CREATE", event)

            self.assertTrue(adapter._in_login_fallback)
            self.assertFalse(self.token_manager.has_credentials())
            # Sent reply prompting admin to enter login email
            mock_qq_client.send_message.assert_called()
            sent_text = mock_qq_client.send_message.call_args_list[0][0][2]
            self.assertIn("登录回落流程", sent_text)

    async def test_explicit_login_initiates_otp_session(self):
        """Explicit /login starts an owned temporary OTP interaction."""
        qq_config = self._make_qq_config()
        adapter = QQAdapter(config=qq_config, muse_service=self.muse_service)
        adapter._in_login_fallback = True
        mock_qq_client = MagicMock()
        mock_qq_client.send_message.return_value = (200, {})
        adapter.client = mock_qq_client

        with patch("muse_bridge.muse.login.MuseProtocolLogin.send_otp") as mock_send_otp:
            event = {
                "id": "msg_002",
                "content": "/login user@example.com",
                "author": {"user_openid": "ADMIN_OPENID"},
            }
            await adapter.handle_event("C2C_MESSAGE_CREATE", event)

            mock_send_otp.assert_called_once_with("user@example.com")
            # Login session is active
            session_key = ("private", "ADMIN_OPENID", "ADMIN_OPENID")
            interaction = self.muse_service.interactions.current(session_key)
            self.assertIsNotNone(interaction)
            self.assertEqual(interaction.kind, "login")
            self.assertEqual(interaction.state, "wait_otp")
            self.muse_service.interactions.close()

    async def test_outbound_pusher_handles_404_session_not_found(self):
        """Test outbound pusher treats 404 session not found as empty and marks it gated."""
        qq_config = self._make_qq_config()
        mock_qq_client = MagicMock()
        pusher = QQOutboundPusher(
            config=qq_config,
            client=mock_qq_client,
            muse_service=self.muse_service,
            deduplicator=MessageDeduplicator(),
            route_manager=RouteManager(),
            session_id="new-empty-session",
        )

        with patch.object(self.muse_service, "create_client") as mock_create:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get_history.side_effect = RuntimeError(
                'GET /chat/history?session_id=new-empty-session -> status 404: b\'{"ok":false,"error":{"code":"not_found"}}\''
            )
            mock_create.return_value = mock_client

            # _fetch_session should gracefully return empty list
            res = pusher._fetch_session(mock_client, "new-empty-session")
            self.assertEqual(res, [])

            # Run one tick of run_loop
            pusher._running = True
            task = asyncio.create_task(pusher._run_loop())
            await asyncio.sleep(0.05)
            pusher._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # The new session should now be marked gated so it is not re-fetched constantly
            self.assertIn("new-empty-session", pusher._gated)

    async def test_inbound_post_message_404_auto_recreates_session(self):
        """Test sending message encountering 404 automatically creates new session and posts."""
        qq_config = self._make_qq_config(session_id="initial-session-id")
        adapter = QQAdapter(config=qq_config, muse_service=self.muse_service)
        mock_qq_client = MagicMock()
        mock_qq_client.send_message.return_value = (200, {})
        adapter.client = mock_qq_client

        old_sid = adapter.active_session_id
        calls = []

        def mock_post(sid, text, *args, **kwargs):
            calls.append(sid)
            if len(calls) == 1:
                raise RuntimeError("GET /chat/history -> status 404: session not found")
            return "new_mid_123"

        with patch.object(self.muse_service, "post_message", side_effect=mock_post):
            event = {
                "id": "msg_003",
                "content": "Hello",
                "author": {"user_openid": "ADMIN_OPENID"},
            }
            await adapter.handle_event("C2C_MESSAGE_CREATE", event)

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], old_sid)
            self.assertNotEqual(calls[1], old_sid)
            # Active session was updated
            self.assertEqual(adapter.active_session_id, calls[1])


if __name__ == "__main__":
    unittest.main()
