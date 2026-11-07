"""MuseClient: the adapter-facing Muse gateway client.

One instance owns one Noise+WebSocket connection. Typical use (one
connection per turn, the pattern the QQ relay has used since 2026-10-24
because it sidesteps stale sessions entirely):

    # Tokens and deployment values come from the caller (the main program
    # reads the mint file and config and passes the values in); this
    # library never reads config files or environment variables itself,
    # and it carries no deployment defaults for vm_id or client_ip.
    with MuseClient(token, notary_token, vm_id, client_ip,
                    proxy_url=proxy_url) as client:
        turn = client.send_and_wait(session_id, "hello")
        print(turn.text)

Or, for a persistent connection shared across turns (same instance,
sequence numbers keep incrementing per request):

    client = MuseClient(token, notary_token, vm_id, client_ip,
                        proxy_url=proxy_url)
    client.connect()
    turn = client.send_and_wait(session_id, "hello")
    turn = client.send_and_wait(session_id, "and another")
    client.close()

The client is synchronous and NOT thread-safe: one turn at a time per
instance. Async adapters run it in an executor (see adapters/qq).

The client knows nothing about QQ, WeChat, or any adapter: it takes
tokens + session ids in, and returns TurnResult objects out. Rendering
a turn to plain text lives in render.py, so a new platform reuses it
unchanged.
"""
import base64
import json
import time
import uuid
from dataclasses import dataclass, field

from . import render
from .errors import (
    ConnectionLost,
    HandshakeError,
    MuseError,
    PushFailed,
    ReplyTimeout,
    RPCError,
    SubscribeError,
    TurnCancelled,
)
from .push import PushTurnTracker, parse_subscribe_chunk
from .protocol import (
    ChunkReassembler,
    chunked_transport_frames,
    http_request_envelope,
    parse_service_frame,
    transport_frame,
    unwrap_service_response,
)
from .transport import WS, noise_handshake
from urllib.parse import quote

DEFAULT_CAPABILITIES = [
    "hotel_results",
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
]

# File upload, captured 2026-10-25 from the web client
# (buildAttachmentItems + uploadFile): non-image files at/above the
# web client's eager threshold go up first, in 512 KiB /fs/write
# chunks, then the chat turn carries only a file_ref. The relay uses
# a lower threshold (see adapters/qq) because its VM inline route
# fails far earlier than the web client's 10 MiB threshold.
UPLOAD_CHUNK_SIZE = 512 * 1024
WEB_EAGER_UPLOAD_THRESHOLD = 10 * 1024 * 1024


@dataclass
class TurnMessage:
    message_id: str
    seq: int
    text: str
    # Image descriptors from render.extract_images /
    # render.extract_workspace_images, if the Muse reply carried any.
    # URL descriptors are downloaded by the adapter; workspace_path
    # descriptors are read via client.read_file. The adapter turns
    # both into QQ media messages (upload bytes to QQ).
    images: list = field(default_factory=list)
    # File descriptors from render.extract_files, same shape as images
    # ({url, filename, content_type}) but for non-image attachments.
    # URL-bearing only: a file_ref part with a media-library path and
    # no URL cannot be downloaded by the adapter, so it is not listed.
    files: list = field(default_factory=list)
    # Video descriptors from render.extract_workspace_videos, same
    # shape ({workspace_path, filename, content_type}). The video
    # presentation shape is not yet captured live, so the
    # extractor accepts video_path / data.file.path / markdown.
    videos: list = field(default_factory=list)


@dataclass
class TurnResult:
    messages: list = field(default_factory=list)
    text: str = ""
    user_message_id: str = ""


from ...observability import new_id, observed, trace_event


