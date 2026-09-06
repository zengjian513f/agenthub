"""Common server-facing interface for CLI-specific send ledgers."""

from __future__ import annotations

import hashlib
import time

from . import (claude_bridge, claude_queue, codex_bridge, send_queue, term)


class SendDriver:
    source = ""

    def snapshot(self, uid: str) -> dict:
        raise NotImplementedError

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        raise NotImplementedError

    def revision(self) -> int:
        raise NotImplementedError

    def composer_snapshot(self, name: str) -> tuple[str, tuple[int, int] | None]:
        raise NotImplementedError

    def composer_state(self, screen: str, cursor: tuple[int, int] | None) -> str:
        raise NotImplementedError

    def clear_composer(self, name: str) -> None:
        raise NotImplementedError

    def busy_screen(self, screen: str) -> bool:
        raise NotImplementedError

    def terminal_probe(self, name: str) -> dict:
        screen, cursor = self.composer_snapshot(name)
        return {
            "draft_state": self.composer_state(screen, cursor),
            "busy": self.busy_screen(screen),
        }

    def mark_interrupted(self, uid: str, restored: bool = False) -> bool:
        raise NotImplementedError

    def composer_probe(self, name: str) -> dict:
        """只返回草稿状态和不可逆指纹，不把终端正文暴露给浏览器。"""
        try:
            screen, cursor = self.composer_snapshot(name)
        except (OSError, RuntimeError, ValueError, KeyError):
            return {"draft_state": "unknown"}
        state = self.composer_state(screen, cursor)
        result = {"draft_state": state}
        if state == "editing":
            fingerprint = screen if cursor is None else (
                f"{cursor[0]}\0{cursor[1]}\0{screen}")
            result.update(
                draft_conflict=True,
                draft_token=hashlib.sha256(
                    fingerprint.encode("utf-8", "replace")).hexdigest(),
            )
        return result

    def overwrite_draft(self, name: str, expected_token: str) -> dict | None:
        """只清空用户刚确认的那一版草稿，并等原生编辑器确实变空。"""
        probe = self.composer_probe(name)
        if probe.get("draft_state") != "editing":
            return None
        if not expected_token or expected_token != probe.get("draft_token"):
            return probe

        term.leave_copy_mode(name)
        self.clear_composer(name)
        for delay in (0.03, 0.05, 0.08, 0.13, 0.21):
            time.sleep(delay)
            if self.composer_probe(name).get("draft_state") == "empty":
                return None
        return {"error": "未能确认终端草稿已清空，消息未发送"}


class ClaudeSendDriver(SendDriver):
    source = "claude"

    def snapshot(self, uid: str) -> dict:
        return claude_queue.snapshot(uid)

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        return claude_queue.observe(uid, messages, activity, cursor)

    def revision(self) -> int:
        return claude_queue.revision()

    def composer_snapshot(self, name: str) -> tuple[str, tuple[int, int]]:
        # 只取可见物理屏，使 cursor_y 与行号保持一致；历史里的旧 ❯ 不参与。
        return term.capture_screen(name), term.cursor_position(name)

    def composer_state(self, screen: str, cursor: tuple[int, int] | None) -> str:
        return claude_bridge.composer_state(screen, cursor)

    def clear_composer(self, name: str) -> None:
        # Ctrl-C exits Claude when it is already idle.  Clear both sides of the
        # cursor with line-editor keys instead, then let overwrite_draft verify
        # the actual empty frame before any new prompt is persisted.
        term.send_keys(name, "C-u", "C-k")

    def busy_screen(self, screen: str) -> bool:
        return claude_bridge.busy_screen(screen)

    def mark_interrupted(self, uid: str, restored: bool = False) -> bool:
        return claude_queue.mark_interrupted(uid, restored)


class CodexSendDriver(SendDriver):
    source = "codex"

    def snapshot(self, uid: str) -> dict:
        return send_queue.snapshot(uid)

    def observe(self, uid: str, messages: list[dict], activity: dict | None,
                cursor: dict | None = None) -> bool:
        return send_queue.observe(uid, messages, activity, cursor=cursor)

    def revision(self) -> int:
        return send_queue.revision()

    def composer_snapshot(self, name: str) -> tuple[str, tuple[int, int]]:
        # 只取可见物理屏，让光标行与输入块对齐。短窗口中 Codex
        # 会完全隐藏 model/status footer，此时光标是区分实时 composer
        # 与历史 ``›`` 文本的必要证据。
        return term.capture_screen_state(name)

    def composer_state(self, screen: str, cursor: tuple[int, int] | None) -> str:
        return codex_bridge.composer_state(screen, cursor)

    def clear_composer(self, name: str) -> None:
        term.send_keys(name, "C-u", "C-k")

    def busy_screen(self, screen: str) -> bool:
        return codex_bridge.busy_screen(screen)

    def mark_interrupted(self, uid: str, restored: bool = False) -> bool:
        return send_queue.mark_interrupted(uid)


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
