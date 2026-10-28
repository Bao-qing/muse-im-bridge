#!/usr/bin/env python3
"""Muse Pure-Protocol Login Tool (Python).

Logs into Muse using native HTTP/2 requests (NO headless browser, NO visible browser).
Sends email OTP, prompts for verification code (or auto-reads Agent Mail),
and extracts cookies to the specified JSON file.

Usage:
  python tools/login.py --email "user@example.com" --output "cookies_test.json"
"""

import argparse
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from muse_bridge.muse.login import run_protocol_login


def main():
    parser = argparse.ArgumentParser(description="Muse 纯协议登录工具 (100% Python, 无浏览器)")
    parser.add_argument("--email", default=None, help="登录邮箱地址")
    parser.add_argument("--output", default="cookies_test.json", help="输出 Cookies JSON 文件路径 (默认: cookies_test.json)")
    parser.add_argument("--no-auto-otp", action="store_true", help="禁用 Agent Mail 自动收件抓码，强制手动输入")
    args = parser.parse_args()

    try:
        run_protocol_login(
            email=args.email,
            output_path=args.output,
            auto_otp=not args.no_auto_otp,
        )
    except KeyboardInterrupt:
        print("\n[MuseLogin] 用户取消登录。")
        sys.exit(1)
    except Exception as e:
        print(f"[MuseLogin] 登录失败：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
