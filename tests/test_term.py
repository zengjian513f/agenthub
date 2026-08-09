import unittest
from unittest.mock import call, patch

from sesman import term


class TerminalSubmitTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
