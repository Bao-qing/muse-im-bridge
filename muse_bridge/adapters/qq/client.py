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

from ...config.constants import BLOCKED_DOWNLOAD_HOST

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
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.proxy_url = proxy_url
        self.download_proxy = download_proxy

        self._token: str = ""
        self._token_expires_at: float = 0.0

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Obtain a valid Bot AppAccessToken, refreshing if near expiry."""
        now = time.time()
        if not force_refresh and self._token and now < self._token_expires_at - 120:
            return self._token

        url = "https://bots.qq.com/app/getAppAccessToken"
        body = json.dumps({"appId": self.app_id, "clientSecret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
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
        req = urllib.request.Request(url, headers={"Authorization": f"QQBot {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("url") or "wss://api.sgroup.qq.com/websocket"
        except Exception:
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
        }
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode("utf-8"))
                except Exception:
                    return resp.status, {"raw": raw.decode("utf-8", "ignore")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body_json = json.loads(raw.decode("utf-8"))
            except Exception:
                body_json = {"raw": raw.decode("utf-8", "ignore")}
            return e.code, body_json
        except Exception as e:
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

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
            }
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                raise RuntimeError(f"HTTP {resp.status} {resp.reason}")

            content_len_hdr = resp.getheader("Content-Length")
            expected_len = int(content_len_hdr) if content_len_hdr else None

            data = resp.read(max_bytes + 1)
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

            return data
        finally:
            conn.close()
