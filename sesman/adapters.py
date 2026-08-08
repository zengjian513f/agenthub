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
TAIL_BYTES = 512 * 1024  # rename/custom-title 通常追加在文件尾


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


def _tail_lines(path: Path):
    """解析文件尾部的完整 JSONL 记录，用于读取后追加的 rename 元数据。"""
    try:
        size = path.stat().st_size
        start = max(0, size - TAIL_BYTES)
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    lines = blob.split(b"\n")
    if start:
        lines = lines[1:]  # 尾读通常从一条大记录中间开始，丢掉残行
    out = []
    for raw in lines:
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


_QUESTION_TOOLS = {"askuserquestion", "request_user_input"}


def _is_question_tool(name: str | None) -> bool:
    key = str(name or "").lower()
    return key in _QUESTION_TOOLS or key.endswith(".request_user_input") \
        or key.endswith("__request_user_input")


def _json_value(v):
    """工具参数有时是 dict，有时是 JSON 字符串。"""
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def _question_message(name: str, value) -> dict | None:
    """把 Claude/Codex 的询问工具统一成可直接排版的问题结构。"""
    if not _is_question_tool(name):
        return None
    data = _json_value(value)
    if not isinstance(data, dict):
        return None
    rows = data.get("questions") or []
    if isinstance(rows, dict):
        rows = [rows]
    questions = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("question"):
            continue
        options = []
        for opt in row.get("options") or []:
            if isinstance(opt, str):
                options.append({"label": opt, "description": ""})
            elif isinstance(opt, dict) and opt.get("label"):
                options.append({"label": str(opt["label"]),
                                "description": str(opt.get("description") or "")})
        questions.append({
            "header": str(row.get("header") or ""),
            "question": str(row["question"]),
            "options": options,
            "multiple": bool(row.get("multiSelect") or row.get("multiple")),
        })
    if not questions:
        return None
    return {"text": "\n\n".join(q["question"] for q in questions),
            "questions": questions}


