"""OneBot v11 forward WebSocket gateway.

Maintains a persistent connection to the OneBot server and forwards
incoming message events to the adapter.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlparse

try:
    import websockets
except ImportError:
    websockets = None  # Handled gracefully if not installed


class OneBotGateway:
    def __init__(
        self,
        ws_url: str,
        access_token: str,
        on_message: Callable[[dict], Coroutine[Any, Any, None]],
    ):
        self.ws_url = ws_url.rstrip("/")
        self.access_token = access_token
        self.on_message = on_message
        self._running = False

    async def start(self) -> None:
        if websockets is None:
            print("[OneBotGateway] websockets library not available; WebSocket gateway disabled.")
            return

        self._running = True
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        token_sep = "&" if "?" in self.ws_url else "?"
        url = f"{self.ws_url}/{token_sep}access_token={self.access_token}" if self.access_token else self.ws_url

        while self._running:
            try:
                async with websockets.connect(url, ssl=ssl_ctx) as ws:
                    print(f"[OneBotGateway] Connected to OneBot WS at {self.ws_url}")
                    while self._running:
                        msg_raw = await ws.recv()
                        try:
                            event = json.loads(msg_raw)
                        except Exception:
                            continue

                        if event.get("post_type") == "message":
                            asyncio.create_task(self._safe_dispatch(event))
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                print(f"[OneBotGateway] Connection error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5.0)

    async def stop(self) -> None:
        self._running = False

    async def _safe_dispatch(self, event: dict) -> None:
        try:
            await self.on_message(event)
        except Exception as e:
            print(f"[OneBotGateway] Error dispatching message: {e}")
