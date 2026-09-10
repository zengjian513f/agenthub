import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agenthub import adapters, index


class IsolatedIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.claude_root = root / "claude"
        self.codex_root = root / "codex"
        self.grok_root = root / "grok"
        self.codex_index = root / "session_index.jsonl"
        self.cache_file = root / "cache" / "index.json"
        self.trash_dir = root / "trash"
        for directory in (self.claude_root, self.codex_root, self.grok_root):
            directory.mkdir()

        self.fresh_adapters = {
            "claude": adapters.ClaudeAdapter(),
            "codex": adapters.CodexAdapter(),
            "grok": adapters.GrokAdapter(),
        }
        self.old_state = index._state
        self.old_cursor_cache = index._cursor_cache
        self.patchers = [
            patch.object(adapters, "CLAUDE_ROOT", self.claude_root),
            patch.object(adapters, "CODEX_ROOT", self.codex_root),
            patch.object(adapters, "CODEX_INDEX", self.codex_index),
            patch.object(adapters, "GROK_ROOT", self.grok_root),
            patch.object(index, "ADAPTERS", self.fresh_adapters),
            patch.object(index, "CACHE_FILE", self.cache_file),
            patch.object(index, "TRASH_DIR", self.trash_dir),
            patch.object(index, "CHECK_TTL", -1.0),
        ]
        for patcher in self.patchers:
            patcher.start()
        index._state = index._empty_state()
        index._cursor_cache = {}
        index._search_text_cache.clear()

    def tearDown(self):
        index._state = self.old_state
        index._cursor_cache = self.old_cursor_cache
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def write_rows(path: Path, rows: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                   for row in rows) + "\n")

    def claude_session(self, name: str, title: str) -> Path:
        path = self.claude_root / "-tmp-project" / f"{name}.jsonl"
        self.write_rows(path, [{
            "type": "user", "uuid": f"{name}-user", "parentUuid": None,
            "sessionId": name, "cwd": "/tmp/project",
            "timestamp": "2026-08-11T08:00:00Z",
            "message": {"content": [{"type": "text", "text": title}]},
        }])
        return path

    def codex_session(self, sid: str, title: str | None = None,
                      parent: str = "", cutoff: int = 0,
                      thread_source: str = "user", session_id: str | None = None) -> Path:
        path = self.codex_root / "2026" / "08" / "11" / f"rollout-{sid}.jsonl"
        payload = {
            "id": sid, "session_id": session_id or sid,
            "thread_source": thread_source,
            "timestamp": "2026-08-11T08:00:00Z",
            "cwd": "/tmp/project",
        }
        if parent:
            payload["forked_from_id"] = parent
            payload["history_base"] = {
                "thread_id": parent, "end_byte_offset": cutoff,
            }
        rows = [{"type": "session_meta", "timestamp": payload["timestamp"],
                 "payload": payload}]
        if title:
            rows.append({
                "type": "response_item", "timestamp": "2026-08-11T08:00:01Z",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": title}]},
            })
        self.write_rows(path, rows)
        return path

    def test_append_refreshes_only_changed_claude_session(self):
        first = self.claude_session("first", "第一个")
        second = self.claude_session("second", "第二个")
        before = index.load(force=True)
        old_second = next(row for row in before if row["path"] == str(second))
        old_sig = index.signature()

        with first.open("a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "新标题",
                                 "timestamp": "2026-08-11T09:00:00Z"},
                                ensure_ascii=False) + "\n")
        future = time.time_ns() + 5_000_000_000
        os.utime(first, ns=(future, future))

        claude = self.fresh_adapters["claude"]
        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("append must not rebuild")), \
                patch.object(claude, "_meta", wraps=claude._meta) as parse_meta:
            after = index.load()

        changed = next(row for row in after if row["path"] == str(first))
        same = next(row for row in after if row["path"] == str(second))
        self.assertEqual(changed["title"], "新标题")
        self.assertEqual(after[0]["path"], str(first))
        self.assertEqual(same, old_second)
        self.assertNotEqual(index.signature(), old_sig)
        self.assertEqual([str(call.args[0]) for call in parse_meta.call_args_list],
                         [str(first)])

    def test_search_disk_cache_survives_restart_and_tracks_content_changes(self):
        path = self.codex_session("search-cache", "文件管理 old")
        row = index.load(force=True)[0]
        ad = self.fresh_adapters["codex"]
        self.assertIn("old", index._search_text(row))
        index._search_text_cache.clear()
        with patch.object(ad, "read", side_effect=AssertionError("must reuse disk text")):
            self.assertIn("old", index._search_text(row))
        path.write_text(path.read_text().replace("old", "new"))
        self.assertNotIn("old", index._search_text(row))
        self.assertIn("new", index._search_text(row))
        cached = list((self.cache_file.parent / "search-text").glob("*.gz"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].stat().st_mode & 0o777, 0o600)
        cached[0].write_bytes(b"invalid gzip")
        index._search_text_cache.clear()
        self.assertIn("new", index._search_text(row))

    def test_search_cache_invalidates_inherited_parent_and_streams_matches(self):
        parent = self.codex_session("search-parent", "parentneedle")
        child = self.codex_session("search-child", "child text", parent="search-parent",
                                   cutoff=parent.stat().st_size)
        row = next(s for s in index.load(force=True) if s["path"] == str(child))
        self.assertIn("parentneedle", index._search_text(row))
        parent.write_text(parent.read_text().replace("parentneedle", "parentupdate"))
        index._search_text_cache.clear()
        self.assertNotIn("parentneedle", index._search_text(row))
        self.assertIn("parentupdate", index._search_text(row))
        batches = []
        result = index.search("parentupdate", matches=batches.extend)
        self.assertEqual({r["uid"] for r in batches}, {r["uid"] for r in result["results"]})

    def test_search_limit_reports_scanned_sessions_without_claiming_entire_pool(self):
        for name in ("one", "two", "three"):
            self.codex_session(name, "needle")
        result = index.search("needle", limit=1)
        self.assertTrue(result["truncated"])
        self.assertEqual((result["scanned"], result["total_pool"]), (1, 3))

    def test_search_claude_body_matches_reader_and_persisted_rewind(self):
        path = self.claude_session("search-claude", "visible root")
        def record(uuid, parent, role, content, **extra):
            return {"type": role, "uuid": uuid, "parentUuid": parent,
                    "message": {"content": content}, **extra}
        with path.open("a") as fh:
            for row in [
                record("reply", "search-claude-user", "assistant", [
                    {"type": "text", "text": "visible reply"},
                    {"type": "thinking", "thinking": "visible thinking"},
                    {"type": "tool_use", "name": "Bash", "id": "shell", "input": {"command": "hidden-tool"}},
                    {"type": "tool_use", "name": "AskUserQuestion", "id": "ask", "input": {
                        "questions": [{"question": "visible question", "options": []}]}}]),
                record("answer", "reply", "user", [
                    {"type": "tool_result", "tool_use_id": "shell", "content": "hidden-output"},
                    {"type": "tool_result", "tool_use_id": "ask", "content": "visible answer"}]),
                record("final", "answer", "assistant", "later reply"),
            ]:
                fh.write(json.dumps(row) + "\n")
        row = index.load(force=True)[0]
        ad = self.fresh_adapters["claude"]
        full = ad.read(str(path))[0]
        expected = "\n".join(m["text"] for m in full if m["role"] in index.SEARCH_ROLES and m.get("text"))
        with patch.object(adapters, "_tool_file_changes", side_effect=AssertionError("no tool rendering")):
            self.assertEqual(index._search_text(row), expected)
        self.assertIn("visible answer", expected)
        self.assertNotIn("hidden-output", expected)
        # A rewind can change the logical body without changing the JSONL file.
        rewind = {"tip": "reply", "stale_end": path.stat().st_size}
        index._search_text_cache.clear()
        with patch.object(index.session_meta, "timeline", return_value=rewind):
            text = index._search_text(row)
        self.assertNotIn("later reply", text)
        self.assertIn("visible reply", text)

    def test_search_parse_omits_tool_rendering_but_keeps_questions_and_body(self):
        path = self.codex_session("search-content", "visible user 文件管理")
        records = [
            {"type": "function_call", "name": "exec_command", "call_id": "tool",
             "arguments": '{"cmd":"private-tool-input"}'},
            {"type": "function_call_output", "call_id": "tool", "output": "private-tool-output"},
            {"type": "function_call", "name": "request_user_input", "call_id": "question",
             "arguments": json.dumps({"questions": [{"header": "Pick", "id": "pick",
                 "question": "visible-question", "options": [{"label": "A", "description": "choice"}]}]})},
            {"type": "function_call_output", "call_id": "question",
             "output": '{"answers":{"pick":{"answers":["visible-answer"]}}}'},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "visible-thinking"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "visible-reply"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<INSTRUCTIONS>hidden-injection</INSTRUCTIONS>"}]},
        ]
        with path.open("a") as fh:
            for payload in records:
                fh.write(json.dumps({"type": "response_item", "payload": payload}) + "\n")
        ad = self.fresh_adapters["codex"]
        full = ad.read(str(path))[0]
        with patch.object(adapters, "_tool_file_changes", side_effect=AssertionError("no tool rendering")):
            fast = ad.read(str(path), search_only=True)[0]
        text = lambda msgs: "\n".join(m["text"] for m in msgs if m["role"] in index.SEARCH_ROLES and m.get("text"))
        self.assertEqual(text(full), text(fast))
        for term in ("visible-question", "visible-answer", "visible-thinking", "visible-reply"):
            self.assertIn(term, text(fast))
        self.assertNotIn("private-tool", text(fast))
        self.assertNotIn("hidden-injection", text(fast))

    def test_claude_mtime_only_change_does_not_reorder_session(self):
        older = self.claude_session("older", "较早会话")
        newer = self.claude_session("newer", "较新会话")
        with newer.open("a") as fh:
            fh.write(json.dumps({
                "type": "assistant", "uuid": "newer-answer",
                "parentUuid": "newer-user",
                "timestamp": "2026-08-11T09:00:00Z",
                "message": {"content": "较新回答"},
            }, ensure_ascii=False) + "\n")

        before = index.load(force=True)
        old_row = next(row for row in before if row["path"] == str(older))
        old_updated = old_row["updated"]
        old_sig = index.signature()

        # 复现真实故障：Claude 没有追加任何 JSONL 记录，只把旧文件的
        # mtime 碰到未来。索引应检测到文件变化并重读，但不能制造新活动。
        future = time.time_ns() + 86_400_000_000_000
        os.utime(older, ns=(future, future))
        after = index.load()
        refreshed = next(row for row in after if row["path"] == str(older))

        self.assertEqual(after[0]["path"], str(newer))
        self.assertEqual(refreshed["updated"], old_updated)
        self.assertNotEqual(index.signature(), old_sig)

    def test_new_and_deleted_main_sessions_update_raw_without_full_rebuild(self):
        first = self.claude_session("first", "第一个")
        index.load(force=True)
        second = self.claude_session("second", "第二个")

        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("topology diff must be incremental")):
            added = index.load()
            old_second_uid = next(row["uid"] for row in added
                                  if row["path"] == str(second))
            moved = second.with_name("second.moved.jsonl")
            second.rename(moved)
            renamed = index.load()
            first.unlink()
            removed = index.load()

        self.assertEqual({row["path"] for row in added}, {str(first), str(second)})
        self.assertEqual({row["path"] for row in renamed}, {str(first), str(moved)})
        self.assertNotIn(old_second_uid, {row["uid"] for row in renamed})
        self.assertEqual([row["path"] for row in removed], [str(moved)])

    def test_new_subagent_refreshes_only_its_parent(self):
        main = self.claude_session("parent.with-dot", "主会话")
        index.load(force=True)
        agent = main.parent / main.stem / "subagents" / "agent-a1.jsonl"
        self.write_rows(agent, [{
            "type": "assistant", "uuid": "agent-answer", "parentUuid": None,
            "message": {"content": [{"type": "text", "text": "子任务"}]},
        }])
        agent.with_suffix(".meta.json").write_text(json.dumps({
            "description": "检查索引", "agentType": "Explore",
        }, ensure_ascii=False))

        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("agent creation must be local")):
            sessions = index.load()
            old_size = sessions[0]["agent_items"][0]["size"]
            with agent.open("a") as fh:
                fh.write(json.dumps({"type": "assistant", "uuid": "more",
                                     "parentUuid": "agent-answer"}) + "\n")
            sessions = index.load()

        parent = sessions[0]
        self.assertEqual(parent["path"], str(main))
        self.assertEqual(parent["agents"], 1)
        self.assertEqual(parent["agent_items"][0]["title"], "检查索引")
        self.assertGreater(parent["agent_items"][0]["size"], old_size)

    def test_codex_fork_topology_and_logical_size_are_incremental(self):
        parent_sid = "00000000-0000-0000-0000-000000000001"
        child_sid = "00000000-0000-0000-0000-000000000002"
        parent = self.codex_session(parent_sid, "父会话标题")
        cutoff = parent.stat().st_size
        initial = index.load(force=True)
        self.assertEqual([row["sid"] for row in initial], [parent_sid])

        child = self.codex_session(child_sid, parent=parent_sid, cutoff=cutoff)
        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("fork topology is in-memory")):
            forked = index.load()
            self.assertEqual({row["sid"] for row in forked}, {parent_sid, child_sid})
            leaf = next(row for row in forked if row["sid"] == child_sid)
            self.assertEqual(leaf["root_sid"], parent_sid)
            self.assertEqual(leaf["fork_depth"], 1)
            self.assertEqual(leaf["title"], "父会话标题")
            self.assertEqual(leaf["size"], cutoff + child.stat().st_size)

            with child.open("a") as fh:
                fh.write(json.dumps({"type": "event_msg",
                                     "payload": {"type": "task_started"}}) + "\n")
            appended = next(row for row in index.load() if row["sid"] == child_sid)
            self.assertEqual(appended["size"], cutoff + child.stat().st_size)
            self.assertEqual(appended["root_sid"], parent_sid)

            child.unlink()
            restored = index.load()
        self.assertEqual([row["sid"] for row in restored], [parent_sid])

    def test_new_codex_subagent_does_not_hide_parent_incrementally(self):
        parent_sid = "00000000-0000-0000-0000-000000000031"
        agent_sid = "00000000-0000-0000-0000-000000000032"
        parent = self.codex_session(parent_sid, "主线程")
        initial = index.load(force=True)
        self.assertEqual([row["sid"] for row in initial], [parent_sid])

        agent = self.codex_session(
            agent_sid, parent=parent_sid, thread_source="subagent",
            session_id=parent_sid)
        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("subagent add must be incremental")):
            public = index.load()

        self.assertEqual([row["sid"] for row in public], [parent_sid])
        self.assertEqual(public[0]["path"], str(parent))
        raw_agent = index._state["raw"]["codex"][str(agent)]
        self.assertEqual(raw_agent["sid"], agent_sid)
        self.assertTrue(raw_agent["_is_subagent"])

    def test_codex_rename_recomputes_without_reading_rollouts(self):
        sid = "00000000-0000-0000-0000-000000000003"
        self.codex_session(sid, "原始标题")
        index.load(force=True)
        self.codex_index.write_text(json.dumps({
            "id": sid, "thread_name": "重命名后",
            "updated_at": "2026-08-11T09:00:00Z",
        }, ensure_ascii=False) + "\n")

        codex = self.fresh_adapters["codex"]
        with patch.object(codex, "_raw_meta",
                          side_effect=AssertionError("rename must not parse rollout")):
            session = index.load()[0]
        self.assertEqual(session["title"], "重命名后")
        self.assertEqual(session["renamed_to"], "重命名后")

    def test_current_cache_restores_raw_and_uid_map_without_rebuild(self):
        path = self.claude_session("cached", "缓存会话")
        built = index.load(force=True)
        self.assertTrue(self.cache_file.is_file())
        index._state = index._empty_state()

        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("valid cache must be enough")):
            restored = index.load()
        self.assertEqual(restored, built)
        self.assertEqual(index.get(built[0]["uid"])["path"], str(path))
        self.assertIn(str(path), index._state["raw"]["claude"])

    def test_non_object_cache_safely_falls_back_to_inventory_scan(self):
        self.claude_session("cache-shape", "缓存形状")
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text("[]")

        with patch.object(index, "_scan_raw", wraps=index._scan_raw) as scanned:
            sessions = index.load()

        self.assertEqual(sessions[0]["title"], "缓存形状")
        self.assertEqual(scanned.call_count, 1)
        payload = json.loads(self.cache_file.read_text())
        self.assertEqual(payload["version"], index.CACHE_VERSION)

    def test_extreme_numeric_cache_safely_falls_back(self):
        self.claude_session("cache-number", "极端缓存数值")
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({
            "version": index.CACHE_VERSION, "sig": "bad",
            "raw": {"claude": [], "codex": [], "grok": []},
            "files": {"/bad": ["claude-main", "/bad", float("inf"), 0, 0]},
        }))

        sessions = index.load()

        self.assertEqual(sessions[0]["title"], "极端缓存数值")
        self.assertEqual(json.loads(self.cache_file.read_text())["version"],
                         index.CACHE_VERSION)

    def test_stale_cache_reconciles_only_changed_owner_on_cold_start(self):
        path = self.claude_session("cached", "缓存旧标题")
        index.load(force=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "缓存新标题"},
                                ensure_ascii=False) + "\n")
        index._state = index._empty_state()

        with patch.object(index, "_scan_raw",
                          side_effect=AssertionError("stale cache must reconcile")):
            restored = index.load()
        self.assertEqual(restored[0]["title"], "缓存新标题")
        self.assertEqual(index._state["files"], index._inventory())

    def test_get_uses_published_uid_map_without_inventory_scan(self):
        self.claude_session("known", "已知会话")
        session = index.load(force=True)[0]
        with patch.object(index, "_inventory",
                          side_effect=AssertionError("get must not scan")):
            self.assertIs(index.get(session["uid"]), session)
            self.assertIsNone(index.get("missing"))
            self.assertIs(index.cached()[0], session)

    def test_signature_changes_when_index_schema_changes(self):
        files = {"/session": ("claude-main", "/session", 123, 456, 789)}
        current = index._signature(files)
        with patch.object(index, "CACHE_VERSION", index.CACHE_VERSION + 1):
            upgraded = index._signature(files)
        self.assertNotEqual(current, upgraded)

    def test_concurrent_cursor_materialization_is_per_version_singleflight(self):
        self.claude_session("cursor", "游标缓存")
        session = index.load(force=True)[0]
        workers = 8
        ready = threading.Barrier(workers + 1)
        started = threading.Event()
        release = threading.Event()
        results = []
        original = index.cursor

        def build(value):
            started.set()
            self.assertTrue(release.wait(2))
            return original(value)

        def worker():
            ready.wait()
            results.append(index.with_cursors([session]))

        with patch.object(index, "cursor", side_effect=build) as built:
            threads = [threading.Thread(target=worker) for _ in range(workers)]
            for thread in threads:
                thread.start()
            ready.wait()
            self.assertTrue(started.wait(2))
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

            # 暖缓存不再读取正文；文件 append 后才为新版本重算一次。
            index.with_cursors([session])
            with Path(session["path"]).open("a") as fh:
                fh.write(json.dumps({"type": "assistant", "uuid": "later",
                                     "parentUuid": "cursor-user"}) + "\n")
            index.with_cursors([session])

        self.assertEqual(built.call_count, 2)
        self.assertEqual(len(results), workers)
        self.assertTrue(all(result == results[0] for result in results))

    def test_concurrent_initial_load_is_singleflight(self):
        index._state = index._empty_state()
        workers = 8
        ready = threading.Barrier(workers + 1)
        started = threading.Event()
        release = threading.Event()
        calls = []
        results = []

        def scan_raw(files=None):
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(2))
            return {name: {} for name in self.fresh_adapters}

        def worker():
            ready.wait()
            results.append(index.load())

        with patch.object(index, "_scan_raw", side_effect=scan_raw), \
                patch.object(index, "_write_cache"):
            threads = [threading.Thread(target=worker) for _ in range(workers)]
            for thread in threads:
                thread.start()
            ready.wait()
            self.assertTrue(started.wait(2))
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), workers)
        self.assertTrue(all(result is results[0] for result in results))

    def test_concurrent_waiters_reuse_dirty_refresh_until_ttl(self):
        path = self.claude_session("busy", "持续写入")
        index.load(force=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "uuid": "reply",
                                 "parentUuid": "busy-user",
                                 "message": {"content": "第一段"}}) + "\n")

        first_inventory = index._inventory()
        later_inventory = dict(first_inventory)
        kind, owner, size, mtime_ns, inode = later_inventory[str(path)]
        later_inventory[str(path)] = (kind, owner, size + 1, mtime_ns + 1, inode)

        workers = 8
        ready = threading.Barrier(workers + 1)
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        inventory_calls = []
        results = []
        claude = self.fresh_adapters["claude"]
        original = claude.session_meta

        def inventory():
            inventory_calls.append(1)
            return first_inventory if len(inventory_calls) == 1 else later_inventory

        def refresh(owner_path):
            refresh_started.set()
            self.assertTrue(release_refresh.wait(2))
            return original(owner_path)

        def worker():
            ready.wait()
            results.append(index.load())

        with patch.object(index, "CHECK_TTL", 1.0), \
                patch.object(index, "_inventory", side_effect=inventory), \
                patch.object(claude, "session_meta", side_effect=refresh) as refreshed:
            index._state = {**index._state, "checked_at": 0.0}
            threads = [threading.Thread(target=worker) for _ in range(workers)]
            for thread in threads:
                thread.start()
            ready.wait()
            self.assertTrue(refresh_started.wait(2))
            release_refresh.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

        self.assertEqual(refreshed.call_count, 1)
        self.assertEqual(len(inventory_calls), 2)
        self.assertEqual(len(results), workers)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertTrue(index._state["dirty"])
        self.assertGreater(index._state["checked_at"], 0)

    def test_failed_incremental_parse_keeps_old_snapshot_and_retries(self):
        path = self.claude_session("retry", "旧标题")
        old = index.load(force=True)[0]
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "新标题"},
                                ensure_ascii=False) + "\n")

        claude = self.fresh_adapters["claude"]
        original = claude.session_meta
        with patch.object(claude, "session_meta", side_effect=RuntimeError("busy")):
            failed = index.load()[0]
        self.assertEqual(failed, old)
        self.assertTrue(index._state["dirty"])

        with patch.object(claude, "session_meta", wraps=original) as retried:
            fresh = index.load()[0]
        self.assertEqual(fresh["title"], "新标题")
        self.assertEqual(retried.call_count, 1)
        self.assertFalse(index._state["dirty"])

    def test_failed_force_scan_keeps_snapshot_and_same_sig_retries_full(self):
        self.claude_session("force-retry", "完整旧快照")
        old_sessions = index.load(force=True)
        claude = self.fresh_adapters["claude"]
        original = claude.session_meta

        with patch.object(claude, "session_meta", side_effect=RuntimeError("busy")):
            failed = index.load(force=True)
        self.assertIs(failed, old_sessions)
        self.assertTrue(index._state["dirty"])

        with patch.object(index, "_refresh_raw",
                          side_effect=AssertionError("dirty same-sig must full scan")), \
                patch.object(index, "_scan_raw", wraps=index._scan_raw) as rescanned, \
                patch.object(claude, "session_meta", wraps=original):
            recovered = index.load()
        self.assertEqual(recovered[0]["title"], "完整旧快照")
        self.assertEqual(rescanned.call_count, 1)
        self.assertFalse(index._state["dirty"])

    def test_codex_name_read_failure_keeps_old_table_and_retries(self):
        sid = "00000000-0000-0000-0000-000000000004"
        self.codex_session(sid, "原始标题")
        self.codex_index.write_text(json.dumps({
            "id": sid, "thread_name": "旧名称",
            "updated_at": "2026-08-11T09:00:00Z",
        }) + "\n")
        old = index.load(force=True)[0]
        self.assertEqual(old["title"], "旧名称")

        self.codex_index.write_text(json.dumps({
            "id": sid, "thread_name": "读取成功后的新名称",
            "updated_at": "2026-08-11T09:01:00Z",
        }, ensure_ascii=False) + "\n")
        with patch("builtins.open", side_effect=OSError("index busy")):
            failed = index.load()[0]
        self.assertEqual(failed["title"], "旧名称")
        self.assertTrue(index._state["dirty"])

        recovered = index.load()[0]
        self.assertEqual(recovered["title"], "读取成功后的新名称")
        self.assertFalse(index._state["dirty"])

    def test_delete_stays_successful_if_post_move_finalize_fails(self):
        path = self.claude_session("delete", "移入回收站")
        session = index.load(force=True)[0]
        codex = self.fresh_adapters["codex"]

        with patch.object(codex, "finalize_sessions",
                          side_effect=OSError("codex index busy")):
            destination = Path(index.delete(session["uid"]))

        self.assertFalse(path.exists())
        self.assertTrue(destination.is_file())
        self.assertIsNone(index.get(session["uid"]))
        self.assertTrue(index._state["dirty"])
        self.assertIsNone(index._state["sig"])


if __name__ == "__main__":
    unittest.main()
