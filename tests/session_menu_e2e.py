"""Session menu hit testing over xterm; isolated HTTP/SSE/WS, no CLI or tmux."""
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import NodeHandler, start_node, stop


class MenuNode(NodeHandler):
    def do_GET(self):
        if urlparse(self.path).path == '/api/term/list':
            return self._json({'enabled': True, 'sources': {'claude': True},
                               'sessions': [{'name': 'menu-terminal',
                                             'uid': self.state['row']['uid']}],
                               'pending': []})
        return super().do_GET()

    def messages(self, q):
        data = super().messages(q)
        agent = q.get('agent', [''])[0]
        if agent:
            data['meta'] = {**data['meta'], 'agent_id': agent,
                            'parent_title': data['meta']['title'], 'title': agent}
        return data


def assert_menu_hits(page):
    # Visibility alone passes for clipped elements. Test actual pointer targets,
    # including the first row crossing the terminal's resize handle.
    hits = page.locator('#session-view-menu button').evaluate_all('''items =>
      items.map(e => {
        const r = e.getBoundingClientRect();
        return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      })''')
    assert hits == [True] * 6, hits


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'MenuNode')
        node.RequestHandlerClass = MenuNode
        node.state['row']['agent_items'] = [
            {'id': f'agent-{i}', 'title': f'Agent task {i}', 'type': 'Explore'}
            for i in range(5)]
        node.state['row']['agents'] = 5
        node.state['messages'] *= 50  # Exercise independent message scrolling.
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'MenuNode', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [False, True]:
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped
                            else f'http://127.0.0.1:{node.server_port}/')
                    uid = 'claude:' + ('a' * 32 + '~' if scoped else '') + 'same-file-hash'
                    name = ('a' * 32 + '~' if scoped else '') + 'menu-terminal'
                    for width, height in [(1698, 986), (900, 650), (721, 650), (720, 650), (390, 844)]:
                        context = browser.new_context(viewport={'width': width, 'height': height},
                                                      color_scheme='dark' if scoped else 'light')
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(base)
                        page.wait_for_function('S.sessions.length && T.enabled')
                        page.evaluate('(uid) => openSession(uid)', uid)
                        page.locator('#a-view-switch').wait_for()
                        page.evaluate('''async ([uid, name]) => {
                          T.uid = uid; await openTermPane(name, false, 'full');
                        }''', [uid, name])
                        page.wait_for_function('T.ws?.readyState === 1')
                        for mode in (['full', 'normal', 'collapsed'] if width > 720 else ['full']):
                            page.evaluate('''mode => {
                              T.mode = mode; T.height = 10000; layoutTermPane();
                            }''', mode)
                            page.locator('#a-view-switch').click()
                            if mode == 'collapsed':
                                assert page.locator('#msgs').evaluate('''e => {
                                  e.scrollTop = 20;
                                  return e.scrollHeight > e.clientHeight && e.scrollTop === 20;
                                }''')
                            assert_menu_hits(page)
                            page.locator('#a-view-switch').click()
                            if mode != 'collapsed' and width > 720:
                                assert page.locator('.term-resizer').evaluate('''e => {
                                  const r = e.getBoundingClientRect();
                                  return e === document.elementFromPoint(r.right - 20, r.top + 3);
                                }''')
                        # Click an actual submenu entry, then return to the main
                        # view and verify the retained terminal layout restores.
                        page.evaluate("T.mode = 'full'; rememberTermLayout(T.name); layoutTermPane()")
                        page.locator('#a-view-switch').click()
                        page.locator('[data-agent="agent-4"]').click()
                        page.wait_for_function("S.agent === 'agent-4' && document.querySelector('.dhead h2')?.textContent.includes('agent-4')")
                        assert page.locator('#termpane').is_hidden()
                        page.locator('#a-view-switch').click()
                        page.locator('#session-view-menu [data-agent=""]').click()
                        page.wait_for_function("!S.agent && !document.querySelector('#termpane').classList.contains('hidden')")
                        page.locator('#a-view-switch').click()
                        assert_menu_hits(page)
                        # Native modal dialogs must still cover the menu.
                        page.evaluate("document.querySelector('#settings-dialog').showModal()")
                        assert page.locator('#settings-dialog').evaluate('''e => {
                          const r = e.getBoundingClientRect();
                          return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
                        }''')
                        assert not errors, errors
                        print(f'{"Hub" if scoped else "Node"} {width}x{height}: menu hit targets, view switching, resize handle and modal passed', flush=True)
                        context.close()
                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
