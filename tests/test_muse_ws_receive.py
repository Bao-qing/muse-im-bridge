"""Offline WebSocket framing regressions for short subscription polls."""

from collections import deque
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from muse_bridge.muse.client.transport import CipherState, UA, WS


UPGRADE = b"HTTP/1.1 101 Switching Protocols\r\nConnection: close\r\n\r\n"


def frame(payload=b"", opcode=2, *, fin=True, length_bytes=None, mask=None):
    """Build server frames, including forced extended lengths for boundary tests."""
    size = len(payload)
    if length_bytes is None:
        length_bytes = 0 if size < 126 else 2 if size < 65536 else 8
    marker = {0: size, 2: 126, 8: 127}[length_bytes]
    header = bytes([(0x80 if fin else 0) | opcode,
                    (0x80 if mask is not None else 0) | marker])
    if length_bytes:
        header += size.to_bytes(length_bytes, "big")
    return header + (mask or b"") + payload


class ChunkSocket:
    """Deterministic recv chunks/timeouts; never opens a real connection."""

    def __init__(self, events):
        self.events = deque(events)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def recv(self, size):
        if not self.events:
            raise AssertionError("unexpected socket read")
        event = self.events.popleft()
        if isinstance(event, BaseException):
            raise event
        if len(event) > size:
            self.events.appendleft(event[size:])
        return event[:size]

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def setsockopt(self, *args):
        pass

    def close(self):
        self.closed = True


