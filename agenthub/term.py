"""终端后端调度: 共享的 CLI 命令拼装、目录校验、进程结束, 以及 tmux / 宿主两个后端。

后端选择 (决定新会话建在哪里, 按优先级):
  - 网页设置里选过的值, 持久化在本机数据目录, 重启后仍然生效。
  - ``AGENTHUB_TERM_BACKEND`` 或 server ``--terminal-backend`` 给出的初始默认值。
  - 默认 ptyhost。配置的后端在这台机器上不可用 (例如还没拷 ptyhost 二进制)
    时退到另一个可用的, 控制台不会因此整个消失。

按名称操作的接口会在两个后端里查找会话, 因此切换主后端只影响新建, 旧后端里
仍在跑的会话继续可用, 直到自然结束。
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import claude_bridge, term_host, term_tmux
from .host import procs

PREFIX = "agenthub-"
MANAGED_SERVER = term_tmux.MANAGED_SERVER
WINDOWS = sys.platform == "win32"

# 三家续接已有会话的参数。可执行文件必须另外解析成绝对路径，因为 systemd
# 服务的 PATH 通常不含 ~/.local/bin 和 ~/.grok/bin。
RESUME = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "grok": ("--resume",),
}

SOURCES = ("claude", "codex", "grok")
DIRECTORY_COMPLETION_LIMIT = 24
CODEX_QUESTION_ARGS = (
    "--enable", "default_mode_request_user_input",
    "-c", "suppress_unstable_features_warning=true",
)

BACKENDS = {"tmux": term_tmux, "ptyhost": term_host}
BACKEND_LABELS = {"tmux": "tmux", "ptyhost": "ptyhost"}
# 早期版本把宿主后端叫 host；已经落盘的选择和旧启动参数要继续认。
BACKEND_ALIASES = {"host": "ptyhost"}
BACKEND_FILE = Path.home() / ".local" / "share" / "agenthub" / "terminal-backend"
_default: str | None = None        # 启动参数给的初始默认值
_chosen: str | None = None         # 网页里选过的值, 覆盖默认
_loaded = False                    # 是否已经尝试读过持久化的选择


class DirectoryCreationRequired(ValueError):
    """A validated absolute session directory is missing and needs consent."""

    def __init__(self, path: Path):
        self.path = str(path)
        super().__init__("启动目录不存在")


# ----------------------------------------------------------------- 后端选择
def _normalize(name: str) -> str:
    value = str(name or "").strip().lower()
    return BACKEND_ALIASES.get(value, value)


def default_backend() -> str:
    raw = _normalize(os.environ.get("AGENTHUB_TERM_BACKEND") or "")
    if raw in BACKENDS:
        return raw
    return "ptyhost"


def _read_choice() -> str | None:
    try:
        value = _normalize(BACKEND_FILE.read_text(encoding="utf-8"))
    except OSError:
        return None
    return value if value in BACKENDS else None


def configure(backend: str | None) -> str:
    """server 启动时设定初始默认后端; 网页选过的值优先。返回实际生效的名称。"""
    global _default, _chosen, _loaded
    name = _normalize(backend or "")
    if name in ("", "auto"):
        name = default_backend()
    if name not in BACKENDS:
        raise ValueError(f"未知终端后端: {backend}")
    _default = name
    _chosen = _read_choice()
    _loaded = True
    return backend_name()


def configured_backend() -> str:
    """持久化的选择是这台机器的唯一事实来源。

    没调过 configure() 的进程（脚本、工具）也必须看到同一个答案，否则会悄悄
    按默认值建会话, 落到用户没选的那个后端里。只读一次, 之后由 API 更新。
    """
    global _chosen, _loaded
    if _chosen is None and not _loaded:
        _chosen = _read_choice()
        _loaded = True
    return _chosen or _default or default_backend()


def backend_name() -> str:
    """实际生效的后端: 配置的那个可用就用它, 否则退到任何一个可用的。

    默认是 ptyhost, 但一台刚部署、还没拷二进制的节点上它不可用; 若因此把整个
    终端功能关掉, 连已有的 tmux 会话都会从列表里消失。退让只发生在"配置的
    不可用"这一种情况, 网页里能选中的永远是可用的。
    """
    wanted = configured_backend()
    if BACKENDS[wanted].available():
        return wanted
    for name, module in BACKENDS.items():
        if module.available():
            return name
    return wanted


def set_backend(name: str) -> str:
    """网页选择主后端。只影响新建会话, 已在跑的会话不受影响。"""
    global _chosen, _loaded
    value = _normalize(name)
    if value not in BACKENDS:
        raise ValueError(f"未知终端后端: {name}")
    module = BACKENDS[value]
    if not module.available():
        raise ValueError(backend_unavailable_reason(value)
                         or f"{BACKEND_LABELS[value]} 当前不可用")
    if value != configured_backend():
        try:
            BACKEND_FILE.parent.mkdir(parents=True, exist_ok=True)
            BACKEND_FILE.write_text(value + "\n", encoding="utf-8")
        except OSError as e:
            raise ValueError(f"无法保存终端后端选择：{getattr(e, 'strerror', None) or e}") from None
    _chosen = value
    _loaded = True
    return value


def backend_unavailable_reason(name: str) -> str:
    if _normalize(name) == "tmux":
        return "" if term_tmux.available() else "服务器未安装 tmux。"
    return term_host.unavailable_reason()


def backends() -> list[dict]:
    """供网页渲染的后端清单, 含当前选中项与不可用原因。"""
    current = backend_name()
    rows = []
    for name, module in BACKENDS.items():
        ok = module.available()
        rows.append({"name": name, "label": BACKEND_LABELS[name], "available": ok,
                     "current": name == current,
                     "unavailable_reason": "" if ok else backend_unavailable_reason(name)})
    return rows


def primary():
    return BACKENDS[backend_name()]


def _backends() -> list:
    first = primary()
    rest = [m for m in BACKENDS.values() if m is not first and m.available()]
    return [first, *rest]


def available() -> bool:
    return primary().available()


def unavailable_reason() -> str:
    name = backend_name()
    return (backend_unavailable_reason(name)
            or f"当前终端后端（{BACKEND_LABELS.get(name, name)}）不可用。")


def _owner(name: str):
    for module in _backends():
        try:
            if module.has_session(name):
                return module
        except Exception:
            continue
    return None


def _require(name: str):
    module = _owner(name)
    if module is None:
        raise RuntimeError(f"tmux 会话不存在: {name}")
    return module


# ----------------------------------------------------------------- 命令拼装
def resume_command(source: str, sid: str) -> str | list[str]:
    """拼续接命令。sid 只允许 UUID 字符, 否则就成了命令注入。"""
    if source not in RESUME:
        raise ValueError(f"不支持的来源: {source}")
    if not sid or not all(c in "0123456789abcdefABCDEF-" for c in sid):
        raise ValueError(f"会话 id 不合法: {sid!r}")
    exe = _which_cli(source)
    if not exe:
        raise ValueError(f"找不到 {source} 命令")
    args = [*RESUME[source], sid]
    if source == "claude":
        args = ["--settings", claude_bridge.settings_path(), *args]
    elif source == "codex":
        args = [*CODEX_QUESTION_ARGS, *args]
    return _clean_cli_command(exe, *args)


def session_name_for(source: str, sid: str) -> str:
    return f"{PREFIX}{source}-{sid[:8]}"


def _which_cli(source: str) -> str | None:
    found = shutil.which(source)
    if found:
        return found
    home = Path.home()
    candidates = [home / ".local" / "bin" / source, home / ".grok" / "bin" / source]
    if WINDOWS:
        candidates += [home / ".local" / "bin" / f"{source}.exe",
                       home / ".local" / "bin" / f"{source}.cmd"]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def available_sources() -> dict[str, bool]:
    return {source: _which_cli(source) is not None for source in SOURCES}


def complete_directories(raw: str, limit: int = DIRECTORY_COMPLETION_LIMIT) -> list[str]:
    """Return shell-style directory completions without resolving the shown path.

    The caller may use an absolute path or ``~/``.  Only the last path component
    is matched, hidden entries stay hidden until ``.`` is typed, and every result
    ends in ``/`` so accepting it can immediately continue into the next level.
    Symlink spelling is deliberately preserved in the browser even though the
    final session creation still resolves and validates the selected directory.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    if len(text) > 4096:
        raise ValueError("启动目录路径过长")
    limit = max(1, min(int(limit), 50))

    if text == "~":
        return ["~/"] if Path.home().is_dir() else []
    if text.startswith("~") and not text.startswith("~/"):
        return []

    expanded = Path.home() / text[2:] if text.startswith("~/") else Path(text)
    if not expanded.is_absolute():
        return []
    if text.endswith("/"):
        parent, prefix = expanded, ""
        display_parent = text
    else:
        parent, prefix = expanded.parent, expanded.name
        display_parent = text[:text.rfind("/") + 1]

    rows: list[str] = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name
                if (not prefix.startswith(".") and name.startswith(".")) \
                        or not name.startswith(prefix):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                rows.append(f"{display_parent}{name}/")
    except (OSError, ValueError):
        return []
    rows.sort(key=lambda value: (value.casefold(), value))
    return rows[:limit]


