"""会话索引: 枚举 + 磁盘缓存 + 全文搜索。"""

from __future__ import annotations

import _sre
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .adapters import ADAPTERS, ClaudeAdapter
from . import audit, media, session_meta, trash

CACHE_DIR = Path.home() / ".cache" / "agenthub"
CACHE_FILE = CACHE_DIR / "index.json"
CACHE_VERSION = 10  # hide superseded Claude continued-in parents in the list
WINDOW_CACHE_DIR = CACHE_DIR / "message-windows"
WINDOW_CACHE_VERSION = 11  # windows now hold media-enriched messages + tokens
MESSAGE_CURSOR_VERSION = 10
WINDOW_CACHE_MIN_BYTES = 8 * 1024 * 1024
WINDOW_CACHE_MEMORY_ITEMS = 16
VIEW_CACHE_MAX_ITEMS = 32
# 按已序列化 JSON 正文 (+ 已压缩流) 计量；实测消息对象本身约再占 1.8 倍
# 正文大小 (56 MB 正文 ≈ 100 MB 对象)，即上限对应约 3 倍的常驻内存。
VIEW_CACHE_MAX_BYTES = 192 * 1024 * 1024
TRASH_DIR = Path.home() / ".local" / "share" / "agenthub" / "trash"
CHECK_TTL = 0.5       # 高频热路径复用已发布快照；列表轮询仍会及时发现磁盘变化
# 浏览器列表轮询接受的最大快照年龄。server 的预热线程按更短的节奏主动扫
# inventory，轮询请求自己几乎不再扫盘；预热线程不在时（测试、脚本）轮询
# 至多晚 POLL_TTL 秒看到磁盘变化，仍远小于 8 秒的轮询间隔。
POLL_TTL = 2.0
# 活跃会话每秒都在追加，快照随之每秒换新；磁盘缓存只服务冷启动，不必每次
# 都把 2 MB JSON 重写一遍。两次落盘之间至少隔这么久，期间的版本先记着，
# 下一次 load()（哪怕 inventory 没变）满足间隔就补写。
CACHE_WRITE_INTERVAL = 10.0

_lock = threading.Lock()


def _empty_state() -> dict:
    return {
        "initialized": False,
        "sessions": [],
        "by_uid": {},
        "raw": {name: {} for name in ADAPTERS},
        "files": {},
        "built_at": 0.0,
        "checked_at": 0.0,
        "dirty": False,
        "sig": None,
        "cache_written_at": 0.0,
        "cache_pending": None,
    }


_state = _empty_state()


# inventory value: (kind, owning primary path, size, mtime_ns, inode)
_KIND_SOURCE = {
    "claude-main": "claude",
    "claude-agent": "claude",
    "claude-agent-meta": "claude",
    "codex-main": "codex",
    "grok-summary": "grok",
    "grok-chat": "grok",
}


def _scandir(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _entry_is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _inventory() -> dict[str, tuple[str, str, int, int, int]]:
    """枚举索引依赖，但不解析会话正文。子文件同时记录其主会话 owner。

    这是列表轮询的地板：每秒一次的预热扫描要把 ~1600 个文件各 stat 一遍。
    用 os.scandir 单趟走目录树，比 Path.glob 分三趟匹配快一倍多；输出与
    glob 版本逐项相同（同样的路径键、同样的 owner 规则）。
    """
    from .adapters import CLAUDE_ROOT, CODEX_ROOT, CODEX_INDEX, GROK_ROOT

    files = {}

    def add(path: str, kind: str, owner: str):
        try:
            st = os.stat(path)
        except OSError:
            return
        files[path] = (kind, owner, st.st_size, st.st_mtime_ns,
                       int(getattr(st, "st_ino", 0)))

    if CLAUDE_ROOT.is_dir():
        for project in _scandir(str(CLAUDE_ROOT)):
            if not _entry_is_dir(project):
                continue
            for item in _scandir(project.path):
                if item.name.endswith(".jsonl"):
                    add(item.path, "claude-main", item.path)
                if not _entry_is_dir(item):
                    continue
                owner = os.path.join(project.path, f"{item.name}.jsonl")
                for sub in _scandir(os.path.join(item.path, "subagents")):
                    if sub.name.endswith(".jsonl"):
                        add(sub.path, "claude-agent", owner)
                    elif sub.name.endswith(".meta.json"):
                        add(sub.path, "claude-agent-meta", owner)
    if CODEX_ROOT.is_dir():
        stack = [str(CODEX_ROOT)]
        while stack:
            for entry in _scandir(stack.pop()):
                if _entry_is_dir(entry):
                    stack.append(entry.path)
                elif entry.name.endswith(".jsonl"):
                    add(entry.path, "codex-main", entry.path)
    if GROK_ROOT.is_dir():
        for top in _scandir(str(GROK_ROOT)):
            if not _entry_is_dir(top):
                continue
            for session in _scandir(top.path):
                if not _entry_is_dir(session):
                    continue
                add(os.path.join(session.path, "summary.json"),
                    "grok-summary", session.path)
                add(os.path.join(session.path, "chat_history.jsonl"),
                    "grok-chat", session.path)
    if CODEX_INDEX.exists():
        add(str(CODEX_INDEX), "codex-index", "")
    return files


def _signature(files=None) -> str:
    """为一次已经枚举完成的 inventory 生成稳定签名。"""
    import hashlib
    files = _inventory() if files is None else files
    h = hashlib.sha1()
    # 文件没有变化时，索引语义升级也必须让已打开的浏览器换新列表；否则
    # 它会拿旧 sig 得到 unchanged，并永久保留升级前被误隐藏的会话。
    h.update(f"schema:{CACHE_VERSION}\n".encode())
    for path, (kind, owner, size, mtime_ns, inode) in sorted(files.items()):
        h.update(f"{kind}|{owner}|{path}|{size}|{mtime_ns}|{inode}\n".encode())
    return h.hexdigest()


_PRIMARY_KIND = {
    "claude": "claude-main",
    "codex": "codex-main",
    "grok": "grok-summary",
}


def _read_owner(source: str, owner: str, files: dict) -> dict | None:
    """严格读取 inventory 中的 owner；瞬时 I/O 失败不能伪装成删除。"""
    refresh = getattr(ADAPTERS[source], "session_meta", None)
    if refresh is None:
        raise RuntimeError(f"{source} adapter does not support indexed scan")
    row = refresh(owner)
    if row is None:
        primary = _PRIMARY_KIND[source]
        still_present = any(kind == primary and entry_owner == owner and size > 0
                            for kind, entry_owner, size, *_ in files.values())
        if still_present:
            raise OSError(f"{source} owner disappeared while reading: {owner}")
        return None

    if source == "claude":
        expected = {
            Path(path).stem.removeprefix("agent-")
            for path, (kind, entry_owner, *_rest) in files.items()
            if kind == "claude-agent" and entry_owner == owner
        }
        actual = {str(item.get("id") or "") for item in row.get("agent_items") or []}
        if actual != expected:
            raise OSError(f"claude subagent inventory changed while reading: {owner}")
    return row


def _scan_raw(files: dict | None = None) -> dict[str, dict[str, dict]]:
    """按同一份 inventory 全量解析，避免独立枚举产生幽灵或漏行。"""
    files = _inventory() if files is None else files
    raw = {name: {} for name in ADAPTERS}
    owners = set()
    for kind, owner, *_ in files.values():
        source = _KIND_SOURCE.get(kind)
        if source and owner:
            owners.add((source, owner))
    for source, owner in sorted(owners):
        row = _read_owner(source, owner, files)
        if row is not None:
            raw[source][owner] = row
    return raw


def _finalize(raw: dict[str, dict[str, dict]]) -> list[dict]:
    """从每文件 raw 元数据生成公开列表；拓扑计算只操作内存。"""
    out = []
    for name, ad in ADAPTERS.items():
        rows = list(raw.get(name, {}).values())
        if hasattr(ad, "finalize_sessions"):
            rows = ad.finalize_sessions(rows)
        else:
            rows = [dict(row) for row in rows]
        out.extend(rows)
    out.sort(key=lambda s: s["updated"], reverse=True)
    return out


def _build() -> list[dict]:
    """兼容显式全量构建调用；正常 append 由 load() 做单 owner 刷新。"""
    files = _inventory()
    return _finalize(_scan_raw(files))


def _refresh_raw(raw: dict[str, dict[str, dict]], old_files: dict,
                 new_files: dict) -> dict[str, dict[str, dict]] | None:
    """只重读变化文件所属的主会话；返回 None 表示 adapter 不支持局部刷新。"""
    changed = {path for path in old_files.keys() | new_files.keys()
               if old_files.get(path) != new_files.get(path)}
    owners = set()
    for path in changed:
        entry = new_files.get(path) or old_files.get(path)
        kind, owner = entry[0], entry[1]
        if kind == "codex-index":
            continue  # 名称在 Codex finalize 阶段从小型全局文件重新套用
        source = _KIND_SOURCE.get(kind)
        if source and owner:
            owners.add((source, owner))

    updated = {name: dict(raw.get(name, {})) for name in ADAPTERS}
    for source, owner in sorted(owners):
        row = _read_owner(source, owner, new_files)
        if row is None:
            updated[source].pop(owner, None)
        else:
            updated[source][owner] = row
    return updated


def _audit_inventory_changes(old_files: dict, new_files: dict) -> None:
    """Record file-level evidence even before a changed owner is parsed."""
    if not old_files:
        return
    owner_uids = {
        (str(row.get("source") or ""), str(row.get("path") or "")):
            str(row.get("uid") or "")
        for row in _state.get("sessions", [])
    }
    for path in sorted(old_files.keys() | new_files.keys()):
        previous, current = old_files.get(path), new_files.get(path)
        if previous == current:
            continue
        entry = current or previous
        kind, owner = entry[0], entry[1]
        source = _KIND_SOURCE.get(kind, "codex" if kind == "codex-index" else "")
        uid = owner_uids.get((source, str(owner)), "")
        audit.record(
            "jsonl.file.changed", category="filesystem", uid=uid, source=source,
            data={"path": path, "kind": kind, "owner": owner,
                  "change": "created" if previous is None else
                            "deleted" if current is None else "modified",
                  "before": previous, "after": current},
        )


def _cache_raw(value) -> dict[str, dict[str, dict]]:
    """严格恢复 v3 raw schema；任何异常都让调用方安全回退全量扫描。"""
    if not isinstance(value, dict):
        raise ValueError("missing raw cache")
    raw = {name: {} for name in ADAPTERS}
    for name in ADAPTERS:
        rows = value.get(name)
        if not isinstance(rows, list):
            raise ValueError(f"invalid {name} raw cache")
        for row in rows:
            if not isinstance(row, dict) or not row.get("path"):
                raise ValueError(f"invalid {name} row")
            raw[name][str(row["path"])] = row
    return raw


def _cache_files(value) -> dict[str, tuple[str, str, int, int, int]]:
    if not isinstance(value, dict):
        raise ValueError("missing inventory cache")
    files = {}
    for path, entry in value.items():
        if not isinstance(path, str) or not isinstance(entry, list) or len(entry) != 5:
            raise ValueError("invalid inventory cache")
        kind, owner, size, mtime_ns, inode = entry
        files[path] = (str(kind), str(owner), int(size), int(mtime_ns), int(inode))
    return files


def _read_cache() -> tuple[dict, dict, str, float] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(CACHE_FILE.read_text())
        if not isinstance(cached, dict):
            return None
        if cached.get("version") != CACHE_VERSION or not cached.get("sig"):
            return None
        return (_cache_raw(cached.get("raw")), _cache_files(cached.get("files")),
                str(cached["sig"]), float(cached.get("built_at", 0)))
    except (OSError, TypeError, ValueError, OverflowError):
        return None


def _write_cache(raw: dict, sessions: list[dict], files: dict,
                 sig: str, built_at: float):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "version": CACHE_VERSION,
            "sig": sig,
            "built_at": built_at,
            "sessions": sessions,
            "raw": {name: list(raw.get(name, {}).values()) for name in ADAPTERS},
            "files": {path: list(entry) for path, entry in files.items()},
        }, ensure_ascii=False)
        temp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        temp.write_text(payload)
        temp.replace(CACHE_FILE)
    except OSError:
        pass


