"""URL validation and HTTPS transport for iLink.

Authenticated API and CDN requests accept only the pinned 2.4.6 host baseline
and use normal HTTPS/DNS/proxy resolution.
MockTransport is the sole URL/transport stub, for explicitly injected offline tests.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import httpx

API_HOSTS = frozenset({"ilinkai.weixin.qq.com"})
CDN_HOSTS = frozenset({"novac2c.cdn.weixin.qq.com"})
DEFAULT_API = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN = "https://novac2c.cdn.weixin.qq.com/c2c"


class WeChatError(Exception):
    """Base for sanitized protocol errors (never include remote messages)."""


class WeChatSecurityError(WeChatError):
    """A URL, DNS answer or media payload violated the security policy."""


def validate_url(url: str, *, cdn: bool = False, base: bool = False, public: bool = False) -> str:
    """Validate without DNS. public=True is only for unauthenticated source fetches."""
    try:
        if not isinstance(url, str) or not url or any(
            ord(c) <= 32 or ord(c) >= 127 for c in url
        ) or "\\" in url:
            raise ValueError
        parts = urlsplit(url)
        hosts = CDN_HOSTS if cdn else API_HOSTS
        if (parts.scheme != "https" or not parts.hostname
                or (not public and parts.hostname not in hosts)
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment
                or parts.netloc.lower() not in {
                    parts.hostname, parts.hostname + ":443"
                }):
            raise ValueError
        if base and (parts.path not in ("", "/") or parts.query):
            raise ValueError
        # Let HTTPX reject any authority syntax its own parser cannot safely
        # represent, but never propagate its URL-containing error messages.
        httpx.URL(url)
    except (ValueError, TypeError, AttributeError, httpx.InvalidURL):
        # A fingerprint is useful for reviewing a new regional host, without
        # reflecting attacker-controlled strings or URL credentials into logs.
        fingerprint = hashlib.sha256(str(url).encode()).hexdigest()[:12]
        raise WeChatSecurityError(
            f"Untrusted WeChat URL (fingerprint={fingerprint})"
        ) from None
    if base:
        return parts._replace(scheme="https", netloc=parts.hostname).geturl().rstrip("/")
    return url


def redirect_base(host: str) -> str:
    """redirect_host is a bare authority, never an arbitrary URL/path."""
    if not isinstance(host, str) or any(c in host for c in "/?#@\\"):
        raise WeChatSecurityError("Invalid WeChat redirect authority")
    return validate_url("https://" + host, base=True)


class PinnedTransport(httpx.AsyncBaseTransport):
    """Validated HTTPS transport without redirects or HTTP retries."""

    def __init__(self, *, public: bool = False) -> None:
        self._public = public
        self._pool: httpx.AsyncHTTPTransport | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        validate_url(str(request.url), cdn=request.url.host in CDN_HOSTS, public=self._public)
        if self._pool is None:
            self._pool = httpx.AsyncHTTPTransport(
                verify=True, trust_env=True, retries=0,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
            )
        return await self._pool.handle_async_request(request)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
