import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sesman import claude_queue, send_audit, server


class ClaudeQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(
            claude_queue, "QUEUE_FILE",
            Path(self.tmp.name) / "claude-send-queue.json")
        self.file_patch.start()
        self.audit_patch = patch.object(
            send_audit, "LOG_FILE", Path(self.tmp.name) / "send-events.jsonl")
        self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()
        self.file_patch.stop()
        self.tmp.cleanup()

    def enqueue(self, text="消息", item_id="req-1"):
        return claude_queue.enqueue(
            "claude:u", "sesman-claude-u", text, [], item_id,
            {"start": 100, "head": "head", "anchor": "anchor"})

    def accepted_ts(self, row=None):
        row = row or claude_queue.tracked()[0]
        return (datetime.fromisoformat(row["after_ts"])
                + timedelta(milliseconds=20)).isoformat()

    def test_persist_before_submit_and_request_id_is_idempotent(self):
        first, created = self.enqueue()
        again, repeated = self.enqueue()

        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first, again)
        self.assertEqual(len(claude_queue.list_for("claude:u")), 1)
        row = claude_queue.tracked()[0]
        self.assertEqual(
            (row["watch_start"], row["watch_head"], row["watch_anchor"]),
            (100, "head", "anchor"))
        audit = send_audit.LOG_FILE.read_text()
        self.assertIn('"event":"persisted"', audit)
        self.assertIn('"text_sha256"', audit)
        self.assertNotIn("消息", audit)

    def test_native_user_retires_submitted_row_but_old_same_text_does_not(self):
        self.enqueue("同文")
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_submitted("req-1", "claude:u")
        row = claude_queue.tracked()[0]
        old = (datetime.fromisoformat(row["after_ts"])
               - timedelta(seconds=1)).isoformat()

        claude_queue.observe("claude:u", [
            {"role": "user", "text": "同文", "ts": old}], None)
        self.assertEqual(len(claude_queue.list_for("claude:u")), 1)

        claude_queue.observe("claude:u", [
            {"role": "user", "text": "同文", "ts": self.accepted_ts(row)}], None)
        self.assertEqual(claude_queue.list_for("claude:u"), [])

    def test_native_enqueue_is_ack_not_failure_and_user_finally_retires(self):
        self.enqueue("忙时消息")
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_submitted("req-1", "claude:u")
        ts = self.accepted_ts()

        claude_queue.observe("claude:u", [{
            "role": "queue_operation", "operation": "enqueue",
            "text": "忙时消息", "ts": ts,
        }], {"state": "working"})
        self.assertEqual(
            claude_queue.list_for("claude:u")[0]["state"], "native_queued")

        claude_queue.observe("claude:u", [{
            "role": "queue_operation", "operation": "dequeue",
            "text": "", "ts": ts,
        }], {"state": "idle"})
        self.assertEqual(
            claude_queue.list_for("claude:u")[0]["state"], "submitted")

        claude_queue.observe("claude:u", [{
            "role": "user", "text": "忙时消息", "ts": ts,
        }], {"state": "working"})
        self.assertEqual(claude_queue.list_for("claude:u"), [])

    def test_exception_after_injection_is_ambiguous_not_retryable_failure(self):
        self.enqueue()
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_ambiguous("req-1", "claude:u", "Enter failed")

        item = claude_queue.list_for("claude:u")[0]
        self.assertEqual(item["state"], "ambiguous")
        self.assertIn("Enter failed", item["error"])

    def test_only_one_path_can_claim_terminal_injection(self):
        self.enqueue()

        first = claude_queue.mark_injecting("req-1", "claude:u")
        second = claude_queue.mark_injecting("req-1", "claude:u")

        self.assertEqual(first["state"], "injecting")
        self.assertIsNone(second)
        self.assertEqual(claude_queue.ready(), [])

    def test_restart_recovers_persisted_row_once_before_terminal_touch(self):
        self.enqueue("重启恢复")
        session = {"uid": "claude:u", "source": "claude", "sid": "u"}
        pane = {"name": "sesman-claude-u"}

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "leave_copy_mode"), \
                patch.object(server.term, "submit_text") as submit:
            server._deliver_claude_item(claude_queue.ready()[0], [pane])
            server._deliver_claude_item(claude_queue.tracked()[0], [pane])

        submit.assert_called_once_with(pane["name"], "重启恢复")
        self.assertEqual(
            claude_queue.list_for("claude:u")[0]["state"], "submitted")

    def test_server_queue_injects_once_for_repeated_http_request(self):
        session = {"uid": "claude:u", "source": "claude", "sid": "u"}
        pane = {"name": "sesman-claude-u"}
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        body = {
            "uid": "claude:u", "name": pane["name"], "text": "只发一次",
            "request_id": "same-request",
            "cursor": {"start": 100, "head": "head", "anchor": "anchor"},
        }

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "cursor", return_value={
                    "end": 900, "head": "server-head", "anchor": "server-anchor"}), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(handler, "_display_ip", return_value="127.0.0.1"), \
                patch.object(server.term, "leave_copy_mode"), \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.OUTBOX_WAKE, "set"):
            first = handler._queue_message(body)
            second = handler._queue_message(body)

        self.assertEqual(first["_status"], 200)
        self.assertEqual(second["_status"], 200)
        submit.assert_called_once_with(pane["name"], "只发一次")
        row = claude_queue.tracked()[0]
        self.assertEqual(
            (row["watch_start"], row["watch_head"], row["watch_anchor"]),
            (900, "server-head", "server-anchor"))
        self.assertEqual(
            claude_queue.list_for("claude:u")[0]["state"], "submitted")

        claude_queue.observe("claude:u", [{
            "role": "user", "text": "只发一次",
            "ts": self.accepted_ts(row),
        }], None)
        self.assertEqual(claude_queue.list_for("claude:u"), [])
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "cursor", return_value={
                    "end": 999, "head": "later-head", "anchor": "later-anchor"}), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(handler, "_display_ip", return_value="127.0.0.1"), \
                patch.object(server.term, "submit_text") as late_submit:
            repeated_after_ack = handler._queue_message(body)
        self.assertEqual(repeated_after_ack["_status"], 200)
        late_submit.assert_not_called()
        self.assertEqual(claude_queue.list_for("claude:u"), [])

    def test_server_poll_confirms_without_any_browser_connection(self):
        self.enqueue("离线确认")
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_submitted("req-1", "claude:u")
        session = {"uid": "claude:u", "source": "claude", "path": "/tmp/fake"}
        result = {
            "messages": [{"role": "user", "text": "离线确认",
                          "ts": self.accepted_ts()}],
            "activity": {"state": "working"},
            "end": 220, "version": {"head": "new-head"},
            "anchor": "new-anchor",
        }
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "messages_for", return_value=result) as read:
            server._poll_outbox()

        read.assert_called_once_with(
            session, start=100, head="head", anchor="anchor")
        self.assertEqual(claude_queue.list_for("claude:u"), [])

    def test_rename_dialog_is_confirmed_by_incremental_custom_title(self):
        self.enqueue("/rename")
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_submitted("req-1", "claude:u")

        claude_queue.observe("claude:u", [{
            "role": "command", "text": "/rename 新标题", "ts": None,
            "silent": True,
        }], None)

        self.assertEqual(claude_queue.list_for("claude:u"), [])

    def test_full_reset_does_not_use_timeless_old_title_as_rename_ack(self):
        self.enqueue("/rename")
        claude_queue.mark_injecting("req-1", "claude:u")
        claude_queue.mark_submitted("req-1", "claude:u")

        claude_queue.observe("claude:u", [{
            "role": "command", "text": "/rename 旧标题", "ts": None,
            "silent": True,
        }], None, {"start": 200, "head": "head", "anchor": "anchor",
                   "reset": True})

        self.assertEqual(len(claude_queue.list_for("claude:u")), 1)

    def test_ui_discard_hides_row_but_keeps_idempotency_tombstone(self):
        self.enqueue("不要复活")

        self.assertTrue(claude_queue.discard("req-1", "claude:u"))
        self.assertEqual(claude_queue.list_for("claude:u"), [])
        repeated, created = self.enqueue("不要复活")
        self.assertFalse(created)
        self.assertEqual(repeated["state"], "confirmed")
        self.assertNotIn("不要复活", claude_queue.QUEUE_FILE.read_text())


if __name__ == "__main__":
    unittest.main()
