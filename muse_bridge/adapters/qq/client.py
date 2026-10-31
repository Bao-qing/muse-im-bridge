"""QQ Open Platform HTTP API client.

Encapsulates:
- AppAccessToken lifecycle (acquisition, cache, auto-refresh on 401).
- Gateway URL retrieval.
- C2C and Group message sending (passive replies and active pushes).
- Media file upload (images, files, videos) returning `file_info`.
- Robust CDN attachment downloading with Range resume and proxy support.
"""

from __future__ import annotations

import base64
import http.client
import json
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from ...config import constants
from ...config.constants import BLOCKED_DOWNLOAD_HOST
from ...observability import get_logger, new_id, safe_error, safe_url, trace_event

logger = get_logger(__name__)


def _trace_headers(headers):
    result = {}
    for key, value in (headers or {}).items():
        name = str(key).lower()
        # Unknown headers can contain credentials, signed URLs or user text.
        if name == "content-length" and str(value).isdigit():
            result[str(key)] = str(value)
        elif name == "content-type" and str(value) in {"application/json", "application/json; charset=utf-8",
                                                        "application/octet-stream", "image/png", "image/jpeg"}:
            result[str(key)] = str(value)
        else:
            result[str(key)] = "[REDACTED]"
    return result


def _trace_payload(value):
    # Snapshot only: never alter the object handed to the HTTP client.
    if isinstance(value, bytes):
        try:
            return _trace_payload(json.loads(value))
        except (ValueError, UnicodeError):
            return {"omitted": "unstructured", "bytes": len(value)}
    if isinstance(value, dict):
        if "file_data" in value:
            return {"binary_upload": True, "omitted": "binary_upload",
                    "encoded_length": len(value["file_data"]),
                    "file_type": value.get("file_type"),
                    "srv_send_msg": value.get("srv_send_msg")}
        return {key: "[REDACTED]" if str(key).lower() == "file_info" else _trace_payload(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_trace_payload(item) for item in value]
    return value


def _trace_http(event, request_id, started, *, method, url, payload=None,
                sensitive=False, response=None, error=None, **fields):
    # Logging must not change HTTPError handling or consume a response stream.
    try:
        if response is not None:
            fields["status"] = getattr(response, "status", getattr(response, "code", None))
            fields["headers"] = _trace_headers(getattr(response, "headers", {}))
        trace_event("qq.http", event, request_id=request_id, method=method,
                    url=safe_url(url), duration_ms=(time.perf_counter() - started) * 1000,
                    payload="[REDACTED]" if sensitive else _trace_payload(payload),
                    sensitive=sensitive, messages=True,
                    error=safe_error(error) if error is not None else None, **fields)
    except Exception:
        pass

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"


class QQClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        proxy_url: Optional[str] = None,
        download_proxy: str = "",
        user_agent: str = constants.DEFAULT_USER_AGENT,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.proxy_url = proxy_url
        self.download_proxy = download_proxy
        self.user_agent = user_agent

        self._token: str = ""
        self._token_expires_at: float = 0.0

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Obtain a valid Bot AppAccessToken, refreshing if near expiry."""
        now = time.time()
        if not force_refresh and self._token and now < self._token_expires_at - 120:
            return self._token

        url = "https://bots.qq.com/app/getAppAccessToken"
        body = json.dumps({"appId": self.app_id, "clientSecret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
        )

        request_id, started = new_id(), time.perf_counter()
        _trace_http("start", request_id, started, method="POST", url=url,
                    payload=body, sensitive=True, headers=_trace_headers(req.headers))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                _trace_http("response", request_id, started, method="POST", url=url,
                            payload=raw, sensitive=True, response=resp)
                data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            _trace_http("error", request_id, started, method="POST", url=url,
                        sensitive=True, error=e, response=e if isinstance(e, urllib.error.HTTPError) else None)
            logger.error("QQ access token acquisition failed: %s", safe_error(e))
            raise RuntimeError(f"failed to acquire QQ access token: {e}")

        token = data.get("access_token")
        expires_in = data.get("expires_in", 7200)
        if not token:
            raise RuntimeError(f"QQ API returned no access_token: {data}")

        self._token = token
        self._token_expires_at = now + int(expires_in)
        return self._token

    def get_gateway_url(self) -> str:
        token = self.get_access_token()
        url = "https://api.sgroup.qq.com/gateway/bot"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"QQBot {token}", "User-Agent": self.user_agent},
        )
        request_id, started = new_id(), time.perf_counter()
        _trace_http("start", request_id, started, method="GET", url=url,
                    headers=_trace_headers(req.headers))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                _trace_http("response", request_id, started, method="GET", url=url, payload=raw, response=resp)
                data = json.loads(raw.decode("utf-8"))
                return data.get("url") or "wss://api.sgroup.qq.com/websocket"
        except Exception as e:
            _trace_http("error", request_id, started, method="GET", url=url, error=e,
                        response=e if isinstance(e, urllib.error.HTTPError) else None)
            logger.warning("QQ gateway URL lookup failed; using fallback: %s", safe_error(e))
            return "wss://api.sgroup.qq.com/websocket"

    def _http_json(
        self,
        method: str,
        url: str,
        payload: Optional[dict] = None,
        timeout: int = 25,
    ) -> Tuple[int, dict]:
        token = self.get_access_token()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        request_id, started = new_id(), time.perf_counter()
        _trace_http("start", request_id, started, method=method, url=url, payload=payload,
                    headers=_trace_headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                _trace_http("response", request_id, started, method=method, url=url, payload=raw, response=resp)
                try:
                    return resp.status, json.loads(raw.decode("utf-8"))
                except Exception:
                    return resp.status, {"raw": raw.decode("utf-8", "ignore")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            _trace_http("error", request_id, started, method=method, url=url,
                        payload=raw, response=e, error=e)
            try:
                body_json = json.loads(raw.decode("utf-8"))
            except Exception:
                body_json = {"raw": raw.decode("utf-8", "ignore")}
            return e.code, body_json
        except Exception as e:
            _trace_http("error", request_id, started, method=method, url=url, error=e)
            logger.error("QQ HTTP request failed: %s", safe_error(e))
            raise RuntimeError(f"request to {url} failed: {e}")

    def post_with_retry(self, url: str, payload: dict, timeout: int = 25) -> Tuple[int, dict]:
        code, body = self._http_json("POST", url, payload, timeout=timeout)
        if code == 401:
            self.get_access_token(force_refresh=True)
            code, body = self._http_json("POST", url, payload, timeout=timeout)
        return code, body

    def send_message(
        self,
        target_type: str,  # "private" (C2C) or "group"
        target_id: str,
        content: Optional[str] = None,
        markdown_content: Optional[str] = None,
        media_file_info: Optional[str] = None,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
    ) -> Tuple[int, dict]:
        """Send a message to C2C or Group."""
        if target_type == "private":
            url = f"https://api.sgroup.qq.com/v2/users/{target_id}/messages"
        else:
            url = f"https://api.sgroup.qq.com/v2/groups/{target_id}/messages"

        payload: dict = {}
        if media_file_info:
            payload["msg_type"] = 7
            payload["media"] = {"file_info": media_file_info}
        elif markdown_content is not None:
            payload["msg_type"] = 2
            payload["markdown"] = {"content": markdown_content}
        else:
            payload["content"] = content or ""

        if msg_id:
            payload["msg_id"] = msg_id
            if msg_seq is not None:
                payload["msg_seq"] = msg_seq

        return self.post_with_retry(url, payload)

    def upload_media(
        self,
        target_type: str,
        target_id: str,
        data: bytes,
        file_type: int = 1,  # 1=image, 2=video, 4=file
        file_name: Optional[str] = None,
        srv_send_msg: bool = False,
    ) -> str:
        """Upload media bytes and obtain file_info."""
        if target_type == "private":
            url = f"https://api.sgroup.qq.com/v2/users/{target_id}/files"
        else:
            url = f"https://api.sgroup.qq.com/v2/groups/{target_id}/files"

        payload: dict = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode(),
            "srv_send_msg": srv_send_msg,
        }
        if file_name:
            payload["file_name"] = file_name

        code, body = self.post_with_retry(url, payload, timeout=120)
        file_info = body.get("file_info")
        if not (200 <= code < 300) or not file_info:
            raise RuntimeError(f"QQ media upload failed ({code}): {str(body)[:200]}")
        return file_info

    def proxied_url(self, url: str) -> str:
        """Rewrite blocked host through download_proxy if configured."""
        if not self.download_proxy or not url:
            return url
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc == BLOCKED_DOWNLOAD_HOST:
                stripped = url.split("://", 1)[-1]
                return self.download_proxy.rstrip("/") + "/" + stripped
        except Exception:
            pass
        return url

    def download_bytes(self, url: str, max_bytes: int = 8 * 1024 * 1024, timeout: int = 60) -> bytes:
        """Download bytes from QQ CDN or external URL without Connection: close."""
        target_url = self.proxied_url(url)
        p = urllib.parse.urlparse(target_url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        path = p.path + ("?" + p.query if p.query else "")

        ctx = ssl.create_default_context() if p.scheme == "https" else None
        conn: http.client.HTTPConnection
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

        request_id, started = new_id(), time.perf_counter()
        connection_id = new_id()
        try:
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "*/*",
            }
            _trace_http("start", request_id, started, method="GET", url=target_url,
                        connection_id=connection_id, headers=_trace_headers(headers))
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            _trace_http("response", request_id, started, method="GET", url=target_url,
                        connection_id=connection_id, response=resp)
            if resp.status not in (200, 206):
                raise RuntimeError(f"HTTP {resp.status} {resp.reason}")

            content_len_hdr = resp.getheader("Content-Length")
            expected_len = int(content_len_hdr) if content_len_hdr else None

            data = resp.read(max_bytes + 1)
            _trace_http("body", request_id, started, method="GET", url=target_url,
                        connection_id=connection_id, payload={"omitted": "binary_download", "bytes": len(data)})
            if len(data) > max_bytes:
                raise RuntimeError(f"download exceeded limit ({max_bytes} bytes)")
            if len(data) == 0:
                raise RuntimeError("empty download (0 bytes)")
            if expected_len is not None and len(data) != expected_len:
                raise RuntimeError(f"incomplete download: got {len(data)}, expected {expected_len}")

            # Verify image end markers
            if data[:2] == JPEG_START and not data.endswith(JPEG_END):
                raise RuntimeError("truncated JPEG: missing FFD9 marker")
            if data[:8] == PNG_MAGIC and not data.endswith(PNG_IEND):
                raise RuntimeError("truncated PNG: missing IEND marker")

            _trace_http("end", request_id, started, method="GET", url=target_url, connection_id=connection_id)
            return data
        except BaseException as e:
            _trace_http("error", request_id, started, method="GET", url=target_url,
                        connection_id=connection_id, error=e)
            raise
        finally:
            conn.close()
