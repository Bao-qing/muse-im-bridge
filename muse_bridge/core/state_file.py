"""Helpers for runtime state files kept under ``data/``."""

from __future__ import annotations

import os


def ensure_parent(path) -> bool:
    """Create the directory ``path`` lives in, best-effort.

    State files may be configured into a directory that does not exist yet
    (the default ``data/``). Returns True when the directory exists after the
    call, and False otherwise; it never raises, so a filesystem problem stays
    at the write site instead of breaking the caller.
    """
    if not path:
        return False
    try:
        directory = os.path.dirname(os.path.abspath(os.fspath(path)))
        os.makedirs(directory, exist_ok=True)
        return True
    except (OSError, TypeError, ValueError):
        return False
