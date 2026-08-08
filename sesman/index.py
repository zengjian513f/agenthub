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
TRASH_DIR = Path.home() / ".local" / "share" / "sesman" / "trash"

_lock = threading.Lock()
_state = {"sessions": [], "built_at": 0.0, "sig": None}


def signature() -> str:
    """当前磁盘状态的签名。全量 stat 约 3ms, 便宜到可以让前端轮询。"""
    return _signature()


def _signature() -> str:
    """所有会话文件的 (path, mtime, size) 摘要, 用来判断缓存是否过期。"""
    import hashlib
    h = hashlib.sha1()
    from .adapters import CLAUDE_ROOT, CODEX_ROOT, CODEX_INDEX, GROK_ROOT
    roots = [(CLAUDE_ROOT, "*/*.jsonl"), (CODEX_ROOT, "**/*.jsonl"),
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
                if cached.get("sig") == sig:
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
                {"sig": sig, "built_at": _state["built_at"], "sessions": sessions}, ensure_ascii=False))
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


def version(s: dict) -> dict:
    """用 (大小, mtime, 文件头哈希) 标识一个版本。

    会话是 append-only 的, 所以 "头部不变 + 变大" 就能安全地从旧偏移续读;
    头哈希变了或文件缩小, 说明被重写/回退过, 必须整份重来。
    """
    import hashlib
    f = data_file(s)
    try:
        st = f.stat()
        with open(f, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return {"size": 0, "mtime": 0, "head": ""}
    return {"size": st.st_size, "mtime": int(st.st_mtime * 1000),
            "head": hashlib.sha1(head).hexdigest()[:16]}


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


def messages(uid: str, include_agents: bool = False,
             start: int = 0, head: str = "", anchor: str = "") -> dict:
    s = get(uid)
    if not s:
        raise KeyError(uid)
    return messages_for(s, include_agents, start, head, anchor)


def messages_for(s: dict, include_agents: bool = False,
                 start: int = 0, head: str = "", anchor: str = "") -> dict:
    """整份或增量读取。传的是会话元数据而不是 uid —— SSE 那边每 50ms 要调一次,
    走 uid 的话每次都会连带重算索引签名(203 次 stat)甚至重建整个索引。

    能接着上次读的条件: 文件头没变、文件没缩短、而且**偏移点之前的内容也没变**。
    最后一条是必需的 —— 会话可以被回滚(双 Esc)截断后再写新内容, 那时文件头照旧、
    长度也可能重新超过旧偏移, 只看头和长度会从旧偏移读到一段完全不同的内容。
    """
    ver = version(s)
    ok = bool(start and head and head == ver["head"] and start <= ver["size"])
    if ok and anchor:
        ok = anchor == _anchor_hash(data_file(s), start)
    elif ok and not anchor:
        ok = False                       # 没带锚点就不给续读, 宁可重来
    reset = not ok
    if reset:
        start = 0
    ad = ADAPTERS[s["source"]]
    if include_agents and isinstance(ad, ClaudeAdapter):
        msgs, end = ad.read(s["path"], include_agents=True)
        reset, start = True, 0          # 合并子代理时按整份处理
    else:
        msgs, end = ad.read(s["path"], start=start)
    for msg in msgs:
        media.enrich_message(msg, s.get("cwd"))
    return {"meta": s, "version": ver, "reset": reset, "start": start, "end": end,
            "anchor": _anchor_hash(data_file(s), end), "messages": msgs}


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
                          "assistant·subagent", "thinking"})
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


def to_markdown(uid: str) -> str:
    d = messages(uid)
    s, msgs = d["meta"], d["messages"]
    lines = [f"# {s['title']}", "",
             f"- 来源: {s['source']}", f"- 会话 ID: {s['sid']}", f"- 目录: {s['cwd']}",
             f"- 创建: {s['created']}", f"- 更新: {s['updated']}",
             f"- 文件: {s['path']}", "", "---", ""]
    for m in msgs:
        head = m["role"] + (f" · {m['name']}" if m.get("name") else "")
        lines.append(f"### {head}")
        if m["role"] in ("tool", "tool_result"):
            lines += ["```", m["text"][:20000], "```", ""]
        else:
            lines += [m["text"], ""]
    return "\n".join(lines)
