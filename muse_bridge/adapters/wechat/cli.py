"""Explicit local iLink login and redacted, offline account status commands."""
from __future__ import annotations

import asyncio
import getpass
import json

from ...config.models import WeChatConfig


def display_qr(content: str) -> None:
    """Render the supplied QR content locally; never download a display URL."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    print("请使用微信扫描终端二维码；不要分享二维码或验证信息。")
    qr.print_ascii(invert=True)


def verify_code(*_args, **_kwargs) -> str:
    # Never echo server prompts, tickets, URLs, or the verification value.
    return getpass.getpass("请输入微信验证信息（隐藏输入）: ").strip()


def show_status(*_args, **_kwargs) -> None:
    # Auth may supply server payloads: do not print them, even on failure.
    print("微信登录状态已更新，请按终端或微信提示继续。")


async def _login(config: WeChatConfig) -> dict:
    from .auth import WeChatAuth
    from .client import WeChatClient
    from .state import WeChatState

    state = WeChatState(config.state_dir)
    client = WeChatClient(channel_version=config.channel_version, route_tag=config.route_tag,
                          timeout=config.http_timeout_s)
    try:
        credentials = await WeChatAuth(client).login(
            state.accounts(), display=display_qr, verify=verify_code, on_status=show_status)
        state.save_account(credentials)
    finally:
        await client.aclose()
    print("微信账号已保存并激活。扫码者不会自动成为管理员；请在本地显式配置 admin_id。")
    # Only return the state-owned redacted summary, not auth credentials.
    return state.status()


def run_login(config: WeChatConfig) -> dict:
    """Run login only on an explicit CLI or wizard request."""
    return asyncio.run(_login(config))


def run_status(config: WeChatConfig) -> dict:
    from .state import WeChatState

    status = WeChatState(config.state_dir).status()
    print("微信本地状态（未连接微信，不代表远端登录有效）:")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return status
