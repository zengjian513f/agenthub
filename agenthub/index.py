"""会话索引: 枚举 + 磁盘缓存 + 全文搜索。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .adapters import ADAPTERS, ClaudeAdapter
from . import audit, media, session_meta, trash

CACHE_DIR = Path.home() / ".cache" / "agenthub"
CACHE_FILE = CACHE_DIR / "index.json"
CACHE_VERSION = 7  # 子代理项新增 created/active
WINDOW_CACHE_DIR = CACHE_DIR / "message-windows"
WINDOW_CACHE_VERSION = 8
MESSAGE_CURSOR_VERSION = 8
WINDOW_CACHE_MIN_BYTES = 8 * 1024 * 1024
WINDOW_CACHE_MEMORY_ITEMS = 16
TRASH_DIR = Path.home() / ".local" / "share" / "agenthub" / "trash"
CHECK_TTL = 0.5       # 高频热路径复用已发布快照；列表轮询仍会及时发现磁盘变化

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


def _inventory() -> dict[str, tuple[str, str, int, int, int]]:
    """枚举索引依赖，但不解析会话正文。子文件同时记录其主会话 owner。"""
    from .adapters import CLAUDE_ROOT, CODEX_ROOT, CODEX_INDEX, GROK_ROOT

    files = {}

    def add(path: Path, kind: str, owner: Path | str):
        try:
            st = path.stat()
        except OSError:
            return
        files[str(path)] = (kind, str(owner), st.st_size, st.st_mtime_ns,
                            int(getattr(st, "st_ino", 0)))

    if CLAUDE_ROOT.is_dir():
        for f in CLAUDE_ROOT.glob("*/*.jsonl"):
            add(f, "claude-main", f)
        for f in CLAUDE_ROOT.glob("*/*/subagents/*.jsonl"):
            session_dir = f.parent.parent
            add(f, "claude-agent", session_dir.parent / f"{session_dir.name}.jsonl")
        for f in CLAUDE_ROOT.glob("*/*/subagents/*.meta.json"):
            session_dir = f.parent.parent
            add(f, "claude-agent-meta",
                session_dir.parent / f"{session_dir.name}.jsonl")
    if CODEX_ROOT.is_dir():
        for f in CODEX_ROOT.glob("**/*.jsonl"):
            add(f, "codex-main", f)
    if GROK_ROOT.is_dir():
        for f in GROK_ROOT.glob("*/*/summary.json"):
            add(f, "grok-summary", f.parent)
        for f in GROK_ROOT.glob("*/*/chat_history.jsonl"):
            add(f, "grok-chat", f.parent)
    if CODEX_INDEX.exists():
        add(CODEX_INDEX, "codex-index", "")
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
    }


def signature() -> str:
    """返回已发布列表对应的签名，不在普通读请求里偷偷扫描磁盘。"""
    cached()
    return str(_state["sig"] or "")


def load(force: bool = False) -> list[dict]:
    """扫描 inventory 并增量协调；同一时刻只允许一个刷新者。"""
    with _lock:
        now = time.monotonic()
        if (not force and _state["initialized"]
                and _state["checked_at"] > 0
                and now - _state["checked_at"] <= CHECK_TTL):
            return _state["sessions"]

        files = _inventory()
        if _state["initialized"]:
            _audit_inventory_changes(_state["files"], files)
        sig = _signature(files)
        if (not force and _state["initialized"] and not _state["dirty"]
                and _state["sig"] == sig):
            _publish(_state["raw"], _state["sessions"], files, sig,
                     _state["built_at"], time.monotonic(), False)
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
                        _write_cache(raw, sessions, files, sig, built_at)
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
            _write_cache(raw, sessions, files, sig, built_at)
        if full:
            print(f"[agenthub] 索引重建: {len(sessions)} 个会话, {time.time() - t0:.1f}s")
        return _state["sessions"]


def load_snapshot(force: bool = False) -> tuple[list[dict], str, float]:
    """原子取得同一发布版本的列表、签名和构建时间。"""
    while True:
        sessions = load(force=force)
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


def _window_cache_identity(s: dict) -> str:
    """缓存文件名只暴露散列，不把会话路径或 uid 写进目录项。"""
    raw = "\0".join((str(s.get("source") or ""), str(data_file(s)),
                     str(s.get("agent_id") or "")))
    return hashlib.sha256(raw.encode()).hexdigest()


def _window_dependency(path: str | Path, role: str,
                       limit: int | None = None) -> dict:
    """记录能识别内部改写、截断和路径替换的文件身份。"""
    path = Path(path)
    row = {"role": role, "path": str(path), "limit": limit}
    try:
        st = path.stat()
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
    dependencies = []
    if s.get("source") == "codex":
        for parent, limit in ad._history_segments(s["path"]):
            dependencies.append(_window_dependency(parent, "history", int(limit)))
    dependencies.append(_window_dependency(data_file(s), "current"))

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
        return copy.deepcopy(cached[1])


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
    return row["value"]


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


def _cursor_stamp(s: dict) -> tuple:
    """游标缓存版本；ctime/inode 补上同尺寸重写和路径复用的边界。"""
    f = data_file(s)
    try:
        st = f.stat()
    except OSError:
        return (str(f), 0, 0, 0, 0,
                session_meta.timeline_revision(s.get("uid", "")))
    return (str(f), st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            int(getattr(st, "st_ino", 0)),
            session_meta.timeline_revision(s.get("uid", "")))


def _cached_cursor(s: dict) -> dict:
    """按文件版本复用游标；同一新版本并发到达时只允许一次重读。"""
    cache_id = (str(s.get("source") or ""), str(data_file(s)),
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


def with_cursors(sessions: list[dict]) -> list[dict]:
    """给列表元数据附加主会话及子代理的 EOF 游标。

    这里只读取每个文件头 4 KiB、尾部锚点和 Claude 最后一条树记录；浏览器
    随后只拉变化文件的新增区间，不需要为了左栏未读数下载整份历史。
    """
    out = []
    for session in sessions:
        row = {**session, "cursor": _cached_cursor(session)}
        items = []
        for item in session.get("agent_items") or []:
            copy = dict(item)
            try:
                copy["cursor"] = _cached_cursor(
                    session_view(session, str(item.get("id") or "")))
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
    roles: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
    audit.record(
        "jsonl.batch.parsed", category="parser",
        uid=str(s.get("uid") or ""), source=str(s.get("source") or ""),
        data={
            "path": str(data_file(s)), "agent": str(s.get("agent_id") or ""),
            "requested": requested, "reset": bool(result.get("reset")),
            "start": result.get("start"), "end": result.get("end"),
            "version": result.get("version"), "anchor": result.get("anchor"),
            "message_count": len(messages), "message_total": result.get("message_total"),
            "roles": roles, "partial": result.get("partial"),
            "activity_changed": bool(result.get("activity_changed")),
        },
        content={"messages": messages, "activity": result.get("activity")},
    )


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
            s, ad, lambda: _read_message_batch(s, ad, 0, ver, True))
    else:
        batch = _read_message_batch(s, ad, start, ver, windowed and reset)
    msgs = batch["messages"]
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
        with _search_text_lock:
            _search_text_cache.pop(uid, None)
    return str(dest)


_ANSI_T = re.compile(r"\x1b\[[0-9;]*m")
HIT_CAP = 200   # 单会话命中计数上限, 超过只报 "200+"
SEARCH_ROLES = frozenset({"user", "assistant", "user·subagent",
                          "assistant·subagent", "thinking", "question", "answer"})
SEARCH_CACHE_VERSION = 1
SEARCH_CACHE_MEMORY_BYTES = 64 * 1024 * 1024
_search_text_cache = OrderedDict()
_search_text_lock = threading.Lock()
_search_parse_locks = [threading.Lock() for _ in range(16)]


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


def _search_text(s: dict) -> str:
    """返回与会话页语义一致的对话正文，并按文件版本缓存在内存中。

    原始 JSONL 还含工具协议、系统注入、compact 摘要和 JSON 包装，直接扫文件会
    产生大量用户在对话正文里看不到的假命中。
    """
    identity = _window_cache_identity(s)
    # Concurrent searches share parsed text without a global scan lock.
    with _search_parse_locks[int(identity[:8], 16) % len(_search_parse_locks)]:
        return _cached_search_text(s, identity)


def _cached_search_text(s: dict, identity: str) -> str:
    ad = ADAPTERS[s["source"]]
    key = {**_window_cache_stamp(s, ad), "search_schema": SEARCH_CACHE_VERSION}
    path = CACHE_FILE.parent / "search-text" / f"{identity}.json.gz"
    with _search_text_lock:
        cached = _search_text_cache.get(s["uid"])
        if cached and cached[0] == key:
            _search_text_cache.move_to_end(s["uid"])
            return cached[1]
    text = None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("stamp") == key and isinstance(saved.get("text"), str):
            text = saved["text"]
    except (OSError, ValueError, EOFError, AttributeError):
        pass
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
            return text
        temp = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as fh:
                    fh.write(json.dumps({"stamp": key, "text": text},
                                        ensure_ascii=False).encode())
            os.replace(temp, path)
        except OSError:
            pass  # Read-only/full cache storage must not prevent searching.
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
    with _search_text_lock:
        _search_text_cache[s["uid"]] = (key, text)
        _search_text_cache.move_to_end(s["uid"])
        size = sum(len(value[1]) * 4 for value in _search_text_cache.values())
        while size > SEARCH_CACHE_MEMORY_BYTES and _search_text_cache:
            _, (_, removed) = _search_text_cache.popitem(last=False)
            size -= len(removed) * 4
    return text


def search(query: str, sources=None, limit: int = 60,
           word=False, case=False, regex=False, progress=None, matches=None) -> dict:
    """按解析后的用户/助手/思考正文匹配，返回带命中片段的会话列表。"""
    if not query.strip():
        return {"results": [], "truncated": False, "total_pool": 0, "scanned": 0}
    pat = build_pattern(query, word, case, regex)
    hits, truncated, scanned = [], False, 0
    pool = [s for s in load() if not sources or s["source"] in sources]
    if progress:
        progress(0, len(pool))
    for done, s in enumerate(pool, 1):
        if len(hits) >= limit:
            truncated = True     # 还有没扫的会话, 结果不完整
            break
        snippet, count, capped = None, 0, False
        hay = _search_text(s)
        for m in pat.finditer(hay):
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