class PushSubscription:
    """A live /chat/subscribe stream. read_events() returns parsed event
    dicts (newline-delimited JSON from body_chunks), filtering to this
    stream_id. The stream never ends (no end_body) — the caller stops
    reading when its turn logic says done. reset() discards buffered
    data (used to drop pre-send snapshots so a stale task.completed
    cannot prematurely mark a new turn done)."""

    def __init__(self, client, stream_id: int, buffer: bytes = b""):
        self._client = client
        self.stream_id = stream_id
        self._buf = buffer
        if hasattr(client, "_register_subscription"):
            client._register_subscription(self)

    def reset(self):
        self._buf = b""

    def feed_bytes(self, data: bytes):
        if data:
            self._buf += data

    def drain_events(self):
        events = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(obj, dict):
                trace_event("muse.push", "event.receive", connection_id=getattr(self._client, "_log_connection_id", ""), stream_id=self.stream_id, payload=obj)
                events.append(obj)
        return events

    def read_events(self, timeout: float):
        """Read until at least one complete event is available or the
        timeout expires. Returns a list (possibly empty on timeout).
        Raises ConnectionLost on transport drop."""
        deadline = time.time() + timeout
        events = self.drain_events()
        if events:
            return events
        while time.time() < deadline:
            try:
                _, frame = self._client._ws.recv(
                    timeout=min(10, max(0.1, deadline - time.time())))
            except ConnectionError as e:
                raise ConnectionLost(f"push stream lost: {e}") from e
            except Exception:
                continue
            try:
                pt = self._client._recv_cs.decrypt(frame)
            except Exception as e:
                raise ConnectionLost(f"push decrypt failed: {e}") from e
            full = self._client._reasm.feed(pt)
            if full is None:
                continue
            sid, kind, detail = parse_service_frame(unwrap_service_response(full))
            trace_event("muse.rpc", "frame.receive", stream_id=sid, kind=kind, payload=detail, messages=True)
            if sid != self.stream_id:
                if hasattr(self._client, "_dispatch_other_stream_frame"):
                    self._client._dispatch_other_stream_frame(sid, kind, detail)
                continue
            if kind == "body_chunk":
                data = detail.get("data", b"")
                if data:
                    self._buf += data
                events.extend(self.drain_events())
                if events:
                    return events
                if detail.get("end_body"):
                    break
        events.extend(self.drain_events())
        return events


