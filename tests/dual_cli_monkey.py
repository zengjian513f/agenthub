"""One-hour paid Claude/Codex state-machine monkey.

This test is intentionally never part of unittest/e2e discovery.  It creates
ten real sessions per CLI, pins their cheapest requested models, registers the
whole run as hidden before starting a TUI, and keeps evidence under the run
root.  A crash leaves the debug registry in place, so test sessions stay out of
the user's ordinary sesman list.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import socket
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
from sesman import claude_bridge, debug_runs, term


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
            exe, "--safe-mode", "--permission-mode", "plan",
            "--tools", "AskUserQuestion", "--model", self.model,
            "--effort", "low", "--session-id", session.sid,
            "--settings", claude_bridge.settings_path(),
        ])

    def ready(self, screen: str) -> bool:
        text = screen.casefold()
        return "haiku 4.5" in text and "plan mode on" in text

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
    def __init__(self, base: str, count: int, duration: int, seed: int):
        if count != 10:
            raise ValueError("本轮按用户要求必须每种 CLI 恰好 10 个会话")
        self.base = base.rstrip("/")
        self.count = count
        self.duration = duration
        self.seed = seed
        self.random = random.Random(seed)
        self.run_id = f"monkey-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.root = Path(tempfile.mkdtemp(prefix=f"sesman-{self.run_id}-"))
        os.chmod(self.root, 0o700)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.events_path = self.artifacts / "events.jsonl"
        self.failures: list[dict] = []
        self.event_counts: dict[str, int] = {}
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
        self.event("run_registered", root=str(self.root), duration=duration, seed=seed)

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

    def post(self, path: str, body: dict, *, debug=True):
        payload = json.dumps({**body, "_build": self.build}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.url(path, debug=debug), data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            data = json.loads(error.read() or b"{}")
            raise AssertionError(f"POST {path} -> {error.code}: {data}") from error
        return data

    def event(self, kind: str, session: Session | None = None, **data):
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        row = {"wall": time.time(), "mono": time.monotonic(), "event": kind,
               "run_id": self.run_id, **data}
        if session:
            row.update({"source": session.source, "slot": session.index,
                        "uid": session.uid, "sid": session.sid,
                        "name": session.name})
        with self.events_path.open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        # High-frequency state transitions remain in the evidence JSONL.  Keep
        # stdout compact enough that an hour-long run can be monitored live.
        if kind not in {"dom_state", "delivery_state", "send_response", "ui_sent"}:
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
        self.event("failure", session, **row)

    def messages(self, session: Session):
        return self.get("/api/messages/" + urllib.parse.quote(session.uid, safe=""))

    def outbox(self, session: Session):
        return self.get("/api/session/outbox?uid="
                        + urllib.parse.quote(session.uid, safe=""))

    def create_sessions(self):
        self.claude.create()
        self.codex.create()
        self.assert_hidden(allow_debug_count=None)

    def bootstrap(self):
        for offset in range(0, len(self.sessions), 5):
            for session in self.sessions[offset:offset + 5]:
                token = f"BOOT-{self.run_id}-{session.source}-{session.index}"
                prompt = f'Reply exactly "{token}" and nothing else.'
                session.tracked[prompt] = {
                    "created": time.time(), "kind": "bootstrap", "reply": token}
                term.submit_text(session.name, prompt)
                session.turns += 1
                self.event("bootstrap_sent", session, text=token)
            time.sleep(0.8)

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
            debug_runs.add_session(
                self.run_id, source=session.source, cwd=str(session.cwd),
                sid=session.sid, uid=session.uid, name=session.name)
            self.event("session_linked", session, path=str(session.path), model=row.get("model"))

        for session in self.sessions:
            token = next(iter(session.tracked.values()))["reply"]
            wait_for(
                f"{session.source} {session.index} bootstrap 回复",
                lambda s=session, t=token: any(
                    message.get("role") == "assistant" and t in str(message.get("text") or "")
                    for message in self.messages(s).get("messages", [])),
                timeout=180, interval=0.4)
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

    def body(self, session: Session, text: str, request_id: str):
        current = self.messages(session)
        return {"uid": session.uid, "name": session.name, "text": text,
                "media": [], "request_id": request_id,
                "page_id": f"dual-monkey-{self.run_id}",
                "activity": current.get("activity"),
                "cursor": {"start": current["end"],
                           "head": current["version"]["head"],
                           "anchor": current.get("anchor", "")}}

    def send_api(self, session: Session, text: str, kind="api"):
        if session.frozen or session.turns >= 8:
            return None
        request_id = f"{self.run_id}-{session.source}-{session.index}-{uuid.uuid4().hex[:8]}"
        before = time.time()
        result = self.post("/api/session/send", self.body(session, text, request_id))
        session.turns += 1
        session.tracked[text] = {"created": before, "request_id": request_id, "kind": kind}
        self.event("send_response", session, text=text, request_id=request_id, response=result)
        self.audit(session, immediate=True)
        return result

    def audit(self, session: Session, immediate=False):
        if not session.uid:
            return
        data = self.messages(session)
        messages = data.get("messages", [])
        outbox = self.outbox(session).get("outbox", [])
        now = time.time()
        for text, tracked in session.tracked.items():
            formal = sum(message.get("role") in {"user", "command"}
                         and str(message.get("text") or "").strip() == text.strip()
                         for message in messages)
            pending = sum(str(item.get("text") or "").strip() == text.strip()
                          for item in outbox)
            previous = tracked.get("last")
            state = (formal, pending)
            if state != previous:
                self.event("delivery_state", session, text=text, formal=formal,
                           pending=pending, activity=data.get("activity"))
                tracked["last"] = state
            if formal > 1:
                self.fail("同一输入在活动时间线出现多次", session, text=text, formal=formal)
            if not formal and not pending and not tracked.get("may_cancel"):
                self.fail("输入既无 pending 也无正式消息", session, text=text,
                          age=now - tracked["created"], immediate=immediate)
            if formal and pending and now - tracked["created"] > 5:
                self.fail("正式消息与 pending 重复超过 5 秒", session, text=text)

    def audit_all(self):
        for session in self.sessions:
            if not session.frozen:
                try:
                    self.audit(session)
                except Exception as error:
                    self.fail("对账异常", session, error=str(error))
        now = time.monotonic()
        if now - self.last_model_audit >= 30:
            self.assert_models()
        if now - self.last_visibility_audit >= 30:
            self.assert_hidden(allow_debug_count=20)

    def open_session(self, page: Page, session: Session):
        page.evaluate("uid => openSession(uid)", session.uid)
        page.wait_for_function("uid => S.sel === uid", arg=session.uid, timeout=30000)
        page.wait_for_selector("#msgs", timeout=30000)

    def ensure_composer(self, page: Page, session: Session):
        self.open_session(page, session)
        if page.locator("#composer:not(.hidden)").count():
            return
        page.locator("#a-term").click()
        page.wait_for_selector("#composer:not(.hidden)", timeout=30000)
        if page.evaluate("T.mode") == "full":
            page.locator("#a-term").click()
            page.wait_for_function("T.mode === 'collapsed'", timeout=30000)

    def send_ui(self, page: Page, session: Session, text: str, kind="ui"):
        if session.frozen or session.turns >= 8:
            return
        self.ensure_composer(page, session)
        page.locator("#cinput").fill(text)
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
        session.turns += 1
        session.tracked[text] = {"created": sent_at, "kind": kind}
        self.event("ui_sent", session, text=text, response=result)
        if response.status >= 400 or result.get("error") or result.get("draft_conflict"):
            self.fail("网页发送未获成功响应", session, text=text, response=result)
            return
        self.audit(session, immediate=True)

    def install_dom_monitor(self, page: Page, label: str):
        page.evaluate("""label => {
          window.__monkeyDom=[]; window.__monkeyLast='';
          window.__monkeyTimer=setInterval(()=>{
            const state=JSON.stringify({uid:S.sel,
              pending:[...document.querySelectorAll('.client-pending')].map(x=>x.innerText),
              users:[...document.querySelectorAll('.msg.user')].slice(-4).map(x=>x.innerText),
              activity:document.querySelector('#activity')?.innerText||''});
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

    def phase_baseline(self, page: Page, end: float):
        cursor = 0
        while time.monotonic() < end:
            session = self.sessions[cursor % len(self.sessions)]
            cursor += 1
            if not session.frozen and session.turns < 3:
                token = f"BASE-{self.run_id}-{session.source}-{session.index}-{session.turns}"
                self.send_ui(page, session, f'Reply exactly "{token}" and nothing else.', "baseline")
                other = self.sessions[(cursor + 7) % len(self.sessions)]
                self.open_session(page, other)
            self.audit_all()
            self.drain_dom(page, "desktop")
            time.sleep(1.0)

    def phase_queue(self, page: Page, end: float):
        targets = [self.claude.sessions[2], self.codex.sessions[2],
                   self.claude.sessions[1], self.codex.sessions[1]]
        cursor = 0
        while time.monotonic() < end:
            session = targets[cursor % len(targets)]
            cursor += 1
            if not session.frozen and session.turns <= 5:
                marker = f"BUSY-END-{self.run_id}-{session.source}-{cursor}"
                self.send_api(session,
                              f"Print integers 1 through 40 one per line, then {marker}.", "busy")
                time.sleep(0.15)
                queued = f"QUEUED-{self.run_id}-{session.source}-{cursor}"
                self.send_api(session, f'Reply exactly "{queued}" and nothing else.', "busy_queue")
            self.open_session(page, self.sessions[cursor % len(self.sessions)])
            self.audit_all()
            self.drain_dom(page, "desktop")
            time.sleep(2.0)

    def phase_escape(self, page: Page, end: float):
        targets = [*self.claude.sessions[3:6], *self.codex.sessions[3:6]]
        cursor = 0
        delays = [0.1, 0.3, 1.0, 2.0]
        while time.monotonic() < end:
            session = targets[cursor % len(targets)]
            delay = delays[cursor % len(delays)]
            cursor += 1
            if not session.frozen and session.turns < 7:
                text = f"ESC-{self.run_id}-{session.source}-{session.index}-{cursor}"
                self.send_ui(page, session,
                             f'Reply exactly "{text}" and nothing else.', "escape")
                tracked_text = next(reversed(session.tracked))
                session.tracked[tracked_text]["may_cancel"] = True
                time.sleep(delay)
                page.locator("#cesc").click()
                time.sleep(0.35)
                screen = term.capture_plain(session.name, 140)
                self.event("escape_result", session, delay=delay, screen=screen[-3000:])
                # Do not let a restored draft silently contaminate the next web send.
                term.send_keys(session.name, "C-u")
            self.audit_all()
            self.drain_dom(page, "desktop")
            time.sleep(1.0)

    def answer_question(self, page: Page, session: Session):
        """Exercise the complete browser question flow while the TUI is alive."""
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
        options.first.click()
        # Claude's multi-question protocol selects first, then submits the
        # complete form.  Codex's one-question protocol submits on selection.
        submit = page.locator(".live-question .question-submit")
        if submit.count():
            submit.wait_for(state="visible", timeout=10000)
            if submit.is_disabled():
                self.fail("选择题选项未启用提交按钮", session)
                return
            submit.click()
        try:
            page.locator(".live-question").wait_for(state="detached", timeout=60000)
        except PlaywrightTimeoutError:
            self.fail("选择题提交后未收敛", session)
            return
        self.question_sources.add(session.source)
        self.event("question_answered", session)

    def phase_special(self, page: Page, mobile: Page, end: float):
        question_targets = [self.claude.sessions[6], self.codex.sessions[6]]
        for index, session in enumerate(question_targets):
            if session.turns < 8 and not session.frozen:
                tool = "AskUserQuestion" if session.source == "claude" else "request_user_input"
                self.send_api(session,
                              f"Use {tool} now to ask one question with exactly two options Alpha and Beta.",
                              "question")
                self.answer_question(page if index == 0 else mobile, session)
        command_targets = [self.claude.sessions[7], self.codex.sessions[7]]
        for session in command_targets:
            term.submit_text(session.name, "/help")
            time.sleep(0.5)
            term.send_keys(session.name, "Escape")
            term.submit_text(session.name, f"/rename monkey-{self.run_id}-{session.source}")
            time.sleep(0.5)
            term.submit_text(session.name, "/compact")
            self.event("slash_commands", session)

        cursor = 0
        sizes = [(390, 760), (430, 820), (900, 560), (1366, 768)]
        while time.monotonic() < end:
            session = self.sessions[cursor % len(self.sessions)]
            cursor += 1
            target_page = mobile if cursor % 3 == 0 else page
            target_page.set_viewport_size({"width": sizes[cursor % len(sizes)][0],
                                           "height": sizes[cursor % len(sizes)][1]})
            self.open_session(target_page, session)
            if cursor % 4 == 0 and target_page.locator("#a-term").count():
                target_page.locator("#a-term").click()
                time.sleep(0.2)
                target_page.locator("#a-term").click()
            self.audit_all()
            self.drain_dom(page, "desktop")
            self.drain_dom(mobile, "mobile")
            time.sleep(1.0)

    def phase_soak(self, page: Page, mobile: Page, end: float):
        sizes = [(390, 700), (720, 540), (1024, 600), (1440, 900)]
        cursor = 0
        while time.monotonic() < end:
            session = self.random.choice([x for x in self.sessions if not x.frozen] or self.sessions)
            target = page if cursor % 2 else mobile
            target.set_viewport_size({"width": sizes[cursor % 4][0],
                                      "height": sizes[cursor % 4][1]})
            self.open_session(target, session)
            if cursor % 5 == 0 and target.locator("#a-term").count():
                target.locator("#a-term").click()
            cursor += 1
            self.audit_all()
            self.drain_dom(page, "desktop")
            self.drain_dom(mobile, "mobile")
            time.sleep(1.5)

    def run_hour(self):
        debug_url = self.base + "/?" + urllib.parse.urlencode({"debug_run": self.run_id})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 720},
                                          record_video_dir=str(self.artifacts / "video"))
            page = context.new_page()
            mobile = context.new_page()
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
            marks = [started + self.duration * ratio for ratio in (0.17, 0.39, 0.62, 0.85, 1.0)]
            self.event("timed_run_started", deadline_wall=time.time() + self.duration,
                       debug_url=debug_url)
            self.phase_baseline(page, marks[0])
            self.phase_queue(page, marks[1])
            self.phase_escape(page, marks[2])
            self.phase_special(page, mobile, marks[3])
            self.phase_soak(page, mobile, marks[4])
            self.timed_elapsed = time.monotonic() - started
            self.drain_dom(page, "desktop")
            self.drain_dom(mobile, "mobile")
            if errors:
                self.fail("浏览器 pageerror", errors=errors)
            context.close()
            browser.close()
            if time.monotonic() + 1 < deadline:
                raise AssertionError("monkey 未持续到设定时长")

    def settle(self):
        settle_end = time.monotonic() + 180
        while time.monotonic() < settle_end:
            self.audit_all()
            outstanding = sum(len(self.outbox(session).get("outbox", []))
                              for session in self.sessions if not session.frozen)
            if outstanding == 0:
                break
            time.sleep(2)
        self.assert_models()
        self.assert_hidden(allow_debug_count=20)
        missing_questions = {"claude", "codex"} - self.question_sources
        if missing_questions:
            self.fail("选择题覆盖不完整", missing=sorted(missing_questions))
        summary = {"run_id": self.run_id, "root": str(self.root),
                   "debug_url": self.base + "/?" + urllib.parse.urlencode(
                       {"debug_run": self.run_id}),
                   "duration": self.duration, "seed": self.seed,
                   "timed_elapsed": self.timed_elapsed,
                   "models": {"claude": CLAUDE_MODEL, "codex": CODEX_MODEL},
                   "normal_leaks": 0,
                   "coverage": {"questions": sorted(self.question_sources),
                                "events": dict(sorted(self.event_counts.items()))},
                   "failures": self.failures,
                   "sessions": [{"source": s.source, "slot": s.index,
                                  "uid": s.uid, "sid": s.sid, "name": s.name,
                                  "path": str(s.path), "turns": s.turns,
                                  "frozen": s.frozen} for s in self.sessions]}
        (self.artifacts / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
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
    args = parser.parse_args()
    monkey = DualMonkey(args.base, args.sessions, args.duration, args.seed)
    print(f"debug URL: {args.base}/?debug_run={monkey.run_id}", flush=True)
    monkey.run()
    return 1 if monkey.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
