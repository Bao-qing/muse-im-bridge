"""Inner protocol: protobuf wire helpers, the ServiceFrame envelope,
NoiseTransportFrame chunking, and response parsing.

This module knows nothing about chat semantics, sessions, or adapters.
It only encodes requests the way the page does and decodes what the
gateway returns, using the field numbers verified against live bytes
on 2026-10-24 (notably: ServiceFrame response=3, body_chunk=4, and
server responses wrapped as ServiceResponse{ payload=1 }).
"""
import os
import uuid


def pb_varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def pb_bytes(field: int, data: bytes) -> bytes:
    return pb_varint((field << 3) | 2) + pb_varint(len(data)) + data


def pb_field_varint(field: int, v: int) -> bytes:
    return pb_varint((field << 3) | 0) + pb_varint(v)


def _read_varint(data: bytes, pos: int):
    result = 0
    shift = 0
    n = 0
    while True:
        b = data[pos + n]
        result |= (b & 0x7F) << shift
        n += 1
        if not (b & 0x80):
            return result, n
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def pb_parse(data: bytes):
    """Parse into {field: [(wire, value)]}; value is int for varint, bytes for LEN."""
    out = {}
    pos = 0
    while pos < len(data):
        tag, n = _read_varint(data, pos)
        pos += n
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, n = _read_varint(data, pos)
            pos += n
        elif wire == 2:
            ln, n = _read_varint(data, pos)
            pos += n
            val = data[pos:pos + ln]
            pos += ln
        elif wire == 5:
            val = data[pos:pos + 4]
            pos += 4
        elif wire == 1:
            val = data[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"wire {wire}")
        out.setdefault(field, []).append((wire, val))
    return out


def http_request_envelope(seq: int, method: str, path: str, body: bytes = b"",
                          extra_headers=(), include_content_type: bool = True,
                          service: str = "daemon") -> bytes:
    """ServiceRequest{ payload: ServiceFrame{ stream_id: seq,
    kind.request: ApplicationRequest{ verb, path, headers[], body, end_body } } }.
    Service enum verified in the web client's module 545050: daemon=0,
    sentinel=1, vault=2, authd=3, ssh_access=4. GET requests on the page
    carry no Content-Type header, so callers can omit it."""
    headers = []
    if include_content_type:
        headers.append(("Content-Type", "application/json"))
    headers += [
        ("x-request-id", str(uuid.uuid4())),
        ("x-app-id", "hatch-web"),
        ("Accept-Language", "zh-CN"),
        ("Accept-Encoding", "identity"),
    ] + list(extra_headers)
    req = pb_bytes(1, method.encode()) + pb_bytes(2, path.encode())
    for k, v in headers:
        req += pb_bytes(3, pb_bytes(1, k.encode()) + pb_bytes(2, v.encode()))
    if body:
        req += pb_bytes(4, body)
    req += pb_field_varint(5, 1)  # end_body = true
    frame = pb_field_varint(1, seq) + pb_bytes(2, req)
    services = {"daemon": 0, "sentinel": 1, "vault": 2, "authd": 3, "ssh_access": 4}
    if service not in services:
        raise ValueError(f"Unknown Noise service: {service}")
    selector = pb_field_varint(1, services[service]) if services[service] else b""
    return selector + pb_bytes(2, frame)


def transport_frame(payload: bytes, chunk_id=None, chunk_index=0,
                    total_chunks=1) -> bytes:
    """NoiseTransportFrame{chunk_id: rand SIGNED int64, chunk_index,
    total_chunks, payload}. The page's decoder rejects chunkIds outside
    int64 range, and a 64-bit value with the high bit set encodes as a
    10-byte varint that the server reads as negative -> session killed."""
    if chunk_id is None:
        raw = int.from_bytes(os.urandom(8), "big")
        chunk_id = raw if raw < (1 << 63) else raw - (1 << 64)
        # protobuf int64 varint encoding: negative values always take 10 bytes.
        # Encode the signed value as its unsigned 64-bit two's-complement form so
        # the wire varint matches what protobufjs emits for a negative int64.
        chunk_id = chunk_id & ((1 << 64) - 1)
    return (
        pb_field_varint(1, chunk_id)
        + pb_field_varint(2, chunk_index)
        + pb_field_varint(3, total_chunks)
        + pb_bytes(4, payload)
    )


