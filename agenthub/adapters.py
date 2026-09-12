"""三类 CLI 会话的存储适配器。

统一产出:
  元数据 dict: uid/source/sid/title/cwd/created/updated/size/path/model/extra
  消息 dict:   role/ts/text/name/args/output
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import stat as statmod
import threading
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


def _ts_after(later: str | None, earlier: str | None) -> bool:
    """两个 ``_norm_ts``/``_iso`` 串按真实时刻比较；解析不了的一律不算更晚。"""
    try:
        return datetime.fromisoformat(str(later)) > datetime.fromisoformat(str(earlier))
    except (TypeError, ValueError):
        return False


def _norm_ts(v) -> str | None:
    """把各家时间戳统一成本地时区 ISO 串。"""
    if not v:
        return None
    if isinstance(v, (int, float)):
        dt = datetime.fromtimestamp(
            float(v) / (1000 if v > 1e11 else 1), timezone.utc)
    else:
        s = str(v).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # 网页发送队列会拿记录时间与服务端入队时间做严格因果比较；截到秒会把
    # 同一秒内稍后写入的 user 记录误判成历史消息。
    return dt.astimezone().isoformat(timespec="milliseconds")


def _head_lines(path: Path, limit: int = 40, *, strict: bool = False):
    """读文件头若干行并解析 JSON, 跳过坏行。"""
    out = []
    try:
        with open(path, "rb") as fh:
            blob = fh.read(HEAD_BYTES)
    except OSError:
        if strict:
            raise
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


def _tail_lines(path: Path, *, strict: bool = False):
    """解析文件尾部的完整 JSONL 记录，用于读取后追加的 rename 元数据。"""
    try:
        size = path.stat().st_size
        start = max(0, size - TAIL_BYTES)
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()
    except OSError:
        if strict:
            raise
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


def _latest_jsonl_timestamp(path: Path, parsed_tail: list[dict] | None = None,
                            *, strict: bool = False) -> str | None:
    """返回最后一条带合法时间戳的 JSONL 记录时间。

    文件 mtime 只能说明文件元数据或内容被碰过，并不等于会话发生了活动。
    Claude Code 偶尔会在没有追加记录时刷新 transcript 的 mtime；列表若直接
    信任它，就会把数天前的静态会话突然排到最前。

    元数据读取本来就解析文件尾，所以先复用 ``parsed_tail``。极端情况下最后
    一条记录本身大于 TAIL_BYTES，``_tail_lines`` 会把它当残行丢掉；此时再从
    EOF 分块反向寻找，不能退回会制造假活动时间的 mtime。
    """
    for rec in reversed(parsed_tail or []):
        normalized = _norm_ts(rec.get("timestamp"))
        if normalized:
            return normalized

    try:
        for rec in _iter_records_reversed(path):
            normalized = _norm_ts(rec.get("timestamp"))
            if normalized:
                return normalized
    except OSError:
        if strict:
            raise
    return None


def _iter_records_reversed(path: Path):
    """从 EOF 反向逐条解析 JSONL；跨块残行拼接，坏行跳过，I/O 错误直接抛出。"""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        cursor = fh.tell()
        suffix = b""
        while cursor > 0:
            lo = max(0, cursor - 64 * 1024)
            fh.seek(lo)
            data = fh.read(cursor - lo) + suffix
            lines = data.split(b"\n")
            if lo:
                suffix = lines[0]
                lines = lines[1:]
            else:
                suffix = b""
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except Exception:
                    continue
            cursor = lo


def _first_jsonl_timestamp(path: Path, *, strict: bool = False) -> str | None:
    """文件头第一条带合法时间戳的记录时间。"""
    for rec in _head_lines(path, 8, strict=strict):
        normalized = _norm_ts(rec.get("timestamp"))
        if normalized:
            return normalized
    return None


# Claude 子代理 transcript 没有 turn_duration；回合是否收尾只能看最后一条
# assistant 记录的 stop_reason。
_CLAUDE_TURN_CLOSED = frozenset({"end_turn", "stop_sequence", "refusal"})


def _claude_agent_tail(path: Path) -> tuple[str | None, bool]:
    """子代理 transcript 的 (最后一条记录时间, 最后一个回合是否仍未收尾)。

    同一版本 CLI 里，收尾的 text 记录有时带 end_turn，有时 stop_reason 还是
    None（message_delta 晚于 content_block_stop 落盘）。所以这里的"未收尾"
    只能排除确定已收尾的情况，最终是否仍在运行还要对照父会话的停止通知，
    见 ``_claude_agent_stops``。I/O 错误直接抛出。
    """
    updated, closed = None, None
    for rec in _iter_records_reversed(path):
        if updated is None:
            updated = _norm_ts(rec.get("timestamp"))
        if closed is None and rec.get("type") in ("user", "assistant"):
            closed = (rec.get("type") == "assistant"
                      and (rec.get("message") or {}).get("stop_reason")
                      in _CLAUDE_TURN_CLOSED)
        if updated is not None and closed is not None:
            break
    return updated, closed is False


# Codex 子代理与用户线程共用一套 Session 事件环：task_started / task_complete /
# turn_aborted 恒落盘（codex-rs/rollout/src/policy.rs），ShutdownComplete 不落盘，
# followup 再唤起会写新的 task_started。子代理在父进程内运行，没有自己的进程。
_CODEX_TURN_OPEN = {"task_started": True, "turn_started": True,
                    "task_complete": False, "turn_complete": False,
                    "turn_aborted": False}


def _codex_agent_tail(path: Path) -> tuple[str | None, bool]:
    """Codex 子代理 rollout 的 (最后一条记录时间, 最后一个回合是否仍未收尾)。I/O 错误直接抛出。"""
    updated, open_turn = None, None
    for rec in _iter_records_reversed(path):
        if updated is None:
            updated = _norm_ts(rec.get("timestamp"))
        if open_turn is None and rec.get("type") == "event_msg":
            open_turn = _CODEX_TURN_OPEN.get((rec.get("payload") or {}).get("type"))
        if updated is not None and open_turn is not None:
            break
    return updated, bool(open_turn)


_AGENT_TASK_ID = re.compile(rb"<task-id>([A-Za-z0-9_-]{1,64})</task-id>")
_agent_stops_lock = threading.Lock()
_agent_stops: dict[str, dict] = {}


def _collect_agent_stops(raw: bytes, stops: dict[str, str]) -> None:
    has_notice = b"<task-id>" in raw
    has_result = b'"agentId"' in raw and b'"tool_result"' in raw
    if not (has_notice or has_result):
        return
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return
    ts = _norm_ts(rec.get("timestamp"))
    if not ts:
        return
    agent_ids = set()
    if has_notice:
        # 同一条 task-notification 会以 queue-operation、queued_command 附件或
        # user 记录各写一遍，时间一致；哪一份都算停止点。后台 Bash 任务的
        # task-id 也会进来，只是永远匹配不到子代理。
        agent_ids.update(m.group(1).decode() for m in _AGENT_TASK_ID.finditer(raw))
    if has_result:
        # 前台 Agent 调用没有 task-notification，结束时 tool_result 直接带
        # 完整结果；后台调用的 tool_result 只是 async_launched，不算停止。
        result = rec.get("toolUseResult")
        if (isinstance(result, dict) and result.get("agentId")
                and result.get("status") != "async_launched"):
            agent_ids.add(str(result["agentId"]))
    for agent_id in agent_ids:
        if ts > stops.get(agent_id, ""):
            stops[agent_id] = ts


def _claude_agent_stops(path: Path) -> dict[str, str]:
    """主会话 transcript 里每个子代理最近一次停止的时间，按文件增量扫描。

    子代理停止（完成、失败、被杀、随旧进程消失）后 30–150 ms 内父会话必写
    task-notification 或 Agent 的 tool_result；之后只有 SendMessage 能把它
    唤起，而唤起一定会往子代理 transcript 追加新的 user 记录。因此"子代理
    最后一条记录晚于父会话最近一次停止通知"就是它仍在运行的判据。通知可能
    离文件尾很远，尾读兜不住，所以按路径记住已扫到的字节数，只读新增部分。
    """
    key = str(path)
    size = path.stat().st_size
    with _agent_stops_lock:
        entry = _agent_stops.get(key)
        if entry is None or size < entry["size"]:
            entry = {"size": 0, "stops": {}}
        if size > entry["size"]:
            with open(path, "rb") as fh:
                fh.seek(entry["size"])
                blob = fh.read(size - entry["size"])
            lines = blob.split(b"\n")
            # 最后一段可能是尚未写完的半行，留到下次再解析。
            for raw in lines[:-1]:
                _collect_agent_stops(raw, entry["stops"])
            entry["size"] += len(blob) - len(lines[-1])
        _agent_stops[key] = entry
        return dict(entry["stops"])


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


def _question_answer_text(value) -> tuple[str, bool]:
    """把 Codex request_user_input 的协议结果压成用户实际选择。"""
    text = _stringify(value)
    if re.match(r"^aborted by user(?:\s|$)", text.strip(), re.I):
        return "已取消回答", True
    data = _json_value(value)
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        return text, False
    rows = []
    for answer in answers.values():
        values = answer.get("answers") if isinstance(answer, dict) else answer
        if isinstance(values, list):
            shown = "、".join(str(x) for x in values if str(x).strip())
        else:
            shown = str(values or "").strip()
        if shown:
            rows.append(shown)
    return "\n".join(rows) or text, False


# "bash -c '…'" / "/usr/bin/zsh -lc '…'" 一类的解释器包装, 展示时剥掉只留命令本体
_SHELL_WRAP = re.compile(r"^\s*(?:/usr/bin/|/bin/)?(?:ba|z|da)?sh\s+(?:-[A-Za-z]+\s+)*")
# Codex functions.exec 把命令包在 JS 里: tools.exec_command({ cmd: "..." } 或 {"cmd": "..."})
_EXEC_CMD = re.compile(r'\bcmd["\']?\s*:\s*("(?:\\.|[^"\\])*")')
_EXEC_STEP = re.compile(r'\bstep["\']?\s*:\s*"((?:\\.|[^"\\])*)"')
_EXEC_TOOLS = re.compile(r'\btools\.(\w+)\s*\(')


def _shell_cmd(cmd) -> str:
    """把 shell 调用参数还原成一行可读命令(剥解释器包装和外层引号)。"""
    if isinstance(cmd, list):
        cmd = [str(x) for x in cmd]
        if len(cmd) >= 3 and re.fullmatch(r"-[A-Za-z]*c", cmd[-2] or ""):
            cmd = cmd[-1]          # ["zsh", "-lc", "实际命令"]
        else:
            cmd = " ".join(cmd)
    s = str(cmd).strip()
    stripped = _SHELL_WRAP.sub("", s, count=1)
    if stripped != s and stripped[:1] in ("'", '"') and stripped[-1:] == stripped[:1]:
        stripped = stripped[1:-1]
    return " ".join((stripped or s).split())


def _tool_summary(name: str, value) -> str | None:
    """给工具调用生成一行语义摘要(对标 codex TUI 的 `$ 命令` 风格)。

    只覆盖能可靠识别的工具; 识别不了返回 None, 前端退回原始参数预览。
    """
    data = _json_value(value)
    key = str(name or "").lower().rsplit("__", 1)[-1].rsplit(".", 1)[-1]

    if key in ("bash", "shell", "local_shell_call", "exec_command", "terminal",
               "run_terminal_cmd"):
        cmd = data
        if isinstance(data, dict):
            cmd = data.get("command") or data.get("cmd") \
                or (data.get("action") or {}).get("command")
        if cmd:
            return _clip("$ " + _shell_cmd(cmd), 200)
    if key == "exec" and isinstance(data, str):
        cmds = []
        for m in _EXEC_CMD.finditer(data):
            try:
                cmds.append(_shell_cmd(json.loads(m.group(1))))
            except (TypeError, ValueError):
                continue
        if cmds:
            more = f" …(+{len(cmds) - 1})" if len(cmds) > 1 else ""
            return _clip("$ " + cmds[0], 200) + more
        steps = _EXEC_STEP.findall(data)
        if "update_plan" in _EXEC_TOOLS.findall(data) and steps:
            return _clip(f"计划 ×{len(steps)}: " + "; ".join(steps[:2]), 200)
        called = list(dict.fromkeys(_EXEC_TOOLS.findall(data)))
        if called:
            return _clip("tools." + "  tools.".join(called[:3]), 200)
    if not isinstance(data, dict):
        return None
    path = data.get("file_path") or data.get("path") or data.get("target_file")
    if key in ("read", "read_file", "notebookread", "open") and path:
        span = ""
        if data.get("offset") or data.get("limit"):
            span = f" ⌖{data.get('offset') or 0}+{data.get('limit') or ''}".rstrip("+")
        return _clip(f"读 {path}{span}", 200)
    if key in ("grep", "grep_search", "search", "rg", "codebase_search"):
        pat = data.get("pattern") or data.get("query") or data.get("regex")
        scope = data.get("path") or data.get("glob") or data.get("include") or ""
        if pat:
            return _clip(f"搜 {pat}" + (f" ⌁ {scope}" if scope else ""), 200)
    if key in ("glob", "find", "list_dir", "ls", "file_search") and (data.get("pattern") or path):
        return _clip(f"找 {data.get('pattern') or path}", 200)
    if key in ("webfetch", "web_fetch", "fetch") and data.get("url"):
        return _clip(f"抓 {data['url']}", 200)
    if key in ("websearch", "web_search") and data.get("query"):
        return _clip(f"搜索 {data['query']}", 200)
    if key in ("task", "agent") and (data.get("description") or data.get("prompt")):
        kind = data.get("subagent_type") or data.get("agentType") or ""
        head = data.get("description") or data.get("prompt")
        return _clip(f"子代理{f'({kind})' if kind else ''}: {head}", 200)
    if key == "todowrite":
        todos = [t for t in data.get("todos") or [] if isinstance(t, dict)]
        if todos:
            subj = "; ".join(_clip(str(t.get("subject") or t.get("content") or ""), 36)
                             for t in todos[:3])
            return _clip(f"TODO ×{len(todos)}: {subj}", 200)
    if key == "update_plan":
        plan = [s for s in data.get("plan") or [] if isinstance(s, dict)]
        if plan:
            return _clip(f"计划 ×{len(plan)}: "
                         + "; ".join(_clip(str(s.get("step") or ""), 36) for s in plan[:2]), 200)
    scalars = [(k, v) for k, v in data.items()
               if isinstance(v, (str, int, float, bool)) and str(v).strip()]
    if scalars:
        return _clip("  ".join(f"{k}={v}" for k, v in scalars[:4]), 200)
    return None


_EXIT_CODE = re.compile(
    r'"exit_code"\s*:\s*(-?\d+)|\bexit(?:ed)?(?: with)?(?: code| status)? (-?\d+)',
    re.I)


def _output_exit_code(text: str) -> int | None:
    """从工具输出头部提取退出码；Claude 常只把它写进可见文本。"""
    m = _EXIT_CODE.search(str(text or "")[:400])
    return int(m.group(1) or m.group(2)) if m else None


def _output_error(text: str) -> bool | None:
    """从输出文本头部嗅探退出码; 嗅不到返回 None(未知), 不误报。"""
    code = _output_exit_code(text)
    if code is None:
        return None
    return code != 0


def _quoted_apply_patch(text: str) -> str | None:
    """从 Codex 的 exec 包装代码里取出传给 apply_patch 的字符串。"""
    if not isinstance(text, str):
        return None
    if text.lstrip().startswith("*** Begin Patch"):
        return text[text.index("*** Begin Patch"):]
    # functions.exec 把自由格式补丁写成 JS 字符串；逐个解码字符串字面量比
    # 猜测变量名可靠，也不会把后面的 JS 包装代码混进补丁。
    for match in re.finditer(r'"(?:\\.|[^"\\])*"', text, re.S):
        try:
            value = json.loads(match.group())
        except (TypeError, ValueError):
            continue
        if isinstance(value, str) and value.lstrip().startswith("*** Begin Patch"):
            return value[value.index("*** Begin Patch"):]
    return None


def _apply_patch_changes(patch: str) -> list[dict]:
    """把 apply_patch 协议拆成可独立打开的文件 diff。"""
    rows = patch.splitlines()
    changes, current = [], None
    header = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")

    def finish():
        nonlocal current
        if not current:
            return
        body = current.pop("_body")
        current["patch"] = "\n".join(body)
        current["added"] = sum(1 for line in body
                               if line.startswith("+") and not line.startswith("+++"))
        current["removed"] = sum(1 for line in body
                                 if line.startswith("-") and not line.startswith("---"))
        changes.append(current)
        current = None

    for line in rows:
        match = header.match(line)
        if match:
            finish()
            action, path = match.groups()
            current = {
                "path": path.strip(), "operation": action.lower(), "_body": [],
                # Update 只保存 hunk；Add/Delete 的已知一侧则是完整文件。
                "before_complete": action == "Delete",
                "after_complete": action == "Add",
                "before_available": action != "Add",
                "after_available": action != "Delete",
            }
            continue
        if not current or line in ("*** Begin Patch", "*** End Patch"):
            continue
        if line.startswith("*** Move to: "):
            current["new_path"] = line.removeprefix("*** Move to: ").strip()
            continue
        current["_body"].append(line)
    finish()
    return changes


def _edit_change(path: str, old, new, operation: str = "edit") -> dict:
    old, new = str(old or ""), str(new or "")
    rows = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=path, tofile=path,
        lineterm="", n=3,
    ))
    return {
        "path": path or "(未知文件)", "operation": operation,
        "patch": "\n".join(rows),
        "added": sum(1 for line in rows if line.startswith("+") and not line.startswith("+++")),
        "removed": sum(1 for line in rows if line.startswith("-") and not line.startswith("---")),
        "before_available": True, "after_available": True,
        "before_complete": False, "after_complete": False,
    }


def _tool_file_changes(name: str, value) -> list[dict]:
    """识别各 CLI 的结构化文件修改；无法证明的 shell 修改不做推断。"""
    data = _json_value(value)
    key = str(name or "").lower().rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    patch = None
    if isinstance(data, dict):
        for field in ("patch", "input"):
            patch = _quoted_apply_patch(data.get(field))
            if patch:
                break
    elif isinstance(data, str):
        patch = _quoted_apply_patch(data)
    if patch:
        return _apply_patch_changes(patch)

    if not isinstance(data, dict):
        return []
    path = str(data.get("file_path") or data.get("path") or "")
    if key in ("edit", "str_replace") and path and "old_string" in data and "new_string" in data:
        return [_edit_change(path, data["old_string"], data["new_string"])]
    if key in ("multiedit", "multi_edit") and path:
        return [_edit_change(path, row.get("old_string"), row.get("new_string"))
                for row in data.get("edits") or [] if isinstance(row, dict)]
    if key in ("write", "write_file") and path and "content" in data:
        content = str(data.get("content") or "")
        return [{
            "path": path, "operation": "write",
            "patch": "\n".join("+" + line for line in content.splitlines()),
            "added": len(content.splitlines()), "removed": 0,
            "before_available": False, "after_available": True,
            "before_complete": False, "after_complete": True,
        }]
    return []


# 各家 CLI 都会把项目说明 / 环境信息伪装成 user 消息注入, 这些不能当标题
_INJECTED = re.compile(
    r"AGENTS\.md instructions|<INSTRUCTIONS>|<user_info>|<environment_context>|"
    r"<system-reminder>|<command-name>|Caveat: The messages below|"
    r"<local-command-(?:caveat|stdout)>|"
    r"<task-notification>|"
    r"This session is being continued from a previous conversation|"
    r"# Global User Guidance|<project_instructions>|<user_instructions>", re.I)
_TIMELINE_PROTOCOL = re.compile(
    r"^\s*(?:#\s*AGENTS\.md instructions|#\s*Global User Guidance|"
    r"<INSTRUCTIONS>|<user_info>|<environment_context>|<system-reminder>|"
    r"<command-name>|<local-command-(?:caveat|stdout)>|<task-notification>|"
    r"<project_instructions>|<user_instructions>|"
    r"Caveat:\s*The messages below were generated by the user while running local commands|"
    r"This session is being continued from a previous conversation)", re.I)
_CLAUDE_INTERRUPT = re.compile(
    r"\[Request interrupted by user(?: for tool use)?\]", re.I)
_CODEX_ABORT_MARKER = re.compile(
    r"\s*<turn_aborted>.*?</turn_aborted>\s*", re.I | re.S)
_CODEX_ABORT_PREFIX = re.compile(
    r"^\s*<turn_aborted>.*?</turn_aborted>\s*", re.I | re.S)
_GROK_USER_QUERY = re.compile(
    r"^\s*(?:<image_files>.*?</image_files>\s*)*"
    r"<user_query>(.*?)</user_query>\s*$", re.I | re.S)
_CLAUDE_BASH_INPUT = re.compile(
    r"^\s*<bash-input>(.*?)</bash-input>\s*$", re.I | re.S)
_CLAUDE_BASH_STREAM = re.compile(
    r"<(bash-(?:stdout|stderr))>(.*?)</\1>", re.I | re.S)


def _is_injected(text: str) -> bool:
    return bool(_INJECTED.search(text[:2000]))


def _is_timeline_protocol(text: str) -> bool:
    """只识别位于正文开头的 CLI 协议块；用户在句中讨论同名标签仍应显示。"""
    return bool(_TIMELINE_PROTOCOL.match(str(text or "")))


def _is_claude_interrupt(text: str) -> bool:
    """Claude 把 Esc 中断记成 user 消息，但它不是一个新回合。"""
    return bool(_CLAUDE_INTERRUPT.fullmatch(text.strip()))


def _strip_codex_abort_prefix(text: str) -> str:
    """Codex 会把一轮或多轮中断控制块拼到下一条真实 user 正文前。"""
    while match := _CODEX_ABORT_PREFIX.match(text):
        text = text[match.end():]
    return text


def _strip_grok_user_query(text: str) -> str:
    """剥 Grok 的 user_query/image_files 信封，图片本身由结构化 part 显示。"""
    match = _GROK_USER_QUERY.fullmatch(str(text or ""))
    if not match:
        return text
    body = match.group(1)
    body = re.sub(r"^\r?\n", "", body, count=1)
    return re.sub(r"\r?\n$", "", body, count=1)


def _is_codex_protocol_injection(role: str, text: str,
                                 native_meta: dict | None = None) -> bool:
    """Codex 重建提示词时写入 rollout 的指令，不是时间线对话。

    compact 后这些记录会重新出现；判断不能依赖同时读到 compact 边界，
    否则从字节偏移增量续读时仍会把它们画成大气泡。
    """
    if role == "developer":
        return True
    if role != "user":
        return False
    kinds = (native_meta or {}).get("content_item_kinds")
    if isinstance(kinds, list) and "goal.internal_context" in kinds:
        return True
    return _is_timeline_protocol(text)


def _notification_tag(text: str, name: str) -> str:
    match = re.search(fr"<{re.escape(name)}>(.*?)</{re.escape(name)}>", text,
                      re.I | re.S)
    return html.unescape(match.group(1).strip()) if match else ""


def _claude_local_command(text: str) -> str | None:
    """Extract the user-facing command from Claude's local-command envelope.

    Recent Claude Code versions write these envelopes as system records rather
    than user records. Letting the generic system renderer see the XML creates
    protocol bubbles; dropping every envelope also loses real slash commands.
    """
    name = _notification_tag(str(text or ""), "command-name")
    if not name.startswith("/"):
        return None
    args = _notification_tag(str(text or ""), "command-args")
    return f"{name} {args}".rstrip()


def _claude_bash_input(text: str) -> str | None:
    """Decode Claude's native ``!`` local-shell input envelope."""
    match = _CLAUDE_BASH_INPUT.fullmatch(str(text or ""))
    if not match:
        return None
    command = html.unescape(match.group(1)).strip()
    if not command:
        return None
    return command if command.startswith("!") else f"! {command}"