def _clean_cli_command(exe: str, *args: str) -> str | list[str]:
    """用绝对 CLI 路径启动，并清掉可能由 tmux server 继承的旧会话身份。

    Windows 没有 env -u; 宿主进程自己会剔除这些变量, 直接返回 argv。
    """
    if WINDOWS:
        return [exe, *args]
    return shlex.join([
        "env", "-u", "CLAUDE_CODE_SESSION_ID", "-u", "CODEX_COMPANION_SESSION_ID",
        "-u", "GROK_SESSION_ID", exe, *args,
    ])


def new_cli_session(source: str, cwd: str, cols: int = 120, rows: int = 32,
                    create_cwd: bool = False) -> dict:
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
    try:
        path = Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("启动目录路径无效") from None
    if not path.is_absolute():
        raise ValueError("启动目录必须是绝对路径")
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError:
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("启动目录路径无效") from None
        if not create_cwd:
            raise DirectoryCreationRequired(path) from None
        try:
            path.mkdir(parents=True, exist_ok=True)
            path = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as e:
            detail = getattr(e, "strerror", None) or str(e)
            raise ValueError(f"创建启动目录失败：{detail}") from None
    except (OSError, RuntimeError, ValueError) as e:
        detail = getattr(e, "strerror", None) or str(e)
        raise ValueError(f"无法访问启动目录：{detail}") from None
    if not path.is_dir():
        raise ValueError("启动路径不是目录")

    sid = str(uuid.uuid4()) if source in ("claude", "grok") else None
    args = [exe]
    if sid:
        args += ["--session-id", sid]
    if source == "claude":
        args[1:1] = ["--settings", claude_bridge.settings_path()]
    elif source == "codex":
        args[1:1] = CODEX_QUESTION_ARGS
    command = _clean_cli_command(args[0], *args[1:])
    token = sid or str(uuid.uuid4())
    suffix = sid[:8] if sid else f"new-{token[:8]}"
    name = new_session(f"{source}-{suffix}", command, str(path), cols, rows)
    return {"name": name, "source": source, "sid": sid, "cwd": str(path), "token": token}


