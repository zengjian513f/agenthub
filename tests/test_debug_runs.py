import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import debug_runs


class DebugRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_patch = patch.object(debug_runs, "DATA_DIR", root)
        self.file_patch = patch.object(debug_runs, "REGISTRY_FILE", root / "runs.json")
        self.data_patch.start()
        self.file_patch.start()
        debug_runs._cache_mtime = None
        debug_runs._cache = {"version": 1, "runs": {}}

    def tearDown(self):
        self.file_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def test_normal_view_hides_registered_root_and_debug_view_is_exclusive(self):
        root = Path(self.tmp.name) / "monkey-run"
        debug_runs.register("run-1", root)
        rows = [
            {"uid": "claude:real", "cwd": "/work/real"},
            {"uid": "claude:test", "cwd": str(root / "claude/1")},
        ]

        self.assertEqual([row["uid"] for row in debug_runs.filter_rows(rows)],
                         ["claude:real"])
        self.assertEqual([row["uid"] for row in debug_runs.filter_rows(rows, "run-1")],
                         ["claude:test"])
        self.assertEqual(debug_runs.filter_rows(rows, "missing-run"), [])

    def test_registered_identity_stays_hidden_without_cwd(self):
        root = Path(self.tmp.name) / "monkey-run"
        debug_runs.register("run-2", root)
        debug_runs.add_session(
            "run-2", source="codex", cwd=str(root / "codex/1"),
            sid="sid-1", uid="codex:test", name="agenthub-codex-test")

        for row in ({"uid": "codex:test"}, {"sid": "sid-1"},
                    {"name": "agenthub-codex-test"}):
            self.assertEqual(debug_runs.run_for(row), "run-2")
            self.assertEqual(debug_runs.filter_rows([row]), [])

        self.assertTrue(debug_runs.remove("run-2"))
        self.assertEqual(debug_runs.run_for({"uid": "codex:test"}), None)


if __name__ == "__main__":
    unittest.main()