def _schedule_cache_write(raw: dict, sessions: list[dict], files: dict,
                          sig: str, built_at: float) -> None:
    """按 CACHE_WRITE_INTERVAL 限频落盘；来不及写的版本先挂起。调用方持有 _lock。"""
    now = time.monotonic()
    if now - _state["cache_written_at"] >= CACHE_WRITE_INTERVAL:
        _write_cache(raw, sessions, files, sig, built_at)
        _state["cache_written_at"] = now
        _state["cache_pending"] = None
    else:
        _state["cache_pending"] = (raw, sessions, files, sig, built_at)


def _flush_cache_write() -> None:
    """inventory 没变的一轮：把上次挂起的版本补写到磁盘。调用方持有 _lock。"""
    pending = _state["cache_pending"]
    if pending and time.monotonic() - _state["cache_written_at"] >= CACHE_WRITE_INTERVAL:
        _write_cache(*pending)
        _state["cache_written_at"] = time.monotonic()
        _state["cache_pending"] = None


def _publish(raw: dict, sessions: list[dict], files: dict, sig: str,
             built_at: float, checked_at: float, dirty: bool):
    """一次替换完整快照，HTTP 读者不会看到半新半旧的组合。"""
    global _state
    _state = {
        "initialized": True,
        "sessions": sessions,
        "by_uid": {row["uid"]: row for row in sessions},
        "raw": raw,
        "files": files,
        "built_at": built_at,
        "checked_at": checked_at,
        "dirty": dirty,
        "sig": sig,
        "cache_written_at": _state["cache_written_at"],
        "cache_pending": _state["cache_pending"],
    }


def signature() -> str:
    """返回已发布列表对应的签名，不在普通读请求里偷偷扫描磁盘。"""
    cached()
    return str(_state["sig"] or "")


def load(force: bool = False, ttl: float | None = None) -> list[dict]:
    """扫描 inventory 并增量协调；同一时刻只允许一个刷新者。

    ttl 是调用方接受的快照年龄（默认 CHECK_TTL）；列表轮询传 POLL_TTL，
    由预热线程负责把快照保持在这个年龄以内。
    """
    ttl = CHECK_TTL if ttl is None else ttl
    with _lock:
        now = time.monotonic()
        if (not force and _state["initialized"]
                and _state["checked_at"] > 0
                and now - _state["checked_at"] <= ttl):
            return _state["sessions"]

        files = _inventory()
        if _state["initialized"]:
            _audit_inventory_changes(_state["files"], files)
        sig = _signature(files)
        if (not force and _state["initialized"] and not _state["dirty"]
                and _state["sig"] == sig):
            _publish(_state["raw"], _state["sessions"], files, sig,
                     _state["built_at"], time.monotonic(), False)
            _flush_cache_write()
            return _state["sessions"]

        if not force and not _state["initialized"]:
            restored = _read_cache()
            if restored is not None:
                raw, cached_files, cached_sig, built_at = restored
                try:
                    if cached_sig != sig:
                        raw = _refresh_raw(raw, cached_files, files)
                        if raw is None:
                            raise ValueError("cache cannot be incrementally refreshed")
                        built_at = time.time()
                    sessions = _finalize(raw)
                except Exception:
                    pass
                else:
                    dirty = _inventory() != files
                    _publish(raw, sessions, files, sig, built_at,
                             time.monotonic(), dirty)
                    if not dirty and cached_sig != sig:
                        _schedule_cache_write(raw, sessions, files, sig, built_at)
                    return sessions

        t0 = time.time()
        # dirty 且 inventory 又回到原签名时，空 diff 无法证明候选 raw 与磁盘
        # 一致（例如解析窗口里短暂出现又消失的文件），必须按本次 inventory
        # 重读，而不是直接清掉 dirty。
        full = (force or not _state["initialized"]
                or (_state["dirty"] and _state["sig"] == sig))
        try:
            raw = _scan_raw(files) if full else _refresh_raw(
                _state["raw"], _state["files"], files)
            if raw is None:
                full = True
                raw = _scan_raw(files)
            sessions = _finalize(raw)
        except Exception as e:
            # 局部文件可能正写到一半。保留上一份完整快照且不认领新 stamp，
            # 下一次 load 即使磁盘没有再次变化也会重试。
            if _state["initialized"]:
                _publish(_state["raw"], _state["sessions"], _state["files"],
                         _state["sig"], _state["built_at"], time.monotonic(), True)
                print(f"[agenthub] 索引增量刷新失败，稍后重试: {e}")
                return _state["sessions"]
            raise

        built_at = time.time()
        # 若解析期间又有 append，只发布本轮起点对应的签名并立即标 dirty；
        # 不能拿更新的签名给较旧 rows 背书。下一轮只会再读变化 owner。
        dirty = _inventory() != files
        _publish(raw, sessions, files, sig, built_at, time.monotonic(), dirty)
        if not dirty:
            _schedule_cache_write(raw, sessions, files, sig, built_at)
        if full:
            print(f"[agenthub] 索引重建: {len(sessions)} 个会话, {time.time() - t0:.1f}s")
        return _state["sessions"]


def load_snapshot(force: bool = False,
                  ttl: float | None = None) -> tuple[list[dict], str, float]:
    """原子取得同一发布版本的列表、签名和构建时间。"""
    while True:
        sessions = load(force=force, ttl=ttl)
        force = False
        with _lock:
            if sessions is _state["sessions"]:
                return sessions, str(_state["sig"] or ""), _state["built_at"]


def cached() -> list[dict]:
    """读取最近一次完整快照；只有进程尚未初始化时才扫描磁盘。"""
    if not _state["initialized"]:
        return load()
    return _state["sessions"]


def get(uid: str) -> dict | None:
    """O(1) 查询已发布 UID，不让消息、live、outbox 热路径触发刷新。"""
    if not _state["initialized"]:
        load()
    return _state["by_uid"].get(uid)


