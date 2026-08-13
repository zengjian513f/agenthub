import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sesman import live


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


if __name__ == "__main__":
    unittest.main()
