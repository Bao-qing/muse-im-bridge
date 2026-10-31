"""OneBot v11 forward WebSocket gateway.

Maintains a persistent connection to the OneBot server and forwards
incoming message events to the adapter.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlparse

from ...observability import get_logger, new_id, safe_error, safe_url, trace_event

logger = get_logger(__name__)


def _trace_gateway(event, connection_id, **fields):
    try:
        trace_event("onebot.gateway", event, connection_id=connection_id, **fields)
    except Exception:
        pass


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
            logger.warning("websockets library not available; OneBot WebSocket gateway disabled")
            return

        self._running = True
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        token_sep = "&" if "?" in self.ws_url else "?"
        url = f"{self.ws_url}/{token_sep}access_token={self.access_token}" if self.access_token else self.ws_url

        while self._running:
            connection_id = new_id()
            started = time.perf_counter()
            try:
                _trace_gateway("connect.start", connection_id, url=safe_url(url))
                async with websockets.connect(url, ssl=ssl_ctx) as ws:
                    logger.info("Connected to OneBot WS at %s", safe_url(self.ws_url))
                    _trace_gateway("connect.end", connection_id,
                                   duration_ms=(time.perf_counter() - started) * 1000)
                    while self._running:
                        msg_raw = await ws.recv()
                        # No raw JSON, attachment, quoted message or OTP text at this layer.
                        _trace_gateway("receive", connection_id,
                                       direction="in", length=len(msg_raw))
                        try:
                            event = json.loads(msg_raw)
                        except Exception:
                            continue

                        if event.get("post_type") == "message":
                            _trace_gateway("dispatch", connection_id,
                                           post_type="message")
                            asyncio.create_task(self._safe_dispatch(event))
            except asyncio.CancelledError:
                break
            except Exception as e:
                _trace_gateway("connect.error", connection_id,
                               error=safe_error(e), duration_ms=(time.perf_counter() - started) * 1000)
                if not self._running:
                    break
                logger.warning("OneBot WS connection error; reconnecting in 5s: %s", safe_error(e))
                await asyncio.sleep(5.0)

    async def stop(self) -> None:
        self._running = False

    async def _safe_dispatch(self, event: dict) -> None:
        try:
            await self.on_message(event)
        except Exception as e:
            logger.error("OneBot message dispatch failed: %s", safe_error(e))
