"""Wait for a TUI to finish consuming a paste before Enter.

Paste followed 40 ms later by Enter is too fast for Windows ConPTY Claude: the
TUI still shows ``Pasting…`` when the key arrives, treats it as part of the
paste burst, and leaves the draft sitting in the composer
(BUG-20260913-093411-0837da).  Wait until that indicator is gone and the
screen has settled.  Do not scrape the composer or a fixed number of tail
rows: mobile panes are often one or two lines, and long drafts do not fit.
"""

from __future__ import annotations

import re
import time


PASTE_POLL_SECONDS = 0.04
PASTE_STABLE_SECONDS = 0.08
PASTE_WAIT_SECONDS = 1.2
_PASTING = re.compile(r"Pasting[….]+")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _plain(screen: str) -> str:
    return _ANSI.sub("", str(screen or ""))


def wait_paste_consumed(snapshot, before: str, text: str = "", *,
                        clock=time.monotonic, sleep=time.sleep) -> None:
    """Block until paste has left the transient ``Pasting…`` frame.

    ``snapshot`` returns ``(screen, cursor)``.  ``text`` is unused; callers
    still pass the pasted body so older wiring does not break.
    """
    del text
    deadline = clock() + PASTE_WAIT_SECONDS
    last = None
    stable_since = None
    changed = False
    failures = 0
    while clock() < deadline:
        try:
            screen, _cursor = snapshot()
            failures = 0
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            failures += 1
            if failures >= 3 and last is None:
                sleep(PASTE_POLL_SECONDS)
                return
            sleep(PASTE_POLL_SECONDS)
            continue
        now = clock()
        if screen != last:
            last = screen
            stable_since = now
        if before is None or screen != before:
            changed = True
        pasting = bool(_PASTING.search(_plain(screen)))
        stable = (stable_since is not None
                  and now - stable_since >= PASTE_STABLE_SECONDS)
        if changed and not pasting and stable:
            return
        sleep(PASTE_POLL_SECONDS)