# 各家 CLI 都会把项目说明 / 环境信息伪装成 user 消息注入, 这些不能当标题
_INJECTED = re.compile(
    r"AGENTS\.md instructions|<INSTRUCTIONS>|<user_info>|<environment_context>|"
    r"<system-reminder>|<command-name>|Caveat: The messages below|"
    r"<local-command-(?:caveat|stdout)>|"
    r"This session is being continued from a previous conversation|"
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
            name = it.get("name", "tool")
            question = _question_message(name, it.get("input", {}))
            if question:
                parts.append({"kind": "question", "name": name,
                              "call_id": it.get("id"), **question})
            else:
                parts.append({
                    "kind": "tool", "name": name, "call_id": it.get("id"),
                    "text": json.dumps(it.get("input", {}), ensure_ascii=False, indent=2),
                })
        elif t == "tool_result":
            nested = _flatten_content(it.get("content"))
            images = [p["media"] for p in nested if p.get("media")]
            texts = [p["text"] for p in nested if p.get("kind") != "image" and p.get("text")]
            parts.append({"kind": "tool_result", "text": "\n".join(texts) or "[图片]",
                          "call_id": it.get("tool_use_id"), "media": images})
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


def _msg(role, text="", ts=None, name=None, args=None, media_parts=None, **extra):
    out = {"role": role, "text": text, "ts": ts, "name": name, "args": args}
    images = [x for x in (media_parts or []) if x]
    if images:
        out["media"] = images
    out.update(extra)
    return out


def _status(state: str, ts=None, **extra):
    return _msg("status", state, ts, state=state, **extra)


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
        generated_title = cwd = branch = created = sid = None
        first_user = None
        for rec in _head_lines(f):
            t = rec.get("type")
            if t == "ai-title" and not generated_title:
                generated_title = rec.get("aiTitle")
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
        custom_title = latest_ai_title = None
        tail_cwds = {}
        for rec in _tail_lines(f):
            if rec.get("cwd"):
                value = str(rec["cwd"])
                tail_cwds[value] = tail_cwds.get(value, 0) + 1
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                custom_title = rec["customTitle"]
            elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
                latest_ai_title = rec["aiTitle"]
        title = custom_title or latest_ai_title or generated_title
        if not title:
            title = _title_from_text(first_user) if first_user else f.stem[:8]
        if not cwd and tail_cwds:
            # 老会话的开头可能先塞入大段 hook/attachment，真正的 cwd 会落在
            # HEAD_BYTES 或前 40 条之后。文件尾本来就会为 rename 读取，优先从
            # 其中恢复出现次数最多的真实路径，避免把有歧义的 Claude slug 硬拆。
            cwd = max(tail_cwds, key=tail_cwds.get)
        if not cwd:
            cwd = "/" + proj_name.lstrip("-").replace("-", "/")
        agent_items = []
        for af in sorted((f.parent / f.stem / "subagents").glob("agent-*.jsonl")):
            agent_id = af.stem.removeprefix("agent-")
            try:
                info = json.loads(af.with_suffix(".meta.json").read_text(errors="replace"))
            except (OSError, ValueError):
                info = {}
            try:
                ast = af.stat()
            except OSError:
                continue
            agent_items.append({
                "id": agent_id,
                "title": str(info.get("description") or f"子代理 {agent_id[:8]}"),
                "type": str(info.get("agentType") or "subagent"),
                "updated": _iso(ast.st_mtime), "size": ast.st_size,
            })
        return {
            "uid": _uid("claude", str(f)), "source": "claude", "sid": sid or f.stem,
            "title": title, "cwd": cwd, "created": created or _iso(st.st_mtime),
            "updated": _iso(st.st_mtime), "size": st.st_size, "path": str(f),
            "model": None, "branch": branch, "agents": len(agent_items),
            "agent_items": agent_items,
        }

    def read(self, path: str, start: int = 0, agent: str | None = None):
        return self._read_one(path, start=start, agent=agent[:8] if agent else None)

    def _read_one(self, path: str, agent: str | None = None, start: int = 0):
        msgs, end, calls = [], start, {}
        for rec, off in _iter_records(path, start):
            end = off
            t = rec.get("type")
            ts = _norm_ts(rec.get("timestamp"))
            tag = agent or (rec.get("agentId", "")[:8] if rec.get("isSidechain") else None)
            if t in ("user", "assistant"):
                role = f"{t}·subagent" if tag else t
                parts = _flatten_content((rec.get("message") or {}).get("content"))
                # Claude 没有 task_started；真实用户输入就是新回合的结构化起点。
                text_parts = [p["text"] for p in parts if p["kind"] == "text"]
                if t == "user" and not tag and any(
                        x.strip() and not _is_injected(x) for x in text_parts):
                    msgs.append(_status("working", ts))
                for p in parts:
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
                        calls[p.get("call_id")] = p["name"]
                        msgs.append(_msg("tool", p["text"], ts, name=p["name"]))
                    elif p["kind"] == "question":
                        calls[p.get("call_id")] = p["name"]
                        msgs.append(_msg("question", p["text"], ts,
                                         questions=p["questions"]))
                        if not tag:
                            msgs.append(_status("waiting", ts))
                    elif p["kind"] == "tool_result":
                        name = calls.get(p.get("call_id"))
                        is_answer = _is_question_tool(name)
                        msgs.append(_msg("answer" if is_answer else "tool_result",
                                         p["text"], ts, name=name,
                                         media_parts=p.get("media")))
                        if is_answer and not tag:
                            msgs.append(_status("working", ts))
            elif t == "system":
                if not tag and rec.get("subtype") == "turn_duration":
                    msgs.append(_status("idle", ts, duration_ms=rec.get("durationMs")))
                elif not tag and rec.get("subtype") == "compact_boundary":
                    # /compact 没有 turn_duration；边界记录就是压缩完成点。
                    msgs.append(_status("idle", ts))
                elif rec.get("content"):
                    msgs.append(_msg("system", _stringify(rec["content"]), ts))
        return msgs, end


# --------------------------------------------------------------------------
# Codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
# --------------------------------------------------------------------------

class CodexAdapter:
    source = "codex"

    def __init__(self):
        self._names = None
        self._names_key = None
        self._sid_paths: dict[str, Path] = {}

    def _thread_names(self):
        try:
            st = CODEX_INDEX.stat()
            key = (st.st_size, st.st_mtime_ns)
        except OSError:
            key = None
        if self._names is None or key != self._names_key:
            self._names = {}
            self._names_key = key
            if CODEX_INDEX.exists():
                with open(CODEX_INDEX, "r", errors="replace") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("id") and r.get("thread_name"):
                            self._names[r["id"]] = {
                                "name": str(r["thread_name"]),
                                "updated": _norm_ts(r.get("updated_at")),
                            }
        return self._names

    def _name_event(self, sid: str) -> dict | None:
        """session_index 只保留最终名称和更新时间，不保留原始 /rename 输入。"""
        row = self._thread_names().get(sid)
        return row if row and row.get("updated") else None

    def list_sessions(self):
        if not CODEX_ROOT.is_dir():
            return []
        names = self._thread_names()
        out = []
        self._sid_paths = {}
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
            sid = str(meta.get("session_id") or meta.get("id") or f.stem)
            self._sid_paths[sid] = f
            created = _norm_ts(meta.get("timestamp")) or _iso(st.st_mtime)
            named = names.get(sid)
            title = (named or {}).get("name") or (_title_from_text(first_user) if first_user
                                                   else "(无标题) " + f.stem.replace("rollout-", "")[:16])
            name_event = self._name_event(sid)
            out.append({
                "uid": _uid("codex", str(f)), "source": "codex", "sid": sid,
                "title": _clip(title, 110), "cwd": meta.get("cwd") or "(未知)",
                "created": created,
                "updated": _iso(st.st_mtime), "size": st.st_size, "path": str(f),
                "model": model, "branch": None,
                "forked_from_id": str(meta.get("forked_from_id") or ""),
                "history_base": meta.get("history_base")
                    if isinstance(meta.get("history_base"), dict) else None,
                "renamed_at": name_event.get("updated") if name_event else None,
                "renamed_to": name_event.get("name") if name_event else None,
                "_named": bool(named), "_local_size": st.st_size,
            })
        by_sid = {str(s["sid"]): s for s in out}
        superseded = {s["forked_from_id"] for s in out
                      if s.get("forked_from_id") in by_sid}

        # 双 Esc 回退会创建一个新 UUID，但新 rollout 只保存分叉点之后的增量，
        # history_base 指向父文件的有效前缀。列表里用当前叶子替代被回退的父项；
        # 历史仍由 read() 按链补齐，不能把两个分支的尾部直接拼在一起。
        for s in out:
            chain, seen = [], {str(s["sid"])}
            cur = s
            while cur.get("forked_from_id"):
                parent = by_sid.get(cur["forked_from_id"])
                if not parent or str(parent["sid"]) in seen:
                    break
                seen.add(str(parent["sid"]))
                chain.append(parent)
                cur = parent
            if chain:
                root = chain[-1]
                s["created"] = root["created"]
                s["root_sid"] = root["sid"]
                s["fork_depth"] = len(chain)
                if not s["_named"]:
                    titled = next((p for p in chain if p["_named"]), root)
                    s["title"] = titled["title"]
                history_size = 0
                cur = s
                for parent in chain:
                    base = cur.get("history_base") or {}
                    try:
                        limit = max(0, int(base.get("end_byte_offset") or 0))
                    except (TypeError, ValueError):
                        limit = 0
                    history_size += min(limit, parent["_local_size"])
                    cur = parent
                s["size"] = s["_local_size"] + history_size
        for s in out:
            s.pop("_named", None)
            s.pop("_local_size", None)
        return [s for s in out if str(s["sid"]) not in superseded]

    @staticmethod
    def _session_meta(path: str | Path) -> dict:
        return next((rec.get("payload") or {} for rec in _head_lines(Path(path), 120)
                     if rec.get("type") == "session_meta"), {})

    def _find_session_path(self, sid: str) -> Path | None:
        path = self._sid_paths.get(sid)
        if path and path.is_file():
            return path
        if not CODEX_ROOT.is_dir():
            return None
        for candidate in CODEX_ROOT.rglob(f"*-{sid}.jsonl"):
            meta = self._session_meta(candidate)
            got = str(meta.get("session_id") or meta.get("id") or "")
            if got == sid:
                self._sid_paths[sid] = candidate
                return candidate
        return None

    def _history_segments(self, path: str | Path,
                          seen: set[str] | None = None) -> list[tuple[Path, int]]:
        """返回当前 rollout 继承的父文件前缀，顺序从最老祖先到直接父项。"""
        path = Path(path)
        seen = set() if seen is None else seen
        key = str(path)
        if key in seen:
            return []
        seen.add(key)
        meta = self._session_meta(path)
        base = meta.get("history_base") if isinstance(meta.get("history_base"), dict) else {}
        parent_sid = str(base.get("thread_id") or meta.get("forked_from_id") or "")
        try:
            limit = max(0, int(base.get("end_byte_offset") or 0))
        except (TypeError, ValueError):
            limit = 0
        parent = self._find_session_path(parent_sid) if parent_sid and limit else None
        if not parent or str(parent) in seen:
            return []
        return [*self._history_segments(parent, seen), (parent, limit)]

    def _read_file(self, path: str | Path, start: int = 0,
                   stop: int | None = None):
        msgs, calls, end = [], {}, start
        session_meta = {}
        for rec, off in _iter_records(path, start):
            if stop is not None and off > stop:
                break
            end = off
            ts = _norm_ts(rec.get("timestamp"))
            p = rec.get("payload") or {}
            if rec.get("type") == "session_meta" and not session_meta:
                session_meta = p
            if rec.get("type") == "compacted":
                event_id = rec.get("ordinal") or ts or off
                msgs.append(_msg("event", "上下文已压缩", ts, counted=False,
                                 event_id=f"compact:{event_id}"))
                continue
            if rec.get("type") == "event_msg":
                event = p.get("type")
                if event == "task_started":
                    msgs.append(_status("working", ts, turn_id=p.get("turn_id")))
                elif event == "task_complete":
                    state = "failed" if p.get("error") else "idle"
                    msgs.append(_status(state, ts, turn_id=p.get("turn_id"),
                                        duration_ms=p.get("duration_ms")))
                elif event == "turn_aborted":
                    msgs.append(_status("aborted", ts, turn_id=p.get("turn_id"),
                                        reason=p.get("reason"), duration_ms=p.get("duration_ms")))
                continue
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
                question = _question_message(name, body)
                if question:
                    msgs.append(_msg("question", question["text"], ts,
                                     questions=question["questions"]))
                    msgs.append(_status("waiting", ts, turn_id=p.get("turn_id")))
                else:
                    msgs.append(_msg("tool", _pretty_json(body), ts, name=name))
            elif k in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                name = calls.get(p.get("call_id"))
                is_answer = _is_question_tool(name)
                msgs.append(_msg("answer" if is_answer else "tool_result",
                                 _stringify(p.get("output")), ts, name=name))
                if is_answer:
                    msgs.append(_status("working", ts))
            elif k in ("web_search_call", "tool_search_call"):
                msgs.append(_msg("tool", _pretty_json(p.get("arguments") or {}), ts, name=k))

        return msgs, end, session_meta

    def read(self, path: str, start: int = 0):
        # 增量偏移始终属于当前叶子文件；父历史是不可变前缀，只在首次整读时补。
        if start:
            msgs, end, _ = self._read_file(path, start=start)
            return msgs, end

        msgs = []
        for parent, limit in self._history_segments(path):
            inherited, _, _ = self._read_file(parent, stop=limit)
            msgs.extend(inherited)
        current, end, session_meta = self._read_file(path)
        msgs.extend(current)

        # /rename 是 TUI 本地命令，不进入 rollout。session_index 只证明名称在此时
        # 被设置过，因此显示成不计数的会话事件，不能伪装成原始 user 消息。
        if session_meta:
            sid = session_meta.get("session_id") or session_meta.get("id")
            name_event = self._name_event(str(sid or ""))
            if name_event:
                event = _msg("command", f'/rename {name_event["name"]}',
                             name_event["updated"], counted=False, inferred=True,
                             event_id=f'rename:{sid}:{name_event["updated"]}')
                at = next((i for i, msg in enumerate(msgs)
                           if msg.get("ts") and msg["ts"] > name_event["updated"]), len(msgs))
                msgs.insert(at, event)
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
                    question = _question_message(name, args)
                    if question:
                        msgs.append(_msg("question", question["text"],
                                         questions=question["questions"]))
                        msgs.append(_status("waiting"))
                    else:
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
