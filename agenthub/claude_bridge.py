"""Claude Code 交互态到 agenthub 的窄桥接。

Claude 恢复会话时，AskUserQuestion 对话框可能已经显示在 TUI 中，但对应
tool_use 要等用户回答后才追加到 transcript。这里用 Claude Code 官方 hook
被动记录这个结构化工具调用；不批准、不拒绝，也不改写任何工具输入。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


DATA_DIR = Path.home() / ".local" / "share" / "agenthub"
PROMPT_DIR = DATA_DIR / "claude-prompts"
SETTINGS_FILE = DATA_DIR / "claude-bridge-settings.json"
VERSION = 1
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_COMPOSER_RULE = re.compile(r"^\s*─{12,}(?:\s+.+?\s+─+)?\s*$")
_BUSY_STATUS = re.compile(
    r"(?:\besc\s+to\s+(?:interrupt|stop|cancel)\b"
    r"|^\s*[✻✽✢✶✳✣✤]\s+\S[^\n]*…(?:\s+\([^\n)]*\))?\s*$"
    r"|^\s*[✻✽✢✶✳✣✤]\s+[^\n]*\b\d+\s+shells?\s+still\s+running\b[^\n]*$"
    r"|^\s*\*\s+\S[^\n]*…\s+\([^\n)]*\btokens?\b[^\n)]*\)\s*$)",
    re.IGNORECASE | re.MULTILINE)


def busy_screen(screen: str) -> bool:
    """Whether Claude visibly has a turn in progress.

    Wide panes expose ``esc to interrupt`` in the footer.  Claude drops that
    footer text in narrow panes but keeps its animated ``✢ Unfurling…`` row;
    both are positive busy evidence.  The ASCII ``*`` frame is accepted only
    with Claude's token counter so ordinary Markdown bullets cannot match.
    """
    return bool(_BUSY_STATUS.search(_ANSI.sub("", str(screen or ""))))


def _styled_chars(text: str) -> list[tuple[str, bool]]:
    """返回可见字符及其 dim 状态；Claude 用 dim 绘制输入建议。"""
    result: list[tuple[str, bool]] = []
    dim = False
    pos = 0
    while pos < len(text):
        ansi = _ANSI.match(text, pos)
        if not ansi:
            result.append((text[pos], dim))
            pos += 1
            continue
        sgr = _SGR.fullmatch(ansi.group(0))
        if sgr:
            fields = sgr.group(1).split(";") if ";" in sgr.group(1) else [sgr.group(1)]
            at = 0
            while at < len(fields):
                field = fields[at]
                head = field.split(":", 1)[0]
                try:
                    code = int(head or 0)
                except ValueError:
                    at += 1
                    continue
                if code == 0:
                    dim = False
                elif code == 2:
                    dim = True
                elif code == 22:
                    dim = False
                # RGB/256 色参数里的数字 2 不是 SGR dim。
                if code in {38, 48, 58} and ":" not in field and at + 1 < len(fields):
                    try:
                        mode = int(fields[at + 1] or 0)
                    except ValueError:
                        mode = 0
                    at += 2 if mode == 5 else 4 if mode == 2 else 0
                at += 1
        pos = ansi.end()
    return result


def _composer_upper(line: str) -> bool:
    """Accept Claude's titled upper border even when a narrow pane clips it.

    With a long renamed-session title Claude can consume every leading rule
    glyph and leave only ``title ─``.  The caller still requires a real lower
    rule plus a ``❯`` editor row, so this relaxed upper-only check does not
    turn question menus or transcript prompts into a live composer.
    """
    clean = str(line or "").rstrip()
    return bool(_COMPOSER_RULE.match(clean)
                or (clean.endswith("─") and "─" in clean))


def composer_state(screen: str, cursor: tuple[int, int] | None) -> str:
    """识别 Claude 当前编辑器是 ``empty``、``editing`` 还是 ``unknown``。

    Claude 的建议文本和真实草稿都显示在两条横线之间，不能只看 ``❯`` 后
    是否有字。建议文本是 dim 且真实光标仍停在起点；ESC 回填的草稿是正常
    亮度，光标也会随正文移动。选择题虽然也使用 ``❯``，但没有编辑器横线，
    因而不会被误清空。
    """
    if not cursor:
        return "unknown"
    raw_lines = str(screen or "").replace("\r", "").splitlines()
    clean_lines = [_ANSI.sub("", line) for line in raw_lines]
    cursor_x, cursor_y = cursor
    if cursor_y < 0 or cursor_y >= len(clean_lines):
        return "unknown"

    upper = next((row for row in range(cursor_y, -1, -1)
                  if _composer_upper(clean_lines[row])), None)
    lower = next((row for row in range(cursor_y + 1, len(clean_lines))
                  if _COMPOSER_RULE.match(clean_lines[row])), None)
    if upper is None or lower is None or lower <= upper + 1:
        return "unknown"
    prompt_y = upper + 1
    prompt = clean_lines[prompt_y]
    marker = prompt.find("❯")
    if marker < 0 or prompt[:marker].strip() or not (prompt_y <= cursor_y < lower):
        return "unknown"

    block = "\n".join(raw_lines[prompt_y:lower])
    styled = _styled_chars(block)
    try:
        marker_at = next(i for i, (char, _) in enumerate(styled) if char == "❯")
    except StopIteration:
        return "unknown"
    content = [(char, dim) for char, dim in styled[marker_at + 1:]
               if not char.isspace()]
    if not content:
        return "empty"

    # 光标起点在 marker 后的一个空格/NBSP 后。无色终端会丢失 dim 信息，
    # 此时光标位置仍能分辨静态建议和已经输入的正文。
    at_start = cursor_y == prompt_y and cursor_x <= marker + 2
    has_sgr = bool(_SGR.search(block))
    if has_sgr and any(not dim for _, dim in content):
        return "editing"
    if at_start:
        return "empty"
    return "editing"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text() == data:
            return
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def settings_path() -> str:
    """生成仅供 agenthub 启动的 Claude 进程使用的附加 hook 配置。"""
    script = str(Path(__file__).resolve())
    command = {"type": "command", "command": sys.executable, "args": [script]}
    settings = {
        "hooks": {
            "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [command]}],
            "PostToolUse": [{"matcher": "AskUserQuestion", "hooks": [command]}],
            "PostToolUseFailure": [
                {"matcher": "AskUserQuestion", "hooks": [command]}],
            # 正常退出与随后 resume 都清理未完成的旧对话框；异常退出留下的
            # 状态最迟也会在下次接管时消失。
            "SessionStart": [{"hooks": [command]}],
            "SessionEnd": [{"hooks": [command]}],
        }
    }
    _atomic_json(SETTINGS_FILE, settings)
    return str(SETTINGS_FILE)


def _path(session_id: str) -> Path | None:
    value = str(session_id or "")
    return PROMPT_DIR / f"{value}.json" if _SESSION_ID.fullmatch(value) else None


def _questions(tool_input) -> list[dict]:
    rows = tool_input.get("questions") if isinstance(tool_input, dict) else []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not str(row.get("question") or "").strip():
            continue
        options = []
        for option in row.get("options") or []:
            if isinstance(option, str):
                options.append({"label": option, "description": ""})
            elif isinstance(option, dict) and option.get("label"):
                options.append({
                    "label": str(option["label"]),
                    "description": str(option.get("description") or ""),
                })
        out.append({
            "header": str(row.get("header") or ""),
            "question": str(row["question"]),
            "options": options,
            "multiple": bool(row.get("multiSelect") or row.get("multiple")),
        })
    return out


def clear(session_id: str, tool_use_id: str = "") -> bool:
    path = _path(session_id)
    if not path:
        return False
    if tool_use_id:
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            current = {}
        if current.get("id") and current.get("id") != tool_use_id:
            return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def settle(session_id: str, tool_use_id: str, state: str) -> bool:
    """标记原生对话框已结束，等 transcript 落盘后再由服务端清理。

    Claude resume 会先执行 PostToolUse hook，数秒后才把 tool_use/tool_result
    追加进 JSONL。这里不能在 Post hook 里直接删状态，否则网页会在这段空窗里
    从选择题退回一个虚假的 Working。
    """
    path = _path(session_id)
    if not path or state not in {"submitted", "cancelled"}:
        return False
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return False
    if current.get("id") != tool_use_id:
        return False
    current["state"] = state
    current["settled"] = int(time.time() * 1000)
    _atomic_json(path, current)
    return True


def prompt(session_id: str) -> dict | None:
    path = _path(session_id)
    if not path:
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if value.get("version") != VERSION or not value.get("questions"):
        return None
    return value


def revision(session_id: str):
    path = _path(session_id)
    if not path:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def handle(data: dict) -> None:
    session_id = str(data.get("session_id") or "")
    path = _path(session_id)
    if not path:
        return
    event = str(data.get("hook_event_name") or "")
    if event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        questions = _questions(data.get("tool_input"))
        if questions:
            _atomic_json(path, {
                "version": VERSION,
                "source": "claude",
                "id": str(data.get("tool_use_id") or ""),
                "created": int(time.time() * 1000),
                "state": "waiting",
                "questions": questions,
            })
    elif event == "PostToolUse":
        settle(session_id, str(data.get("tool_use_id") or ""), "submitted")
    elif event == "PostToolUseFailure":
        settle(session_id, str(data.get("tool_use_id") or ""), "cancelled")
    elif event in {"SessionStart", "SessionEnd"}:
        clear(session_id)


def main() -> int:
    try:
        value = json.load(sys.stdin)
        if isinstance(value, dict):
            handle(value)
    except Exception:
        # 桥接失败绝不能影响 Claude 的工具调用；诊断由 agenthub 测试覆盖，
        # 运行时保持无输出并让 Claude 正常继续。
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
