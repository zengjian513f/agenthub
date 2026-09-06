"""agenthub —— Claude / Codex / Grok 会话统一浏览服务 (纯标准库)。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import (audit, bug_report, claude_bridge, claude_queue, codex_bridge,
               debug_runs, index, live, media, pending as pending_store,
               send_protocol, send_queue,
               session_meta, term, term_ownership, trash, wsock)

STATIC = Path(__file__).parent / "static"
ASSET_VERSION = hashlib.sha256(b"".join(
    (STATIC / name).read_bytes()
    for name in ("style.css", "cli.js", "app.js", "term.js")
)).hexdigest()[:12]
HOSTNAME = socket.gethostname().strip() or "localhost"
ALLOWED_IPS: set[str] = set()
ALLOWED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
TERMINAL = False        # 远程终端 = 远程执行, 必须显式 --terminal 打开
WATCH_POLL = 0.05       # 服务端盯文件的间隔; stat 一个文件是微秒级, 这里很便宜
JSON_GZIP_MIN = 1024    # 小响应省不了多少，避免反而增加压缩 CPU 和头部体积
JSON_GZIP_LEVEL = 4     # 实测 4.5 MiB → 1.20 MiB / 75 ms，继续加级收益很小
ATTACHMENT_MAX_BYTES = 512 * 1024 * 1024
ATTACHMENT_DIR = "agenthub_attachments"
ATTACHMENT_DIR_LOCK = threading.Lock()
OUTBOX_WAKE = threading.Event()
TERM_OWNERS = term_ownership.Registry()
_CLAUDE_CONFIRM_REPLAY_AT: dict[str, float] = {}


def _add_allowed(value: str) -> None:
    """Add an exact address or CIDR network to the access allowlist."""
    value = value.strip()
    if not value:
        return
    if "/" in value:
        ALLOWED_NETWORKS.append(ipaddress.ip_network(value, strict=False))
    else:
        ALLOWED_IPS.add(value)


def _ip_allowed(value: str) -> bool:
    if value in ALLOWED_IPS:
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in ALLOWED_NETWORKS)


class _TerminalConnection:
    """Revocable WebSocket transport bound to one ownership lease."""

    def __init__(self, sock, stop: threading.Event):
        self.sock = sock
        self.stop = stop
        self.closed = threading.Event()
        self.send_lock = threading.Lock()
        self.replaced = False

    def send(self, payload: bytes, opcode: int) -> None:
        with self.send_lock:
            wsock.send(self.sock, payload, opcode)

    def revoke(self, new_ip: str, notify: bool = True) -> None:
        self.replaced = True
        self.stop.set()
        try:
            with self.send_lock:
                if notify:
                    payload = json.dumps({"t": "revoked", "ip": new_ip},
                                         ensure_ascii=False).encode()
                    wsock.send(self.sock, payload, wsock.OP_TEXT)
                wsock.close(self.sock, 4001,
                            f"revoked:{new_ip}" if notify else "replaced")
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _claude_prompt(session_id: str, messages: list[dict]) -> dict | None:
    """保留实时题卡，直到对应原生回答确实进入会话记录。"""
    prompt = claude_bridge.prompt(session_id)
    if not prompt:
        return prompt
    tool_id = str(prompt.get("id") or "")
    # Claude 2.1.226 在网页按 Esc 拒绝 AskUserQuestion 时，不保证发出
    # PostToolUseFailure hook；但匹配 tool_use_id 的失败 tool_result 一定会落盘。
    # ID 在单次会话内唯一，所以即便 hook 状态仍是 waiting，也应以记录为准。
    answered = tool_id and any(
        str(message.get("call_id") or "") == tool_id
        and message.get("role") in {"answer", "tool_result"}
        for message in messages
    )
    if answered:
        claude_bridge.clear(session_id, tool_id)
        return None
    return prompt


def _after_terminal_keys(uid: str, keys: list[str]) -> dict | None:
    """特殊键发出后唤醒相关后台工作，但不篡改 CLI 自己的队列语义。"""
    # Esc 只负责中断当前 Codex 回合。待发送项必须继续留在持久队列；
    # _poll_outbox 看到原生 aborted/idle 后才会放行队首。
    if str(uid or "").startswith("codex:") and "Escape" in keys:
        OUTBOX_WAKE.set()
    if str(uid or "").startswith("claude:") and "Escape" in keys:
        # Claude 若在第一条 assistant 记录出现前被 Esc，中断不会落进 JSONL。
        # 持久记录这次显式操作，避免尾部孤立 user 永远被推断成 working。
        return session_meta.stop_activity(uid)
    return None


def _resolve_activity(uid: str, result: dict) -> dict:
    resolved = session_meta.resolve_activity(uid, result.get("activity"))
    if resolved != result.get("activity"):
        result["activity"] = resolved
        result["activity_changed"] = True
    return result


def _poll_outbox() -> None:
    """即使浏览器断开，也独立从原生记录追踪完成与接收事件。"""
    for item in claude_queue.tracked():
        uid = str(item.get("uid") or "")
        item_id = str(item.get("id") or "")
        s = index.get(uid)
        if not s or s.get("source") != "claude":
            continue
        try:
            # watch 游标会持续前移。提交超过确认期限后，定期从实际注入前的
            # 固定游标复核，避免一次解析/匹配遗漏永久制造“等待 Claude 确认”。
            now = time.time()
            submitted_at = float(item.get("submitted_at")
                                 or item.get("injecting_at") or now)
            overdue = (
                item.get("state") in {"injecting", "submitted", "ambiguous"}
                and now - submitted_at >= claude_queue.CONFIRM_TIMEOUT
                and item.get("confirm_start") is not None)
            last_replay = _CLAUDE_CONFIRM_REPLAY_AT.get(item_id, 0.0)
            replay = (overdue
                      and now - last_replay >= claude_queue.CONFIRM_TIMEOUT)
            if replay:
                _CLAUDE_CONFIRM_REPLAY_AT[item_id] = now
            prefix = "confirm" if replay else "watch"
            start = int(item.get(f"{prefix}_start") or 0)
            head = str(item.get(f"{prefix}_head") or "")
            anchor = str(item.get(f"{prefix}_anchor") or "")
            if head and anchor:
                result = index.messages_for(s, start=start, head=head, anchor=anchor)
            else:
                result = index.messages_for(s, append_only=True)
            cursor = {
                "start": result["end"],
                "head": result["version"]["head"],
                "anchor": result["anchor"],
                "reset": result.get("reset", False),
            }
            claude_queue.observe(
                uid, result["messages"], result.get("activity"), cursor)
            if not any(row.get("id") == item_id for row in claude_queue.tracked()):
                _CLAUDE_CONFIRM_REPLAY_AT.pop(item_id, None)
        except (OSError, ValueError, KeyError):
            continue

    for item in send_queue.tracked():
        uid = str(item.get("uid") or "")
        s = index.get(uid)
        if not s or s.get("source") != "codex":
            continue
        try:
            # 正常情况下从持续前移的 watch 游标读。到确认期限时，改从实际
            # 注入前的固定游标持续复核；即使 compact 延迟 user 记录或某次
            # 解析遗漏，也不能把已经写入终端的消息重新暴露为可重试。
            replay = (item.get("state") in {"delivering", "confirming"}
                      and time.time() - float(item.get("delivered_at") or 0)
                      >= send_queue.CONFIRM_TIMEOUT)
            prefix = ("confirm" if replay and item.get("confirm_start") is not None
                      else "watch")
            start = int(item.get(f"{prefix}_start") or 0)
            head = str(item.get(f"{prefix}_head") or "")
            anchor = str(item.get(f"{prefix}_anchor") or "")
            if head and anchor:
                result = index.messages_for(s, start=start, head=head, anchor=anchor)
            else:
                # 没有浏览器续读点时只在当前 EOF 建基线，不能为此整读几十 MB。
                result = index.messages_for(s, append_only=True)
            cursor = {
                "start": result["end"],
                "head": result["version"]["head"],
                "anchor": result["anchor"],
            }
            send_queue.observe(uid, result["messages"], result.get("activity"),
                               cursor=cursor)
        except (OSError, ValueError, KeyError):
            # 文件可能正处于切换/追加的瞬间；下一轮继续，不能把瞬态读失败
            # 伪装成“发送失败”。
            continue


def _deliver_outbox_item(item: dict, panes: list[dict]) -> None:
    """Safely deliver one ready Codex item into a genuinely empty composer."""
    s = index.get(str(item.get("uid") or ""))
    pane = _pane_for_session(s, panes) if s and s.get("source") == "codex" else None
    if not pane:
        send_queue.mark_failed(item["id"], "Codex tmux 会话已断开")
        return
    name = pane["name"]
    claimed = False
    try:
        # task_complete 写盘到 TUI 真正回到输入框仍有一个很短的重绘窗口。
        # 连续两帧终端文本一致才注入，避开本次事故中的 15ms 状态切换。
        before = term.capture_screen_state(name)
        time.sleep(0.08)
        if before != term.capture_screen_state(name):
            send_queue.defer(item["id"])
            return
        screen, cursor = before
        composer = codex_bridge.composer_state(screen, cursor)
        if composer == "editing":
            # 双 Esc 回退失败时 Codex 会把旧 prompt 留在编辑框里。绝不能
            # 清空用户草稿，也不能把新消息粘到它后面形成一条拼接消息。同时
            # resize/重连会短暂留下历史 › 行；同一画面连续稳定数帧后才报错。
            signature = hashlib.sha256(
                f"{cursor[0]}\0{cursor[1]}\0{screen}".encode("utf-8", "replace")
            ).hexdigest()
            send_queue.defer_editing(item["id"], signature)
            return
        if composer == "unknown" and str(item.get("activity_state") or "") in {
                "idle", "aborted", "failed"}:
            # Never mutate an unrecognised TUI with Ctrl+L: current Codex treats it as
            # clear-screen, making an existing conversation look like a new one.
            # resize、切换会话和 TUI 重绘都会短暂产生这种帧；先退避重试，连续
            # 多次仍无法识别才保留为可人工处理的失败项。
            if (codex_bridge.busy_screen(screen)
                    or codex_bridge.approval_prompt(screen)):
                send_queue.defer(item["id"])
                return
            send_queue.defer_unrecognized(item["id"])
            return
        if composer != "empty":
            # 审批、选择题及重绘中的画面都是瞬态状态，等待真正 Ready。
            send_queue.defer(item["id"])
            return
        # 用户可能在 ready() 与这里之间撤销排队项。状态切换失败时
        # 绝不能继续向 tmux 注入已经撤掉的正文。
        if not send_queue.mark_delivering(item["id"]):
            return
        claimed = True
        term.leave_copy_mode(name)
        term.submit_text(name, str(item.get("text") or ""))
    except Exception as e:
        if claimed:
            send_queue.mark_confirming(
                item["id"], f"终端写入状态待核对: {e}")
        else:
            send_queue.mark_failed(item["id"], str(e))


def _deliver_claude_item(item: dict, panes: list[dict]) -> None:
    """Resume only the state that proves no terminal write has begun yet."""
    uid = str(item.get("uid") or "")
    s = index.get(uid)
    pane = _pane_for_session(s, panes) if s and s.get("source") == "claude" else None
    if not pane or pane["name"] != str(item.get("name") or ""):
        return
    # Handler and background loop can see the same persisted row. Exactly one
    # process path may change it to injecting; all others become status readers.
    claimed = claude_queue.mark_injecting(str(item.get("id") or ""), uid)
    if not claimed:
        return
    try:
        term.leave_copy_mode(pane["name"])
        term.submit_text(pane["name"], str(item.get("text") or ""))
        claude_queue.mark_submitted(str(item.get("id") or ""), uid)
    except Exception as error:
        # Once the atomic claim is persisted, the exact crash point is unknown;
        # never turn it back into a retryable row.
        claude_queue.mark_ambiguous(
            str(item.get("id") or ""), uid, str(error))


def _outbox_loop() -> None:
    """交付可证明安全的 Claude persisted 项和 Codex 队首消息。"""
    while True:
        _poll_outbox()
        send_queue.expire_deliveries()
        claude_ready = claude_queue.ready()
        codex_ready = send_queue.ready()
        if not claude_ready and not codex_ready:
            OUTBOX_WAKE.wait(0.5)
            OUTBOX_WAKE.clear()
            continue
        try:
            panes = term.list_sessions()
        except Exception:
            OUTBOX_WAKE.wait(0.5)
            OUTBOX_WAKE.clear()
            continue
        for item in claude_ready:
            _deliver_claude_item(item, panes)
        for item in codex_ready:
            _deliver_outbox_item(item, panes)


def _sessions_signature(index_sig: str | None = None) -> str:
    """原生会话与 agenthub 自有元数据共同决定列表版本。"""
    return f"{index.signature() if index_sig is None else index_sig}:{session_meta.signature()}"


def _debug_run(q: dict) -> str:
    return str(q.get("debug_run", [""])[0] or "")[:64]


def _view_signature(rows: list[dict], run_id: str) -> str:
    """Hidden monkey writes must not make the user's ordinary list churn."""
    payload = json.dumps({"debug_run": run_id, "sessions": rows},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _pane_for_session(session: dict, panes: list[dict],
                      pids: list[int] | None = None) -> dict | None:
    """按规范名或真实进程树找会话所在的 agenthub tmux pane。

    Codex 双 Esc 会换新 UUID，但进程仍留在旧 UUID 命名的 tmux session 中；
    这时名称不再可靠，pane 祖先进程才是权威关联。
    """
    name = term.session_name_for(session["source"], session["sid"])
    exact = next((pane for pane in panes if pane["name"] == name), None)
    if exact:
        return exact
    pids = live.pids_of(session) if pids is None else pids
    return next((pane for pane in panes if pane.get("owned") and any(
        pid > 0 and term.process_belongs_to(pid, pane["pid"]) for pid in pids
    )), None)


def _codex_prompt(session: dict, pane_name: str = "") -> dict | None:
    """Read a Codex approval that exists only on the live TUI screen."""
    if session.get("agent_id") or session.get("source") != "codex":
        return None
    try:
        if not pane_name:
            pane = _pane_for_session(session, term.list_sessions())
            pane_name = str(pane.get("name") or "") if pane else ""
        if not pane_name:
            return None
        return codex_bridge.approval_prompt(term.capture_plain(pane_name, 80))
    except (OSError, RuntimeError):
        # Pane disappearance is a normal race while a session exits.
        return None


def _session_prompt(session: dict, messages: list[dict],
                    codex_pane: str = "") -> dict | None:
    """Return the selected CLI's live prompt through one common interface."""
    if session.get("agent_id"):
        return None
    if session.get("source") == "claude":
        return _claude_prompt(str(session.get("sid") or ""), messages)
    if session.get("source") == "codex":
        return _codex_prompt(session, codex_pane)
    return None


def _accepts_gzip(value: str) -> bool:
    """按 RFC 的 q 值判断客户端是否接受 gzip；显式 gzip;q=0 优先于通配符。"""
    exact = wildcard = None
    for raw in value.lower().split(","):
        parts = [p.strip() for p in raw.split(";")]
        coding = parts[0]
        if not coding:
            continue
        quality = 1.0
        for param in parts[1:]:
            key, sep, val = param.partition("=")
            if sep and key.strip() == "q":
                try:
                    quality = float(val.strip())
                except ValueError:
                    quality = 0.0
        if coding == "gzip":
            exact = quality > 0
        elif coding == "*":
            wildcard = quality > 0
    return exact if exact is not None else bool(wildcard)


class Handler(BaseHTTPRequestHandler):
    server_version = "agenthub"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----------------------------------------------------
    # 这些是秒级轮询, 打出来只会淹没真正有用的日志
    QUIET = ("/api/live", "/api/term/list", "/api/term/attach", "sig=", "start=")

    def log_message(self, format, *args):
        line = str(args[0])
        if "/api/" in line and not any(q in line for q in self.QUIET):
            print(f"[{self.address_string()}] {format % args}")

    def _allowed(self) -> bool:
        ip = self._client_ip()
        return _ip_allowed(ip)

    def _client_ip(self) -> str:
        """Actual TCP peer address; proxy headers never participate in identity."""
        ip = str(self.client_address[0])
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        return ip

    def _display_ip(self) -> str:
        """Best available client IP for ownership prompts only.

        Reverse-proxy headers are deliberately excluded from authorization and
        lease identity.  An invalid/spoofed value can at most affect this label.
        """
        forwarded = (self.headers.get("X-Real-IP", "").strip()
                     or self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip())
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return self._client_ip()

    def _audit_begin(self, method: str, path: str) -> None:
        """Start one HTTP trace without allowing diagnostics to affect routing."""
        self._audit_started = time.monotonic()
        self._audit_method = str(method or "")
        self._audit_path = str(path or "")
        self._audit_trace_id = str(
            self.headers.get("X-AgentHub-Trace", "") if self.headers else "")[:128]
        self._audit_page_id = str(
            self.headers.get("X-AgentHub-Page", "") if self.headers else "")[:128]
        self._audit_build = str(
            self.headers.get("X-AgentHub-Build", "") if self.headers else "")[:128]
        inferred_uid = (unquote(path[len("/api/messages/"):])
                        if path.startswith("/api/messages/") else
                        unquote(path[len("/api/session/"):])
                        if method == "DELETE" and path.startswith("/api/session/") else "")
        self._audit_uid = inferred_uid[:512]
        self._audit_source = self._audit_uid.partition(":")[0]
        self._audit_request_id = ""
        self._audit_response_done = False
        self._audit_json_response = None
        audit.record(
            "http.request.received", category="http",
            uid=self._audit_uid, source=self._audit_source,
            trace_id=self._audit_trace_id, page_id=self._audit_page_id,
            build=self._audit_build,
            data={
                "method": self._audit_method, "path": self._audit_path,
                "content_length": self.headers.get("Content-Length", "")
                if self.headers else "",
                "content_type": self.headers.get("Content-Type", "")
                if self.headers else "",
                "peer_ip": self._client_ip(), "display_ip": self._display_ip(),
            },
        )

    def _audit_body(self, body: dict) -> None:
        if not isinstance(body, dict):
            return
        self._audit_trace_id = str(body.get("_trace_id")
                                   or getattr(self, "_audit_trace_id", ""))[:128]
        self._audit_page_id = str(body.get("page_id") or body.get("_page_id")
                                  or getattr(self, "_audit_page_id", ""))[:128]
        self._audit_build = str(body.get("_build")
                                or getattr(self, "_audit_build", ""))[:128]
        self._audit_uid = str(body.get("uid") or "")[:512]
        self._audit_source = self._audit_uid.partition(":")[0]
        self._audit_request_id = str(body.get("request_id")
                                     or body.get("_request_id") or "")[:128]
        audit.record(
            "http.request.body", category="http", uid=self._audit_uid,
            source=self._audit_source, trace_id=self._audit_trace_id,
            request_id=self._audit_request_id, page_id=self._audit_page_id,
            build=self._audit_build,
            data={"method": getattr(self, "_audit_method", ""),
                  "path": getattr(self, "_audit_path", ""),
                  "keys": sorted(str(key) for key in body)},
            # The telemetry endpoint expands its bounded event batch below; do
            # not store the same browser snapshot a second time as an HTTP body.
            content=None if getattr(self, "_audit_path", "") == "/api/audit/browser"
            else body,
        )

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        delivered = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            delivered = False
        if (hasattr(self, "_audit_started")
                and not getattr(self, "_audit_response_done", False)):
            self._audit_response_done = True
            response_content = getattr(self, "_audit_json_response", None)
            audit.record(
                "http.response.sent" if delivered else "http.response.write_failed",
                category="http",
                severity="error" if code >= 500 else "warning" if code >= 400 else "info",
                uid=getattr(self, "_audit_uid", ""),
                source=getattr(self, "_audit_source", ""),
                trace_id=getattr(self, "_audit_trace_id", ""),
                request_id=getattr(self, "_audit_request_id", ""),
                page_id=getattr(self, "_audit_page_id", ""),
                build=getattr(self, "_audit_build", ""),
                data={
                    "method": getattr(self, "_audit_method", ""),
                    "path": getattr(self, "_audit_path", ""), "status": code,
                    "bytes": len(body), "content_type": ctype,
                    "delivered": delivered,
                    "content_encoding": (extra or {}).get("Content-Encoding", ""),
                    "duration_ms": round(
                        (time.monotonic() - self._audit_started) * 1000, 3),
                },
                content=response_content,
            )

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        decoded_length = len(body)
        # Full small responses are invaluable for distinguishing a server reply
        # from what the browser later rendered. Large history windows keep only
        # their byte/count metadata and are represented by parser/SSE events.
        self._audit_json_response = obj if len(body) <= 512 * 1024 else None
        # Fetch 会把 gzip 解压后的字节交给 ReadableStream，但保留压缩后
        # Content-Length。单独传递解压长度，让前端进度的分子分母同口径。
        headers = {
            "Vary": "Accept-Encoding",
            "X-AgentHub-Decoded-Length": str(decoded_length),
        }
        if len(body) >= JSON_GZIP_MIN and _accepts_gzip(
                self.headers.get("Accept-Encoding", "")):
            packed = gzip.compress(body, compresslevel=JSON_GZIP_LEVEL, mtime=0)
            if len(packed) < len(body):
                body = packed
                headers["Content-Encoding"] = "gzip"
        self._send(code, body, "application/json; charset=utf-8", headers)

    # ---- 路由 --------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        self._audit_begin("POST", u.path)
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        if u.path in {"/api/session/star", "/api/audit/browser",
                      "/api/bug-report", "/api/trash/restore",
                      "/api/trash/purge"}:
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4 * 1024 * 1024:
                    return self._json({"error": "request too large"}, 413)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"error": "bad body"}, 400)
            self._audit_body(body)
            if u.path == "/api/session/star":
                return self._star_session(body)
            if u.path == "/api/audit/browser":
                return self._browser_audit(body)
            if u.path == "/api/trash/restore":
                return self._restore_trash(body)
            if u.path == "/api/trash/purge":
                return self._purge_trash(body)
            return self._bug_report(body)
        if not TERMINAL:
            return self._json({"error": "终端未启用, 服务端需加 --terminal"}, 403)
        if u.path == "/api/session/attachment":
            audit.record(
                "attachment.upload.started", category="attachment",
                trace_id=getattr(self, "_audit_trace_id", ""),
                page_id=getattr(self, "_audit_page_id", ""),
                data={"query": parse_qs(u.query),
                      "bytes": self.headers.get("Content-Length", "")},
            )
            try:
                return self._upload_attachment(parse_qs(u.query))
            except (KeyError, ValueError) as e:
                self.close_connection = True
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                self.close_connection = True
                return self._json({"error": str(e)}, 500)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad body"}, 400)
        self._audit_body(body)
        text_write = (u.path in {"/api/session/send", "/api/session/outbox/retry"}
                      or (u.path == "/api/term/send" and not body.get("keys")
                          and bool(body.get("text"))))
        if text_write and str(body.get("_build") or "") != ASSET_VERSION:
            # A tab can survive deployments for days.  Old code used a local
            # eight-second guess and exposed a blind retry that duplicated prompts.
            # Reject before touching tmux; even old clients will surface this error
            # and keep their editor text intact.
            return self._json({
                "error": "页面版本已过期，请重新加载整个网页后再发送",
                "reload": True,
                "build": ASSET_VERSION,
            }, 409)
        try:
            if u.path == "/api/session/send":
                return self._queue_message(body)
            if u.path == "/api/session/draft-status":
                return self._draft_status(body)
            if u.path == "/api/session/outbox/retry":
                return self._retry_message(body)
            if u.path == "/api/session/outbox/discard":
                return self._discard_message(body)
            if u.path == "/api/session/rewind":
                return self._claude_rewind(body)
            if u.path == "/api/term/create":
                return self._create_session(body)
            if u.path == "/api/term/claim":
                name = str(body.get("name") or "")
                if not any(x["name"] == name for x in term.list_sessions()):
                    return self._json({"error": "tmux 会话不存在"}, 404)
                result = TERM_OWNERS.claim(
                    name, str(body.get("page") or ""), self._display_ip(),
                    bool(body.get("force")))
                return self._json(result, 409 if result.get("conflict") else 200)
            if u.path == "/api/term/kill":
                term.kill_session(body["name"])
                pending_store.discard(body["name"])
                return self._json({"ok": True})
            if u.path == "/api/session/stop":
                return self._stop_session(body)
            if u.path == "/api/term/takeover":
                return self._takeover(body)
            if u.path == "/api/term/scroll":
                name = body["name"]
                if not any(x["name"] == name for x in term.list_sessions()):
                    return self._json({"error": "会话不存在"}, 404)
                if body.get("cancel"):          # 直接回到实时画面
                    term.leave_copy_mode(name)
                    return self._json({"pos": 0})
                at = term.scroll(name, bool(body.get("up")), int(body.get("lines", 3)))
                return self._json({"pos": at})
            if u.path == "/api/term/send":
                name = body["name"]
                if not any(x["name"] == name for x in term.list_sessions()):
                    return self._json({"error": "会话不存在"}, 404)
                term.leave_copy_mode(name)       # 正在翻历史的话先回到实时画面
                if body.get("keys"):                 # 特殊键: Enter / Escape / C-c …
                    term.send_keys(name, *body["keys"])
                    activity = _after_terminal_keys(
                        str(body.get("uid") or ""), body["keys"])
                else:
                    activity = None
                    text = body.get("text", "")
                    enter = body.get("enter", True)
                    if text and enter:
                        term.submit_text(name, text)
                    elif text:
                        term.send_text(name, text)
                    elif enter:
                        term.send_keys(name, "Enter")
                response = {"ok": True}
                if activity:
                    response["activity"] = activity
                return self._json(response)
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)

    @staticmethod
    def _attachment_name(raw: str) -> str:
        """保留可读文件名，但不能让名称参与路径解析或突破文件系统上限。"""
        name = Path(str(raw).replace("\\", "/")).name.strip(" .")
        name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name)
        name = re.sub(r"\s+", " ", name)
        if not name or name in {".", ".."}:
            name = "attachment"
        suffix = Path(name).suffix[:20]
        stem = name[:-len(suffix)] if suffix else name
        while len(stem.encode("utf-8")) > 150:
            stem = stem[:-1]
        return (stem or "attachment") + suffix

    @staticmethod
    def _same_file_content(left: Path, right: Path) -> bool:
        """同名附件内容相同就复用，逐块比较避免把大文件读进内存。"""
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                ac = a.read(1024 * 1024)
                bc = b.read(1024 * 1024)
                if ac != bc:
                    return False
                if not ac:
                    return True

    def _upload_attachment(self, q: dict):
        """把一个原始二进制附件流写入会话 cwd 下的受控子目录。"""
        uid = str(q.get("uid", [""])[0])
        original = self._attachment_name(q.get("name", ["attachment"])[0])
        requested_id = str(q.get("id", [""])[0]).strip()
        if requested_id and not re.fullmatch(r"[1-9]\d{0,8}", requested_id):
            return self._json({"error": "附件目录编号无效"}, 400)
        # 新建 CLI 在写出第一条正式会话记录前只有 tmux 名，没有普通 uid。
        # pending 记录同样由服务端创建并保存可信 cwd，允许它先接收附件。
        session = None
        if uid.startswith("tmux:"):
            pending = pending_store.get(uid.removeprefix("tmux:"))
            if pending:
                session = {"cwd": pending.get("cwd")}
        else:
            session = index.get(uid)
        if not session:
            return self._json({"error": "会话不存在"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if size <= 0:
            return self._json({"error": "附件为空或缺少 Content-Length"}, 400)
        if size > ATTACHMENT_MAX_BYTES:
            self.close_connection = True
            return self._json({"error": "单个附件不能超过 512 MB"}, 413)

        cwd = Path(str(session.get("cwd") or "")).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            return self._json({"error": "会话当前目录不存在"}, 409)
        cwd = cwd.resolve()
        root = cwd / ATTACHMENT_DIR
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            return self._json({"error": f"{ATTACHMENT_DIR} 不是安全目录"}, 409)
        root.mkdir(mode=0o700, exist_ok=True)
        if requested_id:
            attachment_id = requested_id
            base = root / attachment_id
            if base.exists() and (base.is_symlink() or not base.is_dir()):
                return self._json({"error": "附件编号对应的不是安全目录"}, 409)
            base.mkdir(mode=0o700, exist_ok=True)
        else:
            # 正式消息编号在上传时尚未产生，因此按项目目录从 1 递增分配批次号。
            # mkdir(exist_ok=False) 与锁共同保证多个网页并发发送时不会撞号。
            with ATTACHMENT_DIR_LOCK:
                used = [int(entry.name) for entry in root.iterdir()
                        if entry.is_dir() and re.fullmatch(r"[1-9]\d{0,8}", entry.name)]
                attachment_id = str(max(used, default=0) + 1)
                base = root / attachment_id
                base.mkdir(mode=0o700, exist_ok=False)
        stamp = f"{time.strftime('%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
        temp = base / f".{stamp}-{threading.get_ident()}.upload"
        left = size
        target = None
        reused = False
        try:
            with temp.open("xb") as out:
                while left:
                    chunk = self.rfile.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise OSError("附件传输中断")
                    out.write(chunk)
                    left -= len(chunk)
            temp.chmod(0o600)

            suffix = Path(original).suffix
            stem = original[:-len(suffix)] if suffix else original
            # 第一个使用原文件名；同名但内容不同时用 __N，便于命令行引用。
            for number in range(10_000):
                name = original if number == 0 else f"{stem}__{number}{suffix}"
                candidate = base / name
                if candidate.is_symlink():
                    continue
                try:
                    exists = candidate.exists()
                    if exists and candidate.is_file() and self._same_file_content(temp, candidate):
                        target = candidate
                        reused = True
                        break
                    if exists:
                        continue
                    # hard link 带 O_EXCL 语义：并发上传不能覆盖刚创建的同名文件。
                    os.link(temp, candidate)
                    target = candidate
                    break
                except FileExistsError:
                    continue
            if target is None:
                raise OSError("同名附件过多，无法分配文件名")
        finally:
            temp.unlink(missing_ok=True)

        supplied = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_type(original)[0]
        mime = guessed or (supplied if re.fullmatch(r"[\w.+-]+/[\w.+-]+", supplied) else None)
        mime = mime or "application/octet-stream"
        kind = mime.split("/", 1)[0] if mime.split("/", 1)[0] in {"image", "video", "audio"} else "file"
        preview = media.register_path(str(target), name=target.name) if kind == "image" else None
        return self._json({
            "ok": True, "name": target.name, "original_name": original, "path": str(target),
            "relative_path": str(target.relative_to(cwd)),
            "attachment_id": attachment_id, "mime": mime, "kind": kind,
            "size": size, "reused": reused, "media": preview,
        })

    def do_GET(self):
        u = urlparse(self.path)
        self._audit_begin("GET", u.path)
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        q = parse_qs(u.query)
        if u.path == "/api/term/attach":
            return self._attach(q)
        if u.path == "/api/watch":
            return self._watch(q)
        try:
            if u.path.startswith("/api/"):
                return self._api_get(u.path, q)
            return self._static(u.path)
        except KeyError:
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        self._audit_begin("DELETE", u.path)
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        if not u.path.startswith("/api/session/"):
            return self._json({"error": "not found"}, 404)
        uid = unquote(u.path[len("/api/session/"):])
        try:
            s = index.get(uid)
            if not s:
                raise KeyError(uid)
            name = term.session_name_for(s["source"], s["sid"])
            if live.is_live(s, force=True) or term.has_session(name):
                return self._json({"error": "请先停止会话"}, 409)
            dest = index.delete(uid)
        except KeyError:
            return self._json({"error": "会话不存在"}, 404)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        try:
            session_meta.discard(uid)
        except OSError:
            pass  # 会话已成功移入回收站，不能把元数据清理失败误报成删除失败
        send_queue.discard_uid(uid)
        claude_queue.discard_uid(uid)
        self._json({"ok": True, "trash": dest})

    def _api_get(self, path: str, q: dict):
        if path == "/api/meta":
            return self._json({"build": ASSET_VERSION, "hostname": HOSTNAME})
        if path == "/api/trash":
            return self._json(trash.summary())
        if path == "/api/sessions":
            force = q.get("force", ["0"])[0] == "1"
            known = q.get("sig", [""])[0]
            # load 是唯一会扫 inventory 的入口；签名必须与这批 rows 属于同一
            # 已发布快照，不能 signature → load → signature 制造 TOCTOU。
            sessions, index_sig, built_at = index.load_snapshot(force=force)
            run_id = _debug_run(q)
            sessions = debug_runs.filter_rows(sessions, run_id)
            rows = session_meta.enrich(index.with_cursors(sessions))
            current_sig = _view_signature(rows, run_id)
            if known and not force and known == current_sig:
                return self._json({"unchanged": True, "sig": known})
            return self._json({"sessions": rows, "sig": current_sig,
                               "built_at": built_at})

        if path == "/api/live":
            force = q.get("force", ["0"])[0] == "1"
            sessions = debug_runs.filter_rows(index.cached(), _debug_run(q))
            uids = live.live_uids(sessions, force=force)
            live_set = set(uids)
            tmux_uids = [s["uid"] for s in sessions
                         if s["uid"] in live_set and term.in_tmux(live.pids_of(s))]
            started_at = {}
            for s in sessions:
                if s["uid"] not in live_set:
                    continue
                value = live.started_at(s)
                if value is not None:
                    started_at[s["uid"]] = value
            return self._json({"uids": uids, "tmux_uids": tmux_uids,
                               "started_at": started_at})

        if path == "/api/term/list":
            tmux_sessions = term.list_sessions() if TERMINAL else []
            run_id = _debug_run(q)
            tmux_sessions = debug_runs.filter_rows(tmux_sessions, run_id)
            if tmux_sessions:
                # 把 tmux pane 映射回当前列表 uid。前端不能只从 pane 名猜 UUID，
                # 因为 Codex 回退分支会沿用父会话启动时的旧名字。
                linked: dict[str, dict] = {}
                for session in debug_runs.filter_rows(index.cached(), run_id):
                    pane = _pane_for_session(session, tmux_sessions)
                    if pane and (pane["name"] not in linked
                                 or session["updated"] > linked[pane["name"]]["updated"]):
                        linked[pane["name"]] = session
                for pane in tmux_sessions:
                    if pane["name"] in linked:
                        pane["uid"] = linked[pane["name"]]["uid"]
            pending = pending_store.active({x["name"] for x in tmux_sessions}) if TERMINAL else []
            pending = debug_runs.filter_rows(pending, run_id)
            public_pending = [{k: row.get(k) for k in
                               ("name", "source", "sid", "cwd", "started",
                                "cols", "rows", "title", "kind", "report_id")}
                              for row in pending]
            return self._json({"enabled": TERMINAL and term.available(),
                               "sources": term.available_sources() if TERMINAL else {},
                               "home": str(Path.home()),
                               "sessions": tmux_sessions,
                               "pending": public_pending})

        if path == "/api/term/complete-dir":
            if not TERMINAL:
                return self._json({"error": "终端未启用"}, 403)
            try:
                directories = term.complete_directories(q.get("path", [""])[0])
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"directories": directories})

        if path == "/api/term/new-status":
            return self._new_session_status(q)

        if path == "/api/session/outbox":
            uid = q.get("uid", [""])[0]
            session = index.get(uid)
            return self._json(send_protocol.snapshot(
                str((session or {}).get("source") or ""), uid))

        if path == "/api/session/input-history":
            uid = q.get("uid", [""])[0]
            agent = q.get("agent", [""])[0]
            session = index.get(uid)
            if not session:
                return self._json({"error": "会话不存在"}, 404)
            result = index.messages_for(index.session_view(session, agent))
            history = [{"text": str(message.get("text") or ""),
                        "ts": message.get("ts")}
                       for message in result["messages"]
                       if message.get("role") in {"user", "command"}
                       and message.get("counted") is not False
                       and str(message.get("text") or "").strip()]
            return self._json({"history": history, "end": result["end"],
                               "version": result["version"],
                               "anchor": result.get("anchor", "")})

        if path.startswith("/api/media/"):
            token = path[len("/api/media/"):]
            got = media.get(token)
            if not got:
                return self._json({"error": "图片不存在或已过期"}, 404)
            data, mime, name = got
            return self._send(200, data, mime, {
                "Cache-Control": "private, max-age=86400, immutable",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'inline; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", name)}"',
            })

        if path == "/api/search":
            # 别叫 term —— 会把模块 term 遮蔽成局部变量, 同函数里的 term.xxx 全废
            query = q.get("q", [""])[0]
            srcs = [s for s in q.get("source", [""])[0].split(",") if s] or None
            on = lambda k: q.get(k, ["0"])[0] == "1"
            if on("progress"):
                return self._search_stream(query, srcs, _debug_run(q), word=on("word"),
                                           case=on("case"), regex=on("regex"))
            try:
                result = index.search(
                    query, srcs, word=on("word"), case=on("case"), regex=on("regex"))
                run_id = _debug_run(q)
                result["results"] = session_meta.enrich(
                    debug_runs.filter_rows(result["results"], run_id))
                result["total_pool"] = len(debug_runs.filter_rows(index.cached(), run_id))
                return self._json(result)
            except re.error as e:
                return self._json({"error": f"正则无效: {e}"}, 400)

        if path.startswith("/api/messages/"):
            uid = unquote(path[len("/api/messages/"):])
            agent = q.get("agent", [""])[0]
            session = index.get(uid)
            if not session:
                raise KeyError(uid)
            result = index.messages_for(
                index.session_view(session, agent),
                start=int(q.get("start", ["0"])[0]),
                head=q.get("head", [""])[0],
                anchor=q.get("anchor", [""])[0],
                append_only=q.get("append", ["0"])[0] == "1",
                windowed=q.get("window", ["0"])[0] == "1",
            )
            _resolve_activity(uid, result)
            if not agent:
                driver = send_protocol.driver_for(str(session.get("source") or ""))
                if driver:
                    cursor = {"start": result["end"],
                              "head": result["version"]["head"],
                              "anchor": result["anchor"],
                              "reset": result.get("reset", False)}
                    if driver.observe(uid, result["messages"], result.get("activity"),
                                      cursor):
                        OUTBOX_WAKE.set()
                    result.update(driver.snapshot(uid))
                result["prompt"] = _session_prompt(session, result["messages"])
            result["meta"] = session_meta.enrich_one(result["meta"])
            return self._json(result)

        raise KeyError(path)

    def _search_stream(self, query: str, sources, run_id: str = "", **opts):
        """以 NDJSON 推送扫描进度，最后一行给出完整搜索结果。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(obj):
            line = json.dumps(obj, ensure_ascii=False).encode() + b"\n"
            self.wfile.write(line)
            self.wfile.flush()

        try:
            result = index.search(
                query, sources, **opts,
                progress=lambda done, total: emit(
                    {"type": "progress", "done": done, "total": total}),
            )
            result["results"] = session_meta.enrich(
                debug_runs.filter_rows(result["results"], run_id))
            result["total_pool"] = len(debug_runs.filter_rows(index.cached(), run_id))
            emit({"type": "result", "data": result})
        except re.error as e:
            emit({"type": "error", "error": f"正则无效: {e}"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"type": "error", "error": f"{type(e).__name__}: {e}"})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _watch(self, q: dict):
        """SSE: 服务端盯着会话文件, 一有变化立刻把 diff 推过去。

        客户端不再需要轮询。推的内容和 /api/messages 的增量完全一样:
        追加就推新增消息, 截断/改写就推整份并带 reset。
        """
        uid = q.get("uid", [""])[0]
        s = index.get(uid)
        if not s:
            return self._send(404, b"no such session", "text/plain")
        try:
            s = index.session_view(s, q.get("agent", [""])[0])
        except KeyError:
            return self._send(404, b"no such agent", "text/plain")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        connection_id = str(q.get("connection", [""])[0])[:128] or uuid.uuid4().hex
        page_id = str(q.get("page", [""])[0])[:128]
        self._audit_uid = uid
        self._audit_source = str(s.get("source") or "")
        self._audit_page_id = page_id
        self._audit_response_done = True
        packet_seq = 0
        opened_at = time.monotonic()

        def emit(obj: dict, kind: str) -> None:
            nonlocal packet_seq
            packet_seq += 1
            packet_id = f"{connection_id}:{packet_seq}"
            packet = {**obj, "_audit": {
                "connection_id": connection_id, "packet_id": packet_id,
                "kind": kind,
            }}
            payload = json.dumps(packet, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()
            audit.record(
                "sse.packet.sent", category="stream", uid=uid,
                source=str(s.get("source") or ""), page_id=page_id,
                connection_id=connection_id,
                trace_id=packet_id, build=getattr(self, "_audit_build", ""),
                data={
                    "packet_id": packet_id, "kind": kind,
                    "reset": bool(obj.get("reset")),
                    "start": obj.get("start"), "end": obj.get("end"),
                    "messages": len(obj.get("messages") or []),
                    "outbox": len(obj.get("outbox") or []),
                    "prompt_only": bool(obj.get("prompt_only")),
                    "outbox_only": bool(obj.get("outbox_only")),
                    "bytes": len(payload.encode()),
                }, content=packet,
            )

        audit.record(
            "sse.connection.opened", category="stream", uid=uid,
            source=str(s.get("source") or ""), page_id=page_id,
            connection_id=connection_id, build=getattr(self, "_audit_build", ""),
            data={"start": q.get("start", ["0"])[0],
                  "agent": q.get("agent", [""])[0]},
        )

        start = int(q.get("start", ["0"])[0])
        head = q.get("head", [""])[0]
        anchor = q.get("anchor", [""])[0]
        last = None
        outbox_driver = (send_protocol.driver_for(str(s.get("source") or ""))
                         if not s.get("agent_id") else None)
        outbox_revision = -1
        claude_sid = (str(s.get("sid") or "")
                      if not s.get("agent_id") and s.get("source") == "claude" else "")
        prompt_revision = claude_bridge.revision(claude_sid) if claude_sid else None
        codex_session = bool(not s.get("agent_id") and s.get("source") == "codex")
        terminal_session = bool(not s.get("agent_id")
                                and s.get("source") in {"claude", "codex"})
        terminal_name = ""
        if terminal_session:
            pane = _pane_for_session(s, term.list_sessions())
            terminal_name = str(pane.get("name") or "") if pane else ""
        codex_pane = terminal_name if codex_session else ""
        codex_prompt = None
        next_codex_prompt_check = 0.0
        next_terminal_check = 0.0
        next_terminal_lookup = 0.0
        terminal_idle_since = 0.0
        terminal_idle_token = None
        handled_terminal_activity = None
        visible_activity = None
        native_activity = None
        terminal_activity_token = None
        terminal_busy_seen = False
        activity_revision = session_meta.activity_revision(uid)
        beat = time.time()
        try:
            while True:
                ver = index.version(s)
                current_prompt_revision = (claude_bridge.revision(claude_sid)
                                           if claude_sid else None)
                if ver != last:
                    last = ver
                    previous_outbox_revision = outbox_revision
                    previous_prompt_revision = prompt_revision
                    previous_codex_prompt = codex_prompt
                    # 用 messages_for 而不是 messages: 后者要过一遍索引,
                    # 而文件刚变过, 签名对不上就会重建整个索引(百毫秒级)
                    d = index.messages_for(s, start=start, head=head, anchor=anchor)
                    native_activity = d.get("activity")
                    _resolve_activity(uid, d)
                    visible_activity = d.get("activity")
                    if not s.get("agent_id"):
                        # 若文件追加和 Escape 同时发生，本批已经携带修正后的状态；
                        # 同步游标，避免下一轮再推一份完全相同的空状态增量。
                        activity_revision = session_meta.activity_revision(uid)
                        if outbox_driver:
                            cursor = {"start": d["end"],
                                      "head": d["version"]["head"],
                                      "anchor": d["anchor"],
                                      "reset": d.get("reset", False)}
                            if outbox_driver.observe(
                                    uid, d["messages"], d.get("activity"), cursor):
                                OUTBOX_WAKE.set()
                            outbox = outbox_driver.snapshot(uid)
                            d.update(outbox)
                            outbox_revision = outbox["outbox_version"]["revision"]
                    if claude_sid:
                        d["prompt"] = _claude_prompt(claude_sid, d["messages"])
                        # _claude_prompt 可能在确认答案落盘后删掉状态文件。
                        prompt_revision = claude_bridge.revision(claude_sid)
                    elif codex_session:
                        codex_prompt = _codex_prompt(s, codex_pane)
                        d["prompt"] = codex_prompt
                        next_codex_prompt_check = time.time() + 0.4
                    if (d["reset"] or d["messages"] or d["activity_changed"]
                            or outbox_revision != previous_outbox_revision
                            or prompt_revision != previous_prompt_revision
                            or codex_prompt != previous_codex_prompt):
                        emit(d, "messages")
                    start, head, anchor = d["end"], d["version"]["head"], d["anchor"]
                    beat = time.time()
                elif (not s.get("agent_id")
                      and session_meta.activity_revision(uid) != activity_revision):
                    activity_revision = session_meta.activity_revision(uid)
                    visible_activity = session_meta.resolve_activity(
                        uid, native_activity)
                    # 使用普通的空增量格式，已打开、尚未刷新到新版 JS 的页面
                    # 也能立即清掉 Working，不需要认识额外的事件协议。
                    packet = {
                        "reset": False, "start": start, "end": start,
                        "version": ver, "anchor": anchor, "messages": [],
                        "activity_changed": True,
                        "activity": visible_activity,
                    }
                    emit(packet, "activity")
                    beat = time.time()
                elif claude_sid and current_prompt_revision != prompt_revision:
                    prompt_revision = current_prompt_revision
                    emit({"prompt_only": True,
                          "prompt": claude_bridge.prompt(claude_sid)}, "prompt")
                    beat = time.time()
                elif codex_session and time.time() >= next_codex_prompt_check:
                    next_codex_prompt_check = time.time() + 0.4
                    current_codex_prompt = _codex_prompt(s, codex_pane)
                    if current_codex_prompt != codex_prompt:
                        codex_prompt = current_codex_prompt
                        emit({"prompt_only": True,
                              "prompt": codex_prompt}, "prompt")
                        beat = time.time()
                elif (outbox_driver
                      and outbox_driver.revision() != outbox_revision):
                    outbox = outbox_driver.snapshot(uid)
                    outbox_revision = outbox["outbox_version"]["revision"]
                    emit({"outbox_only": True, **outbox}, "outbox")
                    beat = time.time()
                elif time.time() - beat > 20:     # 心跳, 让中间的代理别掐连接
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()

                # Native tmux input bypasses /api/term/send, so an immediate
                # Escape can return the real TUI to its composer without adding
                # a durable abort record.  Reconcile only a stable, recognised
                # idle composer; question/approval overlays remain unknown and
                # can never be mistaken for completion.
                now = time.time()
                if terminal_session and now >= next_terminal_lookup and not terminal_name:
                    next_terminal_lookup = now + 1.0
                    pane = _pane_for_session(s, term.list_sessions())
                    terminal_name = str(pane.get("name") or "") if pane else ""
                    if codex_session:
                        codex_pane = terminal_name
                if (terminal_name and outbox_driver
                        and now >= next_terminal_check):
                    next_terminal_check = now + 0.15
                    try:
                        terminal_state = outbox_driver.terminal_probe(terminal_name)
                    except (OSError, RuntimeError, ValueError, KeyError):
                        terminal_name = ""
                        terminal_idle_since = 0.0
                        terminal_idle_token = None
                    else:
                        state = str((visible_activity or {}).get("state") or "")
                        token = (state, str((visible_activity or {}).get("ts") or ""))
                        if token != terminal_activity_token:
                            terminal_activity_token = token
                            terminal_busy_seen = False
                            terminal_idle_since = 0.0
                            terminal_idle_token = None
                        if terminal_state["busy"]:
                            terminal_busy_seen = True
                            terminal_idle_since = 0.0
                            terminal_idle_token = None
                            # 窄屏 Claude 曾因隐藏 interrupt 页脚被误判为空闲。
                            # 一旦 TUI 本身重新给出明确运行证据，撤销那次启发式
                            # stop；显式网页 Escape 不属于 inferred，不会被清理。
                            if ((visible_activity or {}).get("reason") in {
                                    "终端已结束或中断", "终端已回到输入状态"}):
                                session_meta.clear_inferred_activity_stop(uid)
                            time.sleep(WATCH_POLL)
                            continue
                        settled = (not terminal_state["busy"]
                                   and terminal_state["draft_state"] in {
                                       "empty", "editing"})
                        if (state in {"working", "waiting", "aborted", "failed"}
                                and settled and token != handled_terminal_activity):
                            if terminal_idle_token != token:
                                terminal_idle_token = token
                                terminal_idle_since = now
                            elif now - terminal_idle_since >= 0.45:
                                interrupted = outbox_driver.mark_interrupted(
                                    uid, terminal_state["draft_state"] == "editing")
                                if interrupted:
                                    OUTBOX_WAKE.set()
                                if state in {"working", "waiting"}:
                                    if interrupted:
                                        visible_activity = session_meta.stop_activity(
                                            uid, reason="终端在原生消息落盘前中断")
                                    elif terminal_busy_seen:
                                        # 空输入框只证明回合已经结束，不能证明用户
                                        # 按过 Esc。原生 transcript 若稍后补上 idle，
                                        # 它会自然覆盖这条启发式停止点。
                                        visible_activity = session_meta.stop_activity(
                                            uid, reason="终端已回到输入状态",
                                            state="idle", inferred=True)
                                    else:
                                        # Enter 后 Claude 可能先短暂画出空输入框，
                                        # 再出现 spinner。没有见过忙态、也没有退役
                                        # 待确认发送项时，不得据此宣称回合结束。
                                        terminal_idle_since = 0.0
                                        terminal_idle_token = None
                                        time.sleep(WATCH_POLL)
                                        continue
                                handled_terminal_activity = token
                        else:
                            terminal_idle_since = 0.0
                            terminal_idle_token = None
                time.sleep(WATCH_POLL)
        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            audit.record(
                "sse.connection.error", category="stream", severity="warning",
                uid=uid, source=str(s.get("source") or ""), page_id=page_id,
                connection_id=connection_id,
                data={"error": f"{type(error).__name__}: {error}"},
            )
        finally:
            audit.record(
                "sse.connection.closed", category="stream", uid=uid,
                source=str(s.get("source") or ""), page_id=page_id,
                connection_id=connection_id,
                data={"packets": packet_seq,
                      "duration_ms": round((time.monotonic() - opened_at) * 1000, 3),
                      "end": start},
            )
            self.close_connection = True

    def _restore_trash(self, body: dict):
        try:
            result = trash.restore(str(body.get("id") or ""))
        except KeyError:
            return self._json({"error": "回收站条目不存在"}, 404)
        except FileExistsError as e:
            return self._json({"error": str(e)}, 409)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        index.invalidate()   # 文件已回到原处，下一次列表请求必须重扫磁盘
        return self._json({"ok": True, **result})

    def _purge_trash(self, body: dict):
        try:
            if body.get("all"):
                result = trash.purge_all()
            else:
                result = trash.purge(str(body.get("id") or ""))
        except KeyError:
            return self._json({"error": "回收站条目不存在"}, 404)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, **result})

    def _star_session(self, body: dict):
        uid = str(body.get("uid") or "")
        starred = body.get("starred")
        if not uid or not isinstance(starred, bool):
            return self._json({"error": "需要 uid 和布尔值 starred"}, 400)
        if not index.get(uid):
            return self._json({"error": "会话不存在"}, 404)
        try:
            meta = session_meta.set_starred(uid, starred)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "uid": uid, **meta})

    def _browser_audit(self, body: dict):
        """Accept a bounded browser-side receipt batch.

        These records are the final hop of the trace. They describe what the
        isolated page actually received and rendered, not what the server hoped
        it rendered.
        """
        events = body.get("events")
        if not isinstance(events, list):
            return self._json({"error": "events must be a list"}, 400)
        if len(events) > 100:
            return self._json({"error": "too many events"}, 413)
        page_id = str(body.get("page_id") or body.get("_page_id") or "")[:128]
        accepted = 0
        for item in events:
            if not isinstance(item, dict):
                continue
            name = str(item.get("event") or "")[:160]
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", name):
                continue
            uid = str(item.get("uid") or body.get("uid") or "")[:512]
            trace_id = str(item.get("trace_id") or body.get("_trace_id") or "")[:128]
            connection_id = str(item.get("connection_id") or "")[:128]
            request_id = str(item.get("request_id") or "")[:128]
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            content = item.get("content")
            # Keep individual browser events bounded even within the request cap.
            try:
                if len(json.dumps(content, ensure_ascii=False)) > 512 * 1024:
                    content = {"truncated": True}
            except (TypeError, ValueError):
                content = None
            audit.record(
                f"browser.{name}", category="browser",
                severity=str(item.get("severity") or "info")[:24],
                uid=uid, source=uid.partition(":")[0], trace_id=trace_id,
                request_id=request_id, page_id=page_id,
                connection_id=connection_id,
                build=str(body.get("_build") or item.get("build") or "")[:128],
                data={"client_ts": item.get("ts"), **data}, content=content,
            )
            accepted += 1
        return self._json({"ok": True, "accepted": accepted}, 202)

    def _bug_report(self, body: dict):
        """Capture the current cross-layer state and start a Codex investigator."""
        if not TERMINAL:
            return self._json({"error": "终端未启用，无法启动处理会话"}, 403)
        if not term.available_sources().get("codex"):
            return self._json({"error": "本机找不到 codex 命令"}, 503)
        description = str(body.get("description") or "").strip()
        uid = str(body.get("uid") or "")[:512]
        session = index.get(uid) if uid and not uid.startswith("tmux:") else None
        snapshot = body.get("snapshot") if isinstance(body.get("snapshot"), dict) else {}
        terminal_name = str(body.get("terminal_name") or "")[:256]
        terminal_capture = ""
        if terminal_name and any(row.get("name") == terminal_name
                                 for row in term.list_sessions()):
            try:
                terminal_capture = term.capture_history(terminal_name, 8000)
            except (OSError, RuntimeError, ValueError):
                try:
                    terminal_capture = term.capture_screen(terminal_name)
                except (OSError, RuntimeError, ValueError):
                    terminal_capture = ""
        source = str((session or {}).get("source") or uid.partition(":")[0])
        outbox = send_protocol.snapshot(source, uid) if uid else {}
        try:
            report = bug_report.create(
                description, uid=uid,
                page_id=str(body.get("page_id") or body.get("_page_id") or "")[:128],
                trace_id=str(body.get("_trace_id") or "")[:128],
                build=str(body.get("_build") or "")[:128], hostname=HOSTNAME,
                client_ip=self._display_ip(), snapshot=snapshot,
                terminal_capture=terminal_capture, session=session, outbox=outbox,
            )
        except (OSError, ValueError) as error:
            return self._json({"error": str(error)}, 400)
        try:
            worker = bug_report.launch(
                report, cols=max(40, min(int(body.get("cols") or 120), 300)),
                rows=max(12, min(int(body.get("rows") or 36), 120)))
        except Exception as error:
            message = f"诊断已保存，但 Codex 会话启动失败：{error}"
            bug_report.update_manifest(
                Path(report["path"]), status="failed", error=message)
            audit.record(
                "bug_report.worker_launch_failed", category="bug-report",
                severity="error", uid=uid, trace_id=report["report_id"],
                data={"report_id": report["report_id"], "error": str(error)},
            )
            return self._json({"error": message, "report_id": report["report_id"],
                               "path": report["path"]}, 500)
        return self._json({
            "ok": True, "report_id": report["report_id"], "path": report["path"],
            "worker": {key: worker.get(key) for key in
                       ("name", "source", "sid", "cwd", "token", "title",
                        "kind", "report_id")},
        }, 202)

    def _claude_rewind(self, body: dict):
        """把 Claude 只存在进程内的双-Esc 回滚同步到对话时间线。"""
        uid = str(body.get("uid") or "")
        action = str(body.get("action") or "")
        s = index.get(uid)
        if not s or s.get("source") != "claude" or s.get("agent_id"):
            return self._json({"error": "这不是 Claude 主会话"}, 400)
        pane = _pane_for_session(s, term.list_sessions())
        name = str(body.get("name") or "")
        if not pane or pane.get("name") != name:
            return self._json({"error": "Claude tmux 会话未连接"}, 409)

        if action == "begin":
            tip = index._claude_effective_tip(s)
            if not tip:
                return self._json({"error": "无法确定当前 Claude 时间线"}, 409)
            pending = session_meta.begin_timeline_rewind(
                uid, tip, index.version(s)["size"])
            return self._json({"ok": True, "pending": True,
                               "from_tip": pending["from_tip"]})

        if action != "sync":
            return self._json({"error": "未知回滚操作"}, 400)
        pending = session_meta.pending_timeline_rewind(uid)
        if not pending:
            return self._json({"ok": True, "pending": False, "changed": False})
        try:
            screen = term.capture_screen_plain(name)
        except (OSError, RuntimeError) as exc:
            return self._json({"error": str(exc)}, 409)
        tip = index.claude_screen_tip(s, screen)
        if not tip:
            # 仍停在原生选择器/二级菜单；前端会在下一次 Enter 后重试。
            return self._json({"ok": True, "pending": True, "changed": False})
        if tip == pending.get("from_tip"):
            if index.version(s)["size"] > int(pending.get("stale_end") or 0):
                # 用户已回到普通输入并发出了新消息，说明没有未落盘的旧叶子可 pin。
                session_meta.cancel_timeline_rewind(uid)
                return self._json({"ok": True, "pending": False, "changed": False})
            return self._json({"ok": True, "pending": True, "changed": False})
        timeline = session_meta.finish_timeline_rewind(uid, tip)
        return self._json({"ok": True, "pending": False, "changed": True,
                           "tip": timeline["tip"]})

    def _queue_message(self, body: dict):
        uid = str(body.get("uid") or "")
        s = index.get(uid)
        if not s or s.get("source") not in {"claude", "codex"}:
            return self._json({"error": "服务端发送账本只用于已有 Claude/Codex 会话"}, 400)
        panes = term.list_sessions()
        pane = _pane_for_session(s, panes)
        if not pane or pane["name"] != str(body.get("name") or ""):
            return self._json({"error": f"{s['source'].title()} tmux 会话未连接"}, 409)
        driver = send_protocol.driver_for(str(s.get("source") or ""))
        if not driver:
            return self._json({"error": "会话发送协议不可用"}, 400)
        if s.get("source") == "claude":
            return self._queue_claude_message(body, s, pane)
        failed = next((item for item in send_queue.list_for(uid)
                       if item.get("state") == "failed"), None)
        if failed:
            # 新消息排在失败项后面永远不会投递。拒绝本次请求，让浏览器保留
            # 编辑框正文，并明确要求先处理真正的阻塞项。
            retryable = int(failed.get("attempts") or 0) == 0
            return self._json({
                "error": ("上一条消息发送失败，请先重试或移除" if retryable
                          else "上一条消息状态待核对，请检查终端或移除"),
                **send_queue.snapshot(uid),
            }, 409)
        conflict = driver.overwrite_draft(
            pane["name"], str(body.get("overwrite_draft") or ""))
        if conflict:
            return self._json({**conflict, **send_queue.snapshot(uid)}, 409)
        item = send_queue.enqueue(
            uid, pane["name"], str(body.get("text") or ""), body.get("media"),
            body.get("activity"), str(body.get("request_id") or ""),
            body.get("cursor"))
        OUTBOX_WAKE.set()
        return self._json({"ok": True, "item": item,
                           **send_queue.snapshot(uid)})

    def _queue_claude_message(self, body: dict, session: dict, pane: dict):
        uid = str(session.get("uid") or "")
        text = str(body.get("text") or "")
        request_id = str(body.get("request_id") or "")
        existing = claude_queue.lookup(request_id, uid, text)
        if existing:
            # 网络丢包后的同 ID 重放只是查询状态，绝不能清掉用户后来写的草稿。
            return self._json({"ok": True, "item": existing,
                               **claude_queue.snapshot(uid)})

        driver = send_protocol.driver_for("claude")
        conflict = driver.overwrite_draft(
            pane["name"], str(body.get("overwrite_draft") or ""))
        if conflict:
            return self._json({**conflict, **claude_queue.snapshot(uid)}, 409)

        # Establish the causal boundary on the server immediately before the
        # ledger write. A background tab can send a cursor that predates an old
        # same-text prompt or custom-title record and must not retire this row.
        native_cursor = index.cursor(session)
        causal_cursor = {
            "start": native_cursor["end"],
            "head": native_cursor["head"],
            "anchor": native_cursor["anchor"],
        }
        item, created = claude_queue.enqueue(
            uid, pane["name"], text, body.get("media"), request_id, causal_cursor, {
                "page": str(body.get("page_id") or "")[:128],
                "build": str(body.get("_build") or "")[:32],
                "ip": self._display_ip(),
            })
        if not created:
            # A repeated HTTP request after a network loss is a status lookup,
            # never permission to paste the same prompt into Claude again.
            return self._json({"ok": True, "item": item,
                               **claude_queue.snapshot(uid)})
        claimed = claude_queue.mark_injecting(item["id"], uid)
        if not claimed:
            # The background restart-recovery loop won the atomic claim.
            return self._json({"ok": True, "item": item,
                               **claude_queue.snapshot(uid)})
        try:
            term.leave_copy_mode(pane["name"])
            term.submit_text(pane["name"], text)
            item = claude_queue.mark_submitted(item["id"], uid) or item
        except Exception as error:
            # Paste and Enter are separate operations.  Once injection begins an
            # exception is ambiguous and must never expose a blind retry button.
            item = claude_queue.mark_ambiguous(item["id"], uid, str(error)) or item
        OUTBOX_WAKE.set()
        return self._json({"ok": True, "item": item,
                           **claude_queue.snapshot(uid)})

    def _draft_status(self, body: dict):
        uid = str(body.get("uid") or "")
        s = index.get(uid)
        driver = send_protocol.driver_for(str(s.get("source") or "")) if s else None
        if not s or not driver:
            return self._json({"error": "草稿检测只适用于 Claude/Codex 会话"}, 400)
        pane = _pane_for_session(s, term.list_sessions())
        if not pane or pane["name"] != str(body.get("name") or ""):
            return self._json({"error": f"{s['source'].title()} tmux 会话未连接"}, 409)
        return self._json({"ok": True, **driver.composer_probe(pane["name"])})

    def _retry_message(self, body: dict):
        uid = str(body.get("uid") or "")
        item_id = str(body.get("id") or "")
        s = index.get(uid)
        if s and s.get("source") == "claude":
            if any(item.get("id") == item_id for item in claude_queue.list_for(uid)):
                return self._json({
                    "error": "消息已经提交到终端，禁止盲目重发；请等待原生记录或打开终端检查",
                    **claude_queue.snapshot(uid),
                }, 409)
            return self._json({"error": "待核对消息不存在"}, 404)
        existing = next((item for item in send_queue.list_for(uid)
                         if item.get("id") == item_id), None)
        if not existing:
            return self._json({"error": "待发送消息不存在"}, 404)
        if (existing.get("state") != "failed"
                or int(existing.get("attempts") or 0) > 0):
            return self._json({
                "error": "消息已经写入终端或仍在确认，禁止重复发送",
                **send_queue.snapshot(uid),
            }, 409)
        pane = _pane_for_session(s, term.list_sessions()) if s else None
        if pane:
            driver = send_protocol.driver_for("codex")
            conflict = driver.overwrite_draft(
                pane["name"], str(body.get("overwrite_draft") or ""))
            if conflict:
                return self._json({**conflict, **send_queue.snapshot(uid)}, 409)
        item = send_queue.retry(item_id, body.get("activity"), uid)
        if not item:
            return self._json({"error": "待发送消息不存在"}, 404)
        OUTBOX_WAKE.set()
        return self._json({"ok": True, **send_queue.snapshot(item["uid"])})

    def _discard_message(self, body: dict):
        item_id = str(body.get("id") or "")
        uid = str(body.get("uid") or "")
        s = index.get(uid)
        if s and s.get("source") == "claude":
            if not claude_queue.discard(item_id, uid):
                return self._json({"error": "待核对消息不存在"}, 404)
            return self._json({"ok": True, "uid": uid,
                               **claude_queue.snapshot(uid)})
        existing = next((item for item in send_queue.list_for(uid)
                         if item.get("id") == item_id), None)
        # Dismiss is idempotent. Another tab may already have removed the
        # durable row while this tab still retains its in-memory placeholder;
        # the authoritative snapshot lets that tab clear it too.
        if not existing:
            return self._json({"ok": True, "uid": uid,
                               **send_queue.snapshot(uid)})
        if not send_queue.discard(
                item_id, uid, {"queued", "failed", "aborted", "restored"}):
            return self._json({"error": "消息不存在或已经开始发送"}, 409)
        return self._json({"ok": True, "uid": uid,
                           **send_queue.snapshot(uid)})

    def _takeover(self, body: dict):
        """接管一个会话: 在 tmux 里把它 resume 起来, 之后网页就能直接输入。

        三种情况:
          - 已经有对应的 tmux 会话  → 直接连上
          - 会话没在运行            → 起一个
          - 正在运行但不在 tmux 里  → 先回 needs_confirm, 确认后杀掉原实例再起
        """
        s = index.get(body["uid"])
        if not s:
            return self._json({"error": "会话不存在"}, 404)

        name = term.session_name_for(s["source"], s["sid"])
        pids = live.pids_of(s, force=True)
        panes = term.list_sessions()
        pane = _pane_for_session(s, panes, pids)
        if pane:
            return self._json({"name": pane["name"], "action": "reused"})

        if pids and not term.in_tmux(pids):
            mains = [p for p in pids if p > 0]
            if not body.get("force"):
                return self._json({"needs_confirm": True, "pids": mains,
                                   "reason": "会话正在运行, 且不在 tmux 里"})
            term.kill_pids(pids)

        cols, rows = int(body.get("cols", 120)), int(body.get("rows", 32))
        term.new_session(name, term.resume_command(s["source"], s["sid"]), s["cwd"], cols, rows)
        time.sleep(0.1)
        if not term.has_session(name):
            return self._json({"error": f"{s['source']} 启动后立即退出，请检查 CLI 环境"}, 500)
        return self._json({"name": name, "action": "killed" if pids else "started"})

    def _stop_session(self, body: dict):
        """停止会话的运行实例，保留对话文件供后续查看或删除。"""
        s = index.get(str(body.get("uid") or ""))
        if not s:
            return self._json({"error": "会话不存在"}, 404)

        pids = live.pids_of(s, force=True)
        panes = term.list_sessions()
        pane = _pane_for_session(s, panes, pids)

        if pane:
            # 先退出最里面的 CLI；它是 pane 的前台命令，退出后 tmux 会自然收掉。
            # Ctrl-D 无效时 graceful_stop 才依次升级到 TERM/KILL 和清理 tmux 残壳。
            killed = term.graceful_stop(pane["name"], pids)
            pending_store.discard(pane["name"])
        else:
            killed = term.kill_pids(pids)
        live.snapshot(force=True)
        send_queue.discard_uid(s["uid"])
        # Stop hides unresolved UI state but retains id-only tombstones, so a
        # delayed HTTP replay cannot revive an old prompt after a later resume.
        claude_queue.discard_uid(s["uid"], tombstone=True)
        return self._json({"ok": True, "stopped": bool(pane or killed),
                           "tmux": bool(pane)})

    def _create_session(self, body: dict):
        """用固定 CLI 白名单新建会话；不接受浏览器传入的任意命令。"""
        source = str(body.get("source") or "")
        before = {str(s["sid"]) for s in index.load() if s["source"] == source}
        cols, rows = int(body.get("cols", 120)), int(body.get("rows", 32))
        try:
            info = term.new_cli_session(
                source, str(body.get("cwd") or ""),
                cols, rows, create_cwd=body.get("create_cwd") is True,
            )
        except term.DirectoryCreationRequired as e:
            # 首次提交绝不隐式创建目录；浏览器展示规范化后的实际目标，
            # 用户明确同意后才用 create_cwd=true 重试。
            return self._json({"error": str(e), "needs_create": True,
                               "cwd": e.path}, 409)
        record = {**info, "before": before, "started": time.time(),
                  "cols": cols, "rows": rows}
        try:
            pending_store.put(record)
        except Exception:
            # 记录失败就撤销刚启动的 tmux，不能制造一个网页再也找不到的孤儿。
            term.kill_session(info["name"])
            raise
        return self._json({k: info[k] for k in ("name", "source", "sid", "cwd", "token")})

    def _new_session_status(self, q: dict):
        """等待 CLI 落盘后，把临时 tmux 名称关联到真正的 agenthub 会话。"""
        if not TERMINAL:
            return self._json({"error": "终端未启用"}, 403)
        name = q.get("name", [""])[0]
        pending = pending_store.get(name)
        if pending and pending.get("resolved"):
            return self._json(pending["resolved"])
        if not pending:
            # This is a polling state, not an exceptional resource lookup.  A
            # kill can race with an already-dispatched poll; return a normal
            # terminal state so browsers and reverse proxies do not report a
            # spurious HTTP error after a successful shutdown.
            return self._json({"gone": True})

        # 签名包含路径、mtime 和大小；新文件/首条消息会自然触发重建。
        # 不能在 750ms 状态轮询里强制全量解析所有会话。
        sessions = index.load()
        source, sid, cwd = pending["source"], pending["sid"], pending["cwd"]

        def same_cwd(s):
            try:
                return str(Path(s["cwd"]).expanduser().resolve()) == cwd
            except (OSError, TypeError):
                return False

        if sid:
            candidates = [s for s in sessions if s["source"] == source and str(s["sid"]) == sid]
        else:
            before = set(pending.get("before") or [])
            candidates = [s for s in sessions if s["source"] == source
                          and str(s["sid"]) not in before and same_cwd(s)]

        # Codex 不能预先指定 UUID。若同目录恰好同时新建多条，就用 tmux pane
        # 的进程树与 Codex 持有的 rollout 文件 fd 做精确关联。
        if source == "codex" and candidates:
            pane = next((x for x in term.list_sessions() if x["name"] == name), None)
            if pane:
                live.snapshot(force=True)
                linked = [s for s in candidates if any(
                    term.process_belongs_to(pid, pane["pid"])
                    for pid in live.pids_of(s)
                )]
                if linked:
                    candidates = linked
                else:
                    candidates = []           # rollout 已出现但进程关系还没稳定，下轮再认
        if not candidates:
            exists = any(x["name"] == name for x in term.list_sessions())
            if not exists:
                # CLI 在首条消息前退出：不会再产生可关联记录，不能让前端永远
                # 停在一个已经从列表消失的临时详情页。waiting=True 兼容尚未
                # 刷新的旧前端；新前端识别 exited 后完整清理本地视图。
                pending_store.discard(name)
                return self._json({"waiting": True, "running": False, "exited": True})
            return self._json({"waiting": True, "running": exists})

        s = max(candidates, key=lambda x: x["created"])
        canonical = term.session_name_for(s["source"], s["sid"])
        names = {x["name"] for x in term.list_sessions()}
        if name != canonical and name in names:
            if canonical in names:
                return self._json({"error": f"tmux 会话名冲突: {canonical}"}, 409)
            canonical = term.rename_session(name, canonical)
        running = name in names or canonical in names
        result = {"waiting": False, "running": running,
                  "name": canonical, "uid": s["uid"], "session": s}
        pending_store.resolve(name, result)
        return self._json(result)

    def _attach(self, q: dict):
        """WebSocket ↔ tmux attach 的字节转发。"""
        if not TERMINAL:
            return self._send(403, b"terminal disabled", "text/plain")
        name = q.get("name", [""])[0]
        page = q.get("page", [""])[0]
        token = q.get("token", [""])[0]
        connection_id = str(q.get("connection", [""])[0])[:128] or uuid.uuid4().hex
        self._audit_page_id = str(page)[:128]
        self._audit_connection_id = connection_id
        audit.record(
            "terminal.connection.requested", category="terminal",
            page_id=page, connection_id=connection_id,
            data={"tmux": name, "cols": q.get("cols", [""])[0],
                  "rows": q.get("rows", [""])[0]},
        )
        if not name or not any(s["name"] == name for s in term.list_sessions()):
            return self._send(404, b"no such tmux session", "text/plain")
        # The claim endpoint issues this opaque token.  Binding is repeated
        # after the upgrade below so a concurrent force-claim wins atomically.
        if not page or not token:
            return self._send(409, b"terminal ownership required", "text/plain")
        if not wsock.handshake(self):
            return self._send(400, b"expected websocket", "text/plain")
        self._audit_response_done = True

        sock = self.connection
        stop = threading.Event()
        connection = _TerminalConnection(sock, stop)
        if not TERM_OWNERS.bind(name, page, token, connection):
            audit.record(
                "terminal.connection.rejected", category="terminal",
                severity="warning", page_id=page, connection_id=connection_id,
                data={"tmux": name, "reason": "ownership"},
            )
            connection.revoke("", notify=False)
            connection.closed.set()
            self.close_connection = True
            return
        att = None
        try:
            att = term.Attach(name, int(q.get("cols", ["120"])[0]),
                              int(q.get("rows", ["32"])[0]))
        except Exception:
            TERM_OWNERS.release(name, token)
            connection.closed.set()
            wsock.close(sock, 1011, "attach failed")
            self.close_connection = True
            return

        opened_at = time.monotonic()
        audit.record(
            "terminal.connection.opened", category="terminal",
            page_id=page, connection_id=connection_id,
            data={"tmux": name, "cols": q.get("cols", ["120"])[0],
                  "rows": q.get("rows", ["32"])[0]},
        )

        def pump():                      # tmux → 浏览器
            buffered = bytearray()
            last_record = time.monotonic()

            def record_output(force: bool = False):
                nonlocal last_record
                if not buffered or (not force and len(buffered) < 32 * 1024
                                    and time.monotonic() - last_record < 0.25):
                    return
                payload = bytes(buffered)
                buffered.clear()
                last_record = time.monotonic()
                audit.record(
                    "terminal.output", category="terminal",
                    page_id=page, connection_id=connection_id,
                    data={"tmux": name, "bytes": len(payload)}, content=payload,
                )

            while not stop.is_set():
                data = att.read(0.05)
                if data:
                    buffered.extend(data)
                    try:
                        connection.send(data, wsock.OP_BIN)
                    except OSError:
                        break
                    record_output()
                elif not att.alive():
                    break
                else:
                    record_output()
            record_output(True)
            stop.set()

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            while not stop.is_set():     # 浏览器 → tmux
                op, payload = wsock.recv(sock)
                if op == wsock.OP_CLOSE:
                    break
                if op == wsock.OP_PING:
                    connection.send(payload, wsock.OP_PONG)
                    continue
                if op == wsock.OP_TEXT and payload[:1] == b"{":
                    try:                 # 控制消息只有一种: 改窗口大小
                        m = json.loads(payload)
                        if m.get("t") == "resize":
                            att.resize(int(m["cols"]), int(m["rows"]))
                            audit.record(
                                "terminal.resized", category="terminal",
                                page_id=page, connection_id=connection_id,
                                data={"tmux": name, "cols": int(m["cols"]),
                                      "rows": int(m["rows"])},
                            )
                            continue
                    except Exception:
                        pass
                audit.record(
                    "terminal.input", category="terminal",
                    page_id=page, connection_id=connection_id,
                    data={"tmux": name, "bytes": len(payload), "opcode": op},
                    content=payload,
                )
                att.write(payload)
        except (ConnectionError, OSError):
            pass
        finally:
            stop.set()
            att.close()
            TERM_OWNERS.release(name, token)
            connection.closed.set()
            with connection.send_lock:
                wsock.close(sock)
            audit.record(
                "terminal.connection.closed", category="terminal",
                page_id=page, connection_id=connection_id,
                data={"tmux": name, "replaced": connection.replaced,
                      "duration_ms": round((time.monotonic() - opened_at) * 1000, 3)},
            )
            self.close_connection = True

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        data = f.read_bytes()
        if f.name == "index.html":
            data = data.replace(b"__AGENTHUB_HOSTNAME__",
                                html.escape(HOSTNAME).encode("utf-8"))
            data = data.replace(b"__AGENTHUB_ASSET_VERSION__",
                                ASSET_VERSION.encode("ascii"))
        cache = "no-store" if f.name == "index.html" else "no-cache"
        self._send(200, data, ctype, {"Cache-Control": cache})


def main():
    ap = argparse.ArgumentParser(description="Claude/Codex/Grok 会话管理服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--allow", default="192.0.2.134",
                    help="除本机外允许访问的 IP 或 CIDR, 逗号分隔")
    ap.add_argument("--terminal", action="store_true",
                    help="开启 tmux 远程终端。这等于给白名单 IP 开放本机 shell, 谨慎使用")
    args = ap.parse_args()

    global TERMINAL
    TERMINAL = args.terminal
    if TERMINAL and not term.available():
        print("[agenthub] 警告: 找不到 tmux, 终端功能不可用")
        TERMINAL = False

    if TERMINAL:
        threading.Thread(target=_outbox_loop, daemon=True, name="agenthub-outbox").start()

    ALLOWED_IPS.update({"127.0.0.1", "::1", "localhost"})
    try:
        for value in args.allow.split(","):
            _add_allowed(value)
    except ValueError as exc:
        ap.error(f"--allow 包含无效的 IP/CIDR: {exc}")

    threading.Thread(target=index.load, daemon=True).start()  # 后台预热索引

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    allowed = sorted(ALLOWED_IPS) + [str(network) for network in ALLOWED_NETWORKS]
    print(f"[agenthub] http://{args.host}:{args.port}  允许: {allowed}"
          + ("  [终端已开启]" if TERMINAL else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[agenthub] 已停止")


if __name__ == "__main__":
    main()
