"""QQ Gateway WebSocket event listener.

Handles WebSocket connection, heartbeat maintenance, identify/resume,
and dispatches inbound message events to the adapter.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Optional

from .client import QQClient
from .ws_transport import QQWebSocket, QQWSClosed

# Intents for QQ Group and C2C private messages + Interaction:
INTENTS = (1 << 25) | (1 << 30) | (1 << 12)


class QQGateway:
    def __init__(
        self,
        client: QQClient,
        on_event: Callable[[str, dict], Coroutine[Any, Any, None]],
        proxy_url: Optional[str] = None,
        debug: bool = False,
    ):
        self.client = client
        self.on_event = on_event
        self.proxy_url = proxy_url
        self.debug = debug

        self._running = False
        self._ws: Optional[QQWebSocket] = None
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                gateway_url = self.client.get_gateway_url()
                if self.debug:
                    print(f"[QQGateway] Connecting to {gateway_url}...")
                self._ws = await QQWebSocket.connect(gateway_url, proxy_url=self.proxy_url)
                backoff = 1.0  # Reset backoff on successful connect
                await self._session_loop()
            except (QQWSClosed, asyncio.CancelledError) as e:
                if not self._running:
                    break
                if self.debug:
                    print(f"[QQGateway] Disconnected: {e}, reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:
                if not self._running:
                    break
                print(f"[QQGateway] Error in gateway connection: {e}")
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

        while self._running:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            op = msg.get("op")
            t = msg.get("t")
            d = msg.get("d")
            s = msg.get("s")

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
                    await ws.send_text(json.dumps(identify_payload))

            # Op 0: Dispatch
            elif op == 0:
                if t == "READY":
                    self._session_id = d.get("session_id")
                    print(f"[QQGateway] Logged in as {d.get('user', {}).get('username')} (ID: {d.get('user', {}).get('id')})")
                elif t == "RESUMED":
                    if self.debug:
                        print("[QQGateway] Session resumed successfully.")
                elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
                    asyncio.create_task(self._safe_dispatch(t, d))

            # Op 1: Heartbeat requested
            elif op == 1:
                await ws.send_text(json.dumps({"op": 1, "d": self._last_seq}))

            # Op 7: Reconnect requested by server
            elif op == 7:
                if self.debug:
                    print("[QQGateway] Reconnect requested by gateway server.")
                break

            # Op 9: Invalid Session
            elif op == 9:
                self._session_id = None
                self._last_seq = None
                if self.debug:
                    print("[QQGateway] Invalid session, clearing state and reconnecting.")
                break

    async def _safe_dispatch(self, t: str, d: dict) -> None:
        try:
            await self.on_event(t, d)
        except Exception as e:
            print(f"[QQGateway] Error dispatching event {t}: {e}")

    async def _heartbeat_loop(self, interval_s: float) -> None:
        while self._running and self._ws:
            try:
                await asyncio.sleep(interval_s)
                if self._ws:
                    await self._ws.send_text(json.dumps({"op": 1, "d": self._last_seq}))
            except asyncio.CancelledError:
                break
            except Exception:
                break
