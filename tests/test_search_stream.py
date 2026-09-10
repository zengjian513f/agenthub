import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from agenthub import server


class SearchStreamTests(unittest.TestCase):
    @staticmethod
    def handler(output):
        handler = object.__new__(server.Handler)
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.wfile = output
        return handler

    def test_large_file_has_heartbeats_and_early_matches(self):
        output = io.BytesIO()
        row = {'uid': 'codex:test', 'source': 'codex', 'title': 'needle'}

        def search(query, sources, progress, matches, **opts):
            progress(0, 1)
            matches([row])
            time.sleep(1.1)
            return {'results': [row], 'total_pool': 1, 'truncated': False}

        with patch.object(server.index, 'search', side_effect=search), \
                patch.object(server.index, 'cached', return_value=[row]), \
                patch.object(server.session_meta, 'enrich', side_effect=lambda rows, _: rows):
            self.handler(output)._search_stream('needle', None)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([e['type'] for e in events], ['progress', 'matches', 'heartbeat', 'result'])
        self.assertEqual(events[1]['results'], events[-1]['data']['results'])

    def test_disconnected_browser_stops_scan_at_next_progress(self):
        written = threading.Event()
        finished = threading.Event()
        scanned = []

        class Disconnected:
            def write(self, data):
                written.set()
                raise BrokenPipeError()

        def search(query, sources, progress, **opts):
            try:
                progress(0, 100)
                written.wait(2)
                # Allow the serving thread to observe the disconnect.
                time.sleep(.02)
                for done in range(100):
                    progress(done, 100)
                    scanned.append(done)
            finally:
                finished.set()

        with patch.object(server.index, 'search', side_effect=search):
            self.handler(Disconnected())._search_stream('needle', None)
            self.assertTrue(finished.wait(2))
        self.assertEqual(scanned, [])


if __name__ == '__main__':
    unittest.main()
