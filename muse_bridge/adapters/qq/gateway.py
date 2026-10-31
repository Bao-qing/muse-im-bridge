"""QQ Gateway WebSocket event listener.

Handles WebSocket connection, heartbeat maintenance, identify/resume,
and dispatches inbound message events to the adapter.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Optional

from ...config import constants
from ...observability import get_logger, new_id, safe_error, safe_url, trace_event
from .client import QQClient
from .ws_transport import QQWebSocket, QQWSClosed

logger = get_logger(__name__)

# Intents for QQ Group and C2C private messages + Interaction:
INTENTS = (1 << 25) | (1 << 30) | (1 << 12)
_EVENT_TYPES = {"READY", "RESUMED", "C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE",
                "GROUP_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}


def _trace_gateway(event, connection_id, *, direction=None, op=None, t=None, s=None, **fields):
    # Unknown events, attachments, references and OTP fragments never reach logs.
    try:
        trace_event("qq.gateway", event, connection_id=connection_id, direction=direction,
                    op=op if type(op) is int else None,
                    t=t if isinstance(t, str) and t in _EVENT_TYPES else None,
                    s=s if type(s) is int else None, **fields)
    except Exception:
        pass


class QQGateway:
    def __init__(
        self,
        client: QQClient,
        on_event: Callable[[str, dict], Coroutine[Any, Any, None]],
        proxy_url: Optional[str] = None,
        debug: bool = False,
        ws_guid: str = constants.DEFAULT_WS_GUID,
        user_agent: str = constants.DEFAULT_USER_AGENT,
    ):
        self.client = client
        self.on_event = on_event
        self.proxy_url = proxy_url
        self.debug = debug
        self.ws_guid = ws_guid
        self.user_agent = user_agent

        self._running = False
        self._ws: Optional[QQWebSocket] = None
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connection_id: Optional[str] = None

    async def start(self) -> None:
        self._running = True
        backoff = 1.0

        while self._running:
            connection_id = new_id()
            started = time.perf_counter()
            try:
                gateway_url = self.client.get_gateway_url()
                if self.debug:
                    logger.debug("Connecting to QQ gateway %s", safe_url(gateway_url))
                _trace_gateway("connect.start", connection_id, url=safe_url(gateway_url))
                self._ws = await QQWebSocket.connect(
                    gateway_url,
                    proxy_url=self.proxy_url,
                    ws_guid=self.ws_guid,
                    user_agent=self.user_agent,
                )
                self._connection_id = connection_id
                _trace_gateway("connect.end", connection_id,
                               transport_connection_id=getattr(self._ws, "_connection_id", None),
                               duration_ms=(time.perf_counter() - started) * 1000)
                backoff = 1.0  # Reset backoff on successful connect
                await self._session_loop()
            except (QQWSClosed, asyncio.CancelledError) as e:
                _trace_gateway("disconnect", connection_id, error=safe_error(e))
                if not self._running:
                    break
                if self.debug:
                    logger.debug("QQ gateway disconnected; reconnecting in %ss: %s", backoff, safe_error(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:
                _trace_gateway("connect.error", connection_id, error=safe_error(e),
                               duration_ms=(time.perf_counter() - started) * 1000)
                if not self._running:
                    break
                logger.error("QQ gateway connection failed: %s", safe_error(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _session_loop(self) -> None:
        ws = self._ws
        if not ws:
            return
        connection_id = self._connection_id or new_id()

        while self._running:
            raw = await ws.recv()
            _trace_gateway("receive", connection_id, direction="in", length=len(raw))
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            op = msg.get("op")
            t = msg.get("t")
            d = msg.get("d")
            s = msg.get("s")
            _trace_gateway("envelope", connection_id, direction="in", op=op, t=t, s=s)

            if s is not None:
                self._last_seq = s

            # Op 10: Hello -> Start heartbeat and Identify
            if op == 10:
                interval_ms = d.get("heartbeat_interval", 45000)
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval_ms / 1000.0))

                if self._session_id and self._last_seq is not None:
                    # Try Resume
                    resume_payload = {
                        "op": 6,
                        "d": {
                            "token": f"QQBot {self.client.get_access_token()}",
                            "session_id": self._session_id,
                            "seq": self._last_seq,
                        },
                    }
                    _trace_gateway("send", connection_id, direction="out", op=6, s=self._last_seq, session_present=True)
                    await ws.send_text(json.dumps(resume_payload))
                else:
                    # Identify
                    identify_payload = {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {self.client.get_access_token()}",
                            "intents": INTENTS,
                            "shard": [0, 1],
                            "properties": {
                                "$os": "windows",
                                "$browser": "muse",
                                "$device": "muse",
                            },
                        },
                    }
                    _trace_gateway("send", connection_id, direction="out", op=2, intents=INTENTS, shard=[0, 1])
                    await ws.send_text(json.dumps(identify_payload))

            # Op 0: Dispatch
            elif op == 0:
                if t == "READY":
                    self._session_id = d.get("session_id")
                    logger.info("QQ gateway logged in (username_present=%s id_present=%s)",
                                d.get("user", {}).get("username") is not None,
                                d.get("user", {}).get("id") is not None)
                    _trace_gateway("ready", connection_id, session_present=bool(self._session_id))
                elif t == "RESUMED":
                    if self.debug:
                        logger.debug("QQ gateway session resumed successfully")
                elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
                    asyncio.create_task(self._safe_dispatch(t, d))

            # Op 1: Heartbeat requested
            elif op == 1:
                _trace_gateway("send", connection_id, direction="out", op=1, s=self._last_seq)
                await ws.send_text(json.dumps({"op": 1, "d": self._last_seq}))

            # Op 7: Reconnect requested by server
            elif op == 7:
                if self.debug:
                    logger.debug("QQ gateway server requested reconnect")
                break

            # Op 9: Invalid Session
            elif op == 9:
                self._session_id = None
                self._last_seq = None
                if self.debug:
                    logger.debug("QQ gateway invalid session; clearing state and reconnecting")
                break

    async def _safe_dispatch(self, t: str, d: dict) -> None:
        try:
            await self.on_event(t, d)
        except Exception as e:
            logger.error("QQ gateway event dispatch failed: %s", safe_error(e))

    async def _heartbeat_loop(self, interval_s: float) -> None:
        connection_id = self._connection_id
        while self._running and self._ws:
            try:
                await asyncio.sleep(interval_s)
                if self._ws:
                    _trace_gateway("send", connection_id, direction="out", op=1, s=self._last_seq)
                    await self._ws.send_text(json.dumps({"op": 1, "d": self._last_seq}))
            except asyncio.CancelledError:
                break
            except Exception:
                break