def data_file(s: dict) -> Path:
    """会话真正的数据文件 (grok 的 path 是目录)。"""
    p = Path(s["path"])
    return p / "chat_history.jsonl" if s["source"] == "grok" else p


def _claude_effective_tip(s: dict, pos: int | None = None) -> str | None:
    """合并 Claude 磁盘树与 agenthub 从原生 TUI 确认的未落盘回滚。"""
    ad = ADAPTERS.get(s.get("source"))
    if not isinstance(ad, ClaudeAdapter):
        return None
    path = str(data_file(s))
    agent = s.get("agent_id")
    if agent:
        return ad.active_tip(path, pos=pos, agent=agent)
    timeline = session_meta.timeline(str(s.get("uid") or ""))
    if not timeline:
        return ad.active_tip(path, pos=pos)
    if pos is None:
        try:
            pos = data_file(s).stat().st_size
        except OSError:
            pos = 0
    stale_end = int(timeline["stale_end"])
    # 回滚以后真正发送的新输入会在旧 EOF 后追加一条带 parentUuid 的记录；
    # 从那一刻起新记录再次成为权威。仅有 sidechain/无图事件追加则继续用 pin。
    appended = ad.latest_tip_after(path, stale_end, end=pos) if pos > stale_end else None
    return appended or str(timeline["tip"])


def claude_screen_tip(s: dict, screen: str) -> str | None:
    ad = ADAPTERS.get(s.get("source"))
    if not isinstance(ad, ClaudeAdapter) or s.get("agent_id"):
        return None
    return ad.match_screen_tip(str(data_file(s)), screen)


def _head_hash(f: Path, limit: int = 4096) -> str:
    import hashlib
    try:
        with open(f, "rb") as fh:
            return hashlib.sha1(fh.read(max(0, limit))).hexdigest()[:16]
    except OSError:
        return ""


def _cursor_head(f: Path, limit: int = 4096) -> str:
    """把解析语义版本带进续读游标，升级后让已打开页面自动整份重建。"""
    return f"{MESSAGE_CURSOR_VERSION}:{_head_hash(f, limit)}"


def version(s: dict) -> dict:
    """用 (大小, mtime, 文件头哈希) 标识一个版本。

    会话通常是 append-only 的；续读时用旧 EOF 所对应的固定长度前缀和尾部
    锚点确认旧内容没变。Claude 另在游标中携带树的当前叶子。任一校验失败
    都说明内容被重写或逻辑时间线发生回退，必须整份重来。
    """
    f = data_file(s)
    try:
        st = f.stat()
    except OSError:
        return {"size": 0, "mtime": 0, "head": ""}
    return {"size": st.st_size, "mtime": int(st.st_mtime * 1000),
            "head": _cursor_head(f)}


ANCHOR = 512      # 续读前校验偏移点之前这么多字节的内容
INITIAL_HEAD_MESSAGES = 100
INITIAL_TAIL_MESSAGES = 500


_window_cache_memory: OrderedDict[str, tuple[dict, dict]] = OrderedDict()
_window_cache_locks: dict[str, threading.Lock] = {}
_window_cache_guard = threading.Lock()


_identity_cache: dict[tuple[str, str, str], str] = {}


def _window_cache_identity(s: dict) -> str:
    """缓存文件名只暴露散列，不把会话路径或 uid 写进目录项。"""
    probe = (str(s.get("source") or ""), str(s.get("path") or ""),
             str(s.get("agent_id") or ""))
    identity = _identity_cache.get(probe)
    if identity is None:
        raw = "\0".join((probe[0], str(data_file(s)), probe[2]))
        identity = _identity_cache[probe] = hashlib.sha256(raw.encode()).hexdigest()
    return identity


def _window_dependency(path: str | Path, role: str,
                       limit: int | None = None) -> dict:
    """记录能识别内部改写、截断和路径替换的文件身份。"""
    # 字符串输入只来自本模块已经规范化过的记录, 不再经 pathlib 往返。
    path = path if isinstance(path, str) else str(path)
    row = {"role": role, "path": path, "limit": limit}
    try:
        st = os.stat(path)
    except OSError:
        return {**row, "missing": True}
    return {**row, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "inode": int(getattr(st, "st_ino", 0))}


def _window_cache_stamp(s: dict, ad) -> dict:
    """返回窗口结果的全部解析依赖，而不读取 JSONL 正文。

    不能只看当前文件大小：Codex 回退会继承多个父文件前缀，而且 JSONL
    也可能在中间增、删、等长改写。mtime/ctime/inode 共同保证这些变化令旧
    缓存失效；继承段的路径和截止偏移也属于版本的一部分。
    """
    if s.get("source") == "codex":
        dependencies = _codex_dependencies(s, ad)
    else:
        dependencies = [_window_dependency(data_file(s), "current")]

    semantic = {}
    if isinstance(ad, ClaudeAdapter):
        # timeline_stamp 是落盘语义，服务重启后仍稳定；回退确认与读取都
        # 受 session_meta 的同一把锁保护，不采用重启后会归零的内存 revision。
        semantic["timeline"] = list(session_meta.timeline_stamp(s.get("uid", "")))
    elif s.get("source") == "codex":
        # /rename 不在 rollout 正文中。只依赖当前 sid 的最终名称，避免其他
        # 会话改名导致所有大 Codex 会话的正文缓存一起失效。
        semantic["rename"] = ad._name_event(str(s.get("sid") or ""))

    return {"schema": WINDOW_CACHE_VERSION, "source": s.get("source"),
            "agent": str(s.get("agent_id") or ""),
            "dependencies": dependencies, "semantic": semantic}


def _window_memory_get(key: str, stamp: dict) -> dict | None:
    with _window_cache_guard:
        cached = _window_cache_memory.get(key)
        if not cached or cached[0] != stamp:
            return None
        _window_cache_memory.move_to_end(key)
        value = cached[1]
    # 窗口里的消息已带媒体 token；仓库淘汰后必须重新解析以重新登记。
    if not media.retain(value.get("media_tokens") or ()):
        return None
    return copy.deepcopy(value)


def _window_memory_put(key: str, stamp: dict, value: dict) -> None:
    with _window_cache_guard:
        _window_cache_memory[key] = (copy.deepcopy(stamp), copy.deepcopy(value))
        _window_cache_memory.move_to_end(key)
        while len(_window_cache_memory) > WINDOW_CACHE_MEMORY_ITEMS:
            _window_cache_memory.popitem(last=False)


def _window_key_lock(key: str) -> threading.Lock:
    with _window_cache_guard:
        return _window_cache_locks.setdefault(key, threading.Lock())


def _clear_window_cache_memory() -> None:
    """测试及维护入口；磁盘缓存不受影响。"""
    with _window_cache_guard:
        _window_cache_memory.clear()
        _window_cache_locks.clear()


def _window_disk_read(key: str, stamp: dict) -> dict | None:
    path = WINDOW_CACHE_DIR / f"{key}.json.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, EOFError, ValueError, TypeError):
        return None
    if not isinstance(row, dict) or row.get("schema") != WINDOW_CACHE_VERSION \
            or row.get("stamp") != stamp or not isinstance(row.get("value"), dict):
        return None
    value = row["value"]
    # 服务重启后媒体仓库是空的：引用了本地图片的窗口需要重新解析登记。
    if not media.retain(value.get("media_tokens") or ()):
        return None
    return value


def _window_disk_write(key: str, stamp: dict, value: dict) -> None:
    """私有目录内原子落盘；消息正文绝不能沿用默认的宽松权限。"""
    tmp_path = None
    try:
        WINDOW_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(WINDOW_CACHE_DIR, 0o700)
        fd, raw_path = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp",
                                        dir=WINDOW_CACHE_DIR)
        tmp_path = Path(raw_path)
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1,
                               mtime=0) as zipped:
                zipped.write(json.dumps(
                    {"schema": WINDOW_CACHE_VERSION, "stamp": stamp,
                     "value": value}, ensure_ascii=False,
                    separators=(",", ":")).encode())
        os.replace(tmp_path, WINDOW_CACHE_DIR / f"{key}.json.gz")
        tmp_path = None
    except (OSError, ValueError, TypeError):
        # 缓存失败不能影响会话读取；下一次请求至多重新解析一次。
        pass
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _window_cache_eligible(s: dict) -> bool:
    try:
        logical_size = int(s.get("size") or 0)
    except (TypeError, ValueError):
        logical_size = 0
    if logical_size < WINDOW_CACHE_MIN_BYTES:
        try:
            logical_size = data_file(s).stat().st_size
        except OSError:
            logical_size = 0
    return logical_size >= WINDOW_CACHE_MIN_BYTES


