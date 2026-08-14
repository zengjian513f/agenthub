import unittest

from sesman import codex_bridge


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

    def test_wrapped_ready_footer_does_not_become_editor_text(self):
        screen = ("\x1b[1m›\x1b[0m \x1b[2mImplement {feature}\x1b[0m\n\n"
                  "  gpt-5.6-sol max · weekly 87% left\n"
                  "  Context 19% used · Ready · Full Access")
        self.assertEqual(codex_bridge.composer_state(screen), "empty")


if __name__ == "__main__":
    unittest.main()
