"""三类 CLI 会话的存储适配器。

统一产出:
  元数据 dict: uid/source/sid/title/cwd/created/updated/size/path/model/extra
  消息 dict:   role/ts/text/name/args/output
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import media

HOME = Path.home()
CLAUDE_ROOT = HOME / ".claude" / "projects"
CODEX_ROOT = HOME / ".codex" / "sessions"
CODEX_INDEX = HOME / ".codex" / "session_index.jsonl"
GROK_ROOT = HOME / ".grok" / "sessions"

HEAD_BYTES = 96 * 1024  # 元数据只读文件头, 大文件不全量加载


def _uid(source: str, path: str) -> str:
    return f"{source}:{hashlib.sha1(path.encode()).hexdigest()[:16]}"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_ts(v) -> str | None:
    """把各家时间戳统一成本地时区 ISO 串。"""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return _iso(float(v) / (1000 if v > 1e11 else 1))
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")


def _head_lines(path: Path, limit: int = 40):
    """读文件头若干行并解析 JSON, 跳过坏行。"""
    out = []
    try:
        with open(path, "rb") as fh:
            blob = fh.read(HEAD_BYTES)
    except OSError:
        return out
    for raw in blob.split(b"\n")[:limit]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _iter_records(path, start: int = 0):
    """从字节偏移 start 起逐行解析 jsonl, yield (记录, 本行结束处的偏移)。

    偏移用于增量续读: 会话文件都是 append-only 的, 下次从上次的结束位置接着读即可。
    """
    off = start
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(start)
        for raw in fh:
            off += len(raw)
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            yield rec, off


def _clip(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s[:n] + ("…" if len(s) > n else "")


# 各家 CLI 都会把项目说明 / 环境信息伪装成 user 消息注入, 这些不能当标题
_INJECTED = re.compile(
    r"AGENTS\.md instructions|<INSTRUCTIONS>|<user_info>|<environment_context>|"
    r"<system-reminder>|<command-name>|Caveat: The messages below|"
    r"# Global User Guidance|<project_instructions>|<user_instructions>", re.I)


def _is_injected(text: str) -> bool:
    return bool(_INJECTED.search(text[:2000]))


def _title_from_text(text: str) -> str:
    """从首条用户消息里挑一个像标题的片段, 剥掉系统注入的包裹内容。"""
    text = re.sub(r"<(command-[a-z-]+|system-reminder|local-command[a-z-]*)>.*?</\1>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 4 and not line.startswith(("#", "-", "*", "```")):
            return _clip(line)
    return _clip(text) or "(无标题)"


def _flatten_content(content) -> list[dict]:
    """把 Anthropic/OpenAI 风格的 content 展开成 [{kind, text, ...}]。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"kind": "text", "text": content}]
    if isinstance(content, dict):
        content = [content]
    parts = []
    for it in content:
        if isinstance(it, str):
            parts.append({"kind": "text", "text": it})
            continue
        if not isinstance(it, dict):
            continue
        t = it.get("type", "")
        if t in ("text", "input_text", "output_text", "summary_text"):
            parts.append({"kind": "text", "text": it.get("text", "")})
        elif t == "thinking":
            parts.append({"kind": "thinking", "text": it.get("thinking", "")})
        elif t == "tool_use":
            parts.append({
                "kind": "tool", "name": it.get("name", "tool"),
                "text": json.dumps(it.get("input", {}), ensure_ascii=False, indent=2),
            })
        elif t == "tool_result":
            nested = _flatten_content(it.get("content"))
            images = [p["media"] for p in nested if p.get("media")]
            texts = [p["text"] for p in nested if p.get("kind") != "image" and p.get("text")]
            parts.append({"kind": "tool_result", "text": "\n".join(texts) or "[图片]",
                          "media": images})
        elif t in ("image", "input_image") or "image_url" in it:
            image = media.from_block(it)
            parts.append({"kind": "image", "text": "[图片]", "media": image})
    return parts


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(_stringify(x) for x in v)
    if isinstance(v, dict):
        if v.get("type") in ("image", "input_image") or "image_url" in v:
            return "[图片]"
        if "text" in v:
            return _stringify(v["text"])
        return json.dumps(v, ensure_ascii=False, indent=2)
    return str(v)


