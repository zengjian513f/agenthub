"""宿主会话的客户端: 扫描会话目录、发控制请求、建立 attach 流。"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import time
from pathlib import Path

from . import procs
from .protocol import (FRAME_DATA, FRAME_EXIT, FRAME_RESIZE, ProtocolError, pack_frame,
                       read_frames, recv_json, send_json)

WINDOWS = sys.platform == "win32"


def host_dir() -> Path:
    raw = os.environ.get("AGENTHUB_HOST_DIR")
    return Path(raw) if raw else Path.home() / ".local" / "share" / "agenthub" / "host"


def info_path(name: str, directory: Path | None = None) -> Path:
    return (directory or host_dir()) / f"{name}.json"


def sock_path(name: str, directory: Path | None = None) -> Path:
    return (directory or host_dir()) / f"{name}.sock"


def read_info(path: Path) -> dict | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("name") else None


def _stale(info: dict) -> bool:
    return procs.gone(int(info.get("host_pid") or 0))


def _remove(name: str, directory: Path) -> None:
    for path in (info_path(name, directory), sock_path(name, directory)):
        try:
            path.unlink()
        except OSError:
            pass


def list_sessions(directory: Path | None = None) -> list[dict]:
    directory = directory or host_dir()
    rows: list[dict] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return rows
    for path in entries:
        info = read_info(path)
        if not info or info["name"] != path.stem:
            continue
        if _stale(info):
            _remove(info["name"], directory)
            continue
        rows.append(public_row(info))
    return rows


def public_row(info: dict) -> dict:
    return {
        "name": info["name"], "created": int(info.get("created") or 0),
        "attached": bool(info.get("attached")), "pid": int(info.get("pid") or 0),
        "cwd": info.get("cwd") or "", "cmd": info.get("cmd") or "",
        "cols": int(info.get("cols") or 80), "rows": int(info.get("rows") or 24),
        "owned": True, "server": "ptyhost", "backend": "ptyhost",
        "host_pid": int(info.get("host_pid") or 0), "meta": info.get("meta") or {},
    }


def session_info(name: str, directory: Path | None = None) -> dict | None:
    directory = directory or host_dir()
    info = read_info(info_path(name, directory))
    if not info or info["name"] != name:
        return None
    if _stale(info):
        _remove(name, directory)
        return None
    return info


def connect(info: dict, timeout: float = 5.0) -> socket.socket:
    if WINDOWS or info.get("port"):
        sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout)
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(info["sock"])
    return sock


def request(name: str, op: str, directory: Path | None = None, timeout: float = 10.0,
            **payload) -> dict:
    info = session_info(name, directory)
    if not info:
        raise RuntimeError(f"会话不存在: {name}")
    body = {"op": op, "token": info.get("token", ""), **payload}
    sock = connect(info, timeout)
    try:
        send_json(sock, body)
        reply = recv_json(sock, bytearray())
    except (OSError, ProtocolError) as e:
        raise RuntimeError(f"宿主请求失败 ({op}): {e}") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error") or f"宿主拒绝 {op}"))
    return reply


def wait_for(name: str, directory: Path | None = None, timeout: float = 5.0,
             alive=None) -> dict | None:
    """等待宿主写出会话信息文件; alive() 返回 False 表示启动进程已提前退出。"""
    deadline = time.monotonic() + timeout
    while True:
        info = read_info(info_path(name, directory))
        if info and info.get("name") == name and not procs.gone(int(info.get("host_pid") or 0)):
            return info
        if alive is not None and not alive():
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.03)


class Attach:
    """帧模式连接, 提供与 tmux Attach 相同的 read/write/resize/alive/close。"""

    MIN_COLS = 20
    MIN_ROWS = 8

    def __init__(self, name: str, cols: int = 120, rows: int = 32,
                 replay: bool = True, directory: Path | None = None):
        info = session_info(name, directory)
        if not info:
            raise RuntimeError(f"会话不存在: {name}")
        if cols < self.MIN_COLS or rows < self.MIN_ROWS:
            cols, rows = 120, 32
        self.name = name
        self.cols, self.rows = cols, rows
        self._buffer = bytearray()
        self._pending: list[bytes] = []
        self._dead = False
        self.exit_code: int | None = None
        self.sock = connect(info)
        try:
            send_json(self.sock, {"op": "attach", "token": info.get("token", ""),
                                  "cols": cols, "rows": rows, "replay": replay})
            reply = recv_json(self.sock, self._buffer)
        except (OSError, ProtocolError) as e:
            self.sock.close()
            raise RuntimeError(f"attach 失败: {e}") from None
        if not reply.get("ok"):
            self.sock.close()
            raise RuntimeError(str(reply.get("error") or "attach 被拒绝"))
        self.sock.settimeout(None)
        self.sock.setblocking(False)
        self._drain()                       # 应答后紧跟的回放帧可能已被一并读入

    def _drain(self) -> None:
        for kind, payload in read_frames(self._buffer):
            if kind == FRAME_DATA:
                self._pending.append(payload)
            elif kind == FRAME_EXIT:
                try:
                    self.exit_code = int(json.loads(payload or b"{}").get("code", 0))
                except ValueError:
                    self.exit_code = 0
                self._dead = True

    def read(self, timeout: float = 0.05) -> bytes:
        if self._pending:
            data, self._pending = b"".join(self._pending), []
            return data
        if self._dead:
            return b""
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            self._dead = True
            return b""
        if not r:
            return b""
        try:
            chunk = self.sock.recv(262144)
        except BlockingIOError:
            return b""
        except OSError:
            self._dead = True
            return b""
        if not chunk:
            self._dead = True
            return b""
        self._buffer.extend(chunk)
        self._drain()
        if self._pending:
            data, self._pending = b"".join(self._pending), []
            return data
        return b""

    def _send(self, frame: bytes) -> None:
        view = memoryview(frame)
        while view:
            try:
                n = self.sock.send(view)
            except BlockingIOError:
                select.select([], [self.sock], [], 1.0)
                continue
            except OSError:
                self._dead = True
                return
            view = view[n:]

    def write(self, data: bytes) -> None:
        if data:
            self._send(pack_frame(FRAME_DATA, bytes(data)))

    def resize(self, cols: int, rows: int) -> bool:
        if cols < self.MIN_COLS or rows < self.MIN_ROWS:
            return False
        self.cols, self.rows = cols, rows
        self._send(pack_frame(FRAME_RESIZE, json.dumps({"cols": cols, "rows": rows}).encode()))
        return True

    def alive(self) -> bool:
        return not self._dead

    def close(self) -> None:
        self._dead = True
        try:
            self.sock.close()
        except OSError:
            pass
