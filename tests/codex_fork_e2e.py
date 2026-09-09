"""Free browser regression for server-owned Codex fork-parent visibility."""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agenthub import adapters, hub, index, live, server, session_meta
from hub_fixture import NodeHandler, start_node, stop
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = '11111111-1111-1111-1111-111111111111'
        child = '22222222-2222-2222-2222-222222222222'
        grandchild = '33333333-3333-3333-3333-333333333333'
        rows = []
        term_sessions = []
        adapter = adapters.CodexAdapter()

        def write(sid, ancestor=None):
            meta = dict(id=sid, timestamp='2026-09-08T00:00:00Z', cwd='/tmp/fork-test')
            if ancestor:
                meta.update(forked_from_id=ancestor, history_base=dict(
                    thread_id=ancestor,
                    end_byte_offset=(root / f'rollout-{ancestor}.jsonl').stat().st_size))
            records = [
                dict(type='session_meta', payload=meta),
                dict(type='response_item', payload=dict(
                    type='message', role='user',
                    content=[dict(type='input_text', text=sid)])),
            ]
            (root / f'rollout-{sid}.jsonl').write_text(
                ''.join(json.dumps(record) + '\n' for record in records))
            rows[:] = adapter.list_sessions()

        class Handler(NodeHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                if path == '/api/sessions':
                    return self._json(dict(
                        sessions=session_meta.enrich(rows),
                        sig=session_meta.signature() + '-' + str(len(rows))))
                if path == '/api/live':
                    uids, _owned = live.active_processes(rows)
                    return self._json(dict(uids=uids, tmux_uids=uids, started_at={}))
                if path == '/api/term/list':
                    return self._json(dict(
                        enabled=True, sources=dict(claude=True, codex=True),
                        home='/tmp/fork-test', sessions=term_sessions, pending=[]))
                if path.startswith('/api/messages/'):
                    row = next(item for item in rows
                               if item['uid'] == unquote(path.rsplit('/', 1)[1]))
                    self.state['row'] = row
                    messages, end = adapter.read(row['path'])
                    return self._json(dict(
                        meta=session_meta.enrich_one(row, rows), messages=messages,
                        start=0, end=end, reset=True,
                        version=dict(head='fixed', size=end, mtime=1), anchor='fixed',
                        message_total=len(messages), activity=None, partial=None))
                return super().do_GET()

            def do_POST(self):
                path = urlparse(self.path).path
                if path != '/api/sessions/fork-visibility':
                    return super().do_POST()
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if (self.headers.get('X-AgentHub-Protocol')
                        and self.headers.get('X-AgentHub-Node-Token') != self.state['token']):
                    return self._json({'error': 'forbidden'}, 403)
                body = json.loads(raw or b'{}')
                self.state['writes'].append((path, body))
                return server.Handler._set_fork_parent_visibility(self, body)

        node = start_node('a' * 32, 'Test')
        node.RequestHandlerClass = Handler
        registry = hub.Registry(root / 'registry.json', ['127.0.0.0/8'])
        registry.register(dict(
            name='Test', url=f'http://127.0.0.1:{node.server_port}',
            token=node.state['token']))
        central = ThreadingHTTPServer(('127.0.0.1', 0), hub.HubHandler)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with patch.object(adapters, 'CODEX_ROOT', root), \
                    patch.object(adapters, 'CODEX_INDEX', root / 'index'), \
                    patch.object(session_meta, 'DATA_DIR', root / 'meta'), \
                    patch.object(session_meta, 'META_FILE', root / 'meta' / 'session-meta.json'), \
                    patch.object(index, 'load', side_effect=lambda force=False: rows), \
                    patch.object(live, 'pids_of', return_value=[123]), \
                    sync_playwright() as pw:
                launch = dict(headless=True)
                if executable := os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                    launch['executable_path'] = executable
                browser = pw.chromium.launch(**launch)

                for target in (node, central):
                    for file in root.glob('rollout-*.jsonl'):
                        file.unlink()
                    session_meta.META_FILE.unlink(missing_ok=True)
                    rows.clear()
                    write(parent)
                    base = f'http://127.0.0.1:{target.server_port}/'
                    context = browser.new_context(viewport=dict(width=1100, height=850))
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base)
                    expect(page.locator('#side .item')).to_have_count(1)

                    # A child turns the old row into a parent. The server marks
                    # it hidden by default; no browser-local consent exists.
                    write(child, parent)
                    page.evaluate('pollSessions()')
                    expect(page.locator('#side .item')).to_have_count(1)
                    state = page.evaluate('''(sids) => Object.fromEntries(sids.map(sid => {
                      const row = S.sessions.find(session => session.sid === sid);
                      return [sid, {parent:!!row.fork_parent,
                        shown:row.fork_parent_visible, hidden:sessionHidden(row)}];
                    }))''', [parent, child])
                    assert state[parent] == {'parent': True, 'shown': False, 'hidden': True}
                    assert state[child] == {'parent': False, 'shown': None, 'hidden': False}
                    assert page.locator('#session-total').inner_text() == '1'
                    assert page.locator('dialog[open]').count() == 0

                    # Settings writes a server flag. A brand-new browser sees it,
                    # proving this is not localStorage state.
                    page.locator('#settings').click()
                    page.locator('#restore-fork-parents').click()
                    expect(page.locator('#side .item')).to_have_count(2)
                    page.locator('#settings-dialog button[type="submit"]').last.click()
                    local_parent_uid = next(row['uid'] for row in rows if row['sid'] == parent)
                    assert session_meta.snapshot(local_parent_uid)['fork_parent_visible'] is True
                    fresh = browser.new_context(viewport=dict(width=1100, height=850))
                    fresh_page = fresh.new_page()
                    fresh_page.goto(base)
                    expect(fresh_page.locator('#side .item')).to_have_count(2)
                    fresh.close()

                    parent_uid = page.evaluate(
                        '(sid) => S.sessions.find(row => row.sid === sid).uid', parent)
                    parent_row = page.locator(f'.item[data-uid="{parent_uid}"]')
                    parent_row.click()
                    expect(page.locator('#a-session-action')).to_have_attribute(
                        'title', '隐藏父会话')
                    parent_row.click(button='right')
                    expect(page.locator('#item-menu [data-act="hide"]')).to_be_visible()
                    expect(page.locator('#item-menu [data-act="delete"]')).to_be_hidden()
                    expect(page.locator('#item-menu [data-act="pick"]')).to_be_hidden()
                    page.keyboard.press('Escape')

                    page.locator('#a-session-action').click()
                    expect(page.locator('#side .item')).to_have_count(1)
                    assert not session_meta.snapshot(local_parent_uid).get('fork_parent_visible')
                    page.reload()
                    expect(page.locator('#side .item')).to_have_count(1)

                    # A further rewind hides its immediate parent too. Showing
                    # all creates one durable flag per current parent.
                    write(grandchild, child)
                    page.evaluate('pollSessions()')
                    expect(page.locator('#side .item')).to_have_count(1)
                    page.locator('#settings').click()
                    page.locator('#restore-fork-parents').click()
                    expect(page.locator('#side .item')).to_have_count(3)
                    page.locator('#settings-dialog button[type="submit"]').last.click()
                    page.evaluate("localStorage.setItem('forkParentChoices', JSON.stringify([['ignored',true]]))")
                    page.reload()
                    expect(page.locator('#side .item')).to_have_count(3)
                    assert not errors, errors
                    context.close()
                    print(f'PASS {"hub" if target is central else "node"}: server default, persist, hide-only actions, repeated rewind')

                # The root-stable tmux name belongs to the current child only.
                # With parents explicitly visible, selecting history must not
                # bounce back to that replacement leaf on the next terminal poll.
                child_row = next(row for row in rows if row['sid'] == child)
                term_sessions[:] = [dict(
                    name=f'agenthub-codex-{parent[:8]}', uid=child_row['uid'])]
                for target in (node, central):
                    context = browser.new_context(viewport=dict(width=1100, height=850))
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{target.server_port}/')
                    expect(page.locator('#side .item')).to_have_count(3)
                    ids = page.evaluate('''(sids) => Object.fromEntries(sids.map(sid => {
                      const row = S.sessions.find(session => session.sid === sid);
                      return [sid, row.uid];
                    }))''', [parent, child])
                    parent_uid, child_uid = ids[parent], ids[child]
                    page.evaluate('loadTermList()')
                    page.locator(f'.item[data-uid="{child_uid}"]').click()
                    page.wait_for_selector('#a-term')
                    page.locator('#a-term').click()
                    page.wait_for_function(
                        '(uid) => S.sel === uid && T.uid === uid && !!T.name', arg=child_uid)
                    page.locator(f'.item[data-uid="{parent_uid}"]').click()
                    page.wait_for_function('(uid) => S.sel === uid', arg=parent_uid)
                    page.evaluate('loadTermList()')
                    state = page.evaluate('''([parentUid, childUid]) => ({
                      selected:S.sel, termUid:T.uid, exact:takenOver(parentUid),
                      replacement:linkedTermSession(parentUid, {followReplacement:true})?.uid,
                      composerHidden:document.querySelector('#composer').classList.contains('hidden'),
                    })''', [parent_uid, child_uid])
                    assert state == dict(
                        selected=parent_uid, termUid=child_uid, exact=None,
                        replacement=child_uid, composerHidden=True), state
                    expect(page.locator('#a-session-action')).to_have_attribute(
                        'title', '隐藏父会话')
                    expect(page.locator('#a-term')).to_have_attribute(
                        'title', '切换到当前会话终端')
                    assert not errors, errors
                    context.close()
                    print(f'PASS {"hub" if target is central else "node"}: visible historical parent remains selected across terminal poll')

                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
