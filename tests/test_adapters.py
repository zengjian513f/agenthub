import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sesman import adapters, index as session_index


class CodexEventTests(unittest.TestCase):
    def test_name_and_compaction_are_visible_but_not_counted_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            rollout = root / "2026" / "08" / "08" / "rollout-test.jsonl"
            rollout.parent.mkdir(parents=True)
            sid = "00000000-0000-0000-0000-000000000001"
            rows = [
                {"timestamp": "2026-08-08T10:00:00Z", "ordinal": 0,
                 "type": "session_meta", "payload": {"session_id": sid,
                 "timestamp": "2026-08-08T10:00:00Z", "cwd": "/tmp/project"}},
                {"timestamp": "2026-08-08T10:00:01Z", "ordinal": 1,
                 "type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "普通输入"}]}},
                {"timestamp": "2026-08-08T10:00:02Z", "ordinal": 2,
                 "type": "compacted", "payload": {"message": "", "replacement_history": []}},
            ]
            rollout.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            session_index = Path(tmp) / "session_index.jsonl"
            session_index.write_text(json.dumps({
                "id": sid, "thread_name": "测试名称",
                "updated_at": "2026-08-08T10:00:03Z",
            }) + "\n")

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", session_index):
                adapter = adapters.CodexAdapter()
                session = adapter.list_sessions()[0]
                messages, _ = adapter.read(session["path"])

            self.assertEqual(session["title"], "测试名称")
            self.assertEqual(session["renamed_to"], "测试名称")
            visible = [(m["role"], m["text"], m.get("counted")) for m in messages]
            self.assertIn(("command", "/rename 测试名称", False), visible)
            self.assertIn(("event", "上下文已压缩", False), visible)
            self.assertIn(("user", "普通输入", None), visible)

    def test_escape_fork_replaces_parent_and_inherits_history_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026" / "08" / "08"
            day.mkdir(parents=True)
            parent_id = "00000000-0000-0000-0000-000000000010"
            child_id = "00000000-0000-0000-0000-000000000011"
            parent = day / f"rollout-parent-{parent_id}.jsonl"
            child = day / f"rollout-child-{child_id}.jsonl"

            def row(ts, ordinal, kind, payload):
                return json.dumps({"timestamp": ts, "ordinal": ordinal,
                                   "type": kind, "payload": payload},
                                  ensure_ascii=False).encode() + b"\n"

            parent_rows = [
                row("2026-08-08T10:00:00Z", 0, "session_meta", {
                    "id": parent_id, "session_id": parent_id,
                    "timestamp": "2026-08-08T10:00:00Z", "cwd": "/tmp/project"}),
                row("2026-08-08T10:00:01Z", 1, "response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "继承的开头"}]}),
                row("2026-08-08T10:00:02Z", 2, "response_item", {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "继承的回答"}]}),
                row("2026-08-08T10:00:03Z", 3, "response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "被回退的旧尾部"}]}),
            ]
            cutoff = sum(map(len, parent_rows[:3]))
            parent.write_bytes(b"".join(parent_rows))
            child_rows = [
                row("2026-08-08T10:01:00Z", 0, "session_meta", {
                    "id": child_id, "session_id": child_id,
                    "forked_from_id": parent_id, "history_mode": "paginated",
                    "history_base": {"thread_id": parent_id,
                                     "end_byte_offset": cutoff},
                    "timestamp": "2026-08-08T10:01:00Z", "cwd": "/tmp/project"}),
                row("2026-08-08T10:01:01Z", 1, "response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "回退后的新内容"}]}),
            ]
            child.write_bytes(b"".join(child_rows))
            session_index = Path(tmp) / "session_index.jsonl"
            session_index.write_text("\n".join(json.dumps(x) for x in (
                {"id": parent_id, "thread_name": "原会话",
                 "updated_at": "2026-08-08T10:00:04Z"},
                {"id": child_id, "thread_name": "原会话",
                 "updated_at": "2026-08-08T10:01:02Z"},
            )) + "\n")

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", session_index):
                adapter = adapters.CodexAdapter()
                sessions = adapter.list_sessions()
                self.assertEqual([s["sid"] for s in sessions], [child_id])
                session = sessions[0]
                self.assertEqual(session["root_sid"], parent_id)
                self.assertEqual(session["created"], adapters._norm_ts("2026-08-08T10:00:00Z"))
                self.assertEqual(session["size"], cutoff + child.stat().st_size)
                messages, end = adapter.read(session["path"])

                texts = [m["text"] for m in messages]
                self.assertIn("继承的开头", texts)
                self.assertIn("继承的回答", texts)
                self.assertIn("回退后的新内容", texts)
                self.assertNotIn("被回退的旧尾部", texts)
                self.assertEqual(end, child.stat().st_size)

                with open(child, "ab") as fh:
                    fh.write(row("2026-08-08T10:01:03Z", 2, "response_item", {
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "仅增量"}]}))
                incremental, _ = adapter.read(session["path"], start=end)
                self.assertEqual([m["text"] for m in incremental], ["仅增量"])


