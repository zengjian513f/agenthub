import unittest
from unittest.mock import patch

from sesman import server


class StaticIdentityTests(unittest.TestCase):
    def test_forwarded_ip_is_display_only_and_validated(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("10.0.0.1", 1234)
        handler.headers = {"X-Real-IP": "203.0.113.27"}
        self.assertEqual(handler._client_ip(), "10.0.0.1")
        self.assertEqual(handler._display_ip(), "203.0.113.27")
        handler.headers = {"X-Real-IP": "not-an-ip\nspoof"}
        self.assertEqual(handler._display_ip(), "10.0.0.1")

    def test_index_brand_uses_server_hostname(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._send = lambda status, data, ctype, headers=None: replies.append(
            (status, data, ctype, headers))

        handler._static("/")

        status, data, ctype, headers = replies[0]
        page = data.decode("utf-8")
        expected = server.html.escape(server.HOSTNAME)
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(f'>{expected}</span>', page)
        self.assertIn(f'title="{expected}"', page)
        self.assertNotIn("__SESMAN_HOSTNAME__", page)
        self.assertNotIn("__SESMAN_ASSET_VERSION__", page)
        self.assertIn(f"app.js?v={server.ASSET_VERSION}", page)
        self.assertIn(f"style.css?v={server.ASSET_VERSION}", page)
        self.assertEqual(headers["Cache-Control"], "no-store")


class DirectoryCompletionRouteTests(unittest.TestCase):
    @staticmethod
    def handler():
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def test_route_is_available_only_with_terminal_enabled(self):
        with patch.object(server, "TERMINAL", False), \
                patch.object(server.term, "complete_directories") as complete:
            result = self.handler()._api_get(
                "/api/term/complete-dir", {"path": ["/tmp/se"]})
        self.assertEqual(result, {"error": "终端未启用", "_status": 403})
        complete.assert_not_called()

    def test_route_returns_bounded_directory_candidates(self):
        rows = ["/tmp/sesman/", "/tmp/session/"]
        with patch.object(server, "TERMINAL", True), \
                patch.object(server.term, "complete_directories",
                             return_value=rows) as complete:
            result = self.handler()._api_get(
                "/api/term/complete-dir", {"path": ["/tmp/se"]})
        self.assertEqual(result, {"directories": rows, "_status": 200})
        complete.assert_called_once_with("/tmp/se")


class ClaudePromptTests(unittest.TestCase):
    def test_settled_prompt_waits_for_matching_native_answer(self):
        prompt = {"id": "ask-1", "state": "submitted", "questions": [{}]}
        with patch.object(server.claude_bridge, "prompt", return_value=prompt), \
                patch.object(server.claude_bridge, "clear") as clear:
            self.assertEqual(server._claude_prompt(
                "sid", [{"role": "question", "call_id": "ask-1"}]), prompt)
            clear.assert_not_called()

            self.assertIsNone(server._claude_prompt(
                "sid", [{"role": "answer", "call_id": "ask-1"}]))
            clear.assert_called_once_with("sid", "ask-1")


class ClaudeRewindTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def test_begin_records_raw_tip_without_changing_timeline(self):
        session = {"uid": "claude:test", "source": "claude", "sid": "sid",
                   "path": "/tmp/session.jsonl"}
        pane = {"name": "sesman-claude-test"}
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.index, "_claude_effective_tip", return_value="tail"), \
                patch.object(server.index, "version", return_value={"size": 321}), \
                patch.object(server.session_meta, "begin_timeline_rewind",
                             return_value={"from_tip": "tail", "stale_end": 321}) as begin:
            result = self.handler()._claude_rewind({
                "action": "begin", "uid": session["uid"], "name": pane["name"]})

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        begin.assert_called_once_with(session["uid"], "tail", 321)

    def test_sync_commits_tip_only_after_normal_screen_matches_older_node(self):
        session = {"uid": "claude:test", "source": "claude", "sid": "sid",
                   "path": "/tmp/session.jsonl"}
        pane = {"name": "sesman-claude-test"}
        pending = {"from_tip": "discarded", "stale_end": 321}
        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server, "_pane_for_session", return_value=pane), \
                patch.object(server.session_meta, "pending_timeline_rewind",
                             return_value=pending), \
                patch.object(server.term, "capture_screen_plain", return_value="❯"), \
                patch.object(server.index, "claude_screen_tip", return_value="kept"), \
                patch.object(server.session_meta, "finish_timeline_rewind",
                             return_value={"tip": "kept", "stale_end": 321}) as finish:
            result = self.handler()._claude_rewind({
                "action": "sync", "uid": session["uid"], "name": pane["name"]})

        self.assertTrue(result["changed"])
        self.assertFalse(result["pending"])
        finish.assert_called_once_with(session["uid"], "kept")

    def test_waiting_prompt_is_cleared_by_matching_cancel_result(self):
        prompt = {"id": "ask-1", "state": "waiting", "questions": [{}]}
        with patch.object(server.claude_bridge, "prompt", return_value=prompt), \
                patch.object(server.claude_bridge, "clear") as clear:
            self.assertIsNone(server._claude_prompt(
                "sid", [{"role": "tool_result", "call_id": "ask-1",
                          "error": True}]))
            clear.assert_called_once_with("sid", "ask-1")


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


class MessagesRouteTests(unittest.TestCase):
    def test_messages_route_reuses_resolved_session(self):
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: payload
        session = {"uid": "grok:test", "source": "grok", "sid": "sid",
                   "path": "/tmp/session", "cwd": "/tmp"}
        parsed = {"messages": [], "activity": None, "meta": session}

        with patch.object(server.index, "get", return_value=session) as get, \
                patch.object(server.index, "session_view", return_value=session) as view, \
                patch.object(server.index, "messages_for",
                             return_value=parsed.copy()) as messages_for, \
                patch.object(server.index, "messages",
                             side_effect=AssertionError("must not look up uid twice")), \
                patch.object(server, "_resolve_activity", side_effect=lambda uid, result: result), \
                patch.object(server.send_queue, "observe", return_value=False), \
                patch.object(server.send_queue, "list_for", return_value=[]), \
                patch.object(server.session_meta, "enrich_one", side_effect=lambda value: value):
            result = handler._api_get("/api/messages/grok%3Atest", {})

        get.assert_called_once_with("grok:test")
        view.assert_called_once_with(session, "")
        messages_for.assert_called_once()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["prompt"], None)


if __name__ == "__main__":
    unittest.main()