def _claude_bash_output(text: str) -> dict | None:
    """Decode the stdout/stderr envelope paired with a local-shell input.

    Claude writes both streams inside a ``user`` record because their contents
    are fed back into the model.  They are terminal output in the UI, not a
    second user prompt.  Only accept an exact sequence of known tags so text in
    which the user merely discusses these tags remains ordinary conversation.
    """
    raw = str(text or "")
    matches = list(_CLAUDE_BASH_STREAM.finditer(raw))
    if not matches or _CLAUDE_BASH_STREAM.sub("", raw).strip():
        return None
    streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
    for match in matches:
        name = match.group(1).lower().removeprefix("bash-")
        streams[name].append(html.unescape(match.group(2)).strip("\n"))
    stdout = "\n".join(x for x in streams["stdout"] if x)
    stderr = "\n".join(x for x in streams["stderr"] if x)
    blocks = [stdout] if stdout else []
    if stderr:
        blocks.append(f"stderr:\n{stderr}" if stdout else stderr)
    return {"text": "\n\n".join(blocks), "stderr": bool(stderr)}


def _task_notification_summary(summary: str, status: str) -> str:
    """把 Claude 的内部英文通知压成一条可扫读的中文事件。"""
    patterns = (
        (r'^Monitor event:\s*"(.*)"$', "监控事件 · {}"),
        (r'^Monitor\s+"(.*)"\s+stream ended$', "监控结束 · {}"),
        (r'^Agent\s+"(.*)"\s+finished$', "子代理完成 · {}"),
        (r'^Agent\s+"(.*)"\s+was stopped by user$', "子代理已停止 · {}"),
        (r'^Agent\s+"(.*?)"\s+failed(?::\s*(.*))?$', "子代理失败 · {}{}"),
    )
    for pattern, template in patterns:
        match = re.match(pattern, summary, re.I | re.S)
        if not match:
            continue
        if len(match.groups()) == 1:
            return template.format(match.group(1))
        reason = f" · {match.group(2)}" if match.group(2) else ""
        return template.format(match.group(1), reason)
    if summary:
        return summary
    return {"completed": "后台任务完成", "failed": "后台任务失败",
            "killed": "后台任务已停止"}.get(status, "后台任务通知")


