"""AgentHub's durable receipt ledger for text submitted to the Codex TUI.

The live TUI accepts follow-up input while a turn is running and owns its queue.
AgentHub records only the ambiguous paste boundary and reconciles it against the
native rollout; it never waits locally for the TUI to become idle.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import audit


DATA_DIR = Path.home() / ".local" / "share" / "agenthub"
QUEUE_FILE = DATA_DIR / "send-queue.json"
VERSION = 1
CONFIRM_TIMEOUT = 8.0
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
    before = _read()
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"version": VERSION, "items": rows}, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(QUEUE_FILE)
    _revision += 1
    audit.record_ledger_changes("codex", before, rows)


def revision() -> int:
    with _lock:
        return _revision


def fail_unsubmitted() -> int:
    """Make pre-fix/local-wait rows honest after a process restart.

    ``queued`` was never proof of a tmux write, and ``native_*`` referred to a
    separate short-lived app-server rather than the live TUI.  Neither may be
    silently injected later because newer terminal input may already have
    overtaken it.
    """
    with _lock:
        rows = _read()
        changed = 0
        for row in rows:
            if row.get("state") not in {
                    "queued", "injecting", "native_queuing", "native_queued"}:
                continue
            row.update(
                state="failed",
                error="消息未写入 Codex 终端，请重试或移除",
            )
            for key in (
                "ready_at", "native_id", "native_claimed_at",
                "native_queued_at", "native_retry_at",
            ):
                row.pop(key, None)
            changed += 1
        if changed:
            _write(rows)
        return changed


def _public(row: dict) -> dict:
    public = {k: row.get(k) for k in (
        "id", "uid", "text", "media", "created", "state", "error", "attempts"
    ) if row.get(k) is not None} | {"server": True}
    if row.get("after_ts") is not None:
        public["afterTs"] = row["after_ts"]
    return public


def list_for(uid: str) -> list[dict]:
    with _lock:
        rows = [x for x in _read() if x.get("uid") == uid]
    rows.sort(key=lambda x: (float(x.get("created") or 0), str(x.get("id") or "")))
    return [_public(x) for x in rows]


def lookup(item_id: str, uid: str = "", text: str | None = None) -> dict | None:
    """Return an existing public request without mutating a later TUI draft."""
    with _lock:
        row = next((x for x in _read() if x.get("id") == str(item_id or "")
                    and (not uid or x.get("uid") == uid)), None)
        if row and text is not None and row.get("text") != text:
            raise ValueError("重复发送 ID 对应了不同消息")
        return _public(row) if row else None


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
    """返回每个会话最早的未确认回执，供后台独立追踪 rollout。"""
    with _lock:
        rows = sorted((row for row in _read()
                       if row.get("state") != "aborted"),
                      key=lambda x: float(x.get("created") or 0))
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
            "state": "injecting", "activity_state": state,
            "activity_ts": (activity or {}).get("ts"),
            # 确认边界使用服务端入队时间，而不是可能偏时的浏览器时钟，
            # 也不能使用上一回合 activity 的旧时间。
            "after_ts": datetime.now(timezone.utc).isoformat(), "attempts": 0,
        }
        _put_cursor(row, cursor)
        rows.append(row)
        _write(rows)
        return _public(row)


def observe(uid: str, messages: list[dict] | None,
            activity: dict | None, now: float | None = None,
            cursor: dict | None = None) -> bool:
    """用原生会话增量确认已经提交给 TUI 的消息。"""
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
                       # 回执必须按它的实际输入语义确认，正文仍保留原样显示和投递。
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
            active = [row for row in mine if row.get("state") != "aborted"]
            for row in active:
                if row.get("activity_state") != state or row.get("activity_ts") != activity.get("ts"):
                    row["activity_state"] = state
                    row["activity_ts"] = activity.get("ts")
                    changed = True
        if cursor and mine:
            for row in mine:
                changed = _put_cursor(row, cursor) or changed
        if changed:
            _write(rows)
        return changed


def mark_delivering(item_id: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        rows = _read()
        row = next((x for x in rows if x.get("id") == item_id), None)
        if not row or row.get("state") != "injecting":
            return False
        row.update(state="delivering", delivered_at=now,
                   attempts=int(row.get("attempts") or 0) + 1)
        _snapshot_confirmation_cursor(row)
        row.pop("ready_at", None)
        row.pop("error", None)
        row.pop("composer_attempts", None)
        row.pop("composer_editing_signature", None)
        row.pop("composer_editing_attempts", None)
        _write(rows)
        return True


def mark_failed(item_id: str, error: str) -> None:
    def change(row):
        row.update(state="failed", error=str(error or "发送未确认"))
        row.pop("ready_at", None)
    _update(item_id, change)


def mark_confirming(item_id: str, error: str = "") -> None:
    """Keep tracking a terminal write whose native record has not appeared yet.

    Once ``mark_delivering`` has claimed a row, paste/Enter may already have
    reached Codex.  A slow compact or an exception after that point is
    ambiguous, never a retryable failure: exposing Retry can submit the same
    prompt twice.
    """
    def change(row):
        row.update(
            state="confirming",
            error=str(error or "已送达终端，等待 Codex 写入会话记录"),
        )
        row.pop("ready_at", None)
    _update(item_id, change)


def mark_interrupted(uid: str, now: float | None = None) -> bool:
    """Never infer that a TUI-accepted follow-up was cancelled by idle state.

    An Escape may abort the currently running turn while a later prompt remains
    owned by the TUI.  Only its native user record can retire the receipt.
    """
    return False


def expire_deliveries(now: float | None = None) -> int:
    """Move overdue terminal writes into a non-retryable confirmation state.

    Codex can emit ``task_started`` and then spend tens of seconds compacting
    before persisting the corresponding user message.  The old eight-second
    timeout changed the row to ``failed`` and offered Retry while the original
    request was already running.  Keep polling from the fixed delivery cursor
    instead; only native confirmation will retire the row automatically.
    """
    now = time.time() if now is None else now
    with _lock:
        rows = _read()
        changed = 0
        for row in rows:
            if (row.get("state") == "delivering"
                    and now - float(row.get("delivered_at") or now) >= CONFIRM_TIMEOUT):
                row["state"] = "confirming"
                row["error"] = "已送达终端，等待 Codex 写入会话记录"
                changed += 1
        if changed:
            _write(rows)
        return changed


def retry(item_id: str, activity: dict | None = None, uid: str = "") -> dict | None:
    state = str((activity or {}).get("state") or "")
    with _lock:
        rows = _read()
        row = next((x for x in rows if x.get("id") == item_id
                    and (not uid or x.get("uid") == uid)), None)
        # Only failures proven to have happened before the terminal write are
        # safe to retry.  attempts > 0 means mark_delivering already won the
        # atomic claim; a stale tab or direct API call must not duplicate it.
        if (not row or row.get("state") != "failed"
                or int(row.get("attempts") or 0) > 0):
            return None
        row.update(state="injecting", activity_state=state,
                   activity_ts=(activity or {}).get("ts"))
        row.pop("error", None)
        row.pop("delivered_at", None)
        row.pop("composer_attempts", None)
        row.pop("composer_editing_signature", None)
        row.pop("composer_editing_attempts", None)
        row["after_ts"] = datetime.now(timezone.utc).isoformat()
        row.pop("ready_at", None)
        _write(rows)
        return _public(row)


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