class IncrementalCursorTests(unittest.TestCase):
    def test_small_claude_session_append_keeps_valid_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.jsonl"
            sid = "00000000-0000-0000-0000-000000000099"
            first = {"type": "user", "message": {"role": "user", "content": "hello"},
                     "timestamp": "2026-08-09T10:00:00Z", "cwd": tmp, "sessionId": sid}
            path.write_text(json.dumps(first) + "\n")
            session = {"uid": "claude:test", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}
            cursor = session_index.cursor(session)
            self.assertLess(cursor["end"], 4096)

            reply = {"type": "assistant",
                     "message": {"role": "assistant", "content": "new reply"},
                     "timestamp": "2026-08-09T10:00:01Z", "cwd": tmp, "sessionId": sid}
            with path.open("a") as fh:
                fh.write(json.dumps(reply) + "\n")
            result = session_index.messages_for(
                session, start=cursor["end"], head=cursor["head"],
                anchor=cursor["anchor"], append_only=True)

            self.assertFalse(result["reset"])
            self.assertEqual([(m["role"], m["text"]) for m in result["messages"]],
                             [("assistant", "new reply")])

    def test_initial_window_returns_first_100_and_last_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.jsonl"
            sid = "00000000-0000-0000-0000-000000000100"
            rows = [{
                "type": "assistant",
                "message": {"role": "assistant", "content": f"message {i:03d}"},
                "timestamp": "2026-08-09T10:00:00Z", "cwd": tmp, "sessionId": sid,
            } for i in range(650)]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            session = {"uid": "claude:window", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}

            result = session_index.messages_for(session, windowed=True)

            self.assertEqual(len(result["messages"]), 600)
            self.assertEqual(result["message_total"], 650)
            self.assertEqual(result["partial"], {"head": 100, "tail": 500,
                                                  "omitted": 50})
            self.assertEqual(result["messages"][99]["text"], "message 099")
            self.assertEqual(result["messages"][100]["text"], "message 150")
            self.assertEqual(result["messages"][-1]["text"], "message 649")


class FileChangeTests(unittest.TestCase):
    def test_apply_patch_wrapper_recovers_per_file_diffs(self):
        patch_text = """*** Begin Patch
*** Update File: src/a.py
@@
 keep
-old
+new
*** Add File: src/b.py
+one
+two
*** Delete File: src/c.py
-gone
*** End Patch"""
        wrapped = "const patch = " + json.dumps(patch_text) + ";\ntext(await tools.apply_patch(patch));"
        changes = adapters._tool_file_changes("exec", wrapped)

        self.assertEqual([x["path"] for x in changes],
                         ["src/a.py", "src/b.py", "src/c.py"])
        self.assertEqual((changes[0]["added"], changes[0]["removed"]), (1, 1))
        self.assertFalse(changes[0]["before_complete"])
        self.assertFalse(changes[0]["after_complete"])
        self.assertFalse(changes[1]["before_available"])
        self.assertTrue(changes[1]["after_complete"])
        self.assertTrue(changes[2]["before_complete"])
        self.assertFalse(changes[2]["after_available"])

    def test_claude_edit_and_write_expose_only_known_sides(self):
        edit = adapters._tool_file_changes("Edit", {
            "file_path": "demo.py", "old_string": "return 1", "new_string": "return 2",
        })[0]
        self.assertIn("-return 1", edit["patch"])
        self.assertIn("+return 2", edit["patch"])
        self.assertTrue(edit["before_available"])
        self.assertTrue(edit["after_available"])

        write = adapters._tool_file_changes("Write", {
            "file_path": "new.py", "content": "one\ntwo\n",
        })[0]
        self.assertFalse(write["before_available"])
        self.assertTrue(write["after_complete"])
        self.assertEqual((write["added"], write["removed"]), (2, 0))