def _msg(role, text="", ts=None, name=None, args=None, media_parts=None):
    out = {"role": role, "text": text, "ts": ts, "name": name, "args": args}
    images = [x for x in (media_parts or []) if x]
    if images:
        out["media"] = images
    return out


def _user_role(role: str, text: str) -> str:
    """CLI 注入的项目说明 / 环境信息也是 user 消息, 单列一类以便默认折叠。"""
    if role.startswith("user") and _is_injected(text):
        return "context"
    return role


# --------------------------------------------------------------------------
# Claude Code:  ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
# --------------------------------------------------------------------------

class ClaudeAdapter:
    source = "claude"

    def list_sessions(self):
        if not CLAUDE_ROOT.is_dir():
            return []
        out = []
        for proj in sorted(CLAUDE_ROOT.iterdir()):
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                out.append(self._meta(f, st, proj.name))
        return out

    def _meta(self, f: Path, st, proj_name: str):
        title = cwd = branch = created = sid = None
        first_user = None
        for rec in _head_lines(f):
            t = rec.get("type")
            if t == "ai-title" and not title:
                title = rec.get("aiTitle")
            if not cwd and rec.get("cwd"):
                cwd = rec["cwd"]
            if not branch and rec.get("gitBranch"):
                branch = rec["gitBranch"]
            if not created and rec.get("timestamp"):
                created = _norm_ts(rec["timestamp"])
            if not sid and rec.get("sessionId"):
                sid = rec["sessionId"]
            if t == "user" and first_user is None and not rec.get("isSidechain"):
                parts = _flatten_content((rec.get("message") or {}).get("content"))
                txt = "\n".join(p["text"] for p in parts if p["kind"] == "text")
                if txt.strip() and not _is_injected(txt):
                    first_user = txt
        if not title:
            title = _title_from_text(first_user) if first_user else f.stem[:8]
        if not cwd:
            cwd = "/" + proj_name.lstrip("-").replace("-", "/")
        agents = len(list((f.parent / f.stem / "subagents").glob("*.jsonl")))
        return {
            "uid": _uid("claude", str(f)), "source": "claude", "sid": sid or f.stem,
            "title": title, "cwd": cwd, "created": created or _iso(st.st_mtime),
            "updated": _iso(st.st_mtime), "size": st.st_size, "path": str(f),
            "model": None, "branch": branch, "agents": agents,
        }

    def read(self, path: str, include_agents: bool = False, start: int = 0):
        msgs, end = self._read_one(path, start=start)
        if include_agents:
            sub = Path(path).parent / Path(path).stem / "subagents"
            for af in sorted(sub.glob("*.jsonl")):
                msgs.extend(self._read_one(str(af), agent=af.stem.replace("agent-", "")[:8])[0])
            msgs.sort(key=lambda m: m["ts"] or "")
        return msgs, end

    def _read_one(self, path: str, agent: str | None = None, start: int = 0):
        msgs, end = [], start
        for rec, off in _iter_records(path, start):
            end = off
            t = rec.get("type")
            ts = _norm_ts(rec.get("timestamp"))
            tag = agent or (rec.get("agentId", "")[:8] if rec.get("isSidechain") else None)
            if t in ("user", "assistant"):
                role = f"{t}·subagent" if tag else t
                for p in _flatten_content((rec.get("message") or {}).get("content")):
                    if not str(p.get("text", "")).strip() and not p.get("media"):
                        continue
                    if p["kind"] == "text":
                        msgs.append(_msg(_user_role(role, p["text"]), p["text"], ts, name=tag))
                    elif p["kind"] == "image":
                        msgs.append(_msg(role, p["text"], ts, name=tag,
                                         media_parts=[p.get("media")]))
                    elif p["kind"] == "thinking":
                        msgs.append(_msg("thinking", p["text"], ts, name=tag))
                    elif p["kind"] == "tool":
                        msgs.append(_msg("tool", p["text"], ts, name=p["name"]))
                    elif p["kind"] == "tool_result":
                        msgs.append(_msg("tool_result", p["text"], ts,
                                         media_parts=p.get("media")))
            elif t == "system" and rec.get("content"):
                msgs.append(_msg("system", _stringify(rec["content"]), ts))
        return msgs, end


