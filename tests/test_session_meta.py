import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import session_meta


class SessionMetaTests(unittest.TestCase):
    def test_star_is_private_persistent_metadata_and_enriches_copies(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            self.assertEqual(session_meta.signature(), "0")
            row = session_meta.set_starred("codex:abc", True)
            starred_sig = session_meta.signature()

            self.assertTrue(row["starred"])
            self.assertNotEqual(starred_sig, "0")
            self.assertEqual(stat.S_IMODE(session_meta.META_FILE.stat().st_mode), 0o600)
            original = {"uid": "codex:abc", "title": "demo"}
            enriched = session_meta.enrich([original])[0]
            self.assertTrue(enriched["starred"])
            self.assertNotIn("starred", original)

            session_meta.set_starred("codex:abc", False)
            self.assertNotEqual(session_meta.signature(), starred_sig)
            self.assertNotIn("starred", session_meta.enrich([original])[0])
            saved = json.loads(session_meta.META_FILE.read_text())
            self.assertEqual(saved, {"version": 1, "sessions": {}})

    def test_discard_only_removes_target_session(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            session_meta.set_starred("claude:a", True)
            session_meta.set_starred("codex:b", True)
            session_meta.stop_activity("claude:a", now=100)
            self.assertGreater(session_meta.activity_revision("claude:a"), 0)
            session_meta.discard("claude:a")
            rows = session_meta.enrich([
                {"uid": "claude:a"}, {"uid": "codex:b"},
            ])
            self.assertNotIn("starred", rows[0])
            self.assertTrue(rows[1]["starred"])
            self.assertEqual(session_meta.activity_revision("claude:a"), 0)

    def test_escape_persistently_ends_only_older_busy_activity(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            session_meta.set_starred("claude:a", True)
            before = session_meta.activity_revision("claude:a")
            stopped = session_meta.stop_activity(
                "claude:a", now=100, reason="终端已结束或中断")

            self.assertEqual(stopped["state"], "aborted")
            self.assertEqual(stopped["reason"], "终端已结束或中断")
            self.assertEqual(session_meta.stopped_activity("claude:a")["reason"],
                             "终端已结束或中断")
            self.assertGreater(session_meta.activity_revision("claude:a"), before)
            self.assertEqual(session_meta.activity_revision("claude:b"), 0)
            self.assertEqual(session_meta.resolve_activity("claude:a", {
                "state": "working", "ts": "1970-01-01T00:01:39+00:00",
            })["state"], "aborted")
            newer = {"state": "working", "ts": "1970-01-01T00:01:41+00:00"}
            self.assertEqual(session_meta.resolve_activity("claude:a", newer), newer)
            idle = {"state": "idle", "ts": "1970-01-01T00:01:39+00:00"}
            self.assertEqual(session_meta.resolve_activity("claude:a", idle), idle)
            self.assertTrue(session_meta.enrich_one({"uid": "claude:a"})["starred"])

    def test_inferred_idle_can_be_revoked_by_visible_busy_terminal(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            session_meta.set_starred("claude:a", True)
            stopped = session_meta.stop_activity(
                "claude:a", now=100, reason="终端已回到输入状态",
                state="idle", inferred=True)
            before = session_meta.activity_revision("claude:a")

            self.assertEqual(stopped["state"], "idle")
            self.assertEqual(session_meta.stopped_activity("claude:a")["state"],
                             "idle")
            self.assertTrue(session_meta.clear_inferred_activity_stop("claude:a"))
            self.assertIsNone(session_meta.stopped_activity("claude:a"))
            self.assertGreater(session_meta.activity_revision("claude:a"), before)
            self.assertTrue(session_meta.enrich_one({"uid": "claude:a"})["starred"])
            self.assertFalse(session_meta.clear_inferred_activity_stop("claude:a"))

            session_meta.stop_activity("claude:a", now=101,
                                       reason="网页发送 Escape")
            self.assertFalse(session_meta.clear_inferred_activity_stop("claude:a"))
            self.assertEqual(session_meta.stopped_activity("claude:a")["state"],
                             "aborted")

    def test_claude_timeline_changes_only_after_native_rewind_is_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            pending = session_meta.begin_timeline_rewind("claude:a", "old-tip", 123)
            self.assertEqual(pending["from_tip"], "old-tip")
            self.assertIsNone(session_meta.timeline("claude:a"))
            self.assertEqual(session_meta.pending_timeline_rewind("claude:a"), pending)

            before = session_meta.timeline_revision("claude:a")
            fixed = session_meta.finish_timeline_rewind("claude:a", "kept-tip")
            self.assertEqual(fixed, {"tip": "kept-tip", "stale_end": 123})
            self.assertEqual(session_meta.timeline("claude:a"), fixed)
            self.assertGreater(session_meta.timeline_revision("claude:a"), before)
            self.assertIsNone(session_meta.pending_timeline_rewind("claude:a"))


if __name__ == "__main__":
    unittest.main()
