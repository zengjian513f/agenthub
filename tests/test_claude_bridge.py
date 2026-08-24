import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sesman import claude_bridge


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.prompt_patch = patch.object(claude_bridge, "PROMPT_DIR", root / "prompts")
        self.settings_patch = patch.object(
            claude_bridge, "SETTINGS_FILE", root / "bridge-settings.json")
        self.prompt_patch.start()
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.prompt_patch.stop()
        self.tmp.cleanup()

    def test_question_hook_records_structure_and_matching_result_settles_it(self):
        sid, tool_id = "session-123", "toolu-question"
        claude_bridge.handle({
            "hook_event_name": "PreToolUse", "session_id": sid,
            "tool_name": "AskUserQuestion", "tool_use_id": tool_id,
            "tool_input": {"questions": [{
                "header": "颜色", "question": "选哪个？", "multiSelect": False,
                "options": [{"label": "红", "description": "暖色"}, "蓝"],
            }]},
        })
        prompt = claude_bridge.prompt(sid)
        self.assertEqual(prompt["id"], tool_id)
        self.assertEqual(prompt["state"], "waiting")
        self.assertEqual(prompt["questions"], [{
            "header": "颜色", "question": "选哪个？", "multiple": False,
            "options": [{"label": "红", "description": "暖色"},
                        {"label": "蓝", "description": ""}],
        }])
        self.assertIsNotNone(claude_bridge.revision(sid))

        claude_bridge.handle({"hook_event_name": "PostToolUse",
                              "session_id": sid, "tool_use_id": "other"})
        self.assertIsNotNone(claude_bridge.prompt(sid))
        claude_bridge.handle({"hook_event_name": "PostToolUseFailure",
                              "session_id": sid, "tool_use_id": tool_id})
        self.assertEqual(claude_bridge.prompt(sid)["state"], "cancelled")
        self.assertTrue(claude_bridge.clear(sid, tool_id))
        self.assertIsNone(claude_bridge.prompt(sid))

        claude_bridge.handle({
            "hook_event_name": "PreToolUse", "session_id": sid,
            "tool_name": "AskUserQuestion", "tool_use_id": tool_id,
            "tool_input": {"questions": [{"question": "继续吗？"}]},
        })
        claude_bridge.handle({"hook_event_name": "PostToolUse",
                              "session_id": sid, "tool_use_id": tool_id})
        self.assertEqual(claude_bridge.prompt(sid)["state"], "submitted")

    def test_session_start_clears_stale_prompt_and_settings_are_passive(self):
        sid = "session-456"
        claude_bridge.handle({
            "hook_event_name": "PreToolUse", "session_id": sid,
            "tool_name": "AskUserQuestion", "tool_use_id": "ask",
            "tool_input": {"questions": [{"question": "还在吗？"}]},
        })
        claude_bridge.handle({"hook_event_name": "SessionStart", "session_id": sid})
        self.assertIsNone(claude_bridge.prompt(sid))

        path = Path(claude_bridge.settings_path())
        settings = json.loads(path.read_text())
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"],
                         "AskUserQuestion")
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(command["type"], "command")
        self.assertNotIn("permissionDecision", json.dumps(settings))

    def test_composer_distinguishes_empty_suggestion_and_restored_draft(self):
        rule = "─" * 80
        footer = "  ⏵⏵ bypass permissions on · ← 4 agents"
        empty = f"old output\n{rule}\n\x1b[39m❯\xa0\n{rule}\n{footer}"
        suggestion = (f"old output\n{rule}\n\x1b[39m❯\xa0"
                      f"\x1b[2mTry fixing the tests\x1b[0m\n{rule}\n{footer}")
        restored = (f"old output\n{rule}\n\x1b[39m❯\xa0"
                    f"被 ESC 回填的消息\n{rule}\n{footer}")

        self.assertEqual(claude_bridge.composer_state(empty, (2, 2)), "empty")
        self.assertEqual(claude_bridge.composer_state(suggestion, (2, 2)), "empty")
        self.assertEqual(claude_bridge.composer_state(restored, (19, 2)), "editing")
        # 即使用户把光标移回开头，正常亮度的正文仍然是草稿。
        self.assertEqual(claude_bridge.composer_state(restored, (2, 2)), "editing")

    def test_composer_handles_wrapping_but_rejects_choice_pointer(self):
        rule = "─" * 50
        wrapped = (f"{rule}\n\x1b[39m❯\xa0第一行很长\n"
                   f"第二行草稿\n{rule}\n  ⏵⏵ auto mode on")
        choice = ("  Resume this session?\n\n"
                  "  \x1b[38;5;153m❯\x1b[39m 1. Resume from summary\n"
                  "    2. Resume full session\n\n"
                  "  Enter to confirm · Esc to cancel")

        self.assertEqual(claude_bridge.composer_state(wrapped, (12, 2)), "editing")
        self.assertEqual(claude_bridge.composer_state(choice, (2, 2)), "unknown")

    def test_colourless_placeholder_uses_real_cursor_position(self):
        rule = "─" * 50
        screen = f"{rule}\n❯\xa0Try fixing tests\n{rule}\n  manual mode on"
        self.assertEqual(claude_bridge.composer_state(screen, (2, 1)), "empty")
        self.assertEqual(claude_bridge.composer_state(screen, (18, 1)), "editing")


if __name__ == "__main__":
    unittest.main()
