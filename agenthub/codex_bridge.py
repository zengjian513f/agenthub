"""Bridge Codex TUI-only approval prompts into agenthub's conversation UI.

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
_OPTION = re.compile(r"^\s*(?:[›»>]\s*)?(\d+)\.\s+(.*)$")
_FOOTER = re.compile(
    r"^\s*Press enter to confirm or esc to cancel\s*$", re.IGNORECASE)
_SHORTCUT = re.compile(r"\s*\((y|p|esc)\)\s*$", re.IGNORECASE)
_CONTEXT_FOOTER = re.compile(r"\bContext\s+\d+%\s+used\b", re.IGNORECASE)
_READY_FOOTER = re.compile(r"\bReady\b", re.IGNORECASE)
_MODEL_FOOTER = re.compile(
    r"^\s*(?:gpt|codex|o\d)[\w.-]*(?:\s+\S+)*\s+·\s+\S.*$", re.IGNORECASE)
_REWIND_FOOTER = re.compile(
    r"^\s*esc again to edit previous message\s*$", re.IGNORECASE)
_BUSY_STATUS = re.compile(r"\bWorking\b.*\besc to interrupt\b", re.IGNORECASE)
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_COMPOSER_MARKERS = {"›", "»"}


def busy_screen(screen: str) -> bool:
    """Whether the visible Codex screen still reports an active turn."""
    return bool(_BUSY_STATUS.search(_ANSI.sub("", str(screen or ""))))


def _styled_chars(text: str) -> list[tuple[str, bool]]:
    """Return visible characters together with their ANSI dim state."""
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
            raw = sgr.group(1)
            # Do not flatten extended colour payloads.  In ``38;2;r;g;b`` the
            # second value selects RGB colour; it is not SGR 2 (dim).  Treating
            # it as dim can make an ordinary coloured draft look like Codex's
            # empty placeholder and would let us append to user text.
            fields = raw.split(";") if ";" in raw else [raw]
            at = 0
            while at < len(fields):
                field = fields[at]
                # Colon-form colour commands keep their payload in one field.
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
                if code in {38, 48, 58} and ":" not in field and at + 1 < len(fields):
                    try:
                        mode = int(fields[at + 1] or 0)
                    except ValueError:
                        mode = 0
                    at += 2 if mode == 5 else 4 if mode == 2 else 0
                at += 1
        pos = ansi.end()
    return result


def composer_state(screen: str, cursor: tuple[int, int] | None = None) -> str:
    """Classify the live Codex composer as ``empty``, ``editing`` or ``unknown``.

    Codex renders its empty rotating placeholder with SGR dim, while restored
    rewind text and normal drafts are not dim.  Normally a live model/status
    footer proves that a nearby marker belongs to the composer.  At short pane
    heights Codex suppresses that footer entirely, so a cursor inside the
    bottom composer block is accepted as the equivalent live-screen proof.
    Recent Codex builds no longer print the old Context/Ready labels, so their
    final ``model · cwd`` status line is also accepted.
    """
    raw_lines = str(screen or "").replace("\r", "").splitlines()
    clean_lines = [_ANSI.sub("", line) for line in raw_lines]
    status: list[tuple[int, int]] = []
    for ready, line in enumerate(clean_lines):
        if not _READY_FOOTER.search(line):
            continue
        contexts = [i for i in range(max(0, ready - 3), ready + 1)
                    if _CONTEXT_FOOTER.search(clean_lines[i])]
        if contexts:
            status.append((ready, contexts[-1]))
    footer: int | None = None
    if status:
        ready, context = status[-1]
        footer = min(ready, context)
        # A narrow terminal may wrap the status bar.  Exclude its whole nonblank
        # block, otherwise model/account text above Ready would look like a draft.
        while footer > 0 and clean_lines[footer - 1].strip():
            footer -= 1
    else:
        nonblank = [i for i, line in enumerate(clean_lines) if line.strip()]
        candidate = nonblank[-1] if nonblank else None
        if candidate is not None and _MODEL_FOOTER.match(clean_lines[candidate]):
            footer = candidate
        elif candidate is not None:
            # Immediately after Esc, Codex temporarily replaces its normal
            # model/cwd footer with a dim "esc again to edit previous message"
            # hint.  The composer above it is already live and accepts a new
            # prompt.  Require both the exact hint and its native dim styling;
            # quoted terminal output with the same English sentence must not
            # turn an arbitrary historic › line into a writable composer.
            rewind_style = [dim for char, dim in _styled_chars(raw_lines[candidate])
                            if not char.isspace()]
            if (_REWIND_FOOTER.match(clean_lines[candidate])
                    and rewind_style and all(rewind_style)):
                footer = candidate

    if footer is not None:
        # Only inspect the nonblank block immediately above the status bar.  The
        # former 20-line search could reach into transcript history when a resize
        # caught Codex between clearing and redrawing its composer.  A historic
        # ``› user prompt`` was then reported as a live draft, permanently blocking
        # the web outbox even though the visible composer was empty.
        end = footer - 1
        while end >= 0 and not clean_lines[end].strip():
            end -= 1
        if end < 0:
            return "unknown"
        start = end
        while start > 0 and clean_lines[start - 1].strip():
            start -= 1
    else:
        # The 46x17 frame in BUG-20260829-164614-566cac ended at the live
        # ``› Ask Codex to do anything`` row and omitted the status bar.  Use
        # tmux's visible-screen cursor to identify that exact block; without a
        # cursor, transcript text remains untrusted.
        try:
            cursor_x, cursor_y = (int(value) for value in cursor)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return "unknown"
        if not (0 <= cursor_y < len(clean_lines)):
            return "unknown"
        if clean_lines[cursor_y].strip():
            start = end = cursor_y
            while start > 0 and clean_lines[start - 1].strip():
                start -= 1
            while end + 1 < len(clean_lines) and clean_lines[end + 1].strip():
                end += 1
        else:
            # Codex 0.150.1 can render a footerless composer on the physical
            # bottom row while parking tmux's cursor two blank rows above it.
            # Accept only that tightly anchored layout: an initial-column
            # cursor, one or two blank rows before a nonblank bottom block.
            # This keeps an old styled prompt elsewhere in the transcript from
            # authorising terminal input.
            end = len(clean_lines) - 1
            if (not clean_lines[end].strip()
                    or not 1 <= end - cursor_y <= 2
                    or any(clean_lines[i].strip()
                           for i in range(cursor_y, end))):
                return "unknown"
            start = end
            while start > 0 and clean_lines[start - 1].strip():
                start -= 1
        marker_col = len(clean_lines[start]) - len(clean_lines[start].lstrip())
        if cursor_x <= marker_col:
            return "unknown"
        if cursor_y < start and cursor_x != marker_col + 2:
            return "unknown"
        # A working frame can also be too short to show the footer.  Prefer a
        # conservative retry over injecting while any nearby live busy marker
        # is visible.
        if any(_BUSY_STATUS.search(clean_lines[i])
               for i in range(max(0, start - 6), end + 1)):
            return "unknown"
    if clean_lines[start].lstrip()[:1] not in _COMPOSER_MARKERS:
        return "unknown"
    if any(_BUSY_STATUS.search(clean_lines[i])
           for i in range(max(0, start - 6), start)):
        return "unknown"

    styled = _styled_chars("\n".join(raw_lines[start:end + 1]))
    try:
        marker = next(i for i, (char, _) in enumerate(styled)
                      if char in _COMPOSER_MARKERS)
    except StopIteration:
        return "unknown"
    content = [(char, dim) for char, dim in styled[marker + 1:] if not char.isspace()]
    if any(not dim for _, dim in content):
        return "editing"
    return "empty"


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
