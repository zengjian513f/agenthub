import unittest

from agenthub import term_submit


def _pasting():
    return "Pasting…", (0, 0)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class WaitPasteConsumedTests(unittest.TestCase):
    def _run(self, frames, before, text="hello"):
        clock = FakeClock()
        frames = list(frames)

        def snapshot():
            return frames[0] if len(frames) == 1 else frames.pop(0)

        term_submit.wait_paste_consumed(
            snapshot, before, text, clock=clock.monotonic, sleep=clock.sleep)
        return clock.t, frames

    def test_does_not_return_while_pasting_indicator_is_visible(self):
        draft = ("composer with a long draft tail\nline two", (0, 1))
        elapsed, remaining = self._run(
            [_pasting(), _pasting(), draft, draft, draft], "idle")
        self.assertGreaterEqual(elapsed, term_submit.PASTE_STABLE_SECONDS)
        self.assertFalse(any("Pasting" in frame[0] for frame in remaining))

    def test_does_not_treat_a_stable_pasting_frame_as_consumed(self):
        clock = FakeClock()
        term_submit.wait_paste_consumed(
            _pasting, "idle", "hello",
            clock=clock.monotonic, sleep=clock.sleep)
        self.assertGreaterEqual(clock.t, term_submit.PASTE_WAIT_SECONDS)

    def test_tiny_pane_does_not_need_eight_visible_rows(self):
        tiny = ("LAST-LINE-ONLY\n", (0, 0))
        elapsed, _remaining = self._run([tiny, tiny, tiny], "idle",
                                        "FIRST\n" + ("x\n" * 80) + "LAST-LINE-ONLY")
        self.assertGreaterEqual(elapsed, term_submit.PASTE_STABLE_SECONDS)
        self.assertLess(elapsed, term_submit.PASTE_WAIT_SECONDS)

    def test_falls_back_when_snapshot_keeps_failing(self):
        clock = FakeClock()
        calls = {"n": 0}

        def snapshot():
            calls["n"] += 1
            raise RuntimeError("pane gone")

        term_submit.wait_paste_consumed(
            snapshot, "idle", "hello",
            clock=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(calls["n"], 3)
        self.assertLess(clock.t, term_submit.PASTE_WAIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
