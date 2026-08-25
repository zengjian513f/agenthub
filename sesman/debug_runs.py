"""Keep paid monkey sessions out of ordinary sesman views.

The registry is process-independent: a monkey writes its root before starting
any CLI, while the already-running server reloads the small file by mtime.
Crashes deliberately leave the registration behind so test transcripts never
leak into the user's normal session list on the next refresh.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
REGISTRY_FILE = DATA_DIR / "debug-runs.json"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.RLock()
_cache_mtime: int | None = None
_cache: dict = {"version": 1, "runs": {}}


def _valid_id(run_id: str) -> str:
    value = str(run_id or "")
    if not _RUN_ID.fullmatch(value):
        raise ValueError("debug_run 不合法")
    return value


def _read() -> dict:
    global _cache_mtime, _cache
    try:
        mtime = REGISTRY_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime == _cache_mtime:
        return _cache
    try:
        raw = json.loads(REGISTRY_FILE.read_text())
        runs = raw.get("runs") if isinstance(raw, dict) else None
        data = {"version": 1, "runs": runs if isinstance(runs, dict) else {}}
    except (OSError, ValueError, TypeError):
        data = {"version": 1, "runs": {}}
    _cache_mtime, _cache = mtime, data
    return data


def _write(data: dict) -> None:
    global _cache_mtime, _cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(REGISTRY_FILE)
    _cache_mtime = REGISTRY_FILE.stat().st_mtime_ns
    _cache = data


def register(run_id: str, root: str | Path) -> dict:
    run_id = _valid_id(run_id)
    root = str(Path(root).expanduser().resolve(strict=False))
    if not Path(root).is_absolute():
        raise ValueError("debug root 必须是绝对路径")
    with _lock:
        data = _read()
        runs = dict(data["runs"])
        current = dict(runs.get(run_id) or {})
        if current.get("root") and current["root"] != root:
            raise ValueError("同一 debug_run 不能更换根目录")
        current.update({"root": root, "created": current.get("created") or time.time()})
        current.setdefault("sessions", [])
        runs[run_id] = current
        data = {"version": 1, "runs": runs}
        _write(data)
        return dict(current)


def add_session(run_id: str, *, source: str, cwd: str,
                sid: str = "", uid: str = "", name: str = "") -> None:
    run_id = _valid_id(run_id)
    with _lock:
        data = _read()
        runs = dict(data["runs"])
        if run_id not in runs:
            raise KeyError(run_id)
        run = dict(runs[run_id])
        rows = [dict(row) for row in run.get("sessions", [])
                if isinstance(row, dict)]
        identity = (str(source), str(sid), str(name), str(cwd))
        row = next((item for item in rows if (
            str(item.get("source")), str(item.get("sid")),
            str(item.get("name")), str(item.get("cwd"))) == identity), None)
        if row is None:
            row = {"source": str(source), "cwd": str(cwd),
                   "sid": str(sid), "uid": str(uid), "name": str(name)}
            rows.append(row)
        elif uid:
            row["uid"] = str(uid)
        run["sessions"] = rows
        runs[run_id] = run
        _write({"version": 1, "runs": runs})


def remove(run_id: str) -> bool:
    run_id = _valid_id(run_id)
    with _lock:
        data = _read()
        runs = dict(data["runs"])
        existed = runs.pop(run_id, None) is not None
        if existed:
            _write({"version": 1, "runs": runs})
        return existed


def get(run_id: str) -> dict | None:
    try:
        run_id = _valid_id(run_id)
    except ValueError:
        return None
    with _lock:
        row = _read()["runs"].get(run_id)
        return dict(row) if isinstance(row, dict) else None


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(path), root)) == root
    except (OSError, ValueError, TypeError):
        return False


def run_for(row: dict) -> str | None:
    uid = str(row.get("uid") or "")
    sid = str(row.get("sid") or "")
    name = str(row.get("name") or "")
    cwd = str(row.get("cwd") or "")
    with _lock:
        runs = _read()["runs"]
        for run_id, run in runs.items():
            root = str(run.get("root") or "")
            if root and cwd and _under(cwd, root):
                return run_id
            for item in run.get("sessions", []):
                if uid and uid == str(item.get("uid") or ""):
                    return run_id
                if sid and sid == str(item.get("sid") or ""):
                    return run_id
                if name and name == str(item.get("name") or ""):
                    return run_id
    return None


def filter_rows(rows: list[dict], run_id: str = "") -> list[dict]:
    """Normal views exclude every debug run; a debug view shows only its run."""
    if run_id and get(run_id) is None:
        return []
    return [row for row in rows
            if ((run_for(row) == run_id) if run_id else run_for(row) is None)]

