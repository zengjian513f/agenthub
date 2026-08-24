"""Server-owned delivery ledger for messages submitted to Claude Code.

tmux accepting a paste is not the same thing as Claude accepting the prompt.  The
ledger is deliberately kept after terminal submission and is only retired by a
causally newer native ``user``/``command`` record.  Unlike the old browser timer,
an acknowledgement delay never turns into an unsafe blind retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import send_audit


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
QUEUE_FILE = DATA_DIR / "claude-send-queue.json"
VERSION = 1
TOMBSTONE_SECONDS = 7 * 24 * 60 * 60
CONFIRM_TIMEOUT = 8.0
APPENDED_DRAFT_MAX_DELAY = 2.0
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
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict) and row.get("id")]


def _write(rows: list[dict]) -> None:
    global _revision
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    payload = json.dumps(
        {"version": VERSION, "items": rows}, ensure_ascii=False, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(QUEUE_FILE)
    # The file is already safe for process crashes. Persisting the rename across
    # a sudden power loss is best effort because some filesystems reject dir fsync.
    try:
        directory = os.open(QUEUE_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass
    _revision += 1


def revision() -> int:
    with _lock:
        return _revision


def _public(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "id", "uid", "text", "media", "created", "state", "error", "attempts"
    ) if row.get(key) is not None} | {"server": True}


def list_for(uid: str) -> list[dict]:
    with _lock:
        rows = [row for row in _read()
                if row.get("uid") == uid and row.get("state") != "confirmed"]
    rows.sort(key=lambda row: (float(row.get("created") or 0),
                               str(row.get("id") or "")))
    return [_public(row) for row in rows]


def lookup(request_id: str, uid: str, text: str) -> dict | None:
    """把重复 HTTP 请求识别成状态查询，且不触碰当前终端草稿。"""
    item_id = str(request_id or "")[:128]
    if not item_id:
        return None
    uid, text = str(uid or ""), str(text or "")
    with _lock:
        row = next((item for item in _read() if item.get("id") == item_id), None)
        if not row:
            return None
        same_text = (row.get("text") == text if row.get("text") is not None
                     else row.get("text_sha256") == _text_hash(text))
        if row.get("uid") != uid or not same_text:
            raise ValueError("重复发送 ID 对应了不同消息")
        return _public(row)


def snapshot(uid: str) -> dict:
    with _lock:
        rows = [row for row in _read()
                if row.get("uid") == uid and row.get("state") != "confirmed"]
        rows.sort(key=lambda row: (float(row.get("created") or 0),
                                   str(row.get("id") or "")))
        return {
            "outbox": [_public(row) for row in rows],
            "outbox_version": {"epoch": _epoch, "revision": _revision},
        }


def tracked() -> list[dict]:
    with _lock:
        return [dict(row) for row in _read()
                if row.get("state") != "confirmed"]


def ready() -> list[dict]:
    """Rows durably saved before any terminal write are safe to claim once."""
    with _lock:
        return [dict(row) for row in _read()
                if row.get("state") == "persisted"]


def enqueue(uid: str, name: str, text: str, media: list | None,
            request_id: str, cursor: dict | None = None,
            client: dict | None = None) -> tuple[dict, bool]:
    """Persist before touching tmux and deduplicate repeated HTTP requests."""
    uid, name, text = str(uid or ""), str(name or ""), str(text or "")
    if not uid or not name or not text.strip():
        raise ValueError("缺少 uid、终端名或消息正文")
    item_id = str(request_id or uuid.uuid4())[:128]
    with _lock:
        rows = _read()
        now = time.time()
        rows = [row for row in rows if not (
            row.get("state") == "confirmed"
            and now - float(row.get("confirmed_at") or 0) > TOMBSTONE_SECONDS)]
        existing = next((row for row in rows if row.get("id") == item_id), None)
        if existing:
            same_text = (existing.get("text") == text
                         if existing.get("text") is not None else
                         existing.get("text_sha256") == _text_hash(text))
            if existing.get("uid") != uid or not same_text:
                raise ValueError("重复发送 ID 对应了不同消息")
            return _public(existing), False
        row = {
            "id": item_id,
            "uid": uid,
            "name": name,
            "text": text,
            "text_sha256": _text_hash(text),
            "media": list(media or []),
            "created": int(time.time() * 1000),
            "state": "persisted",
            "attempts": 0,
            # Cursor is the authoritative causal boundary.  A server timestamp is
            # only a fallback for sessions opened by an older client without one.
            "after_ts": datetime.now().astimezone().isoformat(),
            "client": dict(client or {}),
        }
        _put_cursor(row, cursor)
        _snapshot_confirmation_cursor(row)
        rows.append(row)
        _write(rows)
        send_audit.record("claude", uid, item_id, "persisted", text,
                          name=name, **dict(client or {}))
        return _public(row), True


def mark_injecting(item_id: str, uid: str) -> dict | None:
    def change(row):
        if row.get("state") != "persisted":
            return
        row.update(state="injecting", attempts=int(row.get("attempts") or 0) + 1,
                   injecting_at=time.time())
        row.pop("error", None)
    result = _update(item_id, uid, change)
    if result:
        send_audit.record("claude", uid, item_id, "terminal_injecting",
                          result.get("text"))
    return result


def mark_submitted(item_id: str, uid: str) -> dict | None:
    def change(row):
        if row.get("state") != "injecting":
            return
        row.update(state="submitted", submitted_at=time.time())
    result = _update(item_id, uid, change)
    if result:
        send_audit.record("claude", uid, item_id, "terminal_submitted",
                          result.get("text"))
    return result


def mark_ambiguous(item_id: str, uid: str, error: str) -> dict | None:
    """An exception after injection begins can never be advertised as retryable."""
    def change(row):
        row.update(state="ambiguous",
                   error=str(error or "终端提交结果无法确认，请先检查会话"))
    result = _update(item_id, uid, change)
    if result:
        send_audit.record("claude", uid, item_id, "terminal_ambiguous",
                          result.get("text"), error=str(error or ""))
    return result


def observe(uid: str, messages: list[dict] | None,
            _activity: dict | None = None, cursor: dict | None = None) -> bool:
    """Advance/retire ledger rows from Claude's native transcript protocol."""
    # A full reset may replay old custom-title records. They have no native
    # timestamp, so they are only causal evidence when read incrementally from
    # the server-owned pre-injection cursor.
    allow_timeless = not (isinstance(cursor, dict) and cursor.get("reset"))
    with _lock:
        rows = _read()
        if not any(row.get("uid") == uid for row in rows):
            return False
        changed = False
        for message in messages or []:
            role = str(message.get("role") or "")
            operation = str(message.get("operation") or "")
            text = str(message.get("text") or "")
            if role == "event" and message.get("event_kind") == "compact":
                # Current Claude Code rewrites /compact onto a new summary root.
                # The original user command may leave the selected lineage, but
                # the compact completion event is durable causal acknowledgement.
                at = _matching_row(rows, uid, "/compact", message.get("ts"),
                                   allow_timeless=allow_timeless)
                if at is not None:
                    matched = rows[at]
                    send_audit.record(
                        "claude", uid, str(matched.get("id") or ""),
                        "native_compacted", matched.get("text"),
                        native_ts=message.get("ts"))
                    _confirm(rows[at])
                    changed = True
                continue
            if role in {"user", "command"}:
                at = _matching_row(rows, uid, text, message.get("ts"),
                                   allow_timeless=allow_timeless)
                appended_prefix_bytes = None
                if at is None and role == "user":
                    appended = _matching_appended_draft_row(
                        rows, uid, text, message.get("ts"))
                    if appended is not None:
                        at, appended_prefix_bytes = appended
                if at is not None:
                    matched = rows[at]
                    event = ("native_committed_appended_draft"
                             if appended_prefix_bytes is not None
                             else "native_committed")
                    send_audit.record(
                        "claude", uid, str(matched.get("id") or ""), event,
                        matched.get("text"), native_ts=message.get("ts"),
                        appended_prefix_bytes=appended_prefix_bytes)
                    _confirm(rows[at])
                    changed = True
                continue
            if role != "queue_operation":
                continue
            if operation == "enqueue":
                at = _matching_row(rows, uid, text, message.get("ts"),
                                   states={"persisted", "injecting", "submitted",
                                           "ambiguous"})
                if at is not None and rows[at].get("state") != "native_queued":
                    rows[at].update(state="native_queued")
                    rows[at].pop("error", None)
                    send_audit.record("claude", uid,
                                      str(rows[at].get("id") or ""),
                                      "native_queued", rows[at].get("text"),
                                      native_ts=message.get("ts"))
                    changed = True
            elif operation == "remove":
                at = _matching_row(rows, uid, text, message.get("ts"))
                if at is not None:
                    matched = rows[at]
                    send_audit.record("claude", uid, str(matched.get("id") or ""),
                                      "native_removed", matched.get("text"),
                                      native_ts=message.get("ts"))
                    _confirm(rows[at])
                    changed = True
            elif operation == "dequeue":
                row = next((row for row in rows if row.get("uid") == uid
                            and row.get("state") == "native_queued"
                            and _causal(message.get("ts"), row.get("after_ts"))), None)
                if row:
                    row["state"] = "submitted"
                    changed = True
            elif operation == "popAll":
                for row in rows:
                    if (row.get("uid") == uid and row.get("state") == "native_queued"
                            and _causal(message.get("ts"), row.get("after_ts"))):
                        row["state"] = "submitted"
                        changed = True
        if cursor:
            for row in rows:
                if row.get("uid") == uid and row.get("state") != "confirmed":
                    changed = _put_cursor(row, cursor) or changed
        if changed:
            _write(rows)
        return changed


