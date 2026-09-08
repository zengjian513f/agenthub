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
from test_desktop_helper import desktop


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
        client = root / 'browser-computer'
        client.mkdir()
        (client / 'source.py').write_text(source.read_text())
        (client / 'output/placeholder').parent.mkdir(exist_ok=True)
        (client / 'output/curve.png').write_bytes(PNG)
        (client / 'output').mkdir(exist_ok=True)
        desktop_server = ThreadingHTTPServer(('127.0.0.1', 0), desktop.Handler)
        desktop_server.config = {'token': 'fixture-pairing', 'origins': [], 'mappings': [
            {'node': server.NODE_ID or server.federation.identity(), 'remote': str(root), 'local': str(client)}]}
        opened = []
        desktop_server.open_target = opened.append
        threading.Thread(target=desktop_server.serve_forever, daemon=True).start()
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
                         '源码：[`source.py`](source.py:12)，目录：(`./output`)。\n\n'
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
                    patch.object(server.index, 'messages_for', side_effect=lambda view, **kwargs:
                                 {'messages': node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--disable-gpu', '--disable-software-rasterizer'])
                for base, scoped in [(f'http://127.0.0.1:{node.server_port}/', False),
                                     (f'http://127.0.0.1:{central.server_port}/agenthub/', True)]:
                    node.state['messages'] = list(initial)
                    node.state['gets'].clear()
                    ctx = browser.new_context()
                    ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
                    page = ctx.new_page()
                    desktop_server.config['origins'].append(base.rstrip('/').replace('/agenthub', ''))
                    # Use a random loopback port for the real client helper so
                    # this free test cannot contact any installed user helper.
                    app_js = (server.STATIC / 'app.js').read_text().replace(
                        '127.0.0.1:18711', f'127.0.0.1:{desktop_server.server_port}')
                    page.route('**/app.js*', lambda route: route.fulfill(
                        body=app_js, content_type='application/javascript'))
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
                    assert text_link.inner_text() == 'source.py'
                    assert '源码：source.py，目录' in page.locator('.msg[data-role=assistant]').inner_text()
                    assert 'source.py:12' not in page.locator('.msg[data-role=assistant]').inner_text()
                    response = ctx.request.get(text_link.get_attribute('href'))
                    assert response.status == 200 and response.text() == source.read_text()
                    directory = page.locator('.msg[data-role=assistant] a').filter(has_text='./output')
                    assert 'curve.png' in ctx.request.get(directory.get_attribute('href')).text()
                    assert text_link.get_attribute('data-file-kind') == 'file'
                    assert directory.get_attribute('data-file-kind') == 'directory'
                    directory.click(button='right')
                    assert page.locator('#file-menu-target').inner_text() == str(output)
                    assert page.locator('#file-menu [data-action="download"]').is_disabled()
                    page.keyboard.press('Escape')
                    assert any(path == '/api/session/file' and q['uid'] == [node.state['row']['uid']]
                               for path, q in node.state['gets'])
                    checks = page.evaluate(r'''() => {
                      const host = document.createElement('div');
                      host.innerHTML = md('已存curve.png，路径/source.py。 `source.py:12`\n\n'
                        + '[`源码`](source.py:12) [文档](<https://example.com/help> "说明") '
                        + '[坏](javascript:alert(1))\n\n'
                        + '外部 https://example.com/outside 和 www.example.com 不链接。'
                        + '（`curve.png`，上图） (https://example.com/inside)\n\n'
                        + '(https://example.com/a_(b)) (output/curve(final).png) '
                        + '(file:///tmp/private) (javascript:alert(1)) (mailto:a@example.com)\n\n'
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
                    assert '源码 文档 坏' in checks['text'], checks
                    assert 'https://example.com/help' not in checks['text'], checks
                    assert '[坏]' not in checks['text'], checks
                    assert len([r for r in refs if r['text'] == '源码']) == 1, refs
                    assert len([r for r in refs if r['text'] == 'curve.png']) == 1, refs
                    assert any(r['href'] == 'https://example.com/inside' for r in refs), refs
                    assert any(r['href'] == 'https://example.com/a_(b)' for r in refs), refs
                    assert any(r['text'] == 'output/curve(final).png' for r in refs), refs
                    assert not any('private' in r['href'] or 'mailto:' in r['href'] for r in refs), refs
                    assert not any(r['text'] == '/source.py' or 'outside' in r['href'] or 'www.' in r['href'] for r in refs), refs
                    assert not any('print' in r['text'] for r in refs), refs
                    # The bug contract is text preservation, not merely a
                    # visually similar reconstruction of a Markdown link.
                    unchanged = page.evaluate(r'''() => {
                      const samples = [
                        '弯腰动作：单干净参考 v5 (experiments/test/compare_v5.mp4)',
                        '保持  两个空格（ ./output ）和括号。',
                        '原始网址 (https://example.com/a_(b)?q=1&x=2)',
                        '标题外部 example.com，文件 source.py，不生成链接。',
                      ];
                      return samples.map(text => {
                        const host = document.createElement('div');
                        host.innerHTML = inline(text, [], {uid:S.sel});
                        return {text, rendered:host.textContent,
                          labels:[...host.querySelectorAll('a,span[data-file-ref]')].map(a=>a.textContent)};
                      });
                    }''')
                    for item in unchanged:
                        assert item['text'] == item['rendered'], item
                        assert not any(label in ['设计说明', '标题', '弯腰动作：单干净参考 v5']
                                       for label in item['labels']), item
                    standard = page.evaluate(r'''() => {
                      const cases = [
                        ['[保留目录](/project/experiments/run)', '保留目录', '/project/experiments/run'],
                        ['[设计说明](docs/file-links.md)', '设计说明', 'docs/file-links.md'],
                        ['[**说明**](<output/report.pdf> "原有标题")', '说明', 'output/report.pdf'],
                        ['[文档](https://example.com/help "提示")', '文档', 'https://example.com/help'],
                        ['[缺失](missing.txt)', '缺失', 'missing.txt'],
                        ['[不可用](javascript:alert(1))', '不可用', null],
                      ];
                      return cases.map(([raw, expected, target]) => {
                        const host=document.createElement('div');
                        host.innerHTML=inline(raw, [], {uid:S.sel});
                        const link=host.querySelector('a,span[data-file-ref]');
                        return {raw,expected,target,text:host.textContent,
                          destination:link?.dataset.fileRef || link?.getAttribute('href') || null};
                      });
                    }''')
                    for item in standard:
                        assert item['text'] == item['expected'], item
                        assert item['destination'] == item['target'], item
                    web_link = page.locator('.msg[data-role=assistant] a').filter(has_text='文档')
                    web_link.click(button='right')
                    web_menu = page.locator('#file-menu')
                    assert web_menu.locator('#file-menu-target').inner_text() == 'https://example.com/a_(b)'
                    assert web_menu.get_by_role('menuitem').all_text_contents() == [
                        '复制文本', '复制链接地址', '在新标签页打开']
                    web_menu.get_by_role('menuitem', name='复制文本', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == '文档'
                    web_link.click(button='right')
                    web_menu.get_by_role('menuitem', name='复制链接地址', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == 'https://example.com/a_(b)'
                    ctx.route('https://example.com/**', lambda route: route.fulfill(body='fixture'))
                    web_link.click(button='right')
                    with ctx.expect_page() as new_page:
                        web_menu.get_by_role('menuitem', name='在新标签页打开', exact=True).click()
                    popup = new_page.value
                    popup.wait_for_load_state()
                    assert popup.url == 'https://example.com/a_(b)'
                    popup.close()
                    text_link.click(button='right')
                    menu = page.locator('#file-menu')
                    assert menu.locator('#file-menu-target').inner_text() == str(source)
                    assert menu.get_by_role('menuitem').all_text_contents() == [
                        '复制文本', '复制绝对路径', '本地打开', '本地打开目录', '下载']
                    menu.get_by_role('menuitem', name='复制文本', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == 'source.py'
                    text_link.click(button='right')
                    menu.get_by_role('menuitem', name='复制绝对路径', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == str(source)
                    link.click(button='right')
                    menu.get_by_role('menuitem', name='本地打开', exact=True).click()
                    dialog = page.locator('.desktop-dialog')
                    dialog.locator('.desktop-pairing').fill('wrong-key')
                    dialog.get_by_role('button', name='连接并打开').click()
                    page.wait_for_function("document.querySelector('.desktop-error')?.textContent.includes('配对码')")
                    assert not opened or opened[-1] != client / 'output/curve.png'
                    dialog.locator('.desktop-pairing').fill('fixture-pairing')
                    dialog.get_by_role('button', name='连接并打开').click()
                    dialog.wait_for(state='detached')
                    assert opened[-1] == client / 'output/curve.png'
                    text_link.click(button='right')
                    menu.get_by_role('menuitem', name='本地打开目录', exact=True).click()
                    page.wait_for_timeout(250)
                    assert opened[-1] == client
                    directory.click(button='right')
                    menu.get_by_role('menuitem', name='本地打开目录', exact=True).click()
                    page.wait_for_timeout(250)
                    assert opened[-1] == client / 'output'
                    text_link.click(button='right')
                    with page.expect_download() as downloaded:
                        menu.get_by_role('menuitem', name='下载', exact=True).click()
                    download = downloaded.value
                    assert download.suggested_filename == 'source.py'
                    download.save_as(root / 'downloaded.py')
                    assert (root / 'downloaded.py').read_bytes() == source.read_bytes()
                    # Long targets remain readable/selectable without pushing
                    # actions outside a narrow viewport or closing on scroll.
                    page.set_viewport_size({'width': 360, 'height': 640})
                    page.evaluate('showMobileDetail()')
                    long_url = 'https://example.com/' + 'long-segment/' * 100 + '?q=%3Cscript%3E'
                    page.evaluate('''url => {
                      const host=document.createElement('div');host.id='long-link-fixture';host.className='mb';
                      host.innerHTML=inline('[长链接]('+url+')');document.querySelector('#msgs').appendChild(host);
                    }''', long_url)
                    page.locator('#long-link-fixture a').click(button='right')
                    assert menu.locator('#file-menu-target').inner_text() == long_url
                    box = menu.bounding_box()
                    assert box['x'] >= 0 and box['x'] + box['width'] <= 360
                    assert box['y'] >= 0 and box['y'] + box['height'] <= 640
                    menu.locator('#file-menu-target').evaluate('(el) => el.scrollTop = el.scrollHeight')
                    page.wait_for_timeout(50)
                    assert menu.is_visible()
                    assert menu.locator('script').count() == 0
                    page.keyboard.press('Escape')
                    page.locator('#long-link-fixture').evaluate('(el) => el.remove()')
                    page.set_viewport_size({'width': 1280, 'height': 720})
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
            desktop_server.shutdown()
            desktop_server.server_close()
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
