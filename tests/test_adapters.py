import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sesman import adapters


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


if __name__ == "__main__":
    unittest.main()
