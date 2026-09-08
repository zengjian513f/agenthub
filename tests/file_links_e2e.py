"""Free browser regression: reply links through a node and a mounted Hub.

Uses isolated fixture sessions and files; never starts or sends to a CLI.
Run: python tests/file_links_e2e.py
"""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_fixture import NodeHandler, PNG, start_node, stop
from hub_e2e import MountedHub


class FileNode(NodeHandler):
    def do_POST(self):
        if urlparse(self.path).path == '/api/session/resolve-files':
            if (self.headers.get('X-AgentHub-Protocol')
                    and self.headers.get('X-AgentHub-Node-Token') != self.state['token']):
                return self._json({'error': 'forbidden'}, 403)
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.state.setdefault('file_checks', []).append(body)
            if self.state.get('fail_checks'):
                return self._json({'error': 'unavailable'}, 503)
            return server.Handler._resolve_files(self, body)
        return super().do_POST()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/api/session/file':
            # Exercise the production endpoint; index lookup is isolated below.
            self.state['gets'].append((url.path, parse_qs(url.query)))
            return server.Handler._api_get(self, url.path, parse_qs(url.query))
        return super().do_GET()

    def messages(self, query):
        data = super().messages(query)
        if query.get('window') and not int(query.get('start', ['0'])[0]):
            data['messages'] = data['messages'][-1:]
        return data


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / 'output'
        output.mkdir()
        image = output / 'curve.png'
        image.write_bytes(PNG)
        source = root / 'source.py'
        source.write_text('print("fixture")\n')
        node = start_node('a' * 32, 'FileNode')
        node.RequestHandlerClass = FileNode
        node.state['row']['cwd'] = str(root)
        initial = [
            {'role': 'user', 'text': '画曲线', 'ts': '2026-09-01T00:00:00Z'},
            {'role': 'tool', 'name': 'Read', 'call_id': 'read-image',
             'text': json.dumps({'file_path': str(image)})},
        ]
        reply = {'role': 'assistant', 'phase': 'final', 'ts': '2026-09-01T00:01:00Z',
                 'text': '曲线已出（`curve.png`，上图）。\n\n'
                         '源码：[`source.py`](source.py:12)，目录：`./output`。\n\n'
                         '门/筛选臂同期还在缓慢爬升。`missing.png` [缺失](missing.txt)\n\n'
                         '链接：https://example.com/a?q=1&x=2。 [文档](https://example.com/a_(b))'}
        registry = hub.Registry(root / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'FileNode', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with patch.object(server.index, 'get', side_effect=lambda uid:
                              node.state['row'] if uid == node.state['row']['uid'] else None), \
                    patch.object(server.index, 'messages_for', side_effect=lambda view:
                                 {'messages': node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--disable-gpu', '--disable-software-rasterizer'])
                for base, scoped in [(f'http://127.0.0.1:{node.server_port}/', False),
                                     (f'http://127.0.0.1:{central.server_port}/agenthub/', True)]:
                    node.state['messages'] = list(initial)
                    node.state['gets'].clear()
                    ctx = browser.new_context()
                    page = ctx.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base)
                    page.locator('.item').first.click()
                    page.wait_for_function("document.querySelector('.msg[data-role=tool]') !== null")
                    # Wait for the stream before appending, so this is a real
                    # incremental delivery rather than an initial HTTP response.
                    for _ in range(100):
                        if any(path == '/api/watch' for path, _q in node.state['gets']):
                            break
                        page.wait_for_timeout(50)
                    assert any(path == '/api/watch' for path, _q in node.state['gets'])
                    node.state['messages'].append(reply)
                    link = page.locator('.msg[data-role=assistant] a').filter(has_text='curve.png')
                    link.wait_for()
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='筛选臂').count() == 0
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='missing').count() == 0
                    assert link.get_attribute('title') == str(image)
                    href = link.get_attribute('href')
                    query = parse_qs(urlparse(href).query)
                    assert ('~' in query['uid'][0]) == scoped, href
                    assert urlparse(href).path == ('/agenthub' if scoped else '') + '/api/session/file'
                    with page.expect_popup() as popup:
                        link.click()
                    image_page = popup.value
                    image_page.wait_for_function('document.querySelector("img")?.naturalWidth === 1')
                    image_page.close()
                    # Same reference must work on a fresh initial/windowed load.
                    page.reload()
                    link.wait_for()
                    response = ctx.request.get(link.get_attribute('href'))
                    assert response.status == 200 and response.body() == PNG
                    assert 'sandbox' in response.headers['content-security-policy']
                    text_link = page.locator('.msg[data-role=assistant] a').filter(has_text='source.py')
                    assert text_link.inner_text() == '[source.py](source.py:12)'
                    response = ctx.request.get(text_link.get_attribute('href'))
                    assert response.status == 200 and response.text() == source.read_text()
                    directory = page.locator('.msg[data-role=assistant] a').filter(has_text='./output')
                    assert 'curve.png' in ctx.request.get(directory.get_attribute('href')).text()
                    assert any(path == '/api/session/file' and q['uid'] == [node.state['row']['uid']]
                               for path, q in node.state['gets'])
                    checks = page.evaluate(r'''() => {
                      const host = document.createElement('div');
                      host.innerHTML = md('已存curve.png，路径/source.py。 `source.py:12`\n\n'
                        + '[`源码`](source.py:12) [文档](<https://example.com/help> "说明") '
                        + '[坏](javascript:alert(1)) [坏](data:text/html,hi)\n\n'
                        + 'https://example.com/a_(b)。 www.example.com。 <HTTPS://example.com/auto> A/D\n\n'
                        + '```sh\ncat /private/file.txt\n```\n\n'
                        + '`print("curve.png")` ![image](https://example.com/img.png)', true, [], {uid:S.sel, agent:'child'});
                      return {
                        text:host.textContent,
                        refs:[...host.querySelectorAll('a, span[data-file-ref]')].map(a=>({text:a.textContent,href:a.href || a.dataset.fileHref})),
                        premature:host.querySelectorAll('a[href*="/api/session/file?"]').length,
                        nested:host.querySelectorAll('a a').length,
                        codeLinks:host.querySelectorAll('pre a').length,
                        code:host.querySelector('pre').textContent,
                        images:host.querySelectorAll('img').length,
                        unsafe:host.querySelectorAll('a[href^="javascript:"],a[href^="data:"]').length,
                      };
                    }''')
                    assert not checks['nested'] and not checks['codeLinks'] and not checks['unsafe'], checks
                    assert not checks['premature'], checks
                    assert checks['images'] == 1 and checks['code'] == 'cat /private/file.txt', checks
                    refs = checks['refs']
                    assert any(r['text'] == '[源码](source.py:12)' for r in refs), refs
                    assert any(r['text'] == '[文档](<https://example.com/help> "说明")'
                               and r['href'] == 'https://example.com/help' for r in refs), refs
                    assert '[坏](javascript:alert(1))' in checks['text'], checks
                    assert any(r['text'] == 'curve.png' and 'agent=child' in r['href'] for r in refs), refs
                    assert any(r['text'] == 'source.py:12' for r in refs), refs
                    assert any(r['href'] == 'https://example.com/a_(b)' for r in refs), refs
                    assert any(r['href'] == 'https://www.example.com/' for r in refs), refs
                    assert any(r['href'] == 'https://example.com/auto' for r in refs), refs
                    assert any(r['text'] == '<HTTPS://example.com/auto>' for r in refs), refs
                    assert not any(r['text'] == 'A/D' for r in refs), refs
                    assert not any('print' in r['text'] for r in refs), refs
                    # An unfinished Markdown target must not trigger exponential
                    # backtracking while an assistant is still streaming it.
                    page.evaluate("md('[unfinished](' + 'a'.repeat(10000), true)")
                    # A failed check must never create a clickable local link.
                    node.state['fail_checks'] = True
                    page.reload()
                    page.wait_for_selector('span[data-file-ref="curve.png"]')
                    page.wait_for_timeout(300)
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='curve.png').count() == 0
                    node.state['fail_checks'] = False
                    # Existence is checked again on a fresh render, not cached
                    # indefinitely from a previous successful response.
                    image.unlink()
                    page.reload()
                    page.locator('.msg[data-role=assistant] a').filter(has_text='source.py').wait_for()
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='curve.png').count() == 0
                    image.write_bytes(PNG)
                    assert not errors, errors
                    print(('Hub' if scoped else 'Node') + ': SSE, reload, image click, text, directory, URL and safety checks passed')
                    ctx.close()
                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
