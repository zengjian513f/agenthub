"""把浏览器接到 tmux 会话上 —— 真正的远程控制。

为什么绕 tmux 而不是直接注入已有进程:
  - 现有会话是 sshd → zsh → claude 直连 pts, 外部无法写入它的输入队列
  - 内核的 TIOCSTI 注入早已默认关闭 (dev.tty.legacy_tiocsti = 0)
tmux 提供了合法的输入通道, 而且会话独立于 sesman 存活 —— 关掉浏览器、
重启 sesman, 会话照常跑。

实现上不用 send-keys + capture-pane 轮询, 而是起一个 pty 跑 `tmux attach`,
双向转发字节。这样方向键、Ctrl-C、批准提示、鼠标全都原样可用。
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path

PREFIX = "sesman-"          # sesman 起的会话用这个前缀, 便于识别
MANAGED_SERVER = "sesman"   # 独立 socket，不继承用户默认 tmux server 的交互配置
LEGACY_SERVER = "default"   # 兼容改造前已经启动的 sesman-* 会话
TMUX_CONF = Path(__file__).with_name("tmux.conf")
_config_lock = threading.Lock()
_managed_configured = False
_submit_locks: dict[str, threading.Lock] = {}
_submit_locks_guard = threading.Lock()

# 三家续接已有会话的参数。可执行文件必须另外解析成绝对路径，因为 systemd
# 服务的 PATH 通常不含 ~/.local/bin 和 ~/.grok/bin。
RESUME = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "grok": ("--resume",),
}

SOURCES = ("claude", "codex", "grok")


def resume_command(source: str, sid: str) -> str:
    """拼续接命令。sid 只允许 UUID 字符, 否则就成了命令注入。"""
    if source not in RESUME:
        raise ValueError(f"不支持的来源: {source}")
    if not sid or not all(c in "0123456789abcdefABCDEF-" for c in sid):
        raise ValueError(f"会话 id 不合法: {sid!r}")
    exe = _which_cli(source)
    if not exe:
        raise ValueError(f"找不到 {source} 命令")
    return _clean_cli_command(exe, *RESUME[source], sid)


def session_name_for(source: str, sid: str) -> str:
    return f"{PREFIX}{source}-{sid[:8]}"


def available() -> bool:
    return shutil.which("tmux") is not None


def _which_cli(source: str) -> str | None:
    found = shutil.which(source)
    if found:
        return found
    home = Path.home()
    for p in (home / ".local" / "bin" / source, home / ".grok" / "bin" / source):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def available_sources() -> dict[str, bool]:
    return {source: _which_cli(source) is not None for source in SOURCES}


def _clean_cli_command(exe: str, *args: str) -> str:
    """用绝对 CLI 路径启动，并清掉可能由 tmux server 继承的旧会话身份。"""
    return shlex.join([
        "env", "-u", "CLAUDE_CODE_SESSION_ID", "-u", "CODEX_COMPANION_SESSION_ID",
        "-u", "GROK_SESSION_ID", exe, *args,
    ])


def new_cli_session(source: str, cwd: str, cols: int = 120, rows: int = 32) -> dict:
    """在经过校验的目录中新建一条 CLI 会话。

    命令只能来自固定白名单，浏览器不能借这个接口拼任意 shell。Claude/Grok
    支持预先指定 UUID；Codex 启动后再由服务端根据新落盘的会话完成关联。
    """
    if source not in SOURCES:
        raise ValueError(f"不支持的会话类型: {source}")
    exe = _which_cli(source)
    if not exe:
        raise ValueError(f"找不到 {source} 命令")
    raw = str(cwd or "").strip()
    if not raw:
        raise ValueError("请选择启动目录")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("启动目录必须是绝对路径")
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise ValueError("启动目录不存在") from None
    if not path.is_dir():
        raise ValueError("启动路径不是目录")

    sid = str(uuid.uuid4()) if source in ("claude", "grok") else None
    args = [exe]
    if sid:
        args += ["--session-id", sid]
    command = _clean_cli_command(args[0], *args[1:])
    token = sid or str(uuid.uuid4())
    suffix = sid[:8] if sid else f"new-{token[:8]}"
    name = new_session(f"{source}-{suffix}", command, str(path), cols, rows)
    return {"name": name, "source": source, "sid": sid, "cwd": str(path), "token": token}


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
    """列出专用 server，并兼容默认 server 中改造前留下的 sesman 会话。"""
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


def new_session(name: str, cmd: str, cwd: str | None = None,
                cols: int = 120, rows: int = 32) -> str:
    """在 sesman 专用 tmux server 中新建 detached 会话。"""
    global _managed_configured
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
    return full


def process_belongs_to(pid: int, root_pid: int) -> bool:
    """pid 是否等于或派生自指定 tmux pane 的根进程。"""
    cur = abs(int(pid))
    root = abs(int(root_pid))
    for _ in range(16):
        if cur == root:
            return True
        try:
            st = open(f"/proc/{cur}/stat").read()
            cur = int(st[st.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if cur <= 1:
            return cur == root
    return False


def send_text(name: str, text: str) -> None:
    """把一段文本当作键盘输入送进去 (不自动回车)。"""
    _session_tmux(name, "send-keys", "-t", name, "-l", "--", text)


def submit_text(name: str, text: str) -> None:
    """按一次终端标准粘贴提交整段文本，避免 CLI 把末尾 Enter 吞进粘贴批次。

    `send-keys -l` 会以机器速度逐字注入。Codex 等 TUI 有粘贴突发检测，紧随其后的
    Enter 偶尔会被识别成多行粘贴的一部分。tmux `paste-buffer -p` 会显式包上
    bracketed-paste 起止序列，让应用先得到一个完整 Paste 事件，再收到提交键。
    """
    with _submit_locks_guard:
        lock = _submit_locks.setdefault(name, threading.Lock())
    with lock:
        row = session_info(name)
        if not row:
            raise RuntimeError(f"tmux 会话不存在: {name}")
        buffer_name = f"sesman-submit-{uuid.uuid4().hex}"
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


def send_keys(name: str, *keys: str) -> None:
    """送 tmux 键名, 例如 Enter / Escape / C-c / Up。"""
    _session_tmux(name, "send-keys", "-t", name, "--", *keys)


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


def in_tmux(pids: list[int]) -> bool:
    """这些进程是不是跑在 tmux 里 (祖先有 tmux server)。"""
    for pid in pids:
        cur = abs(pid)
        for _ in range(12):
            try:
                st = open(f"/proc/{cur}/stat").read()
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


def gone(pid: int) -> bool:
    """进程是否已经结束。僵尸也算结束 —— 它的 /proc 条目要等父进程收尸才消失。"""
    try:
        st = open(f"/proc/{pid}/stat").read()
        return st[st.rindex(")") + 2] == "Z"
    except (OSError, ValueError, IndexError):
        return True


def kill_pids(pids: list[int], timeout: float = 6.0) -> list[int]:
    """先 TERM 让 CLI 有机会存盘, 不退再 KILL。返回真正被结束的 pid。"""
    import time as _t
    targets = [p for p in pids if p > 0]        # 只杀 CLI 主进程, 不动它的子 shell
    killed = []
    for p in targets:
        try:
            os.kill(p, signal.SIGTERM)
            killed.append(p)
        except OSError:
            pass
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if all(gone(p) for p in killed):
            return killed
        _t.sleep(0.15)
    for p in killed:                            # 赖着不走就硬杀
        if not gone(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    _t.sleep(0.3)
    return killed


def graceful_stop(name: str, pids: list[int], timeout: float = 2.4) -> list[int]:
    """先退出 pane 内最深的 CLI，让单 pane tmux session 自然随前台命令结束。

    Claude/Codex 的空输入提示通常用 Ctrl-D 退出，部分状态需要按第二次。只有两次
    EOF 都无效时才向 CLI 主进程发 TERM/KILL；tmux 残壳最后才作为兜底清理。
    """
    targets = [p for p in pids if p > 0]

    def settled() -> bool:
        return not has_session(name) and all(gone(p) for p in targets)

    each = max(0.0, timeout) / 2
    for _ in range(2):
        if settled():
            return targets
        try:
            send_keys(name, "C-d")
        except RuntimeError:
            if settled():
                return targets
        deadline = time.monotonic() + each
        while time.monotonic() < deadline:
            if settled():
                return targets
            time.sleep(0.1)

    killed = kill_pids(targets)
    if has_session(name):
        kill_session(name)
    return killed


class Attach:
    """一条 pty 上的 `tmux attach`, 供 WebSocket 双向转发。"""

    def __init__(self, name: str, cols: int = 120, rows: int = 32):
        row = session_info(name)
        if not row:
            raise RuntimeError(f"tmux 会话不存在: {name}")
        self.name = name
        self.server = row["server"]
        self._initial = b""
        if self.server == MANAGED_SERVER:
            # tmux attach 只会重绘当前屏；先把已有 history 喂给 xterm 的正常缓冲区，
            # 随后的清屏/重绘会留下真正可由浏览器滚动的历史，不必进入 copy-mode。
            try:
                history = capture(name, 10000)
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
        self.resize(cols, rows)

    def resize(self, cols: int, rows: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

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