def _claude_task_notification(text: str) -> dict | None:
    if not re.match(r"^\s*<task-notification>(?:\s|$)", str(text or ""), re.I):
        return None
    status = _notification_tag(text, "status").lower()
    summary = _task_notification_summary(_notification_tag(text, "summary"), status)
    return _msg("event", summary, event_kind="task", event_status=status,
                details=_notification_tag(text, "result") or None, counted=False)


def _title_from_text(text: str) -> str:
    """从首条用户消息里挑一个像标题的片段, 剥掉系统注入的包裹内容。"""
    text = re.sub(r"<(command-[a-z-]+|system-reminder|local-command[a-z-]*)>.*?</\1>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 4 and not line.startswith(("#", "-", "*", "```")):
            return _clip(line)
    return _clip(text) or "(无标题)"


def _flatten_content(content, search_only: bool = False) -> list[dict]:
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
            tool_input = it.get("input", {})
            question = _question_message(name, tool_input)
            if question:
                parts.append({"kind": "question", "name": name,
                              "call_id": it.get("id"), **question})
            else:
                parts.append({
                    "kind": "tool", "name": name, "call_id": it.get("id"),
                    "text": "[tool]" if search_only else json.dumps(tool_input, ensure_ascii=False, indent=2),
                    "summary": None if search_only else _tool_summary(name, tool_input),
                    "changes": None if search_only else _tool_file_changes(name, tool_input),
                })
        elif t == "tool_result":
            nested = _flatten_content(it.get("content"), search_only=search_only)
            images = [p["media"] for p in nested if p.get("media")]
            texts = [p["text"] for p in nested if p.get("kind") != "image" and p.get("text")]
            parts.append({"kind": "tool_result", "text": "\n".join(texts) or "[图片]",
                          "call_id": it.get("tool_use_id"), "media": images,
                          "error": bool(it.get("is_error"))})
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


def _tool_output(name: str | None, value) -> tuple[str, dict]:
    """展开执行工具中被 text(result) 再包一层的命令结果。

    Codex 会把这种结果记成 ``Script completed / Output:`` 加一段 JSON；
    JSON 的 ``output`` 才是用户真正想看的 stdout。调用与结果可能跨增量
    批次，也可能被 wait 再套一层提示文字，因此按严格字段识别信封，不按
    工具名或正文内容猜测。普通工具返回的 JSON 保持原样。
    """
    text = _stringify(value)

    candidates = [text]
    if isinstance(value, list):
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                candidates.append(part["text"])
            elif isinstance(part, str):
                candidates.append(part)
    elif isinstance(value, dict) and isinstance(value.get("text"), str):
        candidates.append(value["text"])
    elif isinstance(value, str):
        candidates.append(value)

    envelope = None
    for candidate in candidates:
        payloads = [candidate.strip()]
        marker = candidate.rfind("Output:\n")
        if marker >= 0:
            payloads.insert(0, candidate[marker + len("Output:\n"):].strip())
        for payload in payloads:
            # wait 的提示可能位于 JSON 前面；只尝试从行首的 ``{`` 解码到
            # 文本结尾，绝不抽取正文中间的任意 JSON 片段。
            starts = [0] if payload.startswith("{") else []
            starts.extend(m.start() for m in re.finditer(r"(?m)^[ \t]*\{", payload))
            for start in reversed(dict.fromkeys(starts)):
                try:
                    parsed = json.loads(payload[start:].strip())
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) and "output" in parsed \
                        and "wall_time_seconds" in parsed \
                        and ({"exit_code", "session_id", "chunk_id"} & parsed.keys()):
                    envelope = parsed
                    break
            if envelope is not None:
                break
        if envelope is not None:
            break

    if envelope is not None:
        meta = {}
        if isinstance(envelope.get("exit_code"), int):
            meta["exit_code"] = envelope["exit_code"]
        if isinstance(envelope.get("wall_time_seconds"), (int, float)):
            meta["duration_s"] = envelope["wall_time_seconds"]
        return _stringify(envelope.get("output")), meta
    return text, {}