def _cached_initial_window(s: dict, ad, build) -> dict:
    """跨请求、跨服务重启复用大会话的首尾窗口。

    同一会话只允许一个构建者。解析前后再取一次完整依赖指纹；若期间任一
    JSONL 被修改，本次结果仍可返回，但绝不写入缓存。
    """
    key = _window_cache_identity(s)
    before = _window_cache_stamp(s, ad)
    cached = _window_memory_get(key, before)
    if cached is not None:
        return cached

    with _window_key_lock(key):
        before = _window_cache_stamp(s, ad)
        cached = _window_memory_get(key, before)
        if cached is None:
            cached = _window_disk_read(key, before)
        if cached is not None:
            _window_memory_put(key, before, cached)
            return copy.deepcopy(cached)

        value = build()
        after = _window_cache_stamp(s, ad)
        if after == before:
            _window_memory_put(key, before, value)
            _window_disk_write(key, before, value)
        return copy.deepcopy(value)


# ---- 解析视图缓存 ----------------------------------------------------------
#
# 整份读取的结果按会话视图缓存在进程内：消息对象、序列化后的 JSON 正文以及
# adapter 的续读状态。指纹与窗口缓存相同 (路径/大小/mtime/ctime/inode、Codex
# 继承段、Claude 时间线、Codex 改名)。文件只增长时从上次 EOF 起只解析尾部并
# 拼接；截断、改写、指纹以外的变化都退回整份解析。同一视图同一时刻只有一个
# 解析者，其余请求等待后共用结果。


class CachedMessages(list):
    """整份读取返回的消息列表；附带已序列化的 JSON 正文供响应直接拼接。"""

    __slots__ = ("view",)

    def __init__(self, items, view):
        super().__init__(items)
        self.view = view

    def __reduce__(self):
        # deepcopy / pickle 得到普通列表；缓存视图及其锁不随之复制。
        return (list, (list(self),))

    @property
    def json_bytes(self) -> bytes:
        """与 json.dumps(list, ensure_ascii=False).encode() 逐字节相同。"""
        return self.view.body

    def deflate(self, level: int) -> tuple[bytes, int, int]:
        """正文去掉末尾 ']' 后的 raw deflate 流 (已 sync flush)、其 crc32 和长度。"""
        return self.view.deflate(level)


class _View:
    __slots__ = ("stamp", "end", "head", "anchor", "tip", "messages",
                 "message_total", "activity", "activity_changed", "state",
                 "tokens", "body", "floor", "complete", "_deflate", "_deflate_lock")

    def __init__(self):
        self.stamp = None
        self.end = 0
        self.head = ""
        self.anchor = ""
        self.tip = None
        self.messages: list[dict] = []
        self.message_total = 0
        self.activity = None
        self.activity_changed = False
        self.state = None
        self.tokens: list[str] = []
        self.body = b"[]"
        self.floor = 0
        self.complete = True
        self._deflate = None
        self._deflate_lock = threading.Lock()

    @property
    def cost(self) -> int:
        return len(self.body) + (len(self._deflate[1]) if self._deflate else 0)

    def deflate(self, level: int) -> tuple[bytes, int, int]:
        with self._deflate_lock:
            if self._deflate is None or self._deflate[0] != level:
                opened = self.body[:-1]
                packer = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
                data = packer.compress(opened) + packer.flush(zlib.Z_SYNC_FLUSH)
                self._deflate = (level, data, zlib.crc32(opened), len(opened))
                _view_cache_account()
            _, data, crc, length = self._deflate
            return data, crc, length

    def extend_deflate(self, previous: "_View", tail: bytes) -> None:
        """追加时沿用旧视图已压缩的前缀，只压缩新增正文。"""
        with previous._deflate_lock:
            cached = previous._deflate
        if cached is None:
            return
        level, data, crc, length = cached
        packer = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
        data = data + packer.compress(tail) + packer.flush(zlib.Z_SYNC_FLUSH)
        self._deflate = (level, data, zlib.crc32(tail, crc), length + len(tail))


_view_cache: OrderedDict[str, _View] = OrderedDict()
_view_cache_guard = threading.Lock()
_view_locks: dict[str, threading.Lock] = {}


def _view_lock(key: str) -> threading.Lock:
    with _view_cache_guard:
        return _view_locks.setdefault(key, threading.Lock())


def _view_cache_account() -> None:
    with _view_cache_guard:
        _view_cache_trim()


def _view_cache_trim() -> None:
    total = sum(view.cost for view in _view_cache.values())
    while _view_cache and (len(_view_cache) > VIEW_CACHE_MAX_ITEMS
                           or total > VIEW_CACHE_MAX_BYTES):
        _, gone = _view_cache.popitem(last=False)
        total -= gone.cost


def _view_put(key: str, view: _View) -> None:
    with _view_cache_guard:
        _view_cache[key] = view
        _view_cache.move_to_end(key)
        _view_cache_trim()


def _view_peek(key: str) -> _View | None:
    with _view_cache_guard:
        return _view_cache.get(key)


def _view_get(key: str, stamp: dict) -> _View | None:
    with _view_cache_guard:
        view = _view_cache.get(key)
        if view is None or view.stamp != stamp:
            return None
        _view_cache.move_to_end(key)
    if not media.retain(view.tokens):
        return None
    return view


def _clear_view_cache() -> None:
    """测试及维护入口。"""
    with _view_cache_guard:
        _view_cache.clear()
        _view_locks.clear()


def view_cache_stats() -> dict:
    with _view_cache_guard:
        return {"items": len(_view_cache),
                "bytes": sum(view.cost for view in _view_cache.values())}


def _media_tokens(messages) -> list[str]:
    """消息引用的本地媒体 token；外链媒体不经过仓库。"""
    tokens = []
    for message in messages:
        for item in message.get("media") or ():
            src = str(item.get("src") or "")
            if src.startswith("/api/media/"):
                tokens.append(src[len("/api/media/"):])
    return tokens


def _split_status(msgs: list[dict]) -> tuple[list[dict], list[dict]]:
    events = [m for m in msgs if m.get("role") == "status"]
    return [m for m in msgs if m.get("role") != "status"], events


def _line_complete(f: Path, end: int) -> bool:
    """EOF 落在完整一行之后才能缓存；半行会被 _iter_records 跳过，整读可补回。"""
    if end <= 0:
        return True
    try:
        with open(f, "rb") as fh:
            fh.seek(end - 1)
            return fh.read(1) == b"\n"
    except OSError:
        return False


def _claude_read_options(s: dict, ver: dict) -> dict:
    timeline = (session_meta.timeline(str(s.get("uid") or ""))
                if not s.get("agent_id") else None)
    return {"agent": s.get("agent_id"),
            "declared_tip": _claude_effective_tip(s, pos=ver["size"]),
            "abandoned_after": int((timeline or {}).get("stale_end") or 0)}


def _finish_view(s: dict, ad, view: _View, stamp: dict) -> _View:
    f = data_file(s)
    view.stamp = stamp
    view.head = _cursor_head(f, min(4096, view.end))
    view.anchor = _anchor_hash(f, view.end)
    view.tip = (_claude_effective_tip(s, pos=view.end)
                if isinstance(ad, ClaudeAdapter) else None)
    view.complete = _line_complete(f, view.end)
    view.message_total = sum(m.get("counted") is not False for m in view.messages)
    return view


def _view_parse(s: dict, ad, ver: dict, stamp: dict) -> _View:
    """整份解析并生成缓存视图。"""
    if isinstance(ad, ClaudeAdapter):
        msgs, end, state = ad.read_state(s["path"], **_claude_read_options(s, ver))
    else:
        msgs, end, state = ad.read_state(s["path"])
    view = _View()
    view.end = end
    view.state = state
    view.messages, events = _split_status(msgs)
    for msg in view.messages:
        media.enrich_message(msg, s.get("cwd"))
    view.activity_changed = bool(events)
    view.activity = events[-1] if events else None
    if s.get("source") == "codex":
        own_start = int((state or {}).get("own_start") or 0)
        view.floor = sum(1 for m in msgs[:own_start] if m.get("role") != "status")
    view.tokens = _media_tokens(view.messages)
    view.body = json.dumps(view.messages, ensure_ascii=False).encode()
    return _finish_view(s, ad, view, stamp)


def _view_grew(s: dict, ad, old: _View, stamp: dict) -> bool:
    """旧视图之后文件是否只在末尾追加：与浏览器续读游标同一套校验。"""
    previous = old.stamp
    if not previous or not old.complete or old.state is None:
        return False
    for field in ("schema", "source", "agent", "semantic"):
        if previous.get(field) != stamp.get(field):
            return False
    before, after = previous["dependencies"], stamp["dependencies"]
    if len(before) != len(after) or before[:-1] != after[:-1]:
        return False
    was, now = before[-1], after[-1]
    if was.get("missing") or now.get("missing"):
        return False
    if (was["path"] != now["path"] or was["inode"] != now["inode"]
            or now["size"] <= old.end or now["mtime_ns"] < was["mtime_ns"]):
        return False
    f = data_file(s)
    if _cursor_head(f, min(4096, old.end)) != old.head:
        return False
    if _anchor_hash(f, old.end) != old.anchor:
        return False
    if isinstance(ad, ClaudeAdapter):
        prefix_tip = _claude_effective_tip(s, pos=old.end)
        if prefix_tip:
            if prefix_tip != old.tip or not ad.append_extends(
                    str(f), old.end, prefix_tip, agent=s.get("agent_id")):
                return False
        elif old.tip:
            return False
    return True