# --------------------------------------------------------------------------
# Codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
# --------------------------------------------------------------------------

class CodexAdapter:
    source = "codex"

    def __init__(self):
        self._names = None

    def _thread_names(self):
        if self._names is None:
            self._names = {}
            if CODEX_INDEX.exists():
                with open(CODEX_INDEX, "r", errors="replace") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("id") and r.get("thread_name"):
                            self._names[r["id"]] = r["thread_name"]
        return self._names

    def list_sessions(self):
        if not CODEX_ROOT.is_dir():
            return []
        names = self._thread_names()
        out = []
        for f in CODEX_ROOT.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            meta, first_user, model = {}, None, None
            for rec in _head_lines(f, 120):
                p = rec.get("payload") or {}
                if rec.get("type") == "session_meta" and not meta:
                    meta = p
                if rec.get("type") == "turn_context" and not model:
                    model = p.get("model")
                if first_user is None and rec.get("type") == "response_item" \
                        and p.get("type") == "message" and p.get("role") == "user":
                    txt = "\n".join(x["text"] for x in _flatten_content(p.get("content")) if x["kind"] == "text")
                    if txt.strip() and not _is_injected(txt):
                        first_user = txt
            sid = meta.get("session_id") or meta.get("id") or f.stem
            title = names.get(sid) or (_title_from_text(first_user) if first_user
                                       else "(无标题) " + f.stem.replace("rollout-", "")[:16])
            out.append({
                "uid": _uid("codex", str(f)), "source": "codex", "sid": sid,
                "title": _clip(title, 110), "cwd": meta.get("cwd") or "(未知)",
                "created": _norm_ts(meta.get("timestamp")) or _iso(st.st_mtime),
                "updated": _iso(st.st_mtime), "size": st.st_size, "path": str(f),
                "model": model, "branch": None,
            })
        return out

    def read(self, path: str, start: int = 0):
        msgs, calls, end = [], {}, start
        for rec, off in _iter_records(path, start):
            end = off
            ts = _norm_ts(rec.get("timestamp"))
            p = rec.get("payload") or {}
            if rec.get("type") != "response_item":
                continue
            k = p.get("type")
            if k == "message":
                role = p.get("role") or "user"
                role = {"developer": "system", "tool": "tool_result"}.get(role, role)
                parts = _flatten_content(p.get("content"))
                txt = "\n".join(x["text"] for x in parts if x["kind"] == "text")
                images = [x["media"] for x in parts if x.get("media")]
                if txt.strip() or images:
                    shown = txt or "[图片]"
                    msgs.append(_msg(_user_role(role, shown), shown, ts, media_parts=images))
            elif k == "reasoning":
                txt = "\n".join(x["text"] for x in _flatten_content(p.get("summary")) if x["kind"] == "text")
                if txt.strip():
                    msgs.append(_msg("thinking", txt, ts))
            elif k in ("function_call", "custom_tool_call", "local_shell_call"):
                name = p.get("name") or k
                body = p.get("arguments") or p.get("input") or p.get("action") or ""
                calls[p.get("call_id")] = name
                msgs.append(_msg("tool", _pretty_json(body), ts, name=name))
            elif k in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                name = calls.get(p.get("call_id"))
                msgs.append(_msg("tool_result", _stringify(p.get("output")), ts, name=name))
            elif k in ("web_search_call", "tool_search_call"):
                msgs.append(_msg("tool", _pretty_json(p.get("arguments") or {}), ts, name=k))
        return msgs, end


