"""Opt-in Muse Worker routing. No automatic direct fallback on any channel."""
from __future__ import annotations

from urllib.parse import urlsplit

from ..config.models import WorkerConfig

def worker_url(url: str, worker: WorkerConfig) -> str:
    """Prefix without parsing/re-encoding the path or credential query."""
    scheme, rest = url.split("://", 1)
    if scheme not in {"https", "wss"}:
        raise ValueError("Worker route requires HTTPS or WSS")
    base = worker.base
    if scheme == "wss":
        base = "wss://" + urlsplit(base).netloc
    return f"{base}/{scheme}/{rest}"


def http_base(base_url: str, worker: WorkerConfig, *, login: bool = False) -> str:
    base = base_url.rstrip("/")
    return worker_url(base, worker) if (worker.login if login else worker.http) else base


def auth_url(base_url: str, worker: WorkerConfig) -> str:
    real = base_url.rstrip("/")
    # Keep the legacy direct URL (including the unescaped origin) unchanged.
    auth = f"https://auth.{real.split('://')[-1]}/aymh/"
    if not worker.login:
        return f"{auth}?origin={real}"
    # Pending live SSO validation: default to the real origin, but some redirect
    # flows may require the prefixed origin. Switch with worker.login_origin.
    origin = http_base(real, worker, login=True) if worker.login_origin == "worker" else real
    return f"{worker_url(auth, worker)}?origin={origin}"


def cookie_domain(domain: str, worker: WorkerConfig | None = None) -> str | None:
    """Accept only Muse domains (not substring matches), plus the active proxy.

    Return the canonical persistence domain, never the Worker domain. Subdomains
    such as auth.muse.ai preserve the old login cookie extraction semantics.
    """
    domain = domain.lower().lstrip(".")
    if domain == "muse.ai" or domain.endswith(".muse.ai"):
        return "muse.ai"
    if worker and worker.enabled and domain == urlsplit(worker.base).hostname:
        return "muse.ai"
    return None


def worker_http_options(worker: WorkerConfig, *, login: bool = False) -> dict:
    """Ignore local proxies and reject redirect escape before the next send.

    Worker rewrites Location; this guard is not a substitute for that rewrite.
    follow_redirects stays enabled at the caller. No retry to a real origin.
    """
    if not (worker.login if login else worker.http):
        return {}
    base = urlsplit(worker.base)

    def require_worker(request):
        url = request.url
        if (url.scheme != "https" or url.host != base.hostname
                or url.port not in (None, 443)
                or not any(url.path.startswith(f"/{scheme}/{host}/")
                           for scheme in ("https", "http")
                           for host in ("muse.ai", "auth.muse.ai"))):
            raise RuntimeError("Muse Worker redirect escaped the configured relay")

    return {"trust_env": False, "event_hooks": {"request": [require_worker]}}