def _view_extend(s: dict, ad, old: _View, ver: dict, stamp: dict) -> _View | None:
    """只解析旧 EOF 之后的记录并接到旧视图之后；无法保证等价时返回 None。"""
    if isinstance(ad, ClaudeAdapter):
        msgs, end, state = ad.read_state(
            s["path"], **_claude_read_options(s, ver), state=old.state)
    elif s.get("source") == "codex":
        msgs, end, state = ad.read_state(
            s["path"], state=old.state, prefix=old.messages, prefix_floor=old.floor)
    else:
        msgs, end, state = ad.read_state(s["path"], state=old.state)
    if state is None:
        return None
    tail, events = _split_status(msgs)
    for msg in tail:
        media.enrich_message(msg, s.get("cwd"))
    patches = list((state or {}).get("patches") or ())
    view = _View()
    view.end = end
    view.state = state
    combined = list(old.messages)
    for index, replacement in patches:
        combined[index] = replacement
    combined.extend(tail)
    view.messages = combined
    view.activity_changed = old.activity_changed or bool(events)
    view.activity = events[-1] if events else old.activity
    view.floor = old.floor
    view.tokens = old.tokens + _media_tokens(tail) if not patches else _media_tokens(combined)
    if patches:
        view.body = json.dumps(combined, ensure_ascii=False).encode()
    elif not tail:
        view.body = old.body
        view._deflate = old._deflate
    else:
        tail_json = json.dumps(tail, ensure_ascii=False).encode()
        if old.messages:
            joint = b", " + tail_json[1:-1]
            view.body = old.body[:-1] + joint + b"]"
            view.extend_deflate(old, joint)
        else:
            view.body = tail_json
    return _finish_view(s, ad, view, stamp)


def _view_for(s: dict, ad, ver: dict) -> _View:
    """取得会话视图：命中缓存、增量拼接或整份解析，同一视图单飞。"""
    key = _window_cache_identity(s)
    before = _window_cache_stamp(s, ad)
    view = _view_get(key, before)
    if view is not None:
        return view
    with _view_lock(key):
        before = _window_cache_stamp(s, ad)
        view = _view_get(key, before)
        if view is not None:
            return view
        old = _view_peek(key)
        view = None
        if old is not None and _view_grew(s, ad, old, before):
            view = _view_extend(s, ad, old, ver, before)
        if view is None:
            view = _view_parse(s, ad, ver, before)
        after = _window_cache_stamp(s, ad)
        if after == before and view.complete:
            _view_put(key, view)
        return view


def _batch_from_view(view: _View, initial_window: bool) -> dict:
    msgs = view.messages
    partial = None
    window_size = INITIAL_HEAD_MESSAGES + INITIAL_TAIL_MESSAGES
    if initial_window:
        if len(msgs) > window_size:
            omitted = len(msgs) - window_size
            msgs = msgs[:INITIAL_HEAD_MESSAGES] + msgs[-INITIAL_TAIL_MESSAGES:]
            partial = {"head": INITIAL_HEAD_MESSAGES, "tail": INITIAL_TAIL_MESSAGES,
                       "omitted": omitted}
        msgs = [dict(m) for m in msgs]
    else:
        # 消费者只读消息，但仍给每条一个浅副本，避免响应处理误改缓存对象。
        msgs = CachedMessages((dict(m) for m in msgs), view)
    return {"end": view.end, "messages": msgs, "message_total": view.message_total,
            "partial": partial, "activity_changed": view.activity_changed,
            "activity": view.activity, "enriched": True,
            "media_tokens": _media_tokens(msgs) if initial_window else None}


def _view_batch(s: dict, ad, ver: dict, initial_window: bool) -> dict:
    return _batch_from_view(_view_for(s, ad, ver), initial_window)


def _anchor_hash(f: Path, pos: int) -> str:
    """偏移点之前一小段内容的哈希, 用来确认"接着读"接的是同一份内容。"""
    if pos <= 0:
        return ""
    import hashlib
    lo = max(0, pos - ANCHOR)
    try:
        with open(f, "rb") as fh:
            fh.seek(lo)
            return hashlib.sha1(fh.read(pos - lo)).hexdigest()[:16]
    except OSError:
        return ""


def _anchor_parts(anchor: str) -> tuple[str, str | None]:
    """拆分内容锚点与 Claude 逻辑叶子；旧客户端只会带前半段。"""
    raw, sep, tip = str(anchor or "").partition("@")
    return raw, tip if sep and tip else None


def _cursor_anchor(s: dict, pos: int) -> str:
    """内容锚点之外，为 Claude 带上该偏移处的逻辑叶子。"""
    raw = _anchor_hash(data_file(s), pos)
    ad = ADAPTERS.get(s.get("source"))
    if isinstance(ad, ClaudeAdapter):
        tip = _claude_effective_tip(s, pos=pos)
        if tip:
            return f"{raw}@{tip}"
    return raw


def cursor(s: dict) -> dict:
    """供浏览器低成本跟踪后台会话追加内容的安全续读游标。"""
    ver = version(s)
    return {"end": ver["size"], "head": ver["head"],
            "anchor": _cursor_anchor(s, ver["size"])}


_cursor_cache: dict[tuple[str, str, str], tuple[tuple, dict]] = {}
_cursor_cache_lock = threading.Lock()


def _data_path(s: dict) -> str:
    """data_file() 的字符串版；列表路径每次要算几百个，绕开 pathlib 的开销。"""
    path = str(s["path"])
    return os.path.join(path, "chat_history.jsonl") if s["source"] == "grok" else path


def _path_stamp(path: str, uid: str) -> tuple:
    """游标缓存版本；ctime/inode 补上同尺寸重写和路径复用的边界。"""
    try:
        st = os.stat(path)
    except OSError:
        return (path, 0, 0, 0, 0, session_meta.timeline_revision(uid))
    return (path, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            int(getattr(st, "st_ino", 0)), session_meta.timeline_revision(uid))


def _cursor_stamp(s: dict) -> tuple:
    return _path_stamp(_data_path(s), str(s.get("uid") or ""))


def _cursor_hit(cache_id: tuple, stamp: tuple) -> dict | None:
    """缓存里有这个版本的游标就复制一份；没有或文件不在返回 None。"""
    cached_value = _cursor_cache.get(cache_id)
    if cached_value and cached_value[0] == stamp and stamp[2]:
        return dict(cached_value[1])
    return None


def _cached_cursor(s: dict) -> dict:
    """按文件版本复用游标；同一新版本并发到达时只允许一次重读。"""
    cache_id = (str(s.get("source") or ""), _data_path(s),
                str(s.get("agent_id") or ""))
    stamp = _cursor_stamp(s)
    cached_value = _cursor_cache.get(cache_id)
    if cached_value and cached_value[0] == stamp:
        return dict(cached_value[1])

    with _cursor_cache_lock:
        # 等锁期间另一个请求可能已经完成同一版本，锁内必须二次检查。
        stamp = _cursor_stamp(s)
        cached_value = _cursor_cache.get(cache_id)
        if cached_value and cached_value[0] == stamp:
            return dict(cached_value[1])

        value = cursor(s)
        after = _cursor_stamp(s)
        if after == stamp:
            _cursor_cache[cache_id] = (stamp, value)
            if len(_cursor_cache) > 4096:
                # 路径是 append-only key，不按版本增长；这里只限制长期频繁删除、
                # 移动后留下的旧路径。删最早的一批至多令静态会话重算一次。
                for old_key in list(_cursor_cache)[:1024]:
                    _cursor_cache.pop(old_key, None)
        # 若读取期间文件继续 append，本次结果仍与旧逻辑一样可用于响应，但不
        # 写缓存；下一个请求会按新 stamp 重算，不能给混合游标错误背书。
        return dict(value)


def _agent_data_path(s: dict, item: dict, agent: str) -> str:
    """session_view() 会给子代理视图算出的 path，纯字符串版。"""
    if s["source"] == "codex":
        return str(item["path"])
    parent = str(s["path"])
    stem = os.path.splitext(os.path.basename(parent))[0]
    return os.path.join(os.path.dirname(parent), stem, "subagents", f"agent-{agent}.jsonl")


def with_cursors(sessions: list[dict]) -> list[dict]:
    """给列表元数据附加主会话及子代理的 EOF 游标。

    这里只读取每个文件头 4 KiB、尾部锚点和 Claude 最后一条树记录；浏览器
    随后只拉变化文件的新增区间，不需要为了左栏未读数下载整份历史。
    子代理的游标先按 (来源, 文件, agent) 直接查缓存，命中就不必构造整份视图；
    只有版本变了才走 session_view() 重算。
    """
    out = []
    for session in sessions:
        row = {**session, "cursor": _cached_cursor(session)}
        items = []
        source = str(session.get("source") or "")
        uid = str(session.get("uid") or "")
        for item in session.get("agent_items") or []:
            copy = dict(item)
            agent = str(item.get("id") or "")
            hit = None
            if source in {"claude", "codex"} and agent:
                path = _agent_data_path(session, item, agent)
                hit = _cursor_hit((source, path, agent), _path_stamp(path, uid))
            if hit is not None:
                copy["cursor"] = hit
            else:
                try:
                    copy["cursor"] = _cached_cursor(session_view(session, agent))
                except KeyError:
                    pass
            items.append(copy)
        if items:
            row["agent_items"] = items
        out.append(row)
    return out


