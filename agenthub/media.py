"""会话图片的受限、延迟加载媒体仓库。

消息 API 只返回短 token，不把数百 KB 的 base64 跟着整段会话反复传输。
本地路径只有在确实出现在会话文本且文件存在时才注册；浏览器拿不到原始路径，
也不能靠构造 URL 任意读取文件。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote, urlparse


MAX_ITEM = 32 * 1024 * 1024
MAX_MEMORY = 128 * 1024 * 1024
MAX_ENTRIES = 512
ALLOWED_MIMES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "image/avif", "image/bmp",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp"}

_lock = threading.Lock()
_items: OrderedDict[str, dict] = OrderedDict()
_memory = 0


def _mime(v: str | None, name: str = "") -> str | None:
    m = (v or "").split(";", 1)[0].strip().lower()
    if m == "image/jpg":
        m = "image/jpeg"
    if not m and name:
        m = (mimetypes.guess_type(name)[0] or "").lower()
    return m if m in ALLOWED_MIMES else None


def _put(token: str, item: dict) -> str:
    global _memory
    with _lock:
        old = _items.pop(token, None)
        if old:
            _memory -= old.get("memory", 0)
        _items[token] = item
        _memory += item.get("memory", 0)
        while len(_items) > MAX_ENTRIES or _memory > MAX_MEMORY:
            _, gone = _items.popitem(last=False)
            _memory -= gone.get("memory", 0)
    return token


def register_bytes(data: bytes, mime: str, name: str = "图片") -> dict | None:
    mime = _mime(mime, name)
    if not mime or not data or len(data) > MAX_ITEM:
        return None
    token = hashlib.sha256(mime.encode() + b"\0" + data).hexdigest()[:32]
    _put(token, {"mime": mime, "data": data, "memory": len(data), "name": name})
    return {"src": f"/api/media/{token}", "mime": mime, "alt": name}


def register_base64(data: str, mime: str | None, name: str = "图片") -> dict | None:
    if not isinstance(data, str) or len(data) > (MAX_ITEM * 4 // 3 + 16):
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None
    return register_bytes(raw, mime or "", name)


def register_path(value: str, cwd: str | None = None, name: str = "图片") -> dict | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = unquote(value.strip().strip("<>"))
    if raw.startswith("file://"):
        raw = urlparse(raw).path
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if not cwd or not str(cwd).startswith("/"):
            return None
        p = Path(cwd) / p
    try:
        p = p.resolve(strict=True)
        st = p.stat()
    except (OSError, RuntimeError):
        return None
    mime = _mime(None, p.name)
    if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS or not mime or st.st_size > MAX_ITEM:
        return None
    key = f"{p}\0{st.st_mtime_ns}\0{st.st_size}".encode()
    token = hashlib.sha256(key).hexdigest()[:32]
    _put(token, {"mime": mime, "path": str(p), "size": st.st_size,
                 "memory": 0, "name": name or p.name})
    return {"src": f"/api/media/{token}", "mime": mime, "alt": name or p.name}


def _remote(url: str, name: str = "图片") -> dict | None:
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    return {"src": url, "mime": "", "alt": name, "external": True}


def from_block(block: dict) -> dict | None:
    """识别 Claude/OpenAI/Grok 常见的结构化图片块。"""
    if not isinstance(block, dict):
        return None
    for obj in (block.get("source"), block.get("file"), block):
        if not isinstance(obj, dict):
            continue
        data = obj.get("data") or obj.get("base64")
        mime = obj.get("media_type") or obj.get("mime_type") or obj.get("mimeType") or obj.get("type")
        if data:
            got = register_base64(data, mime, block.get("name") or "会话图片")
            if got:
                dims = obj.get("dimensions") or block.get("dimensions")
                if isinstance(dims, dict):
                    got["width"] = dims.get("originalWidth") or dims.get("width")
                    got["height"] = dims.get("originalHeight") or dims.get("height")
                return got
        url = obj.get("url") or obj.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str):
            if url.startswith("data:image/") and ";base64," in url:
                head, payload = url.split(",", 1)
                return register_base64(payload, head[5:].split(";", 1)[0], "会话图片")
            got = _remote(url, "会话图片")
            if got:
                return got
            got = register_path(url, name="会话图片")
            if got:
                return got
    image_url = block.get("image_url")
    if isinstance(image_url, str):
        return _remote(image_url, "会话图片") or register_path(image_url, name="会话图片")
    return None


_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+["\'][^"\']*["\'])?\s*\)')
_RAW_PATH = re.compile(
    r'(?<![\w:/])((?:~|/|\.\.?/)[^\s<>"\'`]+?\.(?:png|jpe?g|gif|webp|avif|bmp))'
    r'(?=$|[\s),;:])', re.I)
_FENCE = re.compile(r"```.*?```", re.S)


def discover(text: str, cwd: str | None, *, allow_raw_paths: bool = True) -> list[dict]:
    """发现 Markdown 图片，以及对话正文中明确写出的本地图片路径。"""
    if not isinstance(text, str) or not text:
        return []
    scan = _FENCE.sub("", text)
    out, seen = [], set()
    markdown_spans = []
    for m in _MD_IMAGE.finditer(scan):
        markdown_spans.append(m.span())
        alt, ref = m.group(1).strip() or "图片", m.group(2).strip().strip("<>")
        got = _remote(ref, alt) or register_path(ref, cwd, alt)
        if got and got["src"] not in seen:
            got["ref"] = ref
            out.append(got)
            seen.add(got["src"])
    if allow_raw_paths:
        for m in _RAW_PATH.finditer(scan):
            if any(lo <= m.start() < hi for lo, hi in markdown_spans):
                continue
            ref = m.group(1).rstrip(".")
            got = register_path(ref, cwd, Path(unquote(ref)).name)
            if got and got["src"] not in seen:
                got["gallery"] = True
                out.append(got)
                seen.add(got["src"])
    return out


def enrich_message(msg: dict, cwd: str | None) -> None:
    role = str(msg.get("role") or "")
    # shell/ls/find/grep 的输出只是数据，路径以 .png 结尾不代表“请展示图片”。
    # 工具若真的返回图片，应走 adapter 已解析的结构化 media；显式 Markdown
    # 图片仍可发现。只有用户/助手自然语言正文才兼容历史上的裸路径写法。
    chat_text = role in {"user", "assistant", "command"} or role.endswith("·subagent")
    found = discover(msg.get("text", ""), cwd, allow_raw_paths=chat_text)
    if not found:
        return
    media = msg.setdefault("media", [])
    have = {x.get("src") for x in media}
    media.extend(x for x in found if x.get("src") not in have)


def get(token: str) -> tuple[bytes, str, str] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return None
    with _lock:
        item = _items.get(token)
        if not item:
            return None
        _items.move_to_end(token)
        item = dict(item)
    if "data" in item:
        return item["data"], item["mime"], item.get("name", "image")
    try:
        p = Path(item["path"])
        if p.stat().st_size > MAX_ITEM:
            return None
        return p.read_bytes(), item["mime"], item.get("name", p.name)
    except OSError:
        return None
