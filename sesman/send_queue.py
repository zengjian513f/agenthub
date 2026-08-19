"""Sesman 托管的 Codex 网页发送队列。

Codex 忙时不会把排队输入写进 rollout，tmux send-keys 成功也不等于 CLI
已经接收。这里先持久化网页消息，只在原生回合结束且终端画面稳定后交付，
最终仍以 rollout 中出现同文 user 消息作为确认。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
QUEUE_FILE = DATA_DIR / "send-queue.json"
VERSION = 1
READY_DELAY = 0.30
CONFIRM_TIMEOUT = 8.0
COMPOSER_RETRY_DELAY = 0.50
COMPOSER_RETRY_LIMIT = 12
COMPOSER_EDITING_RETRY_LIMIT = 4
_lock = threading.RLock()
_revision = 0
_epoch = uuid.uuid4().hex


def _read() -> list[dict]:
    try:
        data = json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return []
    rows = data.get("items")
    return [dict(x) for x in rows if isinstance(x, dict) and x.get("id")]


def _write(rows: list[dict]) -> None:
    global _revision
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"version": VERSION, "items": rows}, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(QUEUE_FILE)
    _revision += 1


def revision() -> int:
    with _lock:
        return _revision


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in (
        "id", "uid", "text", "media", "created", "state", "error", "attempts"
    ) if row.get(k) is not None} | {"server": True}


def list_for(uid: str) -> list[dict]:
    with _lock:
        rows = [x for x in _read() if x.get("uid") == uid]
    rows.sort(key=lambda x: (float(x.get("created") or 0), str(x.get("id") or "")))
    return [_public(x) for x in rows]


def snapshot(uid: str) -> dict:
    """Return one atomic public snapshot and its process-scoped ordering token."""
    with _lock:
        rows = [x for x in _read() if x.get("uid") == uid]
        rows.sort(key=lambda x: (float(x.get("created") or 0),
                                 str(x.get("id") or "")))
        return {
            "outbox": [_public(x) for x in rows],
            "outbox_version": {"epoch": _epoch, "revision": _revision},
        }


def tracked() -> list[dict]:
    """返回每个会话的队首，供后台独立追踪原生 rollout。"""
    with _lock:
        rows = sorted(_read(), key=lambda x: float(x.get("created") or 0))
    first: dict[str, dict] = {}
    for row in rows:
        first.setdefault(str(row.get("uid") or ""), row)
    return [dict(row) for row in first.values()]


def enqueue(uid: str, name: str, text: str, media: list | None,
            activity: dict | None, request_id: str = "",
            cursor: dict | None = None) -> dict:
    uid, name, text = str(uid or ""), str(name or ""), str(text or "")
    if not uid or not name or not text.strip():
        raise ValueError("缺少 uid、终端名或消息正文")
    now = time.time()
    state = str((activity or {}).get("state") or "")
    item_id = str(request_id or uuid.uuid4())[:128]
    with _lock:
        rows = _read()
        existing = next((x for x in rows if x.get("id") == item_id), None)
        if existing:
            if existing.get("uid") != uid or existing.get("text") != text:
                raise ValueError("重复发送 ID 对应了不同消息")
            return _public(existing)
        row = {
            "id": item_id, "uid": uid, "name": name, "text": text,
            "media": list(media or []), "created": int(now * 1000),
            "state": "queued", "activity_state": state,
            "activity_ts": (activity or {}).get("ts"),
            # 确认边界使用服务端入队时间，而不是可能偏时的浏览器时钟，
            # 也不能使用上一回合 activity 的旧时间。
            "after_ts": datetime.now(timezone.utc).isoformat(), "attempts": 0,
        }
        _put_cursor(row, cursor)
        if state in {"idle", "aborted", "failed"}:
            row["ready_at"] = now + READY_DELAY
        rows.append(row)
        _write(rows)
        return _public(row)


def observe(uid: str, messages: list[dict] | None,
            activity: dict | None, now: float | None = None,
            cursor: dict | None = None) -> bool:
    """用原生会话增量确认消息，并在回合结束后放行队首。"""
    now = time.time() if now is None else now
    native = [m for m in (messages or []) if m.get("role") in {"user", "command"}]
    with _lock:
        rows = _read()
        mine = [x for x in rows if x.get("uid") == uid]
        if not mine:
            return False
        changed = False
        for message in native:
            at = next((i for i, row in enumerate(rows)
                       if row.get("uid") == uid
                       # Codex TUI 会去掉提交内容两端的空白再写 rollout；
                       # 队列必须按它的实际输入语义确认，正文仍保留原样显示和投递。
                       # 只 strip 两端，不能折叠内部空格或换行。
                       and _prompt_key(row.get("text"))
                           == _prompt_key(message.get("text"))
                       and _causal(message.get("ts"), row.get("after_ts"))), None)
            if at is not None:
                rows.pop(at)
                changed = True
        mine = [x for x in rows if x.get("uid") == uid]
        if activity and mine:
            state = str(activity.get("state") or "")
            for row in mine:
                if row.get("activity_state") != state or row.get("activity_ts") != activity.get("ts"):
                    row["activity_state"] = state
                    row["activity_ts"] = activity.get("ts")
                    changed = True
                # 看到忙态时，即使事件发生在入队前，也说明客户端提供的 idle
                # 已经过期；必须撤销尚未执行的 ready_at。
                if state in {"working", "waiting"} and row.get("state") == "queued":
                    if row.pop("ready_at", None) is not None:
                        changed = True
            if state in {"idle", "aborted", "failed"}:
                first = mine[0]
                if (first.get("state") == "queued" and not first.get("ready_at")
                        and _causal(activity.get("ts"), first.get("after_ts"))):
                    first["ready_at"] = now + READY_DELAY
                    changed = True
        if cursor and mine:
            for row in mine:
                changed = _put_cursor(row, cursor) or changed
        if changed:
            _write(rows)
        return changed


def ready(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    with _lock:
        rows = _read()
    first: dict[str, dict] = {}
    for row in sorted(rows, key=lambda x: float(x.get("created") or 0)):
        first.setdefault(str(row.get("uid") or ""), row)
    return [dict(row) for row in first.values()
            if row.get("state") == "queued"
            and float(row.get("ready_at") or float("inf")) <= now]


def defer(item_id: str, delay: float = READY_DELAY) -> None:
    _update(item_id, lambda row: row.update(ready_at=time.time() + delay))


def defer_unrecognized(item_id: str) -> bool:
    """Retry a transiently unrecognisable Codex frame before failing visibly."""
    def change(row):
        row.pop("composer_editing_signature", None)
        row.pop("composer_editing_attempts", None)
        attempts = int(row.get("composer_attempts") or 0) + 1
        row["composer_attempts"] = attempts
        if attempts >= COMPOSER_RETRY_LIMIT:
            row.update(state="failed",
                       error="Codex 输入框不可识别，请打开终端后重试")
            row.pop("ready_at", None)
        else:
            row["ready_at"] = time.time() + COMPOSER_RETRY_DELAY
    result = _update(item_id, change)
    return bool(result and result.get("state") == "queued")


def defer_editing(item_id: str, signature: str) -> bool:
    """Require several identical draft frames before declaring a real conflict.

    A resize can leave a stable historic ``›`` block on screen briefly while
    Codex redraws its composer.  Never paste into it, but do not permanently fail
    the outbox from that single observation either.
    """
    signature = str(signature or "")[:64]

    def change(row):
        previous = str(row.get("composer_editing_signature") or "")
        attempts = int(row.get("composer_editing_attempts") or 0) + 1 \
            if previous == signature else 1
        row["composer_editing_signature"] = signature
        row["composer_editing_attempts"] = attempts
        row.pop("composer_attempts", None)
        if attempts >= COMPOSER_EDITING_RETRY_LIMIT:
            row.update(state="failed",
                       error="终端草稿中有内容，请重试并选择是否覆盖")
            row.pop("ready_at", None)
        else:
            row["ready_at"] = time.time() + COMPOSER_RETRY_DELAY

    result = _update(item_id, change)
    return bool(result and result.get("state") == "queued")


def mark_delivering(item_id: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    def change(row):
        row.update(state="delivering", delivered_at=now,
                   attempts=int(row.get("attempts") or 0) + 1)
        _snapshot_confirmation_cursor(row)
        row.pop("ready_at", None)
        row.pop("error", None)
        row.pop("composer_attempts", None)
        row.pop("composer_editing_signature", None)
        row.pop("composer_editing_attempts", None)
    return _update(item_id, change) is not None


def mark_failed(item_id: str, error: str) -> None:
    def change(row):
        row.update(state="failed", error=str(error or "发送未确认"))
        row.pop("ready_at", None)
    _update(item_id, change)


def expire_deliveries(now: float | None = None) -> int:
    now = time.time() if now is None else now
    with _lock:
        rows = _read()
        changed = 0
        for row in rows:
            if (row.get("state") == "delivering"
                    and now - float(row.get("delivered_at") or now) >= CONFIRM_TIMEOUT):
                row["state"] = "failed"
                row["error"] = "Codex 未在会话记录中确认接收"
                changed += 1
        if changed:
            _write(rows)
        return changed


def retry(item_id: str, activity: dict | None = None, uid: str = "") -> dict | None:
    now = time.time()
    state = str((activity or {}).get("state") or "")
    def change(row):
        row.update(state="queued", activity_state=state,
                   activity_ts=(activity or {}).get("ts"))
        row.pop("error", None)
        row.pop("delivered_at", None)
        row.pop("composer_attempts", None)
        row.pop("composer_editing_signature", None)
        row.pop("composer_editing_attempts", None)
        row["after_ts"] = datetime.now(timezone.utc).isoformat()
        if state in {"idle", "aborted", "failed"}:
            row["ready_at"] = now + READY_DELAY
        else:
            row.pop("ready_at", None)
    return _update(item_id, change, uid)


def discard(item_id: str, uid: str = "",
            states: set[str] | None = None) -> bool:
    with _lock:
        rows = _read()
        kept = [x for x in rows if not (
            x.get("id") == item_id and (not uid or x.get("uid") == uid)
            and (states is None or x.get("state") in states))]
        if len(kept) == len(rows):
            return False
        _write(kept)
        return True


def discard_uid(uid: str) -> bool:
    with _lock:
        rows = _read()
        kept = [x for x in rows if x.get("uid") != uid]
        if len(kept) == len(rows):
            return False
        _write(kept)
        return True


def _update(item_id: str, update, uid: str = "") -> dict | None:
    with _lock:
        rows = _read()
        row = next((x for x in rows if x.get("id") == item_id
                    and (not uid or x.get("uid") == uid)), None)
        if not row:
            return None
        update(row)
        _write(rows)
        return _public(row)


def _causal(recorded, boundary) -> bool:
    """同文历史消息不能误确认刚入队的新副本。"""
    if not recorded or not boundary:
        return True
    try:
        left = datetime.fromisoformat(str(recorded).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(boundary).replace("Z", "+00:00"))
        return left.timestamp() >= right.timestamp()
    except (TypeError, ValueError):
        return True


def _prompt_key(value) -> str:
    """Codex 写入 rollout 前会裁掉 prompt 两端空白。"""
    return str(value or "").strip()


def _put_cursor(row: dict, cursor: dict | None) -> bool:
    if not isinstance(cursor, dict):
        return False
    try:
        start = max(0, int(cursor.get("start") or 0))
    except (TypeError, ValueError):
        return False
    values = {
        "watch_start": start,
        "watch_head": str(cursor.get("head") or "")[:128],
        "watch_anchor": str(cursor.get("anchor") or "")[:128],
    }
    if all(row.get(key) == value for key, value in values.items()):
        return False
    row.update(values)
    return True


def _snapshot_confirmation_cursor(row: dict) -> None:
    """保留实际注入前的游标，供确认超时前重新核对已经扫过的记录。"""
    for source, target in (
        ("watch_start", "confirm_start"),
        ("watch_head", "confirm_head"),
        ("watch_anchor", "confirm_anchor"),
    ):
        if row.get(source) is not None:
            row[target] = row[source]
        else:
            row.pop(target, None)
