"""Free browser regression for Codex rewind visibility using native JSONL fixtures."""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agenthub import adapters, hub, live, server
from hub_fixture import NodeHandler, start_node, stop
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = '11111111-1111-1111-1111-111111111111'
        child = '22222222-2222-2222-2222-222222222222'
        grandchild = '33333333-3333-3333-3333-333333333333'
        rows = []
        adapter = adapters.CodexAdapter()

        def write(sid, ancestor=None):
            meta = dict(id=sid, timestamp='2026-09-08T00:00:00Z', cwd='/tmp/fork-test')
            if ancestor:
                meta.update(forked_from_id=ancestor, history_base=dict(
                    thread_id=ancestor, end_byte_offset=(root / f'rollout-{ancestor}.jsonl').stat().st_size))
            records = [dict(type='session_meta', payload=meta), dict(type='response_item',
                payload=dict(type='message', role='user', content=[dict(type='input_text', text=sid)]))]
            (root / f'rollout-{sid}.jsonl').write_text(
                ''.join(json.dumps(r) + '\n' for r in records))
            rows[:] = adapter.list_sessions()

        class Handler(NodeHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                if path == '/api/sessions':
                    return self._json(dict(sessions=rows, sig=str(len(rows))))
                if path == '/api/live':
                    uids, _owned = live.active_processes(rows)
                    return self._json(dict(uids=uids, tmux_uids=uids,
                                           started_at={}))
                if path.startswith('/api/messages/'):
                    row = next(r for r in rows if r['uid'] == unquote(path.rsplit('/', 1)[1]))
                    self.state['row'] = row
                    messages, end = adapter.read(row['path'])
                    return self._json(dict(meta=row, messages=messages, start=0, end=end,
                        reset=True, version=dict(head='fixed', size=end, mtime=1), anchor='fixed',
                        message_total=len(messages), activity=None, partial=None))
                return super().do_GET()

        node = start_node('a' * 32, 'Test')
        node.RequestHandlerClass = Handler
        registry = hub.Registry(root / 'registry.json', ['127.0.0.0/8'])
        registry.register(dict(name='Test', url=f'http://127.0.0.1:{node.server_port}', token=node.state['token']))
        central = ThreadingHTTPServer(('127.0.0.1', 0), hub.HubHandler)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with patch.object(adapters, 'CODEX_ROOT', root), \
                    patch.object(adapters, 'CODEX_INDEX', root / 'index'), \
                    patch.object(live, 'pids_of', return_value=[123]), \
                    sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for target in (node, central):
                    for choice in ('keep', 'hide', 'escape'):
                        for f in root.glob('rollout-*.jsonl'):
                            f.unlink()
                        write(parent)
                        context = browser.new_context(viewport=dict(width=1100, height=850))
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{target.server_port}/')
                        page.wait_for_selector('#side .item')
                        assert page.locator('#side .item').count() == 1
                        assert page.locator('dialog[open]').count() == 0
                        write(child, parent)
                        page.evaluate('pollSessions()')
                        dialog = page.locator('#fork-parent-dialog')
                        dialog.wait_for(state='visible')
                        assert page.locator('#side .item').count() == 2  # No hiding before consent.
                        assert parent in dialog.inner_text()
                        page.evaluate('pollLive()')
                        states = page.evaluate('''(sids) => Object.fromEntries(sids.map(sid => {
                          const session = S.sessions.find(s => s.sid === sid);
                          const row = document.querySelector(`.item[data-uid="${session.uid}"]`);
                          return [sid, {live:S.live.has(session.uid), tmux:row.classList.contains('live-tmux')}];
                        }))''', [parent, child])
                        assert states[parent] == {'live': False, 'tmux': False}
                        assert states[child] == {'live': True, 'tmux': True}
                        if choice == 'escape':
                            page.keyboard.press('Escape')
                        else:
                            dialog.locator(f'button[value="{choice}"]').click()
                        dialog.wait_for(state='hidden')
                        expected = 1 if choice == 'hide' else 2
                        expect(page.locator('#side .item')).to_have_count(expected)
                        assert page.evaluate('S.sessions.length') == 2  # API retains both branches.
                        assert page.locator('#session-total').inner_text() == str(expected)
                        assert page.evaluate('''() => {
                          S.results = S.sessions.map(s => ({...s, hits:1}));
                          const n = visible().length;
                          S.results = null;
                          return n;
                        }''') == expected
                        if target is central:
                            assert page.locator('.node-count').inner_text() == str(expected)
                        page.evaluate('loadSessions(true)')
                        assert page.locator('dialog[open]').count() == 0
                        page.reload()
                        page.wait_for_selector('#side .item')
                        expect(page.locator('#side .item')).to_have_count(expected)
                        assert page.locator('dialog[open]').count() == 0
                        # Opening a decided branch must not ask again.
                        child_uid = page.evaluate('(sid) => S.sessions.find(s => s.sid === sid).uid', child)
                        page.evaluate('(uid) => openSession(uid)', child_uid)
                        assert page.locator('dialog[open]').count() == 0
                        # A successive rewind asks only about its immediate parent.
                        write(grandchild, child)
                        page.evaluate('pollSessions()')
                        dialog.wait_for(state='visible')
                        assert child in dialog.inner_text()
                        page.keyboard.press('Escape')
                        dialog.wait_for(state='hidden')
                        expect(page.locator('#side .item')).to_have_count(expected + 1)
                        if choice == 'hide':
                            page.locator('#settings').click()
                            page.locator('#restore-fork-parents').click()
                            assert page.locator('#side .item').count() == 3
                            page.locator('#settings-dialog button[type="submit"]').last.click()
                        assert not errors, errors
                        context.close()
                        print(f'PASS {"hub" if target is central else "node"}: {choice}, reload, repeated rewind, restore')
                # Existing branches prompt on open; identical IDs on another node cannot be hidden.
                context = browser.new_context(viewport=dict(width=390, height=844))
                page = context.new_page()
                page.goto(f'http://127.0.0.1:{central.server_port}/')
                page.wait_for_selector('#side .item')
                assert page.locator('dialog[open]').count() == 0
                page.evaluate('''(sid) => {
                  const parent = S.sessions.find(s => s.sid === sid);
                  S.sessions.push({...parent, uid:'other-node-parent', node_id:'other-node'});
                }''', parent)
                child_uid = page.evaluate('(sid) => S.sessions.find(s => s.sid === sid).uid', child)
                page.evaluate('(uid) => openSession(uid)', child_uid)
                dialog = page.locator('#fork-parent-dialog')
                dialog.wait_for(state='visible')
                box = dialog.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= 390
                dialog.locator('button[value="hide"]').click()
                dialog.wait_for(state='hidden')
                assert page.evaluate("visible().some(s => s.uid === 'other-node-parent')")
                page.screenshot(path='/tmp/agenthub-codex-fork-after.png')
                context.close()
                browser.close()
                print('PASS existing branch, mobile dialog, node identity isolation')
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
