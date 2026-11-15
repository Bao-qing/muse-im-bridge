"""Helpers for runtime state files kept under ``data/``."""

from __future__ import annotations

import json
import os
import tempfile


def atomic_json_write(path, data) -> None:
    """Durably replace JSON using a private temporary in the same directory.

    Errors propagate: callers must publish new in-memory state only after this
    returns. A failed write leaves the previous destination untouched.
    """
    destination = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
