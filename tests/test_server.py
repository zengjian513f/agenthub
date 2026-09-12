import re
import unittest
from unittest.mock import patch

from agenthub import server, term_host, term_tmux


class AccessAllowlistTests(unittest.TestCase):
    def test_subagent_activity_does_not_inherit_parent_stop_state(self):
        native = {"state": "working", "turn_id": "child-turn"}
        result = {"meta": {"agent_id": "child"}, "activity": native}
        with patch.object(server.session_meta, "resolve_activity",
                          side_effect=AssertionError("parent state must not reach child")):
            self.assertEqual(server._resolve_activity("codex:parent", result)["activity"], native)

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


class SpawnWatchTests(unittest.TestCase):
    """发起关系的记录不能依赖浏览器：没人开网页时服务自己也要按节奏看进程。"""

    def test_loop_records_parents_without_any_http_request(self):
        import tempfile
        import threading
        from pathlib import Path
        from agenthub import index, live, session_meta
        child = {"uid": "grok:child", "source": "grok", "sid": "child-sid",
                 "created": "2026-09-12T20:46:40+08:00"}
        parent = {"uid": "claude:root", "source": "claude", "sid": "root-sid",
                  "created": "2026-09-12T18:00:00+08:00"}
        stop = threading.Event()
        calls = []

        def spawn_parents(sessions, owned, skip=frozenset()):
            calls.append(set(skip))
            stop.set()                      # 一轮之后停下，别让测试真的睡 10 秒
            return {} if "grok:child" in skip else {"grok:child": {"source": "claude", "sid": "root-sid"}}

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"), \
                patch.object(index, "load", return_value=[parent, child]), \
                patch.object(live, "active_processes",
                             return_value=(["grok:child", "claude:root"],
                                           {"grok:child": [201], "claude:root": [100]})), \
                patch.object(live, "spawn_parents", side_effect=spawn_parents):
            server._spawn_watch_loop(stop)
            self.assertEqual(calls, [set()])
            self.assertEqual(session_meta.enrich([child])[0]["spawned_by"],
                             {"source": "claude", "sid": "root-sid"})
            # 已记录的会话下一轮由 skip 略过，不会重写
            stop.clear()
            server._spawn_watch_loop(stop)
            self.assertEqual(calls[-1], {"grok:child"})

    def test_a_failing_tick_does_not_kill_the_loop(self):
        import threading
        from agenthub import index
        stop = threading.Event()
        attempts = []

        def load():
            attempts.append(1)
            if len(attempts) >= 2:
                stop.set()
            raise OSError("inventory unavailable")

        with patch.object(index, "load", side_effect=load), \
                patch.object(server, "SPAWN_WATCH_INTERVAL", 0.01):
            server._spawn_watch_loop(stop)
        self.assertEqual(len(attempts), 2)


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

        def fake_trash(uid):
            if uid == "b":
                raise RuntimeError("请先停止会话")
            if uid == "missing":
                raise KeyError(uid)
            return f"/trash/{uid}"

        with patch.object(server.Handler, "_trash_session", staticmethod(fake_trash)), \
                patch.object(server.index, "get", rows.get), \
                patch.object(server.index, "load", return_value=list(rows.values())):
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

    def test_parent_is_protected_for_whole_batch_even_if_child_is_deleted_first(self):
        handler, replies = self._handler()
        parent = {"uid": "codex:parent", "source": "codex", "sid": "parent",
                  "title": "父"}
        child = {"uid": "codex:child", "source": "codex", "sid": "child",
                 "forked_from_id": "parent", "title": "子"}
        deleted = []

        with patch.object(server.index, "load", return_value=[parent, child]), \
                patch.object(server.index, "get",
                             side_effect=lambda uid: {parent["uid"]: parent,
                                                      child["uid"]: child}.get(uid)), \
                patch.object(server.Handler, "_trash_session",
                             side_effect=lambda uid: deleted.append(uid) or f"/trash/{uid}"):
            handler._delete_sessions({"uids": [child["uid"], parent["uid"]]})

        self.assertEqual(deleted, [child["uid"]])
        self.assertEqual(replies[0][1]["errors"], [{
            "uid": parent["uid"], "title": "父",
            "error": "父会话只能隐藏，不能删除"}])


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
        self.assertIn(
            '<link rel="manifest" href="manifest.webmanifest" crossorigin="use-credentials">',
            page,
        )
        self.assertIn('src="pwa-install.js?v=', page)
        self.assertIn("navigator.serviceWorker.register('service-worker.js')", page)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_pwa_assets_have_installable_types(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._send = lambda status, data, ctype, headers=None: replies.append(
            (status, data, ctype, headers))

        for path, expected_type in [
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/service-worker.js", "javascript"),
            ("/pwa-install.js", "javascript"),
            ("/icons/icon-192.png", "image/png"),
            ("/icons/icon-512.png", "image/png"),
        ]:
            with self.subTest(path=path):
                replies.clear()
                handler._static(path)
                status, data, ctype, headers = replies[0]
                self.assertEqual(status, 200)
                self.assertTrue(data)
                self.assertIn(expected_type, ctype)
                self.assertEqual(headers["Cache-Control"], "no-cache")

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

    def test_terminal_list_explains_disabled_service_and_missing_backend(self):
        """控制台不可用时必须说清是哪一种不可用，而不是只说"不可用"。"""
        # 每种后端都打桩它自己的判据来源，可用性与原因才不会自相矛盾。
        missing_tmux = patch.object(term_tmux, 'available', return_value=False)
        missing_host = patch.object(term_host, 'host_binary', return_value=None)
        cases = [
            (False, 'tmux', missing_tmux, '--terminal'),   # 没加 --terminal
            (True, 'tmux', missing_tmux, 'tmux'),          # 缺依赖时要点名后端
            (True, 'ptyhost', missing_host, 'cargo build'),
        ]
        for enabled, backend, missing, expected in cases:
            with self.subTest(enabled=enabled, backend=backend), missing, \
                    patch.object(server, 'TERMINAL', enabled), \
                    patch.object(server.term, 'backend_name', return_value=backend), \
                    patch.object(server.term, 'available_sources', return_value={}), \
                    patch.object(server.term, 'list_sessions', return_value=[]), \
                    patch.object(server.pending_store, 'active', return_value=[]):
                result = self.handler()._api_get('/api/term/list', {})
                self.assertFalse(result['enabled'])
                self.assertIn(expected, result['unavailable_reason'])

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
                patch.object(server.index, "cached", return_value=[session]), \
                patch.object(server.live, "active_processes",
                             return_value=([session["uid"]], {session["uid"]: [123]})), \
                patch.object(server.term, "session_name_for", return_value=pane["name"]), \
                patch.object(server.term, "list_sessions", return_value=[pane]), \
                patch.object(server.term, "process_belongs_to", return_value=True), \
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

    def test_stop_rejects_parent_whose_process_belongs_to_child(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: replies.append((payload, status)) or payload
        parent = {"uid": "codex:parent", "source": "codex", "sid": "parent",
                  "path": "/tmp/parent"}
        child = {"uid": "codex:child", "source": "codex", "sid": "child",
                 "path": "/tmp/child", "forked_from_id": "parent"}

        with patch.object(server.index, "get", return_value=parent), \
                patch.object(server.index, "cached", return_value=[parent, child]), \
                patch.object(server.live, "pids_of", return_value=[123]), \
                patch.object(server.term, "list_sessions") as panes, \
                patch.object(server.term, "graceful_stop") as stop, \
                patch.object(server.term, "kill_pids") as kill:
            handler._stop_session({"uid": parent["uid"]})

        panes.assert_not_called()
        stop.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(replies[0][1], 409)
        self.assertIn("未停止共享的子会话", replies[0][0]["error"])


class PaneOwnershipTests(unittest.TestCase):
    def test_exact_parent_name_does_not_override_owned_child_process(self):
        session = {"source": "codex", "sid": "child"}
        stale = {"name": "agenthub-codex-child", "owned": True, "pid": 100}
        actual = {"name": "agenthub-codex-parent", "owned": True, "pid": 200}

        with patch.object(server.term, "session_name_for", return_value=stale["name"]), \
                patch.object(server.term, "process_belongs_to",
                             side_effect=lambda _pid, root: root == actual["pid"]):
            pane = server._pane_for_session(session, [stale, actual], [123])

        self.assertIs(pane, actual)


class TakeoverForkTests(unittest.TestCase):
    def test_takeover_rejects_parent_whose_process_belongs_to_child(self):
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: replies.append((payload, status)) or payload
        parent = {"uid": "codex:parent", "source": "codex", "sid": "parent",
                  "path": "/tmp/parent", "cwd": "/tmp"}
        child = {"uid": "codex:child", "source": "codex", "sid": "child",
                 "path": "/tmp/child", "forked_from_id": "parent"}

        with patch.object(server.index, "get", return_value=parent), \
                patch.object(server.index, "cached", return_value=[parent, child]), \
                patch.object(server.live, "pids_of", return_value=[123]), \
                patch.object(server.term, "list_sessions") as panes, \
                patch.object(server.term, "new_session") as start:
            handler._takeover({"uid": parent["uid"]})

        panes.assert_not_called()
        start.assert_not_called()
        self.assertEqual(replies[0][1], 409)
        self.assertIn("更新的子会话", replies[0][0]["error"])


class DeleteSessionTests(unittest.TestCase):
    def test_fork_replacement_query_cannot_bypass_tmux_guard(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/session/codex%3Aold?replacement_uid=codex%3Anew"
        handler._audit_begin = lambda method, route: None
        handler._allowed = lambda: True
        replies = []
        handler._json = lambda payload, status=200: (
            replies.append((payload, status)) or payload)
        parent = {"uid": "codex:old", "source": "codex", "sid": "old-sid"}

        with patch.object(server.index, "load", return_value=[parent]), \
                patch.object(server.term, "session_name_for",
                             return_value="agenthub-codex-old"), \
                patch.object(server.term, "has_session", return_value=True), \
                patch.object(server.live, "is_live", return_value=False), \
                patch.object(server.index, "delete") as delete:
            handler.do_DELETE()

        delete.assert_not_called()
        self.assertEqual(replies[-1], ({"error": "请先停止会话"}, 409))

    def test_fork_parent_cannot_be_deleted_even_when_idle(self):
        parent = {"uid": "codex:parent", "source": "codex", "sid": "parent"}
        child = {"uid": "codex:child", "source": "codex", "sid": "child",
                 "forked_from_id": "parent"}
        with patch.object(server.index, "load", return_value=[parent, child]) as load, \
                patch.object(server.live, "is_live") as is_live, \
                patch.object(server.term, "has_session") as has_session, \
                patch.object(server.index, "delete") as delete:
            with self.assertRaisesRegex(RuntimeError, "父会话只能隐藏"):
                server.Handler._trash_session(parent["uid"])

        load.assert_called_once_with(force=True)
        is_live.assert_not_called()
        has_session.assert_not_called()
        delete.assert_not_called()


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


class ForkParentVisibilityTests(unittest.TestCase):
    @staticmethod
    def _handler():
        handler = object.__new__(server.Handler)
        replies = []
        handler._json = lambda payload, status=200: (
            replies.append((payload, status)) or payload)
        return handler, replies

    def test_only_real_parents_receive_persistent_visibility_flag(self):
        handler, replies = self._handler()
        parent = {"uid": "codex:parent", "source": "codex", "sid": "parent"}
        child = {"uid": "codex:child", "source": "codex", "sid": "child",
                 "forked_from_id": "parent"}

        with patch.object(server.index, "load", return_value=[parent, child]) as load, \
                patch.object(server.session_meta, "set_fork_parent_visible",
                             return_value={"fork_parent_visible": True}) as save:
            result = handler._set_fork_parent_visibility({
                "uids": [parent["uid"], child["uid"], "missing"], "visible": True})

        load.assert_called_once_with(force=True)
        save.assert_called_once_with(parent["uid"], True)
        self.assertEqual(result["updated"], [{
            "uid": parent["uid"], "fork_parent_visible": True}])
        self.assertEqual(result["errors"], [
            {"uid": child["uid"], "error": "会话不是父会话"},
            {"uid": "missing", "error": "会话不存在"},
        ])
        self.assertEqual(replies[-1][1], 200)

    def test_bad_visibility_body_is_rejected_without_scanning(self):
        handler, replies = self._handler()
        with patch.object(server.index, "load") as load:
            handler._set_fork_parent_visibility({"uids": ["codex:a"]})
            handler._set_fork_parent_visibility({"uids": [], "visible": False})
        load.assert_not_called()
        self.assertEqual([status for _, status in replies], [400, 400])


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
                patch.object(server.session_meta, "enrich_one",
                             side_effect=lambda value, *_: value):
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


class NewSessionStatusPollTests(unittest.TestCase):
    """A pending row that never lands must not pin the inventory scan."""

    def setUp(self):
        self.terminal = server.TERMINAL
        server.TERMINAL = True
        server._NEW_STATUS_REFRESH_AT.clear()

    def tearDown(self):
        server.TERMINAL = self.terminal
        server._NEW_STATUS_REFRESH_AT.clear()

    @staticmethod
    def handler():
        handler = object.__new__(server.Handler)
        handler._json = lambda payload, status=200: {**payload, "_status": status}
        return handler

    def poll(self, pending, now):
        with patch.object(server.pending_store, "get", return_value=pending), \
                patch.object(server.index, "load", return_value=[]) as load, \
                patch.object(server.index, "cached", return_value=[]) as cached, \
                patch.object(server.time, "time", return_value=now):
            self.handler()._new_session_status({"name": [pending["name"]]})
        return load.call_count, cached.call_count

    def test_a_fresh_pending_session_still_refreshes_on_every_poll(self):
        now = 1_000_000.0
        pending = {"name": "agenthub-codex-new-1", "source": "codex", "sid": None,
                   "cwd": "/work", "started": now, "before": []}
        for offset in (0.0, 0.75, 1.5):
            self.assertEqual(self.poll(pending, now + offset), (1, 0))

    def test_an_aged_pending_session_refreshes_at_the_slow_interval(self):
        started = 1_000_000.0
        aged = started + server.NEW_STATUS_FAST_WINDOW + 1
        pending = {"name": "agenthub-claude-old", "source": "claude",
                   "sid": "sid-1", "cwd": "/work", "started": started, "before": []}

        self.assertEqual(self.poll(pending, aged), (1, 0))
        # Browsers keep polling at 750ms; those polls read the published list.
        for offset in (0.75, 1.5, 2.25):
            self.assertEqual(self.poll(pending, aged + offset), (0, 1))
        # The association is still discovered, just a few seconds later.
        self.assertEqual(self.poll(pending, aged + server.NEW_STATUS_SLOW_INTERVAL), (1, 0))

    def test_terminal_states_forget_their_throttle(self):
        name = "agenthub-codex-new-2"
        started = 1_000_000.0
        pending = {"name": name, "source": "codex", "sid": None,
                   "cwd": "/work", "started": started, "before": []}
        self.poll(pending, started + server.NEW_STATUS_FAST_WINDOW + 1)
        self.assertIn(name, server._NEW_STATUS_REFRESH_AT)

        with patch.object(server.pending_store, "get", return_value=None):
            self.assertTrue(self.handler()._new_session_status({"name": [name]})["gone"])
        self.assertNotIn(name, server._NEW_STATUS_REFRESH_AT)


if __name__ == "__main__":
    unittest.main()
