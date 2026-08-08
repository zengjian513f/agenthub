import unittest
from unittest.mock import patch

from sesman import server


class StopSessionTests(unittest.TestCase):
    def test_tmux_stop_exits_cli_before_refreshing_live_state(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: replies.append((payload, status)) or payload
        session = {"uid": "claude:test", "source": "claude", "sid": "sid", "path": "/tmp/s"}
        pane = {"name": "sesman-claude-test", "owned": True, "pid": 456}
        events = []

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.live, "pids_of", return_value=[123]), \
                patch.object(server.term, "session_name_for", return_value=pane["name"]), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server.term, "graceful_stop",
                             side_effect=lambda name, pids:
                             events.append(("graceful", name, pids)) or [123]), \
                patch.object(server.pending_store, "discard"), \
                patch.object(server.live, "snapshot",
                             side_effect=lambda force=False: events.append(("scan", force))):
            result = handler._stop_session({"uid": session["uid"]})

        self.assertEqual(events, [
            ("graceful", pane["name"], [123]),
            ("scan", True),
        ])
        self.assertEqual(result, {"ok": True, "stopped": True, "tmux": True})
        self.assertEqual(replies, [({"ok": True, "stopped": True, "tmux": True}, 200)])


if __name__ == "__main__":
    unittest.main()
