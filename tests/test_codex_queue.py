import unittest
from unittest.mock import patch

from agenthub import codex_queue


class CodexNativeQueueTests(unittest.TestCase):
    def test_existing_client_request_is_reused_without_duplicate_add(self):
        existing = {
            "id": "native-1", "clientUserMessageId": "request-1",
            "input": [{"type": "text", "text": "消息"}],
        }
        with patch.object(codex_queue, "find_submission", return_value=existing), \
                patch.object(codex_queue, "_request") as request:
            result = codex_queue.enqueue_idempotent("thread", "request-1", "消息")

        self.assertEqual(result, existing)
        request.assert_not_called()

    def test_new_client_request_uses_text_input_and_returns_native_id(self):
        response = {"queuedSubmission": {
            "id": "native-2", "clientUserMessageId": "request-2",
        }}
        with patch.object(codex_queue, "find_submission", return_value=None), \
                patch.object(codex_queue, "_request", return_value=response) as request:
            result = codex_queue.enqueue_idempotent("thread", "request-2", "/rename x")

        self.assertEqual(result["id"], "native-2")
        request.assert_called_once_with("thread/queue/add", {
            "threadId": "thread", "clientUserMessageId": "request-2",
            "input": [{"type": "text", "text": "/rename x"}],
        })

    def test_lost_add_response_is_marked_ambiguous(self):
        with patch.object(codex_queue, "find_submission", return_value=None), \
                patch.object(codex_queue, "_request",
                             side_effect=codex_queue.QueueError("lost")):
            with self.assertRaises(codex_queue.QueueError) as caught:
                codex_queue.enqueue_idempotent("thread", "request", "消息")

        self.assertTrue(caught.exception.ambiguous)

    def test_delete_uses_native_submission_id(self):
        with patch.object(codex_queue, "_request",
                          return_value={"deleted": True}) as request:
            self.assertTrue(codex_queue.delete_submission("thread", "native-1"))
        request.assert_called_once_with("thread/queue/delete", {
            "threadId": "thread", "queuedSubmissionId": "native-1",
        })


if __name__ == "__main__":
    unittest.main()