def discard(item_id: str, uid: str) -> bool:
    """Only hide/retire a submitted row; never imply that Claude was cancelled."""
    with _lock:
        rows = _read()
        row = next((row for row in rows if row.get("id") == item_id
                    and row.get("uid") == uid
                    and row.get("state") != "confirmed"), None)
        if not row:
            return False
        send_audit.record("claude", uid, item_id, "ui_retired", row.get("text"))
        _confirm(row)
        _write(rows)
        return True


def discard_uid(uid: str, tombstone: bool = False) -> bool:
    with _lock:
        rows = _read()
        if tombstone:
            changed = False
            for row in rows:
                if row.get("uid") == uid and row.get("state") != "confirmed":
                    _confirm(row)
                    changed = True
            if changed:
                _write(rows)
            return changed
        kept = [row for row in rows if row.get("uid") != uid]
        if len(kept) == len(rows):
            return False
        _write(kept)
        return True


def _update(item_id: str, uid: str, update) -> dict | None:
    with _lock:
        rows = _read()
        row = next((row for row in rows if row.get("id") == item_id
                    and row.get("uid") == uid), None)
        if not row:
            return None
        before = dict(row)
        update(row)
        if row == before:
            return None
        _write(rows)
        return _public(row)


def _matching_row(rows: list[dict], uid: str, text: str, recorded,
                  states: set[str] | None = None,
                  allow_timeless: bool = True) -> int | None:
    if not recorded and not allow_timeless:
        return None
    key = _prompt_key(text)
    for i, row in enumerate(rows):
        row_key = _prompt_key(row.get("text"))
        # 裸 /rename 会打开 Claude 的命名交互；最终只有带名称的
        # custom-title 协议记录。该记录来自 watch_start 之后，因此可以确认
        # 这个命令已经被 Claude 接收，而不是拿历史标题碰运气。
        rename_dialog = row_key == "/rename" and key.startswith("/rename ")
        if row.get("uid") != uid or (row_key != key and not rename_dialog):
            continue
        if states is not None and row.get("state") not in states:
            continue
        if _causal(recorded, row.get("after_ts")):
            return i
    return None


