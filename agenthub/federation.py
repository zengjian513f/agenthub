"""Node identity and the Hub's wire namespace. Native CLI IDs never change."""
from __future__ import annotations

import os
import re
import secrets
import threading
from pathlib import Path

PROTOCOL = 1
_lock = threading.Lock()


def identity(path: Path | None = None) -> str:
    path = path or Path.home() / ".local/share/agenthub/node-id"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text().strip()
        else:
            value = secrets.token_hex(16)
            with os.fdopen(fd, "w") as out:
                out.write(value + "\n")
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("invalid node identity file")
        return value


def qualify(node: str, value: str, uid: bool = False) -> str:
    if not value:
        return value
    if uid:
        source, sep, tail = value.partition(":")
        if not sep:
            raise ValueError("invalid session reference")
        return f"{source}:{node}~{tail}"
    return f"{node}~{value}"


def split(value: str, uid: bool = False) -> tuple[str, str]:
    prefix = ""
    if uid:
        source, sep, value = value.partition(":")
        if not sep:
            raise ValueError("missing session source")
        prefix = source + ":"
    node, sep, local = value.partition("~")
    if not sep or not re.fullmatch(r"[a-f0-9]{32}", node) or not local:
        raise ValueError("missing or invalid machine reference")
    return node, prefix + local


def public_payload(data, node: dict, path: str):
    """Transform protocol fields, never text, tool arguments or native message IDs."""
    nid = node["id"]

    def walk(obj, key=""):
        if isinstance(obj, list):
            return [walk(v, key) for v in obj]
        if not isinstance(obj, dict):
            return obj
        result = {}
        for k, v in obj.items():
            if k in {"uid", "from_uid", "to_uid"} and isinstance(v, str) and v:
                result[k] = qualify(nid, v, True)
            elif k == "src" and isinstance(v, str) and v.startswith("/api/media/"):
                result[k] = f"/api/nodes/{nid}{v}"
            elif k == "epoch" and isinstance(v, str):
                result[k] = qualify(nid, v)
            elif k in {"data", "content", "input", "arguments", "raw"}:
                result[k] = v
            else:
                result[k] = walk(v, k)
        return result

    result = walk(data)
    if not isinstance(result, dict):
        return result

    def row(item, terminal=False, trash=False):
        item.update(node_id=nid, node_name=node["name"])
        if terminal and item.get("name"):
            item["name"] = qualify(nid, item["name"])
        if trash and item.get("id"):
            item["id"] = qualify(nid, item["id"])
        return item

    if path in {"/api/sessions", "/api/search"}:
        for item in result.get("sessions", result.get("results", [])):
            row(item)
    if path == "/api/live":
        for key in ("uids", "tmux_uids"):
            result[key] = [qualify(nid, v, True) for v in data.get(key, [])]
        result["started_at"] = {qualify(nid, k, True): v
                                for k, v in data.get("started_at", {}).items()}
    if path == "/api/term/list":
        for key in ("sessions", "pending"):
            for item in result.get(key, []):
                row(item, terminal=True)
    if path in {"/api/term/create", "/api/term/takeover", "/api/term/new-status"}:
        row(result, terminal=True)
        if result.get("session"):
            row(result["session"])
    if path == "/api/bug-report" and result.get("worker"):
        row(result["worker"], terminal=True)
    if path == "/api/trash":
        for item in result.get("items", []):
            row(item, trash=True)
    if (path.startswith("/api/messages/") or path == "/api/watch") and result.get("meta"):
        row(result["meta"])
    return result
