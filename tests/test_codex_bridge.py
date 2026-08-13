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


if __name__ == "__main__":
    unittest.main()