def _pretty_json(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, indent=2)
    s = str(v)
    try:
        return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
    except Exception:
        return s


# --------------------------------------------------------------------------
# Grok:  ~/.grok/sessions/<urlencoded-cwd>/<uuid>/{summary.json,chat_history.jsonl}
# --------------------------------------------------------------------------

class GrokAdapter:
    source = "grok"

    def list_sessions(self):
        if not GROK_ROOT.is_dir():
            return []
        out = []
        for sj in GROK_ROOT.glob("*/*/summary.json"):
            sess_dir = sj.parent
            try:
                st = (sess_dir / "chat_history.jsonl").stat()
            except OSError:
                st = sj.stat()  # 会话尚未落盘聊天记录时退回 summary
            try:
                info = json.loads(sj.read_text(errors="replace"))
            except Exception:
                info = {}
            base = info.get("info") or {}
            title = info.get("generated_title") or info.get("session_summary") or sess_dir.name[:8]
            out.append({
                "uid": _uid("grok", str(sess_dir)), "source": "grok",
                "sid": base.get("id") or sess_dir.name, "title": _clip(title, 110),
                "cwd": base.get("cwd") or _unquote_cwd(sess_dir.parent.name),
                "created": _norm_ts(info.get("created_at")) or _iso(st.st_mtime),
                "updated": _norm_ts(info.get("last_active_at") or info.get("updated_at")) or _iso(st.st_mtime),
                "size": _dir_size(sess_dir), "path": str(sess_dir),
                "model": info.get("current_model_id"), "branch": info.get("agent_name"),
            })
        return out

    def read(self, path: str, start: int = 0):
        chat = Path(path) / "chat_history.jsonl"
        msgs, calls, end = [], {}, start
        if not chat.is_file():
            return msgs, end
        for rec, off in _iter_records(chat, start):
            end = off
            t = rec.get("type")
            if t == "reasoning":
                txt = "\n".join(x["text"] for x in _flatten_content(rec.get("summary")) if x["kind"] == "text")
                if txt.strip():
                    msgs.append(_msg("thinking", txt))
            elif t == "tool_result":
                parts = _flatten_content(rec.get("content"))
                txt = "\n".join(x["text"] for x in parts if x.get("kind") != "image" and x.get("text"))
                images = [x["media"] for x in parts if x.get("media")]
                msgs.append(_msg("tool_result", txt or "[图片]",
                                 name=calls.get(rec.get("tool_call_id")), media_parts=images))
            elif t in ("user", "assistant", "system"):
                parts = _flatten_content(rec.get("content"))
                txt = "\n".join(x["text"] for x in parts if x["kind"] == "text")
                images = [x["media"] for x in parts if x.get("media")]
                if txt.strip() or images:
                    shown = txt or "[图片]"
                    role = "context" if rec.get("synthetic_reason") else _user_role(t, shown)
                    msgs.append(_msg(role, shown, media_parts=images))
                for tc in rec.get("tool_calls") or []:
                    name = tc.get("name") or (tc.get("function") or {}).get("name") or "tool"
                    args = tc.get("arguments") or (tc.get("function") or {}).get("arguments") or ""
                    calls[tc.get("id")] = name
                    msgs.append(_msg("tool", _pretty_json(args), name=name))
        return msgs, end


def _unquote_cwd(name: str) -> str:
    from urllib.parse import unquote
    return unquote(name)


def _dir_size(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


ADAPTERS = {a.source: a for a in (ClaudeAdapter(), CodexAdapter(), GrokAdapter())}
