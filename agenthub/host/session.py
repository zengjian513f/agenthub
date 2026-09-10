"""单个托管会话的宿主进程。

一个会话 = 一个 pty 里的 CLI + 一个本地 socket。宿主进程与 agenthub Web 服务
互不依赖: Web 服务重启不影响 CLI, 宿主随 CLI 退出而退出 (对应 tmux 的
remain-on-exit off)。所有输出都喂给屏幕模型, 以便回答 capture/cursor 查询和
attach 时回放历史; 实时字节原样转发给已连接的客户端。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from . import ptyio
from .protocol import (FRAME_DATA, FRAME_EXIT, FRAME_RESIZE, ProtocolError, key_bytes,
                       pack_frame, read_frames, recv_json, send_json)
from .screen import Screen, strip_sgr

WINDOWS = sys.platform == "win32"
# 新会话必须清掉可能从 Web 服务继承的旧会话身份, 否则 CLI 会接上错误的会话。
STRIP_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_COMPANION_SESSION_ID", "GROK_SESSION_ID", "TMUX")


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    if not WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.dead = False

    def send(self, frame: bytes) -> None:
        if self.dead:
            return
        with self.lock:
            try:
                self.sock.sendall(frame)
            except OSError:
                self.dead = True


class Session:
    def __init__(self, name: str, argv: list[str], cwd: str | None, cols: int, rows: int,
                 meta: dict | None, directory: Path, history: int = 10000):
        self.name = name
        self.argv = argv
        self.cwd = cwd or None
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.meta = meta or {}
        self.directory = directory
        self.created = int(time.time())
        self.token = secrets.token_urlsafe(24) if WINDOWS else ""
        self.screen = Screen(self.cols, self.rows, history_limit=history)
        self.lock = threading.RLock()
        self.clients: list[_Client] = []
        self.exit_code: int | None = None
        self.exited = threading.Event()
        self.stopping = threading.Event()
        self.pty: ptyio.BasePty | None = None
        self.listener: socket.socket | None = None
        self.port = 0

    # ----------------------------------------------------------- 生命周期
    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.setdefault("TERM", "xterm-256color")
        env["COLORTERM"] = "truecolor"
        env["AGENTHUB_SESSION"] = self.name
        self.directory.mkdir(parents=True, exist_ok=True)
        if not WINDOWS:
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                pass
        self.listener = self._listen()
        self.pty = ptyio.Pty(self.argv, self.cwd, env, self.cols, self.rows)
        self.write_info()
        threading.Thread(target=self._reader, daemon=True, name="pty-reader").start()
        threading.Thread(target=self._accept, daemon=True, name="acceptor").start()

    def _listen(self) -> socket.socket:
        if WINDOWS:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        else:
            path = self.directory / f"{self.name}.sock"
            try:
                path.unlink()
            except OSError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
            os.chmod(path, 0o600)
        sock.listen(32)
        return sock

    def info(self) -> dict:
        info = {
            "name": self.name, "host_pid": os.getpid(), "pid": self.pty.pid if self.pty else 0,
            "cwd": self.cwd or "", "cmd": os.path.basename(self.argv[-1] if len(self.argv) == 1
                                                          else self._cmd_label()),
            "argv": self.argv, "created": self.created, "cols": self.cols, "rows": self.rows,
            "attached": bool(self.clients), "meta": self.meta,
        }
        if WINDOWS:
            info.update({"port": self.port, "token": self.token})
        else:
            info["sock"] = str(self.directory / f"{self.name}.sock")
        return info

    def _cmd_label(self) -> str:
        for item in self.argv:
            base = os.path.basename(item)
            if base in ("claude", "codex", "grok", "claude.exe", "codex.exe", "grok.exe"):
                return base
        return self.argv[0]

    def write_info(self) -> None:
        _atomic_write(self.directory / f"{self.name}.json",
                      json.dumps(self.info(), ensure_ascii=False))

    def cleanup(self) -> None:
        for suffix in (".json", ".sock"):
            try:
                (self.directory / f"{self.name}{suffix}").unlink()
            except OSError:
                pass
        log = self.directory / f"{self.name}.log"
        try:
            if log.is_file() and log.stat().st_size == 0:
                log.unlink()                     # 空日志没有保留价值
        except OSError:
            pass
        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass

    def serve(self) -> int:
        """阻塞到 CLI 退出; 清理后返回退出码。"""
        try:
            while not self.exited.is_set():
                self.exited.wait(0.5)
                if self.pty and not self.pty.alive() and not self.exited.is_set():
                    # 输出已读尽但 EOF 未到 (极少数平台), 兜底判定退出
                    time.sleep(0.2)
                    if not self.exited.is_set():
                        self._finish()
        finally:
            self.cleanup()
        return self.exit_code or 0

    def stop(self, force: bool = False) -> None:
        self.stopping.set()
        if self.pty:
            self.pty.terminate(force=force)

    # -------------------------------------------------------------- 输出
    def _reader(self) -> None:
        assert self.pty is not None
        while True:
            data = self.pty.read(0.2)
            if data is None:
                break
            if not data:
                continue
            with self.lock:
                self.screen.feed(data)
                responses = self.screen.responses
                self.screen.responses = []
                clients = [c for c in self.clients if not c.dead]
            if responses and not clients:
                # 没有终端连着时由宿主代答光标位置/设备属性查询, 免得 CLI 空等
                for chunk in responses:
                    self.pty.write(chunk)
            frame = pack_frame(FRAME_DATA, data)
            for client in clients:
                client.send(frame)
        self._finish()

    def _finish(self) -> None:
        if self.exited.is_set():
            return
        assert self.pty is not None
        # pty 已到 EOF 但进程可能还在收尾 (或忽略了 HUP); 给 3 秒后 KILL
        deadline = time.monotonic() + 3.0
        while self.pty.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.pty.alive():
            self.pty.terminate(force=True)
            deadline = time.monotonic() + 2.0
            while self.pty.alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        self.exit_code = self.pty.exit_code()
        with self.lock:
            clients = list(self.clients)
            self.clients = []
        frame = pack_frame(FRAME_EXIT, json.dumps({"code": self.exit_code or 0}).encode())
        for client in clients:
            client.send(frame)
            try:
                client.sock.close()
            except OSError:
                pass
        self.exited.set()

    # -------------------------------------------------------------- 连接
    def _accept(self) -> None:
        assert self.listener is not None
        while not self.exited.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        buffer = bytearray()
        conn.settimeout(10)
        try:
            try:
                req = recv_json(conn, buffer)
            except ProtocolError as e:
                send_json(conn, {"ok": False, "error": str(e)})
                return
            if WINDOWS and req.get("token") != self.token:
                send_json(conn, {"ok": False, "error": "凭据不匹配"})
                return
            op = str(req.get("op") or "")
            if op == "attach":
                self._attach(conn, buffer, req)
                return
            try:
                reply = self._dispatch(op, req)
            except (ValueError, KeyError, TypeError) as e:
                reply = {"ok": False, "error": str(e) or op}
            send_json(conn, reply)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, op: str, req: dict) -> dict:
        assert self.pty is not None
        if op == "info":
            return {"ok": True, "info": self.info(), "exited": self.exited.is_set()}
        if op == "send":
            text = str(req.get("text", ""))
            self.pty.write(text.encode("utf-8"))
            return {"ok": True}
        if op == "keys":
            keys = [str(k) for k in req.get("keys", [])]
            with self.lock:
                app_cursor = self.screen.app_cursor
            self.pty.write(b"".join(key_bytes(k, app_cursor) for k in keys))
            return {"ok": True}
        if op == "paste":
            text = str(req.get("text", "")).encode("utf-8")
            with self.lock:
                bracketed = self.screen.bracketed_paste
            if bracketed and req.get("bracketed", True):
                text = b"\x1b[200~" + text + b"\x1b[201~"
            self.pty.write(text)
            return {"ok": True, "bracketed": bracketed}
        if op == "resize":
            self._resize(int(req["cols"]), int(req["rows"]))
            return {"ok": True}
        if op == "capture":
            return self._capture(req)
        if op == "cursor":
            with self.lock:
                x, y = self.screen.cursor
                return {"ok": True, "x": x, "y": y, "visible": self.screen.cursor_visible,
                        "alt": self.screen.alt}
        if op == "rename":
            return self._rename(str(req.get("to") or ""))
        if op == "kill":
            self.stop(force=bool(req.get("force")))
            return {"ok": True}
        raise ValueError(f"未知操作: {op}")

    def _capture(self, req: dict) -> dict:
        kind = str(req.get("kind") or "screen")
        styled = bool(req.get("styled", True))
        join = bool(req.get("join", False))
        lines = int(req.get("lines") or 0)
        with self.lock:
            if kind == "screen":
                rows = self.screen.screen_lines(styled=styled, join=join)
            elif kind == "scrollback":
                rows = self.screen.scrollback_lines(lines, styled=styled, join=join)
            else:
                raise ValueError(f"未知捕获类型: {kind}")
            x, y = self.screen.cursor
            alt = self.screen.alt
        return {"ok": True, "text": "\n".join(rows), "cursor": [x, y], "alt": alt,
                "cols": self.cols, "rows": self.rows}

    def _resize(self, cols: int, rows: int) -> None:
        cols, rows = max(1, cols), max(1, rows)
        with self.lock:
            if (cols, rows) == (self.cols, self.rows):
                return
            self.cols, self.rows = cols, rows
            self.screen.resize(cols, rows)
        assert self.pty is not None
        self.pty.resize(cols, rows)
        self.write_info()

    def _rename(self, new: str) -> dict:
        if not new or "/" in new or "\\" in new or new.startswith("."):
            raise ValueError("会话名不合法")
        if new == self.name:
            return {"ok": True, "name": new}
        if (self.directory / f"{new}.json").exists():
            raise ValueError(f"会话已存在: {new}")
        old = self.name
        with self.lock:
            if not WINDOWS:
                new_listener = None
                path = self.directory / f"{new}.sock"
                try:
                    path.unlink()
                except OSError:
                    pass
                new_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                new_listener.bind(str(path))
                os.chmod(path, 0o600)
                new_listener.listen(32)
                old_listener = self.listener
                self.listener = new_listener
                threading.Thread(target=self._accept, daemon=True, name="acceptor").start()
                if old_listener:
                    try:
                        old_listener.close()
                    except OSError:
                        pass
            self.name = new
            self.write_info()
            for suffix in (".json", ".sock"):
                try:
                    (self.directory / f"{old}{suffix}").unlink()
                except OSError:
                    pass
        return {"ok": True, "name": new}

    def _attach(self, conn: socket.socket, buffer: bytearray, req: dict) -> None:
        assert self.pty is not None
        if self.exited.is_set():
            send_json(conn, {"ok": False, "error": "会话已结束"})
            return
        cols = int(req.get("cols") or self.cols)
        rows = int(req.get("rows") or self.rows)
        client = _Client(conn)
        with self.lock:
            self._resize(cols, rows)
            replay = b""
            if req.get("replay", True):
                history = [t for t, _ in self.screen.history]
                if history:
                    replay = ("\r\n".join(history) + "\x1b[0m\r\n").encode("utf-8", "replace")
                replay += self.screen.redraw_bytes()
            self.clients.append(client)
        send_json(conn, {"ok": True, "cols": self.cols, "rows": self.rows})
        self.write_info()
        conn.settimeout(None)
        if replay:
            client.send(pack_frame(FRAME_DATA, replay))
        try:
            while not client.dead and not self.exited.is_set():
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                for kind, payload in read_frames(buffer):
                    if kind == FRAME_DATA:
                        self.pty.write(payload)
                    elif kind == FRAME_RESIZE:
                        try:
                            size = json.loads(payload)
                            self._resize(int(size["cols"]), int(size["rows"]))
                        except (ValueError, KeyError, TypeError):
                            pass
        finally:
            client.dead = True
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            if not self.exited.is_set():
                self.write_info()


def run(name: str, argv: list[str], cwd: str | None, cols: int, rows: int,
        meta: dict | None, directory: Path, history: int = 10000) -> int:
    session = Session(name, argv, cwd, cols, rows, meta, directory, history)

    def _on_signal(signum, _frame):
        session.stop(force=signum == getattr(signal, "SIGKILL", -1))

    for sig in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _on_signal)
            except (OSError, ValueError):
                pass
    session.start()
    return session.serve()
