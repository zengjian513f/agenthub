import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agenthub import live


class BareClaudeTests(unittest.TestCase):
    def session(self, created_delta=2, cwd="/tmp/project"):
        started = 1_786_268_640.0
        created = datetime.fromtimestamp(started + created_delta, timezone.utc).isoformat()
        return {
            "uid": "claude:test", "source": "claude", "sid": "session-id",
            "cwd": cwd, "created": created, "path": "/tmp/session.jsonl",
        }

    def test_bare_claude_matches_only_nearby_session_in_same_cwd(self):
        cache = {"at": time.monotonic(), "sids": {}, "paths": {},
                 "bare_claude": {123: ("/tmp/project", 1_786_268_640.0)}}
        with patch.dict(live._cache, cache, clear=True):
            self.assertTrue(live.is_live(self.session()))
            self.assertEqual(live.pids_of(self.session()), [123])
            self.assertFalse(live.is_live(self.session(created_delta=60)))
            self.assertFalse(live.is_live(self.session(cwd="/tmp/other")))


class SnapshotTests(unittest.TestCase):
    @staticmethod
    def result(call):
        return ({f"sid-{call}": {call}}, {f"path-{call}": {call}}, {})

    def test_ttl_starts_when_scan_finishes(self):
        clock = [10.0]
        calls = []

        def scan():
            calls.append(len(calls) + 1)
            clock[0] += 5.0
            return self.result(calls[-1])

        empty = {"at": 0.0, "sids": {}, "paths": {}, "bare_claude": {}}
        with patch.dict(live._cache, empty, clear=True), \
                patch.object(live.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(live, "_scan", side_effect=scan):
            first = live.snapshot()
            self.assertEqual(live._cache["at"], 15.0)

            clock[0] = 17.9
            self.assertIs(live.snapshot()[0], first[0])
            self.assertEqual(len(calls), 1)

            clock[0] = 18.1
            live.snapshot()
            self.assertEqual(len(calls), 2)
            self.assertEqual(live._cache["at"], 23.1)

    def test_concurrent_expired_requests_share_one_scan(self):
        worker_count = 8
        ready = threading.Barrier(worker_count + 1)
        scan_started = threading.Event()
        release_scan = threading.Event()
        calls = []
        results = []

        def scan():
            calls.append(1)
            scan_started.set()
            self.assertTrue(release_scan.wait(2))
            return self.result(1)

        def worker():
            ready.wait()
            results.append(live.snapshot())

        empty = {"at": 0.0, "sids": {}, "paths": {}, "bare_claude": {}}
        with patch.dict(live._cache, empty, clear=True), \
                patch.object(live, "_scan", side_effect=scan):
            threads = [threading.Thread(target=worker) for _ in range(worker_count)]
            for thread in threads:
                thread.start()
            ready.wait()
            self.assertTrue(scan_started.wait(2))
            release_scan.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), worker_count)
        self.assertTrue(all(result == results[0] for result in results))


class FakeProc:
    """一棵可控的 /proc 树：只放 _scan 与 _cli_ancestor 会读的文件。"""

    def __init__(self, root: Path):
        self.root = root

    def add(self, pid: int, comm: str, ppid: int, cmdline: str,
            env: dict | None = None, fds: dict[int, str] | None = None):
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 0 0 0\n")
        (d / "environ").write_bytes(b"".join(
            f"{k}={v}".encode() + b"\0" for k, v in (env or {}).items()))
        for fd, target in (fds or {}).items():
            os.symlink(target, d / "fd" / str(fd))


