"""Durable creation receipts. An interrupted launch is never blindly repeated."""
import hashlib
import json
import os
import re
import threading
from pathlib import Path

DATA_DIR = Path.home() / ".local/share/agenthub/create-requests"
_lock = threading.RLock()


def run(body, execute):
    """execute returns (HTTP status, JSON). Persist the receipt before replying."""
    request = str(body.get("request_id") or "")
    if not request:
        return execute()  # Existing local clients remain compatible.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request):
        return 400, {"error": "invalid creation request ID"}
    spec = {k: body.get(k) for k in ("source", "cwd", "cols", "rows", "create_cwd")}
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    # Directory confirmation is a separate, explicit phase of the same request.
    key = request + ("-mkdir" if body.get("create_cwd") is True else "")
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / (key + ".json")
        if path.exists():
            prior = json.loads(path.read_text())
            if prior["fingerprint"] != fingerprint:
                return 409, {"error": "创建请求 ID 已用于另一组参数"}
            if "response" in prior:
                return prior["status"], prior["response"]
            return 409, {"error": "上次创建结果待核对，请检查会话列表和目标机器，不能自动重复启动"}
        def write(data):
            temp = path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"fingerprint": fingerprint, **data}, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            temp.replace(path)
            directory = os.open(DATA_DIR, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        write({"state": "started"})
        status, response = execute()
        write({"state": "complete", "status": status, "response": response})
        return status, response
