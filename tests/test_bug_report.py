import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sesman import audit, bug_report, server


class BugReportBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = audit.EventStore(self.root / "audit.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_bundle_filters_related_events_and_captures_context(self):
        self.store.record("browser.dom.snapshot", uid="codex:one", page_id="page-1",
                          content={"messages": ["visible"]})
        self.store.record("unrelated", uid="claude:other", page_id="page-2")
        now = time.time()
        with patch.object(bug_report, "REPORT_ROOT", self.root / "reports"), \
                patch.object(bug_report, "_command", return_value={"exit_code": 0}):
            report = bug_report.create(
                "消息发送后消失", uid="codex:one", page_id="page-1",
                trace_id="trace-1", build="build", hostname="host",
                client_ip="192.0.2.1", snapshot={"data": {"selected": "codex:one"}},
                terminal_capture="terminal frame", session={"path": "/tmp/session.jsonl"},
                outbox={"outbox": [{"state": "confirming"}]},
                event_store=self.store, now=now)

        directory = Path(report["path"])
        manifest = json.loads((directory / "manifest.json").read_text())
        rows = [json.loads(line) for line in
                (directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(manifest["uid"], "codex:one")
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(manifest["event_count"], 2)  # snapshot + report.created
        self.assertEqual({row["event"] for row in rows},
                         {"browser.dom.snapshot", "bug_report.created"})
        self.assertEqual((directory / "terminal.txt").read_text(), "terminal frame")
        worker_prompt = (directory / "worker-prompt.md").read_text()
        self.assertIn("Playwright/headless Chromium", worker_prompt)
        self.assertIn("默认创建一个本地 commit", worker_prompt)
        self.assertIn("绝不 push", worker_prompt)
        self.assertNotIn("不 commit", worker_prompt)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)


class BugReportWorkerTests(unittest.TestCase):
    def test_ready_worker_receives_prompt_and_updates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text('{"status":"starting"}\n')
            report = {"report_id": "BUG-ready", "path": str(directory),
                      "prompt": "investigate this report"}
            info = {"name": "sesman-codex-new-ready"}
            driver = MagicMock()
            driver.composer_probe.return_value = {"draft_state": "empty"}
            with patch.object(bug_report.term, "has_session", return_value=True), \
                    patch.object(bug_report.send_protocol, "driver_for",
                                 return_value=driver), \
                    patch.object(bug_report.term, "submit_text") as submit, \
                    patch.object(bug_report.audit, "record"):
                bug_report._inject_worker(report, info, timeout=0.1)

            submit.assert_called_once_with(info["name"], report["prompt"])
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "submitted")
            self.assertEqual(manifest["tmux"], info["name"])

    def test_launch_creates_pending_session_without_running_model_in_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text('{"status":"captured"}\n')
            report = {"report_id": "BUG-test", "path": str(directory),
                      "prompt": "investigate"}
            info = {"name": "sesman-codex-new-test", "source": "codex",
                    "sid": None, "cwd": str(bug_report.PROJECT_ROOT), "token": "token"}
            fake_thread = MagicMock()
            with patch.object(bug_report.index, "load", return_value=[
                    {"source": "codex", "sid": "old"}]), \
                    patch.object(bug_report.term, "new_cli_session",
                                 return_value=info) as new, \
                    patch.object(bug_report.pending_store, "put") as put, \
                    patch.object(bug_report.audit, "record"), \
                    patch.object(bug_report.threading, "Thread",
                                 return_value=fake_thread):
                result = bug_report.launch(report, 100, 30)

            new.assert_called_once_with(
                "codex", str(bug_report.PROJECT_ROOT), 100, 30, create_cwd=False)
            record = put.call_args.args[0]
            self.assertEqual(record["kind"], "bug-report")
            self.assertEqual(record["title"], "处理 BUG-test")
            self.assertEqual(record["before"], {"old"})
            fake_thread.start.assert_called_once()
            self.assertEqual(result["report_id"], "BUG-test")

    def test_server_endpoint_captures_then_launches_worker(self):
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        handler._display_ip = lambda: "192.0.2.8"
        session = {"uid": "codex:one", "source": "codex", "sid": "one"}
        report = {"report_id": "BUG-test", "path": "/tmp/report"}
        worker = {"name": "sesman-codex-new-test", "source": "codex",
                  "sid": None, "cwd": str(bug_report.PROJECT_ROOT), "token": "token",
                  "title": "处理 BUG-test", "kind": "bug-report",
                  "report_id": "BUG-test"}
        with patch.object(server, "TERMINAL", True), \
                patch.object(server.term, "available_sources", return_value={"codex": True}), \
                patch.object(server.term, "list_sessions",
                             return_value=[{"name": "sesman-codex-one"}]), \
                patch.object(server.term, "capture_history", return_value="screen"), \
                patch.object(server.index, "get", return_value=session), \
                patch.object(server.send_protocol, "snapshot", return_value={"outbox": []}), \
                patch.object(server.bug_report, "create", return_value=report) as create, \
                patch.object(server.bug_report, "launch", return_value=worker) as launch:
            result = handler._bug_report({
                "description": "lost message", "uid": "codex:one",
                "page_id": "page", "terminal_name": "sesman-codex-one",
                "snapshot": {"data": {}}, "cols": 100, "rows": 30,
            })

        self.assertEqual(result["_status"], 202)
        self.assertEqual(result["report_id"], "BUG-test")
        self.assertEqual(create.call_args.kwargs["terminal_capture"], "screen")
        launch.assert_called_once_with(report, cols=100, rows=30)


if __name__ == "__main__":
    unittest.main()
