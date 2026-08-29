"""One-hour paid Claude/Codex state-machine monkey.

This test is intentionally never part of unittest/e2e discovery.  It creates
ten real sessions per CLI, pins their cheapest requested models, registers the
whole run as hidden before starting a TUI, and keeps evidence under the run
root.  A crash leaves the debug registry in place, so test sessions stay out of
the user's ordinary sesman list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sesman import claude_bridge, codex_bridge, debug_runs, term
from monkey_model import (
    ACTIONS,
    ConsistencyOracle,
    CoverageScheduler,
    LayerObservation,
    MonkeySessionState,
    PendingView,
    ScheduledAction,
    classify_terminal,
    simulate_schedule,
)


CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_DURATION = 3600


def wait_for(label, predicate, timeout=120.0, interval=0.1):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(interval)
    suffix = f": {last_error}" if last_error else ""
    raise AssertionError(f"等待超时：{label}{suffix}")


def terminal_has(text: str, marker: str) -> bool:
    """Match a token even when a TUI hard-wraps it after a resize.

    ``tmux capture-pane -J`` rejoins tmux soft wraps, but Claude/Codex may
    repaint a narrow pane with literal newlines and indentation inside a long
    identifier.  Monkey markers contain no meaningful whitespace, so compare
    a whitespace-free form as the resize-safe fallback.
    """
    return marker in text or "".join(marker.split()) in "".join(text.split())


@dataclass
class Session:
    source: str
    index: int
    cwd: Path
    name: str
    sid: str = ""
    uid: str = ""
    path: Path | None = None
    turns: int = 0
    frozen: bool = False
    tracked: dict[str, dict] = field(default_factory=dict)
    startup_flags: set[str] = field(default_factory=set)
    ops: set[str] = field(default_factory=set)
    last_reply: str = ""
    terminal_state: tuple | None = None
    schedule: MonkeySessionState | None = None
    oracle: ConsistencyOracle | None = None


class Driver:
    source = ""
    model = ""

    def __init__(self, monkey: "DualMonkey", count: int):
        self.monkey = monkey
        self.count = count
        self.sessions: list[Session] = []

    def command(self, session: Session) -> str:
        raise NotImplementedError

    def ready(self, screen: str) -> bool:
        raise NotImplementedError

    def native_models(self, session: Session) -> set[str]:
        raise NotImplementedError

    def create(self):
        for number in range(1, self.count + 1):
            cwd = self.monkey.root / self.source / f"session-{number:02d}"
            cwd.mkdir(parents=True)
            sid = str(uuid.uuid4()) if self.source == "claude" else ""
            token = sid[:8] if sid else f"{self.monkey.run_id}-{number:02d}"
            shell_name = f"monkey-{self.source}-{token}"
            placeholder = Session(self.source, number, cwd, "", sid=sid)
            placeholder.schedule = MonkeySessionState(self.source, number)
            command = self.command(placeholder)
            name = term.new_session(shell_name, command, str(cwd), 108, 30)
            placeholder.name = name
            debug_runs.add_session(
                self.monkey.run_id, source=self.source, cwd=str(cwd),
                sid=sid, name=name)
            self.sessions.append(placeholder)
            self.monkey.sessions.append(placeholder)
            self.monkey.event("session_started", placeholder)

        for session in self.sessions:
            screen = wait_for(
                f"{session.name} {self.model} 输入框",
                lambda s=session: self._startup(s), timeout=60, interval=0.2)
            self.monkey.event("session_ready", session, screen=screen[-1200:])

    def _startup(self, session: Session):
        screen = term.capture_plain(session.name, 120)
        lowered = screen.casefold()
        if "trust this folder" in lowered or "trust this directory" in lowered:
            term.send_keys(session.name, "Enter")
            return ""
        return screen if self.ready(screen) else ""


class ClaudeDriver(Driver):
    source = "claude"
    model = CLAUDE_MODEL

    def command(self, session: Session) -> str:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("找不到 claude")
        return shlex.join([
            # ``plan`` occasionally refuses even a deterministic echo prompt,
            # which tests model policy rather than sesman's transport.  Keep
            # the ordinary permission gate and expose only the one tool needed
            # for the cross-surface question scenario.
            exe, "--setting-sources", "", "--permission-mode", "default",
            "--tools", "AskUserQuestion", "--model", self.model,
            "--effort", "low", "--session-id", session.sid,
            "--settings", claude_bridge.settings_path(),
        ])

    def ready(self, screen: str) -> bool:
        text = screen.casefold()
        return ("haiku 4.5" in text and "plan mode on" not in text
                and ("❯" in screen or "how can i help" in text))

    def native_models(self, session: Session) -> set[str]:
        models = set()
        if not session.path or not session.path.is_file():
            return models
        for raw in session.path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            model = (row.get("message") or {}).get("model")
            if model:
                models.add(str(model))
        return models


class CodexDriver(Driver):
    source = "codex"
    model = CODEX_MODEL

    def command(self, session: Session) -> str:
        exe = shutil.which("codex")
        if not exe:
            raise RuntimeError("找不到 codex")
        return shlex.join([
            exe, "--model", self.model,
            "--sandbox", "read-only", "--ask-for-approval", "on-request",
            "--no-alt-screen", "--enable", "default_mode_request_user_input",
            "-c", 'model_reasoning_effort="low"',
            "-c", "suppress_unstable_features_warning=true",
        ])

    def ready(self, screen: str) -> bool:
        text = screen.casefold()
        return "gpt-5.6-luna" in text and ("›" in screen or "ask codex" in text)

    def _startup(self, session: Session):
        screen = term.capture_plain(session.name, 120)
        lowered = screen.casefold()
        if "update available" in lowered and "update_prompt" not in session.startup_flags:
            session.startup_flags.add("update_prompt")
            term.send_keys(session.name, "2", "Enter")
            return ""
        if (("trust this folder" in lowered or "trust this directory" in lowered
                or "do you trust the contents" in lowered)
                and "trust_prompt" not in session.startup_flags):
            session.startup_flags.add("trust_prompt")
            term.send_keys(session.name, "1", "Enter")
            return ""
        return screen if self.ready(screen) else ""

    def native_models(self, session: Session) -> set[str]:
        models = set()
        if not session.path or not session.path.is_file():
            return models
        for raw in session.path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("type") != "turn_context":
                continue
            model = (row.get("payload") or {}).get("model")
            if model:
                models.add(str(model))
        return models


class DualMonkey:
    def __init__(self, base: str, count: int, duration: int, seed: int,
                 max_paid_turns: int = 14,
                 replay_actions: list[dict] | None = None):
        if count != 10:
            raise ValueError("本轮按用户要求必须每种 CLI 恰好 10 个会话")
        self.base = base.rstrip("/")
        self.count = count
        self.duration = duration
        self.seed = seed
        self.random = random.Random(seed)
        self.max_paid_turns = max_paid_turns
        self.replay_actions = list(replay_actions or [])
        self.scheduler = CoverageScheduler(seed, max_paid_turns=max_paid_turns)
        self.current_action: ScheduledAction | None = None
        self.action_serial = 0
        self.action_trace: list[dict] = []
        self.pages: dict[str, Page] = {}
        # One native-dialog listener per Page for its whole lifetime.  Adding
        # and removing ad-hoc listeners around actions is racy: a late
        # takeover/network alert can cross the action boundary and two
        # callbacks then both call Page.handleJavaScriptDialog.  Chromium
        # rejects the second call and can take down Playwright's driver.
        self.dialog_brokers: dict[int, dict] = {}
        self.run_id = f"monkey-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.root = Path(tempfile.mkdtemp(prefix=f"sesman-{self.run_id}-"))
        os.chmod(self.root, 0o700)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.events_path = self.artifacts / "events.jsonl"
        self.failures: list[dict] = []
        self.event_counts: dict[str, int] = {}
        self.dom_rows: list[dict] = []
        self.question_sources: set[str] = set()
        self.last_model_audit = 0.0
        self.last_visibility_audit = 0.0
        self.timed_elapsed = 0.0
        self.build = str(self.get("/api/meta", debug=False)["build"])
        debug_runs.register(self.run_id, self.root)
        self.claude = ClaudeDriver(self, count)
        self.codex = CodexDriver(self, count)
        self.drivers = {"claude": self.claude, "codex": self.codex}
        self.sessions: list[Session] = []
        self.event("run_registered", root=str(self.root), duration=duration,
                   seed=seed, max_paid_turns=max_paid_turns)

    def url(self, path: str, *, debug=True) -> str:
        url = urllib.parse.urlsplit(self.base + path)
        if not debug:
            return urllib.parse.urlunsplit(url)
        query = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        query.append(("debug_run", self.run_id))
        return urllib.parse.urlunsplit((*url[:3], urllib.parse.urlencode(query), url.fragment))

    def get(self, path: str, *, debug=True):
        with urllib.request.urlopen(self.url(path, debug=debug), timeout=60) as response:
            return json.loads(response.read())

    def event(self, kind: str, session: Session | None = None, **data):
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        row = {"wall": time.time(), "mono": time.monotonic(), "event": kind,
               "run_id": self.run_id, **data}
        if self.current_action:
            row.update({"step": self.current_action.sequence,
                        "action": self.current_action.action.name})
        if session:
            row.update({"source": session.source, "slot": session.index,
                        "uid": session.uid, "sid": session.sid,
                        "name": session.name})
        if kind == "dom_state":
            self.dom_rows.append(row)
        with self.events_path.open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        # High-frequency state transitions remain in the evidence JSONL.  Keep
        # stdout compact enough that an hour-long run can be monitored live.
        if kind not in {"dom_state", "delivery_state", "send_response", "ui_sent",
                        "oracle_observation"}:
            console = {key: value for key, value in row.items()
                       if key not in {"screen", "summary", "response"}}
            print(json.dumps(console, ensure_ascii=False, default=str), flush=True)

    def fail(self, reason: str, session: Session | None = None, **data):
        row = {"reason": reason, "source": session.source if session else "",
               "slot": session.index if session else 0, **data}
        self.failures.append(row)
        if session:
            session.frozen = True
            try:
                row["screen"] = term.capture_plain(session.name, 160)[-6000:]
            except Exception as error:
                row["screen_error"] = str(error)
        try:
            row["evidence"] = str(self.capture_failure_bundle(session, row))
        except Exception as error:
            row["evidence_error"] = f"{type(error).__name__}: {error}"
        self.event("failure", session, **row)

    def capture_failure_bundle(self, session: Session | None,
                               failure: dict) -> Path:
        """Persist enough evidence to replay a failure without guesswork."""
        number = len(self.failures)
        bundle = self.artifacts / f"failure-{number:03d}"
        bundle.mkdir(exist_ok=True)
        (bundle / "failure.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2, default=str) + "\n")
        (bundle / "replay.json").write_text(json.dumps({
            "seed": self.seed, "run_id": self.run_id,
            "failed_step": self.current_action.sequence if self.current_action else 0,
            "max_paid_turns": self.max_paid_turns,
            "actions": self.action_trace,
        }, ensure_ascii=False, indent=2, default=str) + "\n")
        if self.events_path.exists():
            tail = self.events_path.read_text(errors="replace").splitlines()[-400:]
            (bundle / "events-tail.jsonl").write_text("\n".join(tail) + "\n")
        if session and session.name:
            (bundle / "tmux-history.txt").write_text(
                term.capture_plain(session.name, 3000), errors="replace")
            (bundle / "tmux-screen.txt").write_text(
                term.capture_screen_plain(session.name), errors="replace")
            (bundle / "tmux-screen.ansi").write_text(
                term.capture_screen(session.name), errors="replace")
            if session.uid:
                query = urllib.parse.urlencode({"uid": session.uid})
                outbox = self.get(f"/api/session/outbox?{query}")
                (bundle / "server-outbox.json").write_text(
                    json.dumps(outbox, ensure_ascii=False, indent=2) + "\n")
            if session.path and session.path.is_file():
                with session.path.open("rb") as stream:
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, size - 256 * 1024))
                    native = stream.read().decode("utf-8", "replace")
                (bundle / "native-tail.jsonl").write_text(native)
        for label, page in list(self.pages.items()):
            try:
                state = page.evaluate("""() => ({
                  href:location.href, selected:String(S?.sel||''),
                  terminal:{name:T?.name||'',mode:T?.mode||'',
                    views:[...(T?.views||new Map()).entries()].map(([name,v])=>({
                      name,ws:v?.ws?.readyState??-1,revoked:!!v?.revoked}))},
                  pending:[...(typeof pendingByUid!=='undefined' ? pendingByUid : new Map())]
                    .map(([uid,rows])=>[uid,rows]),
                  cache:[...(typeof cache!=='undefined' ? cache : new Map()).entries()]
                    .map(([key,v])=>({key,uid:v?.meta?.uid||'',end:v?.end,
                      count:v?.msgs?.length||0,activity:v?.activity||null})),
                  body:(document.body?.innerText||'').slice(-20000)
                })""")
                (bundle / f"browser-{label}.json").write_text(
                    json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n")
                (bundle / f"browser-{label}.html").write_text(
                    page.content(), errors="replace")
                page.screenshot(path=str(bundle / f"browser-{label}.png"),
                                full_page=True)
            except Exception as error:
                (bundle / f"browser-{label}-error.txt").write_text(str(error))
        return bundle

    def create_sessions(self):
        self.claude.create()
        self.codex.create()
        self.assert_hidden(allow_debug_count=None)

    def exchange(self, session: Session, kind: str, serial: str | int = "") -> dict:
        suffix = str(serial or session.turns + 1)
        stem = f"{kind}-{self.run_id}-{session.source}-{session.index}-{suffix}"
        request = f"{stem}-REQ"
        # The request stays verbose so every transport hop is unambiguous, but
        # asking a cheap model to reproduce that whole identifier introduced
        # content typos unrelated to delivery.  A 40-bit public test label
        # keeps the oracle unique while remaining easy to include naturally.
        label = ("sesman-test-" + hashlib.sha256(stem.encode()).hexdigest()[:10]
                 + "-req")
        reply = label.removesuffix("-req") + "-rsp"
        text = (f"This is a local sesman UI synchronization test for request {request}. "
                "Reply in one short sentence confirming that you saw it, and include "
                f"the test label {label} after changing its final -req to -rsp.")
        return {"text": text, "request": request, "reply": reply}

    def track_exchange(self, session: Session, exchange: dict, kind: str,
                       origin: str, *, created: float | None = None,
                       server_id: str = "") -> dict:
        """Register one expected input once for both trace and live oracle."""
        tracked = {
            **exchange, "created": created or time.time(), "kind": kind,
            "origin": origin, "server_id": server_id,
        }
        session.tracked[exchange["request"]] = tracked
        if session.oracle:
            session.oracle.expect(
                exchange["request"], exchange["text"], exchange["request"],
                exchange["reply"], origin, tracked["created"],
                server_id=server_id)
        return tracked

    def terminal_snapshot(self, session: Session, history_lines: int = 300) -> dict:
        history = term.capture_plain(session.name, history_lines)
        current = term.capture_screen_plain(session.name)
        styled = term.capture_screen(session.name)
        cursor = None
        try:
            cursor = term.cursor_position(session.name)
        except (OSError, RuntimeError, ValueError):
            pass
        # The test oracle is deliberately independent of the product bridge.
        # Keep the production result only as diagnostic evidence; never use it
        # to decide whether a test action was accepted or whether a turn ended.
        classified = classify_terminal(
            session.source, styled, current, cursor, exists=True)
        if session.source == "claude":
            product_composer = claude_bridge.composer_state(styled, cursor)
            product_busy = claude_bridge.busy_screen(styled)
        else:
            product_composer = codex_bridge.composer_state(styled)
            product_busy = codex_bridge.busy_screen(styled)
        return {
            "history": history, "current": current, "styled": styled,
            "cursor": cursor, "composer": classified.composer,
            "busy": classified.busy, "phase": classified.phase,
            "question": classified.question,
            "product_composer": product_composer,
            "product_busy": product_busy,
            "history_hash": hashlib.sha256(history.encode("utf-8", "replace")).hexdigest()[:16],
            "current_hash": hashlib.sha256(current.encode("utf-8", "replace")).hexdigest()[:16],
        }

    def probe_terminal(self, session: Session) -> dict:
        snapshot = self.terminal_snapshot(session)
        state = (snapshot["phase"], snapshot["composer"], snapshot["busy"],
                 snapshot["product_composer"], snapshot["product_busy"])
        if state != session.terminal_state:
            self.event("terminal_state", session, phase=snapshot["phase"],
                       composer=snapshot["composer"], busy=snapshot["busy"],
                       product_composer=snapshot["product_composer"],
                       product_busy=snapshot["product_busy"],
                       classifier_agrees=(
                           snapshot["composer"] == snapshot["product_composer"]
                           and snapshot["busy"] == snapshot["product_busy"]),
                       cursor=snapshot["cursor"],
                       current_hash=snapshot["current_hash"],
                       screen=snapshot["current"][-4000:])
            session.terminal_state = state
        if session.schedule:
            session.schedule.phase = snapshot["phase"]
        return snapshot

    def wait_terminal_marker(self, session: Session, marker: str, event: str,
                             timeout: float = 120) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session.frozen:
                return None
            try:
                snapshot = self.probe_terminal(session)
            except Exception as error:
                self.fail("读取 tmux pane 失败", session, error=str(error), marker=marker)
                return None
            if (terminal_has(snapshot["history"], marker)
                    or terminal_has(snapshot["current"], marker)):
                self.event(event, session, marker=marker,
                           composer=snapshot["composer"], busy=snapshot["busy"],
                           history_hash=snapshot["history_hash"],
                           screen=snapshot["history"][-5000:])
                return snapshot
            time.sleep(0.08)
        self.fail("tmux pane 未出现预期文本", session, marker=marker,
                  expected_event=event, timeout=timeout)
        return None

    def type_terminal(self, session: Session, text: str, marker: str,
                      operation: str, *, submit: bool) -> dict | None:
        """Type through the pane and optionally press Enter as a human would.

        This must stay independent of ``term.submit_text``.  The latter is the
        product's browser-to-TUI paste path and is exercised only by Playwright
        sends.  Reusing it here would make both directions share the same bug.
        """
        if session.frozen:
            return None
        term.send_text(session.name, text)
        deadline = time.monotonic() + 10
        typed = None
        while time.monotonic() < deadline:
            typed = self.probe_terminal(session)
            if terminal_has(typed["current"], marker):
                break
            time.sleep(0.04)
        else:
            self.fail("tmux 键入的文字未出现在真实编辑器", session,
                      operation=operation, marker=marker)
            return None
        self.event("terminal_text_typed", session, operation=operation,
                   marker=marker, composer=typed["composer"],
                   busy=typed["busy"], current_hash=typed["current_hash"],
                   screen=typed["current"][-4000:])
        if not submit:
            return typed

        # A short human-scale pause separates literal key events from Enter.
        # It is intentionally not the product's bracketed-paste workaround.
        time.sleep(0.15)
        term.send_keys(session.name, "Enter")
        self.event("tmux_enter", session, operation=operation, marker=marker)

        # Seeing the marker is insufficient: an ignored Enter leaves it in the
        # composer.  Require the pane to leave editing state (or show activity)
        # stably before treating the key as accepted.
        accepted_since = None
        deadline = time.monotonic() + 12
        after = typed
        while time.monotonic() < deadline:
            after = self.probe_terminal(session)
            accepted = (after["busy"] or after["composer"] != "editing"
                        or not terminal_has(after["current"], marker))
            if accepted:
                accepted_since = accepted_since or time.monotonic()
                if time.monotonic() - accepted_since >= 0.2:
                    self.event("terminal_enter_accepted", session,
                               operation=operation, marker=marker,
                               composer=after["composer"], busy=after["busy"],
                               current_hash=after["current_hash"],
                               screen=after["current"][-4000:])
                    return after
            else:
                accepted_since = None
            time.sleep(0.04)
        self.fail("tmux 按 Enter 后文字仍留在编辑器", session,
                  operation=operation, marker=marker,
                  composer=after["composer"], busy=after["busy"])
        return None

    def submit_native(self, session: Session, exchange: dict, kind: str,
                      *, wait_reply: bool = True) -> dict | None:
        paid = max(session.turns,
                   session.schedule.paid_turns if session.schedule else 0)
        if session.frozen or paid >= self.max_paid_turns:
            return None
        if not self.type_terminal(session, exchange["text"],
                                  exchange["request"], kind, submit=True):
            return None
        session.turns += 1
        self.track_exchange(session, exchange, kind, "tmux")
        if not self.wait_terminal_marker(
                session, exchange["request"], "terminal_prompt_seen", timeout=20):
            return None
        if not wait_reply:
            return self.probe_terminal(session)
        reply = self.wait_terminal_marker(
            session, exchange["reply"], "terminal_reply_seen", timeout=180)
        if reply:
            session.last_reply = exchange["reply"]
        return reply

    def bootstrap(self):
        for offset in range(0, len(self.sessions), 5):
            for session in self.sessions[offset:offset + 5]:
                exchange = self.exchange(session, "BOOT", 1)
                self.track_exchange(session, exchange, "bootstrap", "tmux")
                if not self.type_terminal(
                        session, exchange["text"], exchange["request"],
                        "bootstrap", submit=True):
                    continue
                session.turns += 1
            time.sleep(0.8)

        # Bootstrap completion is proven from each pane, not from JSONL/API.
        for session in self.sessions:
            tracked = next(iter(session.tracked.values()))
            self.wait_terminal_marker(
                session, tracked["request"], "terminal_prompt_seen", timeout=30)
            reply = self.wait_terminal_marker(
                session, tracked["reply"], "terminal_reply_seen", timeout=180)
            if reply:
                session.last_reply = tracked["reply"]

        def indexed():
            rows = self.get("/api/sessions?force=1").get("sessions", [])
            by_cwd = {str(row.get("cwd")): row for row in rows}
            return rows if all(str(session.cwd) in by_cwd for session in self.sessions) else None

        rows = wait_for("20 个 debug 会话落盘并索引", indexed, timeout=180, interval=0.5)
        by_cwd = {str(row.get("cwd")): row for row in rows}
        for session in self.sessions:
            row = by_cwd[str(session.cwd)]
            session.uid = str(row["uid"])
            session.sid = str(row["sid"])
            session.path = Path(row["path"])
            session.oracle = ConsistencyOracle(session.uid, session.source)
            for tracked in session.tracked.values():
                session.oracle.expect(
                    tracked["request"], tracked["text"], tracked["request"],
                    tracked["reply"], tracked["origin"], tracked["created"],
                    server_id=str(tracked.get("server_id") or ""))
            debug_runs.add_session(
                self.run_id, source=session.source, cwd=str(session.cwd),
                sid=session.sid, uid=session.uid, name=session.name)
            self.event("session_linked", session, path=str(session.path), model=row.get("model"))

        self.assert_models()
        self.assert_hidden(allow_debug_count=20)

    def assert_models(self):
        if len(self.sessions) != self.count * 2 or any(not session.path for session in self.sessions):
            raise AssertionError("20 个会话尚未全部关联，不能执行模型校验")
        for session in self.sessions:
            expected = self.drivers[session.source].model
            models = self.drivers[session.source].native_models(session)
            if models != {expected}:
                raise AssertionError(
                    f"{session.source} {session.index} 模型不符，立即停止：{models} != {expected}")
        self.event("models_verified", claude=CLAUDE_MODEL, codex=CODEX_MODEL)
        self.last_model_audit = time.monotonic()

    def assert_hidden(self, allow_debug_count: int | None):
        normal = self.get("/api/sessions?force=1", debug=False).get("sessions", [])
        leaked = [row for row in normal if str(row.get("cwd") or "").startswith(str(self.root))]
        debug = self.get("/api/sessions?force=1").get("sessions", [])
        if leaked:
            raise AssertionError(f"debug 会话泄漏到正常列表：{leaked}")
        if allow_debug_count is not None and len(debug) != allow_debug_count:
            raise AssertionError(f"debug 列表数量 {len(debug)} != {allow_debug_count}")
        self.event("visibility_verified", normal_leaks=0, debug_count=len(debug))
        self.last_visibility_audit = time.monotonic()

    def audit_all(self):
        for session in self.sessions:
            if not session.frozen:
                try:
                    if not term.has_session(session.name):
                        self.fail("tmux pane 意外退出", session)
                        continue
                    self.probe_terminal(session)
                except Exception as error:
                    self.fail("tmux pane 巡检异常", session, error=str(error))
        now = time.monotonic()
        if now - self.last_model_audit >= 30:
            self.assert_models()
        if now - self.last_visibility_audit >= 30:
            self.assert_hidden(allow_debug_count=20)

    def open_session(self, page: Page, session: Session):
        # Do not return openSession's Promise to Playwright.  A reconnect bug
        # can leave that Promise pending forever, and Page.evaluate has no
        # per-call timeout in the sync API.  Trigger it like a real click and
        # observe the externally visible state with a bounded wait instead.
        page.evaluate("uid => { void openSession(uid); }", session.uid)
        page.wait_for_function("uid => S.sel === uid", arg=session.uid, timeout=30000)
        # A desktop session intentionally hides the conversation while its
        # terminal is in ``full`` mode.  ``openSession`` has still completed
        # and #msgs is valid in that state; requiring visibility here made the
        # resize/toggle actions deadlock before they could switch back to chat.
        page.wait_for_selector("#msgs", state="attached", timeout=30000)

    def ensure_composer(self, page: Page, session: Session):
        self.open_session(page, session)
        # Playwright's ``is_visible`` only checks layout geometry.  On the
        # mobile terminal overlay the chat composer keeps geometry underneath
        # xterm, so it reports visible although every pointer event hits the
        # terminal shortcut bar.  Require both the input and Send button to be
        # the topmost element at their centres.
        probe = """() => {
          const input=document.querySelector('#cinput');
          const send=document.querySelector('#csend');
          const pane=document.querySelector('#termpane');
          const hits=el => {
            if (!el) return false;
            const r=el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            const top=document.elementFromPoint(
              r.left + r.width / 2, r.top + r.height / 2);
            return !!top && (top === el || el.contains(top));
          };
          return {ready:hits(input) && hits(send), mobile:MOBILE.matches,
            mode:T.mode, paneVisible:!!pane && !pane.classList.contains('hidden')};
        }"""
        view = page.evaluate(probe)
        if view["ready"]:
            return
        if (not view["paneVisible"]
                or (not view["mobile"] and view["mode"] != "full")):
            raise RuntimeError(f"对话编辑框不可交互：{view}")
        page.locator("#a-term").click()
        page.wait_for_function("""() => {
          const hits=el => {
            if (!el) return false;
            const r=el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            const top=document.elementFromPoint(
              r.left + r.width / 2, r.top + r.height / 2);
            return !!top && (top === el || el.contains(top));
          };
          return hits(document.querySelector('#cinput'))
            && hits(document.querySelector('#csend'));
        }""", timeout=30000)

    def install_dialog_broker(self, page: Page, label: str):
        """Install the page's sole native-dialog listener.

        Tests arm a one-shot policy instead of registering another listener.
        Unsolicited ownership/revocation alerts are accepted by default so a
        late alert cannot suspend every subsequent Playwright operation.
        """
        key = id(page)
        if key in self.dialog_brokers:
            return
        state = {"label": label, "policies": [], "records": []}

        def handle(dialog):
            policy = next((row for row in state["policies"]
                           if not row.get("expected")
                           or row["expected"] in dialog.message), None)
            if policy is not None:
                state["policies"].remove(policy)
            record = {"page": label, "type": dialog.type,
                      "message": dialog.message,
                      "response": (policy or {}).get("response", "accept")}
            state["records"].append(record)
            if policy is not None:
                policy["records"].append(record)
                policy["used"] = True
            try:
                if record["response"] == "dismiss":
                    dialog.dismiss()
                else:
                    dialog.accept()
            except Exception as error:
                # Navigation, disconnect and ownership revocation can close a
                # dialog between its event and the CDP acknowledgement.  It is
                # evidence, but must not escape the callback and kill the
                # Playwright driver.
                record["error"] = f"{type(error).__name__}: {error}"
                self.event("dialog_handle_error", label=label,
                           dialog=record)

        state["handler"] = handle
        self.dialog_brokers[key] = state
        page.on("dialog", handle)

    def arm_dialog(self, page: Page, response: str = "accept", *,
                   expected: str = "") -> dict:
        if response not in {"accept", "dismiss"}:
            raise ValueError(f"未知 dialog 响应：{response}")
        state = self.dialog_brokers.get(id(page))
        if state is None:
            raise RuntimeError("页面尚未安装 dialog broker")
        policy = {"response": response, "expected": expected,
                  "records": [], "used": False}
        state["policies"].append(policy)
        return policy

    def disarm_dialog(self, page: Page, policy: dict) -> list[dict]:
        state = self.dialog_brokers.get(id(page))
        if state is not None and policy in state["policies"]:
            state["policies"].remove(policy)
        return list(policy["records"])

    def dialog_checkpoint(self, page: Page) -> int:
        state = self.dialog_brokers.get(id(page))
        return len(state["records"]) if state else 0

    def dialogs_since(self, page: Page, checkpoint: int) -> list[dict]:
        state = self.dialog_brokers.get(id(page))
        return list(state["records"][checkpoint:]) if state else []

    def recover_browser(self, page: Page, label: str):
        """Return a disturbed page to the debug list without hiding failure."""
        page.context.set_offline(False)
        if label == "desktop":
            page.set_viewport_size({"width": 1280, "height": 720})
        else:
            page.set_viewport_size({"width": 390, "height": 760})
        debug_url = self.base + "/?" + urllib.parse.urlencode(
            {"debug_run": self.run_id})
        page.goto(debug_url, wait_until="domcontentloaded")
        page.wait_for_function("S.sessions.length === 20", timeout=60000)
        self.install_dom_monitor(page, label)

    def browser_presence(self, page: Page, marker: str) -> dict:
        return page.evaluate("""marker => {
          const has = node => (node.innerText || '').includes(marker);
          const user = [...document.querySelectorAll(
            '#msgs .msg[data-role="user"]:not(.client-pending), #msgs .msg[data-role="command"]')].filter(has);
          const pending = [...document.querySelectorAll('#msgs .client-pending')].filter(has);
          const assistant = [...document.querySelectorAll(
            '#msgs .msg[data-role="assistant"]')].filter(has);
          return {user:user.length, pending:pending.length, assistant:assistant.length,
            activity:document.querySelector('#activity')?.dataset.state || ''};
        }""", marker)

    def browser_model_snapshot(self, page: Page, session: Session) -> dict:
        """Read this tab's cache and optimistic ledger without changing routes."""
        return page.evaluate("""uid => {
          const entry=cache.get(viewKey(uid));
          const messages=Array.isArray(entry?.msgs) ? entry.msgs : [];
          const pending=typeof queuedMessages === 'function' ? queuedMessages(uid) : [];
          return {loaded:!!entry, selected:String(S.sel||''),
            users:messages.filter(m=>['user','command'].includes(m.role))
              .map(m=>String(m.text||'')),
            assistants:messages.filter(m=>m.role==='assistant')
              .map(m=>String(m.text||'')),
            pending:pending.map(m=>({text:String(m.text||''),
              state:String(m.state||''),id:String(m.id||'')})),
            dom:[...document.querySelectorAll(
              '#msgs .msg, #msgs .client-outbox, #msgs .timeline-event')]
              .map(node=>String(node.innerText||'')),
            activity:String(entry?.activity?.state||'')};
        }""", session.uid)

    def audit_consistency(self, page: Page, session: Session,
                          label: str) -> bool:
        """Compare physical tmux, this browser tab and the server ledger."""
        if session.frozen or not session.oracle:
            return not session.frozen
        try:
            terminal = self.probe_terminal(session)
            browser = self.browser_model_snapshot(page, session)
            if browser["selected"] != session.uid:
                leaked = [marker for marker in session.tracked
                          if any(terminal_has(text, marker)
                                 for text in browser["dom"])]
                if leaked:
                    self.fail("其他会话的消息泄漏到当前对话 DOM", session,
                              page=label, selected=browser["selected"],
                              markers=leaked[:10])
                    return False
            visible_order = [
                marker for marker, tracked in sorted(
                    session.tracked.items(), key=lambda item: item[1]["created"])
                if any(terminal_has(text, marker) for text in browser["users"])
            ]
            observed_order = []
            for text in browser["users"]:
                observed_order.extend(marker for marker in session.tracked
                                      if terminal_has(text, marker))
            # Every marker is unique.  Compare only visible tracked messages so
            # an intentionally cancelled fast-Esc turn does not create a gap.
            if observed_order != visible_order:
                self.fail("浏览器用户消息顺序与提交顺序不一致", session,
                          page=label, expected=visible_order,
                          observed=observed_order)
                return False
            query = urllib.parse.urlencode({"uid": session.uid})
            server = self.get(f"/api/session/outbox?{query}")
            observation = LayerObservation(
                now=time.time(), page_id=label, loaded=browser["loaded"],
                selected_uid=browser["selected"],
                terminal_phase=terminal["phase"],
                terminal_history=terminal["history"],
                terminal_current=terminal["current"],
                browser_users=tuple(browser["users"]),
                browser_assistants=tuple(browser["assistants"]),
                browser_pending=tuple(PendingView(
                    row["text"], row["state"], row["id"])
                    for row in browser["pending"]),
                browser_activity=browser["activity"],
                server_outbox=tuple(PendingView(
                    str(row.get("text") or ""), str(row.get("state") or ""),
                    str(row.get("id") or ""))
                    for row in server.get("outbox", [])),
            )
            issues = session.oracle.observe(observation)
            self.event("oracle_observation", session, page=label,
                       phase=terminal["phase"], loaded=browser["loaded"],
                       selected=browser["selected"],
                       pending=len(browser["pending"]),
                       outbox=len(server.get("outbox", [])),
                       issues=[row.code for row in issues])
            if issues:
                self.fail("跨层状态不变量失败", session, page=label,
                          issues=[{"code": row.code, "message": row.message,
                                   "logical_id": row.logical_id,
                                   "details": row.details} for row in issues])
                return False
            return True
        except Exception as error:
            self.fail("跨层状态巡检异常", session, page=label,
                      error=f"{type(error).__name__}: {error}")
            return False

    def wait_browser_marker(self, page: Page, session: Session, marker: str,
                            role: str, timeout: float = 90) -> bool:
        self.open_session(page, session)
        deadline = time.monotonic() + timeout
        duplicate_since = None
        while time.monotonic() < deadline:
            presence = self.browser_presence(page, marker)
            count = presence[role]
            if role == "user":
                count += presence["pending"]
            if count == 1:
                self.event("browser_marker_seen", session, marker=marker,
                           role=role, presence=presence)
                return True
            if count > 1:
                duplicate_since = duplicate_since or time.monotonic()
                if time.monotonic() - duplicate_since > 0.5:
                    self.fail("对话页同一 tmux 消息显示多次", session,
                              marker=marker, role=role, presence=presence)
                    return False
            else:
                duplicate_since = None
            time.sleep(0.08)
        self.fail("tmux 已出现但对话页未同步", session,
                  marker=marker, role=role, timeout=timeout)
        return False

    def wait_browser_exchange(self, page: Page, session: Session,
                              exchange: dict, timeout: float = 120) -> bool:
        if not self.wait_browser_marker(
                page, session, exchange["request"], "user", timeout):
            return False
        return self.wait_browser_marker(
            page, session, exchange["reply"], "assistant", timeout)

    def wait_web_exchange(self, page: Page, session: Session, exchange: dict,
                          *, wait_reply: bool, watch: tuple[dict, ...] = (),
                          timeout: float = 180) -> bool:
        deadline = time.monotonic() + timeout
        prompt_seen = False
        reply_seen = False
        ui_seen = False
        missing_since = None
        duplicate_since = None
        tracked = (exchange, *watch)
        while time.monotonic() < deadline:
            if session.frozen:
                return False
            snapshot = self.probe_terminal(session)
            presence = self.browser_presence(page, exchange["request"])
            visible = presence["user"] + presence["pending"]
            if visible:
                ui_seen = True
                missing_since = None
            else:
                missing_since = missing_since or time.monotonic()
                grace = 1.0 if not ui_seen else 0.4
                if time.monotonic() - missing_since > grace:
                    self.fail("对话发送后用户消息从页面消失", session,
                              marker=exchange["request"], presence=presence,
                              terminal_prompt=prompt_seen)
                    return False
            if visible > 1:
                duplicate_since = duplicate_since or time.monotonic()
                if time.monotonic() - duplicate_since > 0.5:
                    self.fail("对话发送形成重复气泡", session,
                              marker=exchange["request"], presence=presence)
                    return False
            else:
                duplicate_since = None

            for previous in tracked[1:]:
                old = self.browser_presence(page, previous["request"])
                if old["user"] + old["pending"] != 1:
                    self.fail("等待后续消息时前一条气泡消失或重复", session,
                              marker=previous["request"], presence=old)
                    return False

            if (not prompt_seen
                    and terminal_has(snapshot["history"], exchange["request"])):
                prompt_seen = True
                self.event("terminal_prompt_seen", session,
                           marker=exchange["request"], origin="browser",
                           history_hash=snapshot["history_hash"],
                           screen=snapshot["history"][-5000:])
            if prompt_seen and not wait_reply and visible == 1:
                return True
            if (not reply_seen
                    and terminal_has(snapshot["history"], exchange["reply"])):
                reply_seen = True
                session.last_reply = exchange["reply"]
                self.event("terminal_reply_seen", session,
                           marker=exchange["reply"], origin="browser",
                           history_hash=snapshot["history_hash"],
                           screen=snapshot["history"][-5000:])
            if reply_seen:
                answer = self.browser_presence(page, exchange["reply"])
                if answer["assistant"] == 1 and visible == 1:
                    tracked_row = session.tracked.get(exchange["request"])
                    if tracked_row is not None:
                        tracked_row["ended"] = time.time()
                    self.event("browser_exchange_complete", session,
                               request=exchange["request"], reply=exchange["reply"])
                    return True
            time.sleep(0.08)
        self.fail("网页与 tmux 往返未在时限内完成", session,
                  request=exchange["request"], reply=exchange["reply"],
                  terminal_prompt=prompt_seen, terminal_reply=reply_seen,
                  wait_reply=wait_reply, timeout=timeout)
        return False

    def start_ui_send(self, page: Page, session: Session, exchange: dict,
                      kind: str) -> dict | None:
        paid = max(session.turns,
                   session.schedule.paid_turns if session.schedule else 0)
        if session.frozen or paid >= self.max_paid_turns:
            return None
        self.ensure_composer(page, session)
        page.locator("#cinput").fill(exchange["text"])
        sent_at = time.time()
        with page.expect_response(
                lambda response: "/api/session/send" in response.url
                and response.request.method == "POST", timeout=60000) as pending_response:
            page.locator("#csend").click()
        response = pending_response.value
        try:
            result = response.json()
        except Exception:
            result = {"status": response.status}
        if not isinstance(result, dict):
            result = {"status": response.status, "response": result}
        session.turns += 1
        item = result.get("item")
        server_id = str(item.get("id") or "") if isinstance(item, dict) else ""
        self.track_exchange(session, exchange, kind, "browser",
                            created=sent_at, server_id=server_id)
        self.event("ui_sent", session, text=exchange["text"],
                   request=exchange["request"], reply=exchange["reply"],
                   response=result)
        if response.status >= 400 or result.get("error") or result.get("draft_conflict"):
            self.fail("网页发送未获成功响应", session,
                      request=exchange["request"], response=result)
            return None
        return result

    def send_ui(self, page: Page, session: Session, exchange: dict, kind="ui",
                *, wait_reply=True, watch: tuple[dict, ...] = ()) -> bool:
        if self.start_ui_send(page, session, exchange, kind) is None:
            return False
        return self.wait_web_exchange(page, session, exchange,
                                      wait_reply=wait_reply, watch=watch)

    def install_dom_monitor(self, page: Page, label: str):
        page.evaluate("""label => {
          if(window.__monkeyTimer) clearInterval(window.__monkeyTimer);
          window.__monkeyDom=[]; window.__monkeyLast='';
          window.__monkeyTimer=setInterval(()=>{
            const state=JSON.stringify({uid:S.sel,
              pending:[...document.querySelectorAll('.client-pending')].map(x=>x.innerText),
              users:[...document.querySelectorAll('.msg[data-role="user"]:not(.client-pending),.msg[data-role="command"]')].slice(-6).map(x=>x.innerText),
              assistants:[...document.querySelectorAll('.msg[data-role="assistant"]')].slice(-6).map(x=>x.innerText),
              activity:document.querySelector('#activity')?.dataset.state||''});
            if(state!==window.__monkeyLast){
              window.__monkeyLast=state;
              window.__monkeyDom.push({at:Date.now(),label,state:JSON.parse(state)});
              if(window.__monkeyDom.length>5000) window.__monkeyDom.shift();
            }
          },100);
        }""", label)

    def drain_dom(self, page: Page, label: str):
        rows = page.evaluate("""() => {const x=window.__monkeyDom||[];
          window.__monkeyDom=[]; return x;}""")
        for row in rows:
            self.event("dom_state", **row)

    def busy_exchange(self, session: Session, kind: str, serial: str | int) -> dict:
        exchange = self.exchange(session, kind, serial)
        request_label = exchange["reply"].removesuffix("-rsp") + "-req"
        exchange["text"] = (
            f"For local sesman terminal rendering test {exchange['request']}, print "
            "integers 1 through 80 one per line, then finish with a line containing "
            f"the test label {request_label} after changing its final -req to -rsp.")
        return exchange

    def wait_terminal_busy(self, session: Session, request: str,
                           timeout: float = 15) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.probe_terminal(session)
            if snapshot["busy"]:
                self.event("terminal_busy_seen", session, marker=request,
                           composer=snapshot["composer"],
                           current_hash=snapshot["current_hash"],
                           screen=snapshot["current"][-4000:])
                return snapshot
            time.sleep(0.05)
        self.fail("tmux pane 未呈现忙碌状态", session, marker=request,
                  timeout=timeout)
        return None

    def wait_browser_activity(self, page: Page, session: Session, state: str,
                              timeout: float = 15) -> bool:
        self.open_session(page, session)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            shown = page.locator(f'#activity[data-state="{state}"]').count()
            if shown:
                self.event("browser_activity_seen", session, state=state)
                return True
            time.sleep(0.08)
        self.fail("tmux 状态未同步到对话页", session, state=state,
                  timeout=timeout)
        return False

    def queue_case(self, page: Page, session: Session, *,
                   start_origin: str, queue_origin: str, serial: int):
        if session.frozen:
            return
        route = f"{start_origin.upper()}-TO-{queue_origin.upper()}"
        first = self.busy_exchange(session, f"BUSY-{route}", serial)
        second = self.exchange(session, f"QUEUE-{route}", serial)
        if start_origin == "browser":
            if not self.send_ui(page, session, first, "browser_busy", wait_reply=False):
                return
        else:
            if not self.submit_native(session, first, "tmux_busy", wait_reply=False):
                return
            if not self.wait_browser_marker(
                    page, session, first["request"], "user", timeout=30):
                return
        if not self.wait_terminal_busy(session, first["request"]):
            return
        if not self.wait_browser_activity(page, session, "working"):
            return

        if queue_origin == "browser":
            if not self.send_ui(page, session, second, "browser_cross_queued",
                                wait_reply=True, watch=(first,)):
                return
        else:
            if not self.submit_native(
                    session, second, "tmux_cross_queued", wait_reply=False):
                return
            if not self.wait_terminal_marker(
                    session, second["reply"], "terminal_reply_seen", timeout=180):
                return
            session.last_reply = second["reply"]
            if not self.wait_browser_exchange(page, session, second, timeout=120):
                return

        if not self.wait_terminal_marker(
                session, first["reply"], "terminal_reply_seen", timeout=180):
            return
        session.last_reply = second["reply"]
        self.wait_browser_marker(page, session, first["reply"], "assistant", timeout=120)
        first_track = session.tracked.get(first["request"])
        if first_track is not None:
            first_track["ended"] = time.time()
        session.ops.add(f"queue_{start_origin}_to_{queue_origin}")

    def wait_terminal_settled(self, session: Session, timeout: float = 30) -> dict | None:
        deadline = time.monotonic() + timeout
        stable = None
        stable_at = 0.0
        while time.monotonic() < deadline:
            snapshot = self.probe_terminal(session)
            state = (snapshot["busy"], snapshot["composer"], snapshot["current_hash"])
            if not snapshot["busy"] and snapshot["composer"] in {"empty", "editing"}:
                if state != stable:
                    stable, stable_at = state, time.monotonic()
                elif time.monotonic() - stable_at >= 0.25:
                    return snapshot
            else:
                stable = None
            time.sleep(0.05)
        self.fail("中断后 tmux 未回到稳定编辑器", session, timeout=timeout)
        return None

    def wait_browser_not_working(self, page: Page, session: Session,
                                 timeout: float = 20) -> bool:
        self.open_session(page, session)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not page.locator('#activity[data-state="working"]').count():
                return True
            time.sleep(0.08)
        self.fail("tmux 已停止但对话页仍显示 Working", session, timeout=timeout)
        return False

    def interrupt_case(self, page: Page, session: Session, *,
                       start_origin: str, interrupt_origin: str,
                       delay: float, serial: int):
        if session.frozen:
            return
        exchange = self.busy_exchange(session, "ESC", serial)
        if start_origin == "browser":
            if not self.send_ui(page, session, exchange, "escape_start",
                                wait_reply=False):
                return
        else:
            if not self.submit_native(session, exchange, "escape_start",
                                      wait_reply=False):
                return
            if not self.wait_browser_marker(
                    page, session, exchange["request"], "user", timeout=30):
                return
        if not self.wait_terminal_busy(session, exchange["request"]):
            return
        self.wait_browser_activity(page, session, "working")
        time.sleep(delay)
        tracked = session.tracked.get(exchange["request"])
        if tracked is not None:
            tracked["ended"] = time.time()
        if interrupt_origin == "browser":
            self.open_session(page, session)
            page.locator("#cesc").click()
        else:
            term.send_keys(session.name, "Escape")
        settled = self.wait_terminal_settled(session)
        if not settled:
            return
        self.wait_browser_not_working(page, session)
        deadline = time.monotonic() + 20
        presence = self.browser_presence(page, exchange["request"])
        while time.monotonic() < deadline and presence["pending"]:
            time.sleep(0.1)
            presence = self.browser_presence(page, exchange["request"])
        if presence["pending"] or presence["user"] > 1:
            self.fail("tmux 中断后对话页仍有假排队或重复消息", session,
                      request=exchange["request"], presence=presence,
                      composer=settled["composer"])
            return
        if not presence["user"] and session.oracle:
            session.oracle.settle(exchange["request"], "cancelled")
        self.event("escape_result", session, delay=delay,
                   start_origin=start_origin, interrupt_origin=interrupt_origin,
                   composer=settled["composer"], cursor=settled["cursor"],
                   request_in_current=terminal_has(
                       settled["current"], exchange["request"]),
                   screen=settled["current"][-5000:])
        if settled["composer"] == "editing":
            session.ops.add("escape_restored_draft")
            term.send_keys(session.name, "C-c")
            self.wait_terminal_settled(session)
        session.ops.add(f"interrupt_{start_origin}_to_{interrupt_origin}")

    def wait_composer(self, session: Session, state: str,
                      timeout: float = 10) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.probe_terminal(session)
            if snapshot["composer"] == state:
                return snapshot
            time.sleep(0.05)
        self.fail("tmux 编辑器状态不符合预期", session,
                  expected=state, timeout=timeout)
        return None

    def cross_draft_case(self, page: Page, session: Session, *,
                         accept: bool, serial: int):
        if session.frozen:
            return
        if not self.wait_composer(session, "empty", timeout=20):
            return
        draft_marker = f"TMUX-DRAFT-{self.run_id}-{session.source}-{serial}"
        term.send_text(session.name, draft_marker)
        # TUI repaint can expose ``composer=editing`` one frame before the
        # typed cells appear in capture-pane.  Require both observations in
        # the same snapshot instead of failing on that intermediate frame.
        draft = None
        last = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            last = self.probe_terminal(session)
            if (last["composer"] == "editing"
                    and terminal_has(last["current"], draft_marker)):
                draft = last
                break
            time.sleep(0.05)
        if draft is None:
            self.fail("tmux 半成品草稿未保持在编辑器", session,
                      marker=draft_marker,
                      composer=(last or {}).get("composer", "missing"))
            return

        exchange = self.exchange(session, "OVERWRITE", serial)
        self.ensure_composer(page, session)
        page.locator("#cinput").fill(exchange["text"])
        policy = self.arm_dialog(
            page, "accept" if accept else "dismiss",
            expected="终端草稿中有内容，是否覆盖？")
        try:
            if accept:
                with page.expect_response(
                        lambda response: "/api/session/send" in response.url
                        and response.request.method == "POST", timeout=60000) as pending:
                    page.locator("#csend").click()
                response = pending.value
                result = response.json()
                session.turns += 1
                self.track_exchange(
                    session, exchange, "draft_overwrite", "browser",
                    server_id=str((result.get("item") or {}).get("id") or ""))
                if response.status >= 400 or result.get("error") or result.get("draft_conflict"):
                    self.fail("确认覆盖终端草稿后发送失败", session, response=result)
                    return
                if not self.wait_web_exchange(page, session, exchange, wait_reply=True):
                    return
            else:
                page.locator("#csend").click()
                page.wait_for_function("!composerSending", timeout=15000)
        finally:
            records = self.disarm_dialog(page, policy)
        dialog_text = [row["message"] for row in records]

        if dialog_text != ["终端草稿中有内容，是否覆盖？"]:
            self.fail("终端草稿覆盖提示不正确", session,
                      accept=accept, dialogs=dialog_text)
            return
        after = self.probe_terminal(session)
        if accept:
            if terminal_has(after["current"], draft_marker):
                self.fail("确认覆盖后旧 tmux 草稿仍在编辑器", session,
                          marker=draft_marker)
                return
        else:
            if (not terminal_has(after["current"], draft_marker)
                    or after["composer"] != "editing"):
                self.fail("拒绝覆盖却改动了 tmux 草稿", session,
                          marker=draft_marker, composer=after["composer"])
                return
            presence = self.browser_presence(page, exchange["request"])
            if presence["user"] + presence["pending"] + presence["assistant"] != 0:
                self.fail("拒绝覆盖后仍产生了对话消息", session,
                          marker=exchange["request"], presence=presence)
                return
            page.locator("#cinput").fill("")
            term.send_keys(session.name, "C-c")
            self.wait_composer(session, "empty")
        self.event("cross_draft_verified", session, accept=accept,
                   draft_marker=draft_marker, request=exchange["request"])
        session.ops.add(f"cross_draft_{'accept' if accept else 'dismiss'}")

    @staticmethod
    def question_options_visible(screen: str) -> bool:
        alpha = re.search(r"(?mi)^\s*(?:[›»❯>]\s*)?1[.)]\s+Alpha\b", screen)
        beta = re.search(r"(?mi)^\s*(?:[›»❯>]\s*)?2[.)]\s+Beta\b", screen)
        return bool(alpha and beta)

    def wait_terminal_question(self, session: Session,
                               timeout: float = 60) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.probe_terminal(session)
            if self.question_options_visible(snapshot["current"]):
                self.event("terminal_question_seen", session,
                           current_hash=snapshot["current_hash"],
                           screen=snapshot["current"][-5000:])
                return snapshot
            time.sleep(0.08)
        self.fail("tmux pane 未呈现选择题", session, timeout=timeout)
        return None

    def answer_question(self, page: Page, session: Session, *, origin: str):
        """Answer in one surface and prove completion from the tmux pane."""
        self.open_session(page, session)
        options = page.locator(".live-question [data-question-option]")
        try:
            options.first.wait_for(state="visible", timeout=60000)
        except PlaywrightTimeoutError:
            self.fail("选择题未在网页呈现", session)
            return
        options.first.hover()
        time.sleep(0.25)
        if not options.count() or not options.first.is_visible():
            self.fail("选择题悬停后消失", session)
            return
        if origin == "browser":
            options.first.click()
            # Claude's form selects first, then explicitly submits.  Codex's
            # single question submits directly on selection.
            submit = page.locator(".live-question .question-submit")
            if submit.count():
                submit.wait_for(state="visible", timeout=10000)
                if submit.is_disabled():
                    self.fail("选择题选项未启用提交按钮", session)
                    return
                submit.click()
        else:
            term.send_keys(session.name, "Enter")
        try:
            page.locator(".live-question").wait_for(state="detached", timeout=60000)
        except PlaywrightTimeoutError:
            self.fail("选择题提交后未收敛", session)
            return
        self.question_sources.add(session.source)
        self.event("question_answered", session, origin=origin)

    def question_case(self, page: Page, session: Session, *,
                      start_origin: str, answer_origin: str, serial: int):
        if session.frozen:
            return
        exchange = self.exchange(session, "QUESTION", serial)
        tool = "AskUserQuestion" if session.source == "claude" else "request_user_input"
        request_label = exchange["reply"].removesuffix("-rsp") + "-req"
        exchange["text"] = (
            f"For local sesman question UI test {exchange['request']}, use {tool} now "
            "to ask one question with exactly two options labelled Alpha and Beta. "
            "After the answer, reply in one brief sentence that includes the test label "
            f"{request_label} after changing its final -req to -rsp.")
        if start_origin == "browser":
            if not self.send_ui(page, session, exchange, "question_start",
                                wait_reply=False):
                return
        else:
            if not self.submit_native(session, exchange, "question_start",
                                      wait_reply=False):
                return
            if not self.wait_browser_marker(
                    page, session, exchange["request"], "user", timeout=30):
                return
        if not self.wait_terminal_question(session):
            return
        self.answer_question(page, session, origin=answer_origin)
        if session.frozen:
            return
        if not self.wait_terminal_marker(
                session, exchange["reply"], "terminal_reply_seen", timeout=180):
            return
        session.last_reply = exchange["reply"]
        if not self.wait_browser_exchange(page, session, exchange, timeout=120):
            return
        tracked = session.tracked.get(exchange["request"])
        if tracked is not None:
            tracked["ended"] = time.time()
        session.ops.add(f"question_{start_origin}_to_{answer_origin}")

    def browser_tail(self, page: Page) -> list[dict]:
        return page.evaluate("""() => [...document.querySelectorAll(
          '#msgs .msg, #msgs .timeline-event')]
          .slice(-30).map(node => ({role:node.dataset.role || '',
            text:(node.innerText || '').trim()}))""")

    def slash_help_interleave(self, page: Page, other: Page, session: Session,
                              serial: int):
        if session.frozen:
            return
        self.open_session(page, session)
        before = self.browser_tail(page)
        initial = self.probe_terminal(session)
        partial_text = "/he" if session.source == "claude" else "/mod"
        remainder = "lp" if session.source == "claude" else "el"
        command = partial_text + remainder
        partial = self.type_terminal(
            session, partial_text, partial_text,
            "slash_help_partial", submit=False)
        if not partial:
            self.fail("tmux 斜杠命令的半成品未留在编辑器", session,
                      composer=partial["composer"] if partial else "missing")
            return

        def menu_changed():
            snapshot = self.probe_terminal(session)
            return snapshot if (snapshot["current_hash"] != initial["current_hash"]
                                and terminal_has(snapshot["current"], partial_text)) else None

        changed = wait_for(
            f"{session.source} /help 菜单", menu_changed,
            timeout=15, interval=0.08)
        self.open_session(other, session)
        other.set_viewport_size({"width": 390, "height": 720})
        cross = self.exchange(session, "SLASH-CROSS", serial)
        if session.source == "claude":
            self.ensure_composer(other, session)
            other.locator("#cinput").fill(cross["text"])
            policy = self.arm_dialog(
                other, "dismiss", expected="终端草稿中有内容，是否覆盖？")
            try:
                other.locator("#csend").click()
                other.wait_for_function("!composerSending", timeout=15000)
            finally:
                records = self.disarm_dialog(other, policy)
            dialogs = [row["message"] for row in records]
            if dialogs != ["终端草稿中有内容，是否覆盖？"]:
                self.fail("Claude 斜杠草稿未阻止网页覆盖", session,
                          dialogs=dialogs)
                return
        elif self.start_ui_send(
                other, session, cross, "slash_cross_mode") is None:
            return
        time.sleep(0.35)
        after_switch = self.probe_terminal(session)
        if (not term.has_session(session.name)
                or not terminal_has(after_switch["current"], partial_text)
                or terminal_has(after_switch["history"], cross["request"])):
            self.fail("斜杠菜单未完成时网页操作破坏了终端状态", session)
            return
        if not self.type_terminal(
                session, remainder, command, "slash_help_complete", submit=True):
            return
        time.sleep(0.3)
        term.send_keys(session.name, "Escape")
        self.wait_terminal_settled(session)
        if session.source == "claude":
            if not self.send_ui(
                    other, session, cross, "slash_cross_after_dismiss"):
                return
        elif not self.wait_web_exchange(
                other, session, cross, wait_reply=True):
            return
        self.open_session(page, session)
        after = self.browser_tail(page)
        if any(not row["text"] for row in after):
            self.fail("未完成的斜杠菜单产生空白气泡", session,
                      before=before, after=after)
            return
        raw_protocol = [row for row in after
                        if "<command-" in row["text"]
                        or "<local-command" in row["text"]]
        if raw_protocol:
            self.fail("斜杠命令协议 XML 泄漏到对话页", session,
                      rows=raw_protocol)
            return
        if session.source == "claude":
            help_rows = [row for row in after
                         if row["role"] == "command" and row["text"] == "/help"]
            if len(help_rows) != 1:
                self.fail("Claude tmux /help 未同步为一条语义命令", session,
                          rows=help_rows, after=after)
                return
        final = self.wait_terminal_settled(session)
        if not final:
            return
        presence = self.browser_presence(page, cross["request"])
        reply = self.browser_presence(page, cross["reply"])
        if (final["composer"] != "empty" or final["busy"]
                or presence["user"] != 1 or presence["pending"] != 0
                or reply["assistant"] != 1):
            self.fail("tmux/对话交错操作后的终态不一致", session,
                      composer=final["composer"], busy=final["busy"],
                      request_presence=presence, reply_presence=reply)
            return
        self.event("slash_help_interleave", session,
                   before_count=len(before), after_count=len(after),
                   menu_hash=changed["current_hash"], command=command,
                   cross_request=cross["request"],
                   final_composer=final["composer"], final_busy=final["busy"])
        session.ops.add("slash_help_interleave")

    def slash_rename(self, page: Page, session: Session, serial: int):
        if session.frozen:
            return
        title = (f"monkey-{self.run_id}-{session.source}-"
                 f"{session.index}-{serial}")
        command = f"/rename {title}"
        if not self.type_terminal(
                session, command, command, "slash_rename", submit=True):
            return
        self.wait_terminal_settled(session, timeout=30)
        self.open_session(page, session)
        try:
            page.wait_for_function(
                "([uid,title]) => S.sessions.some(s => s.uid === uid && s.title === title)",
                arg=[session.uid, title], timeout=30000)
        except PlaywrightTimeoutError:
            self.fail("tmux /rename 后会话标题未同步", session, title=title)
            return
        if not self.wait_browser_marker(page, session, command, "user", timeout=30):
            return
        self.event("slash_rename_synced", session, title=title)
        session.ops.add("slash_rename")

    def slash_compact(self, page: Page, session: Session):
        if session.frozen:
            return
        self.open_session(page, session)
        before = self.browser_tail(page)
        initial = self.probe_terminal(session)
        if not self.type_terminal(
                session, "/compact", "/compact", "slash_compact", submit=True):
            return
        submitted_at = time.monotonic()
        deadline = time.monotonic() + 180
        changed = False
        settled = None
        while time.monotonic() < deadline:
            snapshot = self.probe_terminal(session)
            changed = changed or snapshot["current_hash"] != initial["current_hash"]
            if (changed and time.monotonic() - submitted_at >= 1.0
                    and not snapshot["busy"] and snapshot["composer"] == "empty"):
                time.sleep(0.25)
                confirm = self.probe_terminal(session)
                if confirm["current_hash"] == snapshot["current_hash"]:
                    settled = confirm
                    break
            time.sleep(0.1)
        if not settled:
            self.fail("tmux /compact 未完成", session, changed=changed)
            return
        self.open_session(page, session)
        deadline = time.monotonic() + 60
        after = self.browser_tail(page)
        while time.monotonic() < deadline:
            after = self.browser_tail(page)
            if any(row["role"] == "event"
                   and ("已压缩" in row["text"]
                        or "compact" in row["text"].casefold())
                   for row in after):
                break
            time.sleep(0.15)
        before_rows = {(row["role"], row["text"]) for row in before}
        added = [row for row in after if (row["role"], row["text"]) not in before_rows]
        if any(not row["text"] for row in after):
            self.fail("tmux /compact 后出现空白气泡", session, added=added)
            return
        if not any(row["role"] == "event"
                   and ("上下文" in row["text"] or "compact" in row["text"].casefold())
                   for row in after):
            self.fail("tmux /compact 完成但对话页没有压缩事件", session,
                      added=added)
            return
        if len(added) > 3:
            self.fail("tmux /compact 后出现过多附加气泡", session, added=added)
            return
        self.event("slash_compact_synced", session, added=added,
                   terminal_hash=settled["current_hash"])
        session.ops.add("slash_compact")

    def qualify_sessions(self, page: Page):
        """Prove both directions once before random state transitions begin."""
        for session in self.sessions:
            if session.frozen:
                continue
            bootstrap = next(iter(session.tracked.values()))
            if self.wait_browser_exchange(page, session, bootstrap, timeout=90):
                session.ops.add("bootstrap_browser_sync")
            self.audit_consistency(page, session, "desktop")
        for session in self.sessions:
            if session.frozen:
                continue
            exchange = self.exchange(session, "QUALIFY-WEB", 1)
            if self.send_ui(page, session, exchange, "browser_to_tmux"):
                session.ops.add("browser_to_tmux")
            if session.schedule:
                session.schedule.paid_turns = session.turns
            self.audit_consistency(page, session, "desktop")

    def simple_roundtrip(self, page: Page, session: Session, *, origin: str,
                         variant: str, serial: int):
        exchange = self.exchange(session, variant.upper(), serial)
        if variant == "browser_trim_roundtrip":
            # Real CLIs trim only the outer whitespace.  This exact shape caught
            # the formal-message + duplicate-pending regression in production.
            exchange["text"] = " \n" + exchange["text"] + "\n\n"
        if origin == "browser":
            ok = self.send_ui(page, session, exchange, variant)
        else:
            ok = bool(self.submit_native(session, exchange, variant))
            if ok:
                ok = self.wait_browser_exchange(page, session, exchange, timeout=120)
        if ok:
            session.ops.add(variant)

    def lost_response_retry_case(self, page: Page, session: Session,
                                 serial: int):
        """Let the server accept once, drop its response, then retry the same ID."""
        exchange = self.exchange(session, "LOST-RESPONSE", serial)
        self.ensure_composer(page, session)
        page.locator("#cinput").fill(exchange["text"])
        # Debug runs append ?debug_run=... to every API URL.  A glob ending in
        # ``send`` silently misses that request and turns a successful send
        # into a false harness failure, so match both plain and query URLs.
        pattern = re.compile(r"/api/session/send(?:\?|$)")
        upstream: list[dict] = []

        def accept_then_drop(route):
            response = route.fetch()
            try:
                upstream.append({"status": response.status,
                                 "body": response.json()})
            except Exception:
                upstream.append({"status": response.status})
            route.abort("failed")

        page.route(pattern, accept_then_drop, times=1)
        policy = self.arm_dialog(page, "accept", expected="发送失败")
        try:
            page.locator("#csend").click()
            page.wait_for_function("!composerSending", timeout=60000)
        finally:
            page.unroute(pattern, accept_then_drop)
            records = self.disarm_dialog(page, policy)
        dialogs = [row["message"] for row in records]
        if not upstream or upstream[0].get("status", 500) >= 400:
            self.fail("丢响应测试的上游请求没有成功入账", session,
                      upstream=upstream, dialogs=dialogs)
            return
        if page.locator("#cinput").input_value() != exchange["text"]:
            self.fail("HTTP 响应丢失后编辑框草稿被清空", session,
                      dialogs=dialogs)
            return

        with page.expect_response(
                lambda response: "/api/session/send" in response.url
                and response.request.method == "POST", timeout=60000) as retried:
            page.locator("#csend").click()
        response = retried.value
        result = response.json()
        if response.status >= 400 or result.get("error"):
            self.fail("同一 request_id 重试失败", session, response=result)
            return
        session.turns += 1
        server_id = str((result.get("item") or {}).get("id") or "")
        self.track_exchange(session, exchange, "lost_response_idempotent_retry",
                            "browser", server_id=server_id)
        if not server_id or server_id != str(
                ((upstream[0].get("body") or {}).get("item") or {}).get("id") or ""):
            self.fail("丢响应后的重试没有复用同一幂等 ID", session,
                      upstream=upstream, retry=result)
            return
        if self.wait_web_exchange(page, session, exchange, wait_reply=True):
            session.ops.add("lost_response_idempotent_retry")
            self.event("lost_response_retry_verified", session,
                       request=exchange["request"], server_id=server_id,
                       dialogs=dialogs)

    def busy_disturbance_case(self, page: Page, mobile: Page, session: Session,
                              disturbance: str, serial: int):
        """Mutate browser/terminal lifecycle while a real turn is visibly live."""
        exchange = self.busy_exchange(session, disturbance.upper(), serial)
        if self.start_ui_send(page, session, exchange, disturbance) is None:
            return
        if not self.wait_terminal_busy(session, exchange["request"]):
            return
        self.wait_browser_activity(page, session, "working")
        original_size = page.viewport_size or {"width": 1280, "height": 720}
        debug_url = self.base + "/?" + urllib.parse.urlencode(
            {"debug_run": self.run_id})
        try:
            if disturbance == "resize_while_busy":
                self.open_session(page, session)
                if page.locator("#a-term").count():
                    page.locator("#a-term").click()
                # Include vertical-only and horizontal-only changes; the old
                # soak almost exclusively resized an idle chat viewport.
                for width, height in ((390, 760), (390, 430), (820, 430),
                                      (1280, 430), (1280, 760)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(90)
                self.ensure_composer(page, session)
            elif disturbance == "switch_while_busy":
                other = next((row for row in self.sessions
                              if row is not session and not row.frozen), None)
                if other:
                    self.open_session(page, other)
                    page.wait_for_timeout(180)
                self.open_session(page, session)
            elif disturbance == "toggle_terminal_while_busy":
                self.open_session(page, session)
                for _ in range(4):
                    page.locator("#a-term").click()
                    page.wait_for_timeout(120)
                self.ensure_composer(page, session)
            elif disturbance == "reload_while_busy":
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("S.sessions.length === 20", timeout=60000)
                self.install_dom_monitor(page, "desktop")
                self.open_session(page, session)
            elif disturbance == "disconnect_while_busy":
                page.context.set_offline(True)
                page.wait_for_timeout(900)
                page.context.set_offline(False)
                page.goto(debug_url, wait_until="domcontentloaded")
                page.wait_for_function("S.sessions.length === 20", timeout=60000)
                self.install_dom_monitor(page, "desktop")
                self.install_dom_monitor(mobile, "mobile")
                self.open_session(page, session)
        finally:
            page.context.set_offline(False)
            page.set_viewport_size(original_size)
        self.open_session(page, session)
        if self.wait_web_exchange(page, session, exchange, wait_reply=True):
            session.ops.add(disturbance)

    def idle_browser_action(self, page: Page, mobile: Page, session: Session,
                            action: str, serial: int):
        if action == "switch_idle":
            other = next((row for row in self.sessions
                          if row is not session and not row.frozen), None)
            if other:
                self.open_session(page, other)
            self.open_session(page, session)
        elif action == "resize_idle":
            before = self.terminal_snapshot(session, history_lines=1200)
            marker = session.last_reply
            width = self.random.choice((360, 390, 720, 1024, 1440))
            for height in (380, 760, 460, 900):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(60)
            after = self.terminal_snapshot(session, history_lines=1200)
            if marker and (not terminal_has(before["history"], marker)
                           or not terminal_has(after["history"], marker)):
                self.fail("空闲 resize 后 tmux 历史丢失", session, marker=marker)
                return
        elif action == "terminal_takeover":
            size = self.random.choice(((390, 700), (720, 540),
                                       (1024, 600), (1440, 900)))
            self.cross_view_case(page, mobile, session, size)
            return
        session.ops.add(action)
        self.event("idle_browser_action", session, operation=action, serial=serial)

    def execute_scheduled_action(self, page: Page, mobile: Page,
                                 scheduled: ScheduledAction):
        session = next(row for row in self.sessions
                       if (row.source, row.index) ==
                       (scheduled.source, scheduled.slot))
        action = scheduled.action.name
        self.action_serial += 1
        serial = self.action_serial
        if action in {"browser_roundtrip", "browser_trim_roundtrip"}:
            self.simple_roundtrip(page, session, origin="browser",
                                  variant=action, serial=serial)
        elif action == "lost_response_idempotent_retry":
            self.lost_response_retry_case(page, session, serial)
        elif action == "tmux_roundtrip":
            self.simple_roundtrip(page, session, origin="tmux",
                                  variant=action, serial=serial)
        elif action == "queue_browser_behind_tmux":
            self.queue_case(page, session, start_origin="tmux",
                            queue_origin="browser", serial=serial)
        elif action == "queue_tmux_behind_browser":
            self.queue_case(page, session, start_origin="browser",
                            queue_origin="tmux", serial=serial)
        elif action in {"interrupt_fast_cross_surface",
                        "interrupt_mid_cross_surface"}:
            fast = action.startswith("interrupt_fast")
            start = "browser" if serial % 2 else "tmux"
            interrupt = "tmux" if start == "browser" else "browser"
            self.interrupt_case(page, session, start_origin=start,
                                interrupt_origin=interrupt,
                                delay=0.05 if fast else 0.8, serial=serial)
        elif action == "draft_reject":
            self.cross_draft_case(page, session, accept=False, serial=serial)
        elif action == "draft_overwrite":
            self.cross_draft_case(page, session, accept=True, serial=serial)
        elif action == "question_cross_surface":
            start = "browser" if serial % 2 else "tmux"
            answer = "tmux" if start == "browser" else "browser"
            self.question_case(page, session, start_origin=start,
                               answer_origin=answer, serial=serial)
        elif action == "slash_interleave":
            self.slash_help_interleave(page, mobile, session, serial)
        elif action == "slash_rename":
            self.slash_rename(page, session, serial)
        elif action == "slash_compact":
            self.slash_compact(page, session)
        elif action in {"resize_while_busy", "switch_while_busy",
                        "toggle_terminal_while_busy", "reload_while_busy",
                        "disconnect_while_busy"}:
            self.busy_disturbance_case(page, mobile, session, action, serial)
        elif action in {"switch_idle", "resize_idle", "terminal_takeover"}:
            self.idle_browser_action(page, mobile, session, action, serial)
        else:
            raise ValueError(f"没有实现 monkey 动作：{action}")
        return session

    def refresh_schedule_states(self):
        for session in self.sessions:
            if not session.schedule:
                continue
            session.schedule.frozen = session.frozen
            session.schedule.paid_turns = max(
                session.schedule.paid_turns, session.turns)
            if not session.frozen:
                session.schedule.phase = self.probe_terminal(session)["phase"]

    def run_state_machine(self, page: Page, mobile: Page, deadline: float):
        """Coverage-guided actions replace the former five fixed phases."""
        audit_cursor = 0
        replay_at = 0
        actions_by_name = {action.name: action for action in ACTIONS}
        while time.monotonic() < deadline:
            self.refresh_schedule_states()
            states = [session.schedule for session in self.sessions
                      if session.schedule is not None]
            if self.replay_actions:
                if replay_at >= len(self.replay_actions):
                    break
                row = self.replay_actions[replay_at]
                replay_at += 1
                action = actions_by_name.get(str(row.get("action") or ""))
                if not action:
                    raise ValueError(f"replay 含未知动作：{row.get('action')}")
                scheduled = ScheduledAction(
                    int(row.get("sequence") or replay_at), action,
                    str(row.get("source") or ""), int(row.get("slot") or 0))
                state = next((item for item in states
                              if (item.source, item.slot) ==
                              (scheduled.source, scheduled.slot)), None)
                if not state or (state, action) not in self.scheduler.eligible(states):
                    raise RuntimeError(
                        f"replay 第 {replay_at} 步当前不可执行：{scheduled}")
            else:
                scheduled = self.scheduler.choose(states)
            self.current_action = scheduled
            trace = {"sequence": scheduled.sequence,
                     "action": scheduled.action.name,
                     "source": scheduled.source, "slot": scheduled.slot,
                     "started": time.time(), "seed": self.seed}
            self.action_trace.append(trace)
            self.event("action_started", source=scheduled.source,
                       slot=scheduled.slot, paid_turns=scheduled.action.paid_turns)
            before_failures = len(self.failures)
            session = next(row for row in self.sessions
                           if (row.source, row.index) ==
                           (scheduled.source, scheduled.slot))
            try:
                self.execute_scheduled_action(page, mobile, scheduled)
            except Exception as error:
                # One malformed transition must not turn a one-hour soak into
                # an eight-step smoke test.  Capture it against the exact
                # session while Playwright is still alive, freeze that session,
                # restore both pages, and keep exercising the remaining pool.
                self.fail("monkey 动作异常", session,
                          error=f"{type(error).__name__}: {error}")
                recovery_errors = []
                for current, label in ((page, "desktop"), (mobile, "mobile")):
                    try:
                        self.recover_browser(current, label)
                    except Exception as recovery_error:
                        recovery_errors.append(
                            f"{label}: {type(recovery_error).__name__}: {recovery_error}")
                if recovery_errors:
                    raise RuntimeError("浏览器恢复失败：" + "; ".join(
                        recovery_errors)) from error
            succeeded = len(self.failures) == before_failures and not session.frozen
            trace.update({"ended": time.time(), "succeeded": succeeded,
                          "actual_turns": session.turns})
            if succeeded and session.schedule:
                self.scheduler.record(scheduled, session.schedule)
                session.schedule.paid_turns = max(
                    session.schedule.paid_turns, session.turns)
            self.audit_consistency(page, session, "desktop")
            self.audit_consistency(mobile, session, "mobile")
            # Also audit a different cached session every step.  This catches a
            # late SSE/outbox reconciliation after the monkey has switched away.
            other = self.sessions[audit_cursor % len(self.sessions)]
            audit_cursor += 1
            if other is not session and not other.frozen:
                self.audit_consistency(page, other, "desktop")
                self.audit_consistency(mobile, other, "mobile")
            self.audit_all()
            self.drain_dom(page, "desktop")
            self.drain_dom(mobile, "mobile")
            self.event("action_finished", session, succeeded=succeeded,
                       scheduler=self.scheduler.snapshot())
            self.current_action = None

    def cross_view_case(self, primary: Page, secondary: Page, session: Session,
                        size: tuple[int, int]):
        """Interleave chat/terminal, resize and two browser owners around one pane."""
        if session.frozen:
            return
        before = self.terminal_snapshot(session, history_lines=2000)
        marker = session.last_reply
        if marker and not terminal_has(before["history"], marker):
            self.fail("切换前 tmux 历史已丢失最后回复", session, marker=marker)
            return
        checkpoints = ((primary, self.dialog_checkpoint(primary)),
                       (secondary, self.dialog_checkpoint(secondary)))

        def recent_dialogs():
            return [row for current, checkpoint in checkpoints
                    for row in self.dialogs_since(current, checkpoint)]
        ownership = []
        terminal_visible = """name => {
          const view=T.views.get(name), pane=document.querySelector('#termpane');
          return T.name === name && view && pane
            && !pane.classList.contains('hidden')
            && (MOBILE.matches || T.mode !== 'collapsed');
        }"""
        terminal_hidden = """name => {
          const view=T.views.get(name), pane=document.querySelector('#termpane');
          return T.name !== name || !view || !pane
            || pane.classList.contains('hidden')
            || (!MOBILE.matches && T.mode === 'collapsed');
        }"""

        def normalize_chat(current: Page):
            """Put either mobile overlay or desktop terminal into chat view."""
            self.ensure_composer(current, session)
            if current.evaluate(terminal_visible, session.name):
                current.locator("#a-term").click()
                current.wait_for_function(terminal_hidden, arg=session.name,
                                          timeout=30000)

        try:
            # Resize first.  Mobile uses an overlay and deliberately does not
            # mutate the desktop-only T.mode; waiting for T.mode === 'full'
            # before this resize therefore produced a false 30-second stall.
            primary.set_viewport_size({"width": size[0], "height": size[1]})
            normalize_chat(primary)
            primary.locator("#a-term").click()
            primary.wait_for_function(terminal_visible, arg=session.name,
                                      timeout=30000)
            primary.wait_for_timeout(120)

            # The second page must take ownership.  Accept both the takeover
            # confirmation there and the revocation alert on the first page;
            # unhandled native dialogs otherwise suspend Playwright and turn a
            # real ownership test into a timeout.
            normalize_chat(secondary)
            secondary.locator("#a-term").click()
            secondary.wait_for_function(terminal_visible, arg=session.name,
                                        timeout=30000)
            secondary.wait_for_function("""name => {
              const view=T.views.get(name), pane=document.querySelector('#termpane');
              return T.name === name && view?.ws?.readyState === 1 && !view.revoked
                && pane && !pane.classList.contains('hidden');
            }""", arg=session.name, timeout=30000)
            primary.wait_for_function("""name => {
              const view=T.views.get(name), pane=document.querySelector('#termpane');
              return pane?.classList.contains('hidden')
                && (!view || view.revoked || view.ws?.readyState !== 1);
            }""", arg=session.name, timeout=30000)
            for current in (primary, secondary):
                ownership.append(current.evaluate("""name => {
                  const view=T.views.get(name), pane=document.querySelector('#termpane');
                  return {mode:T.mode, ws:view?.ws?.readyState ?? -1, uid:S.sel,
                    revoked:!!view?.revoked,
                    paneHidden:!pane || pane.classList.contains('hidden')};
                }""", session.name))
            owners = [(current, state) for current, state
                      in zip((primary, secondary), ownership)
                      if state["ws"] == 1 and not state["revoked"]
                      and not state["paneHidden"]]
            if len(owners) != 1:
                self.fail("两个网页同时保持 tmux 写连接", session,
                          ownership=ownership, dialogs=recent_dialogs())
                return

            # Only the surviving owner is allowed to switch back to chat.  A
            # click on the revoked page would immediately reclaim ownership and
            # create an artificial takeover ping-pong.
            for current, _state in owners:
                current.locator("#a-term").click()
                current.wait_for_function(terminal_hidden, arg=session.name,
                                          timeout=30000)
        except PlaywrightTimeoutError as error:
            states = []
            for current in (primary, secondary):
                try:
                    states.append(current.evaluate("""name => {
                      const view=T.views.get(name), pane=document.querySelector('#termpane');
                      return {mode:T.mode, mobile:MOBILE.matches, name:T.name,
                        ws:view?.ws?.readyState ?? -1, revoked:!!view?.revoked,
                        paneHidden:!pane || pane.classList.contains('hidden'), uid:S.sel};
                    }""", session.name))
                except Exception as state_error:
                    states.append({"error": str(state_error)})
            self.fail("双页面接管/切换终端超时", session, error=str(error),
                      states=states, dialogs=recent_dialogs())
            return
        # resize 时 TUI 会短暂清屏后重画；一次抓屏可能正落在两帧之间。
        # 终态必须恢复，但不能把中间帧误报成 scrollback 丢失。
        deadline = time.monotonic() + 5
        after = None
        while time.monotonic() < deadline:
            candidate = self.terminal_snapshot(session, history_lines=2000)
            if not marker or terminal_has(candidate["history"], marker):
                after = candidate
                break
            time.sleep(0.05)
        if after is None:
            after = self.terminal_snapshot(session, history_lines=2000)
            self.fail("切换/resize 后 tmux 内容丢失", session,
                      marker=marker, ownership=ownership)
            return
        self.open_session(primary, session)
        if marker:
            presence = self.browser_presence(primary, marker)
            if presence["assistant"] != 1:
                self.fail("切换终端与对话后回复未同步", session,
                          marker=marker, presence=presence)
                return
        info = term.session_info(session.name) or {}
        self.event("cross_view_verified", session, size=size,
                   ownership=ownership, pane_size=[info.get("cols"), info.get("rows")],
                   history_hash=after["history_hash"],
                   dialogs=recent_dialogs())
        session.ops.add("cross_view")

    def audit_dom_continuity(self):
        """Detect a browser bubble vanishing between tmux receipt and completion."""
        checked = 0
        for session in self.sessions:
            for tracked in session.tracked.values():
                if tracked.get("origin") != "browser" or not tracked.get("ended"):
                    continue
                marker = tracked["request"]
                start = float(tracked["created"]) + 0.8
                end = float(tracked["ended"])
                for label in {str(row.get("label") or "") for row in self.dom_rows}:
                    rows = sorted((row for row in self.dom_rows
                                   if row.get("label") == label),
                                  key=lambda row: int(row.get("at") or 0))
                    seen = False
                    relevant = False
                    for at, row in enumerate(rows):
                        wall = int(row.get("at") or 0) / 1000
                        if wall < start or wall > end:
                            continue
                        state = row.get("state") or {}
                        if state.get("uid") != session.uid:
                            continue
                        texts = [*(state.get("pending") or []), *(state.get("users") or [])]
                        visible = sum(marker in str(text) for text in texts)
                        if visible:
                            relevant = seen = True
                            if visible > 1:
                                self.fail("DOM 轨迹中同一消息曾同时显示多份", session,
                                          marker=marker, label=label, wall=wall)
                                break
                            continue
                        if not seen:
                            continue
                        next_wall = end
                        if at + 1 < len(rows):
                            next_wall = min(end, int(rows[at + 1].get("at") or 0) / 1000)
                        if next_wall - wall > 0.4:
                            self.fail("DOM 轨迹确认消息曾在回复前消失", session,
                                      marker=marker, label=label,
                                      missing_ms=round((next_wall - wall) * 1000))
                            break
                    if relevant:
                        checked += 1
        self.event("dom_continuity_verified", checked=checked)

    def run_hour(self):
        debug_url = self.base + "/?" + urllib.parse.urlencode({"debug_run": self.run_id})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 720},
                                          record_video_dir=str(self.artifacts / "video"))
            page = context.new_page()
            mobile = context.new_page()
            self.pages = {"desktop": page, "mobile": mobile}
            self.install_dialog_broker(page, "desktop")
            self.install_dialog_broker(mobile, "mobile")
            errors = []
            for current in (page, mobile):
                current.on("pageerror", lambda error: errors.append(str(error)))
                current.goto(debug_url, wait_until="networkidle")
                current.wait_for_function("S.sessions.length === 20", timeout=60000)
            mobile.set_viewport_size({"width": 390, "height": 760})
            self.install_dom_monitor(page, "desktop")
            self.install_dom_monitor(mobile, "mobile")

            started = time.monotonic()
            deadline = started + self.duration
            self.event("timed_run_started", deadline_wall=time.time() + self.duration,
                       debug_url=debug_url)
            self.qualify_sessions(page)
            self.run_state_machine(page, mobile, deadline)
            self.timed_elapsed = time.monotonic() - started
            self.drain_dom(page, "desktop")
            self.drain_dom(mobile, "mobile")
            self.audit_dom_continuity()
            if errors:
                self.fail("浏览器 pageerror", errors=errors)
            context.close()
            browser.close()
            self.pages = {}
            if not self.replay_actions and time.monotonic() + 1 < deadline:
                raise AssertionError("monkey 未持续到设定时长")

    def settle(self):
        settle_end = time.monotonic() + 180
        while time.monotonic() < settle_end:
            self.audit_all()
            states = [self.probe_terminal(session) for session in self.sessions
                      if not session.frozen]
            if all(not row["busy"] and row["composer"] in {"empty", "editing"}
                   for row in states):
                break
            time.sleep(2)
        self.assert_models()
        self.assert_hidden(allow_debug_count=20)
        missing_questions = {"claude", "codex"} - self.question_sources
        if missing_questions and not self.replay_actions:
            self.fail("选择题覆盖不完整", missing=sorted(missing_questions))
        required_each = {"bootstrap_browser_sync", "browser_to_tmux"}
        missing_ops = {
            f"{session.source}-{session.index}": sorted(required_each - session.ops)
            for session in self.sessions
            if not session.frozen and not required_each.issubset(session.ops)
        }
        if missing_ops:
            self.fail("双向 tmux/对话覆盖不完整", missing=missing_ops)
        missing_core = {source: names for source, names
                        in self.scheduler.missing_core().items() if names}
        if missing_core and not self.replay_actions:
            self.fail("状态机核心转移覆盖不完整", missing=missing_core,
                      scheduler=self.scheduler.snapshot())
        summary = {"run_id": self.run_id, "root": str(self.root),
                   "debug_url": self.base + "/?" + urllib.parse.urlencode(
                       {"debug_run": self.run_id}),
                   "duration": self.duration, "seed": self.seed,
                   "timed_elapsed": self.timed_elapsed,
                   "models": {"claude": CLAUDE_MODEL, "codex": CODEX_MODEL},
                   "normal_leaks": 0,
                   "coverage": {"questions": sorted(self.question_sources),
                                "events": dict(sorted(self.event_counts.items())),
                                "scheduler": self.scheduler.snapshot(),
                                "session_ops": {f"{s.source}-{s.index}": sorted(s.ops)
                                                for s in self.sessions}},
                   "failures": self.failures,
                   "sessions": [{"source": s.source, "slot": s.index,
                                  "uid": s.uid, "sid": s.sid, "name": s.name,
                                  "path": str(s.path), "turns": s.turns,
                                  "frozen": s.frozen} for s in self.sessions]}
        (self.artifacts / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        (self.artifacts / "replay.json").write_text(json.dumps({
            "seed": self.seed, "run_id": self.run_id,
            "max_paid_turns": self.max_paid_turns,
            "actions": self.action_trace,
        }, ensure_ascii=False, indent=2) + "\n")
        self.event("run_finished", failures=len(self.failures), summary=summary)

    def stop_terminals(self):
        for session in self.sessions:
            try:
                term.kill_session(session.name)
            except Exception as error:
                self.event("stop_error", session, error=str(error))

    def run(self):
        try:
            self.create_sessions()
            self.bootstrap()
            self.run_hour()
            self.settle()
        except Exception as error:
            self.fail("monkey 主流程异常", error=f"{type(error).__name__}: {error}")
            if (len(self.sessions) == self.count * 2
                    and all(session.uid and session.path for session in self.sessions)):
                try:
                    self.settle()
                except Exception as settle_error:
                    self.event("settle_error", error=str(settle_error))
            raise
        finally:
            self.stop_terminals()
            # Deliberately keep debug registry and native transcripts.  The user
            # can inspect this exact run through debug_url without seeing it in
            # the ordinary list.  A separate reviewed cleanup can archive them.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get(
        "SESMAN_BASE", "http://127.0.0.1:8710"))
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    parser.add_argument("--seed", type=int, default=int(time.time()))
    parser.add_argument("--max-paid-turns", type=int, default=14,
                        help="每个真实 CLI 会话允许的最大模型回合数")
    parser.add_argument("--simulate", action="store_true",
                        help="只运行免费调度模拟，不启动浏览器、tmux 或模型")
    parser.add_argument("--steps", type=int, default=900,
                        help="--simulate 的动作数")
    parser.add_argument("--replay", type=Path,
                        help="重放失败证据包中的 replay.json（仍会产生模型费用）")
    args = parser.parse_args()
    if args.simulate:
        result = simulate_schedule(
            args.seed, steps=args.steps, sessions_per_source=args.sessions,
            max_paid_turns=args.max_paid_turns)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        missing = result["scheduler"]["missing_core"]
        oracle_failed = bool(result["oracle"]["missing"]
                             or result["oracle"]["normal_issues"])
        return 1 if any(missing.values()) or oracle_failed else 0
    replay_actions = []
    if args.replay:
        replay = json.loads(args.replay.read_text())
        replay_actions = list(replay.get("actions") or [])
        args.seed = int(replay.get("seed") or args.seed)
        args.max_paid_turns = int(
            replay.get("max_paid_turns") or args.max_paid_turns)
    monkey = DualMonkey(args.base, args.sessions, args.duration, args.seed,
                        args.max_paid_turns, replay_actions)
    print(f"debug URL: {args.base}/?debug_run={monkey.run_id}", flush=True)
    monkey.run()
    return 1 if monkey.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
