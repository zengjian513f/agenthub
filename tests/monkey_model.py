"""Pure state model and independent oracles for the paid dual-CLI monkey.

Nothing in this module starts Claude, Codex, tmux, a browser, or agenthub.  The
real harness imports it, while normal unit tests use the exact same scheduler
and invariants for free.  In particular, terminal classification deliberately
does not import ``claude_bridge`` or ``codex_bridge``: a production classifier
must not be allowed to certify itself.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
CLAUDE_RULE_RE = re.compile(r"^\s*─{12,}(?:\s+.+?\s+─+)?\s*$")
CLAUDE_BUSY_RE = re.compile(
    r"(?:\besc\s+to\s+(?:interrupt|stop|cancel)\b"
    r"|^\s*[✻✽✢✶✳✣✤]\s+\S[^\n]*…(?:\s+\([^\n)]*\))?\s*$"
    r"|^\s*[✻✽✢✶✳✣✤]\s+[^\n]*\b\d+\s+shells?\s+still\s+running\b[^\n]*$"
    r"|^\s*\*\s+\S[^\n]*…\s+\([^\n)]*\btokens?\b[^\n)]*\)\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
CODEX_BUSY_RE = re.compile(r"\bWorking\b.*\besc to interrupt\b", re.IGNORECASE)
CODEX_FOOTER_RE = re.compile(
    r"^\s*(?:gpt|codex|o\d)[\w.-]*(?:\s+\S+)*\s+·\s+\S.*$", re.IGNORECASE)
QUESTION_FOOTER_RE = re.compile(
    r"(?:press\s+enter\s+to\s+confirm|enter\s+to\s+(?:confirm|select|submit)"
    r"|esc\s+to\s+cancel)", re.IGNORECASE)
QUESTION_OPTION_RE = re.compile(r"(?m)^\s*(?:[›»❯>]\s*)?\d+[.)]\s+\S")


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", str(value or ""))


def terminal_has(value: str, marker: str) -> bool:
    """Find a marker even when a narrow TUI inserts hard line wraps."""
    return marker in value or "".join(marker.split()) in "".join(value.split())


def canonical_text(source: str, value: str) -> str:
    """Mirror only the CLI's input contract, not agenthub's implementation."""
    text = str(value or "")
    return text.strip() if source in {"claude", "codex"} else text


def _styled_chars(value: str) -> list[tuple[str, bool]]:
    """Return visible characters and whether SGR dim was active for each."""
    result: list[tuple[str, bool]] = []
    dim = False
    pos = 0
    while pos < len(value):
        escape = ANSI_RE.match(value, pos)
        if not escape:
            result.append((value[pos], dim))
            pos += 1
            continue
        sgr = SGR_RE.fullmatch(escape.group(0))
        if sgr:
            fields = sgr.group(1).split(";") if ";" in sgr.group(1) else [sgr.group(1)]
            at = 0
            while at < len(fields):
                field = fields[at]
                try:
                    code = int(field.split(":", 1)[0] or 0)
                except ValueError:
                    at += 1
                    continue
                if code == 0:
                    dim = False
                elif code == 2:
                    dim = True
                elif code == 22:
                    dim = False
                # Skip payload values of extended foreground/background colours.
                if code in {38, 48, 58} and ":" not in field and at + 1 < len(fields):
                    try:
                        mode = int(fields[at + 1] or 0)
                    except ValueError:
                        mode = 0
                    at += 2 if mode == 5 else 4 if mode == 2 else 0
                at += 1
        pos = escape.end()
    return result


def _claude_composer(styled: str, cursor: tuple[int, int] | None) -> str:
    if not cursor:
        return "unknown"
    raw = str(styled or "").replace("\r", "").splitlines()
    clean = [strip_ansi(line) for line in raw]
    cursor_x, cursor_y = cursor
    if cursor_y < 0 or cursor_y >= len(clean):
        return "unknown"
    upper = next((row for row in range(cursor_y, -1, -1)
                  if (CLAUDE_RULE_RE.match(clean[row])
                      or (clean[row].rstrip().endswith("─")
                          and "─" in clean[row]))), None)
    lower = next((row for row in range(cursor_y + 1, len(clean))
                  if CLAUDE_RULE_RE.match(clean[row])), None)
    if upper is None or lower is None or lower <= upper + 1:
        return "unknown"
    prompt_y = upper + 1
    marker = clean[prompt_y].find("❯")
    if marker < 0 or clean[prompt_y][:marker].strip() or not prompt_y <= cursor_y < lower:
        return "unknown"
    block = "\n".join(raw[prompt_y:lower])
    chars = _styled_chars(block)
    try:
        marker_at = next(at for at, (char, _dim) in enumerate(chars) if char == "❯")
    except StopIteration:
        return "unknown"
    content = [(char, dim) for char, dim in chars[marker_at + 1:]
               if not char.isspace()]
    if not content:
        return "empty"
    at_start = cursor_y == prompt_y and cursor_x <= marker + 2
    if SGR_RE.search(block) and any(not dim for _char, dim in content):
        return "editing"
    return "empty" if at_start else "editing"


def _codex_composer(styled: str) -> str:
    raw = str(styled or "").replace("\r", "").splitlines()
    clean = [strip_ansi(line) for line in raw]
    nonblank = [at for at, line in enumerate(clean) if line.strip()]
    if not nonblank:
        return "unknown"
    footer = nonblank[-1]
    if re.search(r"\bReady\b", clean[footer], re.IGNORECASE):
        while footer > 0 and clean[footer - 1].strip():
            footer -= 1
    elif not CODEX_FOOTER_RE.match(clean[footer]):
        return "unknown"
    end = footer - 1
    while end >= 0 and not clean[end].strip():
        end -= 1
    if end < 0:
        return "unknown"
    start = end
    while start > 0 and clean[start - 1].strip():
        start -= 1
    if clean[start].lstrip()[:1] not in {"›", "»"}:
        return "unknown"
    block = "\n".join(raw[start:end + 1])
    chars = _styled_chars(block)
    try:
        marker_at = next(at for at, (char, _dim) in enumerate(chars)
                         if char in {"›", "»"})
    except StopIteration:
        return "unknown"
    content = [(char, dim) for char, dim in chars[marker_at + 1:]
               if not char.isspace()]
    return "editing" if any(not dim for _char, dim in content) else "empty"


@dataclass(frozen=True)
class TerminalClassification:
    phase: str
    composer: str
    busy: bool
    question: bool


def classify_terminal(source: str, styled: str, current: str = "",
                      cursor: tuple[int, int] | None = None,
                      *, exists: bool = True) -> TerminalClassification:
    """Classify a physical pane without calling any production bridge parser."""
    if not exists:
        return TerminalClassification("exited", "unknown", False, False)
    clean = strip_ansi(styled or current)
    option_count = len(QUESTION_OPTION_RE.findall(clean))
    question = bool(option_count >= 2 and QUESTION_FOOTER_RE.search(clean))
    if question:
        return TerminalClassification("question", "unknown", False, True)
    busy = bool((CLAUDE_BUSY_RE if source == "claude" else CODEX_BUSY_RE).search(clean))
    composer = (_claude_composer(styled, cursor) if source == "claude"
                else _codex_composer(styled))
    if busy:
        phase = "busy"
    elif composer == "editing":
        phase = "editing"
    elif composer == "empty":
        phase = "idle"
    else:
        phase = "unknown"
    return TerminalClassification(phase, composer, busy, False)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    weight: float
    paid_turns: int = 0
    sources: frozenset[str] = frozenset({"claude", "codex"})
    phases: frozenset[str] = frozenset({"idle"})
    core: bool = False


ACTIONS: tuple[ActionSpec, ...] = (
    ActionSpec("browser_roundtrip", 8, 1, core=True),
    ActionSpec("browser_trim_roundtrip", 7, 1, core=True),
    ActionSpec("lost_response_idempotent_retry", 7, 1, core=True),
    ActionSpec("tmux_roundtrip", 8, 1, core=True),
    ActionSpec("queue_browser_behind_tmux", 6, 2, core=True),
    ActionSpec("queue_tmux_behind_browser", 5, 2, core=True),
    ActionSpec("interrupt_fast_cross_surface", 7, 1, core=True),
    ActionSpec("interrupt_mid_cross_surface", 5, 1),
    ActionSpec("draft_reject", 5, 0, core=True),
    ActionSpec("draft_overwrite", 5, 1, core=True),
    ActionSpec("question_cross_surface", 4, 1, core=True),
    ActionSpec("slash_interleave", 4, 1, core=True),
    ActionSpec("slash_rename", 2, 0),
    ActionSpec("slash_compact", 2, 1),
    ActionSpec("resize_while_busy", 6, 1, core=True),
    ActionSpec("switch_while_busy", 6, 1, core=True),
    ActionSpec("toggle_terminal_while_busy", 6, 1, core=True),
    ActionSpec("reload_while_busy", 6, 1, core=True),
    ActionSpec("disconnect_while_busy", 5, 1, core=True),
    ActionSpec("switch_idle", 3, 0,
               phases=frozenset({"idle", "editing", "unknown"})),
    ActionSpec("resize_idle", 3, 0,
               phases=frozenset({"idle", "editing", "unknown"})),
    ActionSpec("terminal_takeover", 3, 0,
               phases=frozenset({"idle", "editing", "busy", "question", "unknown"})),
)


@dataclass
class MonkeySessionState:
    source: str
    slot: int
    phase: str = "idle"
    paid_turns: int = 0
    frozen: bool = False
    operations: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class ScheduledAction:
    sequence: int
    action: ActionSpec
    source: str
    slot: int


class CoverageScheduler:
    """Coverage-guided weighted scheduler with deterministic seed replay."""

    def __init__(self, seed: int, *, max_paid_turns: int = 14,
                 actions: Sequence[ActionSpec] = ACTIONS):
        self.seed = seed
        self.random = random.Random(seed)
        self.max_paid_turns = max_paid_turns
        self.actions = tuple(actions)
        self.coverage: Counter = Counter()
        self.sequence = 0
        self.previous: tuple[str, int, str] | None = None

    def eligible(self, sessions: Iterable[MonkeySessionState]) -> list[tuple[MonkeySessionState, ActionSpec]]:
        result = []
        for session in sessions:
            if session.frozen:
                continue
            for action in self.actions:
                if session.source not in action.sources or session.phase not in action.phases:
                    continue
                if session.paid_turns + action.paid_turns > self.max_paid_turns:
                    continue
                result.append((session, action))
        return result

    def _weight(self, session: MonkeySessionState, action: ActionSpec) -> float:
        source_hits = self.coverage[(session.source, action.name)]
        session_hits = session.operations[action.name]
        # First make every core transition happen for both CLIs.  Afterwards,
        # favour under-sampled transitions without becoming deterministic.
        missing_boost = 18.0 if action.core and source_hits == 0 else 1.0
        rarity = 1.0 / math.sqrt(1 + source_hits)
        local_rarity = 1.0 / math.sqrt(1 + session_hits)
        repeat = 0.18 if self.previous == (
            session.source, session.slot, action.name) else 1.0
        return action.weight * missing_boost * rarity * local_rarity * repeat

    def choose(self, sessions: Sequence[MonkeySessionState]) -> ScheduledAction:
        choices = self.eligible(sessions)
        if not choices:
            raise RuntimeError("monkey 没有可执行动作；预算或状态模型已耗尽")
        weights = [self._weight(session, action) for session, action in choices]
        session, action = self.random.choices(choices, weights=weights, k=1)[0]
        self.sequence += 1
        return ScheduledAction(self.sequence, action, session.source, session.slot)

    def record(self, scheduled: ScheduledAction, session: MonkeySessionState) -> None:
        if (session.source, session.slot) != (scheduled.source, scheduled.slot):
            raise ValueError("调度动作与回报会话不一致")
        session.paid_turns += scheduled.action.paid_turns
        session.operations[scheduled.action.name] += 1
        self.coverage[(session.source, scheduled.action.name)] += 1
        self.previous = (session.source, session.slot, scheduled.action.name)

    def missing_core(self, sources: Iterable[str] = ("claude", "codex")) -> dict[str, list[str]]:
        required = [action.name for action in self.actions if action.core]
        return {
            source: [name for name in required if not self.coverage[(source, name)]]
            for source in sources
        }

    def snapshot(self) -> dict:
        return {
            "seed": self.seed,
            "sequence": self.sequence,
            "max_paid_turns": self.max_paid_turns,
            "coverage": {f"{source}:{name}": count
                         for (source, name), count in sorted(self.coverage.items())},
            "missing_core": self.missing_core(),
        }


@dataclass
class MessageExpectation:
    logical_id: str
    text: str
    marker: str
    reply_marker: str
    origin: str
    created: float
    server_id: str = ""
    terminal_seen: float | None = None
    reply_seen: float | None = None
    state: str = "active"


@dataclass(frozen=True)
class PendingView:
    text: str
    state: str = "queued"
    server_id: str = ""


@dataclass(frozen=True)
class LayerObservation:
    now: float
    page_id: str
    loaded: bool
    selected_uid: str
    terminal_phase: str
    terminal_history: str = ""
    terminal_current: str = ""
    browser_users: tuple[str, ...] = ()
    browser_assistants: tuple[str, ...] = ()
    browser_pending: tuple[PendingView, ...] = ()
    browser_activity: str = ""
    server_outbox: tuple[PendingView, ...] = ()


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    message: str
    logical_id: str = ""
    page_id: str = ""
    details: dict = field(default_factory=dict)


class ConsistencyOracle:
    """Stateful cross-layer invariants evaluated after every monkey step."""

    OPTIMISTIC_GRACE = 0.8
    DISAPPEAR_GRACE = 0.45
    DUPLICATE_GRACE = 0.35
    OVERLAP_GRACE = 1.5
    FORMAL_GRACE = 8.0
    REPLY_GRACE = 10.0
    ACTIVITY_GRACE = 3.0
    IDLE_OUTBOX_GRACE = 10.0

    def __init__(self, uid: str, source: str):
        self.uid = uid
        self.source = source
        self.messages: dict[str, MessageExpectation] = {}
        self._timers: dict[tuple, float] = {}
        self._reported: set[tuple] = set()

    def expect(self, logical_id: str, text: str, marker: str,
               reply_marker: str, origin: str, now: float,
               *, server_id: str = "") -> MessageExpectation:
        row = MessageExpectation(logical_id, text, marker, reply_marker,
                                 origin, now, server_id)
        self.messages[logical_id] = row
        return row

    def settle(self, logical_id: str, state: str) -> None:
        if logical_id in self.messages:
            self.messages[logical_id].state = state

    def _after(self, key: tuple, condition: bool, now: float,
               grace: float) -> bool:
        if not condition:
            self._timers.pop(key, None)
            return False
        since = self._timers.setdefault(key, now)
        return now - since >= grace

    def _issue(self, issues: list[InvariantIssue], key: tuple, code: str,
               message: str, expected: MessageExpectation | None,
               observation: LayerObservation, **details) -> None:
        if key in self._reported:
            return
        self._reported.add(key)
        issues.append(InvariantIssue(
            code, message, expected.logical_id if expected else "",
            observation.page_id, details))

    def observe(self, observation: LayerObservation) -> list[InvariantIssue]:
        issues: list[InvariantIssue] = []
        now = observation.now
        terminal = observation.terminal_history + "\n" + observation.terminal_current

        for expected in self.messages.values():
            terminal_user = terminal_has(terminal, expected.marker)
            terminal_reply = terminal_has(terminal, expected.reply_marker)
            if terminal_user and expected.terminal_seen is None:
                expected.terminal_seen = now
            if terminal_reply and expected.reply_seen is None:
                expected.reply_seen = now
            if not observation.loaded:
                continue

            users = sum(terminal_has(text, expected.marker)
                        for text in observation.browser_users)
            assistants = sum(terminal_has(text, expected.reply_marker)
                             for text in observation.browser_assistants)
            canonical = canonical_text(self.source, expected.text)
            pending_rows = [row for row in observation.browser_pending
                            if ((expected.server_id and row.server_id == expected.server_id)
                                or terminal_has(row.text, expected.marker)
                                or canonical_text(self.source, row.text) == canonical)]
            pending = len(pending_rows)
            outbox_rows = [row for row in observation.server_outbox if (
                (expected.server_id and row.server_id == expected.server_id)
                or terminal_has(row.text, expected.marker)
                or canonical_text(self.source, row.text) == canonical)]
            outbox = bool(outbox_rows)

            key_base = (observation.page_id, expected.logical_id)
            if self._after((*key_base, "duplicate-user"), users > 1, now,
                           self.DUPLICATE_GRACE):
                self._issue(issues, (*key_base, "duplicate-user"),
                            "duplicate_formal_user", "同一输入出现多条正式用户消息",
                            expected, observation, count=users)
            if self._after((*key_base, "duplicate-pending"), pending > 1, now,
                           self.DUPLICATE_GRACE):
                self._issue(issues, (*key_base, "duplicate-pending"),
                            "duplicate_pending", "同一输入出现多条浏览器 pending",
                            expected, observation, count=pending)
            if self._after((*key_base, "overlap"), bool(users and pending), now,
                           self.OVERLAP_GRACE):
                self._issue(issues, (*key_base, "overlap"),
                            "formal_pending_overlap", "正式消息出现后 pending 未收敛",
                            expected, observation, users=users, pending=pending,
                            outbox=outbox)

            active = expected.state == "active"
            visible = bool(users or pending)
            optimistic_missing = (active and expected.origin == "browser"
                                  and now - expected.created >= self.OPTIMISTIC_GRACE
                                  and not visible)
            if self._after((*key_base, "optimistic-missing"), optimistic_missing,
                           now, self.DISAPPEAR_GRACE):
                self._issue(issues, (*key_base, "optimistic-missing"),
                            "browser_message_disappeared",
                            "网页发送的消息在正式记录出现前从页面消失",
                            expected, observation, terminal_user=terminal_user,
                            outbox=outbox)

            formal_late = (active and expected.terminal_seen is not None
                           and now - expected.terminal_seen >= self.FORMAL_GRACE
                           and users != 1)
            if formal_late:
                self._issue(issues, (*key_base, "formal-late"),
                            "terminal_user_not_synced",
                            "tmux 已接收输入，但浏览器没有恰好一条正式消息",
                            expected, observation, users=users, pending=pending,
                            outbox=outbox)
            reply_late = (active and expected.reply_seen is not None
                          and now - expected.reply_seen >= self.REPLY_GRACE
                          and assistants != 1)
            if reply_late:
                self._issue(issues, (*key_base, "reply-late"),
                            "terminal_reply_not_synced",
                            "tmux 已显示回复，但浏览器回复没有收敛",
                            expected, observation, assistants=assistants)

            stale_pending = (active and users == 1 and pending > 0 and not outbox)
            if self._after((*key_base, "stale-pending"), stale_pending, now,
                           self.OVERLAP_GRACE):
                self._issue(issues, (*key_base, "stale-pending"),
                            "retired_outbox_still_pending",
                            "服务端已确认且正式消息存在，浏览器仍显示发送中",
                            expected, observation, pending=pending)

            active_outbox = any(row.state in {
                "queued", "native_queuing", "native_queued", "persisted",
                "injecting", "submitted", "delivering",
            } for row in outbox_rows)
            idle_outbox = (active and expected.origin == "browser" and active_outbox
                           and observation.terminal_phase == "idle")
            if self._after((*key_base, "idle-outbox"), idle_outbox, now,
                           self.IDLE_OUTBOX_GRACE):
                self._issue(issues, (*key_base, "idle-outbox"),
                            "idle_outbox_stuck",
                            "tmux 输入框空闲，但服务端消息长期没有投递",
                            expected, observation,
                            states=[row.state for row in outbox_rows])

        if observation.loaded and observation.selected_uid == self.uid:
            busy_mismatch = (observation.terminal_phase == "busy"
                             and observation.browser_activity not in {"working", "waiting"})
            idle_mismatch = (observation.terminal_phase in {"idle", "editing"}
                             and observation.browser_activity == "working")
            if self._after((observation.page_id, "activity-busy"), busy_mismatch,
                           now, self.ACTIVITY_GRACE):
                self._issue(issues, (observation.page_id, "activity-busy"),
                            "busy_not_visible", "tmux 正在工作，但页面没有显示活动状态",
                            None, observation, activity=observation.browser_activity)
            if self._after((observation.page_id, "activity-idle"), idle_mismatch,
                           now, self.ACTIVITY_GRACE):
                self._issue(issues, (observation.page_id, "activity-idle"),
                            "stale_working", "tmux 已空闲，但页面仍显示 Working",
                            None, observation, activity=observation.browser_activity)
        return issues


def simulate_oracle_regressions() -> dict:
    """Prove the free oracle detects the production failures it was built for."""
    uid = "codex:simulated"
    text = "simulated SIM-REQ\n"

    def make_oracle() -> ConsistencyOracle:
        oracle = ConsistencyOracle(uid, "codex")
        oracle.expect("sim", text, "SIM-REQ", "SIM-RSP", "browser", 0,
                      server_id="request-sim")
        return oracle

    def view(at: float, *, users=(), assistants=(), pending=(), outbox=(),
             phase="busy", activity="working", history="SIM-REQ"):
        return LayerObservation(
            at, "desktop", True, uid, phase, history, "", tuple(users),
            tuple(assistants), tuple(pending), activity, tuple(outbox))

    detected: dict[str, list[str]] = {}

    oracle = make_oracle()
    row = PendingView(text, server_id="request-sim")
    oracle.observe(view(0.2, pending=(row,), outbox=(row,)))
    oracle.observe(view(1.0, history=""))
    detected["message_disappeared"] = [
        issue.code for issue in oracle.observe(view(1.6, history=""))]

    oracle = make_oracle()
    oracle.observe(view(1.0, users=(text,), pending=(row,)))
    detected["formal_plus_pending"] = [
        issue.code for issue in oracle.observe(
            view(2.6, users=(text,), pending=(row,)))]

    oracle = make_oracle()
    oracle.observe(view(1.0, users=(text,), phase="idle", activity="working"))
    detected["stale_working"] = [
        issue.code for issue in oracle.observe(
            view(4.1, users=(text,), phase="idle", activity="working"))]

    oracle = make_oracle()
    queued = PendingView(text, state="queued", server_id="request-sim")
    oracle.observe(view(1.0, pending=(queued,), outbox=(queued,),
                        phase="idle", activity="idle", history=""))
    detected["idle_outbox_stuck"] = [
        issue.code for issue in oracle.observe(view(
            11.1, pending=(queued,), outbox=(queued,), phase="idle",
            activity="idle", history=""))]

    oracle = make_oracle()
    normal = []
    normal += oracle.observe(view(0.2, pending=(row,), outbox=(row,)))
    normal += oracle.observe(view(1.0, users=(text,), pending=(), outbox=()))
    normal += oracle.observe(view(
        2.0, users=(text,), assistants=("SIM-RSP",), phase="idle",
        activity="idle", history="SIM-REQ\nSIM-RSP"))

    expected = {
        "message_disappeared": {"browser_message_disappeared"},
        "formal_plus_pending": {
            "formal_pending_overlap", "retired_outbox_still_pending"},
        "stale_working": {"stale_working"},
        "idle_outbox_stuck": {"idle_outbox_stuck"},
    }
    missing = {name: sorted(codes - set(detected.get(name, [])))
               for name, codes in expected.items()
               if codes - set(detected.get(name, []))}
    return {"detected": detected, "normal_issues": [row.code for row in normal],
            "missing": missing}


def simulate_schedule(seed: int, *, steps: int = 500,
                      sessions_per_source: int = 10,
                      max_paid_turns: int = 30,
                      initial_paid_turns: int = 2) -> dict:
    """Exercise the real scheduler without starting either paid CLI."""
    scheduler = CoverageScheduler(seed, max_paid_turns=max_paid_turns)
    sessions = [MonkeySessionState(source, slot,
                                   paid_turns=initial_paid_turns)
                for source in ("claude", "codex")
                for slot in range(1, sessions_per_source + 1)]
    trace = []
    for _ in range(steps):
        scheduled = scheduler.choose(sessions)
        session = next(row for row in sessions
                       if (row.source, row.slot) == (scheduled.source, scheduled.slot))
        scheduler.record(scheduled, session)
        trace.append({"sequence": scheduled.sequence,
                      "source": scheduled.source, "slot": scheduled.slot,
                      "action": scheduled.action.name})
    return {"scheduler": scheduler.snapshot(),
            "oracle": simulate_oracle_regressions(), "trace": trace,
            "sessions": [{"source": row.source, "slot": row.slot,
                          "paid_turns": row.paid_turns,
                          "operations": dict(row.operations)} for row in sessions]}
