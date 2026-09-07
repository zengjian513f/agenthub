"""Free browser integration tests against isolated simulated machines."""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import federation, hub, server
from hub_fixture import start_node, stop


class MountedHub(hub.HubHandler):
    """Emulate Nginx stripping /agenthub/ while the browser retains its base URL."""
    def dispatch(self):
        if self.path.startswith('/agenthub/'):
            self.path = self.path[len('/agenthub'):]
        return super().dispatch()


def main():
    with tempfile.TemporaryDirectory() as root:
        nodes = [start_node(char * 32, name) for char, name in [('a', 'NodeA'), ('b', 'NodeB'), ('c', 'Vega')]]
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        for n in nodes:
            registry.register({'name': n.state['name'], 'url': f'http://127.0.0.1:{n.server_port}', 'token': n.state['token']})
        srv = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        srv.daemon_threads = True; srv.registry = registry; srv.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1280, 'height': 900})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                base = f'http://127.0.0.1:{srv.server_port}/agenthub/'
                page.goto(base)
                page.wait_for_function('S.sessions.length === 3 && Nodes.list.length === 3 && !!Nodes.capabilities["' + 'b' * 32 + '"]')
                assert page.locator('.ghead').count() == 3
                assert all(name in page.locator('#side').inner_text() for name in ['NodeA', 'NodeB', 'Vega'])
                assert len(set(page.evaluate('S.sessions.map(s => s.uid)'))) == 3
                # Scope is a single choice; zero activity still leaves both counts visible.
                page.wait_for_function('document.querySelector("#session-total").textContent === "3"')
                assert page.locator('#session-active').inner_text() == '0'
                page.locator('#livecount').click()
                page.locator('#livecount').click()
                assert page.locator('#livecount').get_attribute('aria-checked') == 'true'
                assert page.locator('#side .item').count() == 0
                page.reload()
                page.wait_for_function('S.sessions.length === 3 && Nodes.list.length === 3')
                assert page.locator('#livecount').get_attribute('aria-checked') == 'true'
                page.locator('#livecount').press('ArrowRight')
                assert page.locator('#allcount').get_attribute('aria-checked') == 'true'
                assert page.locator('#side .item').count() == 3
                page.locator('#allcount').click()
                assert page.locator('#session-scope [aria-checked="true"]').count() == 1
                def check_toolbar():
                    styles = page.evaluate("""() => ['#node-chips','#chips','#view'].map(q=>{
                      const group=document.querySelector(q), r=group.getBoundingClientRect();
                      const s=getComputedStyle(group.querySelector('button.on'));
                      return {top:r.top,height:r.height,background:s.backgroundColor,
                        radius:getComputedStyle(group).borderRadius,buttonHeight:s.height};
                    })""")
                    assert max(r['top'] for r in styles)-min(r['top'] for r in styles) < 1, styles
                    assert all(r == styles[0] for r in styles), styles
                    assert page.locator('header #node-chips').count() == 1
                    assert page.locator('#node-chips').get_by_role('button', name='全部', exact=True).count() == 0
                    assert page.locator('#session-scope').bounding_box()['width'] == 162
                    assert all(b.bounding_box()['width'] == 80 for b in page.locator('#session-scope button').all())
                check_toolbar()
                page.locator('header').screenshot(path='/tmp/agenthub-toolbar-after-desktop.png')
                # Single/multi node filters and independent Agent Type intersection.
                page.get_by_role('button', name='NodeB 1', exact=True).dblclick()
                page.wait_for_function('visible().length === 1')
                assert page.locator('#session-total').inner_text() == '1'
                page.get_by_role('button', name='NodeA 1', exact=True).click()
                page.wait_for_function('visible().length === 2')
                assert page.locator('#session-total').inner_text() == '2'
                page.locator('#chips button[data-source="claude"]').click()
                assert page.locator('#session-total').inner_text() == '0'
                page.locator('#chips button[data-source="claude"]').click()
                page.locator('#q').fill('needle'); page.locator('#q').press('Enter')
                page.wait_for_function('S.results?.length === 2')
                assert {r['node_name'] for r in page.evaluate('S.results')} == {'NodeA', 'NodeB'}
                assert '命中' in page.locator('.side-search #stat').inner_text()
                assert page.locator('#session-total').inner_text() == '2'
                # Open identical native IDs on distinct nodes, render media and SSE.
                for i, name in enumerate(['NodeA', 'NodeB']):
                    uid = federation.qualify(chr(97 + i) * 32, 'claude:same-file-hash', True)
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('(name) => document.querySelector("#msgs")?.textContent.includes("reply " + name)', arg=name)
                    page.wait_for_function('Array.from(document.querySelectorAll("#msgs img")).some(i => i.complete && i.naturalWidth > 0)')
                    nodes[i].state['messages'].append({'role': 'assistant', 'text': name + ' streamed update', 'ts': '2026-09-07T00:01:00Z'})
                    page.wait_for_function('(name) => document.querySelector("#msgs")?.textContent.includes(name + " streamed update")', arg=name)
                # Real nodes heartbeat every 20s: an idle stream must outlive HTTP's 10s timeout.
                nodes[1].state['pause_stream'] = True
                watch_count = len([p for p, _ in nodes[1].state['gets'] if p == '/api/watch'])
                page.wait_for_timeout(12000)
                assert len([p for p, _ in nodes[1].state['gets'] if p == '/api/watch']) == watch_count
                nodes[1].state['pause_stream'] = False
                # New-session machine changes both capabilities and directory suggestions.
                page.locator('#new-session').click()
                page.locator('#new-node').select_option('b' * 32)
                page.locator('#new-cwd').fill('/home/')
                page.wait_for_function('cwdCompletion.rows.includes("/home/NodeB/work/")')
                page.locator('#new-cwd').fill('/home/NodeB/work')
                page.locator('#new-session-go').click()
                page.wait_for_function('String(S.sel).startsWith("tmux:' + 'b' * 32 + '~")')
                assert nodes[1].state['writes'][-1][0] in {'/api/term/create', '/api/term/claim', '/api/audit/browser'}
                creates = [b for p, b in nodes[1].state['writes'] if p == '/api/term/create']
                assert creates[-1]['cwd'] == '/home/NodeB/work' and creates[-1]['request_id']
                assert not any(p == '/api/term/create' for p, _ in nodes[0].state['writes'])
                # WebSocket bidirectional transport (no tmux/CLI involved).
                echoed = page.evaluate('''async () => {
                  const url = new URL(appUrl('api/term/attach')); url.protocol = 'ws:';
                  url.searchParams.set('name', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa~same-terminal');
                  return await new Promise((resolve, reject) => {
                    const ws = new WebSocket(url); ws.binaryType = 'arraybuffer';
                    const timer = setTimeout(() => {ws.close(); reject(new Error('WS timeout'));}, 5000);
                    ws.onopen = () => ws.send('terminal-echo');
                    ws.onmessage = e => {if(e.data === 'terminal-echo') {clearTimeout(timer); ws.close(); resolve(e.data);}};
                    ws.onerror = () => reject(new Error('WS failed'));
                  });
                }''')
                assert echoed == 'terminal-echo'
                # Binary upload routing preserves bytes and filename, scopes media URL.
                uploaded = page.evaluate('''async () => {
                  const u = new URL(appUrl('api/session/attachment'));
                  u.searchParams.set('uid', 'claude:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa~same-file-hash');
                  u.searchParams.set('name', 'same-name.png');
                  return await (await fetch(u, {method:'POST', body: new Uint8Array([0,255,17,0])})).json();
                }''')
                assert uploaded['name'] == 'same-name.png' and 'a' * 32 in uploaded['media']['src']
                assert nodes[0].state['uploads'][-1][1] == bytes([0, 255, 17, 0])
                assert page.locator('#nodes-dialog, #nodes-form').count() == 0
                assert page.get_by_role('button', name='管理机器', exact=True).count() == 0
                # Explicit node deep link resolves duplicate native IDs and survives reload.
                deep = context.new_page()
                deep.goto(base + '?node=' + 'b' * 32 + '&sid=claude:same-native-id')
                deep.wait_for_function('S.sel === "claude:' + 'b' * 32 + '~same-file-hash"')
                deep.reload()
                deep.wait_for_function('S.sel === "claude:' + 'b' * 32 + '~same-file-hash"')
                deep.close()
                # Offline machine doesn't hide healthy results; local UI stays independent.
                nodes[2].state['offline'] = True
                for button in page.locator('#node-chips button[aria-pressed="false"]').all():
                    button.click()
                page.evaluate('() => loadSessions()')
                page.wait_for_function('S.sessions.some(s => s.node_name === "Vega" && s.stale)')
                assert 'Vega' in page.locator('#node-notice').inner_text()
                page.locator('#q').fill('needle'); page.locator('#q').press('Enter')
                page.wait_for_function('S.results?.length === 2')
                # Mobile: same machine controls, no horizontal document overflow.
                page.set_viewport_size({'width': 390, 'height': 844})
                page.evaluate('showMobileList()')
                check_toolbar()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('header').screenshot(path='/tmp/agenthub-toolbar-after-mobile.png')
                page.screenshot(path='/tmp/agenthub-hub-mobile.png')
                local = context.new_page(); local.on('pageerror', lambda e: errors.append(str(e)))
                local.goto(f'http://127.0.0.1:{nodes[0].server_port}')
                local.wait_for_function('S.sessions.length === 1')
                assert not local.locator('#node-chips').is_visible()
                assert local.evaluate('S.sessions[0].uid') == 'claude:same-file-hash'
                assert not errors, errors
                browser.close()
                print('PASS: three-node DOM, filtering, search, SSE, media, create routing, WebSocket, upload, offline, mobile, standalone')
        finally:
            for item in [srv, *nodes]: stop(item)


if __name__ == '__main__':
    main()
