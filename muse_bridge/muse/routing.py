"""Muse-specific URL, cookie, and redirect guards on top of the shared network layer."""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ..network import DIRECT_ROUTE, NetworkRoute


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
