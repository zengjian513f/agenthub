import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agenthub import audit, bug_report, server


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
        self.assertIn("创建一个 commit 并 push 到 GitHub", worker_prompt)
        self.assertIn("同步到中央 Hub 和全部已部署节点", worker_prompt)
        self.assertIn("绝不 reset/clean/强推", worker_prompt)
        self.assertNotIn("绝不 push", worker_prompt)
        self.assertNotIn("附件", worker_prompt.split("诊断包")[0])
        self.assertEqual(manifest["attachments"], [])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_bundle_keeps_attachments_and_lists_them_like_the_composer(self):
        project = self.root / "repo"
        uploads = project / bug_report.ATTACHMENT_DIR / "7"
        uploads.mkdir(parents=True)
        shot = uploads / "屏幕截图.png"
        shot.write_bytes(b"\x89PNG" + b"\0" * 16)
        with patch.object(bug_report, "REPORT_ROOT", self.root / "reports"), \
                patch.object(bug_report, "PROJECT_ROOT", project), \
                patch.object(bug_report, "_command", return_value={"exit_code": 0}):
            attachments = bug_report.resolve_attachments([
                {"path": str(shot), "number": 3, "name": "屏幕截图.png",
                 "kind": "image", "mime": "image/png"}])
            report = bug_report.create(
                "点了按钮没反应，见 [附件3]", uid="codex:one",
                event_store=self.store, attachments=attachments)
        self.assertEqual(attachments[0]["relative_path"],
                         f"{bug_report.ATTACHMENT_DIR}/7/屏幕截图.png")
        self.assertEqual(attachments[0]["size"], 20)
        directory = Path(report["path"])
        manifest = json.loads((directory / "manifest.json").read_text())
        bundled = directory / "attachments" / "03-屏幕截图.png"
        self.assertEqual(bundled.read_bytes(), shot.read_bytes())
        self.assertEqual(manifest["attachments"][0]["bundle_file"], "attachments/03-屏幕截图.png")
        self.assertEqual(manifest["attachments"][0]["path"], str(shot))
        prompt = (directory / "worker-prompt.md").read_text()
        self.assertIn("点了按钮没反应，见 [附件3]\n\n附件3: ./agenthub_attachments/7/屏幕截图.png",
                      prompt)
        self.assertIn("上传了 1 个附件", prompt)
        self.assertEqual((directory / "description.md").read_text(),
                         "点了按钮没反应，见 [附件3]\n")

    def test_resolve_attachments_rejects_paths_outside_the_upload_directory(self):
        project = self.root / "repo"
        (project / bug_report.ATTACHMENT_DIR).mkdir(parents=True)
        outside = self.root / "secret.txt"
        outside.write_text("no")
        with patch.object(bug_report, "PROJECT_ROOT", project):
            self.assertEqual(bug_report.resolve_attachments(None), [])
            self.assertEqual(bug_report.resolve_attachments([]), [])
            with self.assertRaisesRegex(ValueError, "不在附件目录中"):
                bug_report.resolve_attachments([{"path": str(outside)}])
            with self.assertRaisesRegex(ValueError, "不在附件目录中"):
                bug_report.resolve_attachments(
                    [{"path": str(project / bug_report.ATTACHMENT_DIR / "1" / "gone.png")}])
            with self.assertRaisesRegex(ValueError, "缺少路径"):
                bug_report.resolve_attachments([{"name": "x"}])
            with self.assertRaisesRegex(ValueError, "格式无效"):
                bug_report.resolve_attachments("x")
            with self.assertRaisesRegex(ValueError, "最多附带"):
                bug_report.resolve_attachments(
                    [{"path": "x"}] * (bug_report.ATTACHMENT_MAX_COUNT + 1))
            link = project / bug_report.ATTACHMENT_DIR / "link.txt"
            link.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "不在附件目录中"):
                bug_report.resolve_attachments([{"path": str(link)}])
        with patch.object(bug_report, "PROJECT_ROOT", self.root / "missing"):
            with self.assertRaisesRegex(ValueError, "尚未上传"):
                bug_report.resolve_attachments([{"path": "x"}])


