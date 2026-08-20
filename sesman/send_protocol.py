"""Common server-facing interface for CLI-specific send ledgers."""

from __future__ import annotations

from . import claude_queue, send_queue


class SendDriver:
    source = ""

    def snapshot(self, uid: str) -> dict:
        raise NotImplementedError

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        raise NotImplementedError

    def revision(self) -> int:
        raise NotImplementedError


class ClaudeSendDriver(SendDriver):
    source = "claude"

    def snapshot(self, uid: str) -> dict:
        return claude_queue.snapshot(uid)

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        return claude_queue.observe(uid, messages, activity, cursor)

    def revision(self) -> int:
        return claude_queue.revision()


class CodexSendDriver(SendDriver):
    source = "codex"

    def snapshot(self, uid: str) -> dict:
        return send_queue.snapshot(uid)

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        return send_queue.observe(uid, messages, activity, cursor=cursor)

    def revision(self) -> int:
        return send_queue.revision()


DRIVERS = {
    "claude": ClaudeSendDriver(),
    "codex": CodexSendDriver(),
}


def driver_for(source: str) -> SendDriver | None:
    return DRIVERS.get(str(source or ""))


def snapshot(source: str, uid: str) -> dict:
    driver = driver_for(source)
    return driver.snapshot(uid) if driver else {
        "outbox": [], "outbox_version": {"epoch": "none", "revision": 0}}
