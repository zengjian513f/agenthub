import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agenthub import adapters, index as session_index, session_meta


class CodexEventTests(unittest.TestCase):
    def test_normalized_message_timestamp_keeps_milliseconds(self):
        got = adapters._norm_ts("2026-08-10T02:12:53.809Z")
        self.assertIsNotNone(got)
        self.assertEqual(datetime.fromisoformat(got).microsecond, 809000)

    def test_latest_jsonl_timestamp_ignores_mtime_and_large_untimed_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            rows = [
                {"type": "user", "timestamp": "2026-08-12T07:56:28.245Z"},
                {"type": "system", "timestamp": "2026-08-12T07:56:28.409Z"},
                {"type": "metadata", "content": "x" * (adapters.TAIL_BYTES + 1)},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            future = 2_000_000_000_000_000_000
            os.utime(transcript, ns=(future, future))

            tail = adapters._tail_lines(transcript, strict=True)
            got = adapters._latest_jsonl_timestamp(transcript, tail, strict=True)

        self.assertEqual(got, adapters._norm_ts("2026-08-12T07:56:28.409Z"))

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

    def test_structured_abort_marks_only_last_assistant_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            meta = {"internal_chat_message_metadata_passthrough": {
                "turn_id": "turn-aborted",
            }}
            rows = [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user", **meta,
                    "content": [{"type": "input_text", "text": "开始"}]}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "phase": "commentary",
                    **meta, "content": [{"type": "output_text", "text": "先检查"}]}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "phase": "commentary",
                    **meta, "content": [{"type": "output_text", "text": "最后状态"}]}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                    "input": "pwd", **meta}},
                {"type": "event_msg", "payload": {
                    "type": "turn_aborted", "turn_id": "turn-aborted",
                    "reason": "interrupted by user"}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            messages, _ = adapters.CodexAdapter().read(str(rollout))

        assistant = [m for m in messages if m["role"] == "assistant"]
        self.assertNotIn("interrupted", assistant[0])
        self.assertTrue(assistant[1]["interrupted"])
        self.assertEqual(assistant[1]["interrupt_reason"], "interrupted by user")

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

    def test_turn_identity_and_native_answer_phase_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            meta = {"internal_chat_message_metadata_passthrough": {
                "turn_id": "turn-native",
            }}
            rows = [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user", **meta,
                    "content": [{"type": "input_text", "text": "开始"}]}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "phase": "commentary",
                    **meta, "content": [{"type": "output_text", "text": "处理中"}]}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "exec", "call_id": "c1",
                    "input": "pwd", **meta}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call_output", "call_id": "c1",
                    "output": "ok", **meta}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "phase": "final_answer",
                    **meta, "content": [{"type": "output_text", "text": "完成"}]}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            messages, _ = adapters.CodexAdapter().read(str(rollout))

        self.assertTrue(all(m.get("turn_id") == "turn-native" for m in messages))
        assistant = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual([(m["text"], m.get("phase")) for m in assistant], [
            ("处理中", "progress"), ("完成", "final"),
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

    def test_goal_internal_context_is_hidden_for_full_and_incremental_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            goal_text = (
                '<codex_internal_context source="goal">\n'
                "Continue working toward the active thread goal.\n"
                "</codex_internal_context>"
            )
            goal_meta = {"internal_chat_message_metadata_passthrough": {
                "turn_id": "turn-goal",
                "content_item_kinds": ["goal.internal_context"],
            }}
            rows = [
                {"timestamp": "2026-09-06T13:20:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", **goal_meta,
                             "content": [{"type": "input_text", "text": goal_text}]}},
                {"timestamp": "2026-09-06T13:20:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "继续工作"}]}},
                {"timestamp": "2026-09-06T13:20:02Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text":
                                          "请解释标签 " + goal_text}]}},
            ]
            lines = [json.dumps(row, ensure_ascii=False) + "\n" for row in rows]
            rollout.write_text("".join(lines))
            incremental_start = len(lines[0].encode())
            adapter = adapters.CodexAdapter()
            full, _ = adapter.read(str(rollout))
            incremental, _ = adapter.read(str(rollout), start=incremental_start)

        expected = [
            ("assistant", "继续工作"),
            ("user", "请解释标签 " + goal_text),
        ]
        self.assertEqual([(m["role"], m["text"]) for m in full], expected)
        self.assertEqual([(m["role"], m["text"]) for m in incremental], expected)

    def test_goal_internal_context_is_not_used_as_session_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            rollout = root / "2026" / "09" / "06" / "rollout-test.jsonl"
            rollout.parent.mkdir(parents=True)
            sid = "00000000-0000-0000-0000-000000000006"
            rows = [
                {"timestamp": "2026-09-06T13:20:00Z", "type": "session_meta",
                 "payload": {"session_id": sid, "cwd": "/tmp/project"}},
                {"timestamp": "2026-09-06T13:20:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "internal_chat_message_metadata_passthrough": {
                                 "content_item_kinds": ["goal.internal_context"],
                             },
                             "content": [{"type": "input_text", "text":
                                          "内部续跑上下文"}]}},
                {"timestamp": "2026-09-06T13:20:02Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "真实问题"}]}},
            ]
            rollout.write_text("\n".join(
                json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
            session_index = Path(tmp) / "session_index.jsonl"
            session_index.write_text("")

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", session_index):
                session = adapters.CodexAdapter().list_sessions()[0]

        self.assertEqual(session["title"], "真实问题")


class GrokAdapterTests(unittest.TestCase):
    def test_user_query_wrapper_is_removed_without_hiding_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            history = session / "chat_history.jsonl"
            rows = [
                {"type": "user", "content": [
                    {"type": "text", "text":
                     "<user_query>\n请检查这个结果\n</user_query>"},
                ]},
                {"type": "reasoning", "status": "completed",
                 "summary": [{"type": "summary_text", "text":
                              "内部分析。"}]},
                {"type": "assistant", "content": [
                    {"type": "text", "text": "这是正式回复。"},
                ]},
            ]
            history.write_text("\n".join(
                json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
            expected_end = history.stat().st_size

            messages, end = adapters.GrokAdapter().read(str(session))

        self.assertEqual([(m["role"], m["text"]) for m in messages], [
            ("user", "请检查这个结果"),
            ("thinking", "内部分析。"),
            ("assistant", "这是正式回复。"),
        ])
        self.assertEqual(end, expected_end)

    def test_image_metadata_envelope_is_removed_but_media_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            history = session / "chat_history.jsonl"
            history.write_text(json.dumps({
                "type": "user",
                "content": [
                    {"type": "text", "text":
                     "<image_files>\n1. /private/injected/path.png\n</image_files>\n\n"
                     "<user_query>\n[Image #1] 检查这张图\n</user_query>"},
                    {"type": "image", "url": "data:image/png;base64,AA=="},
                ],
            }, ensure_ascii=False) + "\n")

            messages, _ = adapters.GrokAdapter().read(str(session))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "[Image #1] 检查这张图")
        self.assertRegex(messages[0]["media"][0]["src"],
                         r"^/api/media/[0-9a-f]{32}$")

    def test_prompt_index_and_tool_free_final_answer_define_a_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            history = session / "chat_history.jsonl"
            rows = [
                {"type": "user", "prompt_index": 9, "content": "开始"},
                {"type": "assistant", "content": "处理中", "tool_calls": [{
                    "id": "c1", "name": "shell", "arguments": {"command": "pwd"},
                }]},
                {"type": "tool_result", "tool_call_id": "c1", "content": "ok"},
                {"type": "assistant", "content": "完成"},
            ]
            history.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            messages, _ = adapters.GrokAdapter().read(str(session))

        self.assertTrue(all(m.get("turn_id") == "prompt:9" for m in messages))
        assistant = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual([(m["text"], m.get("phase")) for m in assistant], [
            ("处理中", "progress"), ("完成", "final"),
        ])

    def test_old_cursor_resets_after_parser_semantics_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            history = session_dir / "chat_history.jsonl"
            history.write_text(json.dumps({
                "type": "assistant",
                "content": [{"type": "text", "text": "正式回复"}],
            }, ensure_ascii=False) + "\n")
            session = {
                "uid": "grok:test", "source": "grok", "sid": "test",
                "path": str(session_dir), "cwd": tmp,
            }
            cursor = session_index.cursor(session)
            old_head = cursor["head"].split(":", 1)[-1]

            result = session_index.messages_for(
                session, start=cursor["end"], head=old_head,
                anchor=cursor["anchor"])

        self.assertTrue(result["reset"])
        self.assertEqual(result["messages"][0]["text"], "正式回复")


class ClaudeProtocolTests(unittest.TestCase):
    def test_user_uuid_and_end_turn_mark_the_final_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "turn-phase.jsonl"
            rows = [
                {"type": "user", "uuid": "user-turn", "parentUuid": None,
                 "isSidechain": False,
                 "message": {"role": "user", "content": "开始"}},
                {"type": "assistant", "uuid": "progress", "parentUuid": "user-turn",
                 "isSidechain": False,
                 "message": {"role": "assistant", "stop_reason": "tool_use",
                             "content": [{"type": "text", "text": "处理中"}]}},
                {"type": "assistant", "uuid": "final", "parentUuid": "progress",
                 "isSidechain": False,
                 "message": {"role": "assistant", "stop_reason": "end_turn",
                             "content": [{"type": "text", "text": "完成"}]}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            messages, _ = adapters.ClaudeAdapter().read(str(transcript))

        visible = [m for m in messages if m["role"] != "status"]
        self.assertTrue(all(m.get("turn_id") == "user-turn" for m in visible))
        assistant = [m for m in visible if m["role"] == "assistant"]
        self.assertEqual([(m["text"], m.get("phase")) for m in assistant], [
            ("处理中", "progress"), ("完成", "final"),
        ])

    def test_current_claude_screen_identifies_rewound_tip_not_jsonl_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "screen-rewind.jsonl"

            def row(kind, uid, parent, text, **extra):
                record = {"type": kind, "uuid": uid, "parentUuid": parent,
                          "isSidechain": False, **extra}
                if kind in {"user", "assistant"}:
                    record["message"] = {"role": kind, "content": text}
                return record

            rows = [
                row("user", "u0", None, "请详细解释这段价值函数的每一项"),
                row("assistant", "a0", "u0",
                    "第一项是即期移动，第二项在策略继续持仓时接上下一时刻价值，第三项再扣除真实交易成本与持有租金。"),
                row("system", "recap", "a0", "", subtype="away_summary",
                    content="我们刚梳理完价值公式与符号方向，下一步准备验证新的实验分支，并对照完整真实回测检查训练目标是否一致可靠。"),
                row("user", "discarded", "recap",
                    "这条输入已经被双 ESC 回退，但追加式文件仍然保留它。"),
            ]
            transcript.write_text("\n".join(
                json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
            screen = """第一项是即期移动，第二项在策略继续持仓时接上下一时刻价值，第三项再扣除真实交易成本与持有租金。
※ recap: 我们刚梳理完价值公式与符号方向，下一步准备验证新的实验分支，并对照完整真实回测检查训练目标是否一致可靠。
────────────────────────
❯
────────────────────────
"""
            adapter = adapters.ClaudeAdapter()

            self.assertEqual(adapter.active_tip(str(transcript)), "discarded")
            self.assertEqual(adapter.match_screen_tip(str(transcript), screen), "recap")
            self.assertIsNone(adapter.match_screen_tip(
                str(transcript), screen.replace("❯", "Select a message to rewind to")))

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

    def test_unanswered_sibling_branch_remains_visible_as_aborted(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "fast-escape.jsonl"

            def row(kind, uid, parent, text):
                return {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "isSidechain": False, "timestamp": "2026-08-27T17:26:23Z",
                    "message": {"role": kind, "content": text},
                }

            rows = [
                row("user", "u0", None, "共同开头"),
                row("assistant", "a0", "u0", "共同回答"),
                row("user", "cancelled", "a0", "快速 Esc 的输入"),
                {"type": "attachment", "uuid": "reminder",
                 "parentUuid": "cancelled", "isSidechain": False,
                 "attachment": {"type": "total_tokens_reminder"}},
                row("user", "replacement", "a0", "之后的新输入"),
                row("assistant", "answer", "replacement", "新回答"),
            ]
            transcript.write_text("\n".join(
                json.dumps(item, ensure_ascii=False) for item in rows) + "\n")

            messages, _ = adapters.ClaudeAdapter().read(str(transcript))

        interrupted = next(item for item in messages
                           if item.get("text") == "快速 Esc 的输入")
        self.assertTrue(interrupted["interrupted"])
        self.assertIn("已中断", interrupted["interrupt_reason"])
        self.assertEqual(
            [(item["role"], item["text"]) for item in messages],
            [("status", "working"), ("user", "共同开头"),
             ("assistant", "共同回答"),
             ("user", "快速 Esc 的输入"), ("status", "aborted"),
             ("status", "working"), ("user", "之后的新输入"),
             ("assistant", "新回答")])

    def test_interrupted_sibling_with_tools_stays_visible(self):
        """Esc 中断后新输入若挂回上一个 turn_duration，中断那一轮仍应显示。

        真实会话里「第一条，明显前后矛盾」已经有 tool/thinking，随后用户打断，
        再发的「原来写需要授权」parentUuid 却指向上一轮 turn_duration。旧逻辑
        把它当成双 Esc 完成枝丢掉，网页少轮、和 tmux 对不上。
        """
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "interrupted-sibling.jsonl"

            def row(kind, uid, parent, text, **extra):
                record = {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "isSidechain": False, "timestamp": "2026-09-12T12:00:00Z",
                    **extra,
                }
                if kind in {"user", "assistant"}:
                    record["message"] = {"role": kind, "content": text}
                return record

            rows = [
                row("user", "u0", None, "共同开头"),
                row("assistant", "a0", "u0", "全部搜了一遍"),
                row("system", "t0", "a0", "", subtype="turn_duration"),
                row("user", "u-work", "t0", "第一条，明显前后矛盾"),
                row("assistant", "a-work", "u-work", "开始核对文档"),
                row("user", "interrupt", "a-work",
                    "[Request interrupted by user]"),
                row("user", "u-replace", "t0", "原来写需要授权"),
                row("assistant", "a-replace", "u-replace", "已改正"),
            ]
            transcript.write_text("\n".join(
                json.dumps(item, ensure_ascii=False) for item in rows) + "\n")
            messages, _ = adapters.ClaudeAdapter().read(str(transcript))

        interrupted = next(item for item in messages
                           if item.get("text") == "第一条，明显前后矛盾")
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(
            [(item["role"], item["text"]) for item in messages],
            [("status", "working"), ("user", "共同开头"),
             ("assistant", "全部搜了一遍"), ("status", "idle"),
             ("user", "第一条，明显前后矛盾"),
             ("assistant", "开始核对文档"), ("status", "aborted"),
             ("status", "working"), ("user", "原来写需要授权"),
             ("assistant", "已改正")])

    def test_continued_in_session_is_resolved_to_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            parent = proj / "sid-parent.jsonl"
            child = proj / "sid-child.jsonl"
            parent.write_text("\n".join((
                json.dumps({"type": "user", "sessionId": "sid-parent",
                            "timestamp": "2026-09-12T10:00:00Z", "cwd": "/repo",
                            "message": {"role": "user", "content": "hello"}}),
                json.dumps({"type": "continued-in", "sessionId": "sid-parent",
                            "continuedInSessionId": "sid-child",
                            "timestamp": "2026-09-12T12:00:00Z"}),
            )) + "\n")
            child.write_text(json.dumps({
                "type": "user", "sessionId": "sid-child",
                "timestamp": "2026-09-12T12:00:01Z", "cwd": "/repo",
                "message": {"role": "user", "content": "continued"},
            }) + "\n")
            adapter = adapters.ClaudeAdapter()
            rows = adapter.finalize_sessions([
                adapter._meta(parent, parent.stat(), "proj"),
                adapter._meta(child, child.stat(), "proj"),
            ])
        parent_row = next(row for row in rows if row["sid"] == "sid-parent")
        child_row = next(row for row in rows if row["sid"] == "sid-child")
        self.assertEqual(parent_row["continued_in"], child_row["uid"])
        self.assertNotIn("continued_in_sid", parent_row)
        self.assertNotIn("continued_in", child_row)

    def test_compaction_keeps_selected_precompact_branch_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "compacted-tree.jsonl"

            def row(kind, uid, parent, text, **extra):
                record = {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "isSidechain": False, "timestamp": "2026-08-18T09:00:00Z",
                    **extra,
                }
                if kind in {"user", "assistant"}:
                    record["message"] = {"role": kind, "content": text}
                elif text:
                    record["content"] = text
                return record

            before = [
                row("user", "u0", None, "共同开头"),
                row("assistant", "a0", "u0", "压缩前保留的回答"),
                row("user", "u-abandoned", "a0", "双 Esc 后放弃的输入"),
                row("assistant", "a-abandoned", "u-abandoned", "放弃分支的回答"),
                {"type": "last-prompt", "leafUuid": "a0"},
            ]
            after = [
                row("system", "compact", None, "Conversation compacted",
                    subtype="compact_boundary"),
                row("user", "summary", "compact", "内部压缩摘要",
                    isCompactSummary=True),
                row("user", "u1", "summary", "压缩后的新问题"),
                row("assistant", "a1", "u1", "压缩后的新回答"),
            ]
            lines = [json.dumps(item, ensure_ascii=False) + "\n"
                     for item in [*before, *after]]
            transcript.write_text("".join(lines))
            boundary_start = len("".join(lines[:len(before)]).encode())
            adapter = adapters.ClaudeAdapter()

            messages, _ = adapter.read(str(transcript))
            extends = adapter.append_extends(
                str(transcript), boundary_start, "a0")

        self.assertEqual(
            [(m["role"], m["text"]) for m in messages
             if m["role"] != "status"],
            [
                ("user", "共同开头"),
                ("assistant", "压缩前保留的回答"),
                ("event", "已压缩"),
                ("user", "压缩后的新问题"),
                ("assistant", "压缩后的新回答"),
            ])
        self.assertTrue(extends)

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

    def test_current_compact_metadata_reconnects_lineage_and_emits_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "current-compact.jsonl"

            def message(kind, uid, parent, text, **extra):
                return {
                    "type": kind, "uuid": uid, "parentUuid": parent,
                    "timestamp": "2026-08-23T04:23:17Z", **extra,
                    "message": {"role": kind, "content": text},
                }

            before = [
                message("user", "u0", None, "压缩前问题"),
                message("assistant", "a0", "u0", "压缩前回答"),
                message("user", "cmd", "a0", "/compact"),
            ]
            after = [
                {"type": "system", "uuid": "boundary", "parentUuid": None,
                 "timestamp": "2026-08-23T04:25:43Z",
                 "compactMetadata": {"trigger": "manual", "durationMs": 1200}},
                message("user", "summary", "boundary", "内部摘要",
                        isCompactSummary=True),
                message("user", "u1", "summary", "压缩后问题"),
                message("assistant", "a1", "u1", "压缩后回答"),
            ]
            lines = [json.dumps(row, ensure_ascii=False) + "\n"
                     for row in [*before, *after]]
            transcript.write_text("".join(lines))
            start = len("".join(lines[:len(before)]).encode())
            adapter = adapters.ClaudeAdapter()

            full, _ = adapter.read(str(transcript))
            incremental, _ = adapter.read(
                str(transcript), start=start, declared_tip="a1")
            extends = adapter.append_extends(str(transcript), start, "cmd")

        self.assertEqual(
            [(m["role"], m["text"]) for m in full if m["role"] != "status"],
            [("user", "压缩前问题"), ("assistant", "压缩前回答"),
             ("event", "已压缩"), ("user", "压缩后问题"),
             ("assistant", "压缩后回答")])
        self.assertEqual(
            [(m["role"], m["text"]) for m in incremental
             if m["role"] != "status"],
            [("event", "已压缩"), ("user", "压缩后问题"),
             ("assistant", "压缩后回答")])
        self.assertIn("idle", [m["state"] for m in full if m["role"] == "status"])
        self.assertTrue(extends)

    def test_escape_fork_preserves_parent_and_inherits_history_prefix(self):
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
                self.assertEqual({s["sid"] for s in sessions}, {parent_id, child_id})
                session = next(s for s in sessions if s["sid"] == child_id)
                parent_messages, _ = adapter.read(parent)
                self.assertIn("被回退的旧尾部", [m["text"] for m in parent_messages])
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

    def test_multi_agent_rollouts_do_not_hide_parent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026" / "08" / "12"
            day.mkdir(parents=True)
            parent_id = "00000000-0000-0000-0000-000000000020"
            parent = day / f"rollout-parent-{parent_id}.jsonl"

            def write(path, payload, title=None):
                rows = [{"type": "session_meta", "payload": payload}]
                if title:
                    rows.append({
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": title}]},
                    })
                path.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                           for row in rows) + "\n")

            write(parent, {
                "id": parent_id, "session_id": parent_id,
                "thread_source": "user", "timestamp": "2026-08-12T02:00:00Z",
                "cwd": "/tmp/project",
            }, "正在运行的主会话")
            agent_ids = []
            for number in range(3):
                agent_id = f"00000000-0000-0000-0000-00000000002{number + 1}"
                agent_ids.append(agent_id)
                write(day / f"rollout-agent-{agent_id}.jsonl", {
                    "id": agent_id, "session_id": parent_id,
                    "forked_from_id": parent_id, "parent_thread_id": parent_id,
                    "thread_source": "subagent", "source": {
                        "subagent": {"thread_spawn": {
                            "parent_thread_id": parent_id, "depth": 1,
                            "agent_path": f"/root/review_{number}",
                        }}},
                    "timestamp": f"2026-08-12T02:00:0{number + 1}Z",
                    "cwd": "/tmp/project",
                })

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", Path(tmp) / "missing-index"):
                adapter = adapters.CodexAdapter()
                raw = adapter.scan_sessions()
                public = adapter.finalize_sessions(raw)

            self.assertEqual(len(raw), 4)
            self.assertEqual({row["sid"] for row in raw if row["_is_subagent"]},
                             set(agent_ids))
            self.assertEqual([row["sid"] for row in public], [parent_id])
            self.assertEqual(public[0]["title"], "正在运行的主会话")
            self.assertEqual(public[0]["agents"], 3)
            self.assertEqual([item["id"] for item in public[0]["agent_items"]], agent_ids)
            self.assertEqual([item["title"] for item in public[0]["agent_items"]],
                             [f"/root/review_{number}" for number in range(3)])
            self.assertEqual(adapter._sid_paths, {parent_id: parent})


    def test_codex_subagent_items_carry_turn_state_and_last_record_time(self):
        """子代理 rollout 自己的 task_started / task_complete / turn_aborted 就是回合边界。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026" / "09" / "12"
            day.mkdir(parents=True)
            parent_id = "00000000-0000-0000-0000-000000000030"

            def event(ts, kind, **payload):
                return {"type": "event_msg", "timestamp": ts,
                        "payload": {"type": kind, "turn_id": "t", **payload}}

            def write(name, agent_id, rows):
                meta = {"type": "session_meta", "timestamp": "2026-09-12T01:00:00Z",
                        "payload": {"id": agent_id, "session_id": parent_id,
                                    "parent_thread_id": parent_id,
                                    "thread_source": "subagent",
                                    "source": {"subagent": {"thread_spawn": {
                                        "parent_thread_id": parent_id, "depth": 1,
                                        "agent_path": f"/root/{name}"}}},
                                    "timestamp": "2026-09-12T01:00:00Z",
                                    "cwd": "/tmp/project"}}
                (day / f"rollout-{name}-{agent_id}.jsonl").write_text(
                    "\n".join(json.dumps(r) for r in [meta, *rows]) + "\n")

            write("parent", parent_id, [])
            (day / f"rollout-parent-{parent_id}.jsonl").write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-09-12T00:59:00Z",
                "payload": {"id": parent_id, "session_id": parent_id, "thread_source": "user",
                            "timestamp": "2026-09-12T00:59:00Z", "cwd": "/tmp/project"}}) + "\n")
            ids = {kind: f"00000000-0000-0000-0000-00000000003{n}"
                   for n, kind in enumerate(["running", "done", "aborted", "resumed"], start=1)}
            write("running", ids["running"], [
                event("2026-09-12T01:01:00Z", "task_started"),
                {"type": "response_item", "timestamp": "2026-09-12T01:02:00Z",
                 "payload": {"type": "reasoning"}},
                {"type": "event_msg", "timestamp": "2026-09-12T01:03:00Z",
                 "payload": {"type": "token_count"}}])
            write("done", ids["done"], [
                event("2026-09-12T01:01:00Z", "task_started"),
                event("2026-09-12T01:10:00Z", "task_complete", completed_at=1789166200)])
            write("aborted", ids["aborted"], [
                event("2026-09-12T01:01:00Z", "task_started"),
                event("2026-09-12T01:05:00Z", "turn_aborted", reason="interrupted")])
            write("resumed", ids["resumed"], [
                event("2026-09-12T01:01:00Z", "task_started"),
                event("2026-09-12T01:05:00Z", "task_complete"),
                event("2026-09-12T01:20:00Z", "task_started"),
                {"type": "response_item", "timestamp": "2026-09-12T01:21:00Z",
                 "payload": {"type": "message"}}])

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", Path(tmp) / "missing-index"):
                adapter = adapters.CodexAdapter()
                public = adapter.finalize_sessions(adapter.scan_sessions())

            parent = next(row for row in public if row["sid"] == parent_id)
            items = {item["title"].removeprefix("/root/"): item for item in parent["agent_items"]}
            self.assertEqual({k: v["active"] for k, v in items.items()}, {
                "running": True, "done": False, "aborted": False, "resumed": True})
            self.assertEqual(items["done"]["updated"], adapters._norm_ts("2026-09-12T01:10:00Z"))
            self.assertEqual(items["running"]["updated"], adapters._norm_ts("2026-09-12T01:03:00Z"))
            self.assertEqual(items["done"]["created"], adapters._norm_ts("2026-09-12T01:00:00Z"))
            self.assertNotIn("_agent_active", parent)
            self.assertFalse(any("active" in row for row in public))


class IncrementalCursorTests(unittest.TestCase):
    def test_confirmed_in_memory_claude_rewind_pins_history_until_new_branch_appends(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_meta, "DATA_DIR", Path(tmp)), \
                patch.object(session_meta, "META_FILE", Path(tmp) / "session-meta.json"):
            path = Path(tmp) / "in-memory-rewind.jsonl"
            sid = "00000000-0000-0000-0000-000000000097"

            def row(kind, uid, parent, text):
                return {"type": kind, "uuid": uid, "parentUuid": parent,
                        "isSidechain": False, "timestamp": "2026-08-11T08:00:00Z",
                        "cwd": tmp, "sessionId": sid,
                        "message": {"role": kind, "content": text}}

            initial = [row("user", "u0", None, "共同开头"),
                       row("assistant", "a0", "u0", "共同回答"),
                       row("user", "discarded", "a0", "已在 TUI 回退的输入")]
            path.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                        for x in initial) + "\n")
            session = {"uid": "claude:pinned", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}
            stale_end = path.stat().st_size
            session_meta.begin_timeline_rewind(session["uid"], "discarded", stale_end)
            session_meta.finish_timeline_rewind(session["uid"], "a0")

            pinned = session_index.messages_for(session)
            self.assertEqual([m["text"] for m in pinned["messages"]],
                             ["共同开头", "共同回答"])
            self.assertTrue(pinned["anchor"].endswith("@a0"), pinned["anchor"])

            with path.open("a") as fh:
                fh.write(json.dumps(row("user", "replacement", "a0", "改写后的输入"),
                                    ensure_ascii=False) + "\n")
            appended = session_index.messages_for(
                session, start=pinned["end"], head=pinned["version"]["head"],
                anchor=pinned["anchor"])
            self.assertFalse(appended["reset"])
            self.assertEqual([m["text"] for m in appended["messages"]], ["改写后的输入"])
            self.assertTrue(appended["anchor"].endswith("@replacement"), appended["anchor"])

            # A browser refresh must keep honoring the explicit double-Esc
            # boundary.  The discarded sibling is not a fast-Esc leaf and
            # must not come back merely because a replacement now exists.
            reloaded = session_index.messages_for(session)
            self.assertEqual([m["text"] for m in reloaded["messages"]],
                             ["共同开头", "共同回答", "改写后的输入"])

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

    def test_initial_window_cache_survives_process_memory_reset(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_index, "WINDOW_CACHE_DIR",
                             Path(tmp) / "window-cache"), \
                patch.object(session_index, "WINDOW_CACHE_MIN_BYTES", 0):
            session_index._clear_window_cache_memory()
            self.addCleanup(session_index._clear_window_cache_memory)
            path = Path(tmp) / "persistent.jsonl"
            sid = "00000000-0000-0000-0000-000000000101"
            rows = [{
                "type": "assistant",
                "message": {"role": "assistant", "content": f"cached {i}"},
                "timestamp": "2026-08-09T10:00:00Z", "cwd": tmp,
                "sessionId": sid,
            } for i in range(3)]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            session = {"uid": "claude:persistent", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}

            first = session_index.messages_for(session, windowed=True)
            cache_files = list(session_index.WINDOW_CACHE_DIR.glob("*.json.gz"))
            self.assertEqual(len(cache_files), 1)
            self.assertEqual(session_index.WINDOW_CACHE_DIR.stat().st_mode & 0o777,
                             0o700)
            self.assertEqual(cache_files[0].stat().st_mode & 0o777, 0o600)

            # 模拟服务重启后的空内存；第二次必须直接读持久化窗口，不能再解析。
            session_index._clear_window_cache_memory()
            adapter = session_index.ADAPTERS["claude"]
            with patch.object(adapter, "read",
                              side_effect=AssertionError("unexpected reparse")):
                second = session_index.messages_for(session, windowed=True)
            self.assertEqual(second["messages"], first["messages"])
            self.assertEqual(second["message_total"], 3)

    def test_initial_window_cache_invalidates_on_internal_insert_delete_and_edit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_index, "WINDOW_CACHE_DIR",
                             Path(tmp) / "window-cache"), \
                patch.object(session_index, "WINDOW_CACHE_MIN_BYTES", 0):
            session_index._clear_window_cache_memory()
            self.addCleanup(session_index._clear_window_cache_memory)
            path = Path(tmp) / "mutable.jsonl"
            sid = "00000000-0000-0000-0000-000000000102"
            session = {"uid": "claude:mutable", "source": "claude", "sid": sid,
                       "path": str(path), "cwd": tmp}

            def write(texts):
                rows = [{
                    "type": "assistant",
                    "message": {"role": "assistant", "content": text},
                    "timestamp": "2026-08-09T10:00:00Z", "cwd": tmp,
                    "sessionId": sid,
                } for text in texts]
                path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")

            def visible():
                return [m["text"] for m in session_index.messages_for(
                    session, windowed=True)["messages"]]

            write(["alpha", "bravo", "charlie"])
            self.assertEqual(visible(), ["alpha", "bravo", "charlie"])

            write(["alpha", "insert", "bravo", "charlie"])
            self.assertEqual(visible(), ["alpha", "insert", "bravo", "charlie"])

            write(["alpha", "insert", "charlie"])
            self.assertEqual(visible(), ["alpha", "insert", "charlie"])

            # insert -> modify 长度相同，专门覆盖“大小没有变化”的内部改写。
            write(["alpha", "change", "charlie"])
            self.assertEqual(visible(), ["alpha", "change", "charlie"])

    def test_initial_window_cache_invalidates_when_codex_parent_prefix_changes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_index, "WINDOW_CACHE_DIR",
                             Path(tmp) / "window-cache"), \
                patch.object(session_index, "WINDOW_CACHE_MIN_BYTES", 0):
            session_index._clear_window_cache_memory()
            self.addCleanup(session_index._clear_window_cache_memory)
            root = Path(tmp) / "sessions"
            day = root / "2026" / "08" / "09"
            day.mkdir(parents=True)
            parent_id = "00000000-0000-0000-0000-000000000103"
            child_id = "00000000-0000-0000-0000-000000000104"
            parent = day / f"rollout-parent-{parent_id}.jsonl"
            child = day / f"rollout-child-{child_id}.jsonl"

            def row(kind, payload):
                return json.dumps({"type": kind, "payload": payload},
                                  ensure_ascii=False).encode() + b"\n"

            parent_rows = [
                row("session_meta", {"id": parent_id, "session_id": parent_id,
                                     "timestamp": "2026-08-09T10:00:00Z",
                                     "cwd": tmp}),
                row("response_item", {"type": "message", "role": "user",
                                      "content": [{"type": "input_text",
                                                   "text": "父项旧文"}]}),
            ]
            parent.write_bytes(b"".join(parent_rows))
            child.write_bytes(b"".join([
                row("session_meta", {
                    "id": child_id, "session_id": child_id,
                    "forked_from_id": parent_id,
                    "history_base": {"thread_id": parent_id,
                                     "end_byte_offset": parent.stat().st_size},
                    "timestamp": "2026-08-09T10:01:00Z", "cwd": tmp}),
                row("response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text",
                                                   "text": "子项正文"}]}),
            ]))
            adapter = adapters.CodexAdapter()
            session = {"uid": "codex:child", "source": "codex", "sid": child_id,
                       "path": str(child), "cwd": tmp,
                       "size": parent.stat().st_size + child.stat().st_size}

            with patch.object(adapters, "CODEX_ROOT", root), \
                    patch.object(adapters, "CODEX_INDEX", Path(tmp) / "missing"), \
                    patch.dict(session_index.ADAPTERS, {"codex": adapter}):
                first = session_index.messages_for(session, windowed=True)
                self.assertEqual([m["text"] for m in first["messages"]],
                                 ["父项旧文", "子项正文"])

                original = parent.read_bytes()
                changed = original.replace("父项旧文".encode(), "父项新文".encode())
                self.assertEqual(len(changed), len(original))
                parent.write_bytes(changed)
                second = session_index.messages_for(session, windowed=True)
                self.assertEqual([m["text"] for m in second["messages"]],
                                 ["父项新文", "子项正文"])


class ClaudeSubagentMetaTests(unittest.TestCase):
    """子代理列表项要带起止时间和"仍在运行"判定。

    transcript 自己分不清收尾：同一版本 CLI 里收尾 text 记录既有 end_turn 也有
    stop_reason=None。父会话在子代理停止后写的 task-notification / 前台
    tool_result 才是停止点，之后只有再追加 user 记录才算被唤起。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "-tmp-project"
        self.parent = self.project / "parent-session.jsonl"
        self.subagents = self.project / "parent-session" / "subagents"
        self.subagents.mkdir(parents=True)
        self.parent_rows = [{
            "type": "user", "uuid": "u0", "parentUuid": None, "isSidechain": False,
            "sessionId": "parent-session", "cwd": "/tmp/project",
            "timestamp": "2026-09-12T00:00:00.000Z",
            "message": {"role": "user", "content": "主任务"},
        }]
        patcher = patch.object(adapters, "_agent_stops", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def dump(rows):
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)

    def agent(self, agent_id, rows, tool_use_id="toolu_" + "x" * 20):
        path = self.subagents / f"agent-{agent_id}.jsonl"
        path.write_text(self.dump([{"isSidechain": True, "agentId": agent_id, **row}
                                   for row in rows]))
        path.with_suffix(".meta.json").write_text(json.dumps({
            "agentType": "general-purpose", "description": f"任务 {agent_id}",
            "toolUseId": tool_use_id,
        }))
        return path

    @staticmethod
    def user(ts, text="继续"):
        return {"type": "user", "timestamp": ts,
                "message": {"role": "user", "content": text}}

    @staticmethod
    def assistant(ts, stop_reason, kind="text"):
        block = ({"type": "text", "text": "结论"} if kind == "text"
                 else {"type": "tool_use", "id": "toolu_call", "name": "Bash", "input": {}})
        return {"type": "assistant", "timestamp": ts,
                "message": {"role": "assistant", "stop_reason": stop_reason,
                            "content": [block]}}

    @staticmethod
    def tool_result(ts):
        return {"type": "user", "timestamp": ts,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_call", "content": "ok"}]}}

    @staticmethod
    def notice(ts, agent_id, status, shape="attachment"):
        text = (f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
                f"<status>{status}</status>\n<summary>Agent finished</summary>\n"
                "</task-notification>")
        if shape == "user":
            return {"type": "user", "timestamp": ts,
                    "message": {"role": "user", "content": text}}
        return {"type": "attachment", "timestamp": ts,
                "attachment": {"type": "queued_command", "commandMode": "task-notification",
                               "prompt": text, "timestamp": ts}}

    @staticmethod
    def agent_result(ts, agent_id, status, tool_use_id):
        return {"type": "user", "timestamp": ts,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": [{"type": "text", "text": "Async agent launched successfully"
                                  if status == "async_launched" else "报告"}]}]},
                "toolUseResult": {"status": status, "agentId": agent_id}}

    def items(self):
        meta = adapters.ClaudeAdapter().session_meta(self.parent)
        return {item["id"]: item for item in meta["agent_items"]}

    def test_agent_items_carry_start_end_and_running_state(self):
        self.agent("done", [self.user("2026-09-12T00:10:00.000Z"),
                            self.assistant("2026-09-12T00:10:05.000Z", "tool_use", "tool"),
                            self.tool_result("2026-09-12T00:10:06.000Z"),
                            self.assistant("2026-09-12T00:12:00.000Z", "end_turn")])
        self.agent("streamed", [self.user("2026-09-12T00:20:00.000Z"),
                                self.assistant("2026-09-12T00:22:00.000Z", None)])
        self.agent("resumed", [self.user("2026-09-12T00:30:00.000Z"),
                               self.assistant("2026-09-12T00:31:00.000Z", None),
                               self.user("2026-09-12T00:40:00.000Z", "再来一次"),
                               self.assistant("2026-09-12T00:41:00.000Z", "tool_use", "tool")])
        self.agent("killed", [self.user("2026-09-12T00:50:00.000Z"),
                              self.assistant("2026-09-12T00:51:00.000Z", "tool_use", "tool")])
        self.agent("fresh", [self.user("2026-09-12T01:00:00.000Z")],
                   tool_use_id="toolu_fresh")
        self.agent("foreground", [self.user("2026-09-12T01:10:00.000Z"),
                                  self.assistant("2026-09-12T01:12:00.000Z", None)],
                   tool_use_id="toolu_fg")
        self.parent.write_text(self.dump(self.parent_rows + [
            self.notice("2026-09-12T00:12:00.100Z", "done", "completed"),
            self.notice("2026-09-12T00:22:00.100Z", "streamed", "completed", shape="user"),
            self.notice("2026-09-12T00:31:00.100Z", "resumed", "failed"),
            self.notice("2026-09-12T00:52:00.000Z", "killed", "killed"),
            self.agent_result("2026-09-12T01:00:00.100Z", "fresh", "async_launched", "toolu_fresh"),
            self.agent_result("2026-09-12T01:12:00.200Z", "foreground", "completed", "toolu_fg"),
        ]))

        items = self.items()

        self.assertEqual({k: v["active"] for k, v in items.items()}, {
            "done": False, "streamed": False, "resumed": True,
            "killed": False, "fresh": True, "foreground": False,
        })
        self.assertEqual(items["done"]["created"],
                         adapters._norm_ts("2026-09-12T00:10:00.000Z"))
        self.assertEqual(items["done"]["updated"],
                         adapters._norm_ts("2026-09-12T00:12:00.000Z"))
        self.assertEqual(items["resumed"]["created"],
                         adapters._norm_ts("2026-09-12T00:30:00.000Z"))
        self.assertEqual(items["resumed"]["updated"],
                         adapters._norm_ts("2026-09-12T00:41:00.000Z"))
        self.assertEqual(items["fresh"]["title"], "任务 fresh")

    def test_late_copies_of_a_notice_and_refusal_do_not_flip_a_running_agent(self):
        """父会话会把同一条通知的文本在 dequeue/re-enqueue/吸收成 user 时再写几遍，
        时间可晚十几分钟；refusal 后 CLI 自动重试。两者都不能把已被唤起的子代理翻成已停。"""
        self.agent("worker", [self.user("2026-09-12T00:10:00.000Z"),
                              self.assistant("2026-09-12T00:19:00.600Z", "end_turn"),
                              self.user("2026-09-12T00:19:00.700Z", "追加一条任务"),
                              self.assistant("2026-09-12T00:19:30.000Z", "refusal"),
                              self.assistant("2026-09-12T00:20:59.000Z", "tool_use", "tool")])
        notice = self.notice("2026-09-12T00:19:00.650Z", "worker", "completed")
        text = notice["attachment"]["prompt"]
        late_copy = {"type": "queue-operation", "operation": "remove",
                     "timestamp": "2026-09-12T00:21:00.400Z", "content": text}
        re_enqueue = {"type": "queue-operation", "operation": "enqueue",
                      "timestamp": "2026-09-12T00:33:00.000Z", "content": text}
        absorbed = self.notice("2026-09-12T00:35:00.000Z", "worker", "completed", shape="user")
        self.parent.write_text(self.dump(self.parent_rows + [
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": "2026-09-12T00:19:00.650Z", "content": text},
            late_copy, notice, re_enqueue, absorbed]))

        self.assertTrue(self.items()["worker"]["active"])

        # 换一段新文本的通知（第二次真正停止）才算停
        with self.parent.open("a") as fh:
            fh.write(json.dumps(self.notice("2026-09-12T00:40:00.000Z", "worker", "failed")) + "\n")
        self.assertFalse(self.items()["worker"]["active"])

    def test_stop_notices_are_read_incrementally_and_only_from_complete_lines(self):
        self.agent("worker", [self.user("2026-09-12T01:00:00.000Z"),
                              self.assistant("2026-09-12T01:05:00.000Z", None)])
        self.parent.write_text(self.dump(self.parent_rows))
        self.assertTrue(self.items()["worker"]["active"])

        line = json.dumps(self.notice("2026-09-12T01:05:00.100Z", "worker", "completed"))
        with self.parent.open("a") as fh:
            fh.write(line[:len(line) // 2])
        # 半行还没写完：既不能算停止，也不能把它当作已扫过
        self.assertTrue(self.items()["worker"]["active"])
        with self.parent.open("a") as fh:
            fh.write(line[len(line) // 2:] + "\n")
        self.assertFalse(self.items()["worker"]["active"])

        # 通知之后再追加 user 记录就是被 SendMessage 唤起
        with (self.subagents / "agent-worker.jsonl").open("a") as fh:
            fh.write(json.dumps({"isSidechain": True, "agentId": "worker",
                                 **self.user("2026-09-12T01:20:00.000Z", "继续")}) + "\n")
        self.assertTrue(self.items()["worker"]["active"])

        # 文件被重写变短时从头再扫，不沿用旧偏移
        self.parent.write_text(self.dump(self.parent_rows))
        self.assertTrue(self.items()["worker"]["active"])
        self.parent.write_text(self.dump(self.parent_rows + [
            self.notice("2026-09-12T01:21:00.000Z", "worker", "killed")]))
        self.assertFalse(self.items()["worker"]["active"])


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

    def test_claude_human_queued_command_is_user_but_task_notification_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "queue-operation", "operation": "enqueue",
                 "timestamp": "2026-08-18T04:27:00Z", "content": "排队的人类输入"},
                {"type": "queue-operation", "operation": "remove",
                 "timestamp": "2026-08-18T04:27:01Z", "content": "排队的人类输入"},
                {"type": "attachment", "timestamp": "2026-08-18T04:27:00Z",
                 "attachment": {"type": "queued_command", "commandMode": "prompt",
                                "origin": {"kind": "human"},
                                "prompt": "排队的人类输入"}},
                {"type": "attachment", "timestamp": "2026-08-18T04:27:02Z",
                 "attachment": {"type": "queued_command",
                                "commandMode": "task-notification",
                                "prompt": "后台任务完成通知"}},
            ]
            f.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                     for x in rows) + "\n")
            msgs, _ = adapters.ClaudeAdapter().read(str(f))

        self.assertEqual([m["text"] for m in msgs if m["role"] == "user"],
                         ["排队的人类输入"])
        self.assertNotIn("后台任务完成通知", [m["text"] for m in msgs])

    def test_claude_custom_title_is_a_visible_rename_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            f.write_text(json.dumps({
                "type": "custom-title", "customTitle": "新标题",
                "sessionId": "session-1",
            }, ensure_ascii=False) + "\n")

            msgs, _ = adapters.ClaudeAdapter().read(str(f))

        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "command")
        self.assertEqual(msgs[0]["text"], "/rename 新标题")
        self.assertFalse(msgs[0].get("silent", False))
        self.assertFalse(msgs[0]["counted"])
        self.assertTrue(msgs[0]["event_id"].startswith("rename:session-1:"))

    def test_claude_compact_title_replay_does_not_duplicate_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "custom-title", "customTitle": "同一标题",
                 "sessionId": "session-1"},
                {"type": "user", "timestamp": "2026-08-25T10:00:00Z",
                 "message": {"role": "user", "content": "/compact"}},
                {"type": "custom-title", "customTitle": "同一标题",
                 "sessionId": "session-1"},
                {"type": "system", "subtype": "compact_boundary",
                 "uuid": "compact-1", "timestamp": "2026-08-25T10:00:01Z",
                 "content": "Conversation compacted"},
            ]
            lines = [json.dumps(row, ensure_ascii=False) + "\n" for row in rows]
            f.write_text("".join(lines))
            duplicate_start = len("".join(lines[:2]).encode())
            adapter = adapters.ClaudeAdapter()

            full, _ = adapter.read(str(f))
            incremental, _ = adapter.read(str(f), start=duplicate_start)

        self.assertEqual([m["text"] for m in full if m["role"] == "command"],
                         ["/rename 同一标题"])
        self.assertEqual([m["text"] for m in incremental
                          if m["role"] == "command"], [])
        self.assertEqual([m["text"] for m in incremental
                          if m["role"] == "event"], ["已压缩"])

    def test_claude_system_local_command_is_semantic_not_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "system", "subtype": "local_command",
                 "uuid": "help-1", "timestamp": "2026-08-25T10:00:00Z",
                 "content": "<command-name>/help</command-name>\n"
                            "<command-message>help</command-message>\n"
                            "<command-args></command-args>"},
                {"type": "system", "subtype": "local_command",
                 "uuid": "help-2", "timestamp": "2026-08-25T10:00:01Z",
                 "content": "<local-command-stdout>Help dialog dismissed"
                            "</local-command-stdout>"},
                {"type": "system", "subtype": "local_command",
                 "uuid": "rename-1", "timestamp": "2026-08-25T10:00:02Z",
                 "content": "<command-name>/rename</command-name>\n"
                            "<command-args>新标题</command-args>"},
            ]
            f.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                    for row in rows) + "\n")

            messages, _ = adapters.ClaudeAdapter().read(str(f))

        visible = [(m["role"], m["text"]) for m in messages
                   if m["role"] != "status"]
        self.assertEqual(visible, [("command", "/help")])
        self.assertFalse(any("<command-" in text or "<local-command" in text
                             for _role, text in visible))

    def test_claude_bash_protocol_is_semantic_command_and_terminal_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.jsonl"
            rows = [
                {"type": "user", "uuid": "shell-input",
                 "parentUuid": "root", "timestamp": "2026-08-28T12:00:00Z",
                 "message": {"role": "user",
                             "content": "<bash-input> printf '&lt;ok&gt;'"
                                        "</bash-input>"}},
                {"type": "user", "uuid": "shell-output",
                 "parentUuid": "shell-input",
                 "timestamp": "2026-08-28T12:00:01Z",
                 "message": {"role": "user",
                             "content": "<bash-stdout>&lt;ok&gt;\n</bash-stdout>"
                                        "<bash-stderr>warning</bash-stderr>"}},
                {"type": "assistant", "uuid": "answer",
                 "parentUuid": "shell-output",
                 "timestamp": "2026-08-28T12:00:02Z",
                 "message": {"role": "assistant", "content": "完成"}},
            ]
            f.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                    for row in rows) + "\n")

            messages, _ = adapters.ClaudeAdapter().read(str(f))

        visible = [m for m in messages if m["role"] != "status"]
        self.assertEqual(
            [(m["role"], m["text"]) for m in visible],
            [("command", "! printf '<ok>'"),
             ("tool_result", "<ok>\n\nstderr:\nwarning"),
             ("assistant", "完成")])
        self.assertEqual(visible[0]["call_id"], "local-shell:shell-input")
        self.assertEqual(visible[1]["call_id"], "local-shell:shell-input")
        self.assertFalse(visible[1]["counted"])
        self.assertTrue(visible[1]["has_stderr"])
        self.assertEqual([m["state"] for m in messages if m["role"] == "status"],
                         ["working"])
        self.assertFalse(any("<bash-" in m["text"] for m in visible))

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
