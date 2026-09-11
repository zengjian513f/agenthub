import json
import errno
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
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

    def test_a_receipt_is_written_where_directories_cannot_be_opened(self):
        """Windows 上 os.open(目录) 是 PermissionError：收据写到一半炸了，
        新建会话就只剩一句 [Errno 13] Permission denied（cetus 上真实撞到过）。"""
        real_open = os.open

        def no_directories(path, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(13, 'Permission denied')   # Windows 的行为
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root, \
                patch.object(create_requests, 'DATA_DIR', Path(root)), \
                patch.object(create_requests, 'WINDOWS', True), \
                patch.object(create_requests.os, 'open', no_directories):
            body = {'request_id': 'request-win', 'source': 'claude', 'cwd': 'C:\\Users\\zj'}
            self.assertEqual(create_requests.run(body, lambda: (200, {'name': 't'})),
                             (200, {'name': 't'}))
            self.assertEqual(create_requests.run(body, lambda: (200, {'name': 't2'})),
                             (200, {'name': 't'}), '收据仍然要挡住重复启动')

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
        cls.registry = hub.Registry(Path(cls.temp.name) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
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
            self.registry.check_all()

    def test_offline_node_is_skipped_and_recovers_through_monitor(self):
        self.registry.check_all()
        self.assertFalse(self.call('/api/sessions')[1]['partial'])
        node_b = 'b' * 32
        real = self.registry.request

        def dead(node, path, *args, **kwargs):
            if node['id'] != node_b:
                return real(node, path, *args, **kwargs)
            time.sleep(.3)
            raise TimeoutError('private upstream details')

        uid = quote(federation.qualify(node_b, 'claude:same-file-hash', True), safe='')
        with patch.object(self.registry, 'request', side_effect=dead):
            started = time.monotonic()
            # A node that was reachable keeps its state for one grace probe, so a
            # single slow answer cannot gray out its console; the next pass
            # confirms the outage.
            for _ in range(hub.OFFLINE_STRIKES):
                self.registry.check_all()
            self.assertGreaterEqual(time.monotonic() - started, .3)
            # Page requests never wait for a node the monitor knows to be down.
            for path in ('/api/sessions', '/api/live', '/api/term/list', '/api/trash'):
                started = time.monotonic()
                _, data = self.call(path)
                self.assertLess(time.monotonic() - started, .15, path)
                self.assertTrue(data['partial'], path)
                self.assertEqual([e['name'] for e in data['errors']], ['NodeB'])
                self.assertEqual(data['errors'][0]['error_code'], 'timeout')
                public = next(n for n in data['nodes'] if n['id'] == node_b)
                self.assertFalse(public['online'])
                self.assertIn('offline_since', public)
                self.assertIn('checked_at', public)
                self.assertNotIn('private upstream details', json.dumps(data))
            _, data = self.call('/api/sessions')
            stale = next(r for r in data['sessions'] if r['node_name'] == 'NodeB')
            self.assertTrue(stale['stale'])
            _, data = self.call('/api/sessions?force=1')
            self.assertTrue(next(r for r in data['sessions'] if r['node_name'] == 'NodeB')['stale'])
            _, term = self.call('/api/term/list')
            self.assertFalse(term['capabilities'][node_b]['enabled'])
            started = time.monotonic()
            result = self.search_events()[-1]['data']
            self.assertLess(time.monotonic() - started, .15)
            self.assertTrue(result['partial'])
            self.assertEqual([r['node_name'] for r in result['results']], ['NodeA'])
            # Explicit actions re-check once, then refuse with the known reason and
            # ask the monitor to look again.
            self.registry.wake.clear()
            started = time.monotonic()
            status, body = self.call('/api/messages/' + uid)
            elapsed = time.monotonic() - started
            self.assertEqual(status, 503)
            self.assertTrue(body['node_offline'])
            self.assertIn('NodeB 离线', body['error'])
            self.assertGreaterEqual(elapsed, .3)
            self.assertLess(elapsed, 1.5)
            self.assertTrue(self.registry.wake.is_set())
            self.assertFalse(self.call('/api/sessions')[1]['nodes'][1]['online'])
        # The machine is back: an explicit action succeeds at once through the
        # re-check, and the next monitor pass clears the offline cache.
        status, body = self.call('/api/messages/' + uid)
        self.assertEqual(status, 200)
        self.assertEqual(body['meta']['uid'], federation.qualify(node_b, 'claude:same-file-hash', True))
        self.assertFalse(self.registry.offline(node_b))
        self.registry.check_all()
        _, data = self.call('/api/sessions')
        self.assertFalse(data['partial'])
        public = next(n for n in data['nodes'] if n['id'] == node_b)
        self.assertTrue(public['online'])
        self.assertNotIn('offline_since', public)
        self.assertFalse(any(r.get('stale') for r in data['sessions']))

    def test_session_polls_are_conditional_and_served_from_cache_when_unchanged(self):
        self.registry.check_all()
        seen = len(self.b.state['gets'])
        self.registry.check_all()
        _, data = self.call('/api/sessions')
        probes = self.b.state['gets'][seen:]
        self.assertEqual([p for p, _ in probes], ['/api/sessions', '/api/sessions'])
        self.assertTrue(all(q.get('sig') for _, q in probes))
        self.assertEqual(len({r['uid'] for r in data['sessions'] if r['node_name'] == 'NodeB'}), 1)
        self.assertFalse(any(r.get('stale') for r in data['sessions']))
        self.assertTrue(next(n for n in data['nodes'] if n['name'] == 'NodeB')['online'])
        # A forced refresh and a changed list both bypass the cache.
        self.call('/api/sessions?force=1')
        self.assertNotIn('sig', self.b.state['gets'][-1][1])
        self.b.state['deleted'] = True
        try:
            _, data = self.call('/api/sessions')
            self.assertEqual([r['node_name'] for r in data['sessions']], ['NodeA'])
            self.assertTrue(self.b.state['gets'][-1][1].get('sig'))
        finally:
            self.b.state['deleted'] = False
        _, data = self.call('/api/sessions')
        self.assertEqual({r['node_name'] for r in data['sessions']}, {'NodeA', 'NodeB'})

    def test_session_snapshot_survives_hub_restart_for_offline_machine(self):
        self.registry.check_all()
        snapshot = self.registry.snapshot_path('b' * 32)
        self.assertTrue(snapshot.exists())
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        fresh = hub.Registry(self.registry.path, ['127.0.0.0/8'], monitor=False)
        public = next(n for n in fresh.public() if n['id'] == 'b' * 32)
        self.assertIsNone(public['online'])
        self.assertIn('last_seen', public)
        node = fresh.get('b' * 32)
        self.b.state['offline'] = True
        try:
            fresh.check_all()
            for query in ({}, {'force': ['1']}):
                _, data, failure = fresh.fetch(node, '/api/sessions', query)
                self.assertEqual(failure['error_code'], 'http_error')
                self.assertEqual([r['node_name'] for r in data['sessions']], ['NodeB'])
                self.assertTrue(data['sessions'][0]['stale'])
                self.assertEqual(data['sessions'][0]['last_seen'], public['last_seen'])
        finally:
            self.b.state['offline'] = False
        fresh.check_all()
        self.assertTrue(fresh.state('b' * 32)['online'])

    def test_monitor_thread_polls_and_wakes_on_nudge(self):
        with tempfile.TemporaryDirectory() as root:
            registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
            registry.register({'name': 'NodeA', 'url': f'http://127.0.0.1:{self.a.server_port}',
                               'token': self.a.state['token']})
            seen = len(self.a.state['gets'])
            probes = lambda: len([p for p, _ in self.a.state['gets'][seen:] if p == '/api/sessions'])
            def wait_for(condition):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not condition():
                    time.sleep(.02)
                self.assertTrue(condition())
            with patch.object(hub, 'PROBE_INTERVAL', 30):
                registry.start_monitor()
                try:
                    wait_for(lambda: registry.state('a' * 32).get('online'))
                    time.sleep(.1)
                    self.assertEqual(probes(), 1)
                    registry.nudge()
                    wait_for(lambda: probes() == 2)
                finally:
                    registry.stop_monitor()

    def test_node_health_exposes_safe_failure_reason_and_clears_on_recovery(self):
        node = self.registry.get('b' * 32)
        cases = [(TimeoutError('private upstream details'), 'timeout', '5 秒'),
                 (ConnectionRefusedError('private upstream details'), 'connection_refused', '拒绝连接'),
                 (OSError(errno.EHOSTUNREACH, 'private upstream details'), 'unreachable', '网络不可达'),
                 (ValueError('private upstream details'), 'invalid_response', '无效')]
        for error, code, message in cases:
            with self.subTest(code=code), patch.object(self.registry, 'request', side_effect=error):
                for _ in range(hub.OFFLINE_STRIKES):
                    _, _, failure = self.registry.query(node, '/api/live', {})
            self.assertEqual(failure['error_code'], code)
            _, response = self.call('/api/nodes')
            public = next(n for n in response['nodes'] if n['id'] == node['id'])
            self.assertFalse(public['online'])
            self.assertEqual(public['failed_path'], '/api/live')
            self.assertIn(message, public['error'])
            self.assertNotIn('private upstream details', json.dumps(response))
        with patch.object(self.registry, 'request', return_value=(403, {'error': 'private upstream details'})):
            _, _, failure = self.registry.query(node, '/api/term/list', {})
        self.assertIn('HTTP 403', failure['error'])
        self.assertIn('认证', failure['error'])
        self.registry.query(node, '/api/live', {})
        healthy = next(n for n in self.registry.public() if n['id'] == node['id'])
        self.assertTrue(healthy['online'])
        self.assertNotIn('error', healthy)
        self.assertNotIn('failed_path', healthy)

    def test_one_slow_answer_does_not_gray_out_a_reachable_node(self):
        """A busy machine answering late must not look switched off.

        The request that failed still reports its own error; only repeated
        failures change the node's published state, and the outage is then dated
        from the first failure rather than the probe that gave up.
        """
        node = self.registry.get('b' * 32)
        self.registry.query(node, '/api/live', {})           # known reachable
        with patch.object(self.registry, 'request', side_effect=TimeoutError('slow')):
            _, _, failure = self.registry.query(node, '/api/live', {})
        self.assertEqual(failure['error_code'], 'timeout')
        public = next(n for n in self.registry.public() if n['id'] == node['id'])
        self.assertTrue(public['online'])
        self.assertFalse(self.registry.offline(node['id']))
        self.assertNotIn('offline_since', public)
        first_failure = self.registry.state(node['id'])['failed_since']

        with patch.object(self.registry, 'request', side_effect=TimeoutError('slow')):
            for _ in range(hub.OFFLINE_STRIKES - 1):
                self.registry.query(node, '/api/live', {})
        public = next(n for n in self.registry.public() if n['id'] == node['id'])
        self.assertFalse(public['online'])
        self.assertTrue(self.registry.offline(node['id']))
        self.assertEqual(public['offline_since'], first_failure)

        self.registry.query(node, '/api/live', {})
        recovered = self.registry.state(node['id'])
        self.assertTrue(recovered['online'])
        self.assertNotIn('strikes', recovered)
        self.assertNotIn('failed_since', recovered)

    def test_a_node_never_reached_is_offline_on_its_first_failure(self):
        """The grace probe only protects a node that was actually answering."""
        with tempfile.TemporaryDirectory() as root:
            registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
            registry.register({'name': 'NodeA', 'url': f'http://127.0.0.1:{self.a.server_port}',
                               'token': self.a.state['token']})
            node = registry.get('a' * 32)
            self.assertIsNone(registry.state(node['id']).get('online'))
            with patch.object(registry, 'request', side_effect=ConnectionRefusedError('x')):
                registry.query(node, '/api/sessions', {})
            self.assertTrue(registry.offline(node['id']))
            self.assertIn('offline_since', registry.state(node['id']))

    def search_events(self, nodes=None):
        path = '/api/search?q=needle&progress=1'
        if nodes is not None:
            path += '&nodes=' + nodes
        with urlopen(self.base + path, timeout=10) as response:
            self.assertIn('application/x-ndjson', response.headers['Content-Type'])
            self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
            return [json.loads(line) for line in response]

    def test_search_stream_progress_outlives_idle_timeout_and_scopes_results(self):
        self.b.state.update(search_steps=8, search_delay=.04)
        try:
            with patch.object(hub, 'SEARCH_IDLE_TIMEOUT', .2):
                events = self.search_events()
            progress = [e for e in events if e['type'] == 'progress']
            self.assertTrue(any(0 < e['done'] < e['total'] for e in progress))
            result = events[-1]['data']
            self.assertFalse(result['partial'])
            self.assertEqual({r['node_name'] for r in result['results']}, {'NodeA', 'NodeB'})
            self.assertEqual(len({r['uid'] for r in result['results']}), 2)
            self.assertEqual(result['total_pool'], 2)
        finally:
            self.b.state.pop('search_steps'); self.b.state.pop('search_delay')

    def test_search_progress_waits_for_all_totals_and_reports_actual_truncated_scan(self):
        settings = {'search_prepare_delay': .15, 'search_steps': 10, 'search_stop': 3,
                    'search_pool': 10, 'search_scanned': 2, 'search_truncated': True}
        self.b.state.update(settings)
        try:
            progress = [e for e in self.search_events() if e['type'] == 'progress']
            ready_a = next(e for e in progress if e['done'] == 1 and not e['total_known'])
            self.assertEqual(next(n for n in ready_a['nodes'] if n['name'] == 'NodeB')['state'], 'preparing')
            last = progress[-1]
            self.assertTrue(last['total_known'])
            self.assertEqual((last['done'], last['total']), (3, 11))
            limited = next(n for n in last['nodes'] if n['name'] == 'NodeB')
            self.assertEqual((limited['state'], limited['done'], limited['total']), ('limited', 2, 10))
        finally:
            for key in settings:
                self.b.state.pop(key)

    def test_slow_search_does_not_block_registry_or_other_node_results(self):
        self.b.state.update(search_steps=15, search_delay=.1)
        try:
            with urlopen(self.base + '/api/search?q=needle&progress=1', timeout=5) as response:
                events = []
                while True:
                    event = json.loads(response.readline())
                    events.append(event)
                    if event['type'] == 'progress' and event['done'] > 0:
                        break
                started = time.monotonic()
                self.assertEqual(self.call('/api/nodes')[0], 200)
                self.assertLess(time.monotonic() - started, .5)
                while not any(e['type'] == 'matches' for e in events):
                    events.append(json.loads(response.readline()))
                first = next(e for e in events if e['type'] == 'matches')
                self.assertEqual(first['results'][0]['node_name'], 'NodeA')
                self.assertLess(time.monotonic() - started, .7)
                events.extend(json.loads(line) for line in response)
                self.assertEqual(len(events[-1]['data']['results']), 2)
        finally:
            self.b.state.pop('search_steps'); self.b.state.pop('search_delay')

    def test_search_failure_keeps_health_and_healthy_results(self):
        self.call('/api/live')
        for option in ('search_error', 'search_incomplete', 'search_delay'):
            self.b.state[option] = .3 if option == 'search_delay' else True
            try:
                with patch.object(hub, 'SEARCH_IDLE_TIMEOUT', .1):
                    result = self.search_events()[-1]['data']
                self.assertTrue(result['partial'])
                self.assertEqual([r['node_name'] for r in result['results']], ['NodeA'])
                self.assertEqual([e['name'] for e in result['errors']], ['NodeB'])
                self.assertTrue(next(n for n in result['nodes'] if n['name'] == 'NodeB')['online'])
                self.assertNotIn('private upstream details', json.dumps(result))
            finally:
                self.b.state.pop(option)

    def test_search_failure_retains_matches_already_streamed(self):
        self.b.state.update(search_matches=True, search_error=True)
        try:
            result = self.search_events()[-1]['data']
            self.assertTrue(result['partial'])
            self.assertEqual({r['node_name'] for r in result['results']}, {'NodeA', 'NodeB'})
            self.assertTrue(all(federation.split(r['uid'], uid=True)[0] == r['node_id']
                                for r in result['results']))
        finally:
            self.b.state.pop('search_matches'); self.b.state.pop('search_error')

    def test_search_stream_empty_selection_and_json_node_compatibility(self):
        self.assertEqual(self.search_events('')[-1]['data']['results'], [])
        self.b.state['search_json'] = True
        try:
            result = self.search_events('b' * 32)[-1]['data']
            self.assertFalse(result['partial'])
            self.assertEqual([r['node_name'] for r in result['results']], ['NodeB'])
        finally:
            self.b.state.pop('search_json')

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

    def test_bug_report_uploads_route_by_node_query_without_a_session(self):
        req = Request(self.base + '/api/session/attachment?uid=bug-report&node=' + 'b' * 32
                      + '&name=%E6%88%AA%E5%9B%BE.png', data=b'\x89PNG', method='POST',
                      headers={'Content-Type': 'image/png'})
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload['name'], '截图.png')
        query, raw = self.b.state['uploads'][-1]
        self.assertEqual(query['uid'], ['bug-report'])
        self.assertEqual(raw, b'\x89PNG')
        self.assertNotIn('node', query)
        # 没有 node 又不是限定 uid 的上传依旧被拒绝，不会猜测机器。
        req = Request(self.base + '/api/session/attachment?uid=bug-report&name=x.png',
                      data=b'x', method='POST', headers={'Content-Type': 'image/png'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req, timeout=20)
        self.assertEqual(caught.exception.code, 400)

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

    def test_machine_colour_comes_from_the_registry(self):
        """配色按机器配在注册表里：加机器只改配置，不用改代码再部署三台。

        用独立注册表，因为重新注册会把节点挪到列表末尾，共享夹具里有测试按下标取节点。
        """
        with tempfile.TemporaryDirectory() as root:
            registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
            base = {'name': 'NodeA', 'url': f'http://127.0.0.1:{self.a.server_port}',
                    'token': self.a.state['token']}
            self.assertEqual(registry.register(base)['color'], '')
            self.assertEqual([n['color'] for n in registry.public()], [''])

            self.assertEqual(registry.register({**base, 'color': 'teal'})['color'], 'teal')
            row = registry.public()[0]
            self.assertEqual(row['color'], 'teal')
            self.assertNotIn('url', row)          # 公开列表仍然不含连接地址

            with self.assertRaisesRegex(ValueError, '机器颜色'):
                registry.register({**base, 'color': '#ff0000'})
            self.assertEqual(registry.get('a' * 32).get('color'), 'teal')

            # 配色随注册表落盘，重启后仍在
            fresh = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
            self.assertEqual(fresh.public()[0]['color'], 'teal')

    def test_the_web_can_rename_and_recolour_a_machine(self):
        """网页能改的只有名称和配色；接机器、下机器、地址和凭据仍是服务器端操作。"""
        node_a, node_b = 'a' * 32, 'b' * 32
        original = {n['id']: (n['name'], n.get('color', '')) for n in self.registry.all()}
        try:
            status, body = self.call(f'/api/nodes/{node_a}/display',
                                     {'name': '机房 A', 'color': 'teal'})
            self.assertEqual(status, 200, body)
            self.assertEqual(body['node'], {'id': node_a, 'name': '机房 A', 'color': 'teal'})
            _, listing = self.call('/api/nodes')
            row = next(n for n in listing['nodes'] if n['id'] == node_a)
            self.assertEqual((row['name'], row['color']), ('机房 A', 'teal'))
            self.assertNotIn('url', row)
            self.assertNotIn('token', row)

            # 只给一个字段时，另一个保持不变
            self.assertEqual(self.call(f'/api/nodes/{node_a}/display',
                                       {'color': 'rose'})[1]['node']['name'], '机房 A')
            # 空颜色表示清掉
            self.assertEqual(self.call(f'/api/nodes/{node_a}/display',
                                       {'color': ''})[1]['node']['color'], '')

            for bad, hint in [({'name': ''}, '不能为空'),
                              ({'name': 'x' * 81}, '80'),
                              ({'color': '#ff0000'}, '机器颜色'),
                              ({'name': 'NodeB'}, '已有机器')]:
                with self.subTest(bad=bad):
                    status, body = self.call(f'/api/nodes/{node_a}/display', bad)
                    self.assertEqual(status, 400, body)
                    self.assertIn(hint, body['error'])

            # 地址和凭据改不了：即使带上也不生效
            self.call(f'/api/nodes/{node_a}/display',
                      {'name': '机房 A', 'url': 'http://127.0.0.1:1', 'token': 'x' * 40})
            stored = self.registry.get(node_a)
            self.assertEqual(stored['url'], f'http://127.0.0.1:{self.a.server_port}')
            self.assertEqual(stored['token'], self.a.state['token'])

            self.assertEqual(self.call(f'/api/nodes/{"c" * 32}/display', {'name': 'x'})[0], 404)
        finally:
            for nid, (name, color) in original.items():
                self.registry.update_display(nid, name=name, color=color)
        self.assertEqual({n['id']: (n['name'], n.get('color', '')) for n in self.registry.all()},
                         original)
        # 机器自身的存在性仍然只能在服务器端改：HTTP 上没有这样的接口
        self.assertEqual(self.call('/api/nodes', {'name': 'X'})[0], 400)
        self.assertEqual(len(self.registry.all()), 2)

    def test_terminal_backend_is_reported_and_switched_per_machine(self):
        """终端后端是每台机器各自的设置，网页按机器读取和切换。"""
        self.registry.check_all()
        node_a, node_b = 'a' * 32, 'b' * 32
        _, listing = self.call('/api/term/list')
        self.assertEqual(listing['capabilities'][node_a]['backend'], 'tmux')
        self.assertEqual({b['name'] for b in listing['capabilities'][node_b]['backends']},
                         {'tmux', 'ptyhost'})
        try:
            status, body = self.call(f'/api/nodes/{node_b}/api/term/backend',
                                     {'backend': 'ptyhost'})
            self.assertEqual(status, 200, body)
            self.assertEqual(body['backend'], 'ptyhost')

            _, listing = self.call('/api/term/list')
            self.assertEqual(listing['capabilities'][node_b]['backend'], 'ptyhost')
            # 只改了这台，另一台不受影响
            self.assertEqual(listing['capabilities'][node_a]['backend'], 'tmux')
            self.assertEqual([b['current'] for b in listing['capabilities'][node_b]['backends']
                              if b['name'] == 'ptyhost'], [True])

            status, body = self.call(f'/api/nodes/{node_b}/api/term/backend',
                                     {'backend': 'nope'})
            self.assertEqual(status, 400)
            self.assertIn('未知终端后端', body['error'])
        finally:
            self.b.state.pop('backend', None)

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




if __name__ == '__main__':
    unittest.main()