class MuseClient:
    def __init__(self, token, notary_token, vm_id, client_ip,
                 host="hatch.metaaivm.com",
                 timezone="Asia/Shanghai", proxy_url=None, direct=False):
        # vm_id and client_ip are required, with no defaults: they are
        # deployment values (your VM, your egress IP), not library
        # constants. host is the shared service endpoint and timezone a
        # generic default; both are overridable.
        self.token = token
        self.notary_token = notary_token
        self.vm_id = vm_id
        self.host = host
        self.client_ip = client_ip
        self.timezone = timezone
        self.proxy_url = proxy_url
        self.direct = direct
        self._ws = None
        self._send_cs = None
        self._recv_cs = None
        self._reasm = None
        self._seq = 0
        self._subscriptions = {}  # stream_id -> PushSubscription
        self._log_connection_id = new_id()

    def _register_subscription(self, sub: PushSubscription):
        self._subscriptions[sub.stream_id] = sub

    def _dispatch_other_stream_frame(self, sid: int, kind: str, detail: dict):
        sub = self._subscriptions.get(sid)
        if sub is None:
            return
        if kind == "body_chunk":
            sub.feed_bytes(detail.get("data", b""))
        elif kind == "response" and detail.get("body"):
            sub.feed_bytes(detail["body"])

    def read_any_subscription_events(self, timeout: float = 1.0):
        """Read multiplexed push events across all active PushSubscriptions on this connection.
        Returns a flat list of event dicts."""
        events = []
        for sub in list(self._subscriptions.values()):
            events.extend(sub.drain_events())
        if events:
            return events

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, frame = self._ws.recv(
                    timeout=min(10, max(0.05, deadline - time.time())))
            except ConnectionError as e:
                raise ConnectionLost(f"push stream lost: {e}") from e
            except Exception:
                continue
            try:
                pt = self._recv_cs.decrypt(frame)
            except Exception as e:
                raise ConnectionLost(f"push decrypt failed: {e}") from e
            full = self._reasm.feed(pt)
            if full is None:
                continue
            sid, kind, detail = parse_service_frame(unwrap_service_response(full))
            trace_event("muse.rpc", "frame.receive", stream_id=sid, kind=kind, payload=detail, messages=True)
            self._dispatch_other_stream_frame(sid, kind, detail)
            for sub in list(self._subscriptions.values()):
                events.extend(sub.drain_events())
            if events:
                return events
        return events

    # -- connection ---------------------------------------------------
    @observed("muse.connection")
    def connect(self):
        if self._ws is not None:
            return
        self._log_connection_id = new_id()
        trace_event("muse.connection", "open", connection_id=self._log_connection_id, host=self.host, vm_id=self.vm_id)
        params = [
            f"vm_id={self.vm_id}",
            f"auth_token={quote(self.token, safe='')}",
            f"notary_token={quote(self.notary_token, safe='')}",
            "app_id=hatch-web",
            f"request_id={uuid.uuid4()}",
        ]
        url = f"wss://{self.host}/v1/noise?" + "&".join(params)
        ws = None
        try:
            ws = WS(url, proxy_url=self.proxy_url, direct=self.direct)
            send_cs, recv_cs = noise_handshake(ws)
        except Exception as e:
            if ws is not None:
                ws.close()
            raise HandshakeError(f"gateway handshake failed ({type(e).__name__})", status=getattr(e, "status", None)) from None
        self._ws = ws
        self._send_cs = send_cs
        self._recv_cs = recv_cs
        self._reasm = ChunkReassembler()
        self._seq = 0
        self._subscriptions = {}

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._send_cs = None
            self._recv_cs = None
            self._subscriptions = {}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _ensure_connected(self):
        if self._ws is None:
            self.connect()

    # -- raw RPC ------------------------------------------------------
    def _read_stream_to_end(self, want_seq, timeout):
        """Read frames until want_seq's end_body. Returns (status, chunks)."""
        chunks = []
        status = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, frame = self._ws.recv(
                    timeout=min(20, max(0.05, deadline - time.time())))
            except ConnectionError as e:
                raise ConnectionLost(f"gateway connection lost: {e}") from e
            except Exception:
                continue
            try:
                pt = self._recv_cs.decrypt(frame)
            except Exception as e:
                raise ConnectionLost(f"transport decrypt failed: {e}") from e
            full = self._reasm.feed(pt)
            if full is None:
                continue
            sid, kind, detail = parse_service_frame(unwrap_service_response(full))
            trace_event("muse.rpc", "frame.receive", stream_id=sid, kind=kind, payload=detail, messages=True)
            if sid != want_seq:
                self._dispatch_other_stream_frame(sid, kind, detail)
                continue
            if kind == "response":
                status = detail.get("status")
                if detail["body"]:
                    chunks.append(detail["body"])
                if detail["end_body"]:
                    break
            elif kind == "body_chunk":
                chunks.append(detail["data"])
                if detail["end_body"]:
                    break
        return status, chunks

    @observed("muse.rpc")
    def request(self, method, path, body=b"", timeout=30,
                include_content_type=True, service="daemon"):
        """One RPC on a fresh stream. Returns the joined body bytes.
        Raises RPCError on a non-200 status, MuseError on timeout."""
        self._ensure_connected()
        self._seq += 1
        seq = self._seq
        trace_event("muse.rpc", "stream.open", connection_id=self._log_connection_id, stream_id=seq)
        env = http_request_envelope(seq, method, path, body,
                                    include_content_type=include_content_type,
                                    service=service)
        try:
            # Large requests (image base64) must be chunked: a single
            # ~300KB transport chunk made the server close the
            # connection (2026-10-25). Each chunk is encrypted with the
            # next Noise nonce, in order, under one chunk_id.
            for frame in chunked_transport_frames(env):
                self._ws.send(self._send_cs.encrypt(frame))
        except ConnectionError as e:
            raise ConnectionLost(f"gateway send failed: {e}") from e
        status, chunks = self._read_stream_to_end(seq, timeout)
        if status is None:
            raise MuseError(f"{method} {path} timed out after {timeout}s")
        data = b"".join(chunks)
        if status != 200:
            raise RPCError(method, path, status, data)
        return data

    def ping(self, presence: str = "active", timeout: float = 10.0) -> dict:
        """Application-level Noise RPC heartbeat (`POST /api/ping`).
        Matches the official web client's `startHeartbeatLoop` (30s interval,
        10s timeout) to keep long-lived subscribe streams alive and detect
        half-open dead sockets promptly."""
        body = json.dumps({"presence": presence}).encode()
        raw = self.request("POST", "/api/ping", body, timeout=timeout)
        return json.loads(raw)

    # -- chat RPCs ----------------------------------------------------
    def send_message(self, session_id, text, images=None, files=None,
                     file_refs=None, in_reply_to=None, reply_to_target=None):
        """POST /chat/stream. Returns the user message_id (or None if the
        message event carried none; wait_turn can still match by text).

        images: optional list of {mime_type, filename, data (bytes) |
        data_base64 (str)} — one per image to send with the text.
        files: optional list of the same shape for non-image files
        (e.g. a PDF). Captured 2026-10-25 from the web client
        (buildAttachmentItems + chat.stream call site): with
        attachments the body carries "items" instead of "message" —
        attachment items first, then a text item if text is non-empty.
        An image item is inline base64, no upload step: {type: "image",
        mime_type, data_base64, filename}. A non-image file item with
        no upload step is {type: "file", mime_type, data_base64,
        filename} (send-format.md: "Other file item (no upload)").
        file_refs: optional list of {mime_type, filename, path} for
        files already uploaded with upload_file(): they are sent as
        {type: "file_ref", mime_type, path, filename} items, with no
        base64 in the chat body — this is the route that carries
        large files (verified 2026-10-25: a 21,000,042-byte file
        uploaded in 512 KiB /fs/write chunks, then a file_ref turn,
        was read back exactly, while the same size inline fails).
        The server stores an image to the media library and history
        then shows it as "[image:<mime> <media_library path>]" with
        metadata.message_kind "message_parts". With no attachments the
        body is exactly the old text-only shape."""
        payload = {
            "node_id": str(uuid.uuid4()),
            "capabilities": DEFAULT_CAPABILITIES,
            "session_id": session_id,
            "client_ip": self.client_ip,
            "timezone": self.timezone,
            "metadata": {"thread_is_dictation_used": False},
        }
        # Verified against the web client's chat.stream request builder.
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if reply_to_target:
            payload["reply_to_target"] = reply_to_target
        if images or files or file_refs:
            items = []
            for img in images or []:
                b64 = img.get("data_base64")
                if b64 is None:
                    data = img.get("data") or b""
                    b64 = base64.b64encode(data).decode()
                # Empty attachments are dropped, never sent: a 0-byte
                # download once reached here as an empty image item,
                # and the server rejected the whole turn with 400
                # "must provide message or non-empty items"
                # (2026-10-25). The adapter's download validation
                # should already have rejected empty downloads; this
                # is the library-level guard so no caller can send one.
                if not b64:
                    continue
                items.append({
                    "type": "image",
                    "mime_type": img.get("mime_type") or "image/jpeg",
                    "data_base64": b64,
                    "filename": img.get("filename") or "image",
                })
            for f in files or []:
                b64 = f.get("data_base64")
                if b64 is None:
                    data = f.get("data") or b""
                    b64 = base64.b64encode(data).decode()
                if not b64:
                    continue
                items.append({
                    "type": "file",
                    "mime_type": f.get("mime_type")
                    or "application/octet-stream",
                    "data_base64": b64,
                    "filename": f.get("filename") or "file",
                })
            for fr in file_refs or []:
                # Already uploaded: no base64 here, just the path.
                # A file_ref with no path is dropped, never sent.
                if not fr.get("path"):
                    continue
                items.append({
                    "type": "file_ref",
                    "mime_type": fr.get("mime_type")
                    or "application/octet-stream",
                    "path": fr["path"],
                    "filename": fr.get("filename") or "file",
                })
            if not items:
                # All attachments were empty: fall back to the plain
                # text-only shape (or raise if there is no text either),
                # never to items=[text] or to an empty items list.
                if text and text.strip():
                    payload["message"] = text
                else:
                    raise ValueError(
                        "cannot send empty turn: no text and no "
                        "non-empty images or files")
            else:
                if text and text.strip():
                    items.append({"type": "text", "text": text})
                payload["items"] = items
        else:
            payload["message"] = text
        body = json.dumps(payload, ensure_ascii=False).encode()
        data = self.request("POST", "/chat/stream", body, timeout=150)
        sent_mid = None
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict) and ev.get("message_id"):
                sent_mid = ev["message_id"]
                break
        return sent_mid

    # -- file upload (file_ref route) -------------------------------
    @staticmethod
    def _sanitize_upload_filename(filename):
        """Web-client eC(filename): keep ASCII letters, digits, dot,
        underscore and hyphen; anything else becomes "_". The path
        is only a storage key — the file_ref item still carries the
        original filename, so a mangled path does not lose the name."""
        name = filename or "file"
        if "." in name and name.rfind(".") > 0:
            base, ext = name.rsplit(".", 1)
            ext = "." + ext
        else:
            base, ext = name, ""
        def clean(s):
            return "".join(
                c if (c.isascii() and (c.isalnum() or c in "._-"))
                else "_" for c in s)
        base = clean(base) or "file"
        return base + clean(ext)

    def _fs_call(self, path, params, timeout=60):
        """One gateway fs RPC (POST path, JSON params). Returns the
        result dict. Raises MuseError when the server answers ok=false
        or the body is not the expected shape."""
        body = json.dumps(params, ensure_ascii=False).encode()
        data = self.request("POST", path, body, timeout=timeout)
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            raise MuseError(
                f"{path} returned a non-JSON body: "
                f"{data[:200]!r}")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise MuseError(
                f"{path} failed: {data[:300].decode('utf-8', 'replace')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MuseError(
                f"{path} returned no result dict: "
                f"{data[:300].decode('utf-8', 'replace')}")
        return result

    def upload_file(self, filename, data, stage_before_publish=True):
        """Upload bytes to the Muse file store, chunked like the web
        client, and return the server path for a file_ref item
        (e.g. "workspace/user/files/report.pdf").

        Chunks are 512 KiB /fs/write calls (first overwrite, rest
        append). Multi-chunk uploads with stage_before_publish go to
        a ".upload-<uuid>.part" temp file first, are stat-checked
        (kind file, size exact), then renamed to the final path —
        exactly the web client's staging, so a failed upload never
        leaves a half-written file at the final path. Single-chunk
        uploads go straight to the final path. Verified 2026-10-25:
        20-byte, 3,995,996-byte and 21,000,042-byte files all uploaded
        and stat-checked exactly, then read back via file_ref turns.
        Raises ValueError for empty data, MuseError for any fs failure.
        """
        if not data:
            raise ValueError("cannot upload an empty file")
        clean_name = self._sanitize_upload_filename(filename)
        final_path = "workspace/user/files/" + clean_name
        chunk_size = UPLOAD_CHUNK_SIZE
        nchunks = (len(data) + chunk_size - 1) // chunk_size
        staged = stage_before_publish and nchunks > 1
        if staged:
            target = ("workspace/user/files/.upload-"
                      + str(uuid.uuid4()) + ".part")
        else:
            target = final_path
        try:
            for idx in range(nchunks):
                chunk = data[idx * chunk_size:(idx + 1) * chunk_size]
                b64 = base64.b64encode(chunk).decode()
                self._fs_call("/fs/write", {
                    "path": target,
                    "data_base64": b64,
                    "overwrite": idx == 0,
                    "append": idx != 0,
                    "create_parent": idx == 0,
                })
            if not staged:
                return final_path
            stat = self._fs_call("/fs/stat", {"path": target})
            if stat.get("kind") != "file" or stat.get("size") != len(data):
                raise MuseError(
                    "uploaded file failed size validation: "
                    f"stat={stat!r}, expected size={len(data)}")
            try:
                self._fs_call("/fs/rename", {
                    "source": target,
                    "destination": final_path,
                    "create_parent": True,
                    "overwrite": True,
                })
            except MuseError:
                # Rename may have succeeded despite the error (the
                # web client handles this the same way): accept the
                # final file only if it exists with the exact size.
                final_stat = self._fs_call("/fs/stat",
                                           {"path": final_path})
                if (final_stat.get("kind") != "file"
                        or final_stat.get("size") != len(data)):
                    raise
            return final_path
        except Exception:
            if staged:
                try:
                    self._fs_call("/fs/delete", {
                        "path": target, "recursive": False})
                except Exception:
                    pass
            raise

    def read_file(self, path, chunk_size=1048576):
        """Read a file from the Muse file store, chunked like the
        web client: POST /fs/read {path, offset, len} returns
        {len, data_base64, eof, offset, path}. Verified live
        2026-10-26 on a 732,338-byte imagine_media webp (one chunk,
        RIFF/WEBP magic intact, then a len=0 read). Raises
        MuseError on any fs failure or malformed chunk.
        """
        import base64 as _base64

        data = bytearray()
        offset = 0
        for _ in range(100000):
            result = self._fs_call("/fs/read", {
                "path": path, "offset": offset, "len": chunk_size})
            n = result.get("len") or 0
            if n:
                b64 = result.get("data_base64") or ""
                try:
                    chunk = _base64.b64decode(b64)
                except Exception:
                    raise MuseError(
                        f"/fs/read returned malformed base64 for {path}")
                if len(chunk) != n:
                    raise MuseError(
                        f"/fs/read length mismatch for {path}: "
                        f"len={n} decoded={len(chunk)}")
                data += chunk
                offset += n
            if n == 0 or result.get("eof"):
                return bytes(data)
        raise MuseError(f"/fs/read did not terminate for {path}")

    def get_history(self, session_id, limit=40):
        """GET /chat/history. Returns the raw chat_events list."""
        path = (f"/chat/history?limit={limit}&transcript_mode=messages"
                f"&session_id={session_id}")
        data = self.request("GET", path, b"", timeout=30,
                            include_content_type=False)
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
            return obj["result"]["chat_events"]
        except Exception as e:
            raise MuseError(f"history parse failed: {e}") from e

    def mark_seen(self, session_id):
        """POST /api/chat/<id>/seen. Best-effort: never raises."""
        try:
            self.request("POST", f"/api/chat/{session_id}/seen", b"{}",
                         timeout=15)
        except Exception:
            pass

    def list_sessions(self, include_peer_sidechats=True):
        """GET /api/session/list. Returns the raw sessions list."""
        path = "/api/session/list"
        if include_peer_sidechats:
            path += "?include_peer_sidechats=true"
        data = self.request("GET", path, b"", timeout=30,
                            include_content_type=False)
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
            return obj["result"]["sessions"]
        except Exception as e:
            raise MuseError(f"session list parse failed: {e}")

    def session_action(self, action, session_id, **extra):
        """POST /api/session/<action> (rename/pin/unpin/archive/
        unarchive/delete). The body MUST carry method=/api/session/
        <action> — the server rejects the call without it (proven
        2026-10-26: 400 invalid_params \"`method` must be
        `/api/session/delete`\"). Returns the result object (or the
        whole body when it has no result key)."""
        path = f"/api/session/{action}"
        body = {"method": path, "session_id": session_id}
        body.update(extra)
        data = self.request("POST", path, json.dumps(body).encode(),
                            timeout=30)
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except Exception as e:
            raise MuseError(f"session {action} parse failed: {e}")
        return obj.get("result", obj)

    def delete_session(self, session_id):
        """Delete one session. See session_action for the body shape."""
        return self.session_action("delete", session_id)

    def rename_session(self, session_id, title):
        """Rename one session."""
        return self.session_action("rename", session_id, title=title)

    def pin_session(self, session_id):
        """Pin one session."""
        return self.session_action("pin", session_id)

    def unpin_session(self, session_id):
        """Unpin one session."""
        return self.session_action("unpin", session_id)

    def archive_session(self, session_id):
        """Archive one session."""
        return self.session_action("archive", session_id)

    def unarchive_session(self, session_id):
        """Unarchive one session."""
        return self.session_action("unarchive", session_id)

    # -- push (subscribe) ---------------------------------------------
    @observed("muse.subscribe")
    def subscribe(self, session_id=None, timeout=20):
        """Open a /chat/subscribe stream. Returns a PushSubscription once
        the server ACKs (subscribed:true). Raises SubscribeError on a
        non-200 status, a not_found body, an ended stream, or timeout.
        The stream stays OPEN after the ACK (no end_body) — read it with
        the returned subscription's read_events(). Note: the session must
        already exist; subscribing to a fresh session answers 404
        "session not found" (the caller falls back to polling for that
        turn, and the next turn on the now-existing session uses push)."""
        self._ensure_connected()
        self._seq += 1
        seq = self._seq
        trace_event("muse.rpc", "stream.open", connection_id=self._log_connection_id, stream_id=seq)
        payload = {"session_id": session_id} if session_id else {}
        env = http_request_envelope(
            seq, "POST", "/chat/subscribe",
            json.dumps(payload).encode())
        try:
            self._ws.send(self._send_cs.encrypt(transport_frame(env)))
        except ConnectionError as e:
            raise ConnectionLost(f"gateway send failed: {e}") from e
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # scan buf's complete lines + a parseable tail for the ACK
            parts = buf.split(b"\n")
            complete, tail = parts[:-1], parts[-1]
            candidates = [(False, raw) for raw in complete]
            if tail.strip():
                candidates.append((True, tail.strip()))
            ack = None
            for is_tail, raw in candidates:
                s = raw.strip()
                if not s:
                    continue
                res = parse_subscribe_chunk(s)
                if res == "subscribed":
                    ack = is_tail
                    break
                if res == "not_found":
                    raise SubscribeError("session not found")
            if ack is not None:
                if ack:
                    # ACK was the tail; leftover = all complete lines
                    leftover = b"\n".join(complete)
                    if leftover:
                        leftover += b"\n"
                else:
                    # ACK was a complete line; leftover = the rest + tail
                    rest = []
                    seen_ack = False
                    for raw in complete:
                        if (not seen_ack and raw.strip()
                                and parse_subscribe_chunk(raw.strip())
                                == "subscribed"):
                            seen_ack = True
                            continue
                        rest.append(raw)
                    leftover = b"\n".join(rest)
                    if leftover:
                        leftover += b"\n"
                    leftover += tail
                return PushSubscription(self, seq, leftover)
            # no ACK yet: read another frame
            try:
                _, frame = self._ws.recv(
                    timeout=min(10, max(0.1, deadline - time.time())))
            except ConnectionError as e:
                raise ConnectionLost(f"gateway connection lost: {e}") from e
            except Exception:
                continue
            try:
                pt = self._recv_cs.decrypt(frame)
            except Exception as e:
                raise ConnectionLost(f"transport decrypt failed: {e}") from e
            full = self._reasm.feed(pt)
            if full is None:
                continue
            sid, kind, detail = parse_service_frame(unwrap_service_response(full))
            trace_event("muse.rpc", "frame.receive", stream_id=sid, kind=kind, payload=detail, messages=True)
            if sid != seq:
                self._dispatch_other_stream_frame(sid, kind, detail)
                continue
            if kind == "response":
                status = detail.get("status")
                if status != 200:
                    raise SubscribeError(f"subscribe status {status}", status=status)
                if detail.get("body"):
                    buf += detail["body"]
                if detail.get("end_body"):
                    raise SubscribeError("subscribe stream ended without ACK")
            elif kind == "body_chunk":
                data = detail.get("data", b"")
                if data:
                    buf += data
                if detail.get("end_body"):
                    raise SubscribeError("subscribe stream ended without ACK")
        raise SubscribeError(f"subscribe timed out after {timeout}s")

    def wait_push(self, sub, session_id, text, sent_mid, timeout=120,
                  grace_seconds=2, on_progress=None, should_cancel=None):
        """Wait for an already-sent turn via /chat/subscribe push.
        This is the wait half of push_turn, split out so an adapter
        can send under a per-chat send lock, release the lock, and
        then wait here — a newer message cancels this wait (via
        should_cancel) without ever cancelling the send, which
        already happened. The caller must have subscribed before
        the send and reset the subscription after the send (so
        pre-send snapshots cannot prematurely mark the turn done);
        this method does NOT reset again, so assistant events that
        arrived between send return and wait start are not dropped.
        Raises PushFailed on push failure (caller falls back to
        polling, without re-sending) and TurnCancelled when
        should_cancel() returns True."""
        def log(msg):
            if on_progress is not None:
                on_progress(msg)

        tracker = PushTurnTracker(session_id)
        deadline = time.time() + timeout
        done = False
        while time.time() < deadline:
            if should_cancel is not None and should_cancel():
                raise TurnCancelled("wait cancelled by newer message")
            try:
                events = sub.read_events(timeout=10)
            except ConnectionLost as e:
                raise PushFailed(f"push stream lost: {e}", sent_mid)
            for ev in events:
                if tracker.feed(ev):
                    done = True
                    break
            if done:
                break
        if not done:
            if should_cancel is not None and should_cancel():
                raise TurnCancelled("wait cancelled by newer message")
            raise PushFailed("push turn timed out", sent_mid)
        log("push turn done; grace + history render")
        time.sleep(grace_seconds)
        try:
            hist = self.get_history(session_id)
        except (RPCError, ConnectionLost, MuseError) as e:
            raise PushFailed(f"history render failed: {e}", sent_mid)
        idx = None
        if sent_mid:
            for i, e in enumerate(hist):
                if e.get("message_id") == sent_mid:
                    idx = i
                    break
        if idx is None:
            for i in range(len(hist) - 1, -1, -1):
                e = hist[i]
                if (e.get("event_name") == "message.user"
                        and e.get("display_text") == text):
                    idx = i
                    break
        if idx is None:
            raise PushFailed("user message not in history", sent_mid)
        rendered = render.collect_turn_events(hist, idx)
        images_map = render.collect_turn_images(hist, idx)
        files_map = render.collect_turn_files(hist, idx)
        videos_map = render.collect_turn_videos(hist, idx)
        if not rendered and not images_map and not files_map \
                and not videos_map:
            raise PushFailed("no renderable reply in history", sent_mid)
        mids = set(rendered) | set(images_map) | set(files_map) \
            | set(videos_map)
        messages = [TurnMessage(
            mid,
            (rendered.get(mid) or images_map.get(mid)
             or files_map.get(mid) or videos_map.get(mid))[0],
            (rendered.get(mid) or ("", ""))[1],
            images=images_map.get(mid, (0, []))[1],
            files=files_map.get(mid, (0, []))[1],
            videos=videos_map.get(mid, (0, []))[1])
            for mid in mids]
        messages.sort(key=lambda m: m.seq)
        return TurnResult(messages=messages,
                          text="\n\n".join(m.text for m in messages if m.text),
                          user_message_id=sent_mid or "")

    def push_turn(self, session_id, text, timeout=120, grace_seconds=2,
                  on_progress=None, images=None, files=None,
                  file_refs=None):
        """One turn via /chat/subscribe push. Returns a TurnResult.
        Raises PushFailed (with sent_mid if the message was sent) on any
        push failure, so the caller can fall back to polling.
        Content still comes from one history fetch (authoritative, same
        render as polling); push only decides WHEN the turn is done, so
        latency drops from the 10s settle window to done signals + grace.
        Known limitation: frames arriving on the subscribe stream DURING
        the send request are dropped by the send's reader (it only keeps
        its own stream) — harmless here, because assistant events only
        start after the send completes, and the history fetch is the
        safety net for content either way."""
        try:
            sub = self.subscribe(session_id)
        except SubscribeError as e:
            raise PushFailed(f"subscribe failed: {e}", None)
        sent_mid = self.send_message(session_id, text, images=images,
                                     files=files, file_refs=file_refs)
        # Drop pre-send snapshots (subscribe ACK batch) so a stale
        # task.completed cannot prematurely mark this turn done.
        sub.reset()
        return self.wait_push(sub, session_id, text, sent_mid,
                              timeout=timeout, grace_seconds=grace_seconds,
                              on_progress=on_progress)

    # -- turns --------------------------------------------------------
    def wait_turn(self, session_id, text, sent_mid=None, timeout=180,
                  poll_interval=5, quiet_polls=2, on_progress=None,
                  should_cancel=None):
        """Poll history until the assistant turn settles: at least one
        renderable assistant event, and the rendered set unchanged across
        quiet_polls consecutive polls. Returns a TurnResult. Raises
        ReplyTimeout when nothing renders before the deadline, and
        TurnCancelled when should_cancel() (a newer message's wait
        cancellation flag, set by the adapter) returns True — the
        message was already sent, only this wait is dropped."""
        def log(msg):
            if on_progress is not None:
                on_progress(msg)

        t_send = time.time()
        rendered = {}
        rendered_images = {}
        rendered_files = {}
        rendered_videos = {}
        quiet = 0
        deadline = t_send + timeout
        while time.time() < deadline:
            if should_cancel is not None and should_cancel():
                raise TurnCancelled("wait cancelled by newer message")
            try:
                events = self.get_history(session_id)
            except (RPCError, ConnectionLost, MuseError) as e:
                log(f"history poll failed ({e}); retry")
                time.sleep(poll_interval)
                continue
            idx = None
            if sent_mid:
                for i, e in enumerate(events):
                    if e.get("message_id") == sent_mid:
                        idx = i
                        break
            if idx is None:
                for i in range(len(events) - 1, -1, -1):
                    e = events[i]
                    if (e.get("event_name") == "message.user"
                            and e.get("display_text") == text
                            and (e.get("occurred_at_ms") or 0)
                            >= int(t_send * 1000) - 5000):
                        idx = i
                        break
            if idx is None:
                log("user message not in history yet")
                time.sleep(poll_interval)
                continue
            new_rendered = render.collect_turn_events(events, idx)
            new_images = render.collect_turn_images(events, idx)
            new_files = render.collect_turn_files(events, idx)
            new_videos = render.collect_turn_videos(events, idx)
            if new_rendered or new_images or new_files or new_videos:
                if (new_rendered == rendered
                        and new_images == rendered_images
                        and new_files == rendered_files
                        and new_videos == rendered_videos):
                    quiet += 1
                    log(f"settled ({len(rendered)} parts, quiet {quiet}/{quiet_polls})")
                else:
                    rendered = new_rendered
                    rendered_images = new_images
                    rendered_files = new_files
                    rendered_videos = new_videos
                    quiet = 0
                    log(f"{len(rendered)} parts so far")
                if quiet >= quiet_polls:
                    break
            else:
                log(f"no reply yet ({len(events)} events)")
            time.sleep(poll_interval)
        if not rendered and not rendered_images and not rendered_files \
                and not rendered_videos:
            raise ReplyTimeout(
                f"no renderable assistant message in {timeout}s")
        mids = set(rendered) | set(rendered_images) \
            | set(rendered_files) | set(rendered_videos)
        messages = [TurnMessage(
            mid,
            (rendered.get(mid) or rendered_images.get(mid)
             or rendered_files.get(mid) or rendered_videos.get(mid))[0],
            (rendered.get(mid) or ("", ""))[1],
            images=rendered_images.get(mid, (0, []))[1],
            files=rendered_files.get(mid, (0, []))[1],
            videos=rendered_videos.get(mid, (0, []))[1])
            for mid in mids]
        messages.sort(key=lambda m: m.seq)
        return TurnResult(messages=messages,
                          text="\n\n".join(m.text for m in messages if m.text),
                          user_message_id=sent_mid or "")

    def send_and_wait(self, session_id, text,
                      on_progress=None, timeout=None, poll_interval=5,
                      quiet_polls=2, grace_seconds=2, images=None,
                      files=None, file_refs=None):
        """One full turn: send, wait for the reply, mark seen
        (best-effort). Returns a TurnResult.

        The subscribe event stream provides turn timing; a history poll
        takes over if the stream fails, so a reply is never lost."""
        try:
            turn = self.push_turn(session_id, text,
                                  timeout=timeout or 120,
                                  grace_seconds=grace_seconds,
                                  on_progress=on_progress,
                                  images=images, files=files,
                                  file_refs=file_refs)
            self.mark_seen(session_id)
            return turn
        except PushFailed as e:
            if on_progress is not None:
                on_progress(f"push failed ({e}); falling back to polling")
            sent_mid = e.sent_mid
            if sent_mid is None:
                sent_mid = self.send_message(session_id, text,
                                             images=images,
                                             files=files,
                                             file_refs=file_refs)
            turn = self.wait_turn(session_id, text, sent_mid=sent_mid,
                                  timeout=timeout or 180,
                                  poll_interval=poll_interval,
                                  quiet_polls=quiet_polls,
                                  on_progress=on_progress)
            self.mark_seen(session_id)
            return turn
