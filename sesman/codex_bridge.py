"""Bridge Codex TUI-only approval prompts into sesman's conversation UI.

Codex writes ``request_user_input`` questions to its rollout JSONL, but command
approvals live only in the terminal screen.  Keep this parser deliberately
strict: ordinary command output must never be mistaken for an actionable
prompt.
"""

from __future__ import annotations

import hashlib
import re


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_HEADING = re.compile(r"^\s*(Would you like to .+\?)\s*$", re.IGNORECASE)
_OPTION = re.compile(r"^\s*(?:[›>]\s*)?(\d+)\.\s+(.*)$")
_FOOTER = re.compile(
    r"^\s*Press enter to confirm or esc to cancel\s*$", re.IGNORECASE)
_SHORTCUT = re.compile(r"\s*\((y|p|esc)\)\s*$", re.IGNORECASE)


def _option_text(text: str, key: str) -> tuple[str, str]:
    """Use compact Chinese labels while preserving Codex's actual meaning."""
    folded = " ".join(text.split())
    low = folded.lower()
    if key == "y" or low.startswith("yes, proceed"):
        return "允许本次", "运行这条命令"
    if key == "p" or "don't ask again" in low:
        return "始终允许", "以后不再询问这个命令前缀"
    if key == "esc" or low.startswith("no,"):
        return "拒绝", "取消命令，并告诉 Codex 调整方案"
    return folded, ""


def approval_prompt(screen: str) -> dict | None:
    """Return a live-question payload for the approval at the screen tail.

    The exact footer and shortcut-bearing numbered options are required.  This
    guards against a transcript that merely happens to quote the heading.
    """
    clean = _ANSI.sub("", str(screen or "")).replace("\r", "")
    lines = clean.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    if not starts:
        return None
    start = starts[-1]
    block = lines[start:]
    footer = next((i for i, line in enumerate(block) if _FOOTER.match(line)), -1)
    if footer < 0 or any(line.strip() for line in block[footer + 1:]):
        return None
    block = block[:footer + 1]

    option_starts = [(i, match) for i, line in enumerate(block)
                     if (match := _OPTION.match(line))]
    if len(option_starts) < 2:
        return None

    options: list[dict] = []
    for pos, (line_index, match) in enumerate(option_starts):
        end = option_starts[pos + 1][0] if pos + 1 < len(option_starts) else footer
        raw = " ".join([match.group(2).strip(), *(
            line.strip() for line in block[line_index + 1:end] if line.strip()
        )]).strip()
        shortcut = _SHORTCUT.search(raw)
        if not shortcut:
            return None
        key = shortcut.group(1).lower()
        raw = _SHORTCUT.sub("", raw).strip()
        label, description = _option_text(raw, key)
        option = {"label": label, "key": "Escape" if key == "esc" else key}
        if description:
            option["description"] = description
        options.append(option)

    # Current Codex approvals always expose a positive and a negative path.
    # Requiring both further reduces false positives from printed text.
    keys = {option["key"] for option in options}
    if "Escape" not in keys or not ({"y", "p"} & keys):
        return None

    heading = _HEADING.match(block[0]).group(1)  # guarded above
    environment = ""
    command_lines: list[str] = []
    first_option = option_starts[0][0]
    in_command = False
    for line in block[1:first_option]:
        stripped = line.strip()
        if stripped.lower().startswith("environment:"):
            environment = stripped.split(":", 1)[1].strip()
        if stripped.startswith("$"):
            in_command = True
            command_lines.append(stripped)
        elif in_command and stripped:
            command_lines.append(stripped)
    command = " ".join(command_lines)
    question = "是否运行以下命令？" if "run the following command" in heading.lower() else heading
    if command:
        question += f"\n\n{command}"
    digest_source = "\n".join([heading, environment, command, *(
        f"{option['key']}:{option['label']}" for option in options
    )])
    prompt_id = hashlib.sha256(digest_source.encode()).hexdigest()[:16]
    return {
        "id": f"codex-approval:{prompt_id}",
        "state": "waiting",
        "kind": "approval",
        "questions": [{
            "header": f"命令审批 · {environment}" if environment else "命令审批",
            "question": question,
            "options": options,
            "multiple": False,
        }],
    }
