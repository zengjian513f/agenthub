"""tmux 后端: 把浏览器接到 tmux 会话上 —— 真正的远程控制。

共享的 CLI 命令拼装、目录校验和进程结束逻辑在 term.py; 这里只保留 tmux 特有实现。

为什么绕 tmux 而不是直接注入已有进程:
  - 现有会话是 sshd → zsh → claude 直连 pts, 外部无法写入它的输入队列
  - 内核的 TIOCSTI 注入早已默认关闭 (dev.tty.legacy_tiocsti = 0)
tmux 提供了合法的输入通道, 而且会话独立于 agenthub 存活 —— 关掉浏览器、
重启 agenthub, 会话照常跑。

实现上不用 send-keys + capture-pane 轮询, 而是起一个 pty 跑 `tmux attach`,
双向转发字节。这样方向键、Ctrl-C、批准提示、鼠标全都原样可用。
"""

from __future__ import annotations

import os
import select
import shlex
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

# tmux 后端需要 POSIX pty。Windows 上这三个模块不存在，但 term.py 仍然要能导入
# 本模块（它把两个后端放在同一张表里），所以缺失时只让 available() 报 False，
# 不能在导入期就炸掉整个服务。
try:
    import fcntl
    import pty
    import termios
except ImportError:                     # pragma: no cover - 只在 Windows 命中
    fcntl = pty = termios = None        # type: ignore[assignment]

from . import audit

PREFIX = "agenthub-"          # agenthub 起的会话用这个前缀, 便于识别
MANAGED_SERVER = "agenthub"   # 独立 socket，不继承用户默认 tmux server 的交互配置
LEGACY_SERVER = "default"   # 兼容改造前已经启动的 agenthub-* 会话
TMUX_CONF = Path(__file__).with_name("tmux.conf")
_config_lock = threading.Lock()
_managed_configured = False
_submit_locks: dict[str, threading.Lock] = {}
_submit_locks_guard = threading.Lock()

def available() -> bool:
    return pty is not None and shutil.which("tmux") is not None


def _tmux_argv(server: str, *args: str, no_start: bool = False) -> list[str]:
    argv = ["tmux"]
    if no_start:
        argv.append("-N")              # 查询/操作失败时不可偷起一个脱离 systemd 的 server
    argv += ["-L", server]
    # -f 只在 server 首次启动时生效；已存在时由 _configure_managed 补 source-file。
    if server == MANAGED_SERVER:
        argv += ["-f", str(TMUX_CONF)]
    return [*argv, *args]


def _tmux(*args: str, server: str = MANAGED_SERVER, timeout: int = 10,
          no_start: bool = False) -> str:
    r = subprocess.run(_tmux_argv(server, *args, no_start=no_start),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"tmux[{server}] {args[0]} 失败")
    return r.stdout


def _configure_managed() -> None:
    """服务重启但专用 tmux server 仍在时，重新加载透明配置一次。"""
    global _managed_configured
    if _managed_configured:
        return
    with _config_lock:
        if _managed_configured:
            return
        try:
            _tmux("source-file", str(TMUX_CONF), server=MANAGED_SERVER, no_start=True)
        except Exception:
            return                            # server 尚未创建；new-session 会通过 -f 加载
        _managed_configured = True


def _list_server(server: str) -> list[dict]:
    fmt = "#{session_name}\t#{session_created}\t#{session_attached}\t#{pane_pid}\t#{pane_current_path}\t#{pane_current_command}\t#{window_width}\t#{window_height}"
    try:
        out = _tmux("list-sessions", "-F", fmt, server=server, no_start=True)
    except Exception:
        return []                       # 没有任何会话时 tmux 也返回非 0
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 8:
            continue
        rows.append({
            "name": p[0], "created": int(p[1] or 0), "attached": p[2] == "1",
            "pid": int(p[3] or 0), "cwd": p[4], "cmd": p[5],
            "cols": int(p[6] or 80), "rows": int(p[7] or 24),
            "owned": p[0].startswith(PREFIX),
            "server": server,
        })
    return rows


def list_sessions() -> list[dict]:
    """列出专用 server，并兼容默认 server 中改造前留下的 agenthub 会话。"""
    if not available():
        return []
    managed = _list_server(MANAGED_SERVER)
    if managed:
        _configure_managed()
    names = {row["name"] for row in managed}
    legacy = [row for row in _list_server(LEGACY_SERVER)
              if row["owned"] and row["name"] not in names]
    return [*managed, *legacy]


