"""Muse-specific URL, cookie, client-IP, and redirect guards on top of the shared network layer."""
from __future__ import annotations

import ipaddress
import threading
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ..config import constants
from ..network import DIRECT_ROUTE, NetworkRoute
from ..observability import http_request


def http_base(base_url: str, route: NetworkRoute | None = None) -> str:
    active = route or DIRECT_ROUTE
    return active.http_url(base_url.rstrip("/"))


def auth_url(base_url: str, route: NetworkRoute | None = None) -> str:
    real = base_url.rstrip("/")
    auth = f"https://auth.{real.split('://')[-1]}/aymh/"
    active = route or DIRECT_ROUTE
    if not active.use_worker_http:
        return f"{auth}?origin={real}"
    return f"{active.http_url(auth)}?origin={real}"


def cookie_domain(domain: str, route: NetworkRoute | None = None) -> str | None:
    """Accept only Muse domains, plus the active Worker proxy hostname."""
    domain = domain.lower().lstrip(".")
    if domain == "muse.ai" or domain.endswith(".muse.ai"):
        return "muse.ai"
    if route and route.mode == "worker" and route.worker_base:
        if domain == urlsplit(route.worker_base).hostname:
            return "muse.ai"
    return None


def muse_http_hooks(route: NetworkRoute | None = None) -> dict | None:
    """Reject redirects that escape the configured Worker relay."""
    active = route or DIRECT_ROUTE
    if not active.use_worker_http:
        return None
    base = urlsplit(active.worker_base)

    def require_worker(request):
        url = request.url
        if (
            url.scheme != "https"
            or url.host != base.hostname
            or url.port not in (None, 443)
            or not any(
                url.path.startswith(f"/{scheme}/{host}/")
                for scheme in ("https", "http")
                for host in ("muse.ai", "auth.muse.ai")
            )
        ):
            raise RuntimeError("Muse Worker redirect escaped the configured relay")

    return {"request": [require_worker]}


def muse_http_client(
    route: NetworkRoute | None = None,
    *,
    timeout: float = 20.0,
    follow_redirects: bool = False,
    transport=None,
) -> httpx.Client:
    active = route or DIRECT_ROUTE
    return active.httpx_client(
        http2=True,
        timeout=timeout,
        follow_redirects=follow_redirects,
        transport=transport,
        event_hooks=muse_http_hooks(active),
    )


def fetch_client_ip(
    cookie: str,
    route: NetworkRoute | None = None,
    *,
    base_url: str = "https://muse.ai",
    timeout: float = 3.0,
    transport=None,
) -> str:
    """Query Muse's same-origin /api/client-ip endpoint for the client egress IP."""
    if not isinstance(cookie, str) or not cookie.strip():
        return ""
    active = route or DIRECT_ROUTE
    real_base = base_url.rstrip("/")
    url = f"{http_base(real_base, active)}/api/client-ip"
    headers = {
        "Cookie": cookie.strip(),
        "Accept": "application/json",
        "Origin": real_base,
        "Referer": f"{real_base}/",
        "User-Agent": constants.DEFAULT_USER_AGENT,
    }
    try:
        with muse_http_client(
            active, timeout=timeout, follow_redirects=False, transport=transport,
        ) as client:
            resp = http_request(client, "get", url, headers=headers)
            if resp.status_code != 200:
                return ""
            data = resp.json()
    except Exception:
        return ""
    raw_ip = data.get("ip") if isinstance(data, dict) else None
    if not isinstance(raw_ip, str) or not raw_ip.strip():
        return ""
    try:
        return str(ipaddress.ip_address(raw_ip.strip()))
    except ValueError:
        return ""


class ClientIPResolver:
    """Non-blocking, single-flight memory cache mirroring muse.ai's /api/client-ip flow."""

    def __init__(
        self,
        cookie_provider: Callable[[], str],
        route: NetworkRoute | None = None,
        *,
        configured_ip: str = "",
        base_url: str = "https://muse.ai",
        timeout: float = 3.0,
        fetcher: Callable[..., str] | None = None,
    ):
        self._cookie_provider = cookie_provider
        self._route = route or DIRECT_ROUTE
        self._configured_ip = configured_ip.strip() if isinstance(configured_ip, str) else ""
        self._base_url = base_url
        self._timeout = timeout
        self._fetcher = fetcher or fetch_client_ip
        self._lock = threading.Lock()
        self._cached_ip = ""
        self._fetching = False

    def peek(self) -> str:
        if self._configured_ip:
            return self._configured_ip
        with self._lock:
            return self._cached_ip

    def get(self) -> str:
        """Return configured/cached IP synchronously; warm asynchronously if missing."""
        if self._configured_ip:
            return self._configured_ip
        with self._lock:
            if self._cached_ip:
                return self._cached_ip
            if self._fetching:
                return ""
            self._fetching = True

        def _run() -> None:
            try:
                cookie = self._cookie_provider()
                if not isinstance(cookie, str) or not cookie.strip():
                    return
                detected = self._fetcher(
                    cookie, self._route, base_url=self._base_url, timeout=self._timeout,
                )
                if isinstance(detected, str) and detected:
                    with self._lock:
                        self._cached_ip = detected
            except Exception:
                pass
            finally:
                with self._lock:
                    self._fetching = False

        threading.Thread(target=_run, name="muse-client-ip", daemon=True).start()
        return ""
