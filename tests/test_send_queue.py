import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