def session_info(name: str) -> dict | None:
    """按名称定位所属 server；重名时专用 server 优先。"""
    return next((row for row in list_sessions() if row["name"] == name), None)


def managed_session(name: str) -> bool:
    row = session_info(name)
    return bool(row and row["server"] == MANAGED_SERVER)


def has_session(name: str) -> bool:
    return session_info(name) is not None


def _session_tmux(name: str, *args: str, timeout: int = 10) -> str:
    row = session_info(name)
    if not row:
        raise RuntimeError(f"tmux 会话不存在: {name}")
    return _tmux(*args, server=row["server"], timeout=timeout, no_start=True)


def new_session(name: str, cmd: str | list[str], cwd: str | None = None,
                cols: int = 120, rows: int = 32) -> str:
    """在 agenthub 专用 tmux server 中新建 detached 会话。"""
    global _managed_configured
    if not isinstance(cmd, str):
        cmd = shlex.join(cmd)
    full = name if name.startswith(PREFIX) else PREFIX + name
    if has_session(full):
        raise RuntimeError(f"tmux 会话已存在: {full}")
    # 若独立 server 已由 systemd 空载启动，-f 不会再次生效，需主动 source；
    # server 尚不存在时这里安静返回，紧接着的 new-session 会用 -f 首次启动。
    _configure_managed()
    args = ["new-session", "-d", "-s", full, "-x", str(cols), "-y", str(rows)]
    if cwd and os.path.isdir(cwd):
        args += ["-c", cwd]
    args += [cmd]
    _tmux(*args, server=MANAGED_SERVER)
    _managed_configured = True          # 新 server 已经由 -f 加载；旧 server 先前也 source 过
    audit.record(
        "tmux.session.created", category="terminal",
        data={"tmux": full, "cwd": cwd or "", "cols": cols, "rows": rows},
        content={"command": cmd},
    )
    return full


def kill_session(name: str) -> bool:
    """结束 tmux session；并发自然退出视为已经成功。

    list-sessions 与 kill-session 之间不可避免存在 TOCTOU 窗口。CLI 响应
    Ctrl-D 后会让单 pane session 自然消失，此时 tmux 的 "can't find session"
    不是停止失败。
    """
    row = session_info(name)
    if not row:
        return False
    try:
        _tmux("kill-session", "-t", name, server=row["server"], no_start=True)
        audit.record("tmux.session.killed", category="terminal",
                     data={"tmux": name, "server": row["server"]})
        return True
    except RuntimeError:
        if session_info(name) is None:
            return False
        raise


def rename_session(old: str, new: str) -> str:
    full = new if new.startswith(PREFIX) else PREFIX + new
    if full != old and has_session(full):
        raise RuntimeError(f"tmux 会话已存在: {full}")
    _session_tmux(old, "rename-session", "-t", old, full)
    audit.record("tmux.session.renamed", category="terminal",
                 data={"from": old, "to": full})
    return full


def send_text(name: str, text: str) -> None:
    """把一段文本当作键盘输入送进去 (不自动回车)。"""
    _session_tmux(name, "send-keys", "-t", name, "-l", "--", text)
    audit.record("tmux.text.sent", category="terminal",
                 data={"tmux": name, "chars": len(text)}, content=text)


def submit_text(name: str, text: str) -> None:
    """按一次终端标准粘贴提交整段文本，避免 CLI 把末尾 Enter 吞进粘贴批次。

    `send-keys -l` 会以机器速度逐字注入。Codex 等 TUI 有粘贴突发检测，紧随其后的
    Enter 偶尔会被识别成多行粘贴的一部分。tmux `paste-buffer -p` 会显式包上
    bracketed-paste 起止序列，让应用先得到一个完整 Paste 事件，再收到提交键。
    """
    with _submit_locks_guard:
        lock = _submit_locks.setdefault(name, threading.Lock())
    with lock:
        audit.record("tmux.submit.started", category="terminal",
                     data={"tmux": name, "chars": len(text)}, content=text)
        row = session_info(name)
        if not row:
            raise RuntimeError(f"tmux 会话不存在: {name}")
        buffer_name = f"agenthub-submit-{uuid.uuid4().hex}"
        _tmux("set-buffer", "-b", buffer_name, "--", text,
              server=row["server"], no_start=True)
        try:
            _tmux("paste-buffer", "-p", "-d", "-b", buffer_name, "-t", name,
                  server=row["server"], no_start=True)
        except Exception:
            try:
                _tmux("delete-buffer", "-b", buffer_name,
                      server=row["server"], no_start=True)
            except Exception:
                pass
            raise
        # 给全屏 TUI 一个事件循环间隔来消费 bracketed-paste 的结束序列；
        # 否则紧随其后的 Enter 偶尔会被并入粘贴，文字留到下一次提交。
        time.sleep(0.04)
        _tmux("send-keys", "-t", name, "--", "Enter",
              server=row["server"], no_start=True)
        audit.record("tmux.submit.completed", category="terminal",
                     data={"tmux": name, "chars": len(text)}, content=text)


