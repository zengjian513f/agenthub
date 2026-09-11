"""Offline machine styling and diagnostics on isolated HTTP nodes; no CLI."""
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


def main():
    with tempfile.TemporaryDirectory() as root:
        nodes = [start_node(c * 32, name) for c, name in [('a', 'Orion'), ('b', 'Lyra')]]
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        for node in nodes:
            registry.register({'name': node.state['name'], 'url': f'http://127.0.0.1:{node.server_port}', 'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True; central.registry = registry; central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for scheme in ['light', 'dark']:
                    context = browser.new_context(viewport={'width': 1280, 'height': 900}, color_scheme=scheme)
                    page = context.new_page(); errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('dialog', lambda dialog: dialog.accept())
                    page.goto(f'http://127.0.0.1:{central.server_port}/agenthub/')
                    page.wait_for_function('S.sessions.length === 2 && Nodes.list.every(n => n.online)')
                    button = page.locator('#node-chips button[data-node="' + 'a' * 32 + '"]')
                    healthy_color = button.evaluate('(e) => getComputedStyle(e).color')
                    nodes[0].state['offline'] = True
                    page.evaluate('loadSessions()')
                    page.wait_for_function('Nodes.list.find(n => n.name === "Orion").online === false')
                    assert button.is_visible() and button.is_enabled()
                    assert 'node-offline' in button.get_attribute('class')
                    assert button.evaluate('(e) => getComputedStyle(e).filter') == 'grayscale(1)'
                    page.wait_for_function("(color) => getComputedStyle(document.querySelector('#node-chips button')).color !== color", arg=healthy_color)
                    appearance = '''e => {
                      const s = getComputedStyle(e);
                      return {color: s.color, count: getComputedStyle(e.querySelector('b')).color,
                        background: s.backgroundColor, shadow: s.boxShadow, cursor: s.cursor};
                    }'''
                    for selected in [False, True]:
                        page.mouse.move(0, 300)
                        page.evaluate('''selected => {
                          selected ? Nodes.off.delete('a'.repeat(32)) : Nodes.off.add('a'.repeat(32));
                          renderNodes();
                        }''', selected)
                        page.wait_for_timeout(200)  # Let the selection color transition finish.
                        before = button.evaluate(appearance)
                        if scheme == 'light':
                            assert before['color'] == before['count'] == 'rgb(176, 176, 176)', before
                        assert before['cursor'] == 'not-allowed'
                        button.hover()
                        page.wait_for_timeout(200)
                        assert button.evaluate(appearance) == before, 'offline hover must not change appearance'
                        assert not button.get_attribute('title')
                        assert not page.locator('#node-toast').count()
                        assert not page.locator('#node-notice').is_visible()
                        selection = page.evaluate('selectedNodeIds()')
                        for activate in [button.click, button.dblclick, lambda: button.press('Enter')]:
                            with page.expect_event('dialog') as popup:
                                activate()
                            assert 'Orion 离线' in popup.value.message and 'HTTP 503' in popup.value.message
                            assert page.evaluate('selectedNodeIds()') == selection
                            assert button.get_attribute('aria-pressed') == str(selected).lower()
                        button.dispatch_event('dblclick')
                        assert page.evaluate('selectedNodeIds()') == selection
                    button.focus()
                    page.evaluate('window.offlineButton = document.activeElement')
                    page.evaluate('loadSessions()')
                    assert page.evaluate('document.activeElement === window.offlineButton')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    # 窄屏机器筛选收进下拉，展开后是同一组按钮，离线态照旧
                    assert not button.is_visible()
                    page.locator('#node-pick').click()
                    assert button.is_visible()
                    selection = page.evaluate('selectedNodeIds()')
                    with page.expect_event('dialog') as popup:
                        button.click()
                    assert 'HTTP 503' in popup.value.message
                    assert page.evaluate('selectedNodeIds()') == selection
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=f'/tmp/agenthub-node-offline-{scheme}.png')
                    nodes[0].state['offline'] = False
                    page.evaluate('loadSessions()')
                    page.wait_for_function('Nodes.list.find(n => n.name === "Orion").online === true')
                    assert 'node-offline' not in button.get_attribute('class')
                    page.mouse.move(0, 300)
                    page.wait_for_function("(color) => getComputedStyle(document.querySelector('#node-chips button')).color === color", arg=healthy_color)
                    button.click()
                    assert button.get_attribute('aria-pressed') == 'false', 'online filters must work again after recovery'
                    button.dblclick()
                    assert page.evaluate('selectedNodeIds()') == ['a' * 32]
                    assert not errors, errors
                    context.close()
                browser.close()
            print('PASS: pale offline text in both selection states, inert hover, error dialogs without filtering, no offline banner, stable focus, mobile, recovery')
        finally:
            for srv in [central, *nodes]: stop(srv)


if __name__ == '__main__':
    main()
