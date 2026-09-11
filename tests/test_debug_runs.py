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
        debug_runs._index = debug_runs._index_of = None

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

    def test_earliest_registered_run_wins_for_a_row_matching_several(self):
        """The lookup tables must keep the registry order of the original scan."""
        outer = Path(self.tmp.name) / "outer"
        inner = outer / "inner"
        debug_runs.register("run-a", inner)
        debug_runs.register("run-b", outer)
        debug_runs.add_session("run-b", source="codex", cwd=str(outer),
                               sid="shared", uid="codex:b")
        debug_runs.add_session("run-a", source="codex", cwd=str(inner),
                               sid="shared", uid="codex:a")

        # Nested roots: the run registered first owns the row.
        self.assertEqual(debug_runs.run_for({"cwd": str(inner / "x")}), "run-a")
        self.assertEqual(debug_runs.run_for({"cwd": str(outer / "x")}), "run-b")
        # An identity recorded by both runs resolves to the earlier one, and an
        # identity match outranks a root match belonging to a later run.
        self.assertEqual(debug_runs.run_for({"sid": "shared"}), "run-a")
        self.assertEqual(debug_runs.run_for({"uid": "codex:a",
                                             "cwd": str(outer / "x")}), "run-a")
        self.assertEqual(debug_runs.run_for({"uid": "codex:b",
                                             "cwd": str(inner / "x")}), "run-a")

    def test_lookup_reflects_registry_edits_and_survives_malformed_entries(self):
        root = Path(self.tmp.name) / "run"
        debug_runs.register("run-c", root)
        self.assertIsNone(debug_runs.run_for({"uid": "codex:late"}))
        debug_runs.add_session("run-c", source="codex", cwd=str(root),
                               uid="codex:late")
        self.assertEqual(debug_runs.run_for({"uid": "codex:late"}), "run-c")

        # A registry written by an older or interrupted monkey must never turn a
        # plain list request into a 500.
        broken = {"version": 1, "runs": {
            "bad": {"root": None, "sessions": ["junk", None]},
            "run-d": {"root": "", "sessions": [{"uid": "codex:ok"}]},
            "worse": "not-a-run"}}
        debug_runs._index = debug_runs._index_of = None
        with patch.object(debug_runs, "_read", return_value=broken):
            self.assertEqual(debug_runs.run_for({"uid": "codex:ok"}), "run-d")
            self.assertIsNone(debug_runs.run_for({"uid": "codex:late"}))
            self.assertEqual(debug_runs.filter_rows([{"uid": "codex:ok"},
                                                     {"uid": "codex:other"}]),
                             [{"uid": "codex:other"}])

    def test_empty_registry_keeps_every_row_without_scanning(self):
        rows = [{"uid": "claude:a", "cwd": "/work/a"}, {"uid": "codex:b"}]
        self.assertEqual(debug_runs.filter_rows(rows), rows)
        self.assertEqual(debug_runs.filter_rows(rows, "run-x"), [])


if __name__ == "__main__":
    unittest.main()
