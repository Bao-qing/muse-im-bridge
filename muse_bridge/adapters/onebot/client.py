"""OneBot v11 (NapCat) HTTP API client.

Strictly follows the OneBot v11 specification using Array message segments
for rich text, formatting, and clean escaping. Every action goes through
``call``, which raises ``OneBotAPIError`` when the server answers with a
failed status, so a rejected send is never mistaken for a delivered one.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ...observability import get_logger, new_id, safe_error, safe_url, trace_event

logger = get_logger(__name__)

# Cloudflare in front of NapCat rejects the default Python-urllib agent (error 1010).
USER_AGENT = "muse-im-bridge/0.1 (OneBot v11)"


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


class OneBotAPIError(RuntimeError):
    """The OneBot server processed the action and refused it (``retcode != 0``)."""

    def __init__(self, action: str, retcode, message: str = ""):
        self.action = action
        self.retcode = retcode
        # NapCat may put a stack trace into "message"; keep the first line only.
        self.detail = (str(message or "").splitlines() or [""])[0][:200]
        super().__init__(f"OneBot {action} failed: retcode={retcode} {self.detail}".rstrip())


def _check(action: str, res: Any) -> dict:
    if not isinstance(res, dict):
        raise OneBotAPIError(action, None, "malformed response")
    if res.get("retcode") != 0 or res.get("status") == "failed":
        raise OneBotAPIError(action, res.get("retcode"), res.get("wording") or res.get("message") or "")
    return res


def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def media_segment(kind: str, data: bytes) -> dict:
    """``image`` / ``video`` segment carrying the bytes inline (``base64://``)."""
    return {"type": kind, "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}}


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
            "User-Agent": USER_AGENT,
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

    def call(self, action: str, timeout: int = 15, **params) -> Any:
        """Run one action and return its ``data``; raise ``OneBotAPIError`` if refused."""
        return _check(action, self._post(f"/{action}", params, timeout=timeout)).get("data")

    # -- identity -------------------------------------------------------
    def get_login_info(self) -> dict:
        return self.call("get_login_info") or {}

    # -- sending ----------------------------------------------------------
    def send_msg(self, target_type: str, target_id, segments: List[dict], timeout: int = 30) -> dict:
        """Send segments to ``("group"|"private", id)``; returns ``{"message_id": ...}``."""
        if target_type == "group":
            data = self.call("send_group_msg", timeout=timeout, group_id=int(target_id), message=segments)
        else:
            data = self.call("send_private_msg", timeout=timeout, user_id=int(target_id), message=segments)
        return data if isinstance(data, dict) else {}

    def send_qzone_msg(
        self, content: str, images: Optional[List[str]] = None,
        ugc_right: Optional[int] = None, target_uins: Optional[List[int]] = None,
        timeout: int = 30,
    ) -> dict:
        """Publish Qzone text with optional image sources and visibility settings.

        Image sources are nonempty strings (file://, http(s):// or base64://).
        Visibility: 1 all, 4 friends, 16 allow subset, 64 self, 128 deny subset.
        Returns validated ``{"tid": str, "time": int}`` response data.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a nonempty string")
        params: Dict[str, Any] = {"content": content}
        if images is not None:
            if not isinstance(images, list) or any(
                not isinstance(image, str) or not image.strip() for image in images
            ):
                raise ValueError("images must be a list of nonempty strings")
            params["images"] = images
        if ugc_right is not None:
            if type(ugc_right) is not int or ugc_right not in {1, 4, 16, 64, 128}:
                raise ValueError("ugc_right must be one of 1, 4, 16, 64, 128")
            params["ugc_right"] = ugc_right
        if target_uins is not None:
            if not isinstance(target_uins, list) or any(
                type(uin) is not int or uin <= 0 for uin in target_uins
            ):
                raise ValueError("target_uins must be a list of positive integers")
            params["target_uins"] = target_uins
        data = self.call("send_qzone_msg", timeout=timeout, **params)
        if (not isinstance(data, dict)
                or not isinstance(data.get("tid"), str) or not data["tid"].strip()
                or type(data.get("time")) is not int):
            raise OneBotAPIError("send_qzone_msg", 0, "malformed success response: expected tid and time")
        return data

    def upload_file(self, target_type: str, target_id, data: bytes, name: str, timeout: int = 120) -> None:
        """Upload a file into the group file area / private chat."""
        file = "base64://" + base64.b64encode(data).decode("ascii")
        if target_type == "group":
            self.call("upload_group_file", timeout=timeout, group_id=int(target_id), file=file, name=name)
        else:
            self.call("upload_private_file", timeout=timeout, user_id=int(target_id), file=file, name=name)

    @staticmethod
    def _segments(text: str, image_urls: Optional[List[str]]) -> List[dict]:
        segments: List[dict] = []
        if text:
            segments.append(text_segment(text))
        for url in image_urls or []:
            segments.append({"type": "image", "data": {"file": url}})
        return segments

    def send_group_msg(self, group_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a group message using Array segments format; returns the raw response."""
        payload = {"group_id": int(group_id), "message": self._segments(text, image_urls)}
        return _check("send_group_msg", self._post("/send_group_msg", payload))

    def send_private_msg(self, user_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a private message using Array segments format; returns the raw response."""
        payload = {"user_id": int(user_id), "message": self._segments(text, image_urls)}
        return _check("send_private_msg", self._post("/send_private_msg", payload))

    # -- receiving media ----------------------------------------------------
    def file_source(self, file_id: str, group_id=None) -> Dict[str, str]:
        """Where the bytes of a received ``file`` segment can be read.

        ``{"base64": ...}`` or ``{"url": "http..."}``; empty when NapCat only
        knows a path on its own host, which this process cannot read.
        """
        try:
            data = self.call("get_file", file_id=file_id) or {}
            if data.get("base64"):
                return {"base64": data["base64"]}
            if str(data.get("url") or "").startswith(("http://", "https://")):
                return {"url": data["url"]}
        except OneBotAPIError as e:
            logger.debug("get_file unavailable: %s", safe_error(e))
        if group_id is not None:
            try:
                data = self.call("get_group_file_url", group_id=int(group_id), file_id=file_id, busid=102) or {}
                if str(data.get("url") or "").startswith(("http://", "https://")):
                    return {"url": data["url"]}
            except OneBotAPIError as e:
                logger.debug("get_group_file_url unavailable: %s", safe_error(e))
        return {}

    @staticmethod
    def download_bytes(url: str, max_bytes: int, timeout: int = 60) -> bytes:
        """Download media (QQ CDN URLs carry their own rkey) with a size cap."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ValueError(f"超过 {max_bytes // (1024 * 1024)}MB 上限")
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"超过 {max_bytes // (1024 * 1024)}MB 上限")
        return data

    def get_group_msg_history(self, group_id: int, count: int = 10) -> List[dict]:
        res = self._post("/get_group_msg_history", {"group_id": int(group_id), "count": count})
        if res.get("retcode") == 0:
            return res.get("data", {}).get("messages", [])
        return []
