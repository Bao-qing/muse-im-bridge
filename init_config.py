#!/usr/bin/env python3
"""Compatibility entry point for ``manage.py``.

``init_config.py`` has been superseded by ``manage.py`` (the unified daily
operations console and first-run setup wizard). Direct execution and legacy
imports continue to work transparently through this shim.
"""

from __future__ import annotations

import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import manage as _manage  # noqa: E402

if __name__ == "__main__":
    print("💡 提示：配置管理与首次向导已升级为 manage.py，今后可直接运行 python manage.py。")
    try:
        sys.exit(_manage.main())
    except KeyboardInterrupt:
        print("\n[!] 用户中断，退出。")
        sys.exit(0)
else:
    # Alias the module directly so legacy imports and unittest.mock patches
    # targeting ``init_config.*`` operate on the live ``manage`` module.
    sys.modules[__name__] = _manage
