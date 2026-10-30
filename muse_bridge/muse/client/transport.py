"""Low-level transport for the Muse gateway: Noise_XX crypto and the
hand-rolled WebSocket underneath it.

This module knows nothing about chat, sessions, QQ, WeChat, or any
adapter. It only moves encrypted bytes: Noise handshake state, the
Noise_XX_25519_AESGCM_SHA256 cipher states, and a WebSocket that
tolerates the ingress answering 101 with `Connection: close` (it checks
only the 101 status, unlike the strict `websockets` library).

The WebSocket route is explicit, never read from the environment: pass
proxy_url for the egress CONNECT proxy (the VM has no direct TLS
egress), or direct=True when running somewhere with a direct route
(e.g. the user's PC). Reading configuration is the main program's job,
never this library's.

Nonce note (carried over from the reverse-engineering): the bundle's
cipher builds the 12-byte nonce with the 64-bit counter BIG-endian in
bytes 4..12, deviating from the Noise spec's little-endian layout.
Handshake nonces are all 0, so this only matters for transport frames,
but getting it wrong breaks the whole session.
"""
import base64
import hashlib
import hmac as hmac_mod
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


# ---------- Noise ----------
def hkdf(salt: bytes, ikm: bytes, n: int) -> list:
    temp = hmac_mod.new(salt, ikm, hashlib.sha256).digest()
    outs, prev = [], b""
    for i in range(1, n + 1):
        prev = hmac_mod.new(temp, prev + bytes([i]), hashlib.sha256).digest()
        outs.append(prev)
    return outs


class SymmetricState:
    def __init__(self):
        name = b"Noise_XX_25519_AESGCM_SHA256"
        self.h = name + b"\x00" * (32 - len(name))
        self.ck = self.h
        self.k = None
        self.n = 0
        self.mix_hash(b"")  # empty prologue

    def mix_hash(self, data: bytes):
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes):
        self.ck, self.k = hkdf(self.ck, ikm, 2)
        self.n = 0

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        if self.k is not None:
            nonce = b"\x00" * 4 + struct.pack(">Q", self.n)
            self.n += 1
            ct = AESGCM(self.k).encrypt(nonce, plaintext, self.h)
        else:
            ct = plaintext
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        if self.k is not None:
            nonce = b"\x00" * 4 + struct.pack(">Q", self.n)
            self.n += 1
            pt = AESGCM(self.k).decrypt(nonce, ciphertext, self.h)
        else:
            pt = ciphertext
        self.mix_hash(ciphertext)
        return pt

    def split(self):
        k1, k2 = hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


class CipherState:
    """Transport cipher with BIG-ENDIAN 64-bit nonces (see module note)."""

    def __init__(self, key: bytes):
        self.k = key
        self.n = 0

    def encrypt(self, plaintext: bytes, ad: bytes = b"") -> bytes:
        nonce = b"\x00" * 4 + struct.pack(">Q", self.n)
        self.n += 1
        return AESGCM(self.k).encrypt(nonce, plaintext, ad)

    def decrypt(self, ciphertext: bytes, ad: bytes = b"") -> bytes:
        nonce = b"\x00" * 4 + struct.pack(">Q", self.n)
        self.n += 1
        return AESGCM(self.k).decrypt(nonce, ciphertext, ad)


def gen_keypair():
    priv = X25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw()


def dh(priv, pub_bytes: bytes) -> bytes:
    return priv.exchange(X25519PublicKey.from_public_bytes(pub_bytes))


