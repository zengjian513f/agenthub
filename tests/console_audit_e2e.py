"""Browser audit must keep flowing and must notice a lost console button.

Isolated hub + one simulated node; no CLI, no tmux, nothing on 8710. Covers the
two failure shapes that left the "console button disappears" report without
evidence: a >64 KB snapshot that used to poison the keepalive queue for the
rest of the page's life, and a header change that nothing recorded.
"""
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import start_node, stop
from session_menu_e2e import MenuNode


def audited(node):
    """Every browser event the node has received so far, oldest first."""
    return [event for path, body in node.state['writes'] if path == '/api/audit/browser'
            for event in body.get('events', [])]


def wait_event(page, node, name, predicate=lambda e: True, timeout=8000):
    deadline = timeout
    while deadline > 0:
        hits = [e for e in audited(node) if e['event'] == name and predicate(e)]
        if hits:
            return hits[-1]
        page.wait_for_timeout(100)
        deadline -= 100
    raise AssertionError(f'no {name} event reached the node; got '
                         + str(sorted({e["event"] for e in audited(node)})))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'MenuNode')
        node.RequestHandlerClass = MenuNode
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
                context = browser.new_context(viewport={'width': 1400, 'height': 900})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{central.server_port}/agenthub/')
                page.wait_for_function('S.sessions.length && T.enabled && T.listLoaded')
                uid = 'claude:' + 'a' * 32 + '~same-file-hash'
                name = 'a' * 32 + '~menu-terminal'
                page.evaluate('(uid) => openSession(uid)', uid)
                page.locator('#msgs').wait_for()

                # 1. Rendering the detail pane is itself an event, and the snapshot now
                #    carries the header state instead of only the messages.
                rendered = wait_event(page, node, 'detail.rendered', lambda e: e['data']['source'] == 'render')
                assert rendered['data']['has_head'] and rendered['data']['has_console_button'], rendered
                assert rendered['data']['children'][:2] == ['dhead', 'msgs'], rendered
                wait_event(page, node, 'detail.rendered', lambda e: e['data']['source'] == 'loading')
                layout = wait_event(page, node, 'header.layout')
                assert layout['data']['tier'] == 'wide' and 'a-term' in layout['data']['inline'], layout
                assert layout['data']['title_overflow'] == 0, layout
                state = wait_event(page, node, 'console.button.state')
                assert state['data']['ok'] and state['data']['hit'] and state['data']['in_right'], state
                snapshot = wait_event(page, node, 'dom.snapshot', lambda e: 'header' in e['data'])
                assert snapshot['data']['header']['ok'] and snapshot['data']['header']['label'], snapshot['data']['header']

                # 2. Opening and closing the terminal records who did it and the mode.
                page.locator('#a-term').click()
                page.wait_for_function('T.ws?.readyState === 1')
                opened = wait_event(page, node, 'terminal.pane', lambda e: e['data']['action'] == 'open')
                assert opened['data']['target'] == name and opened['data']['requested_mode'] == 'full', opened
                wait_event(page, node, 'terminal.pane', lambda e: e['data']['action'] == 'toggle')
                page.locator('#a-term').click()
                page.wait_for_function('T.mode === "collapsed"')
                wait_event(page, node, 'console.button.state', lambda e: e['data']['label'] == '切换到终端')

                # 3. A button that leaves the DOM is reported with the header HTML, and
                #    its return is reported too.
                page.evaluate('document.querySelector("#a-term").remove()')
                missing = wait_event(page, node, 'console.button.missing')
                assert missing['severity'] == 'error' and not missing['data']['present'], missing['data']
                assert missing['data']['head'] and missing['data']['detail_children'][0] == 'dhead', missing['data']
                assert missing['content']['dhead'].startswith('<div class="dhead'), missing['content']['dhead'][:100]
                assert missing['data']['terminal']['name'] == name, missing['data']['terminal']
                assert missing['data']['reason'] in ('head-mutation', 'detail-mutation', 'periodic'), missing['data']['reason']
                before = len([e for e in audited(node) if e['event'] == 'console.button.missing'])
                page.wait_for_timeout(1500)   # a lost button must not spam the store
                assert len([e for e in audited(node) if e['event'] == 'console.button.missing']) == before
                page.evaluate('(uid) => openSession(uid)', uid)
                restored = wait_event(page, node, 'console.button.restored')
                assert restored['data']['missing_ms'] > 0 and restored['data']['present'], restored['data']

                # 4. A button still in the DOM but pushed outside #right (the clipping
                #    shape) is reported with the geometry that proves it.
                page.evaluate('document.querySelector("#a-term").style.marginLeft = "4000px"')
                clipped = wait_event(page, node, 'console.button.missing', lambda e: e['data']['present'])
                assert clipped['data']['in_right'] is False and clipped['data']['hit'] is False, clipped['data']
                assert clipped['data']['rect'][0] > 1400, clipped['data']['rect']
                page.evaluate('document.querySelector("#a-term").style.marginLeft = ""')
                wait_event(page, node, 'console.button.restored', lambda e: e['data']['in_right'])

                # 5. Oversized content no longer wedges the queue: it arrives truncated
                #    and later events still flow.
                page.evaluate('''() => {
                  browserAuditEvent('probe.big', {}, {pad: 'x'.repeat(100000)});
                  browserAuditEvent('probe.after', {});
                }''')
                big = wait_event(page, node, 'probe.big')
                assert big['content']['truncated'] and big['content']['bytes'] > 64 * 1024, big['content'].keys()
                assert big['data']['content_truncated'] is True
                wait_event(page, node, 'probe.after')

                # 6. Native dialogs are on the record with their text and answer.
                page.once('dialog', lambda d: d.dismiss())
                assert page.evaluate('confirm("really?")') is False
                wait_event(page, node, 'dialog.shown', lambda e: e['data'] == {'kind': 'confirm', 'text': 'really?'})
                wait_event(page, node, 'dialog.closed', lambda e: e['data'] == {'kind': 'confirm', 'result': False})

                # 7. The pagehide beacon has a 64 KB budget for the whole page: every
                #    queued event still arrives (content dropped), and the final
                #    page.hidden snapshot keeps its content.
                page.evaluate('''() => { for (let i = 0; i < 30; i++) browserAuditEvent('probe.unload', {i}, {pad: 'y'.repeat(4000)}); }''')
                page.evaluate('window.dispatchEvent(new Event("pagehide"))')
                page.wait_for_timeout(800)
                unload = [e for e in audited(node) if e['event'] == 'probe.unload']
                assert len(unload) == 30, len(unload)
                assert all(e['content'] is None and e['data']['content_dropped'] for e in unload), unload[0]
                hidden = wait_event(page, node, 'page.hidden')
                assert hidden['content'] and hidden['content']['messages'], hidden['content']
                assert not errors, errors
                context.close()
                browser.close()
        finally:
            stop(node)
    print('console audit e2e ok')


if __name__ == '__main__':
    main()
