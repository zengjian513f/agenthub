"""检测哪些会话还活着 (对应的 CLI 进程仍在运行)。

三家留下的痕迹各不相同, 所以三种信号都收:
  - Codex  常驻持有会话文件的 fd     → /proc/<pid>/fd 直接给出文件路径
  - Claude 进程参数带 session id     → --session-id / --resume, 子进程还有 CLAUDE_CODE_SESSION_ID
  - Grok   自己维护活跃会话清单       → ~/.grok/active_sessions.json

全部只读 /proc 与状态文件, 不触碰任何 CLI 进程。

这套判断依赖 Linux 的 /proc。没有 /proc 的系统（Windows）上退化为"查不出运行
状态": 会话照常列出和打开控制台, 只是不显示活跃标记。接管会因此把一条其实在
跑的会话当成没在跑, 直接另起一个实例 —— 详见 docs/session-host.md。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

GROK_ACTIVE = Path.home() / ".grok" / "active_sessions.json"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CMD_SID = re.compile(rf"--session-id[= ]({_UUID})|--resume[= ]({_UUID})")
_ENV_SID = ("CLAUDE_CODE_SESSION_ID=", "CODEX_COMPANION_SESSION_ID=", "GROK_SESSION_ID=")
_KEYWORDS = ("claude", "codex", "grok")

PROC_FS = Path("/proc")
HAS_PROC = PROC_FS.is_dir()
TTL = 3.0          # 扫描结果的缓存秒数, 前端可以放心高频轮询
_cache = {"at": 0.0, "sids": set(), "paths": set(), "bare_claude": {}}
_scan_lock = threading.Lock()
_boot_time: float | None = None
# 只在解析 /proc/<pid>/stat 的启动时间时用到；Windows 没有 sysconf，也没有 /proc。
_clock_ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


_CLI_NAMES = ("claude", "codex", "grok")


def _is_cli(cmd: str) -> bool:
    """是不是 CLI 主进程本身 (而不是它拉起来的 shell 之类)。接管时只杀这些。"""
    head = cmd.strip().split(" ", 1)[0].rsplit("/", 1)[-1]
    return head in _CLI_NAMES or head.startswith(("codex-", "claude-"))


def _process_started_at(pid: int) -> float | None:
    """从 /proc 读取进程启动时间（Unix 秒），避免把旧回合状态带入新进程。"""
    global _boot_time
    try:
        if _boot_time is None:
            with open(PROC_FS / "stat") as fh:
                _boot_time = float(next(
                    line.split()[1] for line in fh if line.startswith("btime ")))
        st = open(f"/proc/{pid}/stat").read()
        # 去掉可能含空格的 comm；余下从字段 3(state) 开始，starttime 是索引 19。
        start_ticks = int(st[st.rindex(")") + 2:].split()[19])
        return _boot_time + start_ticks / _clock_ticks
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def _cli_ancestor(pid: int) -> int | None:
    """从一个子进程往上找它所属的 CLI 主进程。

    裸 `claude` 启动的会话, 命令行里没有 session id, 只有子 shell 的环境变量能认出来。
    要接管这种会话就必须顺着进程树找到真正的 CLI 进程, 否则杀不掉旧实例。
    """
    cur = pid
    for _ in range(12):
        try:
            st = open(f"/proc/{cur}/stat").read()
            name = st[st.index("(") + 1:st.rindex(")")]
            ppid = int(st[st.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            return None
        if name in _CLI_NAMES or name.startswith(("codex-", "claude-")):
            return cur
        if ppid <= 1:
            return None
        cur = ppid
    return None


def _scan() -> tuple[dict[str, set[int]], dict[str, set[int]], dict[int, tuple[str, float]]]:
    """返回 (会话id → pid集合, 会话文件路径 → pid集合)。"""
    sids: dict[str, set[int]] = {}
    paths: dict[str, set[int]] = {}
    bare_claude: dict[int, tuple[str, float]] = {}
    if not HAS_PROC:
        # 没有 /proc 就查不出运行状态。返回空集而不是抛异常：列表、控制台、
        # 搜索都不依赖它，只是活跃标记不再显示。
        return sids, paths, bare_claude

    def note(d, k, pid):
        d.setdefault(k, set()).add(pid)

    for spid in os.listdir(PROC_FS):
        if not spid.isdigit():
            continue
        pid = int(spid)
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if not any(k in cmd.lower() for k in _KEYWORDS):
            continue
        main = _is_cli(cmd)

        # resume 命令里的 ID 是这个 CLI 进程的权威身份。agenthub 若本身从另一个
        # Claude 会话启动，tmux 子进程会继承旧的 CLAUDE_CODE_SESSION_ID；不能
        # 因此把新旧两个会话都标成活跃。
        cmd_sids = {(m.group(1) or m.group(2)).lower() for m in _CMD_SID.finditer(cmd)}
        for sid in cmd_sids:
            note(sids, sid, pid)

        # 控制台直接运行 `claude` 时，主进程既没有参数里的 session id，也不
        # 常驻打开 jsonl；空闲时甚至没有带 CLAUDE_CODE_SESSION_ID 的工具子进程。
        # 保留 cwd 与启动时间，稍后只和紧邻创建的 Claude 会话做严格配对。
        head = cmd.strip().split(" ", 1)[0].rsplit("/", 1)[-1]
        if main and head == "claude" and not cmd_sids:
            try:
                cwd = str(Path(os.readlink(f"/proc/{pid}/cwd")).resolve())
                started = _process_started_at(pid)
                if started is not None:
                    bare_claude[pid] = (cwd, started)
            except (OSError, RuntimeError):
                pass

        try:
            for e in open(f"/proc/{pid}/environ", "rb").read().decode("utf8", "replace").split("\0"):
                if e.startswith(_ENV_SID):
                    env_sid = e.split("=", 1)[1].strip().lower()
                    if main and cmd_sids and env_sid not in cmd_sids:
                        continue
                    owner = pid if main else (_cli_ancestor(pid) or -pid)
                    note(sids, env_sid, owner)
        except OSError:
            pass

        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                t = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if t.endswith(".jsonl") and (
                    "/.codex/sessions/" in t or "/.claude/projects/" in t or "/.grok/" in t):
                note(paths, t, pid if main else -pid)

    try:                                    # Grok 自己就记着活跃会话
        data = json.loads(GROK_ACTIVE.read_text())
        entries = data if isinstance(data, list) else data.get("sessions", [])
        for e in entries:
            sid = e.get("id") or e.get("session_id") if isinstance(e, dict) else e
            if sid:
                sids.setdefault(str(sid).lower(), set())
    except Exception:
        pass

    return sids, paths, bare_claude


def snapshot(force: bool = False):
    now = time.monotonic()
    cached_at = _cache["at"]
    if not force and cached_at > 0 and now - cached_at <= TTL:
        return _cache["sids"], _cache["paths"]

    # ThreadingHTTPServer 会让多个浏览器轮询同时落到这里。扫描 /proc 较慢时，
    # 不能让每个普通请求都各扫一遍；等待者复用刚完成的结果。force 请求仍
    # 严格绕过缓存，用于接管、停止等必须立即确认进程状态的操作。
    with _scan_lock:
        now = time.monotonic()
        cached_at = _cache["at"]
        fresh = cached_at > 0 and now - cached_at <= TTL
        if not force and fresh:
            return _cache["sids"], _cache["paths"]

        sids, paths, bare_claude = _scan()
        # TTL 从扫描完成开始算。若扫描本身超过 TTL，用开始时间会令新结果刚写入
        # 就已过期，下一批轮询立刻再次扫描，形成持续高 CPU。
        _cache.update(at=time.monotonic(), sids=sids, paths=paths,
                      bare_claude=bare_claude)
    return _cache["sids"], _cache["paths"]


def _bare_claude_pids(session: dict) -> set[int]:
    """以 cwd + 启动时间识别没有显式 session id 的新建 Claude。

    只接受会话在进程启动前 5 秒到启动后 30 秒内创建，既覆盖文件落盘抖动，
    又不会把同目录的历史会话或稍后另开的会话误标为当前进程。
    """
    if session.get("source") != "claude" or not session.get("cwd"):
        return set()
    try:
        cwd = str(Path(session["cwd"]).expanduser().resolve())
        created = datetime.fromisoformat(str(session.get("created", "")).replace("Z", "+00:00")).timestamp()
    except (OSError, RuntimeError, TypeError, ValueError):
        return set()
    return {pid for pid, (proc_cwd, started) in _cache.get("bare_claude", {}).items()
            if proc_cwd == cwd and -5 <= created - started <= 30}


def pids_of(session: dict, force: bool = False) -> list[int]:
    """该会话对应的进程。正数是 CLI 主进程 (接管时要杀的), 负数是相关子进程。"""
    sids, paths = snapshot(force)
    found: set[int] = set()
    sid = str(session.get("sid", "")).lower()
    if sid:
        found |= sids.get(sid, set())
    for p in (session["path"], f'{session["path"]}/chat_history.jsonl'):
        found |= paths.get(p, set())
    found |= _bare_claude_pids(session)
    return sorted(found)


def is_live(session: dict, force: bool = False) -> bool:
    sids, paths = snapshot(force)
    sid = str(session.get("sid", "")).lower()
    return bool((sid and sid in sids) or session["path"] in paths
                or f'{session["path"]}/chat_history.jsonl' in paths
                or _bare_claude_pids(session))


def started_at(session: dict, force: bool = False,
               pids: list[int] | None = None) -> float | None:
    """当前会话最早的 CLI 主进程启动时间；没有可确认主进程时返回 None。"""
    starts = []
    for pid in pids_of(session, force=force) if pids is None else pids:
        if pid <= 0:
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read() \
                .replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if not _is_cli(cmd):
            continue
        value = _process_started_at(pid)
        if value is not None:
            starts.append(value)
    return min(starts) if starts else None


def _codex_ancestor_sids(session: dict, by_sid: dict[str, dict]) -> set[str]:
    """返回一个 Codex 回滚分支在当前列表中可确认的祖先。"""
    if session.get("source") != "codex":
        return set()
    ancestors: set[str] = set()
    parent = str(session.get("forked_from_id") or "")
    while parent and parent not in ancestors:
        ancestors.add(parent)
        row = by_sid.get(parent)
        if not row:
            break
        parent = str(row.get("forked_from_id") or "")
    return ancestors


def active_processes(sessions: list[dict], force: bool = False,
                     ) -> tuple[list[str], dict[str, list[int]]]:
    """把每个运行进程只归给当前 Codex 回滚叶子。

    Codex 双 Esc 会在同一个 CLI 进程和 tmux pane 内换一个 rollout 文件。进程会
    继续持有祖先 JSONL，因此单纯按 fd 判活会把父、子多行都标成同一个运行实例。
    这里逐 pid 消掉同一分叉链上的祖先归属；若祖先另有独立 pid，它仍保持活跃。
    """
    if force:
        snapshot(True)
    raw = {str(s["uid"]): set(pids_of(s)) for s in sessions}
    rows = {str(s["uid"]): s for s in sessions}
    by_sid = {str(s.get("sid") or ""): s for s in sessions
              if s.get("source") == "codex" and s.get("sid")}
    ancestors = {uid: _codex_ancestor_sids(row, by_sid)
                 for uid, row in rows.items()}
    candidates: dict[int, set[str]] = {}
    for uid, pids in raw.items():
        for pid in pids:
            candidates.setdefault(pid, set()).add(uid)

    owned = {uid: set() for uid in rows}
    for pid, uids in candidates.items():
        # A descendant holding the same process supersedes only its ancestors.
        # Unrelated sessions sharing a helper signal are left untouched.
        keep = {uid for uid in uids if not any(
            uid != other
            and str(rows[uid].get("sid") or "") in ancestors.get(other, set())
            for other in uids
        )}
        for uid in keep:
            owned[uid].add(pid)

    active = []
    for session in sessions:
        uid = str(session["uid"])
        # Grok's native active list can be authoritative without exposing a pid.
        if owned[uid] or (not raw[uid] and is_live(session)):
            active.append(uid)
    return active, {uid: sorted(pids) for uid, pids in owned.items()}


def live_uids(sessions: list[dict], force: bool = False) -> list[str]:
    """在已知会话里挑出还活着的，并折叠共享进程的 Codex 回滚祖先。"""
    return active_processes(sessions, force=force)[0]