def send_keys(name: str, *keys: str) -> None:
    """送 tmux 键名, 例如 Enter / Escape / C-c / Up。"""
    _session_tmux(name, "send-keys", "-t", name, "--", *keys)
    audit.record("tmux.keys.sent", category="terminal",
                 data={"tmux": name, "keys": list(keys)})


def in_copy_mode(name: str) -> bool:
    try:
        return _session_tmux(name, "display", "-p", "-t", name, "#{pane_in_mode}").strip() == "1"
    except Exception:
        return False


def leave_copy_mode(name: str) -> None:
    """回到实时画面。用户一开始打字就该退出, 否则按键会被 copy-mode 吃掉。"""
    if managed_session(name):
        return                              # 专用 server 从不让网页进入 copy-mode
    if in_copy_mode(name):
        try:
            _session_tmux(name, "send-keys", "-X", "-t", name, "cancel")
        except Exception:
            pass


def alt_screen(name: str) -> bool:
    """pane 里跑的是不是全屏应用 (claude/codex 的 TUI、less、vim…)。"""
    try:
        return _session_tmux(name, "display", "-p", "-t", name, "#{alternate_on}").strip() == "1"
    except Exception:
        return False


def scroll(name: str, up: bool, lines: int = 3) -> int:
    """兼容改造前留在默认 server 的网页滚动协议。

    专用 server 的外层 xterm 使用正常缓冲区，前端直接本地滚动，这里不做事。
    旧 server 仍需保留原来的两条路径：
      - 普通 shell 输出 → 翻 tmux 自己的历史 (copy-mode)。浏览器终端连的是
        tmux attach, 输出不进 xterm 的 scrollback, 只能这么翻。
      - 全屏应用 (alternate screen) → tmux 根本不给它存历史, scroll_position
        恒为 0。这时把滚轮转成方向键交给应用自己滚, 和 iTerm 之类的做法一致。
    """
    if managed_session(name):
        return 0                              # xterm 自己滚；不改变 pane 模式或发送按键
    if alt_screen(name):
        _session_tmux(name, "send-keys", "-t", name, "-N", str(min(lines, 10)), "Up" if up else "Down")
        return 0
    inm = in_copy_mode(name)
    if up:
        if not inm:
            _session_tmux(name, "copy-mode", "-t", name)
        _session_tmux(name, "send-keys", "-X", "-N", str(lines), "-t", name, "scroll-up")
    elif inm:
        _session_tmux(name, "send-keys", "-X", "-N", str(lines), "-t", name, "scroll-down")
    else:
        return 0
    pos = _session_tmux(name, "display", "-p", "-t", name, "#{scroll_position}").strip()
    at = int(pos) if pos.isdigit() else 0
    if not up and at == 0:
        leave_copy_mode(name)
    return at


def capture(name: str, lines: int = 200) -> str:
    return _session_tmux(name, "capture-pane", "-p", "-e", "-t", name, "-S", f"-{lines}")


def capture_history(name: str, lines: int = 10000) -> str:
    """Capture styled logical lines for replay into a differently sized xterm."""
    return _session_tmux(name, "capture-pane", "-J", "-p", "-e", "-t", name,
                         "-S", f"-{lines}")


def capture_plain(name: str, lines: int = 80) -> str:
    """Capture screen text without ANSI escapes for native prompt detection."""
    # -J joins terminal soft-wraps, so resizing does not alter a long command's
    # text or the stable prompt id presented to the browser.
    return _session_tmux(name, "capture-pane", "-J", "-p", "-t", name,
                         "-S", f"-{lines}")


def capture_screen_plain(name: str) -> str:
    """只取当前可见屏，不把已经滚出视口的旧分支混进时间线判断。"""
    return _session_tmux(name, "capture-pane", "-J", "-p", "-t", name)


