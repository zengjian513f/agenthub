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


class StarSessionTests(unittest.TestCase):
    def test_star_requires_existing_session_and_persists(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: replies.append((payload, status)) or payload
        session = {"uid": "codex:test"}

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.session_meta, "set_starred",
                             return_value={"starred": True, "starred_at": 123}):
            result = handler._star_session({"uid": session["uid"], "starred": True})

        self.assertEqual(result, {"ok": True, "uid": "codex:test", "starred": True,
                                  "starred_at": 123})
        self.assertEqual(replies[-1][1], 200)

    def test_star_rejects_bad_body_and_unknown_session(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: replies.append((payload, status)) or payload
        handler._star_session({"uid": "codex:test", "starred": "yes"})
        with patch.object(server.index, "get", return_value=None):
            handler._star_session({"uid": "missing", "starred": True})
        self.assertEqual([status for _, status in replies], [400, 404])


if __name__ == "__main__":
    unittest.main()
