import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sesman import send_queue, server


class SendQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(
            send_queue, "QUEUE_FILE", Path(self.tmp.name) / "send-queue.json")
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.tmp.cleanup()

    def test_busy_codex_waits_for_native_turn_end_then_confirms(self):
        item = send_queue.enqueue(
            "codex:u", "sesman-codex-u", "下一条", [],
            {"state": "working", "ts": "2026-08-09T10:00:00Z"}, "req-1",
            {"start": 100, "head": "head", "anchor": "anchor"})
        self.assertEqual(item["state"], "queued")
        self.assertEqual(send_queue.ready(1000), [])

        send_queue.observe(
            "codex:u", [], {"state": "idle", "ts": "2099-08-09T10:01:00Z"}, now=1000)
        self.assertEqual(send_queue.ready(1000.29), [])
        ready = send_queue.ready(1000.31)
        self.assertEqual([x["id"] for x in ready], ["req-1"])

        send_queue.mark_delivering("req-1", now=1001)
        self.assertEqual(send_queue.list_for("codex:u")[0]["state"], "delivering")
        send_queue.observe("codex:u", [{"role": "user", "text": "下一条",
                                        "ts": "2000-08-09T09:59:00Z"}],
                           {"state": "working"}, now=1001.5)
        self.assertEqual(len(send_queue.list_for("codex:u")), 1)
        send_queue.observe("codex:u", [{"role": "user", "text": "下一条"}],
                           {"state": "working"}, now=1002)
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_unknown_or_stale_idle_never_makes_message_ready(self):
        send_queue.enqueue("codex:u", "pane", "消息", [], None, "unknown")
        self.assertEqual(send_queue.ready(10**12), [])
        send_queue.observe("codex:u", [],
                           {"state": "idle", "ts": "2000-01-01T00:00:00Z"},
                           now=100)
        self.assertEqual(send_queue.ready(10**12), [])
        send_queue.observe("codex:u", [],
                           {"state": "idle", "ts": "2099-01-01T00:00:00Z"},
                           now=100)
        self.assertEqual([x["id"] for x in send_queue.ready(100.31)], ["unknown"])

    def test_server_poll_advances_cursor_and_unblocks_without_browser(self):
        send_queue.enqueue(
            "codex:u", "pane", "离线后继续", [],
            {"state": "working", "ts": "2026-08-09T10:00:00Z"}, "offline",
            {"start": 100, "head": "old-head", "anchor": "old-anchor"})
        session = {"uid": "codex:u", "source": "codex", "path": "/tmp/fake"}
        result = {
            "messages": [],
            "activity": {"state": "idle", "ts": "2099-01-01T00:00:00Z"},
            "end": 240, "version": {"head": "new-head"}, "anchor": "new-anchor",
        }
        with patch.object(server.index, "get", return_value=session), patch.object(
                server.index, "messages_for", return_value=result) as read:
            server._poll_outbox()
        read.assert_called_once_with(
            session, start=100, head="old-head", anchor="old-anchor")
        row = send_queue.tracked()[0]
        self.assertEqual((row["watch_start"], row["watch_head"], row["watch_anchor"]),
                         (240, "new-head", "new-anchor"))
        self.assertTrue(row.get("ready_at"))

    def test_timeout_rechecks_from_delivery_cursor_before_failing(self):
        send_queue.enqueue(
            "codex:u", "pane", "已经收到", [], {"state": "idle"}, "recheck",
            {"start": 100, "head": "old-head", "anchor": "old-anchor"})
        send_queue.mark_delivering("recheck", now=10)
        send_queue.observe(
            "codex:u", [], {"state": "working"}, now=11,
            cursor={"start": 240, "head": "new-head", "anchor": "new-anchor"})
        row = send_queue.tracked()[0]
        self.assertEqual(row["watch_start"], 240)
        self.assertEqual(row["confirm_start"], 100)

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

    def test_subsecond_native_record_confirms_server_enqueue(self):
        send_queue.enqueue(
            "codex:u", "pane", "同一秒", [], {"state": "working"}, "millis")
        row = send_queue.tracked()[0]
        boundary = datetime.fromisoformat(row["after_ts"])
        accepted = (boundary + timedelta(milliseconds=800)).isoformat(
            timespec="milliseconds")
        send_queue.observe(
            "codex:u", [{"role": "user", "text": "同一秒", "ts": accepted}], None)
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_codex_native_trim_does_not_leave_a_duplicate_queue_bubble(self):
        send_queue.enqueue(
            "codex:u", "pane", " 2016年到底怎么了\n", [],
            {"state": "idle"}, "trimmed")
        row = send_queue.tracked()[0]
        boundary = datetime.fromisoformat(row["after_ts"])
        accepted = (boundary + timedelta(milliseconds=800)).isoformat(
            timespec="milliseconds")

        send_queue.observe("codex:u", [{
            "role": "user", "text": "2016年到底怎么了", "ts": accepted,
        }], {"state": "working"})

        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_codex_confirmation_keeps_internal_whitespace_significant(self):
        send_queue.enqueue(
            "codex:u", "pane", "echo  one", [], {"state": "idle"}, "spaces")
        send_queue.observe("codex:u", [{
            "role": "user", "text": "echo one",
        }], {"state": "working"})

        self.assertEqual(len(send_queue.list_for("codex:u")), 1)

    def test_idle_message_is_durable_idempotent_and_failure_is_visible(self):
        first = send_queue.enqueue("codex:u", "pane", "消息", [{"src": "token"}],
                                   {"state": "idle"}, "same-id")
        again = send_queue.enqueue("codex:u", "pane", "消息", [{"src": "token"}],
                                   {"state": "idle"}, "same-id")
        self.assertEqual(first, again)
        self.assertTrue(first["server"])
        self.assertEqual(len(send_queue.list_for("codex:u")), 1)

        send_queue.mark_delivering("same-id", now=10)
        self.assertEqual(send_queue.expire_deliveries(now=18.01), 1)
        failed = send_queue.list_for("codex:u")[0]
        self.assertEqual(failed["state"], "failed")
        self.assertIn("未在会话记录中确认", failed["error"])

    def test_failed_item_blocks_fifo_until_retry_or_discard(self):
        send_queue.enqueue("codex:u", "pane", "一", [], {"state": "idle"}, "one")
        send_queue.enqueue("codex:u", "pane", "二", [], {"state": "idle"}, "two")
        send_queue.mark_failed("one", "失败")
        self.assertEqual(send_queue.ready(10**12), [])
        self.assertTrue(send_queue.discard("one"))
        self.assertEqual([x["id"] for x in send_queue.ready(10**12)], ["two"])

    def test_delivery_never_appends_to_restored_codex_editor(self):
        send_queue.enqueue(
            "codex:u", "sesman-codex-u", "新消息", [],
            {"state": "idle"}, "restored")
        row = send_queue.tracked()[0]
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "sesman-codex-u"}
        footer = "gpt-5.6-sol · Context 19% used · Ready"
        screen = "\x1b[1;2m› \x1b[0m旧消息仍在编辑框\n\n" + footer

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture", return_value=screen), \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.time, "sleep"):
            server._deliver_outbox_item(row, [pane])

        submit.assert_not_called()
        failed = send_queue.list_for("codex:u")[0]
        self.assertEqual(failed["state"], "failed")
        self.assertIn("输入框已有内容", failed["error"])

    def test_idle_unknown_screen_fails_without_mutating_terminal(self):
        send_queue.enqueue(
            "codex:u", "sesman-codex-u", "继续消息", [],
            {"state": "idle"}, "unknown")
        row = send_queue.tracked()[0]
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "sesman-codex-u"}
        stale_screen = "• 已完成并上线。\n\n  - 最后一条回答。\n"

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture", return_value=stale_screen), \
                patch.object(server.term, "leave_copy_mode") as leave, \
                patch.object(server.term, "send_keys") as keys, \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.time, "sleep"):
            server._deliver_outbox_item(row, [pane])

        leave.assert_not_called()
        keys.assert_not_called()
        submit.assert_not_called()
        failed = send_queue.list_for("codex:u")[0]
        self.assertEqual(failed["state"], "failed")
        self.assertIn("输入框不可识别", failed["error"])

    def test_unknown_busy_screen_is_deferred_from_stale_idle(self):
        send_queue.enqueue(
            "codex:u", "sesman-codex-u", "下一条", [],
            {"state": "idle"}, "busy-redraw")
        row = send_queue.tracked()[0]
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "sesman-codex-u"}
        screen = "• Working (2s • esc to interrupt)\n› placeholder\n"

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture", return_value=screen), \
                patch.object(server.term, "send_keys") as keys, \
                patch.object(server.term, "submit_text") as submit, \
                patch.object(server.time, "sleep"):
            server._deliver_outbox_item(row, [pane])

        keys.assert_not_called()
        submit.assert_not_called()

    def test_new_message_is_rejected_while_failed_head_blocks_fifo(self):
        send_queue.enqueue(
            "codex:u", "sesman-codex-u", "失败消息", [],
            {"state": "idle"}, "failed")
        send_queue.mark_failed("failed", "未确认")
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "sesman-codex-u"}
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane):
            result = handler._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "后一条",
            })

        self.assertEqual(result["_status"], 409)
        self.assertIn("先重试或移除", result["error"])
        self.assertEqual(len(send_queue.list_for("codex:u")), 1)

    def test_web_send_rejects_nonempty_codex_composer_before_enqueue(self):
        session = {"uid": "codex:u", "source": "codex", "sid": "u"}
        pane = {"name": "sesman-codex-u"}
        footer = "gpt-5.6-sol · Context 19% used · Ready"
        screen = "\x1b[1;2m› \x1b[0m尚未提交的草稿\n\n" + footer
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.term, "capture", return_value=screen):
            result = handler._queue_message({
                "uid": "codex:u", "name": pane["name"], "text": "网页新消息",
            })

        self.assertEqual(result["_status"], 409)
        self.assertIn("输入框已有内容", result["error"])
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_queued_item_can_be_cancelled_before_delivery_claim(self):
        send_queue.enqueue("codex:u", "pane", "撤掉", [], {"state": "idle"}, "cancel")
        self.assertTrue(send_queue.discard(
            "cancel", "codex:u", {"queued", "failed"}))
        self.assertFalse(send_queue.mark_delivering("cancel"))
        self.assertEqual(send_queue.list_for("codex:u"), [])

    def test_codex_escape_keeps_queue_and_abort_releases_only_head(self):
        activity = {"state": "working", "ts": "2026-08-09T10:00:00Z"}
        send_queue.enqueue("codex:u", "pane", "第一条", [], activity, "first")
        send_queue.enqueue("codex:u", "pane", "第二条", [], activity, "second")

        with patch.object(server.OUTBOX_WAKE, "set") as wake:
            server._after_terminal_keys("codex:u", ["Escape"])
        wake.assert_called_once_with()
        self.assertEqual(
            [x["id"] for x in send_queue.list_for("codex:u")],
            ["first", "second"])

        send_queue.observe(
            "codex:u", [],
            {"state": "aborted", "ts": "2099-08-09T10:01:00Z"}, now=1000)
        self.assertEqual(send_queue.ready(1000.29), [])
        self.assertEqual(
            [x["id"] for x in send_queue.ready(1000.31)], ["first"])

    def test_claude_escape_persists_activity_stop_without_touching_codex_worker(self):
        stopped = {"state": "aborted", "ts": "2099-01-01T00:00:00Z"}
        with patch.object(server.OUTBOX_WAKE, "set") as wake, \
                patch.object(server.session_meta, "stop_activity",
                             return_value=stopped) as stop:
            result = server._after_terminal_keys("claude:u", ["Escape"])
        wake.assert_not_called()
        stop.assert_called_once_with("claude:u")
        self.assertEqual(result, stopped)


if __name__ == "__main__":
    unittest.main()
