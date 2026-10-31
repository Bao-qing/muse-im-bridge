"""WebSocket client for the QQ gateway.

QQ's gateway answers WebSocket upgrade with `101 Switching Protocols`
plus `Connection: close` instead of `Connection: upgrade`. Standard
libraries like websockets fail with invalid connection header.
This client verifies 101 and Sec-WebSocket-Accept, ignoring the Connection value.
Runs sync I/O in an executor/thread and pumps frames into an asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import queue
import select
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlparse

from ...config import constants
from ...observability import get_logger, new_id, safe_error, safe_url, trace_event

logger = get_logger(__name__)


def _trace_ws(event, connection_id, **fields):
    # Never log raw frame bodies, fragmented text, close reasons or proxy auth.
    try:
        trace_event("qq.ws", event, connection_id=connection_id, **fields)
    except Exception:
        pass

# Default fallbacks (configurable in config.json)
WS_GUID = constants.DEFAULT_WS_GUID
UA = constants.DEFAULT_USER_AGENT


class QQWSError(Exception):
    pass


class QQWSClosed(QQWSError):
    pass


def _expected_accept(key: str, ws_guid: str = constants.DEFAULT_WS_GUID) -> str:
    return base64.b64encode(hashlib.sha1((key + ws_guid).encode()).digest()).decode()


def _build_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
    return header + mask + masked


def _blocking_handshake(
    host: str,
    port: int,
    path: str,
    proxy_url: str | None = None,
    ws_guid: str = constants.DEFAULT_WS_GUID,
    user_agent: str = constants.DEFAULT_USER_AGENT,
):
    connection_id, request_id = new_id(), new_id()
    started = time.perf_counter()
    _trace_ws("handshake.start", connection_id, request_id=request_id,
              url=safe_url(f"wss://{host}:{port}{path}"), proxy=bool(proxy_url))
    try:
        if proxy_url:
            p = urlparse(proxy_url)
            s = socket.create_connection((p.hostname, p.port or 3128), timeout=20)
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if p.username:
                auth = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
                req += f"Proxy-Authorization: Basic {auth}\r\n"
            req += "\r\n"
            s.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise QQWSError("proxy CONNECT: connection closed")
                buf += chunk
            if b" 200 " not in buf.splitlines()[0]:
                raise QQWSError(f"proxy CONNECT failed: {buf.splitlines()[0]}")
        else:
            s = socket.create_connection((host, port), timeout=20)

        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        try:
            s = ctx.wrap_socket(s, server_hostname=host)
        except Exception as e:
            raise QQWSError(f"TLS handshake failed: {e}")

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {user_agent}\r\n\r\n"
        )
        s.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise QQWSError("handshake: EOF before end of HTTP headers")
            buf += chunk
        header_bytes, leftover = buf.split(b"\r\n\r\n", 1)
        status_line = header_bytes.splitlines()[0].decode("latin-1", "replace")
        _trace_ws("handshake.response", connection_id, request_id=request_id,
                  status=int(status_line[9:12]) if status_line[9:12].isascii() and status_line[9:12].isdigit() else None,
                  header_bytes=len(header_bytes), leftover_bytes=len(leftover),
                  duration_ms=(time.perf_counter() - started) * 1000)
        if " 101 " not in status_line:
            raise QQWSError(f"handshake: expected 101, got {status_line!r}")

        headers = {}
        for line in header_bytes.splitlines()[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
        expected = _expected_accept(key, ws_guid)
        if actual := headers.get("sec-websocket-accept"):
            if actual != expected:
                raise QQWSError(f"bad Sec-WebSocket-Accept: got {actual!r}, expected {expected!r}")
        else:
            raise QQWSError("missing Sec-WebSocket-Accept header")

        _trace_ws("handshake.end", connection_id, request_id=request_id, status=101,
                  headers={name: "[REDACTED]" for name in headers},
                  duration_ms=(time.perf_counter() - started) * 1000)
        return s, leftover
    except BaseException as e:
        _trace_ws("handshake.error", connection_id, request_id=request_id,
                  error=safe_error(e), duration_ms=(time.perf_counter() - started) * 1000)
        raise


class QQWebSocket:
    def __init__(self, sock: ssl.SSLSocket, leftover: bytes, loop: asyncio.AbstractEventLoop):
        self._connection_id = new_id()
        self._sock = sock
        self._loop = loop
        self._send_q: queue.Queue = queue.Queue()
        self._recv_q: asyncio.Queue = asyncio.Queue()
        self._closed = threading.Event()
        self._close_reason: str = ""
        self._thread = threading.Thread(
            target=self._io_loop,
            args=(sock, leftover),
            daemon=True,
            name="qq-ws-io",
        )
        self._thread.start()

    @classmethod
    async def connect(
        cls,
        url: str,
        proxy_url: str | None = None,
        ws_guid: str = constants.DEFAULT_WS_GUID,
        user_agent: str = constants.DEFAULT_USER_AGENT,
    ) -> QQWebSocket:
        p = urlparse(url)
        host = p.hostname
        port = p.port or 443
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        loop = asyncio.get_running_loop()
        attempt_id, started = new_id(), time.perf_counter()
        _trace_ws("connect.start", attempt_id, url=safe_url(url))
        try:
            s, leftover = await loop.run_in_executor(
                None, _blocking_handshake, host, port, path, proxy_url, ws_guid, user_agent
            )
        except BaseException as e:
            _trace_ws("connect.error", attempt_id, error=safe_error(e),
                      duration_ms=(time.perf_counter() - started) * 1000)
            raise
        ws = cls(s, leftover, loop)
        _trace_ws("connect.end", ws._connection_id, attempt_id=attempt_id,
                  duration_ms=(time.perf_counter() - started) * 1000)
        return ws

    def _io_loop(self, sock: ssl.SSLSocket, initial_buf: bytes):
        buf = bytearray(initial_buf)
        frag_opcode = None
        frag_payload = bytearray()
        sock.setblocking(False)

        def _drain_send():
            while True:
                try:
                    data = self._send_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    _trace_ws("frame.send", self._connection_id, direction="out",
                              opcode=data[0] & 0x0F if data else None, wire_length=len(data))
                    sock.sendall(data)
                except Exception as e:
                    _trace_ws("send.error", self._connection_id, error=safe_error(e))
                    self._fail(f"send failed: {e}")
                    return False
            return True

        while not self._closed.is_set():
            has_send = not self._send_q.empty()
            try:
                rlist = [sock]
                wlist = [sock] if has_send else []
                r, w, _ = select.select(rlist, wlist, [], 0.5)
            except Exception as e:
                _trace_ws("select.error", self._connection_id, error=safe_error(e))
                self._fail(f"select failed: {e}")
                break

            if w and not _drain_send():
                break

            if r:
                try:
                    chunk = sock.recv(65536)
                except (BlockingIOError, ssl.SSLWantReadError):
                    continue
                except Exception as e:
                    _trace_ws("receive.error", self._connection_id, error=safe_error(e))
                    self._fail(f"recv failed: {e}")
                    break
                _trace_ws("receive", self._connection_id, direction="in", length=len(chunk))
                if not chunk:
                    self._fail("peer closed connection")
                    break
                buf.extend(chunk)

                # Parse complete frames
                while len(buf) >= 2:
                    b0, b1 = buf[0], buf[1]
                    fin = bool(b0 & 0x80)
                    opcode = b0 & 0x0F
                    masked = bool(b1 & 0x80)
                    length = b1 & 0x7F
                    offset = 2
                    if length == 126:
                        if len(buf) < 4:
                            break
                        length = int.from_bytes(buf[2:4], "big")
                        offset = 4
                    elif length == 127:
                        if len(buf) < 10:
                            break
                        length = int.from_bytes(buf[2:10], "big")
                        offset = 10
                    mask = None
                    if masked:
                        if len(buf) < offset + 4:
                            break
                        mask = buf[offset : offset + 4]
                        offset += 4
                    if len(buf) < offset + length:
                        break

                    payload = bytes(buf[offset : offset + length])
                    del buf[: offset + length]
                    if mask:
                        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

                    _trace_ws("frame.receive", self._connection_id, direction="in",
                              opcode=opcode, length=length, fin=fin, masked=masked)
                    if opcode == 0x8:  # CLOSE
                        self._fail("server sent close frame")
                        return
                    elif opcode == 0x9:  # PING
                        self._send_q.put(_build_frame(0xA, payload))
                    elif opcode == 0xA:  # PONG
                        pass
                    elif opcode == 0x0:  # CONTINUATION
                        frag_payload.extend(payload)
                        if fin:
                            text = bytes(frag_payload).decode("utf-8", "replace")
                            self._loop.call_soon_threadsafe(self._recv_q.put_nowait, text)
                            frag_opcode = None
                            frag_payload.clear()
                    elif opcode in (0x1, 0x2):
                        if fin:
                            text = payload.decode("utf-8", "replace") if opcode == 0x1 else str(payload)
                            self._loop.call_soon_threadsafe(self._recv_q.put_nowait, text)
                        else:
                            frag_opcode = opcode
                            frag_payload = bytearray(payload)

    def _fail(self, reason: str):
        if not self._closed.is_set():
            _trace_ws("closed", self._connection_id)
            logger.debug("QQ WebSocket closed connection_id=%s", self._connection_id)
            self._close_reason = reason
            self._closed.set()
            try:
                self._sock.close()
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._recv_q.put_nowait, QQWSClosed(reason))

    async def send_text(self, text: str):
        if self._closed.is_set():
            raise QQWSClosed(f"closed: {self._close_reason}")
        _trace_ws("send.enqueue", self._connection_id, direction="out", opcode=1, length=len(text))
        self._send_q.put(_build_frame(0x1, text.encode("utf-8")))

    async def recv(self) -> str:
        item = await self._recv_q.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        _trace_ws("close", self._connection_id)
        if not self._closed.is_set():
            try:
                self._send_q.put(_build_frame(0x8, b""))
            except Exception:
                pass
            self._closed.set()
            try:
                self._sock.close()
            except Exception:
                pass
