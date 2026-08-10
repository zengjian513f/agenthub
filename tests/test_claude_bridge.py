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


if __name__ == "__main__":
    unittest.main()
