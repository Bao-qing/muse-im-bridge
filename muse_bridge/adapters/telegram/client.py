"""Official Telegram Bot API transport. Never expose token-bearing URLs in errors."""
from __future__ import annotations

import json
import re

import httpx


HOST = "https://api.telegram.org"
DOWNLOAD_CAP = 20 * 1024 * 1024
UPLOAD_CAP = 50 * 1024 * 1024
PHOTO_CAP = 10 * 1024 * 1024
_FILE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", re.ASCII)


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, *, status: int = 0, rejected: bool = False,
                 retry_after: int = 0, invalid_markdown: bool = False):
        super().__init__(f"Telegram {method} {'rejected' if rejected else 'not confirmed'}"
                         + (f" (HTTP {status})" if status else ""))
        self.status = status
        self.rejected = rejected
        self.retry_after = retry_after
        self.invalid_markdown = invalid_markdown


class TelegramClient:
    def __init__(self, config, *, transport=None):
        self._api_prefix = f"{HOST}/bot{config.bot_token}"
        self._file_prefix = f"{HOST}/file/bot{config.bot_token}"
        self._download_timeout = config.download_timeout_s
        self._http = httpx.AsyncClient(transport=transport, proxy=config.proxy_url,
                                      trust_env=False, follow_redirects=False, timeout=30)

    async def close(self):
        await self._http.aclose()

    async def call(self, method: str, params: dict | None = None, *, files=None, timeout=30):
        payload = params or {}
        kwargs = ({"data": {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                             for k, v in payload.items()}, "files": files}
                  if files else {"json": payload})
        try:
            response = await self._http.post(f"{self._api_prefix}/{method}", timeout=timeout, **kwargs)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise TelegramAPIError(method) from None
        status = response.status_code
        if isinstance(body, dict):
            if response.is_success and body.get("ok") is True and "result" in body:
                return body["result"]
            if (400 <= status < 500 and body.get("ok") is False
                    and type(body.get("error_code")) is int and body["error_code"] == status):
                hints = body.get("parameters")
                delay = hints.get("retry_after") if isinstance(hints, dict) else None
                description = body.get("description")
                invalid_markdown = (method == "sendMessage" and status == 400
                                    and isinstance(description, str)
                                    and description.lower().startswith("bad request: can't parse entities:"))
                raise TelegramAPIError(method, status=status, rejected=True,
                                       retry_after=delay if type(delay) is int and delay > 0 else 0,
                                       invalid_markdown=invalid_markdown)
        raise TelegramAPIError(method, status=status)

    async def updates(self, offset: int | None, poll_timeout: int) -> list[dict]:
        params = {"timeout": poll_timeout, "allowed_updates": ["message", "message_reaction"]}
        if offset is not None:
            params["offset"] = offset
        updates = await self.call("getUpdates", params,
                                  timeout=httpx.Timeout(15, read=poll_timeout + 10))
        if not isinstance(updates, list) or any(
            not isinstance(row, dict) or type(row.get("update_id")) is not int or row["update_id"] <= 0
            for row in updates
        ):
            raise TelegramAPIError("getUpdates")
        return sorted(updates, key=lambda row: row["update_id"])

    async def download(self, file_id: str, max_bytes: int) -> bytes:
        cap = min(max_bytes, DOWNLOAD_CAP)
        if cap <= 0 or not isinstance(file_id, str) or not file_id:
            raise ValueError("Telegram attachment has no usable file ID or download limit")
        info = await self.call("getFile", {"file_id": file_id})
        info = info if isinstance(info, dict) else {}
        path = info.get("file_path")
        if not isinstance(path, str) or not _FILE_PATH.fullmatch(path) or any(
            part in {".", ".."} for part in path.split("/")
        ):
            raise ValueError("Telegram attachment has no usable file path")
        size = info.get("file_size")
        if type(size) is int and size > cap:
            raise ValueError("Telegram attachment exceeds download limit")
        try:
            async with self._http.stream("GET", f"{self._file_prefix}/{path}",
                                         timeout=self._download_timeout) as response:
                if not response.is_success:
                    raise TelegramAPIError("file download", status=response.status_code)
                length = response.headers.get("content-length", "")
                if length.isascii() and length.isdecimal() and len(length) <= 20 and int(length) > cap:
                    raise ValueError("Telegram attachment exceeds download limit")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > cap:
                        raise ValueError("Telegram attachment exceeds download limit")
                    data.extend(chunk)
        except httpx.HTTPError:
            raise TelegramAPIError("file download") from None
        return bytes(data)