class InheritedSessionEnvTests(unittest.TestCase):
    """CLI 退出后遗留的后台脚本带着 session id，但不是会话的运行实例。"""

    SID_RUNNING = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    SID_STOPPED = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        proc = FakeProc(Path(self.tmp.name))
        proc.add(1, "systemd", 0, "/sbin/init")
        proc.add(4856, "systemd", 1, "/usr/lib/systemd/systemd --user")
        # 还在跑的会话：CLI 主进程 + 它的工具子进程
        proc.add(100, "claude", 4856, f"claude --resume {self.SID_RUNNING}")
        proc.add(101, "bash", 100, "bash /tmp/claude-1000/x/tool.sh",
                 {"CLAUDE_CODE_SESSION_ID": self.SID_RUNNING})
        # 已停止的会话：CLI 没了，setsid 起的守护脚本被 systemd --user 收养
        proc.add(200, "bash", 4856, "bash /tmp/claude-1000/y/dispatch.sh",
                 {"CLAUDE_CODE_SESSION_ID": self.SID_STOPPED})
        proc.add(201, "sleep", 200, "sleep 15",
                 {"CLAUDE_CODE_SESSION_ID": self.SID_STOPPED})
        patches = [
            patch.object(live, "PROC_FS", proc.root),
            patch.object(live, "HAS_PROC", True),
            patch.object(live, "GROK_ACTIVE", proc.root / "missing-grok.json"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def session(self, sid):
        return {"uid": f"claude:{sid[:8]}", "source": "claude", "sid": sid,
                "cwd": "/tmp/project", "created": "2026-09-12T12:00:00+08:00",
                "path": f"/tmp/{sid}.jsonl"}

    def test_orphaned_helper_does_not_keep_a_stopped_session_live(self):
        sids, paths, _bare = live._scan()

        self.assertEqual(sids.get(self.SID_RUNNING), {100})
        self.assertNotIn(self.SID_STOPPED, sids)
        self.assertEqual(paths, {})

        cache = {"at": time.monotonic(), "sids": sids, "paths": paths, "bare_claude": {}}
        with patch.dict(live._cache, cache, clear=True):
            running, stopped = self.session(self.SID_RUNNING), self.session(self.SID_STOPPED)
            uids, owned = live.active_processes([running, stopped])
            self.assertEqual(uids, [running["uid"]])
            self.assertEqual(owned[stopped["uid"]], [])
            self.assertFalse(live.is_live(stopped))
            self.assertEqual(live.pids_of(stopped), [])


class CodexForkOwnershipTests(unittest.TestCase):
    @staticmethod
    def session(uid, sid, path, parent=""):
        return {
            "uid": f"codex:{uid}", "source": "codex", "sid": sid,
            "path": path, "forked_from_id": parent,
        }

    def test_shared_process_belongs_only_to_deepest_fork(self):
        parent = self.session("parent", "sid-parent", "/tmp/parent.jsonl")
        child = self.session("child", "sid-child", "/tmp/child.jsonl", "sid-parent")
        leaf = self.session("leaf", "sid-leaf", "/tmp/leaf.jsonl", "sid-child")
        cache = {
            "at": time.monotonic(), "sids": {},
            "paths": {row["path"]: {123} for row in (parent, child, leaf)},
            "bare_claude": {},
        }
        with patch.dict(live._cache, cache, clear=True):
            uids, owned = live.active_processes([parent, child, leaf])

        self.assertEqual(uids, [leaf["uid"]])
        self.assertEqual(owned, {
            parent["uid"]: [], child["uid"]: [], leaf["uid"]: [123],
        })

    def test_ancestor_keeps_a_separate_process(self):
        parent = self.session("parent", "sid-parent", "/tmp/parent.jsonl")
        child = self.session("child", "sid-child", "/tmp/child.jsonl", "sid-parent")
        cache = {
            "at": time.monotonic(), "sids": {},
            "paths": {parent["path"]: {111, 222}, child["path"]: {222}},
            "bare_claude": {},
        }
        with patch.dict(live._cache, cache, clear=True):
            uids, owned = live.active_processes([parent, child])

        self.assertEqual(uids, [parent["uid"], child["uid"]])
        self.assertEqual(owned[parent["uid"]], [111])
        self.assertEqual(owned[child["uid"]], [222])


class GrokHeadlessLiveTests(unittest.TestCase):
    """Claude 用 grok -p 拉起的 headless 会话：不进 active_sessions.json。"""

    SID_GROK = "01a09418-a82e-7370-9303-9c4fcf0b20c3"
    SID_CLAUDE = "c357894e-ac88-4c9e-823e-3b5a9c19b9c0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.grok_dir = (root / "home" / ".grok" / "sessions"
                         / "%2Ftmp%2Fproject" / self.SID_GROK)
        self.grok_dir.mkdir(parents=True)
        self.events = self.grok_dir / "events.jsonl"
        self.events.write_text("{}\n")
        (self.grok_dir / "chat_history.jsonl").write_text("{}\n")
        proc = FakeProc(root / "proc")
        proc.add(1, "systemd", 0, "/sbin/init")
        proc.add(100, "claude", 1, f"claude --resume {self.SID_CLAUDE}")
        proc.add(200, "grok", 100,
                 "/home/zj/.local/bin/grok -p do the task --cwd /tmp/project",
                 env={"CLAUDE_CODE_SESSION_ID": self.SID_CLAUDE},
                 fds={36: str(self.events)})
        patches = [
            patch.object(live, "PROC_FS", proc.root),
            patch.object(live, "HAS_PROC", True),
            patch.object(live, "GROK_ACTIVE", root / "missing-grok.json"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def grok_session(self):
        return {
            "uid": "grok:headless", "source": "grok", "sid": self.SID_GROK,
            "cwd": "/tmp/project", "path": str(self.grok_dir),
            "created": "2026-09-12T13:30:39+08:00",
        }

    def claude_session(self):
        return {
            "uid": "claude:parent", "source": "claude", "sid": self.SID_CLAUDE,
            "cwd": "/tmp/project", "path": "/tmp/parent.jsonl",
            "created": "2026-09-12T12:00:00+08:00",
        }

    def test_events_jsonl_fd_marks_headless_grok_live(self):
        sids, paths, _bare = live._scan()
        grok, claude = self.grok_session(), self.claude_session()

        self.assertEqual(paths.get(str(self.events)), {200})
        self.assertNotIn(self.SID_GROK, sids)
        self.assertEqual(sids.get(self.SID_CLAUDE), {100})

        cache = {"at": time.monotonic(), "sids": sids, "paths": paths, "bare_claude": {}}
        with patch.dict(live._cache, cache, clear=True):
            self.assertTrue(live.is_live(grok))
            self.assertEqual(live.pids_of(grok), [200])
            uids, owned = live.active_processes([grok, claude])
            self.assertEqual(uids, [grok["uid"], claude["uid"]])
            self.assertEqual(owned[grok["uid"]], [200])
            self.assertEqual(owned[claude["uid"]], [100])

    def test_inherited_claude_session_id_is_not_grok_identity(self):
        sids, _paths, _bare = live._scan()
        self.assertNotIn(200, sids.get(self.SID_CLAUDE, set()))


if __name__ == "__main__":
    unittest.main()
