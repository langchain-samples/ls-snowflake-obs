"""Persisted poll cursor: the end timestamp of the last exported event."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

EPOCH_CURSOR = "1970-01-01 00:00:00.000 +0000"


def load_cursor(path: Path) -> str:
    """Return the last exported event timestamp, or the epoch if there is none."""
    try:
        return path.read_text(encoding="utf-8").strip() or EPOCH_CURSOR
    except FileNotFoundError:
        return EPOCH_CURSOR


def save_cursor(path: Path, timestamp: str) -> None:
    """Write the cursor atomically.

    A partially written cursor would either re-export or silently skip spans, so
    the replace must stay atomic: write a sibling temp file, then os.replace.
    """
    directory = path.parent if str(path.parent) else Path()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(timestamp)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