def capture_screen(name: str) -> str:
    """取带样式的当前物理屏；编辑器检测需同时保留 ANSI 与软换行。"""
    return _session_tmux(name, "capture-pane", "-p", "-e", "-t", name)


def capture_screen_state(name: str) -> tuple[str, tuple[int, int]]:
    """Return the visible styled screen and its aligned tmux cursor."""
    return capture_screen(name), cursor_position(name)


def cursor_position(name: str) -> tuple[int, int]:
    """返回 pane 内的光标列、行，用来区分可见占位提示与真实草稿。"""
    value = _session_tmux(
        name, "display-message", "-p", "-t", name, "#{cursor_x}\t#{cursor_y}")
    left, right = value.strip().split("\t", 1)
    return int(left), int(right)


def set_window_size_policy(name: str, policy: str = "latest") -> bool:
    """Set the tmux window sizing policy without depending on the caller's tmux."""
    if policy not in {"latest", "largest", "smallest", "manual"}:
        raise ValueError(f"无效的 tmux window-size: {policy}")
    row = session_info(name)
    if not row:
        return False
    _tmux("set-window-option", "-t", name, "window-size", policy,
          server=row["server"], no_start=True)
    return True


def hosts(pids: list[int]) -> bool:
    """这些进程是不是跑在 tmux 里 (祖先有 tmux server)。"""
    for pid in pids:
        cur = abs(pid)
        for _ in range(12):
            try:
                with open(f"/proc/{cur}/stat") as fh:
                    st = fh.read()
                name = st[st.index("(") + 1:st.rindex(")")]
                ppid = int(st[st.rindex(")") + 2:].split()[1])
            except (OSError, ValueError):
                break
            if name.startswith("tmux"):
                return True
            if ppid <= 1:
                break
            cur = ppid
    return False


class Attach:
    """一条 pty 上的 `tmux attach`, 供 WebSocket 双向转发。"""

    MIN_COLS = 20
    MIN_ROWS = 8

    def __init__(self, name: str, cols: int = 120, rows: int = 32):
        row = session_info(name)
        if not row:
            raise RuntimeError(f"tmux 会话不存在: {name}")
        self.name = name
        self.server = row["server"]
        # A one-off ``resize-window`` repair leaves this option at ``manual``.
        # Every browser owner must restore normal client-driven sizing.
        set_window_size_policy(name, "latest")
        self._initial = b""
        if self.server == MANAGED_SERVER:
            # tmux attach 只会重绘当前屏；先把已有 history 喂给 xterm 的正常缓冲区，
            # 随后的清屏/重绘会留下真正可由浏览器滚动的历史，不必进入 copy-mode。
            # 先 -J 合并 tmux 按旧窗口宽度保存的软折行；否则手机/窄屏
            # xterm 会对已折过的物理行再折一次，出现半截单词和错位短行。
            try:
                history = capture_history(name, 10000)
                if history:
                    self._initial = (history.replace("\n", "\r\n")
                                     + "\x1b[0m\r\n").encode("utf-8", "replace")
            except Exception:
                pass
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                       # 子进程
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("TMUX", None)        # 否则 tmux 拒绝嵌套 attach
            try:
                os.execvp("tmux", _tmux_argv(
                    self.server, "attach-session", "-t", name, no_start=True))
            finally:
                os._exit(1)
        # display:none / 高度吸附到 0 时 FitAddon 会报告 xterm 的内部最小值
        # 10×6。它不是用户可见尺寸，若交给 tmux 会把 Codex 审批屏压碎。
        if cols < self.MIN_COLS or rows < self.MIN_ROWS:
            cols, rows = 120, 32
        self.cols, self.rows = cols, rows
        self.resize(cols, rows)

    def resize(self, cols: int, rows: int) -> bool:
        if cols < self.MIN_COLS or rows < self.MIN_ROWS:
            return False
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            return False
        self.cols, self.rows = cols, rows
        return True

    def read(self, timeout: float = 0.05) -> bytes:
        if self._initial:
            data, self._initial = self._initial[:65536], self._initial[65536:]
            return data
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not r:
            return b""
        try:
            return os.read(self.fd, 65536)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def alive(self) -> bool:
        try:
            return os.waitpid(self.pid, os.WNOHANG) == (0, 0)
        except ChildProcessError:
            return False

    def close(self) -> None:
        # 只结束 attach 客户端。不能依赖 prefix：专用 server 明确将它禁用了；
        # 往 PTY 写 C-b d 会变成发给 CLI 的真实输入。
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGHUP)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass


in_tmux = hosts
