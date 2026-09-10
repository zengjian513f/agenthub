"""Free Codex child-view regression through the real index, HTTP/SSE and Hub."""
import json
import sys
import threading
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from agenthub import audit, hub, index, server, session_meta
from hub_fixture import NodeHandler, start_node, stop
from test_index import IsolatedIndexTests


class NativeNode(NodeHandler):
    def do_GET(self):
        path = urlparse(self.path)
        q = parse_qs(path.query)
        if (self.headers.get('X-AgentHub-Protocol')
                and self.headers.get('X-AgentHub-Node-Token') != self.state['token']):
            return self._json({'error': 'forbidden'}, 403)
        if path.path == '/api/watch':
            return server.Handler._watch(self, q)
        if path.path == '/api/sessions' or path.path.startswith('/api/messages/'):
            try:
                return server.Handler._api_get(self, path.path, q)
            except KeyError:
                return self._json({'error': 'not found'}, 404)
        return super().do_GET()


def main():
    fixture = IsolatedIndexTests()
    fixture.setUp()
    try:
        root = fixture.codex_root.parent
        with ExitStack() as stack:
            stack.enter_context(patch.object(audit, 'record'))
            stack.enter_context(patch.object(server.send_protocol, 'driver_for', return_value=None))
            stack.enter_context(patch.object(server.term, 'list_sessions', return_value=[]))
            stack.enter_context(patch.object(server, '_pane_for_session', return_value=None))
            stack.enter_context(patch.object(session_meta, 'META_FILE', root / 'meta.json'))
            stack.enter_context(patch.object(session_meta, 'DATA_DIR', root))
            fixture.codex_session('parent', 'Parent conversation')
            uid = index.load(force=True)[0]['uid']
            node = start_node('a' * 32, 'NativeNode')
            node.state['token'] = 'subagent-fixture-token-' * 3
            node.RequestHandlerClass = NativeNode
            node.state['row'] = index.get(uid)
            registry = hub.Registry(root / 'registry.json', ['127.0.0.0/8'])
            registry.register({'name': 'NativeNode', 'url': f'http://127.0.0.1:{node.server_port}',
                               'token': node.state['token']})
            central = ThreadingHTTPServer(('127.0.0.1', 0), hub.HubHandler)
            central.daemon_threads = True
            central.registry = registry
            central.hub_mode = True
            server.ALLOWED_IPS.add('127.0.0.1')
            threading.Thread(target=central.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    for target in (node, central):
                        for width in (1280, 390):
                            for path in fixture.codex_root.rglob('rollout-worker*.jsonl'):
                                path.unlink()
                            index.load()
                            scoped = uid if target is node else uid.replace(':', ':' + 'a' * 32 + '~', 1)
                            page = browser.new_page(viewport={'width': width, 'height': 850})
                            errors = []
                            page.on('pageerror', lambda e: errors.append(str(e)))
                            page.goto(f'http://127.0.0.1:{target.server_port}/')
                            page.wait_for_function('S.sessions.length > 0')
                            page.evaluate('uid => openSession(uid)', scoped)
                            expect(page.locator('.dhead h2')).to_contain_text('Parent conversation')
                            expect(page.locator('#a-view-switch')).to_have_count(0)
                            for n in range(3):
                                path = fixture.codex_session(f'worker-{n}', f'Child task {n}',
                                                             thread_source='subagent', session_id='parent')
                                rows = [json.loads(line) for line in path.read_text().splitlines()]
                                rows[0]['payload']['source'] = {'subagent': {'thread_spawn': {
                                    'parent_thread_id': 'parent', 'agent_path': f'/root/worker_{n}'}}}
                                rows[0]['payload']['cwd'] = f'/tmp/worker-{n}'
                                rows.append({'type': 'turn_context', 'payload': {'model': f'child-model-{n}'}})
                                rows.append({'type': 'event_msg', 'payload': {'type': 'task_started'}})
                                fixture.write_rows(path, rows)
                            page.evaluate('pollSessions()')
                            expect(page.locator('#a-view-switch')).to_be_visible(timeout=15000)
                            expect(page.locator('#side .item')).to_have_count(1)
                            for n in range(3):
                                page.locator('#a-view-switch').click()
                                expect(page.locator('#session-view-menu [data-agent]')).to_have_count(4)
                                page.locator(f'[data-agent="worker-{n}"]').click()
                                expect(page.locator('.dhead h2')).to_contain_text(f'/root/worker_{n}')
                                expect(page.locator('#msgs')).to_contain_text(f'Child task {n}')
                                expect(page.locator('#msgs')).not_to_contain_text('Parent conversation')
                                expect(page.locator('#a-term')).to_be_visible()
                                expect(page.locator('#a-term')).to_be_enabled()
                                expect(page.locator('#a-term')).to_have_attribute('data-unavailable', 'true')
                            child = next(fixture.codex_root.rglob('rollout-worker-2.jsonl'))
                            with child.open('a') as fh:
                                fh.write(json.dumps({'type': 'response_item', 'payload': {
                                    'type': 'message', 'role': 'assistant', 'content': [
                                        {'type': 'output_text', 'text': 'Live child update'}]}}) + '\n')
                            expect(page.locator('#msgs')).to_contain_text('Live child update', timeout=10000)
                            page.evaluate('pollSessions()')
                            child_meta = page.evaluate('cache.get(viewKey(S.sel, S.agent)).meta')
                            assert child_meta['cwd'] == '/tmp/worker-2'
                            assert child_meta['model'] == 'child-model-2'
                            page.locator('#a-view-switch').click()
                            page.locator('[data-agent=""]').click()
                            expect(page.locator('#msgs')).to_contain_text('Parent conversation')
                            expect(page.locator('#msgs')).not_to_contain_text('Live child update')
                            assert page.request.get(f'http://127.0.0.1:{target.server_port}/api/messages/{scoped}?agent=unrelated').status == 404
                            assert not errors, errors
                            print(f'PASS {"node" if target is node else "hub"} {width}px: discover, switch, SSE, isolation, invalid ID')
                            page.close()
                    browser.close()
            finally:
                central.shutdown()
                central.server_close()
                stop(node)
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
