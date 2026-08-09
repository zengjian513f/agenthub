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
        cache = {"at": time.time(), "sids": {}, "paths": {},
                 "bare_claude": {123: ("/tmp/project", 1_786_268_640.0)}}
        with patch.dict(live._cache, cache, clear=True):
            self.assertTrue(live.is_live(self.session()))
            self.assertEqual(live.pids_of(self.session()), [123])
            self.assertFalse(live.is_live(self.session(created_delta=60)))
            self.assertFalse(live.is_live(self.session(cwd="/tmp/other")))


if __name__ == "__main__":
    unittest.main()
