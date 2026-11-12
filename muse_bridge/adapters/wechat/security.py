"""Strict URL policy and DNS-pinned HTTPS transport for iLink.

Authenticated API and CDN requests accept only the pinned 2.4.6 host baseline.
New regional hosts require independent verification. Unauthenticated external
media fetches may opt into public HTTPS hosts, still with DNS pinning.
MockTransport is the sole DNS-free path, for explicitly injected offline tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
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
        if public:
            try:
                literal = ipaddress.ip_address(parts.hostname)
            except ValueError:
                literal = None
            if literal is not None and not _is_public(literal):
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


def _is_public(address) -> bool:
    return address.is_global and not address.is_multicast and not address.is_reserved


async def resolve_public(host: str) -> str:
    """Reject the entire DNS answer if any address is not globally routable."""
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        addresses = [record[4][0] for record in records]
        if not addresses or any(not _is_public(ipaddress.ip_address(ip)) for ip in addresses):
            raise WeChatSecurityError("WeChat DNS returned a non-public address")
        return addresses[0]
    except WeChatSecurityError:
        raise
    except (OSError, ValueError):
        raise WeChatSecurityError("WeChat DNS resolution failed") from None


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to the checked IP, retaining the real Host and TLS SNI.

    Separate pools per (hostname, IP) prevent connection reuse across TLS names.
    No proxy, environment override, redirects, TLS downgrade, or HTTP retries.
    The sni_hostname extension is supported by HTTPX's httpcore 1.x transport.
    """

    def __init__(self, *, public: bool = False) -> None:
        self._public = public
        self._pools: dict[tuple[str, str], httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        validate_url(url, cdn=request.url.host in CDN_HOSTS, public=self._public)
        host = request.url.host
        try:
            ip = await asyncio.wait_for(
                resolve_public(host), request.extensions.get("timeout", {}).get("connect", 35)
            )
        except TimeoutError:
            raise httpx.ConnectTimeout("WeChat DNS resolution timed out") from None
        key = (host, ip)
        if key not in self._pools:
            self._pools[key] = httpx.AsyncHTTPTransport(
                verify=True, trust_env=False, retries=0,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
            )
        headers = request.headers.copy()
        headers["Host"] = host
        pinned = httpx.Request(
            request.method, request.url.copy_with(host=ip),
            headers=headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._pools[key].handle_async_request(pinned)

    async def aclose(self) -> None:
        for pool in self._pools.values():
            await pool.aclose()
        self._pools.clear()
