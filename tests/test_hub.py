import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agenthub import create_requests, federation, hub, server
from hub_fixture import start_node, stop


class FederationTests(unittest.TestCase):
    def test_file_metadata_does_not_rewrite_reference_names(self):
        payload = {'resolved': {'uid': '/project/uid', 'epoch': '/project/epoch'},
                   'targets': [{'ref': 'uid', 'path': '/project/uid', 'kind': 'file'}],
                   'node_id': 'a' * 32}
        self.assertEqual(federation.public_payload(payload, {'id': 'a' * 32, 'name': 'A'},
                                                  '/api/session/resolve-files'), payload)

    def test_scope_roundtrip_and_payload_boundaries(self):
        node = {'id': 'a' * 32, 'name': 'A'}
        self.assertEqual(federation.split(federation.qualify(node['id'], 'codex:abc', True), True),
                         (node['id'], 'codex:abc'))
        data = {'meta': {'uid': 'codex:abc', 'cwd': '/same'}, 'messages': [{
            'id': 'native-message', 'text': '/api/media/do-not-rewrite',
            'input': {'uid': 'arbitrary user input'}, 'media': [{'src': '/api/media/token'}]}]}
        out = federation.public_payload(data, node, '/api/watch')
        self.assertEqual(out['meta']['node_id'], node['id'])
        self.assertEqual(out['messages'][0]['input'], data['messages'][0]['input'])
        self.assertEqual(out['messages'][0]['id'], 'native-message')
        self.assertEqual(out['messages'][0]['text'], '/api/media/do-not-rewrite')
        self.assertIn(node['id'], out['messages'][0]['media'][0]['src'])
        self.assertEqual(data['meta']['uid'], 'codex:abc')

    def test_creation_receipt_survives_retry_and_uncertain_launch_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as root, patch.object(create_requests, 'DATA_DIR', Path(root)):
            body = {'request_id': 'request-123', 'source': 'claude', 'cwd': '/same'}
            calls = []
            def execute():
                calls.append(1)
                return 200, {'name': 'terminal'}
            self.assertEqual(create_requests.run(body, execute), create_requests.run(body, execute))
            self.assertEqual(calls, [1])
            self.assertEqual(create_requests.run({**body, 'cwd': '/other'}, execute)[0], 409)
            def interrupted():
                raise RuntimeError('launch interrupted')
            with self.assertRaises(RuntimeError):
                create_requests.run({**body, 'request_id': 'request-456'}, interrupted)
            self.assertEqual(create_requests.run({**body, 'request_id': 'request-456'}, execute)[0], 409)
            self.assertEqual(calls, [1])

    def test_hub_protocol_requires_credential_and_supported_version(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ('127.0.0.1', 1)
        with patch.object(server, 'NODE_TOKEN', 'secret'), patch.object(server, 'ALLOWED_IPS', {'127.0.0.1'}):
            handler.headers = {'X-AgentHub-Protocol': '1'}
            self.assertFalse(handler._allowed())
            handler.headers['X-AgentHub-Node-Token'] = 'secret'
            self.assertTrue(handler._allowed())
            handler.headers['X-AgentHub-Protocol'] = '99'
            self.assertFalse(handler._allowed())
            handler.headers = {}
            self.assertTrue(handler._allowed())


class HubHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.a = start_node('a' * 32, 'NodeA')
        cls.b = start_node('b' * 32, 'NodeB')
        cls.registry = hub.Registry(Path(cls.temp.name) / 'nodes.json', ['127.0.0.0/8'])
        for srv in (cls.a, cls.b):
            cls.registry.register({'name': srv.state['name'], 'url': f'http://127.0.0.1:{srv.server_port}',
                                   'token': srv.state['token']})
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), hub.HubHandler)
        cls.http.daemon_threads = True
        cls.http.registry = cls.registry
        cls.http.hub_mode = True
        cls.allow = patch.object(server, 'ALLOWED_IPS', {'127.0.0.1'})
        cls.allow.start()
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.http, cls.a, cls.b):
            stop(srv)
        cls.allow.stop()
        cls.temp.cleanup()

    def call(self, path, body=None, method=None):
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'}, method=method)
        try:
            response = urlopen(req, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_aggregate_identity_search_filter_and_stable_signature(self):
        _, data = self.call('/api/sessions')
        self.assertEqual(len(data['sessions']), 2)
        self.assertEqual(len({r['uid'] for r in data['sessions']}), 2)
        self.assertEqual({r['cwd'] for r in data['sessions']}, {'/same/project'})
        _, same = self.call('/api/sessions?sig=' + data['sig'])
        self.assertTrue(same['unchanged'])
        _, search = self.call('/api/search?q=needle&nodes=' + 'b' * 32)
        self.assertEqual([r['node_name'] for r in search['results']], ['NodeB'])
        self.assertEqual(self.call('/api/search?q=needle&nodes=')[1]['results'], [])

    def test_offline_node_is_partial_and_cached_without_blocking_healthy_node(self):
        self.call('/api/sessions')
        self.b.state['offline'] = True
        try:
            _, data = self.call('/api/sessions')
            self.assertTrue(data['partial'])
            stale = next(r for r in data['sessions'] if r['node_name'] == 'NodeB')
            self.assertTrue(stale['stale'])
            _, search = self.call('/api/search?q=needle')
            self.assertTrue(search['partial'])
            self.assertEqual([r['node_name'] for r in search['results']], ['NodeA'])
        finally:
            self.b.state['offline'] = False

    def test_routes_writes_without_scoped_identifiers_and_rejects_mixed_targets(self):
        a = federation.qualify('a' * 32, 'claude:same-file-hash', True)
        b = federation.qualify('b' * 32, 'same-terminal')
        status, _ = self.call('/api/session/star', {'uid': a, 'starred': True})
        self.assertEqual(status, 200)
        self.assertEqual(self.a.state['writes'][-1][1]['uid'], 'claude:same-file-hash')
        self.assertEqual(self.call('/api/session/rewind', {'uid': a, 'name': b})[0], 400)
        self.assertEqual(self.call('/api/term/create', {'source': 'claude', 'cwd': '/same'})[0], 400)
        self.assertEqual(self.call('/api/term/create', {'_node': 'b' * 32, '_build': 'stale'})[0], 409)
        status, _ = self.call('/api/session/send', {
            'uid': a, 'name': federation.qualify('a' * 32, 'same-terminal'),
            'text': 'keep exact text', 'request_id': 'request-123', '_build': server.ASSET_VERSION,
            'media': [{'src': '/api/nodes/' + 'a' * 32 + '/api/media/' + 'd' * 32}]})
        self.assertEqual(status, 200)
        forwarded = self.a.state['writes'][-1][1]
        self.assertEqual(forwarded['name'], 'same-terminal')
        self.assertEqual(forwarded['media'][0]['src'], '/api/media/' + 'd' * 32)
        self.assertEqual(forwarded['text'], 'keep exact text')
        self.assertEqual(forwarded['request_id'], 'request-123')

    def test_bulk_delete_and_trash_route_by_machine(self):
        uids = [federation.qualify(c * 32, 'claude:same-file-hash', True) for c in 'ab']
        try:
            _, data = self.call('/api/sessions/delete', {'uids': uids})
            self.assertEqual({row['uid'] for row in data['deleted']}, set(uids))
            self.assertEqual(data['errors'], [])
        finally:
            self.a.state['deleted'] = self.b.state['deleted'] = False
        _, trash = self.call('/api/trash')
        self.assertEqual(len({row['id'] for row in trash['items']}), 2)
        item = next(row for row in trash['items'] if row['node_name'] == 'NodeB')
        self.assertEqual(self.call('/api/trash/restore', {'id': item['id']})[0], 200)
        self.assertEqual(self.b.state['writes'][-1][1]['id'], 'claude/same-trash')

    def test_fork_visibility_is_grouped_by_machine_and_requalified(self):
        uids = [federation.qualify(c * 32, 'codex:parent', True) for c in 'ab']
        status, data = self.call('/api/sessions/fork-visibility', {
            'uids': uids, 'visible': True,
        })
        self.assertEqual(status, 200)
        self.assertEqual({row['uid'] for row in data['updated']}, set(uids))
        self.assertTrue(all(row['fork_parent_visible'] for row in data['updated']))
        self.assertEqual(self.a.state['writes'][-1], (
            '/api/sessions/fork-visibility',
            {'uids': ['codex:parent'], 'visible': True}))
        self.assertEqual(self.b.state['writes'][-1][0],
                         '/api/sessions/fork-visibility')

    def test_cross_origin_post_and_websocket_are_rejected(self):
        for headers, method in [({'Origin': 'https://other.invalid'}, 'POST'),
                                ({'Origin': 'https://other.invalid', 'Upgrade': 'websocket'}, 'GET')]:
            req = Request(self.base + '/api/term/attach', headers=headers, method=method)
            with self.assertRaises(HTTPError) as caught:
                urlopen(req)
            self.assertEqual(caught.exception.code, 403)

    def test_registry_limits_and_no_credentials_in_public_response(self):
        for url in ('http://169.254.169.254', 'http://example.com', 'http://127.0.0.1/a', 'http://user:pass@127.0.0.1'):
            with self.assertRaises(ValueError):
                self.registry.validate_url(url)
        _, data = self.call('/api/nodes')
        self.assertNotIn(self.a.state['token'], json.dumps(data))
        self.assertTrue(all('url' not in node for node in data['nodes']))
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o600)
        bad_token = 'sensitive-' * 6 + '\ninvalid'
        with self.assertRaises(ValueError) as caught:
            self.registry.register({'name': 'bad', 'url': 'http://127.0.0.1', 'token': bad_token})
        self.assertNotIn('sensitive', str(caught.exception))

    def test_machine_management_is_not_exposed_over_http(self):
        before = self.registry.all()
        status, _ = self.call('/api/nodes', {'name': 'Changed',
            'url': before[0]['url'], 'token': before[0]['token']})
        self.assertEqual(status, 405)
        status, _ = self.call('/api/nodes/' + before[0]['id'], method='DELETE')
        self.assertEqual(status, 405)
        self.assertEqual(self.registry.all(), before)



if __name__ == '__main__':
    unittest.main()