# ----------------------------------------------------------------- 会话调度
def list_sessions() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for module in _backends():
        try:
            found = module.list_sessions()
        except Exception:
            found = []
        for row in found:
            if row["name"] in seen:
                continue
            seen.add(row["name"])
            rows.append(row)
    return rows


def session_info(name: str) -> dict | None:
    return next((row for row in list_sessions() if row["name"] == name), None)


def has_session(name: str) -> bool:
    return _owner(name) is not None


def new_session(name: str, cmd: str | list[str], cwd: str | None = None,
                cols: int = 120, rows: int = 32) -> str:
    full = name if name.startswith(PREFIX) else PREFIX + name
    if has_session(full):
        raise RuntimeError(f"tmux 会话已存在: {full}")
    return primary().new_session(full, cmd, cwd, cols, rows)


def kill_session(name: str) -> bool:
    module = _owner(name)
    return module.kill_session(name) if module else False


def rename_session(old: str, new: str) -> str:
    full = new if new.startswith(PREFIX) else PREFIX + new
    if full != old and has_session(full):
        raise RuntimeError(f"tmux 会话已存在: {full}")
    return _require(old).rename_session(old, full)


def send_text(name: str, text: str) -> None:
    _require(name).send_text(name, text)


def submit_text(name: str, text: str) -> None:
    _require(name).submit_text(name, text)


def send_keys(name: str, *keys: str) -> None:
    _require(name).send_keys(name, *keys)


def leave_copy_mode(name: str) -> None:
    module = _owner(name)
    if module:
        module.leave_copy_mode(name)


def scroll(name: str, up: bool, lines: int = 3) -> int:
    return _require(name).scroll(name, up, lines)


def capture(name: str, lines: int = 200) -> str:
    return _require(name).capture(name, lines)


def capture_history(name: str, lines: int = 10000) -> str:
    return _require(name).capture_history(name, lines)


def capture_plain(name: str, lines: int = 80) -> str:
    return _require(name).capture_plain(name, lines)


def capture_screen_plain(name: str) -> str:
    return _require(name).capture_screen_plain(name)


def capture_screen(name: str) -> str:
    return _require(name).capture_screen(name)


def capture_screen_state(name: str) -> tuple[str, tuple[int, int]]:
    return _require(name).capture_screen_state(name)


def cursor_position(name: str) -> tuple[int, int]:
    return _require(name).cursor_position(name)


def Attach(name: str, cols: int = 120, rows: int = 32):   # noqa: N802 - 保持类名式调用
    return _require(name).Attach(name, cols, rows)


# ----------------------------------------------------------------- 进程
def process_belongs_to(pid: int, root_pid: int) -> bool:
    """pid 是否等于或派生自指定会话的根进程。"""
    return procs.descendant_of(pid, root_pid)


def in_tmux(pids: list[int]) -> bool:
    """这些进程是不是跑在某个受管会话里 (tmux 或宿主)。"""
    return any(module.hosts(pids) for module in _backends())


def gone(pid: int) -> bool:
    return procs.gone(pid)


def kill_pids(pids: list[int], timeout: float = 6.0) -> list[int]:
    return procs.kill_pids(pids, timeout)


def graceful_stop(name: str, pids: list[int], timeout: float = 2.4) -> list[int]:
    """先退出会话里最深的 CLI，让会话自然随前台命令结束。

    Claude/Codex 的空输入提示通常用 Ctrl-D 退出，部分状态需要按第二次。只有两次
    EOF 都无效时才向 CLI 主进程发 TERM/KILL；会话残壳最后才作为兜底清理。
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
