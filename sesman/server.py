"""sesman —— Claude / Codex / Grok 会话统一浏览服务 (纯标准库)。"""

from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import index, live, media, pending as pending_store, term, wsock

STATIC = Path(__file__).parent / "static"
ALLOWED_IPS: set[str] = set()
TERMINAL = False        # 远程终端 = 远程执行, 必须显式 --terminal 打开
WATCH_POLL = 0.05       # 服务端盯文件的间隔; stat 一个文件是微秒级, 这里很便宜
JSON_GZIP_MIN = 1024    # 小响应省不了多少，避免反而增加压缩 CPU 和头部体积
JSON_GZIP_LEVEL = 4     # 实测 4.5 MiB → 1.20 MiB / 75 ms，继续加级收益很小
ATTACHMENT_MAX_BYTES = 512 * 1024 * 1024
ATTACHMENT_DIR = "sesman_attachments"
ATTACHMENT_DIR_LOCK = threading.Lock()


def _pane_for_session(session: dict, panes: list[dict],
                      pids: list[int] | None = None) -> dict | None:
    """按规范名或真实进程树找会话所在的 sesman tmux pane。

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
    server_version = "sesman"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----------------------------------------------------
    # 这些是秒级轮询, 打出来只会淹没真正有用的日志
    QUIET = ("/api/live", "/api/term/list", "sig=", "start=")

    def log_message(self, format, *args):
        line = str(args[0])
        if "/api/" in line and not any(q in line for q in self.QUIET):
            print(f"[{self.address_string()}] {format % args}")

    def _allowed(self) -> bool:
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        return ip in ALLOWED_IPS

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        headers = {"Vary": "Accept-Encoding"}
        if len(body) >= JSON_GZIP_MIN and _accepts_gzip(
                self.headers.get("Accept-Encoding", "")):
            packed = gzip.compress(body, compresslevel=JSON_GZIP_LEVEL, mtime=0)
            if len(packed) < len(body):
                body = packed
                headers["Content-Encoding"] = "gzip"
        self._send(code, body, "application/json; charset=utf-8", headers)

    # ---- 路由 --------------------------------------------------------
    def do_POST(self):
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        if not TERMINAL:
            return self._json({"error": "终端未启用, 服务端需加 --terminal"}, 403)
        if u.path == "/api/session/attachment":
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
        try:
            if u.path == "/api/term/create":
                return self._create_session(body)
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
                else:
                    text = body.get("text", "")
                    enter = body.get("enter", True)
                    if text and enter:
                        term.submit_text(name, text)
                    elif text:
                        term.send_text(name, text)
                    elif enter:
                        term.send_keys(name, "Enter")
                return self._json({"ok": True})
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
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
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
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
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
        self._json({"ok": True, "trash": dest})

    def _api_get(self, path: str, q: dict):
        if path == "/api/sessions":
            force = q.get("force", ["0"])[0] == "1"
            known = q.get("sig", [""])[0]
            if known and not force and known == index.signature():
                return self._json({"unchanged": True, "sig": known})
            sessions = index.load(force=force)
            return self._json({"sessions": index.with_cursors(sessions), "sig": index._state["sig"],
                               "built_at": index._state["built_at"]})

        if path == "/api/live":
            force = q.get("force", ["0"])[0] == "1"
            sessions = index.load()
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
            if tmux_sessions:
                # 把 tmux pane 映射回当前列表 uid。前端不能只从 pane 名猜 UUID，
                # 因为 Codex 回退分支会沿用父会话启动时的旧名字。
                linked: dict[str, dict] = {}
                for session in index.load():
                    pane = _pane_for_session(session, tmux_sessions)
                    if pane and (pane["name"] not in linked
                                 or session["updated"] > linked[pane["name"]]["updated"]):
                        linked[pane["name"]] = session
                for pane in tmux_sessions:
                    if pane["name"] in linked:
                        pane["uid"] = linked[pane["name"]]["uid"]
            pending = pending_store.active({x["name"] for x in tmux_sessions}) if TERMINAL else []
            public_pending = [{k: row.get(k) for k in
                               ("name", "source", "sid", "cwd", "started", "cols", "rows")}
                              for row in pending]
            return self._json({"enabled": TERMINAL and term.available(),
                               "sources": term.available_sources() if TERMINAL else {},
                               "home": str(Path.home()),
                               "sessions": tmux_sessions,
                               "pending": public_pending})

        if path == "/api/term/new-status":
            return self._new_session_status(q)

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
                return self._search_stream(query, srcs, word=on("word"),
                                           case=on("case"), regex=on("regex"))
            try:
                return self._json(index.search(
                    query, srcs, word=on("word"), case=on("case"), regex=on("regex")))
            except re.error as e:
                return self._json({"error": f"正则无效: {e}"}, 400)

        if path.startswith("/api/messages/"):
            uid = unquote(path[len("/api/messages/"):])
            return self._json(index.messages(
                uid,
                agent=q.get("agent", [""])[0],
                start=int(q.get("start", ["0"])[0]),
                head=q.get("head", [""])[0],
                anchor=q.get("anchor", [""])[0],
                append_only=q.get("append", ["0"])[0] == "1",
                windowed=q.get("window", ["0"])[0] == "1",
            ))

        raise KeyError(path)

    def _search_stream(self, query: str, sources, **opts):
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

        start = int(q.get("start", ["0"])[0])
        head = q.get("head", [""])[0]
        anchor = q.get("anchor", [""])[0]
        last = None
        beat = time.time()
        try:
            while True:
                ver = index.version(s)
                if ver != last:
                    last = ver
                    # 用 messages_for 而不是 messages: 后者要过一遍索引,
                    # 而文件刚变过, 签名对不上就会重建整个索引(百毫秒级)
                    d = index.messages_for(s, start=start, head=head, anchor=anchor)
                    if d["reset"] or d["messages"] or d["activity_changed"]:
                        payload = json.dumps(d, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    start, head, anchor = d["end"], d["version"]["head"], d["anchor"]
                    beat = time.time()
                elif time.time() - beat > 20:     # 心跳, 让中间的代理别掐连接
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()
                time.sleep(WATCH_POLL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                  # 客户端走了
        finally:
            self.close_connection = True

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
        return self._json({"ok": True, "stopped": bool(pane or killed),
                           "tmux": bool(pane)})

    def _create_session(self, body: dict):
        """用固定 CLI 白名单新建会话；不接受浏览器传入的任意命令。"""
        source = str(body.get("source") or "")
        before = {str(s["sid"]) for s in index.load() if s["source"] == source}
        cols, rows = int(body.get("cols", 120)), int(body.get("rows", 32))
        info = term.new_cli_session(
            source, str(body.get("cwd") or ""),
            cols, rows,
        )
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
        """等待 CLI 落盘后，把临时 tmux 名称关联到真正的 sesman 会话。"""
        if not TERMINAL:
            return self._json({"error": "终端未启用"}, 403)
        name = q.get("name", [""])[0]
        pending = pending_store.get(name)
        if pending and pending.get("resolved"):
            return self._json(pending["resolved"])
        if not pending:
            return self._json({"error": "新会话记录不存在或已过期", "gone": True}, 404)

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
        if not name or not any(s["name"] == name for s in term.list_sessions()):
            return self._send(404, b"no such tmux session", "text/plain")
        if not wsock.handshake(self):
            return self._send(400, b"expected websocket", "text/plain")

        sock = self.connection
        att = term.Attach(name, int(q.get("cols", ["120"])[0]), int(q.get("rows", ["32"])[0]))
        stop = threading.Event()

        def pump():                      # tmux → 浏览器
            while not stop.is_set():
                data = att.read(0.05)
                if data:
                    try:
                        wsock.send(sock, data, wsock.OP_BIN)
                    except OSError:
                        break
                elif not att.alive():
                    break
            stop.set()

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            while not stop.is_set():     # 浏览器 → tmux
                op, payload = wsock.recv(sock)
                if op == wsock.OP_CLOSE:
                    break
                if op == wsock.OP_PING:
                    wsock.send(sock, payload, wsock.OP_PONG)
                    continue
                if op == wsock.OP_TEXT and payload[:1] == b"{":
                    try:                 # 控制消息只有一种: 改窗口大小
                        m = json.loads(payload)
                        if m.get("t") == "resize":
                            att.resize(int(m["cols"]), int(m["rows"]))
                            continue
                    except Exception:
                        pass
                att.write(payload)
        except (ConnectionError, OSError):
            pass
        finally:
            stop.set()
            att.close()
            wsock.close(sock)
            self.close_connection = True

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})


def main():
    ap = argparse.ArgumentParser(description="Claude/Codex/Grok 会话管理服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--allow", default="192.0.2.134",
                    help="除本机外允许访问的 IP, 逗号分隔")
    ap.add_argument("--terminal", action="store_true",
                    help="开启 tmux 远程终端。这等于给白名单 IP 开放本机 shell, 谨慎使用")
    args = ap.parse_args()

    global TERMINAL
    TERMINAL = args.terminal
    if TERMINAL and not term.available():
        print("[sesman] 警告: 找不到 tmux, 终端功能不可用")
        TERMINAL = False

    ALLOWED_IPS.update({"127.0.0.1", "::1", "localhost"})
    ALLOWED_IPS.update(x.strip() for x in args.allow.split(",") if x.strip())

    threading.Thread(target=index.load, daemon=True).start()  # 后台预热索引

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[sesman] http://{args.host}:{args.port}  允许: {sorted(ALLOWED_IPS)}"
          + ("  [终端已开启]" if TERMINAL else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[sesman] 已停止")


if __name__ == "__main__":
    main()
