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
import secrets
import socket
import threading
import queue
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import (audit, bug_report, claude_bridge, claude_queue, codex_bridge,
               debug_runs, index, live, media, pending as pending_store,
               send_protocol, send_queue,
               session_meta, term, term_ownership, trash, wsock)
from . import federation, create_requests, files, file_manager

STATIC = Path(__file__).parent / "static"
ASSET_VERSION = hashlib.sha256(b"".join(
    (STATIC / name).read_bytes()
    for name in ("style.css", "cli.js", "nodes.js", "app.js", "term.js",
                 "files.html", "files.js", "files.css", "typography.css", "typography.js",
                 "file.html", "file.js", "file-preview.css", "file-preview.js",
                 "pwa-install.js", "syntax.js",
                 "vendor/markdown-it/markdown-it.min.js")
)).hexdigest()[:12]
HOSTNAME = socket.gethostname().strip() or "localhost"
HUB_MODE = False
NODE_TOKEN = ""
NODE_ID = ""
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
CODEX_SEND_LOCK = threading.Lock()
TERM_OWNERS = term_ownership.Registry()
_CLAUDE_CONFIRM_REPLAY_AT: dict[str, float] = {}
_CODEX_CONFIRM_REPLAY_AT: dict[str, float] = {}
_NEW_STATUS_REFRESH_AT: dict[str, float] = {}
# 新建后这段时间内，状态轮询照旧每次刷新索引；之后限频到 SLOW_INTERVAL。
NEW_STATUS_FAST_WINDOW = 120.0
NEW_STATUS_SLOW_INTERVAL = 5.0


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
    # Esc 只负责中断当前 Codex 回合；已经提交的 follow-up 由 TUI 自己管理，
    # AgentHub 只需继续轮询其原生用户记录。
    if str(uid or "").startswith("codex:") and "Escape" in keys:
        OUTBOX_WAKE.set()
    if str(uid or "").startswith("claude:") and "Escape" in keys:
        # Claude 若在第一条 assistant 记录出现前被 Esc，中断不会落进 JSONL。
        # 持久记录这次显式操作，避免尾部孤立 user 永远被推断成 working。
        return session_meta.stop_activity(uid)
    return None


def _resolve_activity(uid: str, result: dict) -> dict:
    if (result.get("meta") or {}).get("agent_id"):
        return result
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

    tracked_codex = send_queue.tracked()
    for stale in set(_CODEX_CONFIRM_REPLAY_AT) - {str(x.get("id") or "")
                                                  for x in tracked_codex}:
        _CODEX_CONFIRM_REPLAY_AT.pop(stale, None)
    for item in tracked_codex:
        uid = str(item.get("uid") or "")
        item_id = str(item.get("id") or "")
        s = index.get(uid)
        if not s or s.get("source") != "codex":
            continue
        try:
            # 正常情况下从持续前移的 watch 游标读。到确认期限时，改从实际
            # 注入前的固定游标持续复核；即使 compact 延迟 user 记录或某次
            # 解析遗漏，也不能把已经写入终端的消息重新暴露为可重试。
            # 固定游标复核要重读注入点之后的全部记录，开销随 rollout 增长。
            # 和 Claude 分支一样按 CONFIRM_TIMEOUT 限频，其余轮次只读增量，
            # 否则一条长期未确认的回执会让本线程持续占满一个核心。
            now = time.time()
            overdue = (item.get("state") in {"delivering", "confirming"}
                       and now - float(item.get("delivered_at") or 0)
                       >= send_queue.CONFIRM_TIMEOUT)
            replay = (overdue
                      and now - _CODEX_CONFIRM_REPLAY_AT.get(item_id, 0.0)
                      >= send_queue.CONFIRM_TIMEOUT)
            if replay:
                _CODEX_CONFIRM_REPLAY_AT[item_id] = now
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


def _submit_codex_item(item: dict, pane: dict) -> dict | None:
    """Write directly to the live Codex TUI; the TUI owns follow-up queuing."""
    item_id = str(item.get("id") or "")
    uid = str(item.get("uid") or "")
    name = str(pane.get("name") or "")
    try:
        term.leave_copy_mode(name)
        # Persist the ambiguous boundary before paste + Enter.  A crash after
        # this point must never expose a blind retry that could duplicate input.
        if not send_queue.mark_delivering(item_id):
            return send_queue.lookup(item_id, uid)
        term.submit_text(name, str(item.get("text") or ""))
        send_queue.mark_confirming(item_id)
    except Exception as error:
        if int((send_queue.lookup(item_id, uid) or {}).get("attempts") or 0) > 0:
            send_queue.mark_confirming(item_id, f"终端写入状态待核对: {error}")
        else:
            send_queue.mark_failed(item_id, str(error))
    return send_queue.lookup(item_id, uid)


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
    """追踪 Codex 终端回执，并恢复 Claude 的持久交付项。"""
    while True:
        _poll_outbox()
        send_queue.expire_deliveries()
        claude_ready = claude_queue.ready()
        if not claude_ready:
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


