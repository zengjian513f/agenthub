"""Small client for Codex's native per-thread follow-up queue.

The queue API is exposed by ``codex app-server``.  AgentHub deliberately keeps
this adapter stateless: Codex owns the durable queue, while ``send_queue`` owns
the browser request ID and the recovery claim around each RPC.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading


RPC_TIMEOUT = 5.0
LIST_LIMIT = 1000


class QueueError(RuntimeError):
    # True only when the add request may have reached Codex.  The caller must
    # reconcile by clientUserMessageId before it can safely try anything else.
    def __init__(self, message: str, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


def _request(method: str, params: dict, timeout: float = RPC_TIMEOUT) -> dict:
    try:
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as error:
        raise QueueError(f"Codex 原生队列不可用: {error}") from error

    responses: queue.Queue[dict] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get("id") is not None:
                responses.put(message)

    threading.Thread(target=read_responses, daemon=True).start()

    def call(request_id: int, request_method: str, request_params: dict) -> dict:
        assert process.stdin is not None
        request = {"jsonrpc": "2.0", "id": request_id,
                   "method": request_method, "params": request_params}
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        while True:
            try:
                response = responses.get(timeout=timeout)
            except queue.Empty as error:
                raise QueueError("Codex 原生队列响应超时") from error
            if response.get("id") == request_id:
                return response

    try:
        initialized = call(1, "initialize", {
            "clientInfo": {"name": "agenthub", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if initialized.get("error"):
            raise QueueError("Codex app-server 初始化失败")
        assert process.stdin is not None
        process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
        process.stdin.flush()
        response = call(2, method, params)
    except (OSError, subprocess.SubprocessError) as error:
        raise QueueError(f"Codex 原生队列不可用: {error}") from error
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    if response.get("error"):
        error = response["error"]
        detail = error.get("message") if isinstance(error, dict) else error
        raise QueueError(f"Codex 原生队列拒绝请求: {detail}")
    body = response.get("result")
    if not isinstance(body, dict):
        raise QueueError("Codex 原生队列返回了无效结果")
    return body


def list_submissions(thread_id: str) -> list[dict]:
    result = _request("thread/queue/list", {
        "threadId": str(thread_id or ""), "limit": LIST_LIMIT,
    })
    rows = result.get("data")
    if not isinstance(rows, list):
        raise QueueError("Codex 原生队列列表格式无效")
    return [dict(row) for row in rows if isinstance(row, dict) and row.get("id")]


def find_submission(thread_id: str, request_id: str) -> dict | None:
    request_id = str(request_id or "")
    return next((row for row in list_submissions(thread_id)
                 if str(row.get("clientUserMessageId") or "") == request_id), None)


def enqueue_idempotent(thread_id: str, request_id: str, text: str) -> dict:
    """Return the one native row for an AgentHub browser request.

    Codex currently accepts duplicate ``clientUserMessageId`` values, so the
    durable AgentHub claim must serialize this lookup/add sequence.  A restart
    after a lost add response repeats the lookup and recovers the existing row.
    """
    request_id = str(request_id or "")
    existing = find_submission(thread_id, request_id)
    if existing:
        return existing
    try:
        result = _request("thread/queue/add", {
            "threadId": str(thread_id or ""),
            "clientUserMessageId": request_id,
            "input": [{"type": "text", "text": str(text or "")}],
        })
    except QueueError as error:
        # The add ran in a separate app-server process.  If its response was
        # lost, only a later list can prove whether Codex committed the row.
        error.ambiguous = True
        raise
    queued = result.get("queuedSubmission")
    if not isinstance(queued, dict) or not queued.get("id"):
        raise QueueError("Codex 原生队列没有返回队列项 ID", ambiguous=True)
    return dict(queued)


def delete_submission(thread_id: str, submission_id: str) -> bool:
    result = _request("thread/queue/delete", {
        "threadId": str(thread_id or ""),
        "queuedSubmissionId": str(submission_id or ""),
    })
    return result.get("deleted") is True
