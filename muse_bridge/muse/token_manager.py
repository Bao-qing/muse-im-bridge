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
import sys
import tempfile
import threading
import time
from typing import Optional, Tuple

import httpx

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
        self.base_url = base_url.rstrip("/")

        self._token = ""
        self._notary_token = ""
        self._invalid = False
        self._cookie_mtime_when_invalidated: float = 0.0
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
                resp = client.get(f"{self.base_url}/", headers=headers)
                html = resp.text
        except Exception:
            return ""
        hits = _VM_HOST_RE.findall(html)
        if not hits:
            return ""
        return max(set(hits), key=hits.count).lower()

    def _post_json(self, url: str, payload: dict, cookie: str, timeout: int = 20) -> dict:
        headers = self._get_headers(cookie)
        try:
            with httpx.Client(http2=True, timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    detail = resp.text[:200]
                    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
                return resp.json()
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"request to {url} failed: {e}")

    def _write_mint_atomic(self, token: str, notary: str) -> None:
        if not self.mint_file:
            return
        directory = os.path.dirname(os.path.abspath(self.mint_file)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mint-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "notary_token": notary}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
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

    def invalidate(self) -> None:
        """Mark cached tokens and cookies as invalid and remove cached mint file."""
        self._token = ""
        self._notary_token = ""
        self._invalid = True
        if self.cookies_file and os.path.exists(self.cookies_file):
            try:
                self._cookie_mtime_when_invalidated = os.path.getmtime(self.cookies_file)
            except OSError:
                self._cookie_mtime_when_invalidated = time.time()
        else:
            self._cookie_mtime_when_invalidated = time.time()

        if self.mint_file and os.path.exists(self.mint_file):
            try:
                os.remove(self.mint_file)
            except OSError:
                pass

    def is_fresh(self) -> bool:
        if self._invalid:
            return False
        if not self._token or not self._notary_token:
            return False
        exp = self.notary_expiry(self._notary_token)
        return exp is None or time.time() < exp - 60

    def has_credentials(self) -> bool:
        """Check whether we have usable cached tokens or an existing cookies file."""
        if self._invalid:
            if self._cookie_mtime_when_invalidated and self.cookies_file and os.path.exists(self.cookies_file):
                try:
                    if os.path.getmtime(self.cookies_file) > self._cookie_mtime_when_invalidated:
                        self._invalid = False
                except OSError:
                    pass
            if self._invalid:
                return False

        if self._token and self._notary_token and self.is_fresh():
            return True
        return bool(self.load_cookie_string())

    def refresh_from_cookies(self, interactive: bool = True) -> Tuple[str, str, str]:
        """Mint new tokens using cookies and save to mint_file atomically."""
        with self._lock:
            cookie = self.load_cookie_string()
            if not cookie and interactive and sys.stdin.isatty():
                print(f"[Muse] Cookies 文件 ({self.cookies_file}) 不存在或为空。")
                try:
                    choice = input("是否立即通过纯协议登录获取新 Cookies？(y/n): ").strip().lower()
                    if choice in ("y", "yes"):
                        from .login import run_protocol_login
                        run_protocol_login(output_path=self.cookies_file)
                        cookie = self.load_cookie_string()
                except Exception as e:
                    print(f"[Muse] 登录流程失败或取消：{e}")

            if not cookie:
                self.invalidate()
                raise RuntimeError(
                    f"cookies file {self.cookies_file} not found or empty (cannot refresh mint)"
                )

            if not self.vm_id:
                derived = self.vm_id_from_token(self._token)
                if derived:
                    self.vm_id = derived
                else:
                    self.vm_id = self.vm_id_from_cookies(cookie)

            if not self.vm_id:
                self.invalidate()
                raise RuntimeError("cannot resolve vm_id: not provided and not found in page HTML")

            vm_address = f"wss://{self.vm_id}.metaaivm.com/"
            token_url = f"{self.base_url}/api/hatch/token"
            try:
                data = self._post_json(token_url, {"vmAddress": vm_address, "vmName": self.vm_id}, cookie)
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ("401", "403", "unauthorized", "hatch_vm_auth")):
                    self.invalidate()
                raise

            token = (data.get("token") or "").strip()
            notary = (data.get("notary_token") or "").strip()
            if not token:
                self.invalidate()
                raise RuntimeError(f"{token_url} returned no token")

            if not notary:
                notary_url = f"{self.base_url}/api/hatch/noise-notary-token"
                try:
                    extra = self._post_json(notary_url, {"vmName": self.vm_id}, cookie)
                except Exception as e:
                    msg = str(e).lower()
                    if any(k in msg for k in ("401", "403", "unauthorized", "hatch_vm_auth")):
                        self.invalidate()
                    raise
                notary = (extra.get("notaryToken") or "").strip()

            if not notary:
                self.invalidate()
                raise RuntimeError("failed to acquire notary_token from web API")

            self._token = token
            self._notary_token = notary
            self._invalid = False
            self._write_mint_atomic(token, notary)
            return token, notary, self.vm_id

    def get_credentials(self) -> Tuple[str, str, str]:
        """Get (token, notary_token, vm_id). Refreshes automatically if stale."""
        with self._lock:
            if not self.vm_id:
                self.vm_id = self.vm_id_from_token(self._token)

            if not self.is_fresh():
                try:
                    self.refresh_from_cookies()
                except Exception as e:
                    self.invalidate()
                    raise RuntimeError(f"failed to obtain valid Muse credentials: {e}")
            return self._token, self._notary_token, self.vm_id