# Max bytes per transport chunk. The library used to send every request
# as ONE chunk (total_chunks=1) regardless of size; a ~300KB base64 image
# body sent that way made the server close the TCP connection mid-turn
# ("socket closed", 2026-10-25, while tiny 1x1 PNG and text turns
# worked). The web client splits large payloads, and the server's
# reassembler joins chunks by chunk_id, so split at 16KB: small enough
# to stay under any per-chunk limit, large enough that a 300KB image is
# only ~19 frames.
TRANSPORT_CHUNK_SIZE = 16 * 1024


def chunked_transport_frames(payload: bytes):
    """Split a request payload into transport frames (one chunk_id,
    chunk_index 0..n-1, total_chunks n). Small payloads stay one frame,
    exactly as before."""
    if len(payload) <= TRANSPORT_CHUNK_SIZE:
        return [transport_frame(payload)]
    raw = int.from_bytes(os.urandom(8), "big")
    chunk_id = raw if raw < (1 << 63) else raw - (1 << 64)
    chunk_id = chunk_id & ((1 << 64) - 1)
    parts = [payload[i:i + TRANSPORT_CHUNK_SIZE]
             for i in range(0, len(payload), TRANSPORT_CHUNK_SIZE)]
    return [transport_frame(part, chunk_id=chunk_id, chunk_index=i,
                            total_chunks=len(parts))
            for i, part in enumerate(parts)]


def parse_service_frame(payload: bytes):
    """Parse a ServiceFrame payload -> (stream_id, kind, detail dict)."""
    f = pb_parse(payload)
    stream_id = f.get(1, [(0, None)])[0][1]
    if 2 in f:
        return stream_id, "request", {"raw": f[2][0][1]}
    if 3 in f:
        r = pb_parse(f[3][0][1])
        headers = []
        for _, hb in r.get(2, []):
            h = pb_parse(hb)
            k = h.get(1, [(2, b"")])[0][1]
            v = h.get(2, [(2, b"")])[0][1]
            headers.append((k.decode(errors="replace"), v.decode(errors="replace")))
        return stream_id, "response", {
            "status": r.get(1, [(0, None)])[0][1],
            "headers": headers,
            "body": r.get(3, [(2, b"")])[0][1],
            "end_body": bool(r.get(4, [(0, 0)])[0][1]),
        }
    if 4 in f:
        c = pb_parse(f[4][0][1])
        return stream_id, "body_chunk", {
            "data": c.get(1, [(2, b"")])[0][1],
            "end_body": bool(c.get(2, [(0, 0)])[0][1]),
        }
    if 8 in f:
        return stream_id, "reset", {"raw": f[8][0][1]}
    return stream_id, "unknown", {}


def unwrap_service_response(full: bytes) -> bytes:
    """Server responses arrive as ServiceResponse{ payload=1 } wrapping the
    ServiceFrame. The request direction instead uses ServiceRequest{
    payload=2 }. Detect the wrapper by field 1 wire type: LEN bytes ->
    unwrap; varint -> already a ServiceFrame."""
    f = pb_parse(full)
    if 1 in f and f[1][0][0] == 2:
        return f[1][0][1]
    return full


class ChunkReassembler:
    """Reassemble NoiseTransportFrame chunks by chunk_id into ServiceFrames."""

    def __init__(self):
        self.pending = {}

    def feed(self, pt: bytes):
        """Feed one decrypted transport frame; return complete ServiceFrame bytes or None."""
        f = pb_parse(pt)
        cid = f.get(1, [(0, 0)])[0][1]
        idx = f.get(2, [(0, 0)])[0][1]
        total = f.get(3, [(0, 1)])[0][1]
        payload = f.get(4, [(2, b"")])[0][1]
        if total <= 1:
            return payload
        slot = self.pending.setdefault(cid, {"total": total, "parts": {}})
        slot["parts"][idx] = payload
        if len(slot["parts"]) >= total:
            out = b"".join(slot["parts"][i] for i in range(total))
            del self.pending[cid]
            return out
        return None