def _msg(role, text="", ts=None, name=None, args=None, media_parts=None, **extra):
    out = {"role": role, "text": text, "ts": ts, "name": name, "args": args}
    images = [x for x in (media_parts or []) if x]
    if images:
        out["media"] = images
    out.update(extra)
    return out


def _turn_fields(turn_id=None, phase=None) -> dict:
    """Return compact cross-CLI turn metadata without serializing null fields."""
    out = {}
    if turn_id is not None and str(turn_id):
        out["turn_id"] = str(turn_id)
    if phase in {"progress", "final"}:
        out["phase"] = phase
    return out


def _status(state: str, ts=None, **extra):
    return _msg("status", state, ts, state=state, **extra)


# --------------------------------------------------------------------------
# Claude Code:  ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
# --------------------------------------------------------------------------

class ClaudeAdapter:
    source = "claude"

    @staticmethod
    def _compact_boundary(rec: dict) -> bool:
        """Recognize both legacy and current Claude Code compact records."""
        return (rec.get("type") == "system"
                and (rec.get("subtype") == "compact_boundary"
                     or isinstance(rec.get("compactMetadata"), dict)))

    @staticmethod
    def _graph_uuid(rec: dict, agent: str | None = None) -> str | None:
        """返回参与 Claude 当前时间线的记录 UUID。

        Claude 的 jsonl 是追加式树结构：双 Esc 只会另写一个指向旧祖先的
        节点，不会删除已回退的分支。主会话不能让内嵌 sidechain 决定当前
        叶子；独立读取子代理文件时则照常使用其中的树。
        """
        uid = rec.get("uuid")
        if not uid or "parentUuid" not in rec:
            return None
        if not agent and rec.get("isSidechain"):
            return None
        return str(uid)

    @classmethod
    def _lineage_signal(cls, rec: dict, agent: str | None = None) -> str | None:
        """返回一条记录声明的当前叶子；last-prompt 是 Claude 的显式叶标记。"""
        if rec.get("type") == "last-prompt" and rec.get("leafUuid"):
            return str(rec["leafUuid"])
        return cls._graph_uuid(rec, agent)

    @classmethod
    def _active_lineage(cls, path: str, start: int = 0,
                        agent: str | None = None,
                        declared_tip: str | None = None,
                        abandoned_after: int = 0,
                        ) -> tuple[set[str] | None, set[str], int]:
        """扫描读取区间，求活动祖先链、无回答的废弃输入及实际 EOF。"""
        parents: dict[str, str | None] = {}
        user_parents: dict[str, str | None] = {}
        user_offsets: dict[str, int] = {}
        response_nodes: list[str] = []
        tip = str(declared_tip) if declared_tip else None
        scan_tip = None
        end = start
        for rec, off in _iter_records(path, start):
            end = off
            uid = cls._graph_uuid(rec, agent)
            if uid:
                parent = rec.get("parentUuid")
                # Claude 的 compact 边界会故意以 parentUuid=null 开一棵
                # 新树，供模型从摘要继续；本地 JSONL 中的旧对话却仍然存在。
                # 对“人看的时间线”，压缩是一个连续边界，应接回边界前由
                # last-prompt/最后图节点声明的当前叶子。否则每次 compact 后
                # agenthub 都会把全部旧正文误判成已回退分支。
                if not parent and scan_tip and cls._compact_boundary(rec):
                    parent = scan_tip
                parents[uid] = str(parent) if parent else None
                if not agent and rec.get("type") == "user":
                    user_parents[uid] = parents[uid]
                    user_offsets[uid] = off
                elif (rec.get("type") == "assistant"
                      and (rec.get("message") or {}).get("content")):
                    response_nodes.append(uid)
                elif (rec.get("type") == "system"
                      and rec.get("subtype") == "turn_duration"):
                    response_nodes.append(uid)
            signal = cls._lineage_signal(rec, agent)
            if signal:
                scan_tip = signal
                if not declared_tip:
                    tip = signal
        if not tip:
            return None, set(), end
        active: set[str] = set()
        node = tip
        while node and node not in active:
            active.add(node)
            if node not in parents:
                break
            node = parents[node]
        # A fast Esc can commit a native user row and then make the next input
        # a sibling of it.  The old row leaves the selected lineage despite
        # never receiving an assistant response.  Preserve only that narrow
        # abandoned-leaf case as an interrupted user message; completed old
        # branches (the normal double-Esc rewind case) remain hidden.
        responded: set[str] = set()
        for response in response_nodes:
            node = parents.get(response)
            while node and node not in responded:
                responded.add(node)
                node = parents.get(node)
        active_user_parents = {
            parent for uid, parent in user_parents.items() if uid in active
        }
        abandoned = {
            uid for uid, parent in user_parents.items()
            if uid not in active and uid not in responded
            and parent in active and parent in active_user_parents
            # A confirmed double-Esc rewind records the old EOF.  Inputs at or
            # before that boundary were deliberately removed from the display
            # lineage and must stay hidden after a later replacement arrives.
            and user_offsets.get(uid, 0) > max(0, int(abandoned_after or 0))
        }
        return active, abandoned, end

    @classmethod
    def latest_tip_after(cls, path: str, start: int, end: int | None = None,
                         agent: str | None = None) -> str | None:
        """返回一个追加区间自己声明的最后叶子，不回看区间以前的旧分支。"""
        tip = None
        for rec, off in _iter_records(path, start):
            if end is not None and off > end:
                break
            signal = cls._lineage_signal(rec, agent)
            if signal:
                tip = signal
        return tip

    @staticmethod
    def _screen_key(value) -> str:
        # capture-pane -J 已经拼回软换行；这里再忽略所有布局空白，避免终端宽度
        # 或 Markdown 段落换行影响同一条原生记录的匹配。
        return "".join(str(value or "").replace("\u00a0", " ").split())

    @classmethod
    def _record_screen_text(cls, rec: dict) -> str:
        """取 Claude TUI 会实际画出的自然语言，不拿工具协议 JSON 去碰运气。"""
        kind = rec.get("type")
        if kind in {"user", "assistant"}:
            parts = _flatten_content((rec.get("message") or {}).get("content"))
            rows = [str(part.get("text") or "") for part in parts
                    if part.get("kind") in {"text", "thinking"}]
            if kind == "user":
                rows = [row for row in rows if not _is_injected(row)
                        and not _is_timeline_protocol(row)]
            return "\n".join(row for row in rows if row.strip())
        if kind == "system" and rec.get("subtype") == "away_summary":
            return _stringify(rec.get("content"))
        return ""

    @classmethod
    def match_screen_tip(cls, path: str, screen: str,
                         agent: str | None = None) -> str | None:
        """从 Claude 当前屏幕找最后一条仍可见的图节点。

        Claude 在双 Esc 最终确认后只改进程内的 current leaf，不一定落一条 JSONL
        事件。终端却会立即重画到选中的时间线。只接受正常输入提示符下至少 48
        个连续非空白字符的匹配，宁可稍后重试，也不靠短词猜错分支。
        """
        if not re.search(r"(?m)^\s*❯", str(screen or "")):
            return None
        haystack = cls._screen_key(screen)
        if not haystack:
            return None
        matched = None
        width = 48
        for rec, _ in _iter_records(path):
            uid = cls._graph_uuid(rec, agent)
            if not uid:
                continue
            needle = cls._screen_key(cls._record_screen_text(rec))
            if len(needle) < width:
                continue
            starts = list(range(0, max(1, len(needle) - width + 1), width // 2))
            starts.append(max(0, len(needle) - width))
            if any(needle[at:at + width] in haystack for at in starts):
                matched = uid
        return matched

    @classmethod
    def active_tip(cls, path: str, pos: int | None = None,
                   agent: str | None = None) -> str | None:
        """从文件尾反向找 pos 时刻的 Claude 当前叶子，避免为游标全量解析。"""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                end = fh.tell() if pos is None else min(max(0, pos), fh.tell())
                cursor = end
                suffix = b""
                while cursor > 0:
                    lo = max(0, cursor - 64 * 1024)
                    fh.seek(lo)
                    data = fh.read(cursor - lo) + suffix
                    lines = data.split(b"\n")
                    if lo:
                        suffix = lines[0]
                        lines = lines[1:]
                    else:
                        suffix = b""
                    for raw in reversed(lines):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        signal = cls._lineage_signal(rec, agent)
                        if signal:
                            return signal
                    cursor = lo
        except OSError:
            pass
        return None

    @staticmethod
    def _custom_title_before(path: str, pos: int) -> str | None:
        """Find the latest title before an incremental-read boundary.

        /compact re-emits the current custom-title without a timestamp or UUID.
        A full read can suppress that replay while walking forward; an
        incremental read must seed the same state from the prefix or it would
        append a second /rename bubble. Search backwards only when the new
        interval actually contains a title record, so normal polling pays no
        extra I/O.
        """
        if pos <= 0:
            return None
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                cursor = min(pos, fh.tell())
                suffix = b""
                while cursor > 0:
                    lo = max(0, cursor - 64 * 1024)
                    fh.seek(lo)
                    data = fh.read(cursor - lo) + suffix
                    lines = data.split(b"\n")
                    if lo:
                        suffix = lines[0]
                        lines = lines[1:]
                    else:
                        suffix = b""
                    for raw in reversed(lines):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        if (rec.get("type") == "custom-title"
                                and rec.get("customTitle")):
                            return str(rec["customTitle"])
                    cursor = lo
        except OSError:
            pass
        return None

    @classmethod
    def append_extends(cls, path: str, start: int, old_tip: str,
                       agent: str | None = None) -> bool:
        """判断新增区间的最终叶子是否仍是旧叶子的后代。

        若不是，文件虽然只做了 append，逻辑时间线却已经回退/换分支，前端
        必须丢弃旧 DOM 并整份重建。
        """
        parents: dict[str, str | None] = {}
        tip = None
        scan_tip = old_tip
        for rec, _ in _iter_records(path, start):
            uid = cls._graph_uuid(rec, agent)
            if uid:
                parent = rec.get("parentUuid")
                if not parent and scan_tip and cls._compact_boundary(rec):
                    parent = scan_tip
                parents[uid] = str(parent) if parent else None
            signal = cls._lineage_signal(rec, agent)
            if signal:
                tip = signal
                scan_tip = signal
        if not tip:
            return True
        node = tip
        seen: set[str] = set()
        while node and node not in seen:
            if node == old_tip:
                return True
            seen.add(node)
            if node not in parents:
                return False
            node = parents[node]
        return False

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

    def session_meta(self, path: str | Path) -> dict | None:
        """只刷新一个主会话的列表元数据。"""
        f = Path(path)
        try:
            st = f.stat()
        except FileNotFoundError:
            return None
        except OSError:
            raise
        if st.st_size == 0:
            return None
        return self._meta(f, st, f.parent.name)

    def _meta(self, f: Path, st, proj_name: str):
        generated_title = cwd = branch = created = sid = None
        first_user = None
        for rec in _head_lines(f, strict=True):
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
                local_command = _claude_bash_input(txt)
                if local_command:
                    first_user = local_command
                elif (_claude_bash_output(txt) is None
                      and txt.strip() and not _is_injected(txt)):
                    first_user = txt
        custom_title = latest_ai_title = None
        tail_cwds = {}
        tail_records = _tail_lines(f, strict=True)
        for rec in tail_records:
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
        agent_files = sorted((f.parent / f.stem / "subagents").glob("agent-*.jsonl"))
        stops = _claude_agent_stops(f) if agent_files else {}
        for af in agent_files:
            agent_id = af.stem.removeprefix("agent-")
            meta_file = af.with_suffix(".meta.json")
            try:
                info = json.loads(meta_file.read_text(errors="replace"))
                meta_mtime = meta_file.stat().st_mtime
            except FileNotFoundError:
                info, meta_mtime = {}, None
            except OSError:
                raise
            except ValueError:
                info, meta_mtime = {}, None
            try:
                ast = af.stat()
            except FileNotFoundError:
                continue
            except OSError:
                raise
            try:
                agent_updated, open_turn = _claude_agent_tail(af)
                agent_created = _first_jsonl_timestamp(af, strict=True)
            except FileNotFoundError:
                continue
            agent_updated = agent_updated or _iso(ast.st_mtime)
            # meta.json 在派生时写入，是第一条记录读不到时最接近的开始时间。
            agent_created = agent_created \
                or (_iso(meta_mtime) if meta_mtime else None) or agent_updated
            stopped_at = stops.get(agent_id)
            agent_items.append({
                "id": agent_id,
                "title": str(info.get("description") or f"子代理 {agent_id[:8]}"),
                "type": str(info.get("agentType") or "subagent"),
                "created": agent_created, "updated": agent_updated,
                "size": ast.st_size,
                "active": open_turn and (stopped_at is None
                                         or _ts_after(agent_updated, stopped_at)),
            })
        updated = _latest_jsonl_timestamp(f, tail_records, strict=True) \
            or created or _iso(st.st_mtime)
        return {
            "uid": _uid("claude", str(f)), "source": "claude", "sid": sid or f.stem,
            "title": title, "cwd": cwd, "created": created or _iso(st.st_mtime),
            "updated": updated, "size": st.st_size, "path": str(f),
            "model": None, "branch": branch, "agents": len(agent_items),
            "agent_items": agent_items,
        }

    def read(self, path: str, start: int = 0, agent: str | None = None,
             declared_tip: str | None = None, abandoned_after: int = 0,
             search_only: bool = False):
        return self._read_one(path, start=start, agent=agent[:8] if agent else None,
                              declared_tip=declared_tip,
                              abandoned_after=abandoned_after, search_only=search_only)

    def _read_one(self, path: str, agent: str | None = None, start: int = 0,
                  declared_tip: str | None = None,
                  abandoned_after: int = 0, search_only: bool = False):
        # 先用轻量父指针表确定当前分支，再做原有消息解析。这样双 Esc 后留在
        # append-only 文件里的旧输入/回答不会继续混入当前时间线。
        active, abandoned, end = self._active_lineage(
            path, start=start, agent=agent, declared_tip=declared_tip,
            abandoned_after=abandoned_after)
        msgs, calls = [], {}
        current_turn = None
        previous_custom_title = None
        custom_title_seeded = start == 0
        for rec, off in _iter_records(path, start):
            end = off
            uid = self._graph_uuid(rec, agent)
            interrupted_branch = bool(uid and uid in abandoned)
            if (active is not None and uid and uid not in active
                    and not interrupted_branch):
                continue
            t = rec.get("type")
            ts = None if search_only else _norm_ts(rec.get("timestamp"))
            tag = agent or (rec.get("agentId", "")[:8] if rec.get("isSidechain") else None)
            if t in ("user", "assistant"):
                role = f"{t}·subagent" if tag else t
                content = (rec.get("message") or {}).get("content")
                if search_only and isinstance(content, list):
                    # Ordinary tool output cannot match conversation text. Keep
                    # question answers, whose role is part of the search contract.
                    content = [p for p in content if not isinstance(p, dict)
                               or p.get("type") != "tool_result"
                               or _is_question_tool(calls.get(p.get("tool_use_id")))]
                parts = _flatten_content(content, search_only=search_only)
                hidden_record = bool(rec.get("isMeta") or rec.get("isCompactSummary"))
                # Claude 没有 task_started；真实用户输入就是新回合的结构化起点。
                text_parts = [p["text"] for p in parts if p["kind"] == "text"]
                notifications = [_claude_task_notification(x) for x in text_parts]
                bash_inputs = [(_claude_bash_input(x) if t == "user" and not tag
                                else None) for x in text_parts]
                bash_outputs = [(_claude_bash_output(x) if t == "user" and not tag
                                 else None) for x in text_parts]
                if hidden_record:
                    continue
                visible_user_text = any(
                    command is not None or (
                        output is None and x.strip()
                        and x.strip() != "/compact" and notice is None
                        and not _is_timeline_protocol(x))
                    for x, notice, command, output in zip(
                        text_parts, notifications, bash_inputs, bash_outputs))
                starts_turn = (t == "user" and (not tag or agent)
                               and not interrupted_branch
                               and (visible_user_text
                                    or any(p["kind"] == "image" for p in parts)))
                if starts_turn:
                    current_turn = rec.get("uuid") or off
                if t == "user" and not tag and (
                        rec.get("interruptedMessageId")
                        or any(_is_claude_interrupt(x) for x in text_parts)):
                    msgs.append(_status("aborted", ts, **_turn_fields(current_turn)))
                    continue
                if t == "user" and not tag and not interrupted_branch and visible_user_text:
                    msgs.append(_status("working", ts, **_turn_fields(current_turn)))
                emitted_interrupted_user = False
                interrupted_meta = ({
                    "interrupted": True,
                    "interrupt_reason": "输入已中断，未进入当前 Claude 分支",
                } if interrupted_branch else {})
                stop_reason = (rec.get("message") or {}).get("stop_reason")
                assistant_phase = ("final" if stop_reason == "end_turn"
                                   else "progress" if t == "assistant" and stop_reason
                                   else None)
                for p in parts:
                    if not str(p.get("text", "")).strip() and not p.get("media"):
                        continue
                    if p["kind"] == "text":
                        notice = _claude_task_notification(p["text"])
                        bash_input = (_claude_bash_input(p["text"])
                                      if role == "user" and not tag else None)
                        bash_output = (_claude_bash_output(p["text"])
                                       if role == "user" and not tag else None)
                        if bash_input is not None:
                            event_id = rec.get("uuid") or off
                            call_id = f"local-shell:{event_id}"
                            msgs.append(_msg(
                                "command", bash_input, ts, call_id=call_id,
                                local_shell=True, event_id=call_id,
                                **_turn_fields(current_turn),
                                **interrupted_meta))
                            emitted_interrupted_user |= interrupted_branch
                        elif bash_output is not None:
                            parent = rec.get("parentUuid") or rec.get("uuid") or off
                            msgs.append(_msg(
                                "tool_result", bash_output["text"], ts,
                                name="Shell", call_id=f"local-shell:{parent}",
                                counted=False,
                                has_stderr=bash_output["stderr"],
                                **_turn_fields(current_turn)))
                        elif notice and not tag:
                            notice["ts"] = ts
                            msgs.append(notice)
                        elif role.startswith("user") and (
                                _is_timeline_protocol(p["text"])
                                or (not tag and p["text"].strip() == "/compact")):
                            continue
                        else:
                            msgs.append(_msg(role, p["text"], ts, name=tag,
                                             **_turn_fields(current_turn,
                                                            assistant_phase),
                                             **interrupted_meta))
                            emitted_interrupted_user |= interrupted_branch
                    elif p["kind"] == "image":
                        msgs.append(_msg(role, p["text"], ts, name=tag,
                                         media_parts=[p.get("media")],
                                         **_turn_fields(current_turn,
                                                        assistant_phase),
                                         **interrupted_meta))
                        emitted_interrupted_user |= interrupted_branch
                    elif p["kind"] == "thinking":
                        msgs.append(_msg("thinking", p["text"], ts, name=tag,
                                         **_turn_fields(current_turn)))
                    elif p["kind"] == "tool":
                        calls[p.get("call_id")] = p["name"]
                        msgs.append(_msg("tool", p["text"], ts, name=p["name"],
                                         call_id=p.get("call_id"),
                                         summary=p.get("summary"),
                                         changes=p.get("changes") or None,
                                         **_turn_fields(current_turn)))
                    elif p["kind"] == "question":
                        calls[p.get("call_id")] = p["name"]
                        msgs.append(_msg("question", p["text"], ts,
                                         name=p["name"], call_id=p.get("call_id"),
                                         questions=p["questions"],
                                         **_turn_fields(current_turn)))
                        if not tag:
                            msgs.append(_status("waiting", ts,
                                                **_turn_fields(current_turn)))
                    elif p["kind"] == "tool_result":
                        name = calls.get(p.get("call_id"))
                        is_answer = _is_question_tool(name)
                        answer_text = p["text"]
                        if (is_answer and p.get("error")
                                and answer_text.startswith(
                                    "The user doesn't want to proceed with this tool use.")):
                            answer_text = "已取消回答"
                        exit_code = _output_exit_code(p["text"])
                        output_meta = {"exit_code": exit_code} if exit_code is not None else {}
                        msgs.append(_msg("answer" if is_answer else "tool_result",
                                         answer_text, ts, name=name,
                                         call_id=p.get("call_id"),
                                         error=bool(p.get("error")) or
                                               (exit_code is not None and exit_code != 0),
                                         media_parts=p.get("media"),
                                         **_turn_fields(current_turn), **output_meta))
                        if is_answer and not tag:
                            msgs.append(_status("working", ts,
                                                **_turn_fields(current_turn)))
                if emitted_interrupted_user:
                    msgs.append(_status("aborted", ts,
                                        reason="输入已中断，未进入当前 Claude 分支",
                                        **_turn_fields(current_turn)))
            elif t == "system":
                if not tag and rec.get("subtype") == "turn_duration":
                    duration = rec.get("durationMs")
                    msgs.append(_status("idle", ts, duration_ms=duration,
                                        **_turn_fields(current_turn)))
                    if isinstance(duration, (int, float)) and duration >= 0:
                        msgs.append(_msg("event", "", ts, counted=False,
                                         event_kind="duration", duration_ms=duration,
                                         **_turn_fields(current_turn)))
                elif not tag and rec.get("subtype") == "away_summary" and rec.get("content"):
                    msgs.append(_msg("event", _stringify(rec["content"]), ts,
                                     counted=False, event_kind="recap",
                                     **_turn_fields(current_turn)))
                elif rec.get("subtype") == "local_command":
                    # Claude 2.x writes slash-command protocol as system XML.
                    # /rename already has the earlier custom-title record and
                    # /compact is represented by its completion event. Other
                    # commands remain visible as one semantic command bubble;
                    # local-command-stdout stays out of the timeline.
                    command = _claude_local_command(rec.get("content"))
                    verb = command.split(maxsplit=1)[0] if command else ""
                    if not tag and command and verb not in {"/rename", "/compact"}:
                        event_id = rec.get("uuid") or off
                        msgs.append(_msg("command", command, ts, counted=False,
                                         inferred=True,
                                         event_id=f"command:{event_id}",
                                         **_turn_fields(current_turn)))
                elif self._compact_boundary(rec):
                    # /compact 没有 turn_duration；边界记录就是压缩完成点。
                    if not tag:
                        msgs.append(_status("idle", ts,
                                            **_turn_fields(current_turn)))
                    event_id = rec.get("uuid") or ts or off
                    msgs.append(_msg("event", "已压缩", ts, counted=False,
                                     event_kind="compact",
                                     event_id=f"compact:{event_id}",
                                     **_turn_fields(current_turn)))
                elif rec.get("content"):
                    msgs.append(_msg("system", _stringify(rec["content"]), ts,
                                     **_turn_fields(current_turn)))
            elif t == "attachment" and not tag:
                attachment = rec.get("attachment")
                # Claude 在工具执行期间吸收排队输入时，不一定再写普通 user
                # 记录，而会把真正参与本轮推理的输入保存成 queued_command。
                # task-notification 也复用该附件类型，必须同时核对来源和模式。
                if (isinstance(attachment, dict)
                        and attachment.get("type") == "queued_command"
                        and attachment.get("commandMode") == "prompt"
                        and (attachment.get("origin") or {}).get("kind") == "human"
                        and isinstance(attachment.get("prompt"), str)
                        and attachment["prompt"].strip()):
                    current_turn = rec.get("uuid") or off
                    msgs.append(_msg("user", attachment["prompt"], ts,
                                     **_turn_fields(current_turn)))
            elif t == "queue-operation" and not tag:
                # Claude 忙时会先把网页送入的 prompt 留在自己的内存队列。
                # enqueue 证明 CLI 确实接收；dequeue/popAll 表示提升为正式 user，
                # remove 多用于取消或内部通知迁移。都作为不可见控制事件透传。
                operation = str(rec.get("operation") or "")
                content = rec.get("content")
                if operation in {"enqueue", "remove", "dequeue", "popAll"}:
                    if operation in {"enqueue", "remove"} and not (
                            isinstance(content, str) and content):
                        continue
                    msgs.append(_msg("queue_operation",
                                     content if isinstance(content, str) else "", ts,
                                     operation=operation, counted=False, silent=True))
            elif t == "custom-title" and not tag and rec.get("customTitle"):
                # Claude 的 /rename 不写普通 user 记录，而是在命令之后追加
                # custom-title。它没有 timestamp，但增量读取的文件偏移已经是
                # 可靠的因果边界。用户确实在 TUI 输入过这条斜杠命令；显示为
                # 不计数的 command，同时由扫描器负责更新会话标题。
                title = str(rec["customTitle"])
                if not custom_title_seeded:
                    previous_custom_title = self._custom_title_before(path, start)
                    custom_title_seeded = True
                if title == previous_custom_title:
                    continue
                previous_custom_title = title
                msgs.append(_msg("command", f"/rename {title}", ts,
                                 counted=False, inferred=True,
                                 event_id=f"rename:{rec.get('sessionId') or ''}:{off}"))
        return msgs, end


# --------------------------------------------------------------------------
# Codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
# --------------------------------------------------------------------------

class CodexAdapter:
    source = "codex"

    def __init__(self):
        self._names = None
        self._names_key = None
        self._names_lock = threading.Lock()
        self._sid_paths: dict[str, Path] = {}

    def _thread_names(self):
        with self._names_lock:
            try:
                st = CODEX_INDEX.stat()
                key = (st.st_size, st.st_mtime_ns, int(getattr(st, "st_ino", 0)))
            except FileNotFoundError:
                key = None
            except OSError:
                raise
            if self._names is None or key != self._names_key:
                names = {}
                if key is not None:
                    with open(CODEX_INDEX, "r", errors="replace") as fh:
                        for line in fh:
                            try:
                                r = json.loads(line)
                            except Exception:
                                continue
                            if r.get("id") and r.get("thread_name"):
                                names[r["id"]] = {
                                    "name": str(r["thread_name"]),
                                    "updated": _norm_ts(r.get("updated_at")),
                                }
                # stat、读取和两个缓存字段必须同属一个临界区；否则不同版本
                # 的并发读可交错成“旧内容 + 新 key”，并永久命中错误缓存。
                self._names = names
                self._names_key = key
            return self._names

    def _name_event(self, sid: str) -> dict | None:
        """session_index 只保留最终名称和更新时间，不保留原始 /rename 输入。"""
        row = self._thread_names().get(sid)
        return row if row and row.get("updated") else None

    @staticmethod
    def _raw_meta(f: Path, st) -> dict:
        """读取一个 rollout 的本地元数据，不在这里处理分叉继承与隐藏。"""
        meta, first_user, model = {}, None, None
        for rec in _head_lines(f, 120, strict=True):
            p = rec.get("payload") or {}
            if rec.get("type") == "session_meta" and not meta:
                meta = p
            if rec.get("type") == "turn_context" and not model:
                model = p.get("model")
            if first_user is None and rec.get("type") == "response_item" \
                    and p.get("type") == "message" and p.get("role") == "user":
                txt = "\n".join(x["text"] for x in _flatten_content(p.get("content"))
                                  if x["kind"] == "text")
                native_meta = p.get("internal_chat_message_metadata_passthrough")
                if not isinstance(native_meta, dict):
                    native_meta = {}
                if (txt.strip() and not _is_injected(txt)
                        and not _is_codex_protocol_injection("user", txt, native_meta)):
                    first_user = txt
        thread_source = str(meta.get("thread_source") or "")
        source_meta = meta.get("source")
        is_subagent = (thread_source == "subagent"
                       or isinstance(source_meta, dict) and "subagent" in source_meta)
        subagent = source_meta.get("subagent") if isinstance(source_meta, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        spawn = spawn if isinstance(spawn, dict) else {}
        # Codex multi-agent rollout 的 session_id 指向父线程，真正唯一的是 id。
        # 若仍优先 session_id，多个 agent 会覆盖父 row；其 forked_from_id 又
        # 等于父 SID，最终会把正在运行的父会话从公开列表完全隐藏。
        sid = str((meta.get("id") if is_subagent else meta.get("session_id"))
                  or meta.get("id") or f.stem)
        base_title = (_title_from_text(first_user) if first_user
                      else "(无标题) " + f.stem.replace("rollout-", "")[:16])
        # 只有子代理才读尾部：列表要显示它的结束时间和是否还在跑。
        agent_updated, agent_active = _codex_agent_tail(f) if is_subagent else (None, False)
        return {
            "uid": _uid("codex", str(f)), "source": "codex", "sid": sid,
            "title": _clip(base_title, 110), "cwd": meta.get("cwd") or "(未知)",
            "created": _norm_ts(meta.get("timestamp")) or _iso(st.st_mtime),
            "updated": agent_updated or _iso(st.st_mtime), "size": st.st_size, "path": str(f),
            "model": model, "branch": None,
            "forked_from_id": str(meta.get("forked_from_id") or ""),
            "history_base": meta.get("history_base")
                if isinstance(meta.get("history_base"), dict) else None,
            "renamed_at": None, "renamed_to": None,
            "_base_title": base_title, "_named": False, "_local_size": st.st_size,
            "_is_subagent": is_subagent,
            "_agent_parent_sid": str(meta.get("parent_thread_id")
                                     or spawn.get("parent_thread_id")
                                     or meta.get("forked_from_id") or "") if is_subagent else "",
            "_agent_title": str(meta.get("agent_path") or spawn.get("agent_path")
                                or meta.get("agent_nickname")
                                or spawn.get("agent_nickname") or sid),
            "_agent_type": str(meta.get("agent_role") or spawn.get("agent_role")
                               or "subagent"),
            "_agent_active": agent_active,
        }

    def session_meta(self, path: str | Path) -> dict | None:
        """只解析一个 rollout，返回供索引保存的未归并元数据。"""
        f = Path(path)
        try:
            st = f.stat()
        except FileNotFoundError:
            return None
        except OSError:
            raise
        if st.st_size == 0:
            return None
        return self._raw_meta(f, st)

    def scan_sessions(self) -> list[dict]:
        """枚举全部 rollout；分支父项和当前叶子都保留。"""
        if not CODEX_ROOT.is_dir():
            return []
        out = []
        for f in CODEX_ROOT.rglob("*.jsonl"):
            row = self.session_meta(f)
            if row is not None:
                out.append(row)
        return out

    def finalize_sessions(self, sessions: list[dict]) -> list[dict]:
        """套用 rename、分叉继承和逻辑大小；父项留给用户决定是否隐藏。"""
        names = self._thread_names()
        # 协作 agent rollout 是父线程的内部执行记录，不是用户的回滚分支。
        # 挂到所属主会话的视图列表，不参与公开列表、fork 替代和 history path 映射。
        out = [dict(s) for s in sessions if not s.get("_is_subagent")]
        self._sid_paths = {str(s["sid"]): Path(s["path"]) for s in out}
        for s in out:
            named = names.get(str(s["sid"]))
            s["_named"] = bool(named)
            s["title"] = _clip((named or {}).get("name")
                               or s.get("_base_title") or s.get("title") or "", 110)
            name_event = named if named and named.get("updated") else None
            s["renamed_at"] = name_event.get("updated") if name_event else None
            s["renamed_to"] = name_event.get("name") if name_event else None

        by_sid = {str(s["sid"]): s for s in out}
        agents = {str(s["sid"]): s for s in sessions if s.get("_is_subagent")}
        for agent in sorted(agents.values(), key=lambda s: (s["created"], s["sid"])):
            owner = agent.get("_agent_parent_sid", "")
            seen = {agent["sid"]}
            while owner in agents and owner not in seen:
                seen.add(owner)
                owner = agents[owner].get("_agent_parent_sid", "")
            if owner in seen or owner not in by_sid:
                continue
            items = by_sid[owner].setdefault("agent_items", [])
            items.append({
                "id": agent["sid"], "title": agent["_agent_title"],
                "type": agent["_agent_type"], "active": bool(agent.get("_agent_active")),
                **{k: agent[k] for k in ("path", "cwd", "model", "created", "updated", "size")},
            })
            by_sid[owner]["agents"] = len(items)
        # 双 Esc 回退会创建一个新 UUID，但新 rollout 只保存分叉点之后的增量，
        # history_base 指向父文件的有效前缀。父子会话都保留供用户查看；
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
            s.pop("_base_title", None)
            s.pop("_named", None)
            s.pop("_local_size", None)
            s.pop("_is_subagent", None)
            s.pop("_agent_parent_sid", None)
            s.pop("_agent_title", None)
            s.pop("_agent_type", None)
            s.pop("_agent_active", None)
        return out

    def list_sessions(self):
        return self.finalize_sessions(self.scan_sessions())

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
                   stop: int | None = None, search_only: bool = False):
        msgs, calls, end = [], {}, start
        session_meta = {}
        for rec, off in _iter_records(path, start):
            if stop is not None and off > stop:
                break
            end = off
            ts = None if search_only else _norm_ts(rec.get("timestamp"))
            p = rec.get("payload") or {}
            if rec.get("type") == "session_meta" and not session_meta:
                session_meta = p
            if rec.get("type") == "compacted":
                event_id = rec.get("ordinal") or ts or off
                msgs.append(_msg("event", "已压缩", ts, counted=False,
                                 event_kind="compact", event_id=f"compact:{event_id}"))
                continue
            if rec.get("type") == "event_msg":
                if search_only:
                    continue
                event = p.get("type")
                if event == "task_started":
                    msgs.append(_status("working", ts, turn_id=p.get("turn_id")))
                elif event == "task_complete":
                    state = "failed" if p.get("error") else "idle"
                    msgs.append(_status(state, ts, turn_id=p.get("turn_id"),
                                        duration_ms=p.get("duration_ms")))
                elif event == "turn_aborted":
                    aborted_turn = str(p.get("turn_id") or "")
                    # Codex 不会为中断轮补 final_answer。把结构化中断语义落到
                    # 最后一条 commentary 上，前端才能在整读历史时保留一条
                    # 可见状态；只标最后一条，避免展开过程后每条进展都显示
                    # “已中断”。增量读取若没覆盖这条消息，浏览器会用同一个
                    # activity.turn_id 在缓存中补标。
                    if aborted_turn:
                        for message in reversed(msgs):
                            if (message.get("turn_id") == aborted_turn
                                    and message.get("role") == "assistant"):
                                if message.get("phase") != "final":
                                    message["interrupted"] = True
                                    message["interrupt_reason"] = (
                                        p.get("reason") or "本轮在最终答复前被中断")
                                break
                    msgs.append(_status("aborted", ts, turn_id=p.get("turn_id"),
                                        reason=p.get("reason"), duration_ms=p.get("duration_ms")))
                continue
            if rec.get("type") != "response_item":
                continue
            k = p.get("type")
            native_meta = p.get("internal_chat_message_metadata_passthrough")
            if not isinstance(native_meta, dict):
                native_meta = {}
            turn_id = p.get("turn_id") or native_meta.get("turn_id")
            turn_meta = _turn_fields(turn_id)
            if k == "message":
                native_role = p.get("role") or "user"
                role = native_role
                role = {"developer": "system", "tool": "tool_result"}.get(role, role)
                parts = _flatten_content(p.get("content"), search_only=search_only)
                txt = "\n".join(x["text"] for x in parts if x["kind"] == "text")
                images = [x["media"] for x in parts if x.get("media")]
                # Codex 会在 event_msg:turn_aborted 前额外写一条 developer XML。
                # 后者已经提供结构化状态；把 XML 再画成系统气泡只会重复且吓人。
                if native_role == "developer" and _CODEX_ABORT_MARKER.fullmatch(txt):
                    continue
                if native_role == "user":
                    txt = _strip_codex_abort_prefix(txt)
                if _is_codex_protocol_injection(native_role, txt, native_meta):
                    continue
                if txt.strip() or images:
                    shown = txt or "[图片]"
                    native_phase = p.get("phase")
                    phase = ("final" if native_phase == "final_answer"
                             else "progress" if native_phase == "commentary"
                             else None)
                    msgs.append(_msg(role, shown, ts, media_parts=images,
                                     **_turn_fields(turn_id, phase)))
            elif k == "reasoning":
                txt = "\n".join(x["text"] for x in _flatten_content(p.get("summary"), search_only=search_only) if x["kind"] == "text")
                if txt.strip():
                    msgs.append(_msg("thinking", txt, ts, **turn_meta))
            elif k in ("function_call", "custom_tool_call", "local_shell_call"):
                name = p.get("name") or k
                body = p.get("arguments") or p.get("input") or p.get("action") or ""
                calls[p.get("call_id")] = name
                question = _question_message(name, body)
                if question:
                    msgs.append(_msg("question", question["text"], ts, name=name,
                                     call_id=p.get("call_id"),
                                     questions=question["questions"], **turn_meta))
                    msgs.append(_status("waiting", ts, **turn_meta))
                elif not search_only:
                    msgs.append(_msg("tool", _pretty_json(body), ts, name=name,
                                     call_id=p.get("call_id"),
                                     summary=_tool_summary(name, body),
                                     changes=_tool_file_changes(name, body) or None,
                                     **turn_meta))
            elif k in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                name = calls.get(p.get("call_id"))
                is_answer = _is_question_tool(name)
                if search_only and not is_answer:
                    continue
                out_text, output_meta = _tool_output(name, p.get("output"))
                cancelled = False
                if is_answer:
                    out_text, cancelled = _question_answer_text(p.get("output"))
                exit_code = output_meta.get("exit_code")
                msgs.append(_msg("answer" if is_answer else "tool_result",
                                 out_text, ts, name=name,
                                 call_id=p.get("call_id"),
                                 error=cancelled or (exit_code != 0 if exit_code is not None
                                        else _output_error(out_text) or False),
                                 **turn_meta, **output_meta))
                if is_answer:
                    msgs.append(_status("working", ts, **turn_meta))
            elif k in ("web_search_call", "tool_search_call") and not search_only:
                msgs.append(_msg("tool", _pretty_json(p.get("arguments") or {}), ts,
                                 name=k, **turn_meta))

        return msgs, end, session_meta

    def read(self, path: str, start: int = 0, search_only: bool = False):
        # 增量偏移始终属于当前叶子文件；父历史是不可变前缀，只在首次整读时补。
        if start:
            msgs, end, _ = self._read_file(path, start=start, search_only=search_only)
            return msgs, end

        msgs = []
        for parent, limit in self._history_segments(path):
            inherited, _, _ = self._read_file(parent, stop=limit, search_only=search_only)
            msgs.extend(inherited)
        current, end, session_meta = self._read_file(path, search_only=search_only)
        msgs.extend(current)

        # /rename 是 TUI 本地命令，不进入 rollout。session_index 只证明名称在此时
        # 被设置过，因此显示成不计数的会话事件，不能伪装成原始 user 消息。
        if session_meta and not search_only:
            # 子代理的 session_id 可能继承自主线程，不能套用主线程的改名事件。
            sid = session_meta.get("id") or session_meta.get("session_id")
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
            row = self.session_meta(sj.parent)
            if row is not None:
                out.append(row)
        return out

    def session_meta(self, path: str | Path) -> dict | None:
        """只刷新一个 Grok 会话目录的 summary 与动态大小。"""
        sess_dir = Path(path)
        sj = sess_dir / "summary.json"
        try:
            summary_st = sj.stat()
        except FileNotFoundError:
            return None
        except OSError:
            raise
        if not statmod.S_ISREG(summary_st.st_mode):
            return None
        try:
            st = (sess_dir / "chat_history.jsonl").stat()
        except FileNotFoundError:
            st = summary_st  # 会话尚未落盘聊天记录时退回 summary
        except OSError:
            raise
        try:
            info = json.loads(sj.read_text(errors="replace"))
        except OSError:
            raise
        except (TypeError, ValueError):
            info = {}
        base = info.get("info") or {}
        title = info.get("generated_title") or info.get("session_summary") or sess_dir.name[:8]
        return {
            "uid": _uid("grok", str(sess_dir)), "source": "grok",
            "sid": base.get("id") or sess_dir.name, "title": _clip(title, 110),
            "cwd": base.get("cwd") or _unquote_cwd(sess_dir.parent.name),
            "created": _norm_ts(info.get("created_at")) or _iso(st.st_mtime),
            "updated": _norm_ts(info.get("last_active_at") or info.get("updated_at"))
                or _iso(st.st_mtime),
            "size": _dir_size(sess_dir), "path": str(sess_dir),
            "model": info.get("current_model_id"), "branch": info.get("agent_name"),
        }

    def read(self, path: str, start: int = 0):
        chat = Path(path) / "chat_history.jsonl"
        msgs, calls, end = [], {}, start
        current_turn = None
        if not chat.is_file():
            return msgs, end
        for rec, off in _iter_records(chat, start):
            end = off
            t = rec.get("type")
            if t == "reasoning":
                txt = "\n".join(x["text"] for x in _flatten_content(rec.get("summary")) if x["kind"] == "text")
                if txt.strip():
                    msgs.append(_msg("thinking", txt,
                                     **_turn_fields(current_turn)))
            elif t == "tool_result":
                parts = _flatten_content(rec.get("content"))
                txt = "\n".join(x["text"] for x in parts if x.get("kind") != "image" and x.get("text"))
                images = [x["media"] for x in parts if x.get("media")]
                msgs.append(_msg("tool_result", txt or "[图片]",
                                 name=calls.get(rec.get("tool_call_id")),
                                 call_id=rec.get("tool_call_id"),
                                 error=_output_error(txt) or False, media_parts=images,
                                 **_turn_fields(current_turn)))
            elif t in ("user", "assistant", "system"):
                parts = _flatten_content(rec.get("content"))
                txt = "\n".join(x["text"] for x in parts if x["kind"] == "text")
                images = [x["media"] for x in parts if x.get("media")]
                if txt.strip() or images:
                    shown = txt or "[图片]"
                    if t == "user":
                        shown = _strip_grok_user_query(shown)
                    if not rec.get("synthetic_reason") \
                            and not (t == "user" and _is_timeline_protocol(shown)):
                        if t == "user":
                            prompt_index = rec.get("prompt_index")
                            current_turn = (f"prompt:{prompt_index}"
                                            if prompt_index is not None else f"offset:{off}")
                        phase = ("final" if t == "assistant" and not rec.get("tool_calls")
                                 else "progress" if t == "assistant"
                                 else None)
                        msgs.append(_msg(t, shown, media_parts=images,
                                         **_turn_fields(current_turn, phase)))
                for tc in rec.get("tool_calls") or []:
                    name = tc.get("name") or (tc.get("function") or {}).get("name") or "tool"
                    args = tc.get("arguments") or (tc.get("function") or {}).get("arguments") or ""
                    calls[tc.get("id")] = name
                    question = _question_message(name, args)
                    if question:
                        msgs.append(_msg("question", question["text"],
                                         questions=question["questions"],
                                         **_turn_fields(current_turn)))
                        msgs.append(_status("waiting", **_turn_fields(current_turn)))
                    else:
                        msgs.append(_msg("tool", _pretty_json(args), name=name,
                                         call_id=tc.get("id"),
                                         summary=_tool_summary(name, args),
                                         changes=_tool_file_changes(name, args) or None,
                                         **_turn_fields(current_turn)))
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