def session_view(s: dict, agent: str = "") -> dict:
    """把子代理作为父会话的一个可切换视图，不升格为独立 session。"""
    if not agent:
        return s
    if s.get("source") not in {"claude", "codex"}:
        raise KeyError(agent)
    item = next((x for x in s.get("agent_items", []) if x.get("id") == agent), None)
    if not item:
        raise KeyError(agent)
    if s["source"] == "codex":
        # 路径仅来自已索引的父子关系，绝不把请求里的 agent 当文件路径解析。
        path = Path(item["path"])
    else:
        path = Path(s["path"]).parent / Path(s["path"]).stem / "subagents" / f"agent-{agent}.jsonl"
    if not path.is_file():
        raise KeyError(agent)
    return {**s, **{k: item[k] for k in ("cwd", "model", "created") if k in item},
            "path": str(path), "sid": agent, "title": item["title"],
            "size": item.get("size", path.stat().st_size),
            "updated": item.get("updated", s["updated"]),
            "agent_id": agent, "agent_type": item.get("type", "subagent"),
            "parent_title": s["title"]}


def messages(uid: str, agent: str = "",
             start: int = 0, head: str = "", anchor: str = "",
             append_only: bool = False, windowed: bool = False) -> dict:
    s = get(uid)
    if not s:
        raise KeyError(uid)
    return messages_for(session_view(s, agent), start, head, anchor,
                        append_only, windowed)


def _read_message_batch(s: dict, ad, start: int, ver: dict,
                        initial_window: bool) -> dict:
    """解析一批消息，并在富媒体展开前生成可安全缓存的纯 JSON 结果。"""
    if isinstance(ad, ClaudeAdapter):
        timeline = (session_meta.timeline(str(s.get("uid") or ""))
                    if not s.get("agent_id") else None)
        msgs, end = ad.read(
            s["path"], start=start, agent=s.get("agent_id"),
            declared_tip=_claude_effective_tip(s, pos=ver["size"]),
            abandoned_after=int((timeline or {}).get("stale_end") or 0))
    else:
        msgs, end = ad.read(s["path"], start=start)
    activity_events = [m for m in msgs if m.get("role") == "status"]
    msgs = [m for m in msgs if m.get("role") != "status"]
    message_total = sum(m.get("counted") is not False for m in msgs)
    partial = None
    window_size = INITIAL_HEAD_MESSAGES + INITIAL_TAIL_MESSAGES
    if initial_window and len(msgs) > window_size:
        omitted = len(msgs) - window_size
        msgs = msgs[:INITIAL_HEAD_MESSAGES] + msgs[-INITIAL_TAIL_MESSAGES:]
        partial = {"head": INITIAL_HEAD_MESSAGES, "tail": INITIAL_TAIL_MESSAGES,
                   "omitted": omitted}
    return {"end": end, "messages": msgs, "message_total": message_total,
            "partial": partial, "activity_changed": bool(activity_events),
            "activity": activity_events[-1] if activity_events else None}


_audit_parse_seen: OrderedDict[tuple, None] = OrderedDict()
_audit_parse_lock = threading.Lock()
AUDIT_INLINE_MESSAGES = 256


def _audit_message_batch(s: dict, result: dict, requested: dict) -> None:
    """Describe the parser boundary without making parsing depend on audit I/O."""
    messages = result.get("messages") or []
    if not (messages or result.get("activity_changed") or result.get("reset")):
        return
    version_value = result.get("version") or {}
    activity_value = result.get("activity") or {}
    fingerprint = (
        str(data_file(s)), str(s.get("agent_id") or ""),
        version_value.get("size"), version_value.get("mtime"),
        version_value.get("head"), bool(result.get("reset")),
        result.get("start"), result.get("end"), len(messages),
        activity_value.get("state"), activity_value.get("ts"),
    )
    with _audit_parse_lock:
        if fingerprint in _audit_parse_seen:
            _audit_parse_seen.move_to_end(fingerprint)
            return
        _audit_parse_seen[fingerprint] = None
        while len(_audit_parse_seen) > 4096:
            _audit_parse_seen.popitem(last=False)
    data = {
        "path": str(data_file(s)), "agent": str(s.get("agent_id") or ""),
        "requested": requested, "reset": bool(result.get("reset")),
        "start": result.get("start"), "end": result.get("end"),
        "version": result.get("version"), "anchor": result.get("anchor"),
        "message_count": len(messages), "message_total": result.get("message_total"),
        "partial": result.get("partial"),
        "activity_changed": bool(result.get("activity_changed")),
    }
    activity = result.get("activity")
    uid, source = str(s.get("uid") or ""), str(s.get("source") or "")

    def record() -> None:
        roles: dict[str, int] = {}
        for message in messages:
            role = str(message.get("role") or "unknown")
            roles[role] = roles.get(role, 0) + 1
        audit.record("jsonl.batch.parsed", category="parser", uid=uid, source=source,
                     data={**data, "roles": roles},
                     content={"messages": messages, "activity": activity})

    if len(messages) <= AUDIT_INLINE_MESSAGES:
        record()
        return
    # 整份读取的证据 blob 要对几万条消息做脱敏、序列化和压缩 (百毫秒级)。
    # 去重已经同步完成，序列化放到后台线程，不让首次打开多等这一段。
    threading.Thread(target=record, name="audit-batch", daemon=True).start()


def messages_for(s: dict, start: int = 0, head: str = "", anchor: str = "",
                 append_only: bool = False, windowed: bool = False) -> dict:
    """整份或增量读取。直接持有会话快照，SSE 每 50ms 只检查目标文件。

    能接着上次读的条件: 文件头没变、文件没缩短、而且**偏移点之前的内容也没变**。
    最后一条是必需的 —— 有些会话会截断后重写。Claude 双 Esc 则更特殊：
    文件只追加、字节锚点完全不变，但树的当前叶子会退回祖先，也必须整份重建。
    """
    requested = {"start": start, "head": head, "anchor": anchor,
                 "append_only": append_only, "windowed": windowed}
    ver = version(s)
    # 小于 4 KiB 的新会话追加后，当前 head 会自然变长、哈希也会变化；应当
    # 用旧 EOF 所确定的同长度前缀校验，而不是把正常追加误判成历史改写。
    ok = bool(start and head and start <= ver["size"]
              and head == _cursor_head(data_file(s), min(4096, start)))
    old_tip = None
    if ok and anchor:
        raw_anchor, old_tip = _anchor_parts(anchor)
        ok = raw_anchor == _anchor_hash(data_file(s), start)
    elif ok and not anchor:
        ok = False                       # 没带锚点就不给续读, 宁可重来
    ad = ADAPTERS[s["source"]]
    if ok and isinstance(ad, ClaudeAdapter):
        # Claude 的文件在双 Esc 时不会截断，只会从旧祖先追加一个新分支。
        # 字节锚点仍然完全匹配，因此还必须比较逻辑叶子及新增链的亲缘关系。
        prefix_tip = _claude_effective_tip(s, pos=start)
        if prefix_tip:
            ok = old_tip == prefix_tip
            if ok:
                ok = ad.append_extends(str(data_file(s)), start, old_tip,
                                       agent=s.get("agent_id"))
        elif old_tip:
            ok = False
    reset = not ok
    if reset and append_only:
        # 后台未读探测绝不能因回滚/重写退化成几十 MB 的整份下载；让调用方
        # 丢弃旧缓存并以当前 EOF 重新建立基线即可。
        end = ver["size"]
        result = {"meta": s, "version": ver, "reset": True,
                  "start": end, "end": end,
                  "anchor": _cursor_anchor(s, end), "messages": [],
                  "message_total": 0, "partial": None,
                  "activity_changed": False, "activity": None}
        _audit_message_batch(s, result, requested)
        return result
    if reset:
        start = 0

    if reset and windowed and _window_cache_eligible(s):
        batch = _cached_initial_window(
            s, ad, lambda: _view_batch(s, ad, ver, True))
    elif reset:
        batch = _view_batch(s, ad, ver, windowed)
    else:
        batch = _read_message_batch(s, ad, start, ver, False)
    msgs = batch["messages"]
    if not batch.get("enriched"):
        for msg in msgs:
            media.enrich_message(msg, s.get("cwd"))
    result = {"meta": s, "version": ver, "reset": reset, "start": start,
              "end": batch["end"],
              "anchor": _cursor_anchor(s, batch["end"]), "messages": msgs,
              "message_total": batch["message_total"], "partial": batch["partial"],
              "activity_changed": batch["activity_changed"],
              "activity": batch["activity"]}
    _audit_message_batch(s, result, requested)
    return result


