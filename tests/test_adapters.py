import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sesman import adapters, index as session_index


class CodexEventTests(unittest.TestCase):
    def test_normalized_message_timestamp_keeps_milliseconds(self):
        got = adapters._norm_ts("2026-08-10T02:12:53.809Z")
        self.assertIsNotNone(got)
        self.assertEqual(datetime.fromisoformat(got).microsecond, 809000)

    def test_structured_abort_hides_duplicate_developer_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            marker = ("<turn_aborted>\nThe previous turn was interrupted on purpose. "
                      "Commands may have partially executed.\n</turn_aborted>")
            rows = [
                {"timestamp": "2026-08-09T10:00:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "developer",
                             "content": [{"type": "input_text", "text": marker}]}},
                {"timestamp": "2026-08-09T10:00:00Z", "type": "event_msg",
                 "payload": {"type": "turn_aborted", "turn_id": "turn-1",
                             "reason": "interrupted"}},
                {"timestamp": "2026-08-09T10:00:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text",
                                          "text": marker + "\n" + marker
                                                  + "\n这个作为用户正文保留"}]}},
            ]
            rollout.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            messages, _ = adapters.CodexAdapter().read(str(rollout))

        self.assertEqual([m["state"] for m in messages if m["role"] == "status"],
                         ["aborted"])
        visible = [m for m in messages if m["role"] != "status"]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["role"], "user")
        self.assertEqual(visible[0]["text"], "这个作为用户正文保留")

    def test_request_user_input_keeps_identity_and_compacts_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            question = {
                "questions": [{
                    "header": "方式",
                    "id": "bridge_status",
                    "question": "要继续吗？",
                    "options": [
                        {"label": "继续", "description": "继续执行"},
                        {"label": "停止", "description": "停在这里"},
                    ],
                }],
            }
            rows = [
                {"timestamp": "2026-08-10T10:00:00Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "request_user_input",
                             "arguments": json.dumps(question, ensure_ascii=False),
                             "call_id": "call-ok"}},
                {"timestamp": "2026-08-10T10:00:01Z", "type": "response_item",
                 "payload": {"type": "function_call_output", "call_id": "call-ok",
                             "output": json.dumps({"answers": {"bridge_status": {
                                 "answers": ["继续"]}}}, ensure_ascii=False)}},
                {"timestamp": "2026-08-10T10:00:02Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "request_user_input",
                             "arguments": question, "call_id": "call-cancel"}},
                {"timestamp": "2026-08-10T10:00:03Z", "type": "response_item",
                 "payload": {"type": "function_call_output", "call_id": "call-cancel",
                             "output": "aborted by user after 13.7s"}},
            ]
            rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                           for x in rows) + "\n")
            messages, _ = adapters.CodexAdapter().read(str(rollout))

        questions = [m for m in messages if m["role"] == "question"]
        answers = [m for m in messages if m["role"] == "answer"]
        self.assertEqual([(m["name"], m["call_id"], m["text"])
                          for m in questions], [
            ("request_user_input", "call-ok", "要继续吗？"),
            ("request_user_input", "call-cancel", "要继续吗？"),
        ])
        self.assertEqual(questions[0]["questions"][0]["options"][0], {
            "label": "继续", "description": "继续执行",
        })
        self.assertEqual([(m["call_id"], m["text"], m["error"])
                          for m in answers], [
            ("call-ok", "继续", False),
            ("call-cancel", "已取消回答", True),
        ])

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
            self.assertIn(("event", "已压缩", False), visible)
            self.assertIn(("user", "普通输入", None), visible)

    def test_compaction_prompt_rebuild_is_hidden_for_full_and_incremental_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rows = [
                {"timestamp": "2026-08-10T05:51:38Z", "ordinal": 1,
                 "type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "压缩前正文"}]}},
                {"timestamp": "2026-08-10T05:51:39Z", "ordinal": 2,
                 "type": "compacted", "payload": {"message": "", "replacement_history": []}},
                {"timestamp": "2026-08-10T05:51:40Z", "ordinal": 3,
                 "type": "response_item", "payload": {"type": "message", "role": "developer",
                 "content": [{"type": "input_text", "text":
                              "<skills_instructions>大量技能说明</skills_instructions>"}]}},
                {"timestamp": "2026-08-10T05:51:40Z", "ordinal": 4,
                 "type": "response_item", "payload": {"type": "message", "role": "developer",
                 "content": [{"type": "input_text", "text": "You are the primary agent."}]}},
                {"timestamp": "2026-08-10T05:51:40Z", "ordinal": 5,
                 "type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [
                     {"type": "input_text", "text":
                      "# AGENTS.md instructions\n<INSTRUCTIONS>项目规则</INSTRUCTIONS>"},
                     {"type": "input_text", "text":
                      "<environment_context><cwd>/tmp/project</cwd></environment_context>"},
                 ]}},
                {"timestamp": "2026-08-10T05:51:41Z", "ordinal": 6,
                 "type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "压缩后的真实问题"}]}},
            ]
            lines = [json.dumps(row) + "\n" for row in rows]
            rollout.write_text("".join(lines))
            incremental_start = len("".join(lines[:2]).encode())
            adapter = adapters.CodexAdapter()
            full, _ = adapter.read(str(rollout))
            incremental, _ = adapter.read(str(rollout), start=incremental_start)

        self.assertEqual([(m["role"], m["text"]) for m in full], [
            ("user", "压缩前正文"),
            ("event", "已压缩"),
            ("user", "压缩后的真实问题"),
        ])
        self.assertEqual([(m["role"], m["text"]) for m in incremental], [
            ("user", "压缩后的真实问题"),
        ])