class MuseWSReceiveTests(unittest.TestCase):
    def make_ws(self, *events, initial=b""):
        sock = ChunkSocket([UPGRADE + initial, *events])
        context = Mock()
        context.wrap_socket.return_value = sock
        with patch("muse_bridge.muse.client.transport.socket.create_connection", return_value=sock), \
                patch("muse_bridge.muse.client.transport.ssl.create_default_context", return_value=context), \
                patch("muse_bridge.muse.client.transport.os.urandom", side_effect=lambda n: b"k" * n):
            ws = WS("wss://offline.test/ws?test=1", direct=True)
        self.addCleanup(ws.close)
        return ws, sock

    def test_read_exact_retains_partial_bytes_on_timeout(self):
        ws, _ = self.make_ws(b"a", socket.timeout(), b"bc")
        with self.assertRaises(socket.timeout):
            ws._read_exact(3)
        self.assertEqual(ws.buf, b"a")
        self.assertEqual(ws._read_exact(3), b"abc")

    def test_resume_after_timeout_at_every_frame_boundary(self):
        # Includes partial base headers, extended lengths, optional mask bytes,
        # and payloads. Repeat the timeout to check that retries consume nothing.
        for length_bytes in (0, 2, 8):
            for mask in (None, b"mask"):
                wire = frame(b"approval snapshot", length_bytes=length_bytes, mask=mask)
                for split in range(len(wire)):
                    with self.subTest(length_bytes=length_bytes, mask=mask, split=split):
                        events = ([wire[:split]] if split else []) + [
                            socket.timeout(), socket.timeout(),
                            wire[split:] + frame(b"next"),
                        ]
                        ws, sock = self.make_ws(*events)
                        for _ in range(2):
                            with self.assertRaises(socket.timeout):
                                ws.recv(timeout=1)
                        self.assertEqual(ws.recv(timeout=3), (2, b"approval snapshot"))
                        self.assertEqual(ws.recv(), (2, b"next"))
                        self.assertEqual(sock.timeouts[-4:], [1, 1, 3, 20.0])

    def test_real_extended_payload_sizes_with_chunked_reads(self):
        for size in (0, 125, 126, 65535, 65536):
            with self.subTest(size=size):
                payload = b"x" * size
                wire = frame(payload)
                ws, _ = self.make_ws(*(wire[i:i + 997] for i in range(0, len(wire), 997)))
                self.assertEqual(ws.recv(), (2, payload))

    def test_fragmented_messages_resume_across_timeouts_and_controls(self):
        for opcode in (1, 2):
            with self.subTest(opcode=opcode):
                middle = frame(b"second-", opcode=0, fin=False)
                final = frame(b"third", opcode=0)
                ws, sock = self.make_ws(
                    frame(b"first-", opcode=opcode, fin=False), socket.timeout(),
                    frame(b"ping", opcode=9) + frame(b"pong", opcode=10) + middle[:1],
                    socket.timeout(), middle[1:] + final[:4], socket.timeout(),
                    final[4:] + frame(b"next"),
                )
                with patch("muse_bridge.muse.client.transport.os.urandom", return_value=b"mask"):
                    for _ in range(3):
                        with self.assertRaises(socket.timeout):
                            ws.recv(timeout=1)
                    self.assertEqual(ws.recv(), (opcode, b"first-second-third"))
                self.assertEqual(ws.recv(), (2, b"next"))
                expected_pong = b"\x8a\x84mask" + bytes(b ^ b"mask"[i % 4] for i, b in enumerate(b"ping"))
                self.assertEqual(sock.sent[1:], [expected_pong])

    def test_ping_pong_never_reach_noise_or_advance_receive_nonce(self):
        key = b"n" * 32
        sender, receiver = CipherState(key), CipherState(key)
        first = sender.encrypt(b"snapshot")
        second = sender.encrypt(b"event")
        ws, sock = self.make_ws(
            frame(b"unsolicited", opcode=10) + frame(b"ping", opcode=9) + frame(first),
            frame(b"", opcode=10), socket.timeout(), frame(second),
        )
        self.assertEqual(receiver.decrypt(ws.recv()[1]), b"snapshot")
        self.assertEqual(receiver.n, 1)
        with self.assertRaises(socket.timeout):
            ws.recv(timeout=1)
        self.assertEqual(receiver.n, 1)
        self.assertEqual(receiver.decrypt(ws.recv()[1]), b"event")
        self.assertEqual(receiver.n, 2)
        self.assertEqual(len(sock.sent), 2)  # Upgrade and one pong, no pong reply.

    def test_encrypted_fragmented_message_keeps_nonce_alignment_after_timeout(self):
        key = b"n" * 32
        sender, receiver = CipherState(key), CipherState(key)
        ciphertext = sender.encrypt(b"initial approval snapshot")
        tail = frame(ciphertext[9:], opcode=0)
        ws, _ = self.make_ws(
            frame(ciphertext[:9], fin=False) + tail[:5], socket.timeout(),
            tail[5:] + frame(sender.encrypt(b"approval event")),
        )
        with self.assertRaises(socket.timeout):
            ws.recv(timeout=1)
        self.assertEqual(receiver.n, 0)
        self.assertEqual(receiver.decrypt(ws.recv()[1]), b"initial approval snapshot")
        self.assertEqual(receiver.decrypt(ws.recv()[1]), b"approval event")

    def test_control_frames_resume_after_timeout_at_every_boundary(self):
        for opcode in (9, 10):
            wire = frame(b"heartbeat", opcode=opcode)
            for split in range(1, len(wire)):
                with self.subTest(opcode=opcode, split=split):
                    ws, sock = self.make_ws(
                        wire[:split], socket.timeout(), wire[split:] + frame(b"data"),
                    )
                    with self.assertRaises(socket.timeout):
                        ws.recv(timeout=1)
                    self.assertEqual(ws.recv(), (2, b"data"))
                    self.assertEqual(len(sock.sent), 2 if opcode == 9 else 1)

    def test_invalid_opcodes_never_return_noise_payloads(self):
        for opcode in (0, 3, 7, 11, 15):
            with self.subTest(opcode=opcode):
                ws, _ = self.make_ws(frame(b"not noise", opcode=opcode))
                with self.assertRaisesRegex(ConnectionError, "unexpected ws opcode"):
                    ws.recv()
        ws, _ = self.make_ws(frame(b"partial", fin=False) + frame(b"new message"))
        with self.assertRaisesRegex(ConnectionError, "new ws data frame"):
            ws.recv()

    def test_upgrade_leftover_contains_complete_and_partial_frames(self):
        wire = frame(b"second")
        ws, _ = self.make_ws(socket.timeout(), wire[3:], initial=frame(b"first") + wire[:3])
        self.assertEqual(ws.recv(), (2, b"first"))
        with self.assertRaises(socket.timeout):
            ws.recv(timeout=1)
        self.assertEqual(ws.recv(), (2, b"second"))

    def test_close_frame_preserves_exception_after_timeout_during_fragments(self):
        payload = struct.pack(">H", 1000) + b"server finished"
        wire = frame(payload, opcode=8)
        ws, sock = self.make_ws(
            frame(b"unfinished", fin=False) + wire[:3], socket.timeout(), wire[3:],
        )
        with self.assertRaises(socket.timeout):
            ws.recv(timeout=1)
        with self.assertRaises(ConnectionError) as raised:
            ws.recv()
        self.assertEqual(str(raised.exception), f"ws closed by server: {payload[:120]}")
        self.assertEqual(len(sock.sent), 1)  # No new close reply or implicit close.
        self.assertFalse(sock.closed)

    def test_socket_eof_preserves_exception_and_explicit_close(self):
        ws, sock = self.make_ws(b"\x82", b"")
        with self.assertRaisesRegex(ConnectionError, "^socket closed$"):
            ws.recv()
        self.assertFalse(sock.closed)
        ws.close()
        self.assertTrue(sock.closed)

    def test_default_upgrade_request_and_binary_send_bytes_are_unchanged(self):
        ws, sock = self.make_ws()
        self.assertEqual(sock.sent[0], (
            "GET /ws?test=1 HTTP/1.1\r\nHost: offline.test\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: a2tra2tra2tra2tra2traw==\r\n"
            "Sec-WebSocket-Version: 13\r\nOrigin: https://muse.ai\r\n"
            f"User-Agent: {UA}\r\nAccept-Encoding: gzip, deflate, br, zstd\r\n"
            "Accept-Language: zh-CN,zh;q=0.9\r\nPragma: no-cache\r\nCache-Control: no-cache\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n\r\n"
        ).encode())
        with patch("muse_bridge.muse.client.transport.os.urandom", return_value=b"mask"):
            ws.send(b"hello")
        self.assertEqual(sock.sent[1], b"\x82\x85mask" + bytes(b ^ b"mask"[i % 4] for i, b in enumerate(b"hello")))


if __name__ == "__main__":
    unittest.main()
