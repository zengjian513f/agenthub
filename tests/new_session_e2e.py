"""New-session transitions with slow lists/fonts; isolated nodes, no CLI or tmux."""
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import start_node, stop


def main():
    with tempfile.TemporaryDirectory() as root:
        node = start_node('a' * 32, 'NodeA')
        old = {'name': 'old-terminal', 'uid': node.state['row']['uid'],
               'source': 'claude', 'cwd': '/same/project'}
        node.state['pending'].append(old)
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'NodeA', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for hub_mode in [False, True]:
                    for mobile in [False, True]:
                        context = browser.new_context(viewport={
                            'width': 390 if mobile else 1280, 'height': 900})
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if hub_mode
                                else f'http://127.0.0.1:{node.server_port}/')
                        page.goto(base)
                        page.wait_for_function('S.sessions.length && T.enabled')
                        uid = page.evaluate('S.sessions[0].uid')
                        name = ('a' * 32 + '~' if hub_mode else '') + 'old-terminal'
                        new_name = ('a' * 32 + '~' if hub_mode else '') + 'same-terminal'
                        page.evaluate('(uid) => openSession(uid)', uid)
                        page.wait_for_function('document.querySelector("#msgs")?.textContent.includes("reply NodeA")')
                        page.evaluate('''(name) => {
                          T.uid = S.sel; return openTermPane(name, false, 'normal');
                        }''', name)
                        page.wait_for_function('T.ws?.readyState === 1 && T.term.buffer.active.getLine(0)?.translateToString().includes("NodeA")')
                        page.evaluate('''() => {
                          window.oldView = T.views.get(T.name); window.oldSocket = T.ws;
                          window.oldLayout = {...T.openViews.get(T.name)};
                          window.oldRequest = inflight;
                        }''')
                        assert page.evaluate('!!_es')
                        if mobile:
                            page.evaluate('showMobileList()')
                        node.state.update(search_steps=20, search_delay=.1)
                        page.locator('#q').fill('文件管理')
                        page.locator('#q').press('Enter')
                        page.wait_for_selector('#search-progress.on')
                        page.evaluate('window.oldSearch = searchAbort')
                        assert page.evaluate('''() => document.querySelector('#search-progress')
                          .getBoundingClientRect().bottom <= document.querySelector('#side').getBoundingClientRect().top''')
                        if mobile:   # 窄屏顶栏按钮全部折进 ⋯
                            page.locator('#header-more-btn').click()
                        page.locator('#new-session').click()
                        page.locator('#new-session-form label:has(input[value="codex"])').click()
                        page.locator('#new-cwd').fill('/same/project')
                        held = []
                        page.route('**/api/term/list*', lambda r: held.append(r))
                        page.locator('#new-session-go').click()
                        page.wait_for_function('document.querySelector(".new-session-wait") !== null')
                        # The list is still blocked: neither stale pixels nor the old stream
                        # may survive, and the new WebSocket must already show its output.
                        page.wait_for_function('(name) => T.name === name && T.ws?.readyState === 1', arg=new_name, timeout=3000)
                        assert page.evaluate('oldView.host.hidden && !oldView.host.getBoundingClientRect().height')
                        assert page.evaluate('!_es && oldRequest.signal.aborted && S.agent === null')
                        assert page.evaluate('oldSocket.readyState === 1 && T.views.get(oldView.name) === oldView')
                        page.wait_for_function('T.term.buffer.active.getLine(0)?.translateToString().includes("NodeA")')
                        assert page.locator('#a-term').is_visible()
                        assert held
                        assert page.evaluate('oldSearch.signal.aborted && S.results === null && S.term === ""')
                        assert not page.locator('#search-progress').is_visible()
                        page.wait_for_timeout(2200)
                        assert page.evaluate('S.results === null && visible().some(s => s.uid === S.sel)')

                        # Navigating away during the slow refresh must preserve the old
                        # terminal, including its connection and saved split layout.
                        page.evaluate('(uid) => openSession(uid)', uid)
                        page.wait_for_function('(name) => T.name === name', arg=name)
                        assert page.evaluate('T.ws === oldSocket && T.mode === oldLayout.mode')
                        while held:
                            held.pop(0).continue_()
                        page.unroute('**/api/term/list*')
                        page.wait_for_function('(name) => T.name === name && !oldView.host.hidden', arg=name)

                        # Font initialization is another asynchronous boundary. Opening a
                        # pending session must hide the old host before fonts are ready.
                        page.evaluate('''(name) => {
                          window.savedFontReady = terminalFontReady;
                          terminalFontReady = new Promise(resolve => window.releaseFont = resolve);
                          S.agent = 'previous-child'; store.set('agent', {uid:S.sel, id:S.agent});
                          window.openingPending = openPendingSession(T.pending.find(p => p.name === name));
                        }''', new_name)
                        assert page.evaluate('oldView.host.hidden && T.name === null && S.agent === null && store.get("agent") === null')
                        assert page.locator('#a-term').is_visible()
                        # Switch back while that opening is suspended; its completion must
                        # never activate the new terminal over the selected old session.
                        page.evaluate('(uid) => {void openSession(uid)}', uid)
                        page.evaluate('releaseFont(); terminalFontReady = savedFontReady')
                        page.evaluate('openingPending')
                        page.wait_for_function('(name) => T.name === name && !oldView.host.hidden', arg=name)
                        assert page.evaluate('(uid) => S.sel === uid && T.ws === oldSocket', uid)

                        # Resolving the native record also refreshes lists. A response that
                        # finishes after navigation must not reopen the resolved session.
                        resolving_lists = []
                        page.route('**/api/sessions?force=1', lambda r: resolving_lists.append(r))

                        def status(route):
                            requested = parse_qs(urlparse(route.request.url).query).get('name', [''])[0]
                            if requested == new_name:
                                route.fulfill(json={'uid': uid + '-resolved', 'name': new_name, 'running': True})
                            else:
                                route.fulfill(json={'waiting': True, 'running': True})

                        page.route('**/api/term/new-status*', status)
                        page.evaluate('(name) => openPendingSession(T.pending.find(p => p.name === name))', new_name)
                        page.wait_for_event('request', predicate=lambda r: r.url.endswith('/api/sessions?force=1'))
                        page.wait_for_timeout(50)
                        assert resolving_lists
                        page.evaluate('(uid) => openSession(uid)', uid)
                        while resolving_lists:
                            resolving_lists.pop(0).continue_()
                        page.unroute('**/api/sessions?force=1')
                        page.wait_for_function('(name) => !T.resolving.has(name)', arg=new_name)
                        assert page.evaluate('([uid, name]) => S.sel === uid && T.name === name && T.ws === oldSocket', [uid, name])
                        assert not errors, errors
                        print(f'PASS: {"hub" if hub_mode else "standalone"}, {"mobile" if mobile else "desktop"}', flush=True)
                        context.close()
                browser.close()
        finally:
            central.shutdown()
            central.server_close()
            stop(node)


if __name__ == '__main__':
    main()
