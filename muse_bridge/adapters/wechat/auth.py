"""Independent QR login state machine; never persists or clears credentials."""
from __future__ import annotations

import asyncio
import inspect
from time import monotonic
from collections.abc import Mapping

from .client import WeChatClient, WeChatError
from .security import DEFAULT_API, redirect_base, validate_url


async def _callback(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


def _accounts(local_accounts) -> list[dict]:
    values = local_accounts.values() if isinstance(local_accounts, Mapping) else local_accounts
    return [dict(account) for account in values if isinstance(account, Mapping)]


def _normalize(account: Mapping) -> dict[str, str]:
    token = account.get("bot_token")
    bot_id = account.get("bot_id") or account.get("ilink_bot_id")
    user_id = account.get("user_id") or account.get("ilink_user_id") or ""
    if not isinstance(token, str) or not token.strip():
        raise WeChatError("WeChat login omitted bot token")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise WeChatError("WeChat login omitted bot ID")
    if not isinstance(user_id, str):
        raise WeChatError("Invalid WeChat login user ID")
    return {
        "bot_token": token.strip(), "bot_id": bot_id, "user_id": user_id,
        "baseurl": validate_url(account.get("baseurl") or DEFAULT_API, base=True),
    }


def _bound_account(response: Mapping, accounts: list[dict]) -> dict[str, str]:
    bot_id = response.get("ilink_bot_id")
    user_id = response.get("ilink_user_id")
    # The pinned official source does not guarantee an identity in this state.
    # Without a unique, explicit identity match we must not guess a local token.
    if not bot_id and not user_id:
        raise WeChatError("Bound WeChat account cannot be identified safely")
    matches = [account for account in accounts if (
        (not bot_id or (account.get("bot_id") or account.get("ilink_bot_id")) == bot_id)
        and (not user_id or (account.get("user_id") or account.get("ilink_user_id")) == user_id)
        and account.get("bot_token")
    )]
    if len(matches) != 1:
        raise WeChatError("Bound WeChat account has no unique local credential")
    selected = dict(matches[0])
    if response.get("baseurl"):
        selected["baseurl"] = response["baseurl"]
    return _normalize(selected)


class WeChatAuth:
    def __init__(self, client: WeChatClient) -> None:
        self.client = client

    async def login(self, local_accounts, *, display, verify, on_status=None) -> dict[str, str]:
        """Return credentials only after confirmation or an unambiguous binding.

        local_accounts must be newest-first (mapping insertion order also works).
        display(content), verify(), on_status(status) may be sync or async.
        Cancellation propagates (including verify returning None); local_accounts
        is never modified. The caller owns hidden input and credential storage.
        """
        try:
            async with asyncio.timeout(480):
                return await self._login(local_accounts, display, verify, on_status)
        except TimeoutError:
            raise WeChatError("WeChat QR login timed out") from None

    async def _login(self, local_accounts, display, verify, on_status) -> dict[str, str]:
        accounts = _accounts(local_accounts)
        tokens = []
        for account in accounts:
            token = account.get("bot_token")
            if isinstance(token, str) and token.strip() and token.strip() not in tokens:
                tokens.append(token.strip())
            if len(tokens) == 10:
                break
        for _ in range(3):  # Total QR count, not three additional refreshes.
            qr = await self.client.get_qrcode(tokens)
            qrcode, content = qr.get("qrcode"), qr.get("qrcode_img_content")
            if not isinstance(qrcode, str) or not qrcode or not isinstance(content, str) or not content:
                raise WeChatError("Invalid WeChat QR response")
            await _callback(display, content)
            base_url, code = DEFAULT_API, ""
            while True:
                poll_started = monotonic()
                response = await self.client.get_qrcode_status(qrcode, code, base_url=base_url)
                status = response.get("status")
                if not isinstance(status, str) or status not in {
                    "wait", "scaned", "confirmed", "need_verifycode", "verify_code_blocked",
                    "scaned_but_redirect", "binded_redirect", "expired",
                }:
                    raise WeChatError("Unknown WeChat QR status")
                if on_status is not None:
                    await _callback(on_status, status)
                if status == "confirmed":
                    return _normalize(response)
                if status == "binded_redirect":
                    return _bound_account(response, accounts)
                if status in {"expired", "verify_code_blocked"}:
                    break
                if status == "need_verifycode":
                    code = await _callback(verify)
                    if code is None:
                        raise asyncio.CancelledError()
                    if not isinstance(code, str) or not code.strip():
                        raise WeChatError("Empty WeChat verification code")
                    code = code.strip()
                elif status == "scaned":
                    code = ""
                elif status == "scaned_but_redirect":
                    base_url = redirect_base(response.get("redirect_host"))
                # The service can return immediately; wait/scaned polls start
                # at least one second apart without adding delay to a long poll.
                delay = max(0, 1 - (monotonic() - poll_started)) if status in {"wait", "scaned"} else 0
                await asyncio.sleep(delay)
        raise WeChatError("WeChat QR limit reached (three codes total)")


async def login(local_accounts, *, display, verify, on_status=None) -> dict[str, str]:
    """Convenience login using a temporary, unauthenticated protocol client."""
    client = WeChatClient()
    try:
        return await WeChatAuth(client).login(
            local_accounts, display=display, verify=verify, on_status=on_status,
        )
    finally:
        await client.aclose()
