"""Search progress, cancellation and offline nodes; isolated services, no CLI."""
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import start_node, stop


def main():
    with tempfile.TemporaryDirectory() as root:
        nodes = [start_node(c * 32, name) for c, name in [('a', 'Fast'), ('b', 'Slow'), ('c', 'Offline')]]
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'], monitor=False)
        for node in nodes:
            registry.register({'name': node.state['name'], 'url': f'http://127.0.0.1:{node.server_port}',
                               'token': node.state['token']})
        registry.health['c' * 32] = {'online': False, 'error': '连接超时', 'error_code': 'timeout',
                                     'offline_since': time.time() - 3600}
        nodes[1].state.update(search_steps=20, search_delay=.1, search_prepare_delay=.6)
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for mobile in [False, True]:
                    page = browser.new_page(viewport={'width': 390 if mobile else 1280, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{central.server_port}/agenthub/')
                    page.wait_for_function('S.sessions.length && T.enabled')
                    offline_requests = len(nodes[2].state['gets'])
                    page.locator('#q').fill('文件管理')
                    page.locator('#q').press('Enter')
                    page.wait_for_function('S.results?.length === 1 && !!searchAbort', timeout=1500)
                    assert '离线跳过' in page.locator('.search-progress-nodes').inner_text()
                    assert '准备中' in page.locator('.search-progress-nodes').inner_text()
                    assert '%' not in page.locator('#search-progress b').inner_text()
                    assert page.evaluate('getComputedStyle(document.querySelector("#search-progress i")).animationName === "none"')
                    page.wait_for_function('document.querySelector("#search-progress b").textContent.includes("%")')
                    assert page.locator('.search-progress-track').get_attribute('aria-valuenow') is not None
                    assert '/ 21 个会话' in page.locator('#search-progress b').inner_text()
                    assert '已完成' in page.locator('.search-progress-nodes').inner_text()
                    assert 'Fast' in page.locator('#side').inner_text()
                    assert page.evaluate('''() => document.querySelector('#search-progress').getBoundingClientRect().bottom
                        <= document.querySelector('#side').getBoundingClientRect().top''')
                    elapsed = page.evaluate('''async () => {
                        const start = performance.now(); await fetch(appUrl('api/nodes'));
                        return performance.now() - start;
                    }''')
                    assert elapsed < 500, elapsed
                    page.wait_for_function('S.results?.length === 2 && !searchAbort')
                    assert 'Offline 离线，未搜索' in page.locator('#stat').inner_text()
                    assert len(nodes[2].state['gets']) == offline_requests

                    # Editing a query must invalidate a running response, even if
                    # the server later finishes its previous scan successfully.
                    page.locator('#q').press('Enter')
                    page.wait_for_function('S.results?.length === 1 && !!searchAbort')
                    page.evaluate('window.cancelledSearch = searchAbort')
                    page.locator('#q').fill('changed title filter')
                    assert page.evaluate('cancelledSearch.signal.aborted && S.results === null')
                    page.wait_for_timeout(2800)
                    assert page.evaluate('S.results === null && S.term === "changed title filter"')
                    assert not page.locator('#search-progress').is_visible()

                    page.locator('#q').fill('文件管理')
                    page.locator('#q').press('Enter')
                    page.wait_for_selector('#search-progress.on')
                    page.locator('#search-cancel').click()
                    assert page.evaluate('!searchAbort && S.results === null && S.term === ""')
                    assert page.locator('#side .item').count() == 2
                    assert not errors, errors
                    print(f'PASS: progressive search, offline skip, responsive API, cancellation ({"mobile" if mobile else "desktop"})', flush=True)
                    page.close()
                browser.close()
        finally:
            stop(central)
            for node in nodes:
                stop(node)


if __name__ == '__main__':
    main()
