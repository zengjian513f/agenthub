"""agenthub 自有的会话元数据，不修改任何 CLI 的原生会话文件。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "agenthub"
META_FILE = DATA_DIR / "session-meta.json"
VERSION = 1
_lock = threading.RLock()
_activity_revisions: dict[str, int] = {}
_timeline_revisions: dict[str, int] = {}


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


def timeline_revision(uid: str) -> int:
    with _lock:
        return _timeline_revisions.get(str(uid or ""), 0)


def _epoch(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def stop_activity(uid: str, now: float | None = None,
                  reason: str = "网页发送 Escape", state: str = "aborted",
                  inferred: bool = False) -> dict:
    """记录已确认的停止点，截断此前没有原生结束事件的忙态。"""
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("缺少会话 uid")
    if state not in {"idle", "aborted"}:
        raise ValueError("无效的停止状态")
    stopped = time.time() if now is None else float(now)
    stopped_iso = datetime.fromtimestamp(stopped, timezone.utc).isoformat()
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else {}
        rows[uid] = {
            **previous, "activity_stopped_at": stopped_iso,
            "activity_stop_reason": str(reason or "会话已停止"),
            "activity_stop_state": state,
            "activity_stop_inferred": bool(inferred),
        }
        _write(rows)
        _activity_revisions[uid] = _activity_revisions.get(uid, 0) + 1
    return {
        "role": "status", "state": state, "text": state,
        "ts": stopped_iso, "reason": str(reason or "会话已停止"),
    }


def stopped_activity(uid: str) -> dict | None:
    with _lock:
        row = _read().get(str(uid or ""), {})
    stopped = row.get("activity_stopped_at") if isinstance(row, dict) else None
    if _epoch(stopped) is None:
        return None
    return {
        "role": "status", "state": str(row.get("activity_stop_state") or "aborted"),
        "text": str(row.get("activity_stop_state") or "aborted"),
        "ts": stopped,
        "reason": str(row.get("activity_stop_reason") or "网页发送 Escape"),
    }


def clear_inferred_activity_stop(uid: str) -> bool:
    """运行中的 TUI 推翻先前启发式停止时，只移除那次停止元数据。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else None
        if not previous:
            return False
        # 兼容修复部署前已经写下的启发式记录。显式网页 Escape 的理由不同，
        # 绝不能因为随后一次终端重绘就被清掉。
        inferred = bool(previous.get("activity_stop_inferred")) or (
            previous.get("activity_stop_reason") == "终端已结束或中断")
        if not inferred:
            return False
        row = dict(previous)
        for key in ("activity_stopped_at", "activity_stop_reason",
                    "activity_stop_state", "activity_stop_inferred"):
            row.pop(key, None)
        if row:
            rows[uid] = row
        else:
            rows.pop(uid, None)
        _write(rows)
        _activity_revisions[uid] = _activity_revisions.get(uid, 0) + 1
        return True


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


