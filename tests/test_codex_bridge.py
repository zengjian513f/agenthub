import unittest

from agenthub import codex_bridge


SCREEN = """old output

  Would you like to run the following command?

  Environment: local

  $ /usr/bin/rm -f /tmp/qnd-spread-cookie.jar

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `/usr/bin/rm -f
     /tmp/qnd-spread-cookie.jar` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""


class CodexBridgeTests(unittest.TestCase):
    def test_parses_live_command_approval(self):
        prompt = codex_bridge.approval_prompt(SCREEN)
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["kind"], "approval")
        self.assertEqual(prompt["state"], "waiting")
        question = prompt["questions"][0]
        self.assertEqual(question["header"], "命令审批 · local")
        self.assertIn("/usr/bin/rm -f /tmp/qnd-spread-cookie.jar", question["question"])
        self.assertEqual([item["key"] for item in question["options"]],
                         ["y", "p", "Escape"])

    def test_id_is_stable(self):
        self.assertEqual(codex_bridge.approval_prompt(SCREEN)["id"],
                         codex_bridge.approval_prompt(SCREEN)["id"])

    def test_rejects_transcript_without_live_footer(self):
        self.assertIsNone(codex_bridge.approval_prompt(
            SCREEN + "\ncommand continued after quoted prompt\n"))
        self.assertIsNone(codex_bridge.approval_prompt(
            "Would you like to run the following command?\n$ echo fake\n"))

    def test_empty_composer_is_distinguished_from_restored_text(self):
        footer = "  gpt-5.6-sol · Context 19% used · /home/user · Ready"
        empty = "\x1b[1m›\x1b[0m \x1b[2mImplement {feature}\x1b[0m\n\n" + footer
        restored = "\x1b[1;2m› \x1b[0m上一条尚未清空的输入\n\n" + footer
        self.assertEqual(codex_bridge.composer_state(empty), "empty")
        self.assertEqual(codex_bridge.composer_state(restored), "editing")

    def test_composer_requires_live_ready_footer(self):
        transcript = "输出里引用了 › Implement {feature}\n仍在工作"
        self.assertEqual(codex_bridge.composer_state(transcript), "unknown")

    def test_short_live_screen_uses_cursor_when_codex_hides_footer(self):
        lines = [
            "─" * 46,
            "",
            "• 因为窗口太短，底部状态行不会显示。",
            "",
            "  已完成。",
            "",
            "─ Worked for 1m 01s " + "─" * 24,
            "",
            "",
            "\x1b[1m\x1b[38;5;215m›\x1b[0m "
            "\x1b[2mAsk Codex to do anything\x1b[0m",
        ]
        screen = "\n".join(lines)
        cursor = (2, len(lines) - 1)

        self.assertEqual(codex_bridge.composer_state(screen), "unknown")
        self.assertEqual(codex_bridge.composer_state(screen, cursor), "empty")

        lines[-1] = "\x1b[1m\x1b[38;5;215m›\x1b[0m 真实草稿"
        draft = "\n".join(lines)
        self.assertEqual(
            codex_bridge.composer_state(draft, (6, len(lines) - 1)), "editing")

    def test_bottom_composer_accepts_codex_parked_cursor(self):
        # BUG-20260903-063954-2e6cc2: Codex 0.150.1 drew the live composer on
        # row 31 of a 32-row pane while tmux reported its cursor at (2, 29).
        lines = ["old output", *([""] * 28), "", "",
                 "\x1b[1m\x1b[38;5;215m›\x1b[0m "
                 "\x1b[2mAsk Codex to do anything\x1b[0m"]
        screen = "\n".join(lines)

        self.assertEqual(len(lines), 32)
        self.assertEqual(codex_bridge.composer_state(screen, (2, 29)), "empty")

        lines[-1] = "\x1b[1m\x1b[38;5;215m›\x1b[0m 真实草稿"
        self.assertEqual(
            codex_bridge.composer_state("\n".join(lines), (2, 29)), "editing")

    def test_parked_cursor_only_trusts_the_physical_bottom_composer(self):
        prompt = ("\x1b[1m\x1b[38;5;215m›\x1b[0m "
                  "\x1b[2mAsk Codex to do anything\x1b[0m")
        not_bottom = "\n".join(["old output", "", prompt, "", "later output"])
        too_far = "\n".join(["old output", "", "", "", prompt])

        self.assertEqual(
            codex_bridge.composer_state(not_bottom, (2, 1)), "unknown")
        self.assertEqual(
            codex_bridge.composer_state(too_far, (2, 1)), "unknown")

    def test_cursor_elsewhere_does_not_authorize_footerless_history(self):
        screen = ("\x1b[1m›\x1b[0m "
                  "\x1b[2mAsk Codex to do anything\x1b[0m\n\n"
                  "• 当前只是历史输出")
        self.assertEqual(
            codex_bridge.composer_state(screen, (0, 2)), "unknown")

    def test_wrapped_ready_footer_does_not_become_editor_text(self):
        screen = ("\x1b[1m›\x1b[0m \x1b[2mImplement {feature}\x1b[0m\n\n"
                  "  gpt-5.6-sol max · weekly 87% left\n"
                  "  Context 19% used · Ready · Full Access")
        self.assertEqual(codex_bridge.composer_state(screen), "empty")

    def test_current_model_footer_marks_live_composer(self):
        footer = "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub"
        empty = "\x1b[1m›\x1b[0m \x1b[2mUse /skills to list available skills\x1b[0m\n\n" + footer
        restored = "\x1b[1;2m› \x1b[0m尚未提交的草稿\n\n" + footer
        self.assertEqual(codex_bridge.composer_state(empty), "empty")
        self.assertEqual(codex_bridge.composer_state(restored), "editing")

    def test_current_double_angle_marker_marks_empty_composer(self):
        screen = ("\x1b[0;1m\x1b[38;5;141m»\x1b[0m "
                  "\x1b[2mExplain this codebase\x1b[0m\n\n"
                  "  gpt-5.6-sol ultra · /node-a-share/T0Project…")
        self.assertEqual(codex_bridge.composer_state(screen), "empty")

    def test_current_working_screen_is_not_treated_as_ready(self):
        screen = ("\x1b[1m• Working\x1b[0m \x1b[2m(12s • esc to interrupt)\x1b[0m\n\n"
                  "\x1b[1m›\x1b[0m \x1b[2mRun /review on my current changes\x1b[0m\n\n"
                  "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub")
        self.assertEqual(codex_bridge.composer_state(screen), "unknown")

    def test_resize_frame_does_not_treat_historic_prompt_as_composer(self):
        screen = ("\x1b[1m›\x1b[0m 历史用户消息\n\n"
                  "• 正在重绘，当前输入框尚未出现\n\n"
                  "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub")
        self.assertEqual(codex_bridge.composer_state(screen), "unknown")

    def test_rgb_colour_selector_is_not_mistaken_for_dim(self):
        footer = "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub"
        restored = ("\x1b[1m›\x1b[0m \x1b[38;2;2;120;200m彩色草稿\x1b[0m\n\n"
                    + footer)
        self.assertEqual(codex_bridge.composer_state(restored), "editing")

    def test_wrapped_draft_is_one_adjacent_composer_block(self):
        footer = "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub"
        restored = ("\x1b[1m›\x1b[0m 草稿第一行\n"
                    "  草稿第二行\n\n" + footer)
        self.assertEqual(codex_bridge.composer_state(restored), "editing")

    def test_interrupt_rewind_hint_is_a_live_empty_composer(self):
        prefix = (
            "\x1b[38;5;1m■ Failed to branch before the selected prompt:\x1b[0m\n"
            "\x1b[1;2m› \x1b[0m历史用户消息\n\n"
            "\x1b[38;5;1m■ Conversation interrupted - tell the model\x1b[0m\n\n")
        footer = "\x1b[2m  esc again to edit previous message\x1b[0m"
        empty = (prefix + "\x1b[1m\x1b[38;5;215m›\x1b[0m "
                 "\x1b[2mUse /skills to list available skills\x1b[0m\n\n" + footer)
        draft = (prefix + "\x1b[1m\x1b[38;5;215m›\x1b[0m 新消息草稿\n\n"
                 + footer)
        unstyled_quote = (prefix + "\x1b[1m›\x1b[0m "
                          "\x1b[2mUse /skills to list available skills\x1b[0m\n\n"
                          "  esc again to edit previous message")

        self.assertEqual(codex_bridge.composer_state(empty), "empty")
        self.assertEqual(codex_bridge.composer_state(draft), "editing")
        self.assertEqual(codex_bridge.composer_state(unstyled_quote), "unknown")


if __name__ == "__main__":
    unittest.main()
