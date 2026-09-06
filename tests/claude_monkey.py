"""Real Claude Code delivery/interaction monkey.

This is intentionally separate from the deterministic E2E suite: it starts paid
Claude sessions and therefore only runs when invoked explicitly.  Every session
is pinned to Haiku with low effort, plan permission mode, no tools, and safe mode.

Usage:
    AGENTHUB_BASE=http://127.0.0.1:8710 python3 tests/claude_monkey.py
"""

from __future__ import annotations

import argparse
import json
import os
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
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agenthub import claude_bridge, term

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def wait_for(label, predicate, timeout=90.0, interval=0.1):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError) as error:
            last_error = error
        time.sleep(interval)
    suffix = f": {last_error}" if last_error else ""
    raise AssertionError(f"等待超时：{label}{suffix}")


class Monkey:
    def __init__(self, base: str, count: int, keep: bool,
                 resume_root: str = ""):
        if count < 6:
            raise ValueError("真实 monkey 至少需要 6 个会话")
        self.base = base.rstrip("/")
        self.count = count
        self.keep = keep
        self.run_id = uuid.uuid4().hex[:8]
        if resume_root:
            root = Path(resume_root).resolve()
            if not root.name.startswith("agenthub-claude-monkey-"):
                raise ValueError("--resume-root 必须是本脚本创建的 agenthub-claude-monkey-* 目录")
            self.root = root
        else:
            self.root = Path(tempfile.mkdtemp(
                prefix=f"agenthub-claude-monkey-{self.run_id}-"))
        self.project_stores: list[Path] = []
        self.sessions: list[dict] = []
        self.build = str(self.get("/api/meta")["build"])
        self.checks: list[str] = []

    def ok(self, label: str):
        self.checks.append(label)
        print(f"✅ {label}", flush=True)

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=60) as response:
            return json.loads(response.read())

    def post(self, path: str, body: dict):
        payload = json.dumps({**body, "_build": self.build}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.base + path, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            data = json.loads(error.read() or b"{}")
            raise AssertionError(f"POST {path} -> {error.code}: {data}") from error
        if data.get("error"):
            raise AssertionError(f"POST {path}: {data['error']}")
        return data

    @staticmethod
    def command(sid: str, resume: bool = False) -> str:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("找不到 claude")
        session_args = ["--resume", sid] if resume else ["--session-id", sid]
        return shlex.join([
            exe,
            "--safe-mode",
            "--permission-mode", "plan",
            "--tools", "",
            # Do not use the `haiku` alias here. Claude Code 2.1.237 can paint
            # "Haiku 4.5" in the TUI while persisting claude-sonnet-5 as the
            # actual inference model. The canonical dated ID is verified again
            # from every assistant JSONL record before the monkey continues.
            "--model", HAIKU_MODEL,
            "--effort", "low",
            *session_args,
            "--settings", claude_bridge.settings_path(),
        ])

    @staticmethod
    def store_for(cwd: Path) -> Path:
        return Path.home() / ".claude" / "projects" / (
            "-" + str(cwd).lstrip("/").replace("/", "-"))

    def start_sessions(self):
        for i in range(self.count):
            sid = str(uuid.uuid4())
            cwd = self.root / f"session-{i + 1}"
            cwd.mkdir()
            name = term.new_session(
                f"claude-{sid[:8]}", self.command(sid), str(cwd), 108, 30)
            project_store = self.store_for(cwd)
            transcript = project_store / f"{sid}.jsonl"
            self.project_stores.append(project_store)
            self.sessions.append({
                "i": i + 1, "sid": sid, "name": name, "cwd": cwd,
                "path": transcript, "uid": "",
            })

        for session in self.sessions:
            screen = wait_for(
                f"{session['name']} 信任提示或输入框",
                lambda s=session: self.startup_screen(s), timeout=30)
            if "trust this folder" in screen:
                term.send_keys(session["name"], "Enter")
            screen = wait_for(
                f"{session['name']} Haiku 输入框",
                lambda s=session: self.ready_screen(s), timeout=45)
            assert "Haiku 4.5" in screen and "plan mode on" in screen, screen[-1200:]
            assert "Fable" not in screen, screen[-1200:]
        self.ok(f"{self.count} 个会话全部锁定 Haiku 4.5 / low / plan / no-tools")

    def resume_sessions(self):
        for cwd in sorted(self.root.glob("session-*")):
            project_store = self.store_for(cwd)
            transcripts = sorted(project_store.glob("*.jsonl"))
            if len(transcripts) != 1:
                raise AssertionError(f"无法唯一恢复 {cwd}: {transcripts}")
            transcript = transcripts[0]
            sid = transcript.stem
            name = term.new_session(
                f"claude-{sid[:8]}", self.command(sid, resume=True),
                str(cwd), 108, 30)
            self.project_stores.append(project_store)
            self.sessions.append({
                "i": len(self.sessions) + 1, "sid": sid, "name": name,
                "cwd": cwd, "path": transcript, "uid": "",
            })
        if len(self.sessions) < 6:
            raise AssertionError(f"恢复到的会话不足 6 个：{len(self.sessions)}")
        for session in self.sessions:
            wait_for(f"恢复 {session['name']}",
                     lambda s=session: self.resumed_screen(s), timeout=45)
        rows = self.get("/api/sessions?force=1")["sessions"]
        by_sid = {str(row.get("sid")): row for row in rows if row.get("source") == "claude"}
        for session in self.sessions:
            session["uid"] = by_sid[session["sid"]]["uid"]
        self.assert_haiku()
        self.ok("复用已验证会话恢复了 6 个 tmux，未重放前面的付费 prompt")

    @staticmethod
    def startup_screen(session: dict):
        screen = term.capture_plain(session["name"], 100)
        return screen if "trust this folder" in screen or "Haiku 4.5" in screen else ""

    @staticmethod
    def ready_screen(session: dict):
        screen = term.capture_plain(session["name"], 100)
        return screen if "Haiku 4.5" in screen and "plan mode on" in screen else ""

    @staticmethod
    def resumed_screen(session: dict):
        # 恢复长会话时欢迎页上的模型名已经滚出 capture 范围；实际模型由
        # assert_haiku 从 JSONL 硬校验，此处只等交互输入框真正就绪。
        screen = term.capture_plain(session["name"], 120)
        return screen if "plan mode on" in screen and "❯" in screen else ""

    def bootstrap(self):
        for session in self.sessions:
            token = f"BOOT-{self.run_id}-{session['i']}"
            session["boot"] = token
            term.submit_text(session["name"], f'Reply with exactly "{token}".')

        for session in self.sessions:
            wait_for(
                f"{session['sid']} 首轮回复",
                lambda s=session: self.assistant_contains(s, s["boot"]),
                timeout=120, interval=0.25)
        self.ok("所有隔离会话都完成首轮真实 Haiku 往返")

        rows = self.get("/api/sessions?force=1")["sessions"]
        by_sid = {str(row.get("sid")): row for row in rows if row.get("source") == "claude"}
        for session in self.sessions:
            row = by_sid.get(session["sid"])
            if not row:
                raise AssertionError(f"agenthub 未索引 {session['sid']}")
            session["uid"] = row["uid"]
        self.ok("6 个 Claude JSONL 都已与 agenthub 会话精确关联")

    def assert_haiku(self):
        usage = self.usage()
        if usage["models"] != [HAIKU_MODEL]:
            raise AssertionError(f"实际模型不是 Haiku，立即停止付费 monkey：{usage}")
        self.ok("JSONL 确认实际推理模型是 Haiku，而不只看终端标签")

    def messages(self, session: dict):
        path = "/api/messages/" + urllib.parse.quote(session["uid"], safe="")
        return self.get(path)

    @staticmethod
    def assistant_contains(session: dict, token: str):
        if not session["path"].is_file():
            return False
        for line in session["path"].read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            content = (row.get("message") or {}).get("content")
            if isinstance(content, str) and token in content:
                return True
            if isinstance(content, list) and any(
                    isinstance(part, dict) and token in str(part.get("text") or "")
                    for part in content):
                return True
        return False

    def body(self, session: dict, text: str, request_id: str):
        current = self.messages(session)
        return {
            "uid": session["uid"], "name": session["name"], "text": text,
            "media": [], "request_id": request_id,
            "page_id": f"monkey-{self.run_id}",
            "cursor": {
                "start": current["end"], "head": current["version"]["head"],
                "anchor": current["anchor"],
            },
        }

    def wait_message_once(self, session: dict, text: str, timeout=120):
        def count():
            rows = self.messages(session)["messages"]
            return sum(row.get("role") == "user" and row.get("text") == text
                       for row in rows)

        result = wait_for(f"原生记录 {text}", lambda: count() == 1, timeout=timeout,
                          interval=0.2)
        assert result
        time.sleep(0.4)
        assert count() == 1, f"消息出现多次：{text}"

    def wait_outbox_empty(self, session: dict, timeout=120):
        path = "/api/session/outbox?uid=" + urllib.parse.quote(session["uid"], safe="")
        wait_for(f"{session['uid']} outbox 清空",
                 lambda: self.get(path).get("outbox") == [], timeout=timeout,
                 interval=0.2)

    def server_delivery_matrix(self):
        # 每个会话各走一次服务端账本；快速切换期间也不能依赖浏览器定时器。
        for session in self.sessions:
            text = f"Reply exactly ACK-{self.run_id}-{session['i']}"
            session["ack_prompt"] = text
            body = self.body(session, text, f"ack-{self.run_id}-{session['i']}")
            self.post("/api/session/send", body)

        for session in self.sessions:
            self.wait_message_once(session, session["ack_prompt"])
            self.wait_outbox_empty(session)
            wait_for(f"ACK 回复 {session['i']}",
                     lambda s=session: self.assistant_contains(
                         s, f"ACK-{self.run_id}-{s['i']}"), timeout=120,
                     interval=0.25)
        self.ok("6 个会话的服务端发送均由 Claude 原生 user 记录确认且只出现一次")

        # 真正丢掉第一次 HTTP 响应，再以同一 request_id 重试。
        session = self.sessions[0]
        text = f"Reply exactly LOST-RESPONSE-{self.run_id}"
        request_id = f"lost-{self.run_id}"
        body = self.body(session, text, request_id)
        self.drop_response("/api/session/send", {**body, "_build": self.build})
        time.sleep(0.3)
        repeated = self.post("/api/session/send", body)
        assert repeated.get("ok") is True
        self.wait_message_once(session, text)
        self.wait_outbox_empty(session)
        wait_for("丢响应请求的 Claude 回复",
                 lambda: self.assistant_contains(
                     session, f"LOST-RESPONSE-{self.run_id}"), timeout=120,
                 interval=0.25)
        self.ok("HTTP 响应真实丢失后，同 request_id 重试没有二次注入")

        # 忙时连续输入，第二条应由 Claude 自己排队，最终仍各提交一次。
        session = self.sessions[1]
        first = (f"Print the integers 1 through 80, one per line, then print "
                 f"BUSY-DONE-{self.run_id}.")
        second = f"After that, reply exactly QUEUED-ACK-{self.run_id}"
        self.post("/api/session/send", self.body(session, first, f"busy-{self.run_id}"))
        time.sleep(0.15)
        self.post("/api/session/send", self.body(session, second, f"queued-{self.run_id}"))
        self.wait_message_once(session, first, timeout=180)
        self.wait_message_once(session, second, timeout=180)
        self.wait_outbox_empty(session, timeout=180)
        wait_for("Claude 原生队列的最终回复",
                 lambda: self.assistant_contains(
                     session, f"QUEUED-ACK-{self.run_id}"), timeout=180,
                 interval=0.25)
        self.ok("Claude 忙时连续发送不会消失，排队输入最终只提交一次")

    def drop_response(self, path: str, body: dict):
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("丢响应故障注入只允许本机 HTTP 测试服务")
        payload = json.dumps(body, ensure_ascii=False).encode()
        target = (parsed.path.rstrip("/") + path) or "/"
        request = (
            f"POST {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + payload
        sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=10)
        sock.sendall(request)
        sock.close()  # 服务端已收到请求体，但客户端故意不读取响应。

    def browser_monkey(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.base, wait_until="networkidle")
            page.wait_for_function(
                "ids => ids.every(id => S.sessions.some(s => s.uid === id))",
                arg=[s["uid"] for s in self.sessions], timeout=60000)

            sizes = [(430, 760), (900, 600), (1366, 768), (1024, 520)]
            for cycle in range(3):
                for at, session in enumerate(self.sessions):
                    page.evaluate("uid => openSession(uid)", session["uid"])
                    page.wait_for_function("uid => S.sel === uid",
                                           arg=session["uid"])
                    width, height = sizes[(cycle + at) % len(sizes)]
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(40)

            # 一条消息真实从网页 composer 发出，并立即切走再切回。
            target = self.sessions[2]
            page.evaluate("uid => openSession(uid)", target["uid"])
            page.wait_for_selector("#composer:not(.hidden)", timeout=30000)
            existing = next((row.get("text") for row in self.messages(target)["messages"]
                             if row.get("role") == "user"
                             and str(row.get("text") or "").startswith(
                                 "Reply exactly UI-SWITCH-")), None)
            ui_text = existing or f"Reply exactly UI-SWITCH-{self.run_id}"
            if not existing:
                page.fill("#cinput", ui_text)
                page.click("#csend")
            page.evaluate("uid => openSession(uid)", self.sessions[3]["uid"])
            page.set_viewport_size({"width": 430, "height": 740})
            page.evaluate("uid => openSession(uid)", target["uid"])
            self.wait_message_once(target, ui_text)
            self.wait_outbox_empty(target)

            # 真实 tmux 视图反复开关并缩放，连接对象不应丢失。
            page.wait_for_selector("#a-term", timeout=30000)
            term_title = page.locator("#a-term").get_attribute("title")
            if term_title != "切换到对话":
                page.click("#a-term")
            page.wait_for_function(
                "T.mode === 'full' && T.ws && T.ws.readyState === 1",
                timeout=30000)
            for width, height in sizes * 2:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(35)
            page.click("#a-term")
            page.wait_for_function("T.mode === 'collapsed'", timeout=30000)
            page.click("#a-term")
            page.wait_for_function(
                "T.mode === 'full' && T.ws && T.ws.readyState === 1",
                timeout=30000)
            assert term.has_session(target["name"])
            assert not errors, errors
            browser.close()
        self.ok("18 次会话切换、反复 resize、终端/对话切换和网页发送均通过")

    def usage(self):
        totals = {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0,
        }
        models = set()
        for session in self.sessions:
            messages = {}
            if not session["path"].is_file():
                continue
            for line in session["path"].read_text(errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") or {}
                if row.get("type") != "assistant" or not message.get("usage"):
                    continue
                key = str(message.get("id") or row.get("uuid") or len(messages))
                messages[key] = message["usage"]
                if message.get("model"):
                    models.add(str(message["model"]))
            for item in messages.values():
                for key in totals:
                    totals[key] += int(item.get(key) or 0)
        return {"models": sorted(models), **totals}

    def cleanup(self, failed: bool):
        for session in self.sessions:
            try:
                if term.has_session(session["name"]):
                    term.kill_session(session["name"])
            except (OSError, RuntimeError):
                pass
        # tmux session 消失略早于 Claude 的退出落盘；立刻删目录会被最后一条
        # shutdown 记录重新建回来。先等全部 pane 退出，再清隔离 transcript。
        try:
            wait_for("monkey tmux 全部退出", lambda: all(
                not term.has_session(session["name"])
                for session in self.sessions), timeout=10, interval=0.05)
            time.sleep(0.25)
        except AssertionError:
            pass
        if failed and self.keep:
            stores = " / ".join(map(str, self.project_stores))
            print(f"保留失败现场：{self.root} / {stores}", flush=True)
            return
        shutil.rmtree(self.root, ignore_errors=True)
        for path in self.project_stores:
            shutil.rmtree(path, ignore_errors=True)

    def run(self):
        failed = True
        try:
            self.start_sessions()
            self.bootstrap()
            self.assert_haiku()
            self.server_delivery_matrix()
            self.browser_monkey()
            usage = self.usage()
            assert usage["models"] == [HAIKU_MODEL], usage
            self.ok("完整 monkey 的 JSONL 计费记录确认未使用 Sonnet/Fable")
            print(json.dumps({
                "run": self.run_id, "checks": len(self.checks), "usage": usage,
            }, ensure_ascii=False, indent=2), flush=True)
            failed = False
        finally:
            self.cleanup(failed)

    def run_browser_only(self):
        failed = True
        try:
            self.resume_sessions()
            self.browser_monkey()
            usage = self.usage()
            assert usage["models"] == [HAIKU_MODEL], usage
            print(json.dumps({
                "run": self.run_id, "checks": len(self.checks), "usage": usage,
                "mode": "browser-only",
            }, ensure_ascii=False, indent=2), flush=True)
            failed = False
        finally:
            self.cleanup(failed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get(
        "AGENTHUB_BASE", "http://127.0.0.1:8710"))
    parser.add_argument("--sessions", type=int, default=6)
    parser.add_argument("--keep-on-failure", action="store_true")
    parser.add_argument("--resume-root", default="",
                        help="复用一次失败 monkey 留下的隔离根目录，只跑浏览器阶段")
    args = parser.parse_args()
    monkey = Monkey(args.base, args.sessions, args.keep_on_failure, args.resume_root)
    if args.resume_root:
        monkey.run_browser_only()
    else:
        monkey.run()


if __name__ == "__main__":
    main()
