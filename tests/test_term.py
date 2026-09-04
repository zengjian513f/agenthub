import unittest
import shlex
import struct
import tempfile
from unittest.mock import call, patch

from sesman import term


class DirectoryCompletionTests(unittest.TestCase):
    def test_matches_directories_only_and_hides_dot_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = term.Path(tmp)
            for name in ("Project Alpha", "Projects", ".private"):
                (root / name).mkdir()
            (root / "Project.txt").write_text("not a directory")

            self.assertEqual(term.complete_directories(f"{root}/Pro"), [
                f"{root}/Project Alpha/", f"{root}/Projects/",
            ])
            self.assertNotIn(f"{root}/.private/",
                             term.complete_directories(f"{root}/"))
            self.assertEqual(term.complete_directories(f"{root}/.p"),
                             [f"{root}/.private/"])

    def test_trailing_slash_descends_and_preserves_tilde_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = term.Path(tmp)
            (root / "work" / "child").mkdir(parents=True)
            with patch.object(term.Path, "home", return_value=root):
                self.assertEqual(term.complete_directories("~"), ["~/"])
                self.assertEqual(term.complete_directories("~/wo"), ["~/work/"])
                self.assertEqual(term.complete_directories("~/work/"),
                                 ["~/work/child/"])
            self.assertEqual(term.complete_directories("relative/path"), [])

    def test_completion_limit_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = term.Path(tmp)
            for index in range(5):
                (root / f"dir-{index}").mkdir()
            self.assertEqual(len(term.complete_directories(f"{root}/d", limit=2)), 2)
            self.assertEqual(len(term.complete_directories(f"{root}/d", limit=500)), 5)
        self.assertEqual(term.complete_directories("/tmp/\0invalid"), [])
        with self.assertRaisesRegex(ValueError, "路径过长"):
            term.complete_directories("/" + "x" * 4096)


class NewSessionDirectoryTests(unittest.TestCase):
    def test_missing_directory_requires_confirmation_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = term.Path(tmp) / "parent" / "project"
            with patch.object(term, "_which_cli", return_value="/usr/bin/codex"), \
                    patch.object(term, "new_session") as new:
                with self.assertRaises(term.DirectoryCreationRequired) as raised:
                    term.new_cli_session("codex", str(missing))

            self.assertEqual(raised.exception.path,
                             str(missing.resolve(strict=False)))
            self.assertFalse(missing.exists())
            new.assert_not_called()

    def test_confirmed_missing_directory_is_created_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = term.Path(tmp) / "parent" / "project"
            with patch.object(term, "_which_cli", return_value="/usr/bin/codex"), \
                    patch.object(term, "new_session",
                                 return_value="sesman-codex-test") as new:
                result = term.new_cli_session(
                    "codex", str(missing), create_cwd=True)

            self.assertTrue(missing.is_dir())
            self.assertEqual(new.call_args.args[2], str(missing.resolve()))
            self.assertEqual(result["cwd"], str(missing.resolve()))

    def test_existing_file_cannot_be_used_or_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = term.Path(tmp) / "project"
            target.write_text("file")
            with patch.object(term, "_which_cli", return_value="/usr/bin/codex"), \
                    self.assertRaisesRegex(ValueError, "不是目录"):
                term.new_cli_session("codex", str(target), create_cwd=True)
            self.assertTrue(target.is_file())


