"""Pending session discard and selection; isolated nodes, no CLI or tmux."""
import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import NodeHandler, start_node, stop


class PendingNode(NodeHandler):
    def do_GET(self):
        if urlparse(self.path).path == '/api/live':
            uids = [self.state['row']['uid']] if self.state.get('live') else []
            return self._json({'uids': uids, 'tmux_uids': [], 'started_at': {}})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == '/api/term/kill':
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.state['writes'].append(('/api/term/kill', body))
            if body['name'] in self.state.get('fail_kill', []):
                return self._json({'error': 'fixture stop failed'}, 503)
            self.state['pending'] = [p for p in self.state['pending'] if p['name'] != body['name']]
            return self._json({'ok': True})
        return super().do_POST()


def seed(node):
    node.state.update(deleted=False, writes=[], fail_kill=[], live=False)
    node.state['pending'] = [
        {'name': f'pending-{i}', 'source': source, 'cwd': '/same/project',
         'started': time.time(), 'title': f'New {source} {i}'}
        for i, source in enumerate(['claude', 'codex'])]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        nodes = [start_node(char * 32, name) for char, name in [('a', 'NodeA'), ('b', 'NodeB')]]
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        for node in nodes:
            node.RequestHandlerClass = PendingNode
            registry.register({'name': node.state['name'], 'url': f'http://127.0.0.1:{node.server_port}',
                               'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry, central.hub_mode = registry, True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [False, True]:
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped
                            else f'http://127.0.0.1:{nodes[0].server_port}/')
                    for mobile in [False, True]:
                        for node in nodes:
                            seed(node)
                        count = 6 if scoped else 3
                        prefix = 'a' * 32 + '~' if scoped else ''
                        pending_uid = 'tmux:' + prefix + 'pending-0'
                        other_uid = 'tmux:' + prefix + 'pending-1'
                        recorded_uid = 'claude:' + prefix + 'same-file-hash'
                        context = browser.new_context(
                            viewport={'width': 390 if mobile else 1280, 'height': 900},
                            has_touch=mobile)
                        page = context.new_page()
                        errors, dialogs = [], []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        def accept(dialog):
                            dialogs.append(dialog.message)
                            dialog.accept()
                        page.on('dialog', accept)
                        page.goto(base)
                        page.wait_for_function('(count) => sidebarSessions().length === count && T.enabled', arg=count)
                        page.locator('#allcount').click()
                        row = page.locator(f'#side .item[data-uid="{pending_uid}"]')

                        def menu(target=row):
                            if mobile:
                                target.dispatch_event('pointerdown', {
                                    'pointerType': 'touch', 'clientX': 130, 'clientY': 240})
                                page.wait_for_timeout(550)
                                target.dispatch_event('pointerup', {'pointerType': 'touch'})
                                target.dispatch_event('click')  # synthetic click following long press
                            else:
                                target.click(button='right')
                            assert page.locator('#item-menu').is_visible()

                        menu()
                        assert page.locator('#item-menu button:visible').all_text_contents() == ['丢弃会话', '多选…']
                        assert not page.evaluate('S.sel')
                        page.locator('#item-menu [data-act="pick"]').click()
                        assert row.locator('.item-pick').is_checked()
                        assert page.locator('#side .item-pick').count() == count
                        page.evaluate('renderSide()')  # polling redraw keeps pending selections and count
                        assert row.locator('.item-pick').is_checked()
                        assert page.locator('#side-picked').inner_text() == '已选 1 项'
                        row.click()
                        assert not row.locator('.item-pick').is_checked()
                        group = row.locator('xpath=ancestor::div[contains(@class,"group")]')
                        group.locator('.ghead-pick').click()
                        assert group.locator('.item-pick:checked').count() == 3
                        group.locator('.ghead-pick').click()
                        assert group.locator('.item-pick:checked').count() == 0
                        page.locator('#side-pick-all').click()
                        assert page.locator('#side .item-pick:checked').count() == count
                        page.locator('#side-pick-all').click()
                        assert page.locator('#side .item-pick:checked').count() == 0
                        page.locator('#side-pick-cancel').click()

                        # Existing records retain their normal delete/stop policy.
                        recorded = page.locator(f'#side .item[data-uid="{recorded_uid}"]')
                        menu(recorded)
                        assert page.locator('#item-menu button:visible').all_text_contents() == ['删除会话', '多选…']
                        nodes[0].state['live'] = True
                        page.evaluate('async () => {closeItemMenu(); await refreshLive(true)}')
                        menu(recorded)
                        assert page.locator('#item-menu button:visible').all_text_contents() == ['停止会话', '多选…']
                        nodes[0].state['live'] = False
                        page.evaluate('async () => {closeItemMenu(); await refreshLive(true)}')

                        # Cancellation makes no mutation request.
                        page.remove_listener('dialog', accept)
                        page.once('dialog', lambda d: d.dismiss())
                        menu()
                        page.locator('#item-menu [data-act="delete"]').click()
                        assert not any(p == '/api/term/kill' for p, _ in nodes[0].state['writes'])
                        page.on('dialog', accept)

                        # Discard an open draft and retained terminal; keep the console placeholder.
                        row.click()
                        page.wait_for_function('T.ws?.readyState === 1')
                        page.evaluate('''uid => {
                          composerDrafts.set(uid, {text: 'unsent', attachments: [], quotes: []});
                          T.mode = 'normal'; rememberTermLayout(T.name);
                        }''', pending_uid)
                        if mobile:
                            page.evaluate('showMobileList()')
                        menu()
                        page.locator('#item-menu [data-act="delete"]').click()
                        page.wait_for_function('(uid) => !T.pending.some(p => pendingUid(p.name) === uid) && !sessionDeleteBusy', arg=pending_uid)
                        assert row.count() == 0
                        assert page.evaluate('(uid) => !composerDrafts.has(uid) && !T.views.has(uid.slice(5)) && !T.openViews.has(uid.slice(5)) && S.sel === null', pending_uid)
                        assert page.locator('#a-term').count() == 1
                        assert '草稿' in dialogs[-1] and '回收站' not in dialogs[-1]
                        kills = [body for path, body in nodes[0].state['writes'] if path == '/api/term/kill']
                        assert kills[-1]['name'] == 'pending-0'
                        assert len(nodes[1].state['pending']) == 2

                        # Mixed batch: one failed stop remains selected, others are removed.
                        nodes[0].state['fail_kill'] = ['pending-1']
                        menu(page.locator(f'#side .item[data-uid="{other_uid}"]'))
                        page.locator('#item-menu [data-act="pick"]').click()
                        page.locator('#side-pick-all').click()
                        page.locator('#side-pick-delete').click()
                        page.wait_for_function('(uid) => pickedSessions.size === 1 && pickedSessions.has(uid) && !sessionDeleteBusy', arg=other_uid)
                        assert page.locator('#side-picked').inner_text() == '已选 1 项'
                        assert page.locator('#side-pick-delete').inner_text() == '丢弃 (1)'
                        assert any('回收站' in text and '草稿' in text for text in dialogs)
                        assert any('fixture stop failed' in text for text in dialogs)
                        for node in nodes[:2 if scoped else 1]:
                            deletes = [body for path, body in node.state['writes'] if path == '/api/sessions/delete']
                            assert deletes and all(body['uids'] == ['claude:same-file-hash'] for body in deletes)
                        nodes[0].state['fail_kill'] = []
                        page.locator('#side-pick-delete').click()
                        page.wait_for_function('!S.picking && !sessionDeleteBusy && !sidebarSessions().length')
                        page.reload()
                        page.wait_for_function('T.listLoaded && !!S.sig')
                        assert page.locator('#side .item').count() == 0
                        assert not errors, errors
                        print(f'{"Hub" if scoped else "Node"} {"touch" if mobile else "desktop"}: menu, selection, cancel, draft cleanup, mixed batch, failure/retry, reload passed', flush=True)
                        context.close()
                browser.close()
        finally:
            for srv in [central, *nodes]:
                stop(srv)


if __name__ == '__main__':
    main()
