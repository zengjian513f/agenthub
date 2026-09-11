"""ptyhost 后端: 每个会话一个独立进程 + 本地 socket。

对外提供与 tmux 后端相同的函数面, 由 term.py 统一调度。ptyhost 本体是 Rust 二进制
(源码在 host-rs/), 这里只负责把它拉起来并作为客户端和它对话。宿主进程与 Web 服务
互不牵连, Linux 下尽量放进独立的 systemd scope, Windows 下以脱离作业对象的
独立进程启动, 这样重启 agenthub 服务不会结束 CLI。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import audit
from .host import client, procs

PREFIX = "agenthub-"
WINDOWS = sys.platform == "win32"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BINARY_NAME = "ptyhost.exe" if WINDOWS else "ptyhost"
_submit_locks: dict[str, threading.Lock] = {}
_submit_locks_guard = threading.Lock()


def host_binary() -> str | None:
    """定位宿主二进制。AGENTHUB_HOST_BIN 优先, 然后是构建产物, 最后看 PATH。"""
    raw = os.environ.get("AGENTHUB_HOST_BIN")
    if raw:
        path = Path(raw).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    for candidate in (PROJECT_ROOT / "host-rs" / "target" / "release" / BINARY_NAME,
                      PROJECT_ROOT / "host-rs" / "target" / "debug" / BINARY_NAME,
                      PROJECT_ROOT / "bin" / BINARY_NAME):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(BINARY_NAME)


def available() -> bool:
    return host_binary() is not None


def unavailable_reason() -> str:
    if host_binary() is None:
        return ("服务器未安装 ptyhost，无法打开控制台"
                "（在 host-rs/ 执行 cargo build --release，或从构建机拷一份到 bin/）。")
    return ""


def list_sessions() -> list[dict]:
    return client.list_sessions()


def session_info(name: str) -> dict | None:
    info = client.session_info(name)
    return client.public_row(info) if info else None


def has_session(name: str) -> bool:
    return client.session_info(name) is not None


def _argv_for(cmd: str | list[str]) -> list[str]:
    if isinstance(cmd, list):
        return list(cmd)
    if WINDOWS:
        return ["cmd.exe", "/d", "/c", cmd]
    return ["/bin/sh", "-c", cmd]


def _env_wrapper() -> list[str]:
    """systemd 服务没有交互 shell 的环境; 与 agenthub-tmux-host 一样经 wrapper 启动。"""
    if WINDOWS:
        return []
    raw = os.environ.get("AGENTHUB_HOST_ENV_WRAPPER")
    if raw == "":
        return []
    path = Path(raw).expanduser() if raw else Path.home() / ".local" / "bin" / "with-zshrc"
    return [str(path)] if path.is_file() and os.access(path, os.X_OK) else []


def _unit_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    return f"agenthub-session-{safe}-{uuid.uuid4().hex[:6]}.scope"


def _use_scope() -> bool:
    if WINDOWS or os.environ.get("AGENTHUB_HOST_SCOPE", "1") == "0":
        return False
    if not shutil.which("systemd-run"):
        return False
    return bool(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def _spawn(launch: list[str], name: str, scope: bool) -> subprocess.Popen:
    log_path = client.host_dir() / f"{name}.log"
    log = open(log_path, "wb")
    try:
        if WINDOWS:
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                     | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
            return subprocess.Popen(launch, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, creationflags=flags, close_fds=True)
        argv = list(launch)
        if scope:
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                    "--unit", _unit_name(name), "--description", f"AgentHub session {name}",
                    *argv]
        return subprocess.Popen(argv, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True, close_fds=True)
    finally:
        log.close()


def _log_tail(name: str) -> str:
    try:
        text = (client.host_dir() / f"{name}.log").read_text(errors="replace")
    except OSError:
        return ""
    return text.strip()[-600:]


def new_session(name: str, cmd: str | list[str], cwd: str | None = None,
                cols: int = 120, rows: int = 32) -> str:
    full = name if name.startswith(PREFIX) else PREFIX + name
    if has_session(full):
        raise RuntimeError(f"会话已存在: {full}")
    directory = client.host_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    binary = host_binary()
    if not binary:
        raise RuntimeError(unavailable_reason())
    argv = [*_env_wrapper(), *_argv_for(cmd)]
    launch = [binary, "--dir", str(directory), "run",
              "--name", full, "--cols", str(cols), "--rows", str(rows)]
    if cwd and os.path.isdir(cwd):
        launch += ["--cwd", cwd]
    launch += ["--", *argv]

    attempts = [True, False] if _use_scope() else [False]
    info = None
    for scope in attempts:
        try:
            proc = _spawn(launch, full, scope)
        except OSError as e:
            raise RuntimeError(f"会话启动失败: {e}") from None
        info = client.wait_for(full, directory, timeout=8.0, alive=lambda: proc.poll() is None)
        if info:
            break
        if scope and proc.poll() not in (None, 0):
            continue                          # systemd-run 本身失败, 退回普通启动
        break
    if not info:
        tail = _log_tail(full)
        raise RuntimeError(f"会话启动失败: {tail or '宿主进程未就绪'}")
    audit.record(
        "host.session.created", category="terminal",
        data={"tmux": full, "cwd": cwd or "", "cols": cols, "rows": rows,
              "host_pid": info.get("host_pid"), "pid": info.get("pid")},
        content={"command": cmd if isinstance(cmd, str) else shlex.join(cmd)},
    )
    return full


def kill_session(name: str, timeout: float = 4.0) -> bool:
    if not has_session(name):
        return False
    try:
        client.request(name, "kill")
    except RuntimeError:
        if not has_session(name):
            return False
        raise
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not has_session(name):
            break
        time.sleep(0.05)
    else:
        try:
            client.request(name, "kill", force=True)
        except RuntimeError:
            pass
    audit.record("host.session.killed", category="terminal", data={"tmux": name})
    return True


def rename_session(old: str, new: str) -> str:
    full = new if new.startswith(PREFIX) else PREFIX + new
    if full != old and has_session(full):
        raise RuntimeError(f"会话已存在: {full}")
    client.request(old, "rename", to=full)
    audit.record("host.session.renamed", category="terminal", data={"from": old, "to": full})
    return full


def send_text(name: str, text: str) -> None:
    client.request(name, "send", text=text)
    audit.record("host.text.sent", category="terminal",
                 data={"tmux": name, "chars": len(text)}, content=text)


def submit_text(name: str, text: str) -> None:
    """整段粘贴后再回车; 应用开启 bracketed paste 时由宿主包上起止序列。"""
    with _submit_locks_guard:
        lock = _submit_locks.setdefault(name, threading.Lock())
    with lock:
        audit.record("host.submit.started", category="terminal",
                     data={"tmux": name, "chars": len(text)}, content=text)
        client.request(name, "paste", text=text)
        time.sleep(0.04)
        client.request(name, "keys", keys=["Enter"])
        audit.record("host.submit.completed", category="terminal",
                     data={"tmux": name, "chars": len(text)}, content=text)


def send_keys(name: str, *keys: str) -> None:
    client.request(name, "keys", keys=list(keys))
    audit.record("host.keys.sent", category="terminal",
                 data={"tmux": name, "keys": list(keys)})


def leave_copy_mode(name: str) -> None:
    return None                               # 宿主没有 copy-mode


def scroll(name: str, up: bool, lines: int = 3) -> int:
    return 0                                  # 浏览器 xterm 自己滚动


def _capture(name: str, **kw) -> dict:
    return client.request(name, "capture", **kw)


def capture(name: str, lines: int = 200) -> str:
    return _capture(name, kind="scrollback", lines=lines, styled=True, join=False)["text"]


def capture_history(name: str, lines: int = 10000) -> str:
    return _capture(name, kind="scrollback", lines=lines, styled=True, join=True)["text"]


def capture_plain(name: str, lines: int = 80) -> str:
    return _capture(name, kind="scrollback", lines=lines, styled=False, join=True)["text"]


def capture_screen_plain(name: str) -> str:
    return _capture(name, kind="screen", styled=False, join=True)["text"]


def capture_screen(name: str) -> str:
    return _capture(name, kind="screen", styled=True, join=False)["text"]


def capture_screen_state(name: str) -> tuple[str, tuple[int, int]]:
    reply = _capture(name, kind="screen", styled=True, join=False)
    x, y = reply["cursor"]
    return reply["text"], (int(x), int(y))


def cursor_position(name: str) -> tuple[int, int]:
    reply = client.request(name, "cursor")
    return int(reply["x"]), int(reply["y"])


def hosts(pids: list[int]) -> bool:
    """这些进程是否跑在某个宿主会话里 (祖先链含会话的 CLI 根进程)。"""
    roots = {row["pid"] for row in list_sessions() if row["pid"] > 0}
    if not roots:
        return False
    return any(procs.ancestor_matches(pid, lambda p: p in roots) for pid in pids if pid > 0)


Attach = client.Attach
