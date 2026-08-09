import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sesman import session_meta


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
            session_meta.discard("claude:a")
            rows = session_meta.enrich([
                {"uid": "claude:a"}, {"uid": "codex:b"},
            ])
            self.assertNotIn("starred", rows[0])
            self.assertTrue(rows[1]["starred"])


if __name__ == "__main__":
    unittest.main()
