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
                    button.hover()
                    toast = page.locator('#node-toast')
                    assert toast.is_visible()
                    assert 'Orion 离线' in toast.inner_text() and 'HTTP 503' in toast.inner_text()
                    button.focus()
                    page.evaluate('window.offlineButton = document.activeElement')
                    page.evaluate('loadSessions()')
                    assert page.evaluate('document.activeElement === window.offlineButton')
                    assert toast.is_visible()
                    button.click()
                    assert button.get_attribute('aria-pressed') == 'false'
                    assert toast.is_visible() and 'HTTP 503' in toast.inner_text()
                    page.mouse.move(0, 300)
                    page.locator('#q').focus()
                    assert toast.is_visible(), 'click diagnostics must remain visible after focus moves'
                    button.dblclick()
                    assert page.evaluate('selectedNodeIds()') == ['a' * 32]
                    assert toast.is_visible()
                    assert page.locator('#side .item').count() == 1, 'cached offline sessions remain selectable'
                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert button.is_visible() and toast.is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=f'/tmp/agenthub-node-offline-{scheme}.png')
                    nodes[0].state['offline'] = False
                    page.evaluate('loadSessions()')
                    page.wait_for_function('Nodes.list.find(n => n.name === "Orion").online === true')
                    assert 'node-offline' not in button.get_attribute('class')
                    assert not toast.is_visible()
                    page.wait_for_function("(color) => getComputedStyle(document.querySelector('#node-chips button')).color === color", arg=healthy_color)
                    assert not errors, errors
                    context.close()
                browser.close()
            print('PASS: offline gray in both themes, hover/focus/click reasons, stable focus, filtering, mobile, recovery')
        finally:
            for srv in [central, *nodes]: stop(srv)


if __name__ == '__main__':
    main()
