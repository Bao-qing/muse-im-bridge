#!/usr/bin/env python3
"""Muse Pure-Protocol Login Tool (Python).

Logs into Muse using native HTTP/2 requests (NO browser).
Sends email OTP, prompts for verification code, and extracts cookies
to the specified JSON file.

Usage:
  python tools/login.py [--email "user@example.com"] [--output "cookies.json"]
"""

import argparse
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from muse_bridge.config.models import ConfigLoader, LoggingConfig
from muse_bridge.muse.login import run_protocol_login
from muse_bridge.observability import configure_logging, get_logger, safe_error


logger = get_logger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Muse 纯协议登录工具 (100% Python, 无需浏览器)")
    parser.add_argument("--email", default=None, help="登录邮箱地址")
    parser.add_argument("--output", default="cookies.json", help="输出 Cookies JSON 文件路径 (默认: cookies.json)")
    parser.add_argument("--config", default=None, help="配置文件路径 (默认: config.json 或 credentials.json)")
    args = parser.parse_args(argv)

    configure_logging(LoggingConfig(file=""))
    try:
        config = ConfigLoader.load(args.config)
    except Exception as e:
        logger.error("Error loading configuration: %s", safe_error(e))
        sys.exit(1)
    configure_logging(config.logging, legacy_debug=config.debug)

    try:
        run_protocol_login(
            email=args.email,
            output_path=args.output,
        )
    except KeyboardInterrupt:
        print("\n[MuseLogin] 用户取消登录。")
        sys.exit(1)
    except Exception as e:
        logger.error("[MuseLogin] 登录失败：%s", safe_error(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
