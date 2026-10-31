"""OneBot v11 (NapCat) HTTP API client.

Strictly follows the OneBot v11 specification using Array message segments
for rich text, formatting, and clean escaping.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.request
from typing import Any, Dict, List, Optional

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


def _trace_http(event, request_id, started, *, url, payload=None, response=None, error=None, **fields):
    try:
        if response is not None:
            fields["status"] = getattr(response, "status", getattr(response, "code", None))
            fields["headers"] = _trace_headers(getattr(response, "headers", {}))
        if isinstance(payload, bytes):
            try:
                payload = json.loads(payload)
            except (ValueError, UnicodeError):
                payload = {"omitted": "unstructured", "bytes": len(payload)}
        trace_event("onebot.http", event, request_id=request_id, method="POST",
                    url=safe_url(url), duration_ms=(time.perf_counter() - started) * 1000,
                    payload=payload, messages=True,
                    error=safe_error(error) if error is not None else None, **fields)
    except Exception:
        pass


class OneBotClient:
    def __init__(self, base_url: str, access_token: str):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _post(self, path: str, payload: dict, timeout: int = 15) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        req = urllib.request.Request(url, data=body, headers=headers)
        request_id, started = new_id(), time.perf_counter()
        _trace_http("start", request_id, started, url=url, payload=payload,
                    headers=_trace_headers(headers))
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                raw = resp.read()
                _trace_http("response", request_id, started, url=url, payload=raw, response=resp)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            # Unlike QQ, OneBot deliberately does not read an HTTPError body.
            _trace_http("error", request_id, started, url=url, error=e,
                        response=e if isinstance(e, urllib.error.HTTPError) else None)
            logger.error("OneBot HTTP request failed: %s", safe_error(e))
            raise RuntimeError(f"OneBot API request to {path} failed: {e}")

    def send_group_msg(self, group_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a group message using Array segments format."""
        segments: List[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for url in image_urls or []:
            segments.append({"type": "image", "data": {"file": url}})

        return self._post("/send_group_msg", {"group_id": int(group_id), "message": segments})

    def send_private_msg(self, user_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a private message using Array segments format."""
        segments: List[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for url in image_urls or []:
            segments.append({"type": "image", "data": {"file": url}})

        return self._post("/send_private_msg", {"user_id": int(user_id), "message": segments})

    def get_group_msg_history(self, group_id: int, count: int = 10) -> List[dict]:
        res = self._post("/get_group_msg_history", {"group_id": int(group_id), "count": count})
        if res.get("retcode") == 0:
            return res.get("data", {}).get("messages", [])
        return []