class BugReportWorkerTests(unittest.TestCase):
    def _run_worker(self, after_paste, before_paste=("empty",), timeout=3.0):
        """Drive _inject_worker with scripted composer states.

        ``before_paste`` states are returned (last one repeating) until the
        prompt is pasted; ``after_paste`` states follow the paste.  Each extra
        Enter sent by the worker advances ``after_paste`` by one step so a
        test can express "the composer clears after the second Enter".
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text('{"status":"starting"}\n')
            report = {"report_id": "BUG-ready", "path": str(directory),
                      "prompt": "investigate this report"}
            info = {"name": "agenthub-codex-new-ready"}
            driver = MagicMock()
            phase = {"pasted": False, "before": list(before_paste), "after": list(after_paste)}

            def probe(name):
                if phase["pasted"]:
                    return {"draft_state": phase["after"][0]}
                script = phase["before"]
                return {"draft_state": script.pop(0) if len(script) > 1 else script[0]}

            def paste(name, text):
                phase["pasted"] = True

            def enter(name, key):
                if len(phase["after"]) > 1:
                    phase["after"].pop(0)
            driver.composer_probe.side_effect = probe
            events = []
            with patch.object(bug_report, "SETTLE_SECONDS", 0.05), \
                    patch.object(bug_report, "CONFIRM_WAIT_SECONDS", 0.2), \
                    patch.object(bug_report.term, "has_session", return_value=True), \
                    patch.object(bug_report.send_protocol, "driver_for",
                                 return_value=driver), \
                    patch.object(bug_report.term, "submit_text", side_effect=paste) as submit, \
                    patch.object(bug_report.term, "send_keys", side_effect=enter) as keys, \
                    patch.object(bug_report.audit, "record",
                                 side_effect=lambda event, **kw: events.append(event)):
                bug_report._inject_worker(report, info, timeout=timeout)
            manifest = json.loads((directory / "manifest.json").read_text())
            return submit, keys, events, manifest, info

    def test_ready_worker_waits_for_a_settled_composer_then_confirms(self):
        # empty (fresh start) → paste → empty again = submitted.
        submit, keys, events, manifest, info = self._run_worker(["empty"])
        submit.assert_called_once_with(info["name"], "investigate this report")
        keys.assert_not_called()
        self.assertEqual(manifest["status"], "submitted")
        self.assertEqual(manifest["tmux"], info["name"])
        self.assertIn("bug_report.worker_submitted", events)

    def test_swallowed_enter_is_resent_until_the_composer_clears(self):
        # Settle probes see "empty"; after the paste the prompt stays in the
        # composer ("editing") until the second Enter.
        # After the paste the prompt sits in the composer through the first
        # Enter and clears only after the second one.
        submit, keys, events, manifest, info = self._run_worker(
            ["editing", "editing", "empty"])
        submit.assert_called_once()
        self.assertEqual(keys.call_count, 2)
        keys.assert_called_with(info["name"], "Enter")
        self.assertEqual(manifest["status"], "submitted")
        self.assertIn("bug_report.worker_enter_retry", events)
        self.assertIn("bug_report.worker_submitted", events)

    def test_unconfirmed_submission_is_recorded_honestly(self):
        submit, keys, events, manifest, info = self._run_worker(["editing"])
        submit.assert_called_once()
        self.assertEqual(keys.call_count, bug_report.CONFIRM_ATTEMPTS)
        self.assertEqual(manifest["status"], "submitted_unconfirmed")
        self.assertIn("未能确认已提交", manifest["error"])
        self.assertIn("bug_report.worker_unconfirmed", events)
        self.assertNotIn("bug_report.worker_submitted", events)

    def test_worker_does_not_paste_before_the_composer_settles(self):
        # A single early "empty" frame followed by "unknown" must not trigger the paste.
        submit, keys, events, manifest, info = self._run_worker(
            ["empty"], before_paste=["empty", "unknown", "unknown", "unknown", "empty"])
        submit.assert_called_once()
        self.assertEqual(manifest["status"], "submitted")

    def test_launch_creates_pending_session_without_running_model_in_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text('{"status":"captured"}\n')
            report = {"report_id": "BUG-test", "path": str(directory),
                      "prompt": "investigate"}
            info = {"name": "agenthub-codex-new-test", "source": "codex",
                    "sid": None, "cwd": str(bug_report.PROJECT_ROOT), "token": "token"}
            fake_thread = MagicMock()
            with patch.object(bug_report.index, "load", return_value=[
                    {"source": "codex", "sid": "old"}, {"source": "claude", "sid": "c1"}]), \
                    patch.object(bug_report.term, "new_cli_session",
                                 return_value=info) as new, \
                    patch.object(bug_report.pending_store, "put") as put, \
                    patch.object(bug_report.audit, "record"), \
                    patch.object(bug_report.threading, "Thread",
                                 return_value=fake_thread):
                result = bug_report.launch(report, 100, 30)
                claude = bug_report.launch(report, 100, 30, source="claude")
                with self.assertRaisesRegex(ValueError, "不支持的处理会话类型"):
                    bug_report.launch(report, 100, 30, source="bash")

            self.assertEqual(new.call_args_list[0].args[0], "codex")
            self.assertEqual(new.call_args_list[1].args[0], "claude")
            new.assert_called_with(
                "claude", str(bug_report.PROJECT_ROOT), 100, 30, create_cwd=False)
            record = put.call_args_list[0].args[0]
            self.assertEqual(record["kind"], "bug-report")
            self.assertEqual(record["title"], "处理 BUG-test")
            self.assertEqual(record["before"], {"old"})
            self.assertEqual(put.call_args_list[1].args[0]["before"], {"c1"})
            self.assertEqual(fake_thread.start.call_count, 2)
            self.assertEqual(result["report_id"], "BUG-test")
            self.assertEqual(claude["report_id"], "BUG-test")
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest["worker_source"], "claude")

    def test_grok_worker_uses_screen_stability_without_a_send_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text('{"status":"starting"}\n')
            report = {"report_id": "BUG-grok", "path": str(directory), "prompt": "look"}
            info = {"name": "agenthub-grok-new-1", "source": "grok"}
            frames = {"screen": "", "pasted": False}
            events = []

            def capture(name):
                if frames["pasted"]:
                    return frames["screen"]
                return "grok> _"

            def paste(name, text):
                frames["pasted"] = True
                frames["screen"] = "grok> look"

            def enter(name, key):
                frames["screen"] = "thinking…"
            with patch.object(bug_report, "SETTLE_SECONDS", 0.05), \
                    patch.object(bug_report, "CONFIRM_WAIT_SECONDS", 0.2), \
                    patch.object(bug_report.term, "has_session", return_value=True), \
                    patch.object(bug_report.term, "capture_screen", side_effect=capture), \
                    patch.object(bug_report.term, "submit_text", side_effect=paste) as submit, \
                    patch.object(bug_report.term, "send_keys", side_effect=enter) as keys, \
                    patch.object(bug_report.time, "sleep"), \
                    patch.object(bug_report.audit, "record",
                                 side_effect=lambda event, **kw: events.append(event)):
                bug_report._inject_worker(report, info, timeout=3.0)
            submit.assert_called_once_with("agenthub-grok-new-1", "look")
            # The frame after paste + first Enter stayed identical, so one more Enter.
            self.assertEqual(keys.call_count, 1)
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "submitted")
            self.assertIn("bug_report.worker_enter_retry", events)

    def test_server_endpoint_captures_then_launches_worker(self):
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        handler._display_ip = lambda: "192.0.2.8"
        session = {"uid": "codex:one", "source": "codex", "sid": "one"}
        report = {"report_id": "BUG-test", "path": "/tmp/report"}
        worker = {"name": "agenthub-codex-new-test", "source": "codex",
                  "sid": None, "cwd": str(bug_report.PROJECT_ROOT), "token": "token",
                  "title": "处理 BUG-test", "kind": "bug-report",
                  "report_id": "BUG-test"}
        resolved_attachment = {"number": 1, "path": "/repo/agenthub_attachments/1/shot.png",
                               "relative_path": "agenthub_attachments/1/shot.png",
                               "name": "shot.png", "mime": "image/png", "kind": "image",
                               "size": 4}
        with patch.object(server, "TERMINAL", True), \
                patch.object(server.term, "available_sources",
                             return_value={"codex": True, "claude": True, "grok": False}), \
                patch.object(server.term, "list_sessions",
                             return_value=[{"name": "agenthub-codex-one"}]), \
                patch.object(server.term, "capture_history", return_value="screen"), \
                patch.object(server.bug_report, "resolve_attachments",
                             return_value=[resolved_attachment]), \
                patch.object(server.index, "get", return_value=session), \
                patch.object(server.send_protocol, "snapshot", return_value={"outbox": []}), \
                patch.object(server.bug_report, "create", return_value=report) as create, \
                patch.object(server.bug_report, "launch", return_value=worker) as launch:
            result = handler._bug_report({
                "description": "lost message", "uid": "codex:one",
                "page_id": "page", "terminal_name": "agenthub-codex-one",
                "snapshot": {"data": {}}, "cols": 100, "rows": 30,
                "attachments": [{"path": "/repo/agenthub_attachments/1/shot.png", "number": 1}],
            })
            with patch.object(server.bug_report, "resolve_attachments",
                              side_effect=ValueError("第 1 个附件不在附件目录中或已不存在")):
                rejected = handler._bug_report({
                    "description": "lost message", "uid": "codex:one",
                    "attachments": [{"path": "/etc/passwd"}],
                })
            claude = handler._bug_report({
                "description": "lost message", "uid": "codex:one", "source": "claude",
                "snapshot": {"data": {}}, "cols": 100, "rows": 30,
            })
            missing = handler._bug_report({
                "description": "lost message", "uid": "codex:one", "source": "grok"})
            bogus = handler._bug_report({
                "description": "lost message", "uid": "codex:one", "source": "bash"})

        self.assertEqual(result["_status"], 202)
        self.assertEqual(result["report_id"], "BUG-test")
        self.assertEqual(create.call_args_list[0].kwargs["terminal_capture"], "screen")
        self.assertEqual(create.call_args_list[0].kwargs["attachments"], [resolved_attachment])
        self.assertEqual(launch.call_args_list[0].kwargs,
                         {"cols": 100, "rows": 30, "source": "codex"})
        self.assertEqual(rejected["_status"], 400)
        self.assertIn("不在附件目录中", rejected["error"])
        self.assertEqual(claude["_status"], 202)
        self.assertEqual(launch.call_args_list[1].kwargs["source"], "claude")
        self.assertEqual(missing["_status"], 503)
        self.assertIn("grok", missing["error"])
        self.assertEqual(bogus["_status"], 400)
        self.assertEqual(create.call_count, 2)

    def test_upload_route_accepts_bug_report_uploads_into_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            handler = object.__new__(server.Handler)
            handler._json = lambda payload, status=200: {**payload, "_status": status}
            payload = b"\x89PNG" + b"\1" * 8
            handler.headers = {"Content-Length": str(len(payload)), "Content-Type": "image/png"}
            handler.rfile = io.BytesIO(payload)
            with patch.object(server, "TERMINAL", True), \
                    patch.object(server.bug_report, "PROJECT_ROOT", project), \
                    patch.object(server.index, "get", return_value=None) as lookup, \
                    patch.object(server.media, "register_path", return_value=None):
                result = handler._upload_attachment(
                    {"uid": ["bug-report"], "name": ["屏幕截图.png"]})
            lookup.assert_not_called()
            self.assertEqual(result["_status"], 200)
            self.assertEqual(result["relative_path"], "agenthub_attachments/1/屏幕截图.png")
            self.assertEqual(Path(result["path"]).read_bytes(), payload)
            self.assertEqual(result["kind"], "image")
            handler.headers = {"Content-Length": "4"}
            handler.rfile = io.BytesIO(b"abcd")
            with patch.object(server, "TERMINAL", False), \
                    patch.object(server.bug_report, "PROJECT_ROOT", project):
                denied = handler._upload_attachment({"uid": ["bug-report"], "name": ["x"]})
            self.assertEqual(denied["_status"], 403)


if __name__ == "__main__":
    unittest.main()
