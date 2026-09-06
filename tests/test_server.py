import re
import unittest
from unittest.mock import patch

from agenthub import server


class AccessAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.allowed_ips = set(server.ALLOWED_IPS)
        self.allowed_networks = list(server.ALLOWED_NETWORKS)
        server.ALLOWED_IPS.clear()
        server.ALLOWED_NETWORKS.clear()

    def tearDown(self):
        server.ALLOWED_IPS.clear()
        server.ALLOWED_IPS.update(self.allowed_ips)
        server.ALLOWED_NETWORKS.clear()
        server.ALLOWED_NETWORKS.extend(self.allowed_networks)

    def test_exact_ip_still_matches(self):
        server._add_allowed("192.0.2.134")
        self.assertTrue(server._ip_allowed("192.0.2.134"))
        self.assertFalse(server._ip_allowed("192.0.2.135"))

    def test_ipv4_cidr_matches_only_addresses_in_network(self):
        server._add_allowed("10.0.0.0/24")
        self.assertTrue(server._ip_allowed("10.0.0.7"))
        self.assertFalse(server._ip_allowed("10.0.1.1"))

    def test_cidr_with_host_bits_is_normalized(self):
        server._add_allowed("10.0.0.7/24")
        self.assertEqual(str(server.ALLOWED_NETWORKS[0]), "10.0.0.0/24")


