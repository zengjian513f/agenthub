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
from agenthub import hub, server, file_manager
from hub_fixture import NodeHandler, PNG, start_node, stop
from hub_e2e import MountedHub


class FileNode(NodeHandler):
    def do_POST(self):
        if urlparse(self.path).path in {'/api/session/files/action', '/api/session/files/upload'}:
            if (self.headers.get('X-AgentHub-Protocol')
                    and self.headers.get('X-AgentHub-Node-Token') != self.state['token']):
                return self._json({'error': 'forbidden'}, 403)
            return self._file_post(urlparse(self.path))
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
        if url.path in {'/api/session/file', '/api/session/files'}:
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
        nested = output / '中文 子目录'
        nested.mkdir()
        special = nested / '报告 # & (final).txt'
        special.write_text('中文下载内容\n')
        hostile = nested / '<img onerror=alert(1)>.txt'
        hostile.touch()
        (nested / '.hidden').write_text('hidden')
        (nested / 'empty').mkdir()
        many = output / 'many'
        many.mkdir()
        for i in range(502):
            (many / f'item-{i:04}.txt').touch()
        checkout = root / 'checkout'
        checkout.mkdir()
        for name in ['Example.sln', 'README.md', 'AGENTS.md', 'CLAUDE.md']:
            (checkout / name).write_text('checkout fixture')
        (root / 'CLAUDE.md').write_text('different project')
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
                         '链接：https://example.com/a?q=1&x=2。 [文档](https://example.com/a_(b))\n\n'
                         f'代码已拉下来，位于 `{checkout}`。自带 `Example.sln`、`README.md`、'
                         '`AGENTS.md`、`CLAUDE.md`。版本 `4.1`，分支 `release/3.0`，'
                         '命令 `cat README.md`，网址 `https://example.com/repo`。'}
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
            with patch.object(file_manager, '_manager', file_manager.Manager(root / 'manager-state')), \
                    patch.object(server.index, 'get', side_effect=lambda uid:
                              node.state['row'] if uid == node.state['row']['uid'] else None), \
                    patch.object(server.index, 'messages_for', side_effect=lambda view, **kwargs:
                                 {'messages': node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--disable-gpu', '--disable-software-rasterizer'])
                for base, scoped in [(f'http://127.0.0.1:{node.server_port}/', False),
                                     (f'http://127.0.0.1:{central.server_port}/agenthub/', True)]:
                    node.state['messages'] = list(initial)
                    node.state['gets'].clear()
                    node.state['file_checks'] = []
                    ctx = browser.new_context()
                    ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
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
                    def menu_for(target):
                        target.click(button='right')
                        page.wait_for_function('!document.querySelector("#file-menu").hidden && !document.querySelector("#file-menu-target").textContent.includes("正在读取")')

                    def check_checkout_links():
                        reply_dom = page.locator('.msg[data-role=assistant]')
                        for ref in [str(checkout), 'Example.sln', 'README.md', 'AGENTS.md']:
                            target = reply_dom.locator('a[data-file-ref]').filter(has_text=ref)
                            target.wait_for()
                            assert target.locator('code').inner_text() == ref
                            assert target.get_attribute('data-file-ref') == ref
                            assert ctx.request.get(target.get_attribute('data-file-href')).status == 200
                        for ref in ['4.1', 'release/3.0', 'cat README.md']:
                            assert reply_dom.locator('a').filter(has_text=ref).count() == 0
                        assert reply_dom.locator('a[href="https://example.com/repo"] code').inner_text() == 'https://example.com/repo'
                        ambiguous = reply_dom.locator('a[data-file-ref="CLAUDE.md"]')
                        assert ctx.request.get(ambiguous.get_attribute('data-file-href')).status == 400
                    page.wait_for_timeout(200)
                    assert not node.state.get('file_checks'), 'Rendering/SSE must not check files'
                    check_checkout_links()
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='筛选臂').count() == 0
                    assert page.locator('.msg[data-role=assistant] a').filter(has_text='missing').count() == 1
                    assert link.get_attribute('data-file-ref') == 'curve.png'
                    href = link.get_attribute('data-file-href')
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
                    check_checkout_links()
                    response = ctx.request.get(link.get_attribute('data-file-href'))
                    assert response.status == 200 and response.body() == PNG
                    assert 'sandbox' in response.headers['content-security-policy']
                    text_link = page.locator('.msg[data-role=assistant] a').filter(has_text='source.py')
                    assert text_link.inner_text() == 'source.py'
                    assert '源码：source.py，目录' in page.locator('.msg[data-role=assistant]').inner_text()
                    assert 'source.py:12' not in page.locator('.msg[data-role=assistant]').inner_text()
                    response = ctx.request.get(text_link.get_attribute('data-file-href'))
                    assert response.status == 200 and response.text() == source.read_text()
                    directory = page.locator('.msg[data-role=assistant] a').filter(has_text='./output')
                    with page.expect_popup() as opened:
                        directory.click()
                    browser_page = opened.value
                    browser_page.on('pageerror', lambda error: errors.append(str(error)))
                    browser_page.get_by_role('link', name='curve.png', exact=True).wait_for()
                    assert urlparse(browser_page.url).path == ('/agenthub' if scoped else '') + '/files.html'
                    browser_page.get_by_role('link', name='中文 子目录', exact=True).dblclick()
                    browser_page.get_by_role('link', name=special.name, exact=True).wait_for()
                    assert browser_page.locator('#entries img').count() == 0
                    assert browser_page.get_by_role('link', name=hostile.name, exact=True).is_visible()
                    assert browser_page.get_by_role('link', name='.hidden', exact=True).is_visible()
                    browser_page.get_by_role('link', name=special.name, exact=True).click()
                    with browser_page.expect_download() as downloaded:
                        browser_page.locator('.commandbar [data-action="download"]').click()
                    download = downloaded.value
                    assert download.suggested_filename == special.name
                    download.save_as(root / 'browser-download.txt')
                    assert (root / 'browser-download.txt').read_bytes() == special.read_bytes()
                    browser_page.reload()
                    browser_page.get_by_role('link', name='empty', exact=True).dblclick()
                    browser_page.get_by_text('此目录为空', exact=True).wait_for()
                    browser_page.go_back()
                    browser_page.get_by_role('link', name=special.name, exact=True).wait_for()
                    browser_page.go_forward()
                    browser_page.get_by_text('此目录为空', exact=True).wait_for()
                    browser_page.locator('#breadcrumbs a').filter(has_text='output').click()
                    browser_page.get_by_role('link', name='many', exact=True).dblclick()
                    browser_page.get_by_text('共 502 项，包含隐藏文件', exact=True).wait_for()
                    assert browser_page.locator('#entries tr').count() == 500
                    browser_page.get_by_role('link', name='下一页', exact=True).click()
                    browser_page.get_by_text('501–502 / 502', exact=True).wait_for()
                    assert browser_page.locator('#entries tr').count() == 2
                    browser_page.get_by_role('link', name='上一页', exact=True).click()
                    browser_page.get_by_text('1–500 / 502', exact=True).wait_for()
                    browser_page.get_by_role('link', name='上级目录', exact=True).click()
                    browser_page.get_by_role('link', name='curve.png', exact=True).wait_for()
                    browser_page.get_by_role('link', name='上级目录', exact=True).click()
                    browser_page.get_by_role('link', name='source.py', exact=True).wait_for()
                    browser_page.set_viewport_size({'width': 360, 'height': 640})
                    browser_page.get_by_role('link', name='output', exact=True).dblclick()
                    browser_page.get_by_role('link', name='中文 子目录', exact=True).dblclick()
                    browser_page.get_by_role('link', name=special.name, exact=True).wait_for()
                    assert browser_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    # Errors are rendered inside the browser, with refresh/back recovery.
                    with patch.object(server.files, 'list_directory', side_effect=PermissionError):
                        browser_page.get_by_role('button', name='刷新', exact=True).click()
                        browser_page.locator('#status.error').wait_for()
                    browser_page.get_by_role('button', name='刷新', exact=True).click()
                    browser_page.get_by_role('link', name=special.name, exact=True).wait_for()
                    browser_page.close()
                    menu_for(directory)
                    assert page.locator('#file-menu-target').inner_text() == str(output)
                    assert page.locator('#file-menu [data-action="download"]').is_hidden()
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
                    assert len([r for r in refs if r['text'] == 'source.py:12']) == 1, refs
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
                    for raw, destination in [
                        ('(https://example.com/report)', 'https://example.com/report'),
                        ('(https://example.com)', 'https://example.com/'),
                        ('(www.example.com)', 'https://www.example.com/'),
                        ('[https://example.com](https://example.com/)', 'https://example.com/'),
                    ]:
                        page.evaluate('''raw => {
                          const d=document.createElement('div');d.className='mb';d.id='copy-menu-probe';
                          d.innerHTML=inline(raw);document.querySelector('#msgs').appendChild(d);
                        }''', raw)
                        menu_for(page.locator('#copy-menu-probe a'))
                        copy_menu = page.locator('#file-menu')
                        assert copy_menu.get_by_role('menuitem').all_text_contents() == [
                            '复制链接地址', '在新标签页打开']
                        assert page.evaluate('document.activeElement.dataset.action') == 'copy-url'
                        copy_menu.get_by_role('menuitem', name='复制链接地址', exact=True).click()
                        assert page.evaluate('navigator.clipboard.readText()') == destination
                        page.locator('#copy-menu-probe').evaluate('(el) => el.remove()')
                    web_link = page.locator('.msg[data-role=assistant] a').filter(has_text='文档')
                    menu_for(web_link)
                    web_menu = page.locator('#file-menu')
                    assert web_menu.locator('#file-menu-target').inner_text() == 'https://example.com/a_(b)'
                    assert web_menu.get_by_role('menuitem').all_text_contents() == [
                        '复制链接地址', '在新标签页打开']
                    web_menu.get_by_role('menuitem', name='复制链接地址', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == 'https://example.com/a_(b)'
                    ctx.route('https://example.com/**', lambda route: route.fulfill(body='fixture'))
                    menu_for(web_link)
                    with ctx.expect_page() as new_page:
                        web_menu.get_by_role('menuitem', name='在新标签页打开', exact=True).click()
                    popup = new_page.value
                    popup.wait_for_load_state()
                    assert popup.url == 'https://example.com/a_(b)'
                    popup.close()
                    menu_for(text_link)
                    menu = page.locator('#file-menu')
                    assert menu.locator('#file-menu-target').inner_text() == str(source)
                    assert menu.get_by_role('menuitem').all_text_contents() == [
                        '复制完整路径', '复制所在目录路径', '下载']
                    menu.get_by_role('menuitem', name='复制完整路径', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == str(source)
                    menu_for(text_link)
                    menu.get_by_role('menuitem', name='复制所在目录路径', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == str(root)
                    menu_for(link)
                    menu.get_by_role('menuitem', name='复制所在目录路径', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == str(output)
                    menu_for(directory)
                    assert menu.get_by_role('menuitem').all_text_contents() == ['复制完整路径']
                    menu.get_by_role('menuitem', name='复制完整路径', exact=True).click()
                    assert page.evaluate('navigator.clipboard.readText()') == str(output)
                    menu_for(text_link)
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
                    menu_for(page.locator('#long-link-fixture a'))
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
                    # Opening a link works with older nodes too; no background
                    # checks are sent on initial render or reload.
                    def legacy_node(route):
                        response = route.fetch()
                        data = response.json()
                        data.pop('file_browser', None)
                        route.fulfill(response=response, json=data)
                    ctx.route('**/api/session/resolve-files', legacy_node)
                    page.reload()
                    directory.wait_for()
                    with page.expect_popup() as opened:
                        directory.click()
                    legacy_page = opened.value
                    legacy_page.wait_for_url('**/api/session/file?*')
                    assert 'curve.png' in legacy_page.locator('body').inner_text()
                    legacy_page.close()
                    ctx.unroute('**/api/session/resolve-files', legacy_node)
                    node.state['file_checks'] = []
                    node.state['fail_checks'] = True
                    page.reload()
                    link.wait_for()
                    page.wait_for_timeout(500)
                    assert not node.state['file_checks']
                    with page.expect_popup() as opened:
                        link.click()
                    failed = opened.value
                    failed.locator('#status.error').wait_for()
                    assert 'unavailable' in failed.locator('#status').inner_text()
                    failed.close()
                    node.state['fail_checks'] = False
                    # Missing targets remain candidates, with errors on demand.
                    image.unlink()
                    page.reload()
                    link.wait_for()
                    with page.expect_popup() as opened:
                        link.click()
                    missing = opened.value
                    missing.locator('#status.error').wait_for()
                    assert '文件不存在' in missing.locator('#status').inner_text()
                    image.write_bytes(PNG)
                    missing.get_by_role('button', name='刷新', exact=True).click()
                    missing.wait_for_function('document.querySelector("img")?.naturalWidth === 1')
                    missing.close()
                    assert not errors, errors
                    print(('Hub' if scoped else 'Node') + ': SSE, reload, image click, text, directory, URL and safety checks passed')
                    ctx.close()
                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
