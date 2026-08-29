import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sesman import audit, index


class AuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "audit.sqlite3"
        self.store = audit.EventStore(self.path, queue_limit=32)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_events_are_ordered_and_content_is_deduplicated(self):
        content = {"text": "同一条正文", "rows": [1, 2]}
        self.assertTrue(self.store.record(
            "http.request", uid="codex:one", trace_id="trace-1",
            request_id="request-1", data={"state": "queued"}, content=content))
        self.assertTrue(self.store.record(
            "ledger.persisted", uid="codex:one", trace_id="trace-1",
            request_id="request-1", data={"state": "queued"}, content=content))
        self.assertTrue(self.store.flush())

        rows = self.store.query(uid="codex:one", include_content=True)
        self.assertEqual([row["event"] for row in rows],
                         ["http.request", "ledger.persisted"])
        self.assertLess(rows[0]["seq"], rows[1]["seq"])
        self.assertEqual(rows[0]["content_sha256"], rows[1]["content_sha256"])
        self.assertEqual(json.loads(rows[0]["content"]), content)

        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM blobs").fetchone()[0], 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_sensitive_metadata_is_redacted(self):
        self.store.record("browser.state", data={
            "headers": {"Authorization": "Bearer secret", "Cookie": "sid=x"},
            "nested": {"api_key": "secret", "safe": "kept"},
        })
        rows = self.store.query()
        self.assertEqual(rows[0]["data"]["headers"]["Authorization"], "<redacted>")
        self.assertEqual(rows[0]["data"]["headers"]["Cookie"], "<redacted>")
        self.assertEqual(rows[0]["data"]["nested"]["api_key"], "<redacted>")
        self.assertEqual(rows[0]["data"]["nested"]["safe"], "kept")

    def test_jsonl_export_contains_the_selected_window(self):
        self.store.record("one", uid="claude:a", content="first")
        self.store.record("two", uid="claude:b", content="second")
        target = Path(self.tmp.name) / "report" / "events.jsonl"
        count = self.store.export_jsonl(target, uid="claude:a")
        self.assertEqual(count, 1)
        row = json.loads(target.read_text())
        self.assertEqual(row["event"], "one")
        self.assertEqual(row["content"], "first")
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)


class ParserAuditTests(unittest.TestCase):
    def test_unchanged_polls_are_not_logged_and_identical_resets_are_deduplicated(self):
        session = {"uid": "codex:audit-dedup", "source": "codex",
                   "path": "/tmp/audit-dedup.jsonl"}
        base = {"version": {"size": 10, "mtime": 20, "head": "head"},
                "reset": False, "start": 10, "end": 10, "messages": [],
                "activity_changed": False, "activity": None,
                "message_total": 0, "partial": None, "anchor": "anchor"}
        requested = {"start": 10}
        with patch.object(index.audit, "record") as record:
            index._audit_message_batch(session, base, requested)
            reset = {**base, "reset": True}
            index._audit_message_batch(session, reset, requested)
            index._audit_message_batch(session, reset, requested)
        record.assert_called_once()


if __name__ == "__main__":
    unittest.main()
