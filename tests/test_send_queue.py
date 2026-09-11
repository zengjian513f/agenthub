import hashlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agenthub import send_queue, server


class SendQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(
            send_queue, "QUEUE_FILE", Path(self.tmp.name) / "send-queue.json")
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def handler():
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def test_snapshot_carries_atomic_process_epoch_and_revision(self):
        before = send_queue.snapshot("codex:u")
        self.assertEqual(before["outbox"], [])
        self.assertTrue(before["outbox_version"]["epoch"])

        send_queue.enqueue("codex:u", "pane", "消息", [], None, "snapshot")
        after = send_queue.snapshot("codex:u")
        self.assertEqual([x["id"] for x in after["outbox"]], ["snapshot"])
        self.assertEqual(after["outbox"][0]["afterTs"],
                         send_queue.tracked()[0]["after_ts"])
        self.assertEqual(after["outbox_version"]["epoch"],
                         before["outbox_version"]["epoch"])
        self.assertGreater(after["outbox_version"]["revision"],
                           before["outbox_version"]["revision"])

    def test_working_web_send_is_submitted_to_live_tmux_immediately(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "thread-u"}
        pane = {"name": "agenthub-codex-u"}
        driver = server.send_protocol.driver_for("codex")

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(driver, "overwrite_draft", return_value=None), \
                patch.object(server.term, "leave_copy_mode") as leave, \
                patch.object(server.term, "submit_text") as submit:
            result = self.handler()._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "下一条",
                "request_id": "request-id", "activity": {"state": "working"},
            })

        self.assertEqual(result["_status"], 200)
        self.assertEqual(result["outbox"][0]["state"], "confirming")
        self.assertEqual(result["outbox"][0]["attempts"], 1)
        leave.assert_called_once_with(pane["name"])
        submit.assert_called_once_with(pane["name"], "下一条")

    def test_replayed_working_send_does_not_touch_tmux_or_later_draft(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "thread-u"}
        pane = {"name": "agenthub-codex-u"}
        driver = server.send_protocol.driver_for("codex")
        body = {
            "uid": "codex:u", "name": pane["name"], "text": "下一条",
            "request_id": "same-request", "activity": {"state": "working"},
        }

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(driver, "overwrite_draft", return_value=None) as overwrite, \
                patch.object(server.term, "leave_copy_mode"), \
                patch.object(server.term, "submit_text") as submit:
            first = self.handler()._queue_message(body)
            second = self.handler()._queue_message({**body, "overwrite_draft": "later"})

        self.assertEqual(first["outbox"][0]["state"], "confirming")
        self.assertEqual(second["outbox"][0]["state"], "confirming")
        overwrite.assert_called_once_with(pane["name"], "")
        submit.assert_called_once_with(pane["name"], "下一条")

    def test_native_user_record_retires_terminal_receipt(self):
        send_queue.enqueue(
            "codex:u", "pane", "下一条", [], {"state": "working"}, "req-1",
            {"start": 100, "head": "head", "anchor": "anchor"})
        send_queue.mark_delivering("req-1", now=1001)
        send_queue.mark_confirming("req-1")

        row = send_queue.tracked()[0]
        boundary = datetime.fromisoformat(row["after_ts"])
        old = (boundary - timedelta(milliseconds=1)).isoformat()
        new = (boundary + timedelta(milliseconds=1)).isoformat()
        send_queue.observe("codex:u", [
            {"role": "user", "text": "下一条", "ts": old},
        ], {"state": "working"})
        self.assertEqual(len(send_queue.list_for("codex:u")), 1)
        send_queue.observe("codex:u", [
            {"role": "user", "text": "下一条", "ts": new},
        ], {"state": "working"})
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_codex_native_trim_does_not_leave_duplicate_receipt(self):
        send_queue.enqueue(
            "codex:u", "pane", " 2016年到底怎么了\n", [],
            {"state": "working"}, "trimmed")
        send_queue.mark_delivering("trimmed")
        send_queue.mark_confirming("trimmed")
        send_queue.observe("codex:u", [{
            "role": "user", "text": "2016年到底怎么了",
        }], {"state": "working"})
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_confirmation_keeps_internal_whitespace_significant(self):
        send_queue.enqueue(
            "codex:u", "pane", "echo  one", [], {"state": "working"}, "spaces")
        send_queue.mark_delivering("spaces")
        send_queue.mark_confirming("spaces")
        send_queue.observe("codex:u", [{
            "role": "user", "text": "echo one",
        }], {"state": "working"})
        self.assertEqual(len(send_queue.list_for("codex:u")), 1)

    def test_slow_native_confirmation_never_becomes_retryable(self):
        first = send_queue.enqueue(
            "codex:u", "pane", "消息", [{"src": "token"}],
            {"state": "working"}, "same-id")
        again = send_queue.enqueue(
            "codex:u", "pane", "消息", [{"src": "token"}],
            {"state": "working"}, "same-id")
        self.assertEqual(first, again)

        send_queue.mark_delivering("same-id", now=10)
        self.assertEqual(send_queue.expire_deliveries(now=18.01), 1)
        confirming = send_queue.list_for("codex:u")[0]
        self.assertEqual(confirming["state"], "confirming")
        self.assertIsNone(send_queue.retry(
            "same-id", {"state": "working"}, "codex:u"))
        send_queue.observe("codex:u", [{"role": "user", "text": "消息"}],
                           {"state": "working"}, now=40)
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_abort_of_current_turn_does_not_cancel_tui_owned_followup(self):
        send_queue.enqueue(
            "codex:u", "pane", "中断后的下一条", [],
            {"state": "working"}, "followup")
        send_queue.mark_delivering("followup", now=10)
        send_queue.mark_confirming("followup")

        send_queue.observe("codex:u", [], {
            "state": "aborted", "ts": "2099-01-01T00:00:00Z",
        }, now=11)

        row = send_queue.list_for("codex:u")[0]
        self.assertEqual(row["state"], "confirming")
        self.assertFalse(send_queue.mark_interrupted("codex:u", now=12))

    def test_restart_marks_all_pre_terminal_states_as_not_sent(self):
        send_queue._write([
            {"id": "local", "uid": "codex:u", "state": "queued", "attempts": 0},
            {"id": "crash", "uid": "codex:u", "state": "injecting", "attempts": 0},
            {"id": "claim", "uid": "codex:u", "state": "native_queuing",
             "native_claimed_at": 1, "attempts": 0},
            {"id": "phantom", "uid": "codex:u", "state": "native_queued",
             "native_id": "app-server-only", "attempts": 0},
        ])

        self.assertEqual(send_queue.fail_unsubmitted(), 4)
        rows = send_queue.list_for("codex:u")
        self.assertEqual([row["state"] for row in rows], ["failed"] * 4)
        self.assertTrue(all("未写入 Codex 终端" in row["error"] for row in rows))
        self.assertEqual(send_queue.fail_unsubmitted(), 0)

    def test_server_poll_advances_receipt_cursor_without_browser(self):
        send_queue.enqueue(
            "codex:u", "pane", "离线后确认", [], {"state": "working"}, "offline",
            {"start": 100, "head": "old-head", "anchor": "old-anchor"})
        send_queue.mark_delivering("offline")
        send_queue.mark_confirming("offline")
        session = {"uid": "codex:u", "source": "codex", "path": "/tmp/fake"}
        result = {
            "messages": [], "activity": {"state": "working"}, "end": 240,
            "version": {"head": "new-head"}, "anchor": "new-anchor",
        }
        with patch.object(server.index, "get", return_value=session), patch.object(
                server.index, "messages_for", return_value=result) as read:
            server._poll_outbox()
        read.assert_called_once_with(
            session, start=100, head="old-head", anchor="old-anchor")
        row = send_queue.tracked()[0]
        self.assertEqual((row["watch_start"], row["watch_head"], row["watch_anchor"]),
                         (240, "new-head", "new-anchor"))

    def test_timeout_rechecks_from_terminal_delivery_cursor(self):
        send_queue.enqueue(
            "codex:u", "pane", "已经收到", [], {"state": "working"}, "recheck",
            {"start": 100, "head": "old-head", "anchor": "old-anchor"})
        send_queue.mark_delivering("recheck", now=10)
        send_queue.mark_confirming("recheck")
        send_queue.observe(
            "codex:u", [], {"state": "working"}, now=11,
            cursor={"start": 240, "head": "new-head", "anchor": "new-anchor"})

        session = {"uid": "codex:u", "source": "codex", "path": "/tmp/fake"}
        result = {
            "messages": [{"role": "user", "text": "已经收到",
                          "ts": "2099-01-01T00:00:00.800Z"}],
            "activity": {"state": "working", "ts": "2099-01-01T00:00:01Z"},
            "end": 260, "version": {"head": "latest-head"},
            "anchor": "latest-anchor",
        }
        with patch.object(server.time, "time", return_value=18.01), patch.object(
                server.index, "get", return_value=session), patch.object(
                server.index, "messages_for", return_value=result) as read:
            server._poll_outbox()
        read.assert_called_once_with(
            session, start=100, head="old-head", anchor="old-anchor")
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_exception_after_delivery_claim_is_not_retryable(self):
        send_queue.enqueue(
            "codex:u", "agenthub-codex-u", "只发一次", [],
            {"state": "working"}, "ambiguous")
        row = send_queue.tracked()[0]
        pane = {"name": "agenthub-codex-u"}
        with patch.object(server.term, "leave_copy_mode"), patch.object(
                server.term, "submit_text", side_effect=OSError("lost ack")):
            server._submit_codex_item(row, pane)

        item = send_queue.list_for("codex:u")[0]
        self.assertEqual(item["state"], "confirming")
        self.assertEqual(item["attempts"], 1)
        self.assertIsNone(send_queue.retry(
            "ambiguous", {"state": "idle"}, "codex:u"))

    def test_failed_receipt_does_not_block_a_new_terminal_submission(self):
        send_queue.enqueue(
            "codex:u", "agenthub-codex-u", "旧失败", [],
            {"state": "idle"}, "failed")
        send_queue.mark_failed("failed", "未写入")
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "agenthub-codex-u"}
        driver = server.send_protocol.driver_for("codex")

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(driver, "overwrite_draft", return_value=None), \
                patch.object(server.term, "leave_copy_mode"), \
                patch.object(server.term, "submit_text") as submit:
            result = self.handler()._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "后一条",
                "request_id": "later", "activity": {"state": "working"},
            })

        self.assertEqual(result["_status"], 200)
        submit.assert_called_once_with(pane["name"], "后一条")
        self.assertEqual(
            [(row["id"], row["state"]) for row in result["outbox"]],
            [("failed", "failed"), ("later", "confirming")])

    def test_web_send_requests_confirmation_for_nonempty_codex_composer(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "agenthub-codex-u"}
        footer = "gpt-5.6-sol · Context 19% used · Ready"
        screen = "\x1b[1;2m› \x1b[0m尚未提交的草稿\n\n" + footer
        cursor = (2, 0)

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture_screen_state",
                             return_value=(screen, cursor)):
            result = self.handler()._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "网页新消息",
            })

        self.assertEqual(result["_status"], 409)
        self.assertTrue(result["draft_conflict"])
        self.assertEqual(result["draft_token"], hashlib.sha256(
            f"{cursor[0]}\0{cursor[1]}\0{screen}".encode()).hexdigest())
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_confirmed_draft_is_cleared_before_immediate_tmux_submission(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "agenthub-codex-u"}
        footer = "gpt-5.6-sol · Context 19% used · Ready"
        screen = "\x1b[1;2m› \x1b[0m尚未提交的草稿\n\n" + footer
        empty = "\x1b[2m› Ask Codex to do anything\x1b[0m\n\n" + footer
        cursor = (2, 0)
        token = hashlib.sha256(
            f"{cursor[0]}\0{cursor[1]}\0{screen}".encode()).hexdigest()

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture_screen_state",
                             side_effect=[(screen, cursor), (empty, cursor)]), \
                patch.object(server.term, "leave_copy_mode") as leave, \
                patch.object(server.term, "send_keys") as keys, \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.time, "sleep"):
            result = self.handler()._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "网页新消息",
                "request_id": "confirmed", "overwrite_draft": token,
            })

        self.assertEqual(result["_status"], 200)
        self.assertEqual(leave.call_count, 2)
        keys.assert_called_once_with(pane["name"], "C-u", "C-k")
        submit.assert_called_once_with(pane["name"], "网页新消息")
        self.assertEqual(result["outbox"][0]["state"], "confirming")

    def test_changed_draft_is_never_cleared_by_stale_confirmation(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "agenthub-codex-u"}
        screen = ("\x1b[1;2m› \x1b[0m确认期间变化的新草稿\n\n"
                  "gpt-5.6-sol · Context 19% used · Ready")

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture_screen_state",
                             return_value=(screen, (2, 0))), \
                patch.object(server.term, "send_keys") as keys:
            result = self.handler()._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "网页新消息",
                "overwrite_draft": "stale-token",
            })

        self.assertEqual(result["_status"], 409)
        self.assertTrue(result["draft_conflict"])
        keys.assert_not_called()
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_retry_writes_to_tmux_in_same_request(self):
        send_queue.enqueue(
            "codex:u", "agenthub-codex-u", "重试消息", [],
            {"state": "working"}, "retry")
        send_queue.mark_failed("retry", "未写入")
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "agenthub-codex-u"}
        driver = server.send_protocol.driver_for("codex")

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(driver, "overwrite_draft", return_value=None), \
                patch.object(server.term, "leave_copy_mode"), \
                patch.object(server.term, "submit_text") as submit:
            result = self.handler()._retry_message({
                "uid": "codex:u", "id": "retry", "activity": {"state": "working"},
            })

        self.assertEqual(result["_status"], 200)
        self.assertEqual(result["outbox"][0]["state"], "confirming")
        submit.assert_called_once_with(pane["name"], "重试消息")

    def test_failed_item_can_be_removed_idempotently(self):
        send_queue.enqueue(
            "codex:u", "pane", "未发送", [], {"state": "working"}, "failed")
        send_queue.mark_failed("failed", "未写入")
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        handler = self.handler()
        with patch.object(server.index, "get", return_value=session):
            result = handler._discard_message({"uid": "codex:u", "id": "failed"})
            again = handler._discard_message({"uid": "codex:u", "id": "failed"})
        self.assertEqual(result["_status"], 200)
        self.assertEqual(again["_status"], 200)
        self.assertEqual(again["outbox"], [])

    def test_unconfirmed_terminal_receipt_can_be_removed_without_resending(self):
        """BUG-20260912-014830-879a23: the TUI merged the paste into a draft, so
        no native record will ever retire the ``confirming`` row.  The user must
        be able to drop it; nothing may be written to the terminal for that."""
        send_queue.enqueue(
            "codex:u", "pane", "现在后端代码在哪？", [], {"state": "idle"}, "merged")
        send_queue.mark_delivering("merged")
        send_queue.mark_confirming("merged")
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.term, "send_keys") as keys:
            result = self.handler()._discard_message({"uid": "codex:u", "id": "merged"})
        self.assertEqual(result["_status"], 200)
        self.assertEqual(result["outbox"], [])
        self.assertEqual(send_queue.list_for("codex:u"), [])
        submit.assert_not_called()
        keys.assert_not_called()

    def test_codex_escape_does_not_reclassify_terminal_receipts(self):
        send_queue.enqueue(
            "codex:u", "pane", "下一条", [], {"state": "working"}, "followup")
        send_queue.mark_delivering("followup")
        send_queue.mark_confirming("followup")

        with patch.object(server.OUTBOX_WAKE, "set") as wake:
            server._after_terminal_keys("codex:u", ["Escape"])
        wake.assert_called_once_with()
        self.assertEqual(send_queue.list_for("codex:u")[0]["state"], "confirming")

    def test_claude_escape_persists_activity_stop_without_codex_wake(self):
        stopped = {"state": "aborted", "ts": "2099-01-01T00:00:00Z"}
        with patch.object(server.OUTBOX_WAKE, "set") as wake, patch.object(
                server.session_meta, "stop_activity", return_value=stopped) as stop:
            result = server._after_terminal_keys("claude:u", ["Escape"])
        wake.assert_not_called()
        stop.assert_called_once_with("claude:u")
        self.assertEqual(result, stopped)

    def test_tracking_stops_after_the_window_without_making_the_row_retryable(self):
        """A receipt Codex will never record must not poll its rollout forever.

        The stuck row is also the session's earliest one, so leaving it tracked
        starves every later send to the same session.
        """
        send_queue.enqueue("codex:u", "agenthub-codex-u", "stuck", None, None,
                           request_id="stuck")
        send_queue.mark_delivering("stuck")
        send_queue.mark_confirming("stuck")
        now = time.time()
        self.assertEqual([x["id"] for x in send_queue.tracked(now)], ["stuck"])

        later = now + send_queue.TRACK_WINDOW + 1
        self.assertEqual(send_queue.tracked(later), [])
        # The row stays visible in its non-retryable state; only polling stops.
        row = send_queue.list_for("codex:u")[0]
        self.assertEqual(row["state"], "confirming")
        self.assertIsNone(send_queue.retry("stuck"))

        with patch.object(send_queue.time, "time", return_value=later):
            send_queue.enqueue("codex:u", "agenthub-codex-u", "fresh", None, None,
                               request_id="fresh")
        self.assertEqual([x["id"] for x in send_queue.tracked(later)], ["fresh"])

    def test_codex_confirmation_replay_is_rate_limited_like_claude(self):
        """The fixed-cursor re-read grows with the rollout; it must not run per poll."""
        send_queue.enqueue("codex:u", "agenthub-codex-u", "hello", None, None,
                           request_id="item",
                           cursor={"start": 9_000_000, "head": "h", "anchor": "a"})
        send_queue.mark_delivering("item")
        send_queue.mark_confirming("item")
        session = {"uid": "codex:u", "source": "codex", "path": "/tmp/rollout.jsonl"}
        overdue = time.time() + send_queue.CONFIRM_TIMEOUT + 1
        reads = []

        def messages_for(s, **kwargs):
            reads.append(kwargs.get("start"))
            return {"messages": [], "end": 0, "anchor": "a",
                    "version": {"head": "h"}, "activity": None}

        server._CODEX_CONFIRM_REPLAY_AT.clear()
        with patch.object(send_queue, "_read", wraps=send_queue._read) as read, \
                patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "messages_for", side_effect=messages_for), \
                patch.object(server.claude_queue, "tracked", return_value=[]), \
                patch.object(send_queue, "observe"), \
                patch.object(server.time, "time", return_value=overdue):
            del read
            row = send_queue.tracked(overdue)[0]
            confirm_start = int(row["confirm_start"])
            watch_start = int(row["watch_start"] or 0)
            for _ in range(4):
                server._poll_outbox()
        # One expensive replay from the injection point, then cheap tail reads.
        self.assertEqual(reads, [confirm_start] + [watch_start] * 3)

        reads.clear()
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "messages_for", side_effect=messages_for), \
                patch.object(server.claude_queue, "tracked", return_value=[]), \
                patch.object(send_queue, "observe"), \
                patch.object(server.time, "time",
                             return_value=overdue + send_queue.CONFIRM_TIMEOUT):
            server._poll_outbox()
        self.assertEqual(reads, [confirm_start])
        server._CODEX_CONFIRM_REPLAY_AT.clear()


if __name__ == "__main__":
    unittest.main()