def _matching_appended_draft_row(rows: list[dict], uid: str, text: str,
                                 recorded) -> tuple[int, int] | None:
    """Match a web prompt appended by Claude to a pre-existing composer draft.

    This is deliberately narrower than ordinary prompt matching: only an exact
    suffix, only terminal-submitted rows, and only a native record written within
    two seconds of the server's pre-injection boundary.  It repairs the historical
    ESC-draft failure without allowing a coincidentally similar later prompt to
    acknowledge an old ledger row.
    """
    native_key = _prompt_key(text)
    if not native_key or not recorded:
        return None
    candidates: list[tuple[float, int, int]] = []
    for i, row in enumerate(rows):
        row_key = _prompt_key(row.get("text"))
        if (row.get("uid") != uid
                or row.get("state") not in {"injecting", "submitted", "ambiguous"}
                or not row_key or native_key == row_key
                or not native_key.endswith(row_key)):
            continue
        delay = _timestamp_delta(recorded, row.get("after_ts"))
        if delay is None or not 0 <= delay <= APPENDED_DRAFT_MAX_DELAY:
            continue
        prefix = native_key[:-len(row_key)]
        if not prefix:
            continue
        candidates.append((delay, i, len(prefix.encode("utf-8", "replace"))))
    if not candidates:
        return None
    _, index, prefix_bytes = min(candidates)
    return index, prefix_bytes


def _prompt_key(value) -> str:
    # Claude records the submitted prompt without the editor's outer whitespace.
    return str(value or "").strip()


def _text_hash(value) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()


def _confirm(row: dict) -> None:
    """Keep an invisible idempotency tombstone without retaining prompt text."""
    row.update(state="confirmed", confirmed_at=time.time())
    for key in ("text", "media", "name", "error", "client"):
        row.pop(key, None)


def _causal(recorded, boundary) -> bool:
    if not recorded or not boundary:
        return True
    try:
        left = datetime.fromisoformat(str(recorded).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(boundary).replace("Z", "+00:00"))
        return left.timestamp() >= right.timestamp()
    except (TypeError, ValueError):
        return True


def _timestamp_delta(recorded, boundary) -> float | None:
    """Return a strict native-minus-boundary delta for narrow causal matches."""
    if not recorded or not boundary:
        return None
    try:
        left = datetime.fromisoformat(str(recorded).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(boundary).replace("Z", "+00:00"))
        return left.timestamp() - right.timestamp()
    except (TypeError, ValueError):
        return None


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
    """Keep the pre-injection cursor even after the live watch cursor advances."""
    for source, target in (
        ("watch_start", "confirm_start"),
        ("watch_head", "confirm_head"),
        ("watch_anchor", "confirm_anchor"),
    ):
        if row.get(source) is not None:
            row[target] = row[source]
        else:
            row.pop(target, None)