def delete(uid: str) -> str:
    """移入回收站而非真删, 误操作可恢复。"""
    s = get(uid)
    if not s:
        raise KeyError(uid)
    src = Path(s["path"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = TRASH_DIR / s["source"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stamp}-{src.name}"
    meta = session_meta.snapshot(uid)
    shutil.move(str(src), str(dest))
    try:
        trash.record(dest, s, meta)
    except OSError as e:
        # 清单只影响"能否一键恢复"，文件已经安全落在回收站里，不能因此报错。
        print(f"[agenthub] 回收站清单写入失败，该条目将无法自动恢复: {e}")
    with _lock:
        raw = {name: dict(rows) for name, rows in _state["raw"].items()}
        raw.get(s["source"], {}).pop(str(s["path"]), None)
        try:
            sessions = _finalize(raw)
        except Exception as e:
            # 文件移动已经完成，不能因随后一次瞬时元数据读错把成功删除误报
            # 成 500。先从公开快照移除目标，保留 dirty 让下一轮恢复 Codex
            # 分叉继承等拓扑；源文件不会因用户重试而进一步受损。
            sessions = [row for row in _state["sessions"] if row.get("uid") != uid]
            print(f"[agenthub] 删除后的索引协调失败，稍后重试: {e}")
        # 不用移动后的新 inventory 给尚未协调的其他变化背书；下一次 load
        # 会从旧 files 做完整 diff，保留其他 Codex 分支和父项。
        _publish(raw, sessions, _state["files"], None, time.time(), 0.0, True)
        _search_cache_pop(uid)
    return str(dest)


_ANSI_T = re.compile(r"\x1b\[[0-9;]*m")
HIT_CAP = 200   # 单会话命中计数上限, 超过只报 "200+"
SEARCH_ROLES = frozenset({"user", "assistant", "user·subagent",
                          "assistant·subagent", "thinking", "question", "answer"})
SEARCH_CACHE_VERSION = 1
# 正文与其折叠副本一起计入(按 sys.getsizeof 的真实字节数)。每次搜索都会
# 触碰全部会话, 语料超出预算后 LRU 只会整体退化成逐条读盘, 所以预算要留
# 出语料增长的余量。
SEARCH_CACHE_MEMORY_BYTES = 96 * 1024 * 1024
# uid -> (stamp, 正文, 折叠副本, 上次校验时的 epoch)
_search_text_cache: OrderedDict[str, tuple[dict, str, str | None, tuple | None]] = OrderedDict()
_search_cache_bytes = 0
_search_text_lock = threading.Lock()
_search_parse_locks = [threading.Lock() for _ in range(16)]
# Codex 继承链: path -> 上次解析时链上各文件(含当前)的 _window_dependency。
# 这些 stat 字段没变就不必再读文件头。
_history_memo: dict[str, list[dict]] = {}
_history_memo_lock = threading.Lock()


def invalidate() -> None:
    """外部改动了会话文件(如从回收站恢复)后, 让下一次读重扫磁盘。

    清空已记录的 inventory 而不是只标 dirty: 恢复是把同一个 inode 原样搬
    回原路径, 与删除前的 files 逐字段相同, 只标 dirty 会得到空 diff, 被
    删除时从 raw 摘掉的会话就再也回不来了。
    """
    with _lock:
        if _state["initialized"]:
            _publish(_state["raw"], _state["sessions"], {}, None,
                     _state["built_at"], 0.0, True)


def build_pattern(query: str, word=False, case=False, regex=False) -> re.Pattern:
    """在解析后的 Unicode 对话正文上构造匹配模式。"""
    src = query if regex else re.escape(query)
    if word:
        src = r"(?<!\w)(?:" + src + r")(?!\w)"
    flags = 0 if case else re.I
    return re.compile(src, flags)


def _build_fold_table() -> dict[int, int] | None:
    """由 re 自己的忽略大小写等价表推出"非代表成员 -> 代表"的映射。

    sre 在 IGNORECASE 下把字符 c 与模式字符 q 视为相等, 当且仅当二者的简单
    小写相同, 或落在 re._casefix 列出的同一等价类里。这里给每个等价类挑一
    个代表(优先取 upper→lower 往返稳定的成员, 即普通文本里常见的那个),
    其余成员在折叠时改写成代表。表缺失时返回 None, 搜索退回纯正则路径。
    """
    try:
        from re import _casefix
        extra = _casefix._EXTRA_CASES
        tolower = _sre.unicode_tolower
    except (ImportError, AttributeError):
        return None
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    members: set[int] = set()
    for key, values in extra.items():
        members.add(key)
        for value in values:
            members.add(value)
            a, b = find(key), find(value)
            if a != b:
                parent[max(a, b)] = min(a, b)
    classes: dict[int, set[int]] = {}
    for m in members:
        classes.setdefault(find(m), set()).add(m)
    table: dict[int, int] = {}
    for cls in classes.values():
        stable = {m for m in cls if len(chr(m).upper()) == 1
                  and tolower(ord(chr(m).upper())) == m}
        rep = min(stable) if len(stable) == 1 else min(cls)
        for m in cls:
            if m != rep:
                table[m] = rep
    if any(tolower(m) != m for m in members):
        return None  # 等价表成员不再是各自的小写, 折叠推导不成立
    return table


_FOLD_TABLE = _build_fold_table()
_FOLD_SPECIAL = (re.compile("[" + "".join(re.escape(chr(c)) for c in _FOLD_TABLE) + "]")
                 if _FOLD_TABLE else None)


def _fold(text: str) -> str:
    """把文本逐字符映射到 re.IGNORECASE 的等价类代表, 长度与位置保持不变。

    str.lower() 与 sre 的简单小写映射只在 U+0130 上不同(它展开成两个字
    符), 先单独替换; Final_Sigma 上下文产生的 ς 与 σ 同类, 由表统一。
    对折叠后的正文做子串查找, 是原模式能命中的必要条件, 且命中位置一致。
    """
    if "\u0130" in text:
        text = text.replace("\u0130", "i")
    low = text.lower()
    if len(low) != len(text):
        # 将来 Unicode 若再添多字符小写映射, 退回逐字符简单映射, 只慢不错。
        low = "".join(map(chr, map(_sre.unicode_tolower, map(ord, text))))
    if _FOLD_SPECIAL.search(low):
        low = low.translate(_FOLD_TABLE)
    return low


def _required_literals(query: str, regex: bool) -> list[str]:
    """返回模式必然包含的折叠后字面量片段, 按长度降序; 无法判定时为空。

    非正则查询整个就是字面量。正则只取顶层拼接里连续的 LITERAL 节点:
    分支、分组、量词、断言一律截断片段, 只会漏掉可用的前置过滤, 不会误伤。
    """
    if _FOLD_TABLE is None:
        return []
    if not regex:
        return [_fold(query)]
    try:
        from re import _constants, _parser
        parsed = list(_parser.parse(query))
    except Exception:
        return []
    runs, run = [], []
    for op, av in parsed:
        if op is _constants.LITERAL:
            run.append(chr(av))
        elif run:
            runs.append("".join(run))
            run = []
    if run:
        runs.append("".join(run))
    return sorted((_fold(r) for r in runs), key=len, reverse=True)


def _search_text(s: dict) -> str:
    """返回与会话页语义一致的对话正文，并按文件版本缓存在内存中。

    原始 JSONL 还含工具协议、系统注入、compact 摘要和 JSON 包装，直接扫文件会
    产生大量用户在对话正文里看不到的假命中。
    """
    return _search_entry(s)[0]


def _search_entry(s: dict, epoch: tuple | None = None) -> tuple[str, str | None]:
    """返回 (正文, 折叠副本)。折叠副本为 None 时只能走纯正则路径。

    epoch 是本次搜索所依据的 (inventory 签名, 会话元数据文件身份)。内存
    条目上次就是在同一 epoch 下校验过的, 说明磁盘上没有任何会话文件、Codex
    索引或时间线元数据变化, 直接复用, 免去逐会话 stat 与 stamp 重建。
    """
    if epoch is not None:
        with _search_text_lock:
            cached = _search_text_cache.get(s["uid"])
            if cached and cached[3] == epoch:
                _search_text_cache.move_to_end(s["uid"])
                return cached[1], cached[2]
    identity = _window_cache_identity(s)
    # Concurrent searches share parsed text without a global scan lock.
    with _search_parse_locks[int(identity[:8], 16) % len(_search_parse_locks)]:
        return _cached_search_text(s, identity, epoch)


def _search_epoch() -> tuple[list[dict], tuple | None]:
    """返回搜索池与其磁盘身份; 签名不可用(dirty/未发布)时身份为 None, 逐条校验。"""
    sessions, sig, _ = load_snapshot()
    if not sig:
        return sessions, None
    return sessions, (sig, session_meta.file_identity())


def _search_cache_put(uid: str, key: dict, text: str, folded: str | None,
                      epoch: tuple | None) -> None:
    global _search_cache_bytes
    size = sys.getsizeof(text) + (sys.getsizeof(folded) if folded is not None else 0)
    with _search_text_lock:
        old = _search_text_cache.pop(uid, None)
        if old is not None:
            _search_cache_bytes -= _search_cache_entry_bytes(old)
        if not _search_text_cache:
            _search_cache_bytes = 0   # 测试直接 .clear() 后计数归零
        _search_text_cache[uid] = (key, text, folded, epoch)
        _search_cache_bytes += size
        while _search_cache_bytes > SEARCH_CACHE_MEMORY_BYTES and len(_search_text_cache) > 1:
            _, removed = _search_text_cache.popitem(last=False)
            _search_cache_bytes -= _search_cache_entry_bytes(removed)


def _search_cache_entry_bytes(entry: tuple) -> int:
    return sys.getsizeof(entry[1]) + (sys.getsizeof(entry[2]) if entry[2] is not None else 0)


def _search_cache_pop(uid: str) -> None:
    global _search_cache_bytes
    with _search_text_lock:
        old = _search_text_cache.pop(uid, None)
        if old is not None:
            _search_cache_bytes -= _search_cache_entry_bytes(old)


def _search_cache_clear() -> None:
    global _search_cache_bytes
    with _search_text_lock:
        _search_text_cache.clear()
        _search_cache_bytes = 0


def _search_disk_path(identity: str) -> Path:
    return CACHE_FILE.parent / "search-text" / f"{identity}.txt"


def _search_legacy_path(path: Path) -> Path:
    return path.with_name(path.name[:-len(".txt")] + ".json.gz")


def _search_disk_read(path: Path) -> tuple[dict, str] | None:
    """磁盘正文缓存: 第一行是 JSON stamp, 其后是未压缩的 UTF-8 正文。

    不压缩、不做 JSON 转义, 冷启动后首次搜索只付一次 read + decode。
    新格式缺失时回读旧的 gzip JSON 条目, 让升级后的首次搜索不必重建全部
    正文; 命中后由写入方换成新格式并删除旧文件。
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        head, _, body = blob.partition(b"\n")
        stamp = json.loads(head)
        if not isinstance(stamp, dict):
            return None
        return stamp, body.decode("utf-8")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return None
    try:
        with gzip.open(_search_legacy_path(path), "rt", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved.get("stamp"), dict) and isinstance(saved.get("text"), str):
            return saved["stamp"], saved["text"]
    except (OSError, ValueError, EOFError, AttributeError):
        pass
    return None


def _search_disk_write(path: Path, key: dict, text: str) -> None:
    temp = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(key, ensure_ascii=True).encode())
            fh.write(b"\n")
            fh.write(text.encode("utf-8"))
        os.replace(temp, path)
        # 旧格式(gzip JSON)同名条目已无用, 顺手清掉。
        legacy = _search_legacy_path(path)
        if legacy.exists():
            legacy.unlink()
    except OSError:
        pass  # Read-only/full cache storage must not prevent searching.
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def _seed_history_memo(s: dict, stamp: dict) -> None:
    """冷启动时用磁盘 stamp 里记录的继承链预填 memo; 真伪由随后的 stat 校验。"""
    deps = stamp.get("dependencies") if isinstance(stamp, dict) else None
    if not isinstance(deps, list) or not deps or not all(
            isinstance(d, dict) and {"path", "role", "limit"} <= d.keys() for d in deps):
        return
    if deps[-1].get("role") != "current" or deps[-1].get("path") != str(data_file(s)):
        return
    if any(d.get("role") != "history" for d in deps[:-1]):
        return
    with _history_memo_lock:
        _history_memo.setdefault(str(s["path"]), deps)


def _cached_search_text(s: dict, identity: str,
                        epoch: tuple | None = None) -> tuple[str, str | None]:
    ad = ADAPTERS[s["source"]]
    disk_path = _search_disk_path(identity)
    saved = None
    if s["source"] == "codex":
        with _history_memo_lock:
            seeded = str(s["path"]) in _history_memo
        if not seeded:
            # 进程内第一次见到这个 Codex 会话: 先读磁盘缓存, 用其中记录的继承
            # 链免去读文件头; 链上任一文件变了会在 stat 校验时被推翻。
            saved = _search_disk_read(disk_path)
            if saved is not None:
                _seed_history_memo(s, saved[0])
    key = {**_window_cache_stamp(s, ad), "search_schema": SEARCH_CACHE_VERSION}
    with _search_text_lock:
        cached = _search_text_cache.get(s["uid"])
        if cached and cached[0] == key:
            if epoch is not None and cached[3] != epoch:
                _search_text_cache[s["uid"]] = (key, cached[1], cached[2], epoch)
            _search_text_cache.move_to_end(s["uid"])
            return cached[1], cached[2]
    if saved is None:
        saved = _search_disk_read(disk_path)
    text = saved[1] if saved is not None and saved[0] == key else None
    if text is not None and not disk_path.exists():
        _search_disk_write(disk_path, key, text)   # 旧格式条目就地升级
    if text is None:
        if isinstance(ad, ClaudeAdapter):
            timeline = (session_meta.timeline(str(s.get("uid") or ""))
                        if not s.get("agent_id") else None)
            msgs, _ = ad.read(s["path"], agent=s.get("agent_id"), search_only=True,
                             declared_tip=_claude_effective_tip(s),
                             abandoned_after=int((timeline or {}).get("stale_end") or 0))
        else:
            opts = {"search_only": True} if s["source"] == "codex" else {}
            msgs, _ = ad.read(s["path"], **opts)
        text = "\n".join(m.get("text", "") for m in msgs
                         if m.get("role") in SEARCH_ROLES and m.get("text"))
        # Do not persist a parse under a version that changed while reading it.
        if key != {**_window_cache_stamp(s, ad), "search_schema": SEARCH_CACHE_VERSION}:
            return text, None
        _search_disk_write(disk_path, key, text)
    folded = _fold(text) if _FOLD_TABLE is not None else None
    _search_cache_put(s["uid"], key, text, folded, epoch)
    return text, folded


def _codex_dependencies(s: dict, ad) -> list[dict]:
    """Codex 正文依赖 = 继承的父文件前缀 + 当前文件, 顺序与 stamp 约定一致。

    继承链只由链上各文件的文件头决定; 上次记录的每个依赖的 stat 字段都没变,
    就直接复用, 否则重新读头解析。
    """
    path = str(s["path"])
    with _history_memo_lock:
        memo = _history_memo.get(path)
    if memo is not None:
        fresh = [_window_dependency(d["path"], d["role"], d["limit"]) for d in memo]
        if fresh == memo:
            return fresh
    deps = [_window_dependency(parent, "history", int(limit))
            for parent, limit in ad._history_segments(s["path"])]
    deps.append(_window_dependency(data_file(s), "current"))
    with _history_memo_lock:
        _history_memo[path] = deps
    return deps


def _scan_literal(pat: re.Pattern, hay: str, folded: str, needle: str):
    """以折叠副本上的子串定位候选位置, 再由原模式在原文上逐一确认。

    与 pat.finditer(hay) 的结果完全一致: 模式能在 p 命中则折叠副本在 p 处
    必然出现 needle; 确认失败从 p+1 继续, 成功从 m.end() 继续, 正是
    finditer 的推进规则。
    """
    pos, find, match = 0, folded.find, pat.match
    while True:
        p = find(needle, pos)
        if p < 0:
            return
        m = match(hay, p)
        if m is None:
            pos = p + 1
            continue
        yield m
        pos = m.end()


def search(query: str, sources=None, limit: int = 60,
           word=False, case=False, regex=False, progress=None, matches=None) -> dict:
    """按解析后的用户/助手/思考正文匹配，返回带命中片段的会话列表。"""
    if not query.strip():
        return {"results": [], "truncated": False, "total_pool": 0, "scanned": 0}
    pat = build_pattern(query, word, case, regex)
    needles = _required_literals(query, regex)
    hits, truncated, scanned = [], False, 0
    sessions, epoch = _search_epoch()
    pool = [s for s in sessions if not sources or s["source"] in sources]
    if progress:
        progress(0, len(pool))
    for done, s in enumerate(pool, 1):
        if len(hits) >= limit:
            truncated = True     # 还有没扫的会话, 结果不完整
            break
        snippet, count, capped = None, 0, False
        hay, folded = _search_entry(s, epoch)
        if folded is None or not needles:
            found = pat.finditer(hay)
        elif any(needle not in folded for needle in needles):
            found = ()           # 必要字面量都不在, 模式不可能命中
        elif regex:
            found = pat.finditer(hay)
        else:
            found = _scan_literal(pat, hay, folded, needles[0])
        for m in found:
            count += 1
            if snippet is None:
                # 命中词靠前, 否则片段在窄侧栏里会被右侧省略号吃掉
                lo, hi = max(0, m.start() - 40), m.end() + 150
                snippet = " ".join(_ANSI_T.sub("", hay[lo:hi]).split())
            if count >= HIT_CAP:
                capped = True
                break
        if count:
            hits.append({**s, "hits": count, "hits_capped": capped, "snippet": snippet})
            if matches:
                matches([hits[-1]])
        scanned = done
        if progress:
            progress(done, len(pool))
    hits.sort(key=lambda x: x["updated"], reverse=True)
    return {"results": hits, "truncated": truncated, "total_pool": len(pool), "scanned": scanned}
