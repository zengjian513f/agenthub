import unittest

from monkey_model import (
    ConsistencyOracle,
    CoverageScheduler,
    LayerObservation,
    MonkeySessionState,
    PendingView,
    classify_terminal,
    simulate_oracle_regressions,
    simulate_schedule,
)


UID = "codex:test"
TEXT = "请处理 MONKEY-REQ\n"


def observation(at, *, users=(), assistants=(), pending=(), outbox=(),
                phase="busy", activity="working", history="MONKEY-REQ",
                page="desktop"):
    return LayerObservation(
        at, page, True, UID, phase, history, "", tuple(users), tuple(assistants),
        tuple(pending), activity, tuple(outbox))


class MonkeyModelTests(unittest.TestCase):
    def oracle(self):
        value = ConsistencyOracle(UID, "codex")
        value.expect("logical", TEXT, "MONKEY-REQ", "MONKEY-RSP",
                     "browser", 0, server_id="request-1")
        return value

    def test_scheduler_is_seeded_and_covers_every_core_action_for_both_clis(self):
        first = simulate_schedule(4815, steps=900, max_paid_turns=80)
        second = simulate_schedule(4815, steps=900, max_paid_turns=80)
        self.assertEqual(first["trace"], second["trace"])
        self.assertEqual(first["scheduler"]["missing_core"],
                         {"claude": [], "codex": []})
        self.assertTrue(any(row["action"] == "reload_while_busy"
                            for row in first["trace"]))

    def test_scheduler_obeys_paid_turn_budget(self):
        scheduler = CoverageScheduler(9, max_paid_turns=1)
        sessions = [MonkeySessionState("claude", 1)]
        for _ in range(40):
            picked = scheduler.choose(sessions)
            scheduler.record(picked, sessions[0])
        self.assertLessEqual(sessions[0].paid_turns, 1)

    def test_free_regression_matrix_detects_known_failures_without_false_alarm(self):
        result = simulate_oracle_regressions()
        self.assertEqual(result["missing"], {})
        self.assertEqual(result["normal_issues"], [])

    def test_independent_terminal_classifier_handles_busy_draft_and_question(self):
        rule = "─" * 40
        busy = f"old\n{rule}\n❯\u00a0\n{rule}\n✢ Unfurling… (2s · ↓ 80 tokens)"
        self.assertEqual(classify_terminal("claude", busy, cursor=(2, 2)).phase,
                         "busy")
        draft = f"{rule}\n❯\u00a0尚未发送\n{rule}\nplan mode on"
        self.assertEqual(classify_terminal("claude", draft, cursor=(8, 1)).phase,
                         "editing")
        narrow = f" renamed-title ─\n❯\u00a0\n{rule}\nmanual mode on"
        self.assertEqual(classify_terminal(
            "claude", narrow, cursor=(2, 1)).phase, "idle")
        question = ("Would you like to run this command?\n"
                    "  1. Yes (y)\n  2. No (esc)\n"
                    "Press enter to confirm or esc to cancel")
        self.assertEqual(classify_terminal("codex", question).phase, "question")

    def test_normal_pending_to_formal_to_reply_converges(self):
        oracle = self.oracle()
        self.assertEqual(oracle.observe(observation(
            0.2, pending=(PendingView(TEXT, server_id="request-1"),),
            outbox=(PendingView(TEXT, server_id="request-1"),))), [])
        self.assertEqual(oracle.observe(observation(
            1.0, users=(TEXT,), pending=(), outbox=())), [])
        self.assertEqual(oracle.observe(observation(
            2.0, users=(TEXT,), assistants=("MONKEY-RSP",), pending=(),
            outbox=(), phase="idle", activity="idle",
            history="MONKEY-REQ\nMONKEY-RSP")), [])

    def test_message_that_vanishes_before_formal_record_is_reported(self):
        oracle = self.oracle()
        oracle.observe(observation(
            0.2, pending=(PendingView(TEXT, server_id="request-1"),),
            outbox=(PendingView(TEXT, server_id="request-1"),)))
        self.assertEqual(oracle.observe(observation(1.0, history="")), [])
        issues = oracle.observe(observation(1.6, history=""))
        self.assertIn("browser_message_disappeared", [row.code for row in issues])

    def test_formal_message_and_stale_pending_are_reported(self):
        oracle = self.oracle()
        pending = (PendingView(TEXT, server_id="request-1"),)
        oracle.observe(observation(1.0, users=(TEXT,), pending=pending))
        issues = oracle.observe(observation(2.6, users=(TEXT,), pending=pending))
        codes = {row.code for row in issues}
        self.assertIn("formal_pending_overlap", codes)
        self.assertIn("retired_outbox_still_pending", codes)

    def test_terminal_idle_with_stale_working_is_reported(self):
        oracle = self.oracle()
        oracle.observe(observation(
            1.0, users=(TEXT,), phase="idle", activity="working"))
        issues = oracle.observe(observation(
            4.1, users=(TEXT,), phase="idle", activity="working"))
        self.assertIn("stale_working", [row.code for row in issues])

    def test_idle_terminal_cannot_leave_server_queue_stuck_forever(self):
        oracle = self.oracle()
        queued = PendingView(TEXT, state="queued", server_id="request-1")
        oracle.observe(observation(
            1.0, pending=(queued,), outbox=(queued,), phase="idle",
            activity="idle", history=""))
        issues = oracle.observe(observation(
            11.1, pending=(queued,), outbox=(queued,), phase="idle",
            activity="idle", history=""))
        self.assertIn("idle_outbox_stuck", [row.code for row in issues])


if __name__ == "__main__":
    unittest.main()
