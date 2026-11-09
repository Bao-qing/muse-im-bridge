"""Muse credential and token management.

Maintains the authentication chain:
  cookies (cookies.json / env)
      │
      ├──> vm_id (extracted from HTML if not provided)
      │
      └──> /api/hatch/token -> token + notary_token -> (.mint.json cache)

Automatically checks expiry before connecting, and provides auto-renewal
when credentials expire or are rejected by the server.
"""

from __future__ import annotations

import base64
import json
import os
import re
import hashlib
import math
import tempfile
import threading
import time
from typing import Optional, Tuple

import httpx

from .client.errors import CredentialRefreshError, LoginRequired
from ..observability import http_request, observed

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "Sec-Ch-Ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

_VM_HOST_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.metaaivm\.com",
    re.I,
)


class MuseTokenManager:
    def __init__(
        self,
        mint_file: str = ".mint.json",
        cookies_file: str = "cookies.json",
        vm_id: str = "",
        base_url: str = "https://muse.ai",
    ):
        self.mint_file = mint_file
        self.cookies_file = cookies_file
        self.vm_id = vm_id
        self._explicit_vm = bool(vm_id)
        self.base_url = base_url.rstrip("/")

        self._token = ""
        self._notary_token = ""
        self._invalid = False
        self._invalid_cookie_digest = None
        self._mint_rejected = False
        self._retry_after = 0.0
        self._refresh_error = None
        self._lock = threading.RLock()
        self._load_cached_mint()

    def _load_cached_mint(self) -> None:
        if not self.mint_file or not os.path.exists(self.mint_file):
            return
        try:
            with open(self.mint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._token = (data.get("token") or "").strip()
                self._notary_token = (data.get("notary_token") or "").strip()
        except Exception:
            self._token = ""
            self._notary_token = ""

    @staticmethod
    def vm_id_from_token(token: str) -> str:
        """Derive env_id (vm_id) from the token JWT payload."""
        if not token:
            return ""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return ""
            payload = parts[1]
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            obj = json.loads(decoded.decode("utf-8"))
            env_id = obj.get("env_id") if isinstance(obj, dict) else None
            return env_id if isinstance(env_id, str) and env_id else ""
        except Exception:
            return ""

    @staticmethod
    def notary_expiry(notary: str) -> Optional[int]:
        """Read timeout restriction timestamp from notary token."""
        if not notary:
            return None
        try:
            parts = notary.split(".")
            if len(parts) < 3:
                return None
            payload = parts[2] + "=" * (-len(parts[2]) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            obj = json.loads(decoded.decode("utf-8"))
            restrictions = obj.get("restrictions")
            if not isinstance(restrictions, list):
                return None
            for item in restrictions:
                if isinstance(item, str) and item.startswith("timeout:"):
                    ts = int(item.split(":", 1)[1].strip())
                    return ts if ts > 0 else None
        except Exception:
            return None
        return None

    def load_cookie_string(self) -> str:
        """Load cookie header string from cookies_file or environment."""
        env_cookie = os.environ.get("MUSE_COOKIE", "").strip()
        if env_cookie:
            return env_cookie
        if not self.cookies_file or not os.path.exists(self.cookies_file):
            return ""
        try:
            with open(self.cookies_file, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception:
            return ""
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "cookie" in data and isinstance(data["cookie"], str):
                    return data["cookie"].strip()
                pairs = [f"{k}={v}" for k, v in data.items() if isinstance(v, str) and v.strip()]
                return "; ".join(pairs)
            if isinstance(data, str):
                return data.strip()
        except Exception:
            pass

        # Text / Netscape format fallback
        pairs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            if "\t" in line:
                fields = line.split("\t")
                if len(fields) >= 7 and "muse.ai" in fields[0] and fields[5]:
                    pairs.append(f"{fields[5]}={fields[6]}")
            elif "=" in line:
                pairs.append(line)
        return "; ".join(pairs)

    def _get_headers(self, cookie: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
            "User-Agent": _BASE_HEADERS["User-Agent"],
            "Sec-Ch-Ua": _BASE_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": _BASE_HEADERS["Sec-Ch-Ua-Mobile"],
            "Sec-Ch-Ua-Platform": _BASE_HEADERS["Sec-Ch-Ua-Platform"],
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def vm_id_from_cookies(self, cookie: str, timeout: int = 20) -> str:
        """Extract vm_id from muse.ai index page HTML using cookies."""
        if not cookie:
            return ""
        headers = self._get_headers(cookie)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        try:
            with httpx.Client(http2=True, timeout=timeout, follow_redirects=True) as client:
                resp = http_request(client, "get",f"{self.base_url}/", headers=headers)
                html = resp.text
        except Exception:
            raise CredentialRefreshError("resolve_vm") from None
        hits = _VM_HOST_RE.findall(html)
        if not hits:
            return ""
        return max(set(hits), key=hits.count).lower()

    def _post_json(self, url: str, payload: dict, cookie: str, timeout: int = 20) -> dict:
        headers = self._get_headers(cookie)
        stage = url.rsplit("/", 1)[-1]
        try:
            with httpx.Client(http2=True, timeout=timeout) as client:
                resp = http_request(client, "post",url, headers=headers, json=payload)
                if resp.status_code != 200:
                    try:
                        body = resp.json()
                        code = body.get("code", "") if isinstance(body, dict) else ""
                    except ValueError:
                        code = ""
                    # Never include arbitrary response bodies or credential-bearing URLs.
                    code = code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code) else ""
                    raise CredentialRefreshError(stage, resp.status_code, code)
                data = resp.json()
                if not isinstance(data, dict):
                    raise CredentialRefreshError(stage)
                return data
        except CredentialRefreshError:
            raise
        except Exception:
            raise CredentialRefreshError(stage) from None

    def _write_mint_atomic(self, token: str, notary: str) -> None:
        if not self.mint_file:
            return
        directory = os.path.dirname(os.path.abspath(self.mint_file)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mint-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "notary_token": notary}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.mint_file)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _cookie_digest(cookie):
        return hashlib.sha256(cookie.encode("utf-8")).digest()

    def invalidate(self) -> None:
        """Mark a confirmed missing/rejected cookie session as requiring login."""
        with self._lock:
            self._invalid = True
            self._invalid_cookie_digest = self._cookie_digest(self.load_cookie_string())
            self._mint_rejected = True
            self._retry_after = 0.0
            self._token = self._notary_token = ""
            if self.mint_file:
                try:
                    os.remove(self.mint_file)
                except OSError:
                    pass

    def invalidate_mint(self, token=None, notary=None) -> None:
        """Reject only the failed snapshot; a late failure cannot reject newer mint."""
        with self._lock:
            if (token is None or token == self._token) and (notary is None or notary == self._notary_token):
                self._mint_rejected = True

    @classmethod
    def credentials_expiry(cls, token, notary):
        expiry = cls.notary_expiry(notary)
        try:
            payload = token.split(".")[1]
            value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                expiry = min(expiry, value) if expiry is not None else value
        except Exception:
            pass
        return expiry

    def is_fresh(self) -> bool:
        if self._invalid or self._mint_rejected or not self._token or not self._notary_token:
            return False
        exp = self.credentials_expiry(self._token, self._notary_token)
        return exp is None or time.time() < exp - 60

    def has_credentials(self) -> bool:
        """Availability is not proof of validity, but temporary failures never lock cookies."""
        with self._lock:
            cookie = self.load_cookie_string()
            if self._invalid:
                if self._cookie_digest(cookie) == self._invalid_cookie_digest:
                    return False
                self._invalid = False
                self._retry_after = 0.0
            return self.is_fresh() or bool(cookie)

    def credentials_current(self, token, notary, vm_id) -> bool:
        with self._lock:
            return (token, notary, vm_id) == (self._token, self._notary_token, self.vm_id) and self.is_fresh()

    @observed("muse.login.refresh")
    def refresh_from_cookies(self, interactive: bool = False, *, rejected_token=None, rejected_notary=None) -> Tuple[str, str, str]:
        """Single-flight renewal; never prompts for OTP or conflates mint and cookie rejection."""
        with self._lock:
            changed = ((rejected_token is not None and rejected_token != self._token)
                       or (rejected_notary is not None and rejected_notary != self._notary_token))
            if changed and self.is_fresh():
                return self._token, self._notary_token, self.vm_id
            if not self.has_credentials():
                self.invalidate()
                raise LoginRequired("Muse Cookie 缺失或会话已失效，请管理员私聊 /登录。")
            if time.monotonic() < self._retry_after:
                raise self._refresh_error
            cookie = self.load_cookie_string()
            if not cookie:
                self.invalidate()
                raise LoginRequired("Muse Cookie 缺失，请管理员私聊 /登录。")
            try:
                vm_id = self.vm_id or self.vm_id_from_token(self._token) or self.vm_id_from_cookies(cookie)
                if not vm_id:
                    raise CredentialRefreshError("resolve_vm")
                for attempt in range(2):
                    try:
                        data = self._post_json(f"{self.base_url}/api/hatch/token",
                            {"vmAddress": f"wss://{vm_id}.metaaivm.com/", "vmName": vm_id}, cookie)
                        token = data.get("token")
                        notary = data.get("notary_token")
                        if not isinstance(token, str) or not token.strip():
                            raise CredentialRefreshError("missing_token")
                        if not notary:
                            extra = self._post_json(f"{self.base_url}/api/hatch/noise-notary-token", {"vmName": vm_id}, cookie)
                            notary = extra.get("notaryToken")
                        if not isinstance(notary, str) or not notary.strip():
                            raise CredentialRefreshError("missing_notary")
                        token, notary = token.strip(), notary.strip()
                        expiry = self.credentials_expiry(token, notary)
                        if expiry is not None and time.time() >= expiry - 60:
                            raise CredentialRefreshError("expired_mint")
                        break
                    except CredentialRefreshError as exc:
                        if exc.code.startswith("hatch_vm_auth"):
                            if exc.code == "hatch_vm_auth_no_match" and not self._explicit_vm and attempt == 0:
                                vm_id = self.vm_id_from_cookies(cookie)
                                if vm_id:
                                    continue
                            raise
                        if exc.status == 401:
                            self.invalidate()
                            raise LoginRequired("Muse Cookie 会话已失效，请管理员私聊 /登录。") from None
                        raise
                self._write_mint_atomic(token, notary)
            except LoginRequired:
                raise
            except Exception as exc:
                error = exc if isinstance(exc, CredentialRefreshError) else CredentialRefreshError("refresh")
                self._retry_after = time.monotonic() + 3.0
                self._refresh_error = error
                raise error from None
            self._token, self._notary_token, self.vm_id = token, notary, vm_id
            self._invalid = self._mint_rejected = False
            self._retry_after = 0.0
            self._refresh_error = None
            return token, notary, vm_id

    def get_credentials(self) -> Tuple[str, str, str]:
        """Get usable mint; only confirmed cookie failure can require login."""
        with self._lock:
            if not self.has_credentials():
                raise LoginRequired("Muse Cookie 缺失或会话已失效，请管理员私聊 /登录。")
            if not self.vm_id:
                self.vm_id = self.vm_id_from_token(self._token)
            if not self.is_fresh():
                return self.refresh_from_cookies()
            return self._token, self._notary_token, self.vm_id
