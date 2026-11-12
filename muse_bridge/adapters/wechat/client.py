"""Asynchronous, non-retrying iLink HTTP/JSON protocol client.

Transport responses are consumed directly rather than through AsyncClient.send:
HTTPX's default request log would otherwise expose QR codes and signed CDN URLs.
All network/remote errors are replaced with secret-free protocol exceptions.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from .security import DEFAULT_API, PinnedTransport, WeChatError, WeChatSecurityError, validate_url

__all__ = [
    "WeChatClient", "WeChatError", "WeChatAPIError", "SessionCoolingError",
    "DeliveryUnknownError", "DEFAULT_API",
]


class WeChatAPIError(WeChatError):
    """API failure; code is numeric or None, never the remote errmsg."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code if type(code) is int else None
        super().__init__(f"WeChat API rejected request (code={self.code})")


class SessionCoolingError(WeChatAPIError):
    """Requests are paused, or the server invalidated this session (-14)."""

    def __init__(self) -> None:
        super().__init__(-14)


class DeliveryUnknownError(WeChatError):
    """The request may have been delivered. Do not automatically resend."""

    def __init__(self) -> None:
        super().__init__("WeChat delivery outcome is unknown; do not blindly retry")


class WeChatClient:
    def __init__(
        self, token: str = "", base_url: str = DEFAULT_API, *,
        channel_version: str = "2.4.6", route_tag: str = "", transport=None,
        cooling_until: Callable[[], float] | None = None,
        on_session_invalid: Callable[[], None] | None = None, timeout: float = 35,
    ) -> None:
        self.base_url = validate_url(base_url, base=True)
        try:
            parts = [int(part) for part in channel_version.split(".")]
            if len(parts) != 3 or any(p < 0 for p in parts):
                raise ValueError
        except (AttributeError, ValueError):
            raise WeChatError("Invalid WeChat channel version") from None
        if not isinstance(token, str) or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise WeChatError("Invalid WeChat token encoding")
        if not isinstance(route_tag, str) or any(ord(c) < 32 or ord(c) > 126 for c in route_tag):
            raise WeChatError("Invalid WeChat route tag encoding")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise WeChatSecurityError("Only MockTransport test injection is supported")
        self._token = token
        self.channel_version = channel_version
        self._version = str(((parts[0] & 255) << 16) | ((parts[1] & 255) << 8) | (parts[2] & 255))
        self._route_tag = route_tag
        self._cooling_until = cooling_until
        self._on_session_invalid = on_session_invalid
        self._invalid_until = 0.0
        self._timeout = timeout
        # MockTransport skips real DNS only; the URL policy still applies.
        self._api_transport = transport if transport is not None else PinnedTransport()
        self._cdn_transport = transport if transport is not None else PinnedTransport()
        self._public_transport = transport if transport is not None else PinnedTransport(public=True)

    def _headers(self, *, post: bool, authenticated: bool) -> dict[str, str]:
        headers = {
            "iLink-App-Id": "bot", "iLink-App-ClientVersion": self._version,
            "Accept-Encoding": "identity",
        }
        if self._route_tag:
            headers["SKRouteTag"] = self._route_tag
        if post:
            headers.update({
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            })
        if authenticated and self._token:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    def _check_cooling(self) -> None:
        now = time.time()
        if self._invalid_until > now or (
            self._cooling_until is not None and self._cooling_until() > now
        ):
            raise SessionCoolingError()

    def _check_result(self, result: dict, *, sending: bool = False) -> None:
        codes = [result[k] for k in ("ret", "errcode") if k in result]
        if any(type(code) is int and code == -14 for code in codes):
            # Establish an account-wide in-process guard BEFORE persistence.
            # Even if state.pause cannot write, this client stays quiet an hour.
            self._invalid_until = max(self._invalid_until, time.time() + 3600)
            if self._on_session_invalid is not None:
                try:
                    self._on_session_invalid()
                except Exception:
                    # Caller-side persistence failures must not mask invalidity
                    # or expose callback exception text/credentials.
                    raise SessionCoolingError() from None
            raise SessionCoolingError()
        for code in codes:
            if type(code) is not int or code != 0:
                raise WeChatAPIError(code)
        if sending and (type(result.get("ret")) is not int or result["ret"] != 0):
            raise DeliveryUnknownError()

    @staticmethod
    async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
        # We request identity encoding and reject compressed responses instead
        # of allocating unbounded decompression buffers for hostile payloads.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise WeChatSecurityError("Unexpected compressed WeChat response")
        length = response.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise WeChatSecurityError("Invalid WeChat response size") from None
            if size < 0 or size > max_bytes:
                raise WeChatSecurityError("WeChat response exceeds byte limit")
        chunks = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(chunks) + len(chunk) > max_bytes:
                raise WeChatSecurityError("WeChat response exceeds byte limit")
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        try:
            await response.aclose()
        except httpx.HTTPError:
            # Closing must not mask the sanitized error or turn a confirmed
            # delivery into an apparently retryable transport failure.
            pass

    async def _request(
        self, method: str, endpoint: str, *, body: Mapping[str, Any] | None = None,
        authenticated: bool = True, business: bool = True, base_url: str | None = None,
        params: Mapping[str, Any] | None = None, read_timeout: float | None = None,
        sending: bool = False,
    ) -> dict:
        self._check_cooling()
        base = validate_url(base_url or self.base_url, base=True)
        payload = dict(body or {})
        if business and method == "POST":
            payload["base_info"] = {
                "channel_version": self.channel_version,
                "bot_agent": "MuseBridge/0.1.0 (python)",
            }
        request = httpx.Request(
            method, base + "/ilink/bot/" + endpoint, params=params,
            headers=self._headers(post=method == "POST", authenticated=authenticated),
            json=payload if method == "POST" else None,
            extensions={"timeout": httpx.Timeout(
                self._timeout, read=read_timeout if read_timeout is not None else self._timeout
            ).as_dict()},
        )
        response = None
        try:
            response = await self._api_transport.handle_async_request(request)
            data = await self._read_limited(response, 8 * 1024 * 1024)
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError):
                if sending:
                    raise DeliveryUnknownError() from None
                raise WeChatError("Invalid WeChat JSON response") from None
            if not isinstance(result, dict):
                if sending:
                    raise DeliveryUnknownError()
                raise WeChatError("Invalid WeChat response shape")
            # Some gateways return structured API errors with non-2xx status.
            # A -14 still invalidates the session and must reach the callback.
            self._check_result(result, sending=sending and 200 <= response.status_code < 300)
            if not 200 <= response.status_code < 300:
                if sending:
                    raise DeliveryUnknownError()
                raise WeChatError("WeChat HTTP request failed")
            return result
        except httpx.ReadTimeout:
            if sending:
                raise DeliveryUnknownError() from None
            if endpoint == "getupdates":
                return {"ret": 0, "msgs": [], "get_updates_buf": payload.get("get_updates_buf", "")}
            if endpoint == "get_qrcode_status":
                return {"status": "wait"}
            raise WeChatError("WeChat request timed out") from None
        except httpx.TransportError:
            if sending:
                raise DeliveryUnknownError() from None
            raise WeChatError("WeChat transport request failed") from None
        finally:
            if response is not None:
                await self._close_response(response)

    async def get_updates(self, cursor: str, timeout_ms: int = 35000) -> dict:
        return await self._request("POST", "getupdates", body={"get_updates_buf": cursor},
                                   read_timeout=max(1, timeout_ms) / 1000)

    async def send_message(self, msg: Mapping[str, Any]) -> dict:
        return await self._request("POST", "sendmessage", body={"msg": dict(msg)}, sending=True)

    async def get_upload_url(self, body: Mapping[str, Any]) -> dict:
        return await self._request("POST", "getuploadurl", body=body)

    async def get_config(self, user_id: str, context_token: str | None = None) -> dict:
        body = {"ilink_user_id": user_id}
        if context_token is not None:
            body["context_token"] = context_token
        return await self._request("POST", "getconfig", body=body)

    async def send_typing(self, user_id: str, ticket: str, status: int) -> dict:
        return await self._request("POST", "sendtyping", body={
            "ilink_user_id": user_id, "typing_ticket": ticket, "status": status,
        })

    async def notify(self, start: bool) -> dict:
        return await self._request("POST", "msg/notifystart" if start else "msg/notifystop")

    async def get_qrcode(self, local_tokens: Sequence[str]) -> dict:
        return await self._request(
            "POST", "get_bot_qrcode", authenticated=False, business=False,
            base_url=DEFAULT_API, params={"bot_type": "3"},
            body={"local_token_list": list(local_tokens)[:10]},
        )

    async def get_qrcode_status(
        self, qrcode: str, verify_code: str = "", base_url: str | None = None,
    ) -> dict:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        return await self._request(
            "GET", "get_qrcode_status", params=params, authenticated=False,
            business=False, base_url=base_url or DEFAULT_API,
        )

    async def cdn_download(self, url: str, max_bytes: int) -> bytes:
        """Bounded unauthenticated CDN fetch; never follows HTTP redirects."""
        return await self._download(url, max_bytes, public=False)

    async def download_public(self, url: str, max_bytes: int) -> bytes:
        """Fetch an external HTTPS media source, unauthenticated and DNS-pinned."""
        return await self._download(url, max_bytes, public=True)

    async def _download(self, url: str, max_bytes: int, *, public: bool) -> bytes:
        self._check_cooling()
        validate_url(url, cdn=not public, public=public)
        if type(max_bytes) is not int or max_bytes < 0:
            raise WeChatSecurityError("Invalid WeChat download byte limit")
        response = None
        try:
            request = httpx.Request("GET", url, headers={"Accept-Encoding": "identity"},
                                    extensions={"timeout": httpx.Timeout(self._timeout).as_dict()})
            transport = self._public_transport if public else self._cdn_transport
            response = await transport.handle_async_request(request)
            if not 200 <= response.status_code < 300:
                raise WeChatError("WeChat media download failed")
            return await self._read_limited(response, max_bytes)
        except httpx.TransportError:
            raise WeChatError("WeChat CDN download transport failed") from None
        finally:
            if response is not None:
                await self._close_response(response)

    async def cdn_upload(self, url: str, data: bytes) -> str:
        """Upload ciphertext and return x-encrypted-param (no business headers)."""
        self._check_cooling()
        validate_url(url, cdn=True)
        response = None
        try:
            request = httpx.Request("POST", url, content=data, headers={
                "Content-Type": "application/octet-stream", "Accept-Encoding": "identity",
            }, extensions={"timeout": httpx.Timeout(self._timeout).as_dict()})
            response = await self._cdn_transport.handle_async_request(request)
            if not 200 <= response.status_code < 300:
                raise WeChatError("WeChat CDN upload failed")
            param = response.headers.get("x-encrypted-param")
            if not param:
                raise WeChatError("WeChat CDN upload omitted encrypted parameter")
            return param
        except httpx.TransportError:
            raise DeliveryUnknownError() from None
        finally:
            if response is not None:
                await self._close_response(response)

    async def aclose(self) -> None:
        await self._api_transport.aclose()
        if self._cdn_transport is not self._api_transport:
            await self._cdn_transport.aclose()
        if self._public_transport is not self._api_transport:
            await self._public_transport.aclose()
