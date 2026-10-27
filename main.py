#!/usr/bin/env python3
"""Main entry point for Muse Bridge.

Coordinates configuration loading, credential resolution, and concurrent
adapter lifecycle management.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from muse_bridge.config.models import ConfigLoader
from muse_bridge.engine import BridgeEngine


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Muse Bridge Multi-Platform Runner")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to user config file (default: config.json or credentials.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and credentials without connecting to live gateways",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # 1. Load configuration
    try:
        config = ConfigLoader.load(args.config)
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    # 2. Check mode: print sanitized status
    if args.check:
        print("=== Muse Bridge Configuration Check ===")
        print(f"Muse Host:      {config.muse.host}")
        print(f"Client IP:      {config.muse.client_ip or '(not set)'}")
        print(f"Cookies File:   {config.muse.cookies_file} ({'exists' if os.path.exists(config.muse.cookies_file) else 'missing'})")
        print(f"Mint File:      {config.muse.mint_file} ({'exists' if os.path.exists(config.muse.mint_file) else 'missing'})")
        print(f"VM ID:          {config.muse.vm_id or '(auto-derived)'}")
        print(f"Reply Mode:     {config.muse.reply_mode}")
        print("--- Adapters ---")
        if config.qq.enabled:
            target_type, target_id = config.qq.resolve_push_target()
            wl_priv = len(config.qq.whitelist.get("private") or []) if isinstance(config.qq.whitelist.get("private"), list) else 0
            wl_grp = len([k for k in config.qq.whitelist if k != "private"])
            print(f"QQ Adapter:     ENABLED (AppID: {config.qq.app_id})")
            print(f"  Whitelist:    {wl_priv} private user(s), {wl_grp} group(s)")
            print(f"  Push Subsys:  {'ENABLED' if config.qq.push.enabled else 'DISABLED'} -> {target_type}:{target_id or '(none)'}")
        else:
            print("QQ Adapter:     DISABLED")

        if config.onebot.enabled:
            print(f"OneBot Adapter: ENABLED ({config.onebot.base_url}, Target Group: {config.onebot.target_group_id})")
        else:
            print("OneBot Adapter: DISABLED")

        print("========================================")
        print("Config check passed successfully.")
        return 0

    # 3. Initialize and run BridgeEngine
    engine = BridgeEngine(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(engine.start())

    def _shutdown():
        print("\nShutdown signal received. Stopping...")
        loop.create_task(engine.stop())
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows signal handler limitations
            pass

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.run_until_complete(engine.stop())
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
