"""QQ socket failures may reconnect; cancellation must stop the gateway."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from muse_bridge.adapters.qq.gateway import QQGateway


class QQGatewayLifecycleCase(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_session_closes_socket_without_reconnecting(self):
        client = MagicMock()
        client.get_gateway_url.return_value = "wss://example.invalid/qq"
        gateway = QQGateway(client=client, on_event=AsyncMock())
        socket = MagicMock()
        socket.close = AsyncMock()
        entered = asyncio.Event()

        async def session():
            entered.set()
            await asyncio.Event().wait()

        gateway._session_loop = session
        with patch("muse_bridge.adapters.qq.gateway.QQWebSocket.connect", new=AsyncMock(return_value=socket)):
            task = asyncio.create_task(gateway.start())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertFalse(gateway._running)
        socket.close.assert_awaited_once()
        client.get_gateway_url.assert_called_once()