class ToolSummaryTests(unittest.TestCase):
    def test_shell_wrapper_is_stripped(self):
        self.assertEqual(
            adapters._tool_summary("Bash", {"command": "bash -c 'ls -la /tmp'",
                                            "description": "列目录"}),
            "$ ls -la /tmp")
        self.assertEqual(
            adapters._tool_summary("shell", {"command": ["/usr/bin/zsh", "-lc", "git status"]}),
            "$ git status")
        self.assertEqual(adapters._tool_summary("Bash", {"command": "pytest -q"}),
                         "$ pytest -q")

    def test_codex_exec_js_wrapper(self):
        js = ('const r = await tools.exec_command({\n'
              '  cmd: "git log --oneline -3",\n  timeout_ms: 60000\n});')
        self.assertEqual(adapters._tool_summary("exec", js), "$ git log --oneline -3")

    def test_read_grep_and_fallback(self):
        self.assertEqual(adapters._tool_summary("Read", {"file_path": "/a/b.py"}),
                         "读 /a/b.py")
        self.assertTrue(adapters._tool_summary(
            "Grep", {"pattern": "veto", "path": "reports/"}).startswith("搜 veto"))
        # 未知工具退回标量 k=v 拼接
        got = adapters._tool_summary("mcp__foo__bar", {"cell_id": "50", "n": 3})
        self.assertIn("cell_id=50", got)
        # 完全无法概括时返回 None
        self.assertIsNone(adapters._tool_summary("mystery", {"blob": {"deep": [1]}}))

    def test_output_error_sniffing(self):
        self.assertTrue(adapters._output_error('{"exit_code": 1, "output": "boom"}'))
        self.assertFalse(adapters._output_error('{"exit_code": 0}'))
        self.assertTrue(adapters._output_error("command exited with code -1"))
        self.assertIsNone(adapters._output_error("plain text output"))

    def test_codex_exec_result_envelope_is_unwrapped(self):
        wrapped = [
            {"type": "input_text",
             "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({
                "chunk_id": "abc123", "wall_time_seconds": 0.04,
                "exit_code": 0, "original_token_count": 4,
                "output": "first\nsecond\n",
            })},
        ]
        text, meta = adapters._tool_output("exec", wrapped)
        self.assertEqual(text, "first\nsecond\n")
        self.assertEqual(meta, {"exit_code": 0, "duration_s": 0.04})

        combined = ("Script completed\nWall time 0.1 seconds\nOutput:\n\n"
                    + json.dumps({"session_id": 12, "chunk_id": "def456",
                                  "wall_time_seconds": 10.0,
                                  "output": "still running\n"}))
        self.assertEqual(adapters._tool_output("exec", combined),
                         ("still running\n", {"duration_s": 10.0}))

        waited = json.dumps({
            "session_id": 42, "chunk_id": "ghi789",
            "wall_time_seconds": 30.0, "original_token_count": 2000,
            "output": "line 1\nline 2\n",
        })
        self.assertEqual(adapters._tool_output("wait", waited),
                         ("line 1\nline 2\n", {"duration_s": 30.0}))
        self.assertEqual(adapters._tool_output("write_stdin", waited),
                         ("line 1\nline 2\n", {"duration_s": 30.0}))

        # 业务工具恰好返回 output 字段时，不能仅凭字段名误拆信封。
        plain = '{"output":"business value"}'
        self.assertEqual(adapters._tool_output("exec", plain), (plain, {}))

    def test_claude_messages_carry_summary_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "assistant", "timestamp": "2026-08-09T10:00:00Z",
                 "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                          "input": {"command": "bash -c 'false'"}}]}},
                {"type": "user", "timestamp": "2026-08-09T10:00:01Z",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "is_error": True, "content": "boom"}]}},
            ]
            f.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            msgs, _ = adapters.ClaudeAdapter().read(str(f))
        tool = next(m for m in msgs if m["role"] == "tool")
        result = next(m for m in msgs if m["role"] == "tool_result")
        self.assertEqual(tool["summary"], "$ false")
        self.assertEqual(tool["call_id"], "t1")
        self.assertEqual(result["call_id"], "t1")
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