SPAWN_WATCH_INTERVAL = 10.0


def _record_spawn_parents(sessions: list[dict], owned_pids: dict[str, list[int]]) -> int:
    """发起关系只在双方进程都在时能从进程树看出来，看到就写进 session-meta。"""
    spawned = live.spawn_parents(sessions, owned_pids, skip=session_meta.spawned_uids())
    return session_meta.record_spawn_parents(spawned) if spawned else 0


def _spawn_watch_tick() -> int:
    """扫一遍 inventory 与进程，记下新出现的发起关系；返回新记录数。"""
    sessions = index.load()
    _uids, owned_pids = live.active_processes(sessions)
    return _record_spawn_parents(sessions, owned_pids)


def _spawn_watch_loop(stop: threading.Event | None = None) -> None:
    """不能只靠浏览器轮询 /api/live 顺手记录：agent 批量派出的 headless 会话往往
    在没人开着网页的几分钟里生灭（实测 10 条 grok -p 跑了 5–7 分钟，期间没有
    可见页面，只有收尾时还活着的 3 条被记下）。服务自己按固定节奏看一眼，
    进程扫描与 /api/live 共用同一份 3 秒缓存，网页开着时几乎不额外花钱。
    """
    while not (stop and stop.is_set()):
        try:
            _spawn_watch_tick()
        except Exception:
            pass                                  # 诊断性的记录，绝不能拖垮服务
        if stop:
            stop.wait(SPAWN_WATCH_INTERVAL)
        else:
            time.sleep(SPAWN_WATCH_INTERVAL)


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
    if exact and (pids is None or any(
            pid != 0 and term.process_belongs_to(pid, exact["pid"])
            for pid in pids)):
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
        if self.headers.get("X-AgentHub-Protocol") and not self._hub_protocol():
            return False
        supplied = self.headers.get("X-AgentHub-Node-Token", "")
        if supplied and not (NODE_TOKEN and secrets.compare_digest(supplied, NODE_TOKEN)):
            return False
        return _ip_allowed(ip)

    def _hub_protocol(self) -> bool:
        supplied = self.headers.get("X-AgentHub-Node-Token", "")
        return bool(NODE_TOKEN and supplied and secrets.compare_digest(supplied, NODE_TOKEN)
                    and self.headers.get("X-AgentHub-Protocol") == str(federation.PROTOCOL))

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
        if u.path in {"/api/session/files/action", "/api/session/files/upload"}:
            return self._file_post(u)
        if u.path in {"/api/session/star", "/api/sessions/fork-visibility",
                      "/api/audit/browser",
                      "/api/bug-report", "/api/trash/restore",
                      "/api/trash/purge", "/api/sessions/delete", "/api/session/resolve-files"}:
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4 * 1024 * 1024:
                    return self._json({"error": "request too large"}, 413)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"error": "bad body"}, 400)
            self._audit_body(body)
            if u.path == "/api/session/resolve-files":
                return self._resolve_files(body)
            if u.path == "/api/session/star":
                return self._star_session(body)
            if u.path == "/api/sessions/fork-visibility":
                return self._set_fork_parent_visibility(body)
            if u.path == "/api/audit/browser":
                return self._browser_audit(body)
            if u.path == "/api/sessions/delete":
                return self._delete_sessions(body)
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
        if text_write and str(body.get("_build") or "") != ASSET_VERSION and not self._hub_protocol():
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
            if u.path == "/api/term/backend":
                return self._set_terminal_backend(body)
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
        # 缺陷报告的处理会话在提交后才创建，其 cwd 固定为仓库根目录，因此
        # 报告框的附件与对话一样先上传到该 cwd 的附件目录。
        session = None
        if uid == bug_report.BUG_REPORT_UPLOAD_UID:
            if not TERMINAL:
                return self._json({"error": "终端未启用，无法上传报告附件"}, 403)
            session = {"cwd": str(bug_report.PROJECT_ROOT)}
        elif uid.startswith("tmux:"):
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
            dest = self._trash_session(uid)
        except KeyError:
            return self._json({"error": "会话不存在"}, 404)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 409)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        self._json({"ok": True, "trash": dest})

    @staticmethod
    def _trash_session(uid: str) -> str:
        """把一个会话移入回收站；父会话只能隐藏，不能删除。"""
        sessions = index.load(force=True)
        s = next((row for row in sessions if row.get("uid") == uid), None)
        if not s:
            raise KeyError(uid)
        if session_meta.is_fork_parent(uid, sessions):
            raise RuntimeError("父会话只能隐藏，不能删除")
        name = term.session_name_for(s["source"], s["sid"])
        if live.is_live(s, force=True) or term.has_session(name):
            raise RuntimeError("请先停止会话")
        dest = index.delete(uid)
        try:
            session_meta.discard(uid)
        except OSError:
            pass  # 会话已成功移入回收站，不能把元数据清理失败误报成删除失败
        send_queue.discard_uid(uid)
        claude_queue.discard_uid(uid)
        return dest

    def _delete_sessions(self, body: dict):
        """批量移入回收站; 逐条独立成败, 运行中的会话跳过而不阻断其余。"""
        uids, seen = [], set()
        for raw in body.get("uids") or []:
            uid = str(raw or "").strip()
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
        if not uids:
            return self._json({"error": "没有选中任何会话"}, 400)
        # 固定请求开始时的父会话集合。否则同一批若先删子会话，后删父会话，
        # 后一次重扫会让父会话失去身份，从而绕过“只能隐藏”的约束。
        protected = session_meta.fork_parent_uids(index.load(force=True))
        deleted, errors = [], []
        for uid in uids:
            title = str((index.get(uid) or {}).get("title") or "")
            if uid in protected:
                errors.append({"uid": uid, "title": title,
                               "error": "父会话只能隐藏，不能删除"})
                continue
            try:
                dest = self._trash_session(uid)
            except KeyError:
                errors.append({"uid": uid, "title": title, "error": "会话不存在"})
            except (RuntimeError, OSError) as e:
                errors.append({"uid": uid, "title": title, "error": str(e)})
            else:
                deleted.append({"uid": uid, "title": title, "trash": dest})
        return self._json({"ok": True, "deleted": deleted, "errors": errors})

    def _download_file(self, path, filename=None):
        if not path.is_file():
            return self._json({"error": "请选择文件下载"}, 400)
        filename = filename or path.name
        name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(filename)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sent = 0
            try:
                while sent < size:
                    chunk = stream.read(min(1024 * 1024, size - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            audit.record("file.download", category="http", uid=getattr(self, "_audit_uid", ""),
                         data={"bytes": sent, "size": size, "complete": sent == size})

    def _file_messages(self, view, requested):
        # File links in the current window must not require rereading multi-GB
        # native histories. Fall back only for a reference from older loaded UI.
        messages = index.messages_for(view, windowed=True)["messages"]
        known = files.references(messages)
        # Basenames need the whole branch to detect earlier paths/collisions.
        # Explicit paths in the current window have only one filesystem meaning.
        if any(files.clean_ref(ref) not in known or '/' not in files.clean_ref(ref)
               for ref in requested):
            return index.messages_for(view)["messages"]
        return messages

    def _file_access(self, body):
        session = index.get(body.get('uid', ''))
        if not session:
            raise FileNotFoundError('会话不存在')
        view = index.session_view(session, body.get('agent', ''))
        scope = file_manager.scope_for({**view, 'agent_id': body.get('agent', '')})
        ref = body.get('ref', '')
        if not isinstance(ref, str) or not ref or len(ref) > 4096:
            raise ValueError('无效的目录引用')
        manager = file_manager.manager()
        # An already-open browser must be able to restore its renamed/deleted
        # entry directory. Session validity is still checked on every request.
        key = (scope, ref)
        if key not in manager.grants:
            messages = self._file_messages(view, [ref])
            anchor = files.resolve(messages, view.get('cwd', ''), ref)
            if not anchor.is_dir():
                raise ValueError('文件浏览入口必须是目录')
            manager.grant(scope, ref)
        return manager, scope

    def _file_post(self, url):
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                raise PermissionError('不允许跨站文件操作')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise PermissionError('不允许跨站文件操作')
            if not TERMINAL:
                raise PermissionError('此节点为只读模式')
            length = int(self.headers.get('Content-Length', '-1'))
            if self.headers.get('Transfer-Encoding') or not 0 <= length <= file_manager.UPLOAD_CHUNK:
                raise ValueError('请求过大或长度无效')
            if url.path.endswith('/upload'):
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    raise ValueError('无效的上传格式')
                body = {key: values[0] for key, values in parse_qs(url.query).items()}
                manager, scope = self._file_access(body)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError('上传中断')
                result = manager.upload(scope, body.get('job', ''), int(body.get('offset', '-1')), data)
            else:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('请求必须使用 JSON')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('无效的请求')
                manager, scope = self._file_access(body)
                if body.get('action') in {'cancel', 'retry'}:
                    result = manager.control(scope, body.get('job', ''), body['action'], body.get('conflict'))
                else:
                    result = manager.start(scope, body)
            return self._json({'job': result})
        except (ValueError, TypeError, KeyError) as exc:
            self.close_connection = True
            return self._json({'error': str(exc)}, 400)
        except FileNotFoundError as exc:
            self.close_connection = True
            return self._json({'error': str(exc)}, 404)
        except OSError as exc:
            self.close_connection = True
            return self._json({'error': str(exc)}, 403)

    def _file_navigation(self, query, node=None):
        """Give browser navigation a reader; API clients retain raw bytes."""
        if ('text/html' not in getattr(self, 'headers', {}).get('Accept', '')
                or query.get('raw', [''])[0] == '1'
                or query.get('download', [''])[0] == '1'
                or query.get('mode', [''])[0]):
            return False
        query = dict(query)
        if node and query.get('uid'):
            query['uid'] = [federation.qualify(node, query['uid'][0], uid=True)]
        location = ('../' * (5 if node else 2)) + 'file.html?' + urlencode(query, doseq=True)
        self._send(303, b'', 'text/plain', {'Location': location, 'Cache-Control': 'no-store',
                                         'Vary': 'Accept'})
        return True

    def _file_stream(self, target):
        mime = file_manager.MEDIA.get(target.suffix.lower())
        if not mime or not target.is_file():
            raise ValueError('此格式请使用文本预览或下载')
        with target.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if mime == 'application/pdf':
                if b'%PDF-' not in stream.read(1024):
                    raise ValueError('文件没有有效的 PDF 标识，请下载后检查')
                stream.seek(0)
            start, end, code = 0, size - 1, 200
            request = self.headers.get('Range', '')
            if request:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', request)
                if not match or not any(match.groups()):
                    return self._send(416, b'', mime, {'Content-Range': f'bytes */{size}'})
                left, right = match.groups()
                start = int(left) if left else max(0, size - int(right))
                end = min(size - 1, int(right)) if left and right else size - 1
                if start > end or start >= size:
                    return self._send(416, b'', mime, {'Content-Range': f'bytes */{size}'})
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Disposition', f"inline; filename*=UTF-8''{quote(target.name)}")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # Chromium's native PDF viewer is an isolated browser extension;
            # sandboxing its plugin frame prevents the PDF from rendering.
            self.send_header('Content-Security-Policy', "frame-ancestors 'self'" if mime == 'application/pdf'
                             else "sandbox; default-src 'none'")
            if code == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            stream.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    data = stream.read(min(file_manager.CHUNK, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _files_get(self, q):
        body = {key: values[0] for key, values in q.items()}
        try:
            manager, scope = self._file_access(body)
            mode = body.get('mode', '')
            if mode == 'jobs':
                return self._json({'jobs': manager.jobs(scope)})
            if mode == 'trash':
                return self._json({'items': manager.trash_list(scope)})
            if mode == 'artifact':
                target, name = manager.artifact(scope, body.get('job', ''))
                return self._download_file(target, name)
            if body.get('path'):
                target = file_manager.path_for(body['path'])
            else:
                session = index.get(body['uid'])
                view = index.session_view(session, body.get('agent', ''))
                target = files.resolve(self._file_messages(view, [body['ref']]), view.get('cwd', ''), body['ref'])
            if body.get('download') == '1':
                return self._download_file(target)
            if mode == 'info':
                return self._json(file_manager.describe(target))
            if mode == 'preview':
                return self._file_stream(target)
            if mode == 'thumbnail':
                info = target.stat()
                data = file_manager.thumbnail(str(target), info.st_mtime_ns, info.st_size)
                return self._send(200, data, 'image/jpeg', {'Cache-Control': 'no-store',
                                  'X-Content-Type-Options': 'nosniff'})
            listing = files.list_directory(target.resolve(), int(body.get('offset', '0')),
                                           sort=body.get('sort', 'name'), order=body.get('order', 'asc'),
                                           hidden=body.get('hidden', '1') != '0')
            return self._json({**listing, 'hostname': HOSTNAME, 'writable': TERMINAL,
                               'node_id': NODE_ID or federation.identity()})
        except KeyError:
            return self._json({'error': '子会话不存在'}, 404)
        except FileNotFoundError as exc:
            return self._json({'error': str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            return self._json({'error': str(exc)}, 400)
        except OSError:
            return self._json({'error': '无法访问此路径，请检查权限或刷新目录'}, 403)

    def _resolve_files(self, body):
        requested = body.get("refs") if isinstance(body, dict) else None
        if (not isinstance(requested, list) or len(requested) > 256
                or any(not isinstance(ref, str) or len(ref) > 4096 for ref in requested)):
            return self._json({"error": "无效的文件引用列表"}, 400)
        session = index.get(body.get("uid", ""))
        if not session:
            return self._json({"error": "会话不存在"}, 404)
        try:
            view = index.session_view(session, body.get("agent", ""))
            messages = self._file_messages(view, requested)
            resolved = files.resolve_many(messages, view.get("cwd", ""), requested)
        except KeyError:
            return self._json({"error": "子会话不存在"}, 404)
        except OSError:
            return self._json({"error": "无法检查会话文件"}, 403)
        targets = []
        for ref, path in resolved.items():
            target = Path(path)
            # A directory and a file have different actions. Do not make the
            # browser guess the type from a suffix or from the original text.
            kind = "directory" if target.is_dir() else "file" if target.is_file() else None
            if kind:
                targets.append({"ref": ref, "path": path, "kind": kind})
        # Keep resolved for already open tabs during rolling deployments.
        return self._json({"resolved": resolved, "targets": targets, "file_browser": True,
                           "node_id": NODE_ID or federation.identity()})

    def _api_get(self, path: str, q: dict):
        if path == "/api/meta":
            # A supplied credential is checked by _allowed; protocol requests
            # also require a configured node credential, even on loopback.
            if self.headers.get("X-AgentHub-Protocol") and not self._hub_protocol():
                return self._json({"error": "node authentication required"}, 403)
            return self._json({"build": ASSET_VERSION, "hostname": HOSTNAME,
                               "mode": "local", "protocol": federation.PROTOCOL,
                               "node_id": NODE_ID or federation.identity()})
        if path == "/api/nodes":
            return self._json({"mode": "local", "nodes": [{
                "id": NODE_ID or federation.identity(), "name": HOSTNAME, "online": True}]})
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
            rows = session_meta.enrich(index.with_cursors(sessions), sessions)
            current_sig = _view_signature(rows, run_id)
            if known and not force and known == current_sig:
                return self._json({"unchanged": True, "sig": known})
            return self._json({"sessions": rows, "sig": current_sig,
                               "built_at": built_at})

        if path == "/api/live":
            force = q.get("force", ["0"])[0] == "1"
            sessions = debug_runs.filter_rows(index.cached(), _debug_run(q))
            uids, owned_pids = live.active_processes(sessions, force=force)
            _record_spawn_parents(sessions, owned_pids)   # 趁每次判活顺手记下
            live_set = set(uids)
            tmux_uids = [s["uid"] for s in sessions
                         if s["uid"] in live_set
                         and term.in_tmux(owned_pids.get(s["uid"], []))]
            started_at = {}
            for s in sessions:
                if s["uid"] not in live_set:
                    continue
                value = live.started_at(s, pids=owned_pids.get(s["uid"], []))
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
            available = TERMINAL and term.available()
            reason = ("" if available else "服务未启用控制台（缺少 --terminal 启动选项）。"
                      if not TERMINAL else term.unavailable_reason())
            return self._json({"enabled": available, "unavailable_reason": reason,
                               "sources": term.available_sources() if TERMINAL else {},
                               "home": str(Path.home()),
                               "backend": term.backend_name(),
                               "backends": term.backends() if TERMINAL else [],
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

        if path == '/api/session/files':
            return self._files_get(q)

        if path == "/api/session/file":
            if self._file_navigation(q):
                return
            session = index.get(q.get("uid", [""])[0])
            if not session:
                return self._json({"error": "会话不存在"}, 404)
            try:
                view = index.session_view(session, q.get("agent", [""])[0])
                ref = q.get("ref", [""])[0]
                messages = self._file_messages(view, [ref])
                target = files.resolve(messages, view.get("cwd", ""), ref)
                if q.get("download", [""])[0] == "1":
                    return self._download_file(target)
                if q.get('mode', [''])[0] == 'info':
                    return self._json(file_manager.describe(target))
                if q.get('mode', [''])[0] == 'preview':
                    return self._file_stream(target)
                data, mime, headers = files.read(target)
            except KeyError:
                return self._json({"error": "子会话不存在"}, 404)
            except FileNotFoundError as e:
                return self._json({"error": str(e)}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except OSError:
                return self._json({"error": "无法读取此文件"}, 403)
            return self._send(200, data, mime, headers)

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
                topology = debug_runs.filter_rows(index.cached(), run_id)
                result["results"] = session_meta.enrich(
                    debug_runs.filter_rows(result["results"], run_id), topology)
                result["total_pool"] = len(topology)
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
            topology = debug_runs.filter_rows(index.cached(), _debug_run(q))
            result["meta"] = session_meta.enrich_one(result["meta"], topology)
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

        events = queue.Queue(maxsize=64)
        cancelled = threading.Event()

        def enqueue(obj):
            while not cancelled.is_set():
                try:
                    events.put(obj, timeout=.2)
                    return
                except queue.Full:
                    continue
            raise ConnectionAbortedError("search cancelled")

        def enrich(rows):
            topology = debug_runs.filter_rows(index.cached(), run_id)
            return session_meta.enrich(debug_runs.filter_rows(rows, run_id), topology)

        def scan():
            try:
                result = index.search(
                    query, sources, **opts,
                    progress=lambda done, total: enqueue(
                        {"type": "progress", "done": done, "total": total}),
                    matches=lambda rows: enqueue({"type": "matches", "results": enrich(rows)}),
                )
                result["results"] = enrich(result["results"])
                result["total_pool"] = len(debug_runs.filter_rows(index.cached(), run_id))
                enqueue({"type": "result", "data": result})
            except re.error as e:
                enqueue({"type": "error", "error": f"正则无效: {e}"})
            except ConnectionAbortedError:
                pass
            except Exception as e:
                if not cancelled.is_set():
                    enqueue({"type": "error", "error": f"{type(e).__name__}: {e}"})

        threading.Thread(target=scan, name="session-search", daemon=True).start()
        try:
            while True:
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    event = {"type": "heartbeat"}
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
                if event["type"] in {"result", "error"}:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            cancelled.set()

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

    def _set_fork_parent_visibility(self, body: dict):
        """父会话默认隐藏；这里只保存显式的“显示”例外。"""
        visible = body.get("visible")
        if not isinstance(visible, bool):
            return self._json({"error": "需要布尔值 visible"}, 400)
        uids, seen = [], set()
        for raw in body.get("uids") or []:
            uid = str(raw or "").strip()
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
        if not uids:
            return self._json({"error": "没有选中任何父会话"}, 400)

        sessions = index.load(force=True)
        by_uid = {str(row.get("uid") or ""): row for row in sessions}
        parents = session_meta.fork_parent_uids(sessions)
        updated, errors = [], []
        for uid in uids:
            if uid not in by_uid:
                errors.append({"uid": uid, "error": "会话不存在"})
                continue
            if uid not in parents:
                errors.append({"uid": uid, "error": "会话不是父会话"})
                continue
            try:
                meta = session_meta.set_fork_parent_visible(uid, visible)
            except OSError as error:
                errors.append({"uid": uid, "error": str(error)})
            else:
                updated.append({"uid": uid, **meta})
        return self._json({"ok": True, "updated": updated, "errors": errors})

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
        worker_source = str(body.get("source") or bug_report.DEFAULT_SOURCE)
        if worker_source not in bug_report.WORKER_SOURCES:
            return self._json({"error": f"不支持的处理会话类型: {worker_source}"}, 400)
        if not term.available_sources().get(worker_source):
            return self._json({"error": f"本机找不到 {worker_source} 命令"}, 503)
        description = str(body.get("description") or "").strip()
        try:
            attachments = bug_report.resolve_attachments(body.get("attachments"))
        except ValueError as error:
            return self._json({"error": str(error)}, 400)
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
                attachments=attachments,
            )
        except (OSError, ValueError) as error:
            return self._json({"error": str(error)}, 400)
        try:
            worker = bug_report.launch(
                report, cols=max(40, min(int(body.get("cols") or 120), 300)),
                rows=max(12, min(int(body.get("rows") or 36), 120)),
                source=worker_source)
        except Exception as error:
            label = bug_report.SOURCE_LABELS.get(worker_source, worker_source)
            message = f"诊断已保存，但 {label} 会话启动失败：{error}"
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
        request_id = str(body.get("request_id") or "")
        with CODEX_SEND_LOCK:
            existing = send_queue.lookup(
                request_id, uid, str(body.get("text") or "")) if request_id else None
            if existing:
                # A replay after a lost HTTP response is only a status read.
                # The first request already crossed (or approached) tmux.
                return self._json({"ok": True, "item": existing,
                                   **send_queue.snapshot(uid)})
            conflict = driver.overwrite_draft(
                pane["name"], str(body.get("overwrite_draft") or ""))
            if conflict:
                return self._json({**conflict, **send_queue.snapshot(uid)}, 409)
            item = send_queue.enqueue(
                uid, pane["name"], str(body.get("text") or ""), body.get("media"),
                body.get("activity"), request_id, body.get("cursor"))
            item = _submit_codex_item(item, pane) or item
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
        if not pane:
            return self._json({"error": "Codex tmux 会话未连接",
                               **send_queue.snapshot(uid)}, 409)
        with CODEX_SEND_LOCK:
            driver = send_protocol.driver_for("codex")
            conflict = driver.overwrite_draft(
                pane["name"], str(body.get("overwrite_draft") or ""))
            if conflict:
                return self._json({**conflict, **send_queue.snapshot(uid)}, 409)
            item = send_queue.retry(item_id, body.get("activity"), uid)
            if not item:
                return self._json({"error": "待发送消息不存在"}, 404)
            _submit_codex_item(item, pane)
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
        # A ``confirming`` receipt only records that paste/Enter reached the
        # TUI.  Removing it never resends anything, and once Codex has moved on
        # without recording the text (BUG-20260912-014830-879a23: it merged
        # into a terminal draft) no native record will ever retire it.
        if not send_queue.discard(item_id, uid, {
                "injecting", "confirming", "failed", "aborted", "restored"}):
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
        raw_pids = live.pids_of(s, force=True)
        sessions = index.cached()
        if not any(row.get("uid") == s["uid"] for row in sessions):
            sessions = [*sessions, s]
        _uids, owned = live.active_processes(sessions)
        pids = owned.get(s["uid"], [])
        if raw_pids and not pids:
            return self._json({
                "error": "该回滚分支的运行实例已转移到更新的子会话，请先处理当前子会话"
            }, 409)
        panes = term.list_sessions()
        pane = _pane_for_session(s, panes, pids if raw_pids else None)
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

        raw_pids = live.pids_of(s, force=True)
        sessions = index.cached()
        if not any(row.get("uid") == s["uid"] for row in sessions):
            sessions = [*sessions, s]
        _uids, owned = live.active_processes(sessions)
        pids = owned.get(s["uid"], [])
        if raw_pids and not pids:
            return self._json({
                "error": "该回滚分支已不是当前运行分支，未停止共享的子会话"
            }, 409)
        panes = term.list_sessions()
        pane = _pane_for_session(s, panes, pids if raw_pids else None)

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
        reply = self._json
        def execute():
            captured = []
            self._json = lambda obj, code=200: captured.append((code, obj))
            try:
                self._create_session_once(body)
                return captured[0]
            finally:
                self._json = reply
        status, result = create_requests.run(body, execute)
        return reply(result, status)

    def _create_session_once(self, body: dict):
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

    def _set_terminal_backend(self, body: dict):
        """选择本机新建会话用哪个终端后端。已在跑的会话不受影响。"""
        if not TERMINAL:
            return self._json({"error": "终端未启用"}, 403)
        previous = term.backend_name()
        try:
            chosen = term.set_backend(str(body.get("backend") or ""))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        if chosen != previous:
            audit.record("terminal.backend.changed", category="terminal",
                         data={"from": previous, "to": chosen})
        return self._json({"ok": True, "backend": chosen, "backends": term.backends()})

    def _new_session_status(self, q: dict):
        """等待 CLI 落盘后，把临时 tmux 名称关联到真正的 agenthub 会话。"""
        if not TERMINAL:
            return self._json({"error": "终端未启用"}, 403)
        name = q.get("name", [""])[0]
        pending = pending_store.get(name)
        if pending and pending.get("resolved"):
            _NEW_STATUS_REFRESH_AT.pop(name, None)
            return self._json(pending["resolved"])
        if not pending:
            # This is a polling state, not an exceptional resource lookup.  A
            # kill can race with an already-dispatched poll; return a normal
            # terminal state so browsers and reverse proxies do not report a
            # spurious HTTP error after a successful shutdown.
            _NEW_STATUS_REFRESH_AT.pop(name, None)
            return self._json({"gone": True})

        # 签名包含路径、mtime 和大小；新文件/首条消息会自然触发重建。
        # 不能在 750ms 状态轮询里强制全量解析所有会话。
        # 刚建出的会话要尽快关联，仍每次走 index.load()（本身有 CHECK_TTL）。
        # 但一条长时间没落盘的记录（例如停在提示符、从未发过消息的 CLI）会让
        # 浏览器把 inventory 扫描永久钉在 750ms 轮询上；超过快速窗口后改为
        # 限频刷新，关联最多晚几秒，不影响正常新建体验。
        now = time.time()
        started = float(pending.get("started") or now)
        if (now - started <= NEW_STATUS_FAST_WINDOW
                or now - _NEW_STATUS_REFRESH_AT.get(name, 0.0) >= NEW_STATUS_SLOW_INTERVAL):
            _NEW_STATUS_REFRESH_AT[name] = now
            sessions = index.load()
        else:
            sessions = index.cached()
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
        except Exception as e:
            # 会话在列表里却接不上（宿主进程坏了、tmux attach 失败）。原因必须
            # 进审计并随 close 帧带给浏览器，否则整条链路只剩"1011 attach failed"。
            reason = str(e) or e.__class__.__name__
            audit.record(
                "terminal.connection.failed", category="terminal",
                severity="error", page_id=page, connection_id=connection_id,
                data={"tmux": name, "reason": reason},
            )
            TERM_OWNERS.release(name, token)
            connection.closed.set()
            wsock.close(sock, 1011, f"attach failed: {reason}")
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
        if path == '/files.html':
            query = parse_qs(urlparse(getattr(self, 'path', '')).query, keep_blank_values=True)
            if query.get('open', [''])[0] == '1':
                # Cached conversation tabs may still link to the old intermediary.
                # Redirect before any file manager HTML can reach the browser.
                return self._send(303, b'', 'text/plain', {
                    'Location': 'file.html?' + urlencode(query, doseq=True), 'Cache-Control': 'no-store'})
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        data = f.read_bytes()
        if f.name in {"index.html", "files.html", "file.html"}:
            hub_mode = getattr(getattr(self, "server", None), "hub_mode", HUB_MODE)
            data = data.replace(b"__AGENTHUB_MODE__", b"hub" if hub_mode else b"local")
            data = data.replace(b"__AGENTHUB_HOSTNAME__",
                                html.escape("AgentHub" if hub_mode else HOSTNAME).encode("utf-8"))
            data = data.replace(b"__AGENTHUB_ASSET_VERSION__",
                                ASSET_VERSION.encode("ascii"))
        cache = "no-store" if f.name in {"index.html", "files.html", "file.html"} else "no-cache"
        self._send(200, data, ctype, {"Cache-Control": cache})


def main():
    ap = argparse.ArgumentParser(description="Claude/Codex/Grok 会话管理服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--allow", default="",
                    help="除本机外允许访问的 IP 或 CIDR, 逗号分隔")
    ap.add_argument("--terminal", action="store_true",
                    help="开启远程终端。这等于给白名单 IP 开放本机 shell, 谨慎使用")
    ap.add_argument("--terminal-backend", default="auto",
                    choices=["auto", "tmux", "ptyhost", "host"],
                    help="终端后端初始默认值: ptyhost 或 tmux（host 是 ptyhost 的旧名）。"
                         "auto = ptyhost；网页里选过的值优先于此参数")
    ap.add_argument("--node-token-file", type=Path, help="Hub 节点凭据文件（至少 32 字符）")
    ap.add_argument("--node-id-file", type=Path, help="持久节点身份文件；默认保存在本机数据目录")
    args = ap.parse_args()

    global TERMINAL, NODE_TOKEN, NODE_ID
    NODE_ID = federation.identity(args.node_id_file)
    if args.node_token_file:
        NODE_TOKEN = args.node_token_file.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,256}", NODE_TOKEN):
            ap.error("node token must contain 32–256 characters")
    TERMINAL = args.terminal
    # 服务本身不是 CLI 会话。若它是从某条 Claude/Codex/Grok 会话里启动的, 继承的
    # 身份会一路传给 ptyhost 宿主, 让网页新建的每条会话都被认成那条会话的孩子。
    for key in live.SPAWN_ENV_KEYS:
        os.environ.pop(key, None)
    backend = term.configure(args.terminal_backend)
    if TERMINAL and not term.available():
        print(f"[agenthub] 警告: 终端后端 {backend} 不可用, 终端功能关闭 ({term.unavailable_reason()})")
        TERMINAL = False
    elif TERMINAL:
        print(f"[agenthub] 终端后端: {backend}")

    if TERMINAL:
        send_queue.fail_unsubmitted()
        threading.Thread(target=_outbox_loop, daemon=True, name="agenthub-outbox").start()

    ALLOWED_IPS.update({"127.0.0.1", "::1", "localhost"})
    try:
        for value in args.allow.split(","):
            _add_allowed(value)
    except ValueError as exc:
        ap.error(f"--allow 包含无效的 IP/CIDR: {exc}")

    threading.Thread(target=index.load, daemon=True).start()  # 后台预热索引
    threading.Thread(target=_spawn_watch_loop, daemon=True, name="agenthub-spawn-watch").start()

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