class TerminalSubmitTests(unittest.TestCase):
    def test_new_session_uses_fallback_size_only_as_its_initial_size(self):
        with patch.object(term, "has_session", return_value=False), \
                patch.object(term, "_configure_managed"), \
                patch.object(term, "_tmux") as tmux:
            name = term.new_session("test", "command")

        self.assertEqual(name, "sesman-test")
        tmux.assert_called_once_with(
            "new-session", "-d", "-s", "sesman-test", "-x", "120", "-y", "32",
            "command", server=term.MANAGED_SERVER)

    def test_attach_ignores_hidden_xterm_minimum_size(self):
        attach = term.Attach.__new__(term.Attach)
        attach.fd = 123
        attach.cols, attach.rows = 120, 32
        with patch.object(term.fcntl, "ioctl") as ioctl:
            self.assertFalse(attach.resize(10, 6))
            ioctl.assert_not_called()
            self.assertTrue(attach.resize(80, 24))
        ioctl.assert_called_once_with(
            123, term.termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.assertEqual((attach.cols, attach.rows), (80, 24))

    def test_claude_resume_includes_passive_question_bridge(self):
        sid = "5bc438e2-9532-47af-9fbd-4eec1a0347d9"
        with patch.object(term, "_which_cli", return_value="/usr/bin/claude"), \
                patch.object(term.claude_bridge, "settings_path",
                             return_value="/tmp/claude-bridge.json"):
            command = term.resume_command("claude", sid)
        self.assertEqual(shlex.split(command), [
            "env", "-u", "CLAUDE_CODE_SESSION_ID", "-u",
            "CODEX_COMPANION_SESSION_ID", "-u", "GROK_SESSION_ID",
            "/usr/bin/claude", "--settings", "/tmp/claude-bridge.json",
            "--resume", sid,
        ])

    def test_new_claude_session_includes_passive_question_bridge(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(term, "_which_cli", return_value="/usr/bin/claude"), \
                patch.object(term.claude_bridge, "settings_path",
                             return_value="/tmp/claude-bridge.json"), \
                patch.object(term.uuid, "uuid4",
                             return_value=type("U", (), {
                                 "__str__": lambda self:
                                 "5bc438e2-9532-47af-9fbd-4eec1a0347d9"})()), \
                patch.object(term, "new_session", return_value="sesman-claude-test") as new:
            result = term.new_cli_session("claude", tmp)
        command = new.call_args.args[1]
        self.assertIn("--settings /tmp/claude-bridge.json", command)
        self.assertIn("--session-id 5bc438e2-9532-47af-9fbd-4eec1a0347d9", command)
        self.assertEqual(result["name"], "sesman-claude-test")

    def test_codex_resume_enables_native_question_tool(self):
        sid = "5bc438e2-9532-47af-9fbd-4eec1a0347d9"
        with patch.object(term, "_which_cli", return_value="/usr/bin/codex"):
            command = term.resume_command("codex", sid)
        self.assertEqual(shlex.split(command), [
            "env", "-u", "CLAUDE_CODE_SESSION_ID", "-u",
            "CODEX_COMPANION_SESSION_ID", "-u", "GROK_SESSION_ID",
            "/usr/bin/codex", "--enable", "default_mode_request_user_input",
            "-c", "suppress_unstable_features_warning=true", "resume", sid,
        ])

    def test_new_codex_session_enables_native_question_tool(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(term, "_which_cli", return_value="/usr/bin/codex"), \
                patch.object(term, "new_session", return_value="sesman-codex-test") as new:
            result = term.new_cli_session("codex", tmp)
        command = shlex.split(new.call_args.args[1])
        self.assertIn("--enable", command)
        self.assertIn("default_mode_request_user_input", command)
        self.assertIn("suppress_unstable_features_warning=true", command)
        self.assertNotIn("--session-id", command)
        self.assertIsNone(result["sid"])
        self.assertEqual(result["name"], "sesman-codex-test")

    def test_kill_session_treats_concurrent_natural_exit_as_success(self):
        pane = {"name": "sesman-test", "server": term.MANAGED_SERVER}
        with patch.object(term, "session_info", side_effect=[pane, None]), \
                patch.object(term, "_tmux",
                             side_effect=RuntimeError("can't find session: sesman-test")):
            killed = term.kill_session("sesman-test")

        self.assertFalse(killed)

    def test_kill_session_still_reports_a_real_tmux_failure(self):
        pane = {"name": "sesman-test", "server": term.MANAGED_SERVER}
        with patch.object(term, "session_info", side_effect=[pane, pane]), \
                patch.object(term, "_tmux", side_effect=RuntimeError("permission denied")):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                term.kill_session("sesman-test")

    def test_submit_uses_bracketed_paste_before_enter(self):
        pane = {"name": "sesman-test", "server": term.MANAGED_SERVER}
        with patch.object(term, "session_info", return_value=pane), \
                patch.object(term.uuid, "uuid4") as uuid4, \
                patch.object(term.time, "sleep") as sleep, \
                patch.object(term, "_tmux") as tmux:
            uuid4.return_value.hex = "fixed"
            term.submit_text("sesman-test", "两行\n内容")

        sleep.assert_called_once_with(0.04)
        self.assertEqual(tmux.call_args_list, [
            call("set-buffer", "-b", "sesman-submit-fixed", "--", "两行\n内容",
                 server=term.MANAGED_SERVER, no_start=True),
            call("paste-buffer", "-p", "-d", "-b", "sesman-submit-fixed",
                 "-t", "sesman-test", server=term.MANAGED_SERVER, no_start=True),
            call("send-keys", "-t", "sesman-test", "--", "Enter",
                 server=term.MANAGED_SERVER, no_start=True),
        ])

    def test_graceful_stop_lets_cli_close_tmux_naturally(self):
        with patch.object(term, "has_session", side_effect=[True, False]), \
                patch.object(term, "gone", return_value=True), \
                patch.object(term, "send_keys") as send_keys, \
                patch.object(term, "kill_pids") as kill_pids, \
                patch.object(term, "kill_session") as kill_session:
            stopped = term.graceful_stop("sesman-test", [123], timeout=0)

        self.assertEqual(stopped, [123])
        send_keys.assert_called_once_with("sesman-test", "C-d")
        kill_pids.assert_not_called()
        kill_session.assert_not_called()

    def test_graceful_stop_escalates_only_after_two_eofs(self):
        with patch.object(term, "has_session", side_effect=[True, True, True]), \
                patch.object(term, "send_keys") as send_keys, \
                patch.object(term, "kill_pids", return_value=[123]) as kill_pids, \
                patch.object(term, "kill_session") as kill_session:
            stopped = term.graceful_stop("sesman-test", [123], timeout=0)

        self.assertEqual(stopped, [123])
        self.assertEqual(send_keys.call_args_list, [
            call("sesman-test", "C-d"), call("sesman-test", "C-d"),
        ])
        kill_pids.assert_called_once_with([123])
        kill_session.assert_called_once_with("sesman-test")


class TermCaptureTests(unittest.TestCase):
    def test_history_capture_joins_old_soft_wraps_before_browser_reflow(self):
        with patch.object(term, "_session_tmux", return_value="history") as run:
            self.assertEqual(term.capture_history("sesman-codex-u", 321), "history")

        run.assert_called_once_with(
            "sesman-codex-u", "capture-pane", "-J", "-p", "-e", "-t",
            "sesman-codex-u", "-S", "-321")


if __name__ == "__main__":
    unittest.main()
