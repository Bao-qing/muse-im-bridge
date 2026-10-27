"""OneBot v11 (NapCat) HTTP API client.

Strictly follows the OneBot v11 specification using Array message segments
for rich text, formatting, and clean escaping.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Any, Dict, List, Optional


class OneBotClient:
    def __init__(self, base_url: str, access_token: str):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _post(self, path: str, payload: dict, timeout: int = 15) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"OneBot API request to {path} failed: {e}")

    def send_group_msg(self, group_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a group message using Array segments format."""
        segments: List[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for url in image_urls or []:
            segments.append({"type": "image", "data": {"file": url}})

        return self._post("/send_group_msg", {"group_id": int(group_id), "message": segments})

    def send_private_msg(self, user_id: int, text: str, image_urls: Optional[List[str]] = None) -> dict:
        """Send a private message using Array segments format."""
        segments: List[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for url in image_urls or []:
            segments.append({"type": "image", "data": {"file": url}})

        return self._post("/send_private_msg", {"user_id": int(user_id), "message": segments})

    def get_group_msg_history(self, group_id: int, count: int = 10) -> List[dict]:
        res = self._post("/get_group_msg_history", {"group_id": int(group_id), "count": count})
        if res.get("retcode") == 0:
            return res.get("data", {}).get("messages", [])
        return []
