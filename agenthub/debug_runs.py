"""Keep paid monkey sessions out of ordinary agenthub views.

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
from functools import lru_cache
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "agenthub"
REGISTRY_FILE = DATA_DIR / "debug-runs.json"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.RLock()
_cache_mtime: int | None = None
_cache: dict = {"version": 1, "runs": {}}
_index: dict | None = None
_index_of: dict | None = None          # the runs mapping the index was built from


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


@lru_cache(maxsize=4096)
def _normpath(path: str) -> str:
    return os.path.normpath(path)


def _abspath(path: str) -> str:
    """Session directories repeat heavily across rows, so cache the absolute form.

    Only already-absolute paths are cached: resolving a relative one depends on
    the process working directory, which must never be baked into the cache.
    """
    if os.path.isabs(path):
        return _normpath(path)
    return os.path.abspath(path)


def _build_index(runs: dict) -> dict:
    """Pre-resolve the registry into lookup tables keyed by session identity.

    Every list, live-status and terminal request filters the whole session list,
    so matching one row must not walk every run and every recorded session.
    Runs keep their registry order: the lowest order wins, exactly as the
    original per-run scan returned the first match.
    """
    roots: list[tuple[str, str, int, str]] = []
    tables: dict[str, dict[str, tuple[int, str]]] = {"uid": {}, "sid": {}, "name": {}}
    for order, (run_id, run) in enumerate(runs.items()):
        if not isinstance(run, dict):
            continue
        root = str(run.get("root") or "")
        # ``commonpath`` only ever returns a normalized absolute path, so a root
        # that is relative or unnormalized could never match and is dropped here.
        if root and root == os.path.normpath(root) and os.path.isabs(root):
            prefix = root if root.endswith(os.sep) else root + os.sep
            roots.append((root, prefix, order, run_id))
        for item in run.get("sessions") or ():
            if not isinstance(item, dict):
                continue
            for key, table in tables.items():
                value = str(item.get(key) or "")
                if value and value not in table:
                    table[value] = (order, run_id)
    return {"roots": roots, **tables}


def _runs_index() -> dict:
    global _index, _index_of
    with _lock:
        runs = _read()["runs"]
        if _index is None or _index_of is not runs:
            _index, _index_of = _build_index(runs), runs
        return _index


def _match(index: dict, row: dict) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for key in ("uid", "sid", "name"):
        value = str(row.get(key) or "")
        if not value:
            continue
        hit = index[key].get(value)
        if hit is not None and (best is None or hit[0] < best[0]):
            best = hit
    cwd = str(row.get("cwd") or "")
    if cwd and index["roots"]:
        path = _abspath(cwd)
        for root, prefix, order, run_id in index["roots"]:
            if best is not None and order >= best[0]:
                break                   # roots keep run order; none can win now
            if path == root or path.startswith(prefix):
                return (order, run_id)
    return best


def run_for(row: dict) -> str | None:
    hit = _match(_runs_index(), row)
    return hit[1] if hit else None


def filter_rows(rows: list[dict], run_id: str = "") -> list[dict]:
    """Normal views exclude every debug run; a debug view shows only its run."""
    if run_id and get(run_id) is None:
        return []
    index = _runs_index()
    if not (index["roots"] or index["uid"] or index["sid"] or index["name"]):
        return [] if run_id else list(rows)
    if run_id:
        return [row for row in rows
                if (_match(index, row) or (0, ""))[1] == run_id]
    return [row for row in rows if _match(index, row) is None]

