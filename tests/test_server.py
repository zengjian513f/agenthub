import json
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


class PaneLinkingTests(unittest.TestCase):
    """pane 归谁：自己的 CLI、续写过继的原会话 pane 算；pane 里派出的孙辈会话不算。"""

    ORIGIN = {"uid": "claude:origin", "source": "claude", "sid": "origin-sid",
              "continued_in": "claude:next"}
    NEXT = {"uid": "claude:next", "source": "claude", "sid": "next-sid"}
    SPAWNED = {"uid": "grok:child", "source": "grok", "sid": "child-sid"}
    PANES = [{"name": "agenthub-claude-origin-s", "pid": 10, "owned": True}]

    def setUp(self):
        from agenthub import index, live
        # 进程树：pane 根 10 → claude 11（origin）→ daemon 12 → claude 13（next 的进程）；
        # 11 又派出 grok 14。有 CLI 隔着就不属于 pane：13、14 都不直接属于 10。
        belongs = {(11, 10): True, (10, 10): True, (13, 10): False, (14, 10): False}
        patches = [
            patch.object(server.term, "session_name_for",
                         side_effect=lambda source, sid: f"agenthub-{source}-{sid[:8]}"),
            patch.object(server.term, "process_belongs_to",
                         side_effect=lambda pid, root, *_: belongs.get((pid, root), False)),
            patch.object(live, "pids_of", side_effect=lambda s, force=False:
                         {"claude:origin": [11], "claude:next": [13], "grok:child": [14]}[s["uid"]]),
            patch.object(index, "cached", return_value=[self.ORIGIN, self.NEXT, self.SPAWNED]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_origin_keeps_its_own_pane(self):
        self.assertEqual(server._pane_for_session(self.ORIGIN, self.PANES)["name"],
                         "agenthub-claude-origin-s")

    def test_continued_session_inherits_the_origin_pane(self):
        self.assertEqual(server._pane_for_session(self.NEXT, self.PANES)["name"],
                         "agenthub-claude-origin-s")

    def test_spawned_child_in_the_pane_has_no_console(self):
        self.assertIsNone(server._pane_for_session(self.SPAWNED, self.PANES))
        self.assertIsNone(server._pane_for_session(self.SPAWNED, self.PANES, [14]))


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
                             side_effect=lambda _pid, root, *_: root == actual["pid"]):
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


class PollCacheTests(unittest.TestCase):
    """轮询路径的缓存：来源没变就复用，来源一变（或 force）就重算。"""

    ROWS = [{"uid": "claude:a", "source": "claude", "sid": "sid-a", "updated": "1"},
            {"uid": "claude:b", "source": "claude", "sid": "sid-b", "updated": "2"}]

    def setUp(self):
        self.terminal = server.TERMINAL
        server.TERMINAL = True
        self.reset_caches()
        self.addCleanup(self.reset_caches)
        self.addCleanup(setattr, server, "TERMINAL", self.terminal)

    @staticmethod
    def reset_caches():
        server._sessions_views.clear()
        server._live_views.clear()
        server._term_link_views.clear()
        server._panes_cache["entry"] = (0.0, -1, None, [])
        server._poll_seen.update(sessions=0.0, live=0.0)

    @staticmethod
    def handler(accept_gzip=""):
        handler = object.__new__(server.Handler)
        handler.headers = {"Accept-Encoding": accept_gzip}
        sent = []
        handler._send = lambda code, body, ctype, extra=None: sent.append(
            (code, body, ctype, extra or {}))
        handler.sent = sent
        return handler

    def sessions_env(self, sessions, built_at=1.0, meta_sig="m1", stamp=1, cursor_end=1):
        return {
            "load": patch.object(server.index, "load_snapshot",
                                 return_value=(sessions, "idx", built_at)),
            "meta": patch.object(server.session_meta, "signature", return_value=meta_sig),
            "stamp": patch.object(server.debug_runs, "stamp", return_value=stamp),
            "filter": patch.object(server.debug_runs, "filter_rows",
                                   side_effect=lambda rows, _="": list(rows)),
            "cursors": patch.object(server.index, "with_cursors",
                                    side_effect=lambda rows: [
                                        dict(r, cursor={"end": cursor_end}) for r in rows]),
            "enrich": patch.object(server.session_meta, "enrich",
                                   side_effect=lambda rows, _t=None: rows),
        }

    def list_request(self, patches, q, accept_gzip=""):
        mocks = {name: item.start() for name, item in patches.items()}
        try:
            handler = self.handler(accept_gzip)
            handler._api_get("/api/sessions", q)
            code, body, _ctype, extra = handler.sent[-1]
            if extra.get("Content-Encoding") == "gzip":
                body = server.gzip.decompress(body)
            return code, json.loads(body), extra, mocks, handler
        finally:
            for item in reversed(list(patches.values())):
                item.stop()

    def test_unchanged_poll_reuses_the_view_and_a_new_snapshot_rebuilds_it(self):
        rows = list(self.ROWS)
        code, doc, _, _, _ = self.list_request(self.sessions_env(rows), {})
        self.assertEqual(code, 200)
        self.assertEqual([r["uid"] for r in doc["sessions"]], ["claude:a", "claude:b"])
        self.assertEqual(doc["sessions"][0]["cursor"], {"end": 1})
        sig = doc["sig"]

        _, doc2, _, mocks, _ = self.list_request(self.sessions_env(rows), {"sig": [sig]})
        self.assertEqual(doc2, {"unchanged": True, "sig": sig})
        mocks["cursors"].assert_not_called()          # 命中：没有再算游标

        # 同内容的新快照对象：重算，sig 不变（内容散列）
        _, doc3, _, mocks, _ = self.list_request(
            self.sessions_env(list(self.ROWS), built_at=2.0), {"sig": [sig]})
        self.assertEqual(doc3, {"unchanged": True, "sig": sig})
        mocks["cursors"].assert_called_once()
        # 内容变了的新快照：sig 必须变，整份列表返回
        changed = [dict(self.ROWS[0], title="x"), self.ROWS[1]]
        _, doc4, _, _, _ = self.list_request(
            self.sessions_env(changed, built_at=3.0), {"sig": [sig]})
        self.assertIn("sessions", doc4)
        self.assertNotEqual(doc4["sig"], sig)

    def test_session_meta_or_debug_registry_change_invalidates_the_view(self):
        rows = list(self.ROWS)
        _, doc, _, _, _ = self.list_request(self.sessions_env(rows), {})
        sig = doc["sig"]
        patches = self.sessions_env(rows, meta_sig="m2")
        patches["enrich"] = patch.object(
            server.session_meta, "enrich",
            side_effect=lambda rs, _t=None: [dict(r, starred=True) for r in rs])
        _, doc2, _, _, _ = self.list_request(patches, {"sig": [sig]})
        self.assertIn("sessions", doc2)               # 收藏变了：sig 变，列表返回
        self.assertTrue(doc2["sessions"][0]["starred"])
        self.assertNotEqual(doc2["sig"], sig)
        _, doc3, _, mocks, _ = self.list_request(
            self.sessions_env(rows, stamp=2), {"sig": [doc2["sig"]]})
        mocks["cursors"].assert_called_once()         # 登记表版本变了：重算
        self.assertIn("sessions", doc3)

    def test_force_bypasses_the_poll_ttl_and_recomputes(self):
        rows = list(self.ROWS)
        self.list_request(self.sessions_env(rows), {})
        _, doc, _, mocks, _ = self.list_request(
            self.sessions_env(rows, cursor_end=2), {"force": ["1"], "sig": ["whatever"]})
        mocks["load"].assert_called_once_with(force=True, ttl=None)
        self.assertEqual(doc["sessions"][0]["cursor"], {"end": 2})
        _, doc2, _, mocks, _ = self.list_request(self.sessions_env(rows), {"sig": [doc["sig"]]})
        mocks["load"].assert_called_once_with(force=False, ttl=server.index.POLL_TTL)
        self.assertEqual(doc2, {"unchanged": True, "sig": doc["sig"]})

    def test_prepared_body_is_gzipped_once_and_audited_like_a_plain_response(self):
        big = [dict(r, pad="x" * 2000) for r in self.ROWS]
        with patch.object(server, "JSON_GZIP_MIN", 1024), \
                patch.object(server.gzip, "compress", wraps=server.gzip.compress) as compress:
            _, doc, extra, _, handler = self.list_request(self.sessions_env(big), {}, "gzip")
            _, doc2, extra2, _, handler2 = self.list_request(self.sessions_env(big), {}, "gzip")
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(doc, doc2)
        self.assertEqual(extra2.get("Content-Encoding"), "gzip")
        self.assertEqual(extra2["X-AgentHub-Decoded-Length"],
                         str(len(json.dumps(doc, ensure_ascii=False).encode())))
        prepared = handler2._audit_json_response
        self.assertIsInstance(prepared, server.audit.PreparedContent)
        raw = server.audit._json_bytes(doc)
        self.assertEqual(prepared.sha256, server.hashlib.sha256(raw).hexdigest())
        self.assertEqual(prepared.mime, "application/json")
        self.assertEqual(server.audit.zlib.decompress(prepared.payload), raw)

    def test_panes_reuse_within_ttl_and_refresh_on_expiry_mutation_or_force(self):
        clock = [100.0]
        generation = [7]
        with patch.object(server.term, "list_sessions",
                          side_effect=lambda: [{"name": "agenthub-x", "pid": 1, "owned": True}]) as listed, \
                patch.object(server.term, "generation", side_effect=lambda: generation[0]), \
                patch.object(server.time, "monotonic", side_effect=lambda: clock[0]):
            first = server._panes()
            self.assertIs(server._panes(), first)
            self.assertEqual(listed.call_count, 1)
            clock[0] += server.PANES_TTL + 0.1
            second = server._panes()
            self.assertIsNot(second, first)
            self.assertEqual(listed.call_count, 2)
            generation[0] += 1                   # 本进程新建/结束了会话
            self.assertIsNot(server._panes(), second)
            self.assertEqual(listed.call_count, 3)
            server._panes(force=True)
            self.assertEqual(listed.call_count, 4)
        with patch.object(server, "TERMINAL", False), \
                patch.object(server.term, "list_sessions") as listed:
            self.assertEqual(server._panes(), [])
            listed.assert_not_called()

    def test_terminal_mutations_bump_the_generation(self):
        from agenthub import term
        before = term.generation()
        fake = type("Backend", (), {"kill_session": staticmethod(lambda name: True)})
        with patch.object(term, "_owner", return_value=fake):
            self.assertTrue(term.kill_session("agenthub-x"))
        self.assertGreater(term.generation(), before)
        before = term.generation()
        with patch.object(term.procs, "kill_pids", return_value=[]):
            term.kill_pids([])
        self.assertGreater(term.generation(), before)

    def test_live_view_follows_scan_stamp_snapshot_panes_and_force(self):
        holder = {"sessions": list(self.ROWS), "panes": [], "stamp": 10.0}
        with patch.object(server.index, "cached", side_effect=lambda: holder["sessions"]), \
                patch.object(server, "_panes", side_effect=lambda force=False: holder["panes"]), \
                patch.object(server.live, "scan_stamp", side_effect=lambda: holder["stamp"]), \
                patch.object(server.live, "active_processes",
                             return_value=(["claude:a"], {"claude:a": [11], "claude:b": []})) as active, \
                patch.object(server, "_record_spawn_parents", return_value=0), \
                patch.object(server.term, "in_tmux", return_value=True), \
                patch.object(server.live, "started_at", return_value=123.0), \
                patch.object(server.debug_runs, "filter_rows", side_effect=lambda rows, _="": list(rows)):
            first = server._live_view("")
            self.assertEqual(first, {"uids": ["claude:a"], "tmux_uids": ["claude:a"],
                                     "started_at": {"claude:a": 123.0}})
            self.assertIs(server._live_view(""), first)
            self.assertEqual(active.call_count, 1)
            holder["stamp"] = 13.0                # /proc 重新扫过
            self.assertIsNot(server._live_view(""), first)
            self.assertEqual(active.call_count, 2)
            holder["panes"] = []                  # 受管会话列表换了对象
            server._live_view("")
            self.assertEqual(active.call_count, 3)
            holder["sessions"] = list(self.ROWS)  # 列表快照换了对象
            server._live_view("")
            self.assertEqual(active.call_count, 4)
            server._live_view("", force=True)
            self.assertEqual(active.call_count, 5)
            self.assertTrue(active.call_args.kwargs.get("force"))
            server._live_view("")
            self.assertEqual(active.call_count, 5)   # force 的结果可继续复用

    def test_term_list_links_are_cached_per_snapshot_and_never_mutate_cached_panes(self):
        panes = [{"name": "agenthub-claude-sid-a", "pid": 10, "owned": True}]
        holder = {"sessions": list(self.ROWS)}
        with patch.object(server, "_panes", return_value=panes), \
                patch.object(server.index, "cached", side_effect=lambda: holder["sessions"]), \
                patch.object(server.term, "session_name_for",
                             side_effect=lambda source, sid: f"agenthub-{source}-{sid}"), \
                patch.object(server.live, "pids_of", return_value=[]), \
                patch.object(server.pending_store, "active", return_value=[]), \
                patch.object(server.term, "available", return_value=True), \
                patch.object(server.term, "available_sources", return_value={}), \
                patch.object(server.term, "backend_name", return_value="ptyhost"), \
                patch.object(server.term, "backends", return_value=[]), \
                patch.object(server.debug_runs, "filter_rows", side_effect=lambda rows, _="": list(rows)), \
                patch.object(server, "_PaneLinker", wraps=server._PaneLinker) as linker:
            handler = self.handler()
            handler._api_get("/api/term/list", {})
            doc = json.loads(handler.sent[-1][1])
            self.assertEqual(doc["sessions"][0]["uid"], "claude:a")
            self.assertNotIn("uid", panes[0])     # 缓存里的 pane 行没有被就地改
            handler._api_get("/api/term/list", {})
            self.assertEqual(linker.call_count, 1)
            handler._api_get("/api/term/list", {"force": ["1"]})
            self.assertEqual(linker.call_count, 2)
            holder["sessions"] = list(self.ROWS)
            handler._api_get("/api/term/list", {})
            self.assertEqual(linker.call_count, 3)

    def test_warm_tick_only_works_for_recent_pollers(self):
        with patch.object(server.index, "load") as load, \
                patch.object(server.index, "load_snapshot", return_value=([], "", 0.0)), \
                patch.object(server, "_sessions_view") as view, \
                patch.object(server.live, "snapshot") as snapshot, \
                patch.object(server, "_live_view") as live_view, \
                patch.object(server, "_panes", return_value=[]):
            server._poll_warm_tick()
            load.assert_not_called()
            snapshot.assert_not_called()
            server._note_poll("sessions")
            server._poll_warm_tick()
            load.assert_called_once()
            view.return_value.prepare.assert_called_once()
            snapshot.assert_not_called()
            server._note_poll("live")
            server._poll_warm_tick()
            snapshot.assert_called_once()
            live_view.assert_called_once_with("")
            with patch.object(server.time, "monotonic",
                              return_value=server.time.monotonic() + server.WARM_IDLE + 1):
                server._poll_warm_tick()
            self.assertEqual(load.call_count, 2)  # 超过闲置窗口就不再预热
            self.assertEqual(snapshot.call_count, 1)

    def test_pane_linker_matches_the_scalar_helpers(self):
        origin = {"uid": "claude:origin", "source": "claude", "sid": "o", "continued_in": "claude:next"}
        self_ref = {"uid": "claude:self", "source": "claude", "sid": "s", "continued_in": "claude:self"}
        nxt = {"uid": "claude:next", "source": "claude", "sid": "n"}
        with patch.object(server.index, "cached", return_value=[self_ref, origin, nxt]):
            self.assertIs(server._continued_origin(nxt), origin)
            self.assertIsNone(server._continued_origin(self_ref))
            self.assertIsNone(server._continued_origin({"uid": "grok:x", "source": "grok"}))
            linker = server._PaneLinker([])
            self.assertIs(linker.origin_of(nxt), origin)
            self.assertIsNone(linker.origin_of(self_ref))


if __name__ == "__main__":
    unittest.main()
