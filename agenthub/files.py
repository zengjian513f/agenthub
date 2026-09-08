"""Read files explicitly referenced by a session, without a browser path API."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import media

MAX_BYTES = media.MAX_ITEM
# Bare prose stops at punctuation; quoted/Markdown paths may contain spaces.
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_@/:.-])(?:https?://[^\s<>`\"']+|"
    r"(?:~/|\.\.?/|/|[A-Za-z0-9_.-]+/)[^\s<>`\"'，。；、！？()\[\]{}]+|"
    r"[A-Za-z0-9_.-]+\.[a-zA-Z][\w.-]*(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)?)", re.I)
_QUOTED = re.compile(
    r'`([^`\n]+)`|\]\(\s*(<[^>]+>|(?:[^\s()]|\([^\s()]*\))+)'
    r'(?:\s+["\'][^"\']*["\'])?\s*\)')
_LINE = re.compile(r"(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)$")


def clean_ref(value: str) -> str:
    return _LINE.sub("", value.strip().strip("<>"))


def references(messages: list[dict]) -> set[str]:
    refs = set()

    def scan(value):
        if isinstance(value, dict):
            for item in value.values():
                scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)
        elif isinstance(value, str):
            # Structured tool arguments preserve paths containing spaces.
            if value.startswith(("/", "~/", "./", "../")) and "\n" not in value:
                refs.add(clean_ref(value))
            for match in _QUOTED.finditer(value):
                refs.add(clean_ref(match[1] or match[2]))
            for match in _TOKEN.finditer(value):
                raw = match[0].rstrip(".,;:!?")
                if not raw.lower().startswith(("http://", "https://")):
                    refs.add(clean_ref(raw))

    for message in messages:
        text = message.get("text", "")
        scan(text)
        if message.get("role") == "tool":
            try:
                scan(json.loads(text))
            except (ValueError, TypeError):
                pass
            scan(message.get("args"))
    return refs


def resolve(messages: list[dict], cwd: str, ref: str) -> Path:
    """Resolve an exact reference or an unambiguous basename mentioned earlier.

    Never search the filesystem recursively or accept an unmentioned request.
    Use the complete selected branch so initial windows and SSE behave alike.
    """
    return _resolve(references(messages), cwd, ref)


def resolve_many(messages: list[dict], cwd: str, requested: list[str]) -> dict[str, str]:
    refs = references(messages)
    resolved = {}
    directories = None
    for ref in dict.fromkeys(requested):
        try:
            if clean_ref(ref) in refs and '/' not in clean_ref(ref) and directories is None:
                directories = _referenced_directories(refs, cwd)
            resolved[ref] = str(_resolve(refs, cwd, ref, directories))
        except (ValueError, FileNotFoundError):
            pass  # Missing/ambiguous references stay ordinary text in the UI.
    return resolved


def _existing(raw: str, cwd: str) -> Path | None:
    if not raw or "://" in raw or "\x00" in raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            if not cwd or not Path(cwd).is_absolute():
                return None
            path = Path(cwd) / path
        path = path.resolve(strict=True)
        return path if path.is_file() or path.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _referenced_directories(refs: set[str], cwd: str) -> set[Path]:
    return {p for raw in refs if '/' in raw
            if (p := _existing(raw, cwd)) is not None and p.is_dir()}


def _resolve(refs: set[str], cwd: str, ref: str,
             directories: set[Path] | None = None) -> Path:
    if not ref or len(ref) > 4096 or "\x00" in ref:
        raise ValueError("无效的文件引用")
    ref = clean_ref(ref)
    if ref not in refs:
        raise FileNotFoundError("该路径未出现在此会话中")

    # Explicit paths have one meaning. Short names may refer to tool output in
    # a subdirectory; refuse collisions instead of opening an unrelated file.
    candidates = {ref} if "/" in ref else {r for r in refs if Path(r).name == ref}
    paths = {p for raw in candidates if (p := _existing(raw, cwd)) is not None}
    if '/' not in ref and Path(ref).name == ref:
        # A reply may name files inside a newly cloned/output directory while
        # the session cwd stays at its parent. Check only direct children of
        # directories explicitly recorded in the session, never recurse.
        if directories is None:
            directories = _referenced_directories(refs, cwd)
        paths.update(p for directory in directories
                     if (p := _existing(str(directory / ref), cwd)) is not None)
    if len(paths) > 1:
        raise ValueError("会话中有多个同名文件，请点击完整路径")
    if not paths:
        raise FileNotFoundError("文件不存在，或会话未记录其完整路径")
    return paths.pop()


def read(path: Path) -> tuple[bytes, str, dict]:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "sandbox; default-src 'none'"}
    if path.is_dir():
        # A directory link is a bounded listing, not an unrestricted file browser.
        entries = []
        for item in path.iterdir():
            entries.append(item.name + ("/" if item.is_dir() else ""))
            if len(entries) >= 2000:
                entries.append("…（最多显示 2000 项）")
                break
        listing = str(path) + "/\n\n" + "\n".join(sorted(entries))
        return listing.encode(), "text/plain; charset=utf-8", headers
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("文件超过 32 MiB，无法在网页中打开")
    mime = media._mime(None, path.name)
    if mime:
        return data, mime, headers
    try:
        data.decode("utf-8")
        is_text = b"\x00" not in data
    except UnicodeDecodeError:
        is_text = False
    if is_text:
        # HTML/SVG/scripts are displayed as text and never execute on our origin.
        return data, "text/plain; charset=utf-8", headers
    name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return data, "application/octet-stream", headers
