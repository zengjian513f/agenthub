"""新建 CLI 落盘前的临时会话记录。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "agenthub"
PENDING_FILE = DATA_DIR / "pending-sessions.json"
_lock = threading.RLock()


def _read() -> list[dict]:
    try:
        data = json.loads(PENDING_FILE.read_text())
        return [x for x in data if isinstance(x, dict) and x.get("name")]
    except (OSError, ValueError, TypeError):
        return []


def _write(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(PENDING_FILE)


def put(row: dict) -> None:
    """按 tmux 名新增或覆盖一条记录。"""
    clean = dict(row)
    clean["before"] = sorted(set(clean.get("before") or []))
    with _lock:
        rows = [x for x in _read() if x["name"] != clean["name"]]
        rows.append(clean)
        _write(rows)


def get(name: str) -> dict | None:
    with _lock:
        row = next((x for x in _read() if x["name"] == name), None)
        return dict(row) if row else None


def resolve(name: str, result: dict) -> None:
    """短暂保留关联结果，避免多个浏览器轮询时互相抢掉响应。"""
    with _lock:
        rows = _read()
        for row in rows:
            if row["name"] == name:
                row["resolved"] = result
                row["resolved_at"] = time.time()
                _write(rows)
                return


def discard(name: str) -> None:
    with _lock:
        rows = _read()
        keep = [x for x in rows if x["name"] != name]
        if len(keep) != len(rows):
            _write(keep)


def active(tmux_names: set[str]) -> list[dict]:
    """返回仍在 tmux 中运行且尚未关联的记录，并清理过期完成项。"""
    now = time.time()
    with _lock:
        rows = _read()
        keep = [x for x in rows if
                (x.get("resolved_at") and now - float(x["resolved_at"]) < 600)
                or (not x.get("resolved_at") and
                    (x["name"] in tmux_names or now - float(x.get("started", now)) < 86400))]
        if len(keep) != len(rows):
            _write(keep)
        return [dict(x) for x in keep if not x.get("resolved") and x["name"] in tmux_names]
