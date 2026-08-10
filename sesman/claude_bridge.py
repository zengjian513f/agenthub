"""Claude Code 交互态到 sesman 的窄桥接。

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


DATA_DIR = Path.home() / ".local" / "share" / "sesman"
PROMPT_DIR = DATA_DIR / "claude-prompts"
SETTINGS_FILE = DATA_DIR / "claude-bridge-settings.json"
VERSION = 1
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


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
    """生成仅供 sesman 启动的 Claude 进程使用的附加 hook 配置。"""
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
        # 桥接失败绝不能影响 Claude 的工具调用；诊断由 sesman 测试覆盖，
        # 运行时保持无输出并让 Claude 正常继续。
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