class ClaudeProtocolTests(unittest.TestCase):
    def test_append_only_tree_renders_only_current_claude_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"

            def row(kind, uid, parent, text, **extra):
                record = {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "isSidechain": False, "timestamp": "2026-08-11T08:00:00Z",
                    **extra,
                }
                if kind in {"user", "assistant"}:
                    record["message"] = {"role": kind, "content": text}
                return record

            rows = [
                row("user", "u0", None, "共同开头"),
                row("assistant", "a0", "u0", "共同回答"),
                row("user", "u-old", "a0", "已放弃输入"),
                row("assistant", "a-old", "u-old", "已放弃回答"),
                row("user", "u-new", "a0", "改写后的输入"),
                row("assistant", "a-new", "u-new", "改写后的回答"),
            ]
            transcript.write_text("\n".join(
                json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
            adapter = adapters.ClaudeAdapter()

            current, _ = adapter.read(str(transcript))
            current_text = [m["text"] for m in current if m["role"] != "status"]
            self.assertEqual(current_text,
                             ["共同开头", "共同回答", "改写后的输入", "改写后的回答"])

            rewind = row("system", "rewind", "a0", "", subtype="away_summary",
                         content="已回到共同回答之后")
            with transcript.open("a") as fh:
                fh.write(json.dumps(rewind, ensure_ascii=False) + "\n")
            rewound, _ = adapter.read(str(transcript))
            rewound_text = [m["text"] for m in rewound if m["role"] != "status"]
            self.assertEqual(adapter.active_tip(str(transcript)), "rewind")

        self.assertEqual(rewound_text,
                         ["共同开头", "共同回答", "已回到共同回答之后"])

    def test_compact_protocol_becomes_one_event_for_full_and_incremental_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            rows = [
                {"type": "assistant", "timestamp": "2026-08-10T10:00:00Z",
                 "message": {"role": "assistant", "content": "压缩前回答"}},
                {"type": "user", "timestamp": "2026-08-10T10:00:01Z",
                 "message": {"role": "user", "content": "/compact"}},
                {"type": "system", "subtype": "compact_boundary",
                 "uuid": "compact-1", "timestamp": "2026-08-10T10:00:02Z",
                 "content": "Conversation compacted"},
                {"type": "user", "isCompactSummary": True,
                 "timestamp": "2026-08-10T10:00:02Z",
                 "message": {"role": "user", "content":
                             "This session is being continued from a previous conversation.\nSummary"}},
                {"type": "user", "isMeta": True,
                 "timestamp": "2026-08-10T10:00:02Z",
                 "message": {"role": "user", "content":
                             "<local-command-caveat>internal</local-command-caveat>"}},
                {"type": "user", "timestamp": "2026-08-10T10:00:02Z",
                 "message": {"role": "user", "content":
                             "<command-name>/compact</command-name>"}},
                {"type": "user", "timestamp": "2026-08-10T10:00:03Z",
                 "message": {"role": "user", "content":
                             "<local-command-stdout>Compacted</local-command-stdout>"}},
                {"type": "attachment", "timestamp": "2026-08-10T10:00:03Z",
                 "attachment": {"type": "compact_file_reference", "filename": "/tmp/a"}},
                {"type": "user", "timestamp": "2026-08-10T10:00:04Z",
                 "message": {"role": "user", "content":
                             "请解释句中的 <command-name> 标签"}},
            ]
            lines = [json.dumps(row, ensure_ascii=False) + "\n" for row in rows]
            transcript.write_text("".join(lines))
            adapter = adapters.ClaudeAdapter()
            full, _ = adapter.read(str(transcript))
            after_boundary = len("".join(lines[:3]).encode())
            incremental, _ = adapter.read(str(transcript), start=after_boundary)

        visible = [m for m in full if m["role"] != "status"]
        self.assertEqual([(m["role"], m["text"]) for m in visible], [
            ("assistant", "压缩前回答"),
            ("event", "已压缩"),
            ("user", "请解释句中的 <command-name> 标签"),
        ])
        compact = visible[1]
        self.assertEqual(compact["event_kind"], "compact")
        self.assertFalse(compact["counted"])
        self.assertEqual([m["state"] for m in full if m["role"] == "status"],
                         ["idle", "working"])
        self.assertEqual([(m["role"], m["text"]) for m in incremental
                          if m["role"] != "status"], [
            ("user", "请解释句中的 <command-name> 标签"),
        ])
        self.assertEqual([m["state"] for m in incremental if m["role"] == "status"],
                         ["working"])

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
    def test_claude_append_only_rewind_forces_full_timeline_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "branch.jsonl"
            sid = "00000000-0000-0000-0000-000000000098"

            def row(kind, uid, parent, text, **extra):
                record = {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "isSidechain": False, "timestamp": "2026-08-11T08:00:00Z",
                    "cwd": tmp, "sessionId": sid, **extra,
                }
                if kind in {"user", "assistant"}:
                    record["message"] = {"role": kind, "content": text}
                return record

            initial = [row("user", "u0", None, "共同开头"),
                       row("assistant", "a0", "u0", "共同回答")]
            path.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                        for x in initial) + "\n")
            session = {"uid": "claude:branch", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}
            before = session_index.cursor(session)
            self.assertTrue(before["anchor"].endswith("@a0"), before["anchor"])
            legacy = session_index.messages_for(
                session, start=before["end"], head=before["head"],
                anchor=before["anchor"].split("@", 1)[0])
            self.assertTrue(legacy["reset"])
            self.assertEqual([m["text"] for m in legacy["messages"]],
                             ["共同开头", "共同回答"])

            with path.open("a") as fh:
                for record in (row("user", "u1", "a0", "稍后回退的输入"),
                               row("assistant", "a1", "u1", "稍后回退的回答")):
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            appended = session_index.messages_for(
                session, start=before["end"], head=before["head"],
                anchor=before["anchor"])
            self.assertFalse(appended["reset"])
            self.assertEqual([m["text"] for m in appended["messages"]],
                             ["稍后回退的输入", "稍后回退的回答"])
            self.assertTrue(appended["anchor"].endswith("@a1"), appended["anchor"])

            rewind = row("system", "rewind", "a0", "", subtype="away_summary",
                         content="已回退一个输入")
            with path.open("a") as fh:
                fh.write(json.dumps(rewind, ensure_ascii=False) + "\n")
            result = session_index.messages_for(
                session, start=appended["end"], head=appended["version"]["head"],
                anchor=appended["anchor"])

            self.assertTrue(result["reset"])
            self.assertEqual([m["text"] for m in result["messages"]],
                             ["共同开头", "共同回答", "已回退一个输入"])

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
        # 增量读取时工具调用可能在上一批，当前批拿不到 name；明确的执行
        # 信封仍应展开，不能把 chunk_id 等内部字段显示给用户。
        self.assertEqual(adapters._tool_output(None, waited),
                         ("line 1\nline 2\n", {"duration_s": 30.0}))

        # wait 会先写一段截断提示，再在末尾放真正的执行信封。只能按严格
        # 信封字段解包；还原后的真实换行与字面量反斜杠 n 必须保持区别。
        nested = [
            {"type": "input_text",
             "text": "Script completed\nWall time 14.2 seconds\nOutput:\n"},
            {"type": "input_text",
             "text": "Warning: truncated output\nTotal output lines: 1\n\n" + json.dumps({
                 "chunk_id": "nested", "session_id": 9,
                 "wall_time_seconds": 30.0,
                 "output": "real line 1\nreal line 2\\nkept literal\n",
             })},
        ]
        self.assertEqual(adapters._tool_output("wait", nested),
                         ("real line 1\nreal line 2\\nkept literal\n",
                          {"duration_s": 30.0}))

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
                                          "is_error": True,
                                          "content": "Error: Exit code 7\nboom"}]}},
            ]
            f.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            msgs, _ = adapters.ClaudeAdapter().read(str(f))
        tool = next(m for m in msgs if m["role"] == "tool")
        result = next(m for m in msgs if m["role"] == "tool_result")
        self.assertEqual(tool["summary"], "$ false")
        self.assertEqual(tool["call_id"], "t1")
        self.assertEqual(result["call_id"], "t1")
        self.assertTrue(result["error"])
        self.assertEqual(result["exit_code"], 7)

    def test_claude_interrupt_ends_working_without_fake_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "user", "timestamp": "2026-08-09T10:00:00Z",
                 "message": {"content": "开始任务"}},
                {"type": "assistant", "timestamp": "2026-08-09T10:00:01Z",
                 "message": {"content": "正在处理"}},
                {"type": "queue-operation", "operation": "enqueue",
                 "timestamp": "2026-08-09T10:00:01.250Z", "content": "排队任务"},
                {"type": "queue-operation", "operation": "remove",
                 "timestamp": "2026-08-09T10:00:01.500Z", "content": "排队任务"},
                {"type": "queue-operation", "operation": "dequeue",
                 "timestamp": "2026-08-09T10:00:01.600Z"},
                {"type": "user", "timestamp": "2026-08-09T10:00:02Z",
                 "interruptedMessageId": "msg_123",
                 "message": {"content": [{"type": "text",
                              "text": "[Request interrupted by user]"}]}},
            ]
            f.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            msgs, _ = adapters.ClaudeAdapter().read(str(f))

        statuses = [m["state"] for m in msgs if m["role"] == "status"]
        self.assertEqual(statuses, ["working", "aborted"])
        self.assertNotIn("[Request interrupted by user]",
                         [m["text"] for m in msgs])
        queue_events = [m for m in msgs if m["role"] == "queue_operation"]
        self.assertEqual([m["text"] for m in queue_events],
                         ["排队任务", "排队任务", ""])
        self.assertEqual([m["operation"] for m in queue_events],
                         ["enqueue", "remove", "dequeue"])
        self.assertTrue(all(m["silent"] for m in queue_events))
        self.assertTrue(all(not m["counted"] for m in queue_events))

    def test_claude_notifications_recaps_and_duration_are_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            notification = """<task-notification>
<task-id>secret-id</task-id>
<status>completed</status>
<summary>Monitor \"训练任务\" stream ended</summary>
<result>第一行\n&lt;第二行&gt;</result>
</task-notification>"""
            rows = [
                {"type": "user", "timestamp": "2026-08-09T10:00:00Z",
                 "message": {"content": notification}},
                {"type": "system", "subtype": "away_summary",
                 "timestamp": "2026-08-09T10:00:01Z", "content": "任务已经收口"},
                {"type": "system", "subtype": "turn_duration",
                 "timestamp": "2026-08-09T10:00:02Z", "durationMs": 125000},
            ]
            f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
            msgs, _ = adapters.ClaudeAdapter().read(str(f))

        events = [m for m in msgs if m["role"] == "event"]
        self.assertEqual([m["event_kind"] for m in events],
                         ["task", "recap", "duration"])
        self.assertEqual(events[0]["text"], "监控结束 · 训练任务")
        self.assertEqual(events[0]["details"], "第一行\n<第二行>")
        self.assertEqual(events[0]["event_status"], "completed")
        self.assertFalse(events[0]["counted"])
        self.assertEqual(events[1]["text"], "任务已经收口")
        self.assertEqual(events[2]["duration_ms"], 125000)
        self.assertFalse(any(m["role"] == "user" for m in msgs))
        self.assertEqual([m["state"] for m in msgs if m["role"] == "status"], ["idle"])


if __name__ == "__main__":
    unittest.main()