class BulkSessionDeleteTests(unittest.TestCase):
    """左栏多选删除: 一条失败不能把其余会话一起拖住。"""

    @staticmethod
    def _handler():
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda obj, code=200: replies.append((code, obj))
        return handler, replies

    def test_running_sessions_are_skipped_and_the_rest_still_go(self):
        handler, replies = self._handler()
        rows = {"a": {"title": "第一条"}, "b": {"title": "运行中"},
                "c": {"title": "第三条"}}

        def fake_trash(uid, replacement_uid=""):
            if uid == "b":
                raise RuntimeError("请先停止会话")
            if uid == "missing":
                raise KeyError(uid)
            return f"/trash/{uid}"

        with patch.object(server.Handler, "_trash_session", staticmethod(fake_trash)), \
                patch.object(server.index, "get", rows.get):
            handler._delete_sessions({"uids": ["a", "b", "c", "missing", "a"]})

        code, body = replies[0]
        self.assertEqual(code, 200)
        self.assertEqual([x["uid"] for x in body["deleted"]], ["a", "c"])
        self.assertEqual([x["trash"] for x in body["deleted"]],
                         ["/trash/a", "/trash/c"])
        self.assertEqual([(x["uid"], x["title"], x["error"]) for x in body["errors"]],
                         [("b", "运行中", "请先停止会话"),
                          ("missing", "", "会话不存在")])

    def test_empty_selection_is_rejected_before_touching_any_session(self):
        handler, replies = self._handler()
        with patch.object(server.Handler, "_trash_session",
                          staticmethod(lambda *a, **k: self.fail("不该删除任何会话"))):
            handler._delete_sessions({"uids": ["", None]})
            handler._delete_sessions({})

        self.assertEqual([code for code, _ in replies], [400, 400])
        self.assertEqual(replies[0][1]["error"], "没有选中任何会话")


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
        self.assertIn(f"<title>{expected} · 会话管理</title>", page)
        self.assertIn(f'>{expected}</span>', page)
        self.assertIn(f'title="{expected}"', page)
        self.assertNotIn("__AGENTHUB_HOSTNAME__", page)
        self.assertNotIn("__AGENTHUB_ASSET_VERSION__", page)
        self.assertIn(f"app.js?v={server.ASSET_VERSION}", page)
        self.assertIn(f"style.css?v={server.ASSET_VERSION}", page)
        self.assertIn(
            f'<meta name="agenthub-build" content="{server.ASSET_VERSION}">', page)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_thinking_messages_are_compact_timeline_notes(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._send = lambda status, data, ctype, headers=None: replies.append(
            (status, data, ctype, headers))

        handler._static("/style.css")

        status, data, ctype, _headers = replies[0]
        stylesheet = data.decode("utf-8")
        rule = re.search(
            r'\.msg\[data-role="thinking"\]\s*\{(?P<body>[^}]+)\}',
            stylesheet,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/css", ctype)
        self.assertIsNotNone(rule)
        self.assertIn("border: 0", rule.group("body"))
        self.assertIn("background: transparent", rule.group("body"))
        self.assertIn("box-shadow: none", rule.group("body"))
        self.assertIn('.msg[data-role="thinking"]::before', stylesheet)
        self.assertIn('.msg[data-role="thinking"] > .mb { min-width: 0; padding: 0; }',
                      stylesheet)


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
        rows = ["/tmp/agenthub/", "/tmp/session/"]
        with patch.object(server, "TERMINAL", True), \
                patch.object(server.term, "complete_directories",
                             return_value=rows) as complete:
            result = self.handler()._api_get(
                "/api/term/complete-dir", {"path": ["/tmp/se"]})
        self.assertEqual(result, {"directories": rows, "_status": 200})
        complete.assert_called_once_with("/tmp/se")


class CreateSessionDirectoryTests(unittest.TestCase):
    @staticmethod
    def handler():
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def test_missing_directory_returns_confirmation_without_pending_record(self):
        required = server.term.DirectoryCreationRequired(
            server.Path("/tmp/new-project"))
        with patch.object(server.index, "load", return_value=[]), \
                patch.object(server.term, "new_cli_session",
                             side_effect=required) as new, \
                patch.object(server.pending_store, "put") as put:
            result = self.handler()._create_session({
                "source": "codex", "cwd": "/tmp/new-project",
                "cols": 100, "rows": 30,
            })

        self.assertEqual(result, {
            "error": "启动目录不存在", "needs_create": True,
            "cwd": "/tmp/new-project", "_status": 409,
        })
        new.assert_called_once_with(
            "codex", "/tmp/new-project", 100, 30, create_cwd=False)
        put.assert_not_called()

    def test_explicit_confirmation_is_forwarded_to_directory_creator(self):
        info = {"name": "agenthub-codex-new-test", "source": "codex",
                "sid": None, "cwd": "/tmp/new-project", "token": "token"}
        with patch.object(server.index, "load", return_value=[]), \
                patch.object(server.term, "new_cli_session",
                             return_value=info) as new, \
                patch.object(server.pending_store, "put") as put:
            result = self.handler()._create_session({
                "source": "codex", "cwd": "/tmp/new-project",
                "create_cwd": True, "cols": 100, "rows": 30,
            })

        self.assertEqual(result["name"], info["name"])
        self.assertEqual(result["_status"], 200)
        new.assert_called_once_with(
            "codex", "/tmp/new-project", 100, 30, create_cwd=True)
        put.assert_called_once()


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
        pane = {"name": "agenthub-claude-test"}
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
        pane = {"name": "agenthub-claude-test"}
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
        pane = {"name": "agenthub-claude-test", "owned": True, "pid": 456}
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


class DeleteSessionTests(unittest.TestCase):
    @staticmethod
    def handler(path):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler._audit_begin = lambda method, route: None
        handler._allowed = lambda: True
        handler.replies = []
        handler._json = lambda payload, status=200: (
            handler.replies.append((payload, status)) or payload)
        return handler

    def test_direct_codex_fork_parent_can_be_deleted_while_child_owns_tmux(self):
        parent = {"uid": "codex:old", "source": "codex", "sid": "old-sid"}
        child = {"uid": "codex:new", "source": "codex", "sid": "new-sid",
                 "forked_from_id": "old-sid"}
        handler = self.handler(
            "/api/session/codex%3Aold?replacement_uid=codex%3Anew")

        def get(uid):
            return {parent["uid"]: parent, child["uid"]: child}.get(uid)

        with patch.object(server.index, "get", side_effect=get), \
                patch.object(server.term, "session_name_for",
                             return_value="agenthub-codex-old"), \
                patch.object(server.term, "has_session", return_value=True), \
                patch.object(server.live, "is_live", return_value=False), \
                patch.object(server.index, "delete", return_value="/trash/old") as delete, \
                patch.object(server.session_meta, "discard"), \
                patch.object(server.send_queue, "discard_uid"), \
                patch.object(server.claude_queue, "discard_uid"):
            handler.do_DELETE()

        delete.assert_called_once_with(parent["uid"])
        self.assertEqual(handler.replies[-1],
                         ({"ok": True, "trash": "/trash/old"}, 200))

    def test_unrelated_replacement_cannot_bypass_running_tmux_guard(self):
        parent = {"uid": "codex:old", "source": "codex", "sid": "old-sid"}
        unrelated = {"uid": "codex:new", "source": "codex", "sid": "new-sid",
                     "forked_from_id": "another-sid"}
        handler = self.handler(
            "/api/session/codex%3Aold?replacement_uid=codex%3Anew")

        def get(uid):
            return {parent["uid"]: parent, unrelated["uid"]: unrelated}.get(uid)

        with patch.object(server.index, "get", side_effect=get), \
                patch.object(server.term, "session_name_for",
                             return_value="agenthub-codex-old"), \
                patch.object(server.term, "has_session", return_value=True), \
                patch.object(server.live, "is_live", return_value=False), \
                patch.object(server.index, "delete") as delete:
            handler.do_DELETE()

        delete.assert_not_called()
        self.assertEqual(handler.replies[-1], ({"error": "请先停止会话"}, 409))


class TmuxSessionMappingTests(unittest.TestCase):
    def test_live_fork_leaf_wins_named_parent_even_if_parent_timestamp_is_newer(self):
        pane = {"name": "agenthub-codex-old", "pid": 10, "owned": True}
        parent = {"uid": "codex:old", "source": "codex", "sid": "old",
                  "path": "/sessions/old", "updated": "2026-09-06T12:00:00Z"}
        child = {"uid": "codex:new", "source": "codex", "sid": "new",
                 "path": "/sessions/new", "forked_from_id": "old",
                 "updated": "2026-09-06T11:00:00Z"}

        def pids(session):
            return [123] if session["uid"] == child["uid"] else []

        with patch.object(server.live, "pids_of", side_effect=pids), \
                patch.object(server, "_pane_for_session", return_value=pane):
            linked = server._sessions_by_pane([parent, child], [pane])

        self.assertEqual(linked[pane["name"]]["uid"], child["uid"])


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

    def test_input_history_returns_only_real_user_inputs(self):
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        session = {"uid": "codex:test", "source": "codex", "sid": "sid"}
        parsed = {
            "messages": [
                {"role": "assistant", "text": "回复", "ts": "a"},
                {"role": "user", "text": "第一条", "ts": "b"},
                {"role": "command", "text": "/rename 新标题", "ts": "c"},
                {"role": "user", "text": "隐藏输入", "counted": False, "ts": "d"},
                {"role": "tool_result", "text": "输出", "ts": "e"},
            ],
            "end": 321, "version": {"head": "head"},
        }

        with patch.object(server.index, "get", return_value=session), \
                patch.object(server.index, "session_view", return_value=session), \
                patch.object(server.index, "messages_for", return_value=parsed):
            result = handler._api_get(
                "/api/session/input-history", {"uid": [session["uid"]]})

        self.assertEqual(result["_status"], 200)
        self.assertEqual(result["history"], [
            {"text": "第一条", "ts": "b"},
            {"text": "/rename 新标题", "ts": "c"},
        ])
        self.assertEqual(result["end"], 321)


if __name__ == "__main__":
    unittest.main()