def begin_timeline_rewind(uid: str, from_tip: str, stale_end: int) -> dict:
    """记录 Claude 原生回滚选择器的起点，但尚不改变已显示时间线。"""
    uid = str(uid or "").strip()
    from_tip = str(from_tip or "").strip()
    if not uid or not from_tip or int(stale_end) < 0:
        raise ValueError("回滚起点无效")
    pending = {
        "from_tip": from_tip, "stale_end": int(stale_end),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else {}
        rows[uid] = {**previous, "timeline_rewind": pending}
        _write(rows)
    return dict(pending)


def pending_timeline_rewind(uid: str) -> dict | None:
    with _lock:
        row = _read().get(str(uid or ""), {})
    pending = row.get("timeline_rewind") if isinstance(row, dict) else None
    return dict(pending) if isinstance(pending, dict) else None


def finish_timeline_rewind(uid: str, tip: str) -> dict:
    """把终端已经确认的 Claude 叶子保存为 agenthub 的显示时间线。"""
    uid = str(uid or "").strip()
    tip = str(tip or "").strip()
    if not uid or not tip:
        raise ValueError("回滚叶子无效")
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else {}
        pending = previous.get("timeline_rewind")
        if not isinstance(pending, dict):
            raise ValueError("没有待确认的回滚")
        stale_end = int(pending.get("stale_end") or 0)
        row = {**previous, "timeline_tip": tip,
               "timeline_stale_end": stale_end}
        row.pop("timeline_rewind", None)
        rows[uid] = row
        _write(rows)
        _timeline_revisions[uid] = _timeline_revisions.get(uid, 0) + 1
    return {"tip": tip, "stale_end": stale_end}


def cancel_timeline_rewind(uid: str) -> None:
    uid = str(uid or "").strip()
    with _lock:
        rows = _read()
        previous = rows.get(uid) if isinstance(rows.get(uid), dict) else None
        if not previous or "timeline_rewind" not in previous:
            return
        row = dict(previous)
        row.pop("timeline_rewind", None)
        rows[uid] = row
        _write(rows)


def timeline(uid: str) -> dict | None:
    """返回已确认的显示叶子；pending 选择器绝不能提前改变历史。"""
    with _lock:
        row = _read().get(str(uid or ""), {})
    if not isinstance(row, dict) or not row.get("timeline_tip"):
        return None
    try:
        stale_end = int(row.get("timeline_stale_end") or 0)
    except (TypeError, ValueError):
        return None
    return {"tip": str(row["timeline_tip"]), "stale_end": stale_end}


def timeline_stamp(uid: str) -> tuple[str, int]:
    value = timeline(uid)
    return ((value or {}).get("tip", ""), int((value or {}).get("stale_end", 0)))


def signature() -> str:
    """供会话列表 ETag 式签名使用；文件很小，直接散列可避免时间戳碰撞。"""
    with _lock:
        try:
            return hashlib.sha1(META_FILE.read_bytes()).hexdigest()[:16]
        except OSError:
            return "0"


def set_starred(uid: str, starred: bool) -> dict:
    """设置收藏状态，返回该会话当前的 agenthub 元数据。"""
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


def set_fork_parent_visible(uid: str, visible: bool) -> dict:
    """持久化父会话的显式显示选择；没有标志时一律默认隐藏。"""
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("缺少会话 uid")
    with _lock:
        rows = _read()
        previous = dict(rows.get(uid)) if isinstance(rows.get(uid), dict) else {}
        if visible:
            previous["fork_parent_visible"] = True
            rows[uid] = previous
        else:
            previous.pop("fork_parent_visible", None)
            if previous:
                rows[uid] = previous
            else:
                rows.pop(uid, None)
        _write(rows)
    return {"fork_parent_visible": bool(visible)}


def fork_parent_uids(sessions: list[dict]) -> set[str]:
    """从同一来源的原生 sid 关系计算父会话，绝不把跨来源同名 sid 串起来。"""
    ancestors = {
        (str(row.get("source") or ""), str(row.get("forked_from_id") or ""))
        for row in sessions if row.get("forked_from_id")
    }
    return {
        str(row.get("uid") or "") for row in sessions
        if row.get("uid") and (str(row.get("source") or ""),
                               str(row.get("sid") or "")) in ancestors
    }


def is_fork_parent(uid: str, sessions: list[dict]) -> bool:
    return str(uid or "") in fork_parent_uids(sessions)


def snapshot(uid: str) -> dict:
    """取会话当前的自有元数据, 供删除前留档。"""
    with _lock:
        row = _read().get(str(uid or "").strip())
        return dict(row) if isinstance(row, dict) else {}


def restore(uid: str, meta: dict | None) -> None:
    """会话从回收站放回原位时补回删除前的元数据。"""
    uid = str(uid or "").strip()
    if not uid or not isinstance(meta, dict) or not meta:
        return
    with _lock:
        rows = _read()
        if uid in rows:
            return      # 同一 uid 已有新记录, 恢复不能把它盖掉
        rows[uid] = dict(meta)
        _write(rows)


def discard(uid: str) -> None:
    uid = str(uid or "").strip()
    with _lock:
        rows = _read()
        if uid in rows:
            rows.pop(uid, None)
            _write(rows)
        _activity_revisions.pop(uid, None)
        _timeline_revisions.pop(uid, None)


def enrich_one(session: dict, topology: list[dict] | None = None) -> dict:
    row = dict(session)
    with _lock:
        meta = _read().get(str(session.get("uid") or ""), {})
    if isinstance(meta, dict) and meta.get("starred"):
        row["starred"] = True
        row["starred_at"] = meta.get("starred_at")
    if topology is not None and str(session.get("uid") or "") in fork_parent_uids(topology):
        row["fork_parent"] = True
        row["fork_parent_visible"] = bool(
            isinstance(meta, dict) and meta.get("fork_parent_visible"))
    return row


def enrich(sessions: list[dict], topology: list[dict] | None = None) -> list[dict]:
    with _lock:
        metadata = _read()
    parents = fork_parent_uids(sessions if topology is None else topology)
    out = []
    for session in sessions:
        row = dict(session)
        meta = metadata.get(str(session.get("uid") or ""), {})
        if isinstance(meta, dict) and meta.get("starred"):
            row["starred"] = True
            row["starred_at"] = meta.get("starred_at")
        if str(session.get("uid") or "") in parents:
            row["fork_parent"] = True
            row["fork_parent_visible"] = bool(
                isinstance(meta, dict) and meta.get("fork_parent_visible"))
        out.append(row)
    return out
