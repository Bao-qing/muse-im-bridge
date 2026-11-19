"""Unified outbound network routing for HTTP and WebSocket transports.

Every module chooses one explicit mode:
- ``"none"``: direct connection, never reading environment proxies.
- ``"http"``: explicit HTTP(S) proxy from ``network.proxy_url``.
- ``"worker"``: Cloudflare Worker relay from ``network.worker``.
"""
from __future__ import annotations

import base64
import dataclasses
import http.client
import ssl
import urllib.request
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx


PROXY_MODES = ("none", "http", "worker")


def validate_proxy_mode(mode: Any, section: str) -> str:
    if not isinstance(mode, str) or mode not in PROXY_MODES:
        raise ValueError(f"{section}.proxy must be none, http, or worker")
    return mode


def validate_proxy_url(url: Any, section: str = "network.proxy_url") -> str:
    if not isinstance(url, str):
        raise ValueError(f"{section} must be an HTTP(S) proxy URL")
    value = url.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(f"{section} must be an HTTP(S) proxy URL")
    return value


def validate_worker_base(base: Any, section: str = "network.worker.base") -> str:
    if not isinstance(base, str):
        raise ValueError(f"{section} must be an HTTPS origin on port 443")
    value = base.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{section} must be an HTTPS origin on port 443")
    return value.rstrip("/")


def worker_url(url: str, base: str) -> str:
    """Prefix an HTTP(S)/WS(S) URL with the Worker origin without re-encoding."""
    scheme, rest = url.split("://", 1)
    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("Worker route requires HTTP(S) or WS(S)")
    root = validate_worker_base(base)
    if not root:
        raise ValueError("network.worker.base is required for Worker routing")
    if scheme in {"ws", "wss"}:
        root = "wss://" + urlsplit(root).netloc
    return f"{root}/{scheme}/{rest}"


@dataclasses.dataclass(frozen=True)
class NetworkRoute:
    mode: str = "none"
    proxy_url: str = ""
    worker_base: str = ""
    worker_http: bool = False
    worker_ws: bool = False

    @property
    def use_worker_http(self) -> bool:
        return self.mode == "worker" and self.worker_http

    @property
    def use_worker_ws(self) -> bool:
        return self.mode == "worker" and self.worker_ws

    @property
    def http_proxy(self) -> Optional[str]:
        return self.proxy_url if self.mode == "http" and self.proxy_url else None

    @property
    def ws_proxy(self) -> Optional[str]:
        return self.proxy_url if self.mode == "http" and self.proxy_url else None

    def http_url(self, url: str) -> str:
        return worker_url(url, self.worker_base) if self.use_worker_http else url

    def ws_url(self, url: str) -> str:
        return worker_url(url, self.worker_base) if self.use_worker_ws else url

    def httpx_client(
        self,
        *,
        http2: bool = False,
        follow_redirects: bool = False,
        timeout: Any = 30.0,
        transport: Any = None,
        event_hooks: Any = None,
    ) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "http2": http2,
            "follow_redirects": follow_redirects,
            "timeout": timeout,
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        elif self.http_proxy:
            kwargs["proxy"] = self.http_proxy
        if event_hooks is not None:
            kwargs["event_hooks"] = event_hooks
        return httpx.Client(**kwargs)

    def async_httpx_client(
        self,
        *,
        http2: bool = False,
        follow_redirects: bool = False,
        timeout: Any = 30.0,
        transport: Any = None,
        event_hooks: Any = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "http2": http2,
            "follow_redirects": follow_redirects,
            "timeout": timeout,
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        elif self.http_proxy:
            kwargs["proxy"] = self.http_proxy
        if event_hooks is not None:
            kwargs["event_hooks"] = event_hooks
        return httpx.AsyncClient(**kwargs)

    def async_httpx_transport(
        self,
        *,
        verify: bool = True,
        retries: int = 0,
        limits: Any = None,
    ) -> httpx.AsyncHTTPTransport:
        kwargs: dict[str, Any] = {
            "verify": verify,
            "trust_env": False,
            "retries": retries,
        }
        if self.http_proxy:
            kwargs["proxy"] = self.http_proxy
        if limits is not None:
            kwargs["limits"] = limits
        return httpx.AsyncHTTPTransport(**kwargs)

    def urllib_opener(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> urllib.request.OpenerDirector:
        proxies = {"http": self.http_proxy, "https": self.http_proxy} if self.http_proxy else {}
        handlers: list[Any] = [urllib.request.ProxyHandler(proxies)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        return urllib.request.build_opener(*handlers)

    def open_url(
        self,
        req: urllib.request.Request | str,
        *,
        timeout: float = 15,
        ssl_context: ssl.SSLContext | None = None,
    ):
        return self.urllib_opener(ssl_context=ssl_context).open(req, timeout=timeout)

    def http_connection(
        self,
        url: str,
        *,
        timeout: float = 60,
        ssl_context: ssl.SSLContext | None = None,
    ) -> tuple[http.client.HTTPConnection, str]:
        """Build an ``http.client`` connection and request target honoring this route."""
        target_url = self.http_url(url)
        parsed = urlsplit(target_url)
        host = parsed.hostname
        if not host:
            raise ValueError("URL has no hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

        if self.http_proxy:
            proxy = urlsplit(self.http_proxy)
            p_host = proxy.hostname
            p_port = proxy.port or (443 if proxy.scheme == "https" else 80)
            tunnel_headers: dict[str, str] = {}
            if proxy.username is not None:
                creds = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode("utf-8")
                tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(creds).decode("ascii")
            if parsed.scheme == "https":
                ctx = ssl_context or ssl.create_default_context()
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    p_host, p_port, timeout=timeout, context=ctx
                )
                conn.set_tunnel(host, port, headers=tunnel_headers or None)
                return conn, path
            conn = http.client.HTTPConnection(p_host, p_port, timeout=timeout)
            return conn, target_url

        if parsed.scheme == "https":
            ctx = ssl_context or ssl.create_default_context()
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx), path
        return http.client.HTTPConnection(host, port, timeout=timeout), path


DIRECT_ROUTE = NetworkRoute(mode="none")


def resolve_route(
    mode: str,
    network: Any = None,
    *,
    section: str = "network",
) -> NetworkRoute:
    """Validate and resolve a module's ``proxy`` mode against ``NetworkConfig``."""
    validate_proxy_mode(mode, section)
    if mode == "none":
        return DIRECT_ROUTE
    if mode == "http":
        proxy_url = getattr(network, "proxy_url", "") if network is not None else ""
        if not proxy_url:
            raise ValueError(f"{section}.proxy='http' requires network.proxy_url")
        return NetworkRoute(mode="http", proxy_url=proxy_url)
    worker = getattr(network, "worker", None) if network is not None else None
    base = getattr(worker, "base", "") if worker is not None else ""
    if not base:
        raise ValueError(f"{section}.proxy='worker' requires network.worker.base")
    return NetworkRoute(
        mode="worker",
        worker_base=base,
        worker_http=bool(getattr(worker, "http", True)),
        worker_ws=bool(getattr(worker, "ws", True)),
    )


def validate_app_network(config: Any) -> None:
    """Validate network routes for Muse and every enabled adapter."""
    resolve_route(config.muse.proxy, config.network, section="muse")
    for name in ("qq", "onebot", "wechat", "telegram"):
        adapter_cfg = getattr(config, name, None)
        if adapter_cfg is not None and getattr(adapter_cfg, "enabled", False):
            resolve_route(adapter_cfg.proxy, config.network, section=f"adapters.{name}")
