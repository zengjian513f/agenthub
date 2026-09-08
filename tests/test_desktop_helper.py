import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location('desktop_helper',
    Path(__file__).resolve().parents[1] / 'agenthub/static/desktop-helper.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / 'mounted'
        self.local.mkdir()
        (self.local / 'report.txt').write_text('fixture')
        (self.local / 'folder').mkdir()
        self.config = {'token': 'fixture-key', 'origins': ['https://hub.example.com'],
                       'mappings': [{'node': 'a' * 32, 'remote': '/remote', 'local': str(self.local)}]}
        self.target = {'node': 'a' * 32, 'path': '/remote/report.txt', 'kind': 'file'}

    def test_mapping_requires_identity_existence_and_boundary(self):
        self.assertEqual(desktop.local_target(self.config, self.target), self.local / 'report.txt')
        for change in [{'node': 'b' * 32}, {'path': '/remote-other/report.txt'},
                       {'path': '/remote/../report.txt'}, {'path': '/remote/missing.txt'},
                       {'path': 'file:///remote/report.txt'}, {'kind': 'directory'},
                       {'path': '/remote/C:report.txt'}, {'path': '/remote/..\\report.txt'}]:
            with self.subTest(change=change), self.assertRaises((ValueError, OSError)):
                desktop.local_target(self.config, {**self.target, **change})
        outside = self.root / 'outside.txt'
        outside.write_text('private')
        (self.local / 'escape.txt').symlink_to(outside)
        with self.assertRaises(ValueError):
            desktop.local_target(self.config, {**self.target, 'path': '/remote/escape.txt'})

    def test_http_checks_origin_key_and_typed_open_actions(self):
        service = ThreadingHTTPServer(('127.0.0.1', 0), desktop.Handler)
        service.config = self.config
        opened = []
        service.open_target = opened.append
        threading.Thread(target=service.serve_forever, daemon=True).start()
        self.addCleanup(service.server_close)
        self.addCleanup(service.shutdown)
        url = f'http://127.0.0.1:{service.server_port}/open'

        def send(body, headers=None):
            req = Request(url, data=json.dumps(body).encode(), headers=headers or {
                'Origin': self.config['origins'][0], 'Authorization': 'Bearer fixture-key',
                'Content-Type': 'application/json'})
            try:
                with urlopen(req) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)

        self.assertEqual(send({**self.target, 'action': 'open-local'})[0], 200)
        self.assertEqual(opened[-1], self.local / 'report.txt')
        self.assertEqual(send({**self.target, 'action': 'open-directory'})[0], 200)
        self.assertEqual(opened[-1], self.local)
        directory = {**self.target, 'path': '/remote/folder', 'kind': 'directory'}
        self.assertEqual(send({**directory, 'action': 'open-directory'})[0], 200)
        self.assertEqual(opened[-1], self.local / 'folder')
        for headers, status in [({'Origin': 'https://evil.example', 'Authorization': 'Bearer fixture-key'}, 403),
                                ({'Origin': self.config['origins'][0]}, 401),
                                ({'Authorization': 'Bearer fixture-key'}, 403)]:
            self.assertEqual(send(self.target, headers)[0], status)
        self.assertEqual(send({**self.target, 'action': 'run-command'})[0], 400)
        (self.local / 'run.exe').write_bytes(b'fixture')
        self.assertEqual(send({**self.target, 'path': '/remote/run.exe', 'action': 'open-local'})[0], 400)
        self.assertEqual(len(opened), 3)


if __name__ == '__main__':
    unittest.main()
