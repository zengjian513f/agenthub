"""Privacy-preserving event trace for diagnosing cross-layer send failures."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
LOG_FILE = DATA_DIR / "send-events.jsonl"
MAX_BYTES = 16 * 1024 * 1024
_lock = threading.Lock()


def record(source: str, uid: str, request_id: str, event: str,
           text: str | None = None, **details) -> None:
    """Append one compact event without storing the user's prompt contents."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": str(source or ""),
        "uid": str(uid or ""),
        "request_id": str(request_id or ""),
        "event": str(event or ""),
    }
    if text is not None:
        encoded = str(text).encode("utf-8", "replace")
        row.update(text_sha256=hashlib.sha256(encoded).hexdigest(),
                   text_bytes=len(encoded))
    row.update({key: value for key, value in details.items()
                if value is not None and value != ""})
    try:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    except (TypeError, ValueError):
        return
    # Diagnostics must never change delivery semantics. A read-only home,
    # rotation failure, or full disk may lose an audit row but cannot turn a
    # successful ledger write into an ambiguous client-visible send failure.
    with _lock:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                if LOG_FILE.stat().st_size >= MAX_BYTES:
                    rotated = LOG_FILE.with_suffix(".jsonl.1")
                    try:
                        rotated.unlink()
                    except FileNotFoundError:
                        pass
                    LOG_FILE.replace(rotated)
            except FileNotFoundError:
                pass
            with LOG_FILE.open("a", encoding="utf-8") as stream:
                stream.write(line)
            os.chmod(LOG_FILE, 0o600)
        except OSError:
            return