def noise_handshake(ws) -> tuple:
    """Run Noise_XX over an already-upgraded WS. Returns
    (send_cipher, recv_cipher). Raises on any handshake failure."""
    from .protocol import pb_bytes

    e_priv, e_pub = gen_keypair()
    ss = SymmetricState()
    ss.mix_hash(e_pub)  # writeMessage1: mixHash(e) first, then encryptAndHash
    msg1 = e_pub + ss.encrypt_and_hash(pb_bytes(1, os.urandom(32)))
    if len(msg1) != 66:
        raise RuntimeError(f"msg1 len={len(msg1)} (expect 66)")
    ws.send(msg1)
    _, msg2 = ws.recv(timeout=25)
    if len(msg2) != 166:
        raise RuntimeError(f"msg2 len={len(msg2)} (expect 166)")
    pos = 0
    re_pub = msg2[pos:pos + 32]
    pos += 32
    ss.mix_hash(re_pub)
    ss.mix_key(dh(e_priv, re_pub))  # ee
    rs_pub = ss.decrypt_and_hash(msg2[pos:pos + 48])
    pos += 48
    ss.mix_key(dh(e_priv, rs_pub))  # es
    ss.decrypt_and_hash(msg2[pos:])  # msg2 payload (empty in standard mode)
    s_priv, s_pub = gen_keypair()
    msg3 = ss.encrypt_and_hash(s_pub)
    ss.mix_key(dh(s_priv, re_pub))  # se
    msg3 += ss.encrypt_and_hash(b"")
    if len(msg3) != 64:
        raise RuntimeError(f"msg3 len={len(msg3)} (expect 64)")
    ws.send(msg3)
    return ss.split()


# ---------- WebSocket transport (via egress CONNECT proxy) ----------
class WS:
    def __init__(self, url: str, proxy_url: str = None, direct: bool = False):
        u = urlparse(url)
        host = u.hostname
        path = u.path + ("?" + u.query if u.query else "")
        if direct:
            s = socket.create_connection((host, 443), timeout=15)
        elif proxy_url:
            px = urlparse(proxy_url)
            if not px.hostname:
                raise RuntimeError(f"bad proxy_url: {proxy_url!r}")
            s = socket.create_connection((px.hostname, px.port), timeout=15)
            auth = ""
            if px.username:
                cred = base64.b64encode(
                    f"{px.username}:{px.password}".encode()).decode()
                auth = f"Proxy-Authorization: Basic {cred}\r\n"
            s.sendall(
                f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n{auth}\r\n".encode()
            )
            resp = self._read_until(s, b"\r\n\r\n")
            status = resp.split(b"\r\n")[0].decode()
            if " 200" not in status:
                raise RuntimeError(f"proxy CONNECT failed: {status}")
        else:
            raise RuntimeError(
                "WS route is not configured: pass proxy_url, or direct=True")
        self._enable_tcp_keepalive(s)
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        self.sock = ctx.wrap_socket(s, server_hostname=host)
        self.sock.settimeout(20.0)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: https://muse.ai\r\n"
            f"User-Agent: {UA}\r\nAccept-Encoding: gzip, deflate, br, zstd\r\n"
            f"Accept-Language: zh-CN,zh;q=0.9\r\nPragma: no-cache\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = self._read_until(self.sock, b"\r\n\r\n")
        head = resp.split(b"\r\n\r\n")[0].decode(errors="replace")
        if " 101" not in head.split("\r\n")[0]:
            raise RuntimeError(f"WS upgrade failed: {head[:300]}")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    @staticmethod
    def _enable_tcp_keepalive(sock) -> None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except Exception:
            pass

    @staticmethod
    def _read_until(sock, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return data

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data: bytes, opcode: int = 2):
        mask = os.urandom(4)
        ln = len(data)
        if ln < 126:
            hdr = bytes([0x80 | opcode, 0x80 | ln])
        elif ln < 65536:
            hdr = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", ln)
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", ln)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + masked)

    def recv(self, timeout: float = 20.0):
        """Return (opcode, payload) of the next data/control frame."""
        self.sock.settimeout(timeout)
        frags = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            if b1 & 0x80:
                self._read_exact(4)  # server frames are unmasked; skip if present
            payload = self._read_exact(ln) if ln else b""
            if opcode == 9:  # ping
                self.send(payload, opcode=10)
                continue
            if opcode == 8:
                raise ConnectionError(f"ws closed by server: {payload[:120]}")
            frags.append(payload)
            if fin:
                return opcode, b"".join(frags)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
