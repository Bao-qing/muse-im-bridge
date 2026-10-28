"""Muse Pure-Protocol Authentication Client (No Browser).

Executes the complete Meta/Muse native OTP authentication protocol:
1. Performs HTTP/2 SSO handshake with auth.muse.ai to acquire initial CSRF and datr cookies.
2. Calls /api/auth/native/restart to establish auth state and CSRF token.
3. Calls /api/auth/native/send-otp to trigger 6-digit email verification code.
4. Calls /api/auth/native/confirm-otp with the verification code.
5. Captures and exports the resulting hatch_sess session cookies to JSON.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from typing import Dict, List, Optional

import httpx

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def try_fetch_agent_mail_otp() -> Optional[str]:
    """Check recent emails via agently-cli to automatically capture OTP code."""
    try:
        res = subprocess.run(
            "agently-cli message +list --limit 2",
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            data = json.loads(res.stdout)
            messages = data.get("data", {}).get("data", [])
            for msg in messages:
                snippet = msg.get("snippet", "")
                subject = msg.get("subject", "")
                if "验证" in subject or "验证码" in snippet or "登录" in subject:
                    m = re.search(r"([0-9]{6})", snippet)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return None


class MuseProtocolLogin:
    def __init__(self, base_url: str = "https://muse.ai"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(http2=True, follow_redirects=True, timeout=25.0)
        self.waterfall_id = str(uuid.uuid4())
        self.csrf_token: Optional[str] = None

    def initialize_session(self) -> None:
        """Step 1: Perform SSO redirect cycle to acquire datr and csrf cookies."""
        url = f"https://auth.{self.base_url.split('://')[-1]}/aymh/?origin={self.base_url}"
        resp = self.client.get(url, headers=_BASE_HEADERS)
        if resp.status_code != 200:
            # Fallback to direct landing GET
            self.client.get(f"{self.base_url}/", headers=_BASE_HEADERS)

    def _api_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": _BASE_HEADERS["User-Agent"],
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/?aymh_complete=1",
            "Sec-Ch-Ua": _BASE_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "x-hatch-waterfall-id": self.waterfall_id,
        }
        if self.csrf_token:
            headers["x-hatch-csrf-token"] = self.csrf_token
        return headers

    def send_otp(self, email: str, region: str = "CN") -> dict:
        """Step 2: Restart native auth state and send verification code to email."""
        self.initialize_session()

        # Call /restart
        r1 = self.client.post(f"{self.base_url}/api/auth/native/restart", headers=self._api_headers(), json={})
        if r1.status_code != 201:
            raise RuntimeError(f"failed to initialize native auth ({r1.status_code}): {r1.text}")

        self.csrf_token = r1.json().get("csrf_token")
        if not self.csrf_token:
            # Extract from cookie
            for c in self.client.cookies.jar:
                if c.name == "hatch_native_auth_csrf" and "." in c.value:
                    self.csrf_token = c.value.split(".")[0]
                    break

        # Call /send-otp
        r2 = self.client.post(
            f"{self.base_url}/api/auth/native/send-otp",
            headers=self._api_headers(),
            json={"contact_point": email.strip(), "region": region},
        )
        if r2.status_code != 201:
            raise RuntimeError(f"failed to send OTP to {email} ({r2.status_code}): {r2.text}")

        return r2.json()

    def confirm_otp(self, otp_code: str) -> dict:
        """Step 3: Submit 6-digit OTP code to verify and acquire session cookies."""
        r = self.client.post(
            f"{self.base_url}/api/auth/native/confirm-otp",
            headers=self._api_headers(),
            json={"otp_code": otp_code.strip(), "remember_device": True},
        )
        if r.status_code != 200:
            raise RuntimeError(f"OTP confirmation failed ({r.status_code}): {r.text}")

        data = r.json()

        # If registration completion is required (new account without name/birthday):
        if data.get("registration_state") == "completion_required":
            r_comp = self.client.post(
                f"{self.base_url}/api/auth/native/register-completion",
                headers=self._api_headers(),
                json={
                    "date_of_birth": "2000-01-01",
                    "first_name": "保清",
                    "last_name": "秦",
                    "tos_content_version": "ntm_create_account:default_v1_meta_account_v2+docs_v1",
                    "remember_device": True,
                },
            )
            if r_comp.status_code == 200:
                data = r_comp.json()

        return data

    def extract_cookies(self) -> Dict[str, str]:
        """Extract all cookies for muse.ai as a dict and a concatenated header string."""
        cookies_dict = {}
        for cookie in self.client.cookies.jar:
            if "muse.ai" in cookie.domain and cookie.value:
                cookies_dict[cookie.name] = cookie.value

        pairs = [f"{k}={v}" for k, v in cookies_dict.items()]
        return {"cookie": "; ".join(pairs)}


def run_protocol_login(
    email: Optional[str] = None,
    output_path: str = "cookies_test.json",
    auto_otp: bool = True,
) -> Dict[str, str]:
    """Interactive protocol login runner."""
    if not email:
        email = input("请输入登录邮箱: ").strip()
    if not email:
        raise ValueError("邮箱地址不能为空")

    print(f"[MuseLogin] 正在通过纯 HTTP/2 协议登录 (邮箱: {email})...")
    auth = MuseProtocolLogin()

    print("[MuseLogin] 正在请求发送验证码...")
    auth.send_otp(email)
    print(f"[MuseLogin] 验证码已成功发送至 {email}！")

    otp_code = None
    if email.endswith("@agent.qq.com") and auto_otp:
        print("[MuseLogin] 检测到 Agent Mail，等待邮件到达并自动提取验证码...")
        for _ in range(15):
            time.sleep(2)
            otp_code = try_fetch_agent_mail_otp()
            if otp_code:
                print(f"[MuseLogin] 自动获取到验证码: {otp_code}")
                break

    if not otp_code:
        otp_code = input("请输入收到的 6 位数验证码: ").strip()

    if not otp_code or len(otp_code) != 6:
        raise ValueError(f"验证码格式不正确：{otp_code}")

    print("[MuseLogin] 正在提交验证码并换取会话 Cookies...")
    auth.confirm_otp(otp_code)

    cookies_data = auth.extract_cookies()
    if "hatch_sess" not in cookies_data.get("cookie", ""):
        print("[MuseLogin] 警告: 当前返回未显式包含 hatch_sess（账号可能处于等候名单或需进一步核验）。")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cookies_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[MuseLogin] 纯协议登录成功！Cookies 已保存至: {output_path}")
    return cookies_data
