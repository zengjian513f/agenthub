"""sesman 自有的会话元数据，不修改任何 CLI 的原生会话文件。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
META_FILE = DATA_DIR / "session-meta.json"
VERSION = 1
_lock = threading.RLock()
_activity_revisions: dict[str, int] = {}


def _read() -> dict:
    try:
        data = json.loads(META_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    rows = data.get("sessions")
    return dict(rows) if isinstance(rows, dict) else {}


def _write(rows: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"version": VERSION, "sessions": rows}, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(META_FILE)


def activity_revision(uid: str) -> int:
    with _lock:
        return _activity_revisions.get(str(uid or ""), 0)


def _epoch(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def stop_activity(uid: str, now: float | None = None) -> dict:
    """记录网页发出的 Escape，截断此前没有原生结束事件的忙态。"""
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("缺少会话 uid")
    stopped = time.time() if now is None else float(now)
    stopped_iso = datetime.fromtimestamp(stopped, timezone.utc).isoformat()
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else {}
        rows[uid] = {**previous, "activity_stopped_at": stopped_iso}
        _write(rows)
        _activity_revisions[uid] = _activity_revisions.get(uid, 0) + 1
    return {
        "role": "status", "state": "aborted", "text": "aborted",
        "ts": stopped_iso, "reason": "网页发送 Escape",
    }


def stopped_activity(uid: str) -> dict | None:
    with _lock:
        row = _read().get(str(uid or ""), {})
    stopped = row.get("activity_stopped_at") if isinstance(row, dict) else None
    if _epoch(stopped) is None:
        return None
    return {
        "role": "status", "state": "aborted", "text": "aborted",
        "ts": stopped, "reason": "网页发送 Escape",
    }


def resolve_activity(uid: str, activity: dict | None) -> dict | None:
    """让 Escape 覆盖它之前的 working/waiting；之后的新回合不受影响。"""
    if not activity or activity.get("state") not in {"working", "waiting"}:
        return activity
    stopped = stopped_activity(uid)
    stopped_at = _epoch(stopped.get("ts")) if stopped else None
    activity_at = _epoch(activity.get("ts"))
    if stopped_at is None or (activity_at is not None and activity_at > stopped_at):
        return activity
    return stopped


def signature() -> str:
    """供会话列表 ETag 式签名使用；文件很小，直接散列可避免时间戳碰撞。"""
    with _lock:
        try:
            return hashlib.sha1(META_FILE.read_bytes()).hexdigest()[:16]
        except OSError:
            return "0"


def set_starred(uid: str, starred: bool) -> dict:
    """设置收藏状态，返回该会话当前的 sesman 元数据。"""
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("缺少会话 uid")
    with _lock:
        rows = _read()
        if starred:
            previous = rows.get(uid) if isinstance(rows.get(uid), dict) else {}
            row = {**previous, "starred": True,
                   "starred_at": previous.get("starred_at") or time.time()}
            rows[uid] = row
        else:
            previous = dict(rows.get(uid)) if isinstance(rows.get(uid), dict) else {}
            previous.pop("starred", None)
            previous.pop("starred_at", None)
            if previous:
                rows[uid] = previous
            else:
                rows.pop(uid, None)
            row = {"starred": False, "starred_at": None}
        _write(rows)
        return dict(row)


def discard(uid: str) -> None:
    with _lock:
        rows = _read()
        if uid in rows:
            rows.pop(uid, None)
            _write(rows)


def enrich_one(session: dict) -> dict:
    row = dict(session)
    with _lock:
        meta = _read().get(str(session.get("uid") or ""), {})
    if isinstance(meta, dict) and meta.get("starred"):
        row["starred"] = True
        row["starred_at"] = meta.get("starred_at")
    return row


def enrich(sessions: list[dict]) -> list[dict]:
    with _lock:
        metadata = _read()
    out = []
    for session in sessions:
        row = dict(session)
        meta = metadata.get(str(session.get("uid") or ""), {})
        if isinstance(meta, dict) and meta.get("starred"):
            row["starred"] = True
            row["starred_at"] = meta.get("starred_at")
        out.append(row)
    return out
