"""Cross-layer diagnostic event log for sesman.

The audit trail is deliberately best-effort: losing diagnostics is preferable to
changing message-delivery semantics.  Callers therefore never see storage errors.
Events are ordered by a SQLite sequence; potentially large payloads are compressed
and deduplicated in a content-addressed blob table.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
DB_FILE = DATA_DIR / "audit.sqlite3"
RETENTION_DAYS = 14
QUEUE_LIMIT = 20_000
BATCH_SIZE = 128

_SECRET_KEYS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "api-key", "api_key", "apikey", "password", "passwd", "secret",
    "access-token", "access_token", "refresh-token", "refresh_token",
}
_SECRET_KEYS_NORMALIZED = {key.replace("_", "-") for key in _SECRET_KEYS}


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _redact(value: Any, depth: int = 0) -> Any:
    """Make JSON-safe diagnostic metadata without retaining credentials."""
    if depth > 12:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        clean = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            folded = key.casefold().replace("_", "-")
            clean[key] = "<redacted>" if folded in _SECRET_KEYS_NORMALIZED \
                else _redact(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, depth + 1) for item in value]
    return str(value)


def sanitize(value: Any) -> Any:
    """Public redaction helper for diagnostic manifests outside the database."""
    return _redact(value)


def _json_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return json.dumps(_redact(value), ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True).encode()


@dataclass(frozen=True)
class _QueuedEvent:
    row: tuple
    blob: tuple[str, str, int, bytes] | None


@dataclass(frozen=True)
class _Barrier:
    done: threading.Event


class EventStore:
    """Asynchronous, process-local writer with synchronous query/export APIs."""

    def __init__(self, path: str | Path = DB_FILE,
                 queue_limit: int = QUEUE_LIMIT):
        self.path = Path(path)
        self._queue: queue.Queue[_QueuedEvent | _Barrier | None] = queue.Queue(
            maxsize=max(1, queue_limit))
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        return self._dropped

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._start_lock:
            if self._closed or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._writer, name="sesman-audit", daemon=True)
            self._thread.start()

    def record(self, event: str, *, uid: str = "", source: str = "",
               category: str = "", severity: str = "info",
               trace_id: str = "", request_id: str = "", page_id: str = "",
               connection_id: str = "", build: str = "", data: Any = None,
               content: Any = None) -> bool:
        """Queue one event.  This method never raises or blocks request handling."""
        try:
            now = time.time()
            clean_data = _redact(data if data is not None else {})
            data_json = json.dumps(clean_data, ensure_ascii=False,
                                   separators=(",", ":"), sort_keys=True)
            blob = None
            blob_hash = ""
            if content is not None:
                raw = _json_bytes(content)
                blob_hash = hashlib.sha256(raw).hexdigest()
                mime = "application/json" if not isinstance(content, (str, bytes)) \
                    else "text/plain; charset=utf-8"
                blob = (blob_hash, mime, len(raw), zlib.compress(raw, level=3))
            row = (
                now, _utc_iso(now), time.monotonic_ns(), os.getpid(),
                threading.get_ident(), str(event or "unknown")[:160],
                str(category or "")[:80], str(severity or "info")[:24],
                str(uid or "")[:512], str(source or "")[:64],
                str(trace_id or "")[:128], str(request_id or "")[:128],
                str(page_id or "")[:128], str(connection_id or "")[:128],
                str(build or "")[:128], data_json, blob_hash,
            )
            self._ensure_started()
            self._queue.put_nowait(_QueuedEvent(row, blob))
            return True
        except (OSError, TypeError, ValueError, queue.Full):
            self._dropped += 1
            return False

    def flush(self, timeout: float = 3.0) -> bool:
        if self._closed:
            return False
        self._ensure_started()
        done = threading.Event()
        try:
            self._queue.put(_Barrier(done), timeout=max(0.01, timeout / 2))
        except queue.Full:
            return False
        return done.wait(timeout=max(0.01, timeout))

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS blobs (
                sha256 TEXT PRIMARY KEY,
                mime TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                codec TEXT NOT NULL,
                payload BLOB NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                mono_ns INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                uid TEXT NOT NULL,
                source TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                build TEXT NOT NULL,
                data_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_uid_seq ON events(uid, seq);
            CREATE INDEX IF NOT EXISTS events_trace_seq ON events(trace_id, seq);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS events_request ON events(request_id, seq);
        """)
        db.commit()
        for candidate in (self.path, Path(str(self.path) + "-wal"),
                          Path(str(self.path) + "-shm")):
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass
        return db

    def _writer(self) -> None:
        db = None
        try:
            db = self._connect()
            self._prune(db)
            last_prune = time.monotonic()
            pending: list[_QueuedEvent] = []
            while True:
                try:
                    item = self._queue.get(timeout=0.25 if pending else 2.0)
                except queue.Empty:
                    item = False
                if isinstance(item, _QueuedEvent):
                    pending.append(item)
                    if len(pending) < BATCH_SIZE:
                        continue
                if pending:
                    try:
                        self._write_batch(db, pending)
                    except (OSError, sqlite3.Error, TypeError, ValueError):
                        self._dropped += len(pending)
                        try:
                            db.rollback()
                        except sqlite3.Error:
                            pass
                    pending = []
                if time.monotonic() - last_prune >= 3600:
                    try:
                        self._prune(db)
                    except (OSError, sqlite3.Error):
                        pass
                    last_prune = time.monotonic()
                if isinstance(item, _Barrier):
                    item.done.set()
                elif item is None:
                    break
        except Exception:
            # Drain barriers so callers do not hang when diagnostics storage is
            # unavailable. Normal requests must continue unaffected.
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _Barrier):
                    item.done.set()
                elif isinstance(item, _QueuedEvent):
                    self._dropped += 1
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _write_batch(db: sqlite3.Connection,
                     events: list[_QueuedEvent]) -> None:
        for item in events:
            if item.blob:
                sha, mime, original_bytes, payload = item.blob
                db.execute(
                    "INSERT OR IGNORE INTO blobs VALUES (?, ?, ?, 'zlib', ?, ?)",
                    (sha, mime, original_bytes, payload, time.time()))
            db.execute("""
                INSERT INTO events (
                    ts, ts_iso, mono_ns, pid, thread_id, event, category,
                    severity, uid, source, trace_id, request_id, page_id,
                    connection_id, build, data_json, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, item.row)
        db.commit()

    @staticmethod
    def _prune(db: sqlite3.Connection) -> None:
        cutoff = time.time() - RETENTION_DAYS * 86400
        db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        db.execute("""DELETE FROM blobs WHERE sha256 NOT IN (
            SELECT DISTINCT content_sha256 FROM events WHERE content_sha256 != ''
        )""")
        db.commit()

    def query(self, *, uid: str = "", trace_id: str = "",
              since: float | None = None, until: float | None = None,
              limit: int = 10_000, include_content: bool = False) -> list[dict]:
        """Return an ordered snapshot for diagnostics/report export."""
        self.flush()
        if not self.path.exists():
            return []
        clauses, values = [], []
        if uid:
            clauses.append("e.uid = ?")
            values.append(uid)
        if trace_id:
            clauses.append("e.trace_id = ?")
            values.append(trace_id)
        if since is not None:
            clauses.append("e.ts >= ?")
            values.append(float(since))
        if until is not None:
            clauses.append("e.ts <= ?")
            values.append(float(until))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = """SELECT e.seq, e.ts, e.ts_iso, e.mono_ns, e.pid, e.thread_id,
                    e.event, e.category, e.severity, e.uid, e.source,
                    e.trace_id, e.request_id, e.page_id, e.connection_id,
                    e.build, e.data_json, e.content_sha256,
                    b.mime, b.original_bytes, b.codec, b.payload
                 FROM events e LEFT JOIN blobs b
                   ON b.sha256 = e.content_sha256""" + where + \
              " ORDER BY e.seq DESC LIMIT ?"
        values.append(max(1, min(int(limit), 100_000)))
        try:
            db = sqlite3.connect(self.path, timeout=5)
            rows = db.execute(sql, values).fetchall()
            db.close()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return []
        result = []
        for row in reversed(rows):
            item = {
                "seq": row[0], "ts": row[1], "ts_iso": row[2],
                "mono_ns": row[3], "pid": row[4], "thread_id": row[5],
                "event": row[6], "category": row[7], "severity": row[8],
                "uid": row[9], "source": row[10], "trace_id": row[11],
                "request_id": row[12], "page_id": row[13],
                "connection_id": row[14], "build": row[15],
                "data": {},
                "content_sha256": row[17],
            }
            try:
                item["data"] = json.loads(row[16] or "{}")
            except (TypeError, ValueError):
                item["data_error"] = "unreadable"
            if include_content and row[17] and row[21] is not None:
                try:
                    raw = zlib.decompress(row[21]) if row[20] == "zlib" else row[21]
                    item["content"] = raw.decode("utf-8", "replace")
                    item["content_mime"] = row[18]
                    item["content_bytes"] = row[19]
                except (OSError, ValueError, zlib.error):
                    item["content_error"] = "unreadable"
            result.append(item)
        return result

    def export_jsonl(self, path: str | Path, **query) -> int:
        """Atomically export an event window, including referenced content."""
        rows = self.query(include_content=True, **query)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
        os.chmod(temp, 0o600)
        os.replace(temp, target)
        return len(rows)


_global_lock = threading.Lock()
_global_store: EventStore | None = None


def store() -> EventStore:
    global _global_store
    if _global_store is None:
        with _global_lock:
            if _global_store is None:
                _global_store = EventStore()
    return _global_store


def record(event: str, **fields) -> bool:
    try:
        return store().record(event, **fields)
    except Exception:
        return False


def flush(timeout: float = 3.0) -> bool:
    try:
        return store().flush(timeout)
    except Exception:
        return False


def query(**filters) -> list[dict]:
    try:
        return store().query(**filters)
    except Exception:
        return []


def record_ledger_changes(source: str, before: list[dict], after: list[dict]) -> None:
    """Record durable delivery-ledger mutations after their atomic file write."""
    try:
        old = {str(row.get("id") or ""): row for row in before if row.get("id")}
        new = {str(row.get("id") or ""): row for row in after if row.get("id")}
        for item_id in old.keys() | new.keys():
            previous, current = old.get(item_id), new.get(item_id)
            if previous == current:
                continue
            row = current or previous or {}
            from_state = str((previous or {}).get("state") or "missing")
            to_state = str((current or {}).get("state") or "removed")
            kind = "transition" if from_state != to_state else "updated"
            record(
                f"ledger.{kind}", category="delivery",
                severity="error" if to_state == "failed" else "info",
                source=source, uid=str(row.get("uid") or ""),
                trace_id=item_id, request_id=item_id,
                data={
                    "from_state": from_state, "to_state": to_state,
                    "attempts": row.get("attempts"), "error": row.get("error"),
                    "created": row.get("created"), "after_ts": row.get("after_ts"),
                    "ready_at": row.get("ready_at"),
                    "delivered_at": row.get("delivered_at"),
                    "activity_state": row.get("activity_state"),
                    "queue_depth": sum(1 for item in after
                                       if item.get("uid") == row.get("uid")),
                },
                content={"text": row.get("text"), "media": row.get("media") or []},
            )
    except Exception:
        return
