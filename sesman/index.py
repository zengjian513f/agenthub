"""会话索引: 枚举 + 磁盘缓存 + 全文搜索。"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .adapters import ADAPTERS, ClaudeAdapter
from . import media

CACHE_DIR = Path.home() / ".cache" / "sesman"
CACHE_FILE = CACHE_DIR / "index.json"
CACHE_VERSION = 2
TRASH_DIR = Path.home() / ".local" / "share" / "sesman" / "trash"

_lock = threading.Lock()
_state = {"sessions": [], "built_at": 0.0, "sig": None}


def signature() -> str:
    """当前磁盘状态的签名。全量 stat 为个位数毫秒, 可供前端低频轮询。"""
    return _signature()


def _signature() -> str:
    """所有会话文件的 (path, mtime, size) 摘要, 用来判断缓存是否过期。"""
    import hashlib
    h = hashlib.sha1()
    from .adapters import CLAUDE_ROOT, CODEX_ROOT, CODEX_INDEX, GROK_ROOT
    roots = [(CLAUDE_ROOT, "*/*.jsonl"),
             (CLAUDE_ROOT, "*/*/subagents/*.jsonl"),
             (CLAUDE_ROOT, "*/*/subagents/*.meta.json"),
             (CODEX_ROOT, "**/*.jsonl"),
             (GROK_ROOT, "*/*/summary.json"), (GROK_ROOT, "*/*/chat_history.jsonl")]
    for root, pat in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.glob(pat)):
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f}|{int(st.st_mtime)}|{st.st_size}\n".encode())
    try:
        st = CODEX_INDEX.stat()
        h.update(f"{CODEX_INDEX}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    except OSError:
        pass
    return h.hexdigest()


def _build() -> list[dict]:
    out = []
    for name, ad in ADAPTERS.items():
        try:
            out.extend(ad.list_sessions())
        except Exception as e:  # 单一来源异常不应拖垮整个索引
            print(f"[sesman] {name} 扫描失败: {e}")
    out.sort(key=lambda s: s["updated"], reverse=True)
    return out


def load(force: bool = False) -> list[dict]:
    with _lock:
        sig = _signature()
        if not force and _state["sessions"] and _state["sig"] == sig:
            return _state["sessions"]
        if not force and not _state["sessions"] and CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
                if cached.get("version") == CACHE_VERSION and cached.get("sig") == sig:
                    _state.update(sessions=cached["sessions"], sig=sig, built_at=cached.get("built_at", 0))
                    return _state["sessions"]
            except Exception:
                pass
        t0 = time.time()
        sessions = _build()
        _state.update(sessions=sessions, sig=sig, built_at=time.time())
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CACHE_FILE.write_text(json.dumps(
                {"version": CACHE_VERSION, "sig": sig,
                 "built_at": _state["built_at"], "sessions": sessions}, ensure_ascii=False))
        except OSError:
            pass
        print(f"[sesman] 索引重建: {len(sessions)} 个会话, {time.time() - t0:.1f}s")
        return sessions


def get(uid: str) -> dict | None:
    for s in load():
        if s["uid"] == uid:
            return s
    return None


def data_file(s: dict) -> Path:
    """会话真正的数据文件 (grok 的 path 是目录)。"""
    p = Path(s["path"])
    return p / "chat_history.jsonl" if s["source"] == "grok" else p


def _head_hash(f: Path, limit: int = 4096) -> str:
    import hashlib
    try:
        with open(f, "rb") as fh:
            return hashlib.sha1(fh.read(max(0, limit))).hexdigest()[:16]
    except OSError:
        return ""


def version(s: dict) -> dict:
    """用 (大小, mtime, 文件头哈希) 标识一个版本。

    会话通常是 append-only 的；续读时用旧 EOF 所对应的固定长度前缀和尾部
    锚点确认旧内容没变。任一校验失败都说明可能被重写/回退，必须整份重来。
    """
    f = data_file(s)
    try:
        st = f.stat()
    except OSError:
        return {"size": 0, "mtime": 0, "head": ""}
    return {"size": st.st_size, "mtime": int(st.st_mtime * 1000),
            "head": _head_hash(f)}


ANCHOR = 512      # 续读前校验偏移点之前这么多字节的内容


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


def cursor(s: dict) -> dict:
    """供浏览器低成本跟踪后台会话追加内容的安全续读游标。"""
    ver = version(s)
    return {"end": ver["size"], "head": ver["head"],
            "anchor": _anchor_hash(data_file(s), ver["size"])}


def with_cursors(sessions: list[dict]) -> list[dict]:
    """给列表元数据附加主会话及 Claude 子代理的 EOF 游标。

    这里只读取每个文件头 4 KiB 和尾部 512 B；浏览器随后只拉变化文件的
    新增区间，不需要为了左栏未读数下载整份历史。
    """
    out = []
    for session in sessions:
        row = {**session, "cursor": cursor(session)}
        items = []
        for item in session.get("agent_items") or []:
            copy = dict(item)
            try:
                copy["cursor"] = cursor(session_view(session, str(item.get("id") or "")))
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
    if s.get("source") != "claude":
        raise KeyError(agent)
    item = next((x for x in s.get("agent_items", []) if x.get("id") == agent), None)
    if not item:
        raise KeyError(agent)
    path = Path(s["path"]).parent / Path(s["path"]).stem / "subagents" / f"agent-{agent}.jsonl"
    if not path.is_file():
        raise KeyError(agent)
    return {**s, "path": str(path), "sid": agent, "title": item["title"],
            "size": item.get("size", path.stat().st_size),
            "updated": item.get("updated", s["updated"]),
            "agent_id": agent, "agent_type": item.get("type", "subagent"),
            "parent_title": s["title"]}


def messages(uid: str, agent: str = "",
             start: int = 0, head: str = "", anchor: str = "",
             append_only: bool = False) -> dict:
    s = get(uid)
    if not s:
        raise KeyError(uid)
    return messages_for(session_view(s, agent), start, head, anchor, append_only)


def messages_for(s: dict, start: int = 0, head: str = "", anchor: str = "",
                 append_only: bool = False) -> dict:
    """整份或增量读取。传的是会话元数据而不是 uid —— SSE 那边每 50ms 要调一次,
    走 uid 的话每次都会连带重算索引签名(数百次 stat)甚至重建整个索引。

    能接着上次读的条件: 文件头没变、文件没缩短、而且**偏移点之前的内容也没变**。
    最后一条是必需的 —— 会话可以被回滚(双 Esc)截断后再写新内容, 那时文件头照旧、
    长度也可能重新超过旧偏移, 只看头和长度会从旧偏移读到一段完全不同的内容。
    """
    ver = version(s)
    # 小于 4 KiB 的新会话追加后，当前 head 会自然变长、哈希也会变化；应当
    # 用旧 EOF 所确定的同长度前缀校验，而不是把正常追加误判成历史改写。
    ok = bool(start and head and start <= ver["size"]
              and head == _head_hash(data_file(s), min(4096, start)))
    if ok and anchor:
        ok = anchor == _anchor_hash(data_file(s), start)
    elif ok and not anchor:
        ok = False                       # 没带锚点就不给续读, 宁可重来
    reset = not ok
    if reset and append_only:
        # 后台未读探测绝不能因回滚/重写退化成几十 MB 的整份下载；让调用方
        # 丢弃旧缓存并以当前 EOF 重新建立基线即可。
        end = ver["size"]
        return {"meta": s, "version": ver, "reset": True, "start": end, "end": end,
                "anchor": _anchor_hash(data_file(s), end), "messages": [],
                "activity_changed": False, "activity": None}
    if reset:
        start = 0
    ad = ADAPTERS[s["source"]]
    if isinstance(ad, ClaudeAdapter) and s.get("agent_id"):
        msgs, end = ad.read(s["path"], start=start, agent=s["agent_id"])
    else:
        msgs, end = ad.read(s["path"], start=start)
    activity_events = [m for m in msgs if m.get("role") == "status"]
    msgs = [m for m in msgs if m.get("role") != "status"]
    for msg in msgs:
        media.enrich_message(msg, s.get("cwd"))
    return {"meta": s, "version": ver, "reset": reset, "start": start, "end": end,
            "anchor": _anchor_hash(data_file(s), end), "messages": msgs,
            "activity_changed": bool(activity_events),
            "activity": activity_events[-1] if activity_events else None}


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
    shutil.move(str(src), str(dest))
    with _lock:
        _state["sessions"] = [x for x in _state["sessions"] if x["uid"] != uid]
        _state["sig"] = None
    return str(dest)


_ANSI_T = re.compile(r"\x1b\[[0-9;]*m")
HIT_CAP = 200   # 单会话命中计数上限, 超过只报 "200+"
SEARCH_ROLES = frozenset({"user", "assistant", "user·subagent",
                          "assistant·subagent", "thinking", "question", "answer"})
_search_text_cache = {}
_search_text_lock = threading.Lock()


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
    f = data_file(s)
    try:
        st = f.stat()
        key = (str(f), st.st_size, st.st_mtime_ns)
    except OSError:
        return ""
    with _search_text_lock:
        cached = _search_text_cache.get(s["uid"])
        if cached and cached[0] == key:
            return cached[1]
    try:
        msgs, _ = ADAPTERS[s["source"]].read(s["path"])
    except Exception:
        return ""
    text = "\n".join(m.get("text", "") for m in msgs
                     if m.get("role") in SEARCH_ROLES and m.get("text"))
    with _search_text_lock:
        _search_text_cache[s["uid"]] = (key, text)
    return text


def search(query: str, sources=None, limit: int = 60,
           word=False, case=False, regex=False, progress=None) -> dict:
    """按解析后的用户/助手/思考正文匹配，返回带命中片段的会话列表。"""
    if not query.strip():
        return {"results": [], "truncated": False, "total_pool": 0}
    pat = build_pattern(query, word, case, regex)
    hits, truncated = [], False
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
        if progress:
            progress(done, len(pool))
    hits.sort(key=lambda x: x["updated"], reverse=True)
    return {"results": hits, "truncated": truncated, "total_pool": len(pool)}
