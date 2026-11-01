"""OneBot v11 forward WebSocket event source.

Maintains a persistent connection to the OneBot server and forwards
incoming message events to the adapter. Reconnects with exponential
backoff (1s -> 30s, reset after a successful connect); ``stop()`` closes
the socket so a blocked ``recv()`` returns at once.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ...observability import get_logger, new_id, safe_error, safe_url, trace_event

logger = get_logger(__name__)

MAX_BACKOFF_S = 30.0


def _trace_gateway(event, connection_id, **fields):
    try:
        trace_event("onebot.gateway", event, connection_id=connection_id, **fields)
    except Exception:
        pass


try:
    import websockets
except ImportError:
    websockets = None  # Handled gracefully if not installed


def connect_url(ws_url: str, access_token: str) -> str:
    """``ws_url`` with ``access_token`` added to its query (path and query kept)."""
    if not access_token:
        return ws_url
    parts = urlsplit(ws_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "access_token"]
    query.append(("access_token", access_token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


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
        self.self_id: Optional[str] = None
        self._running = False
        self._ws = None

    async def start(self) -> None:
        if websockets is None:
            logger.warning("websockets library not available; OneBot WebSocket gateway disabled")
            return

        self._running = True
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        url = connect_url(self.ws_url, self.access_token)
        backoff = 1.0

        while self._running:
            connection_id = new_id()
            started = time.perf_counter()
            try:
                _trace_gateway("connect.start", connection_id, url=safe_url(url))
                async with websockets.connect(url, ssl=ssl_ctx if url.startswith("wss") else None) as ws:
                    self._ws = ws
                    backoff = 1.0
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
                        if not isinstance(event, dict):
                            continue

                        post_type = event.get("post_type")
                        if post_type == "meta_event":
                            if event.get("meta_event_type") == "lifecycle" and event.get("self_id"):
                                self.self_id = str(event["self_id"])
                            continue
                        # "message_sent" (the bot's own messages) is never dispatched.
                        if post_type == "message":
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
                logger.warning("OneBot WS connection error; reconnecting in %.0fs: %s", backoff, safe_error(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
            finally:
                self._ws = None

    async def stop(self) -> None:
        self._running = False
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=1.0)
            except Exception:
                pass

    async def _safe_dispatch(self, event: dict) -> None:
        try:
            await self.on_message(event)
        except Exception as e:
            logger.error("OneBot message dispatch failed: %s", safe_error(e))
