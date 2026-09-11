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

    def test_current_working_screen_still_exposes_writable_composer(self):
        screen = ("\x1b[1m• Working\x1b[0m \x1b[2m(12s • esc to interrupt)\x1b[0m\n\n"
                  "\x1b[1m›\x1b[0m \x1b[2mRun /review on my current changes\x1b[0m\n\n"
                  "  gpt-5.6-sol xhigh fast · ~/Projects/agenthub · Working")
        self.assertEqual(codex_bridge.composer_state(screen), "empty")

        draft = screen.replace(
            "\x1b[1m›\x1b[0m \x1b[2mRun /review on my current changes\x1b[0m",
            "\x1b[1m›\x1b[0m 下一条消息",
        )
        self.assertEqual(codex_bridge.composer_state(draft), "editing")

    def test_particle_field_around_the_composer_is_not_input(self):
        """Codex 0.154 scatters RGB-coloured braille particles over the composer.

        BUG-20260912-014830-879a23: the particle rows made the block above the
        footer start without a marker, so ``draft-status`` answered ``unknown``
        for a real draft; the web text was then pasted into the middle of that
        draft and submitted as one merged prompt.  Particles must count neither
        as a draft block nor as draft text.
        """
        def particle(glyph, shade):
            return f"\x1b[38;2;{shade};{shade};{shade}m{glyph}\x1b[39m"

        above = ("\x1b[38;2;97;102;107;48;2;30;30;30m⠁\x1b[39m      "
                 + particle("⠄", 95) + "     " + particle("⢀", 70))
        below = ("\x1b[48;2;30;30;30m      " + particle("⠠", 54) + " "
                 + particle("⠐", 84) + "        " + particle("⠄", 70))
        working = ("\x1b[38;2;143;150;160;1m•\x1b[m Working "
                   "\x1b[2m(12m 06s • esc to interrupt)")
        footers = {
            "ready": ("  \x1b[38;2;246;226;183mgpt-6-astra xhigh\x1b[39;2m · "
                      "\x1b[38;2;171;223;167;22m~/Projects/agenthub\x1b[39;2m · "
                      "\x1b[38;2;242;181;144;22mContext 77% used\x1b[39;2m · "
                      "Ready · Full Access\x1b[m"),
            "working": ("  \x1b[38;2;246;226;183mgpt-6-astra high\x1b[39;2m · "
                        "\x1b[38;2;171;223;167;22m~/Projects/agenthub\x1b[39;2m · "
                        "\x1b[38;2;200;169;238;22mWorking\x1b[39;2m · Main [default]"
                        "\x1b[m"),
        }
        # A particle may sit in the cell between the marker and the placeholder.
        empty_row = ("\x1b[48;2;30;30;30;1m›\x1b[22m" + particle("⠁", 48)
                     + "\x1b[2mAsk Codex to do anything\x1b[22m   " + particle("⠈", 54))
        draft_row = ("\x1b[48;2;30;30;30;1m›\x1b[22m 我想agenthub后端并列建一个agenthub-rs "
                     + particle("⠁", 60) + " " + particle("⠁", 40))

        def frame(row, footer=None):
            lines = [working, "", above, row, below]
            if footer:
                lines.append(footers[footer])
            return "\n".join(lines)

        for footer in footers:
            with self.subTest(footer=footer):
                self.assertEqual(
                    codex_bridge.composer_state(frame(empty_row, footer)), "empty")
                self.assertEqual(
                    codex_bridge.composer_state(frame(draft_row, footer)), "editing")
        # Short panes hide the footer; the cursor after "我" is the same spot
        # the merged paste landed in.
        self.assertEqual(
            codex_bridge.composer_state(frame(draft_row), (4, 3)), "editing")
        self.assertEqual(
            codex_bridge.composer_state(frame(empty_row), (2, 3)), "empty")

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
