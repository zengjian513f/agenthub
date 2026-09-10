"""Free browser checks for file readers, shared fonts, and mounted Hub links."""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import file_manager, hub, server
from file_links_e2e import FileNode
from hub_e2e import MountedHub
from hub_fixture import PNG, start_node, stop


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        doc = root / '中文报告.md'
        doc.write_text('# 中文阅读标题\n\n正文 **粗体** 与 `inline_code`。\n\n'
                       '| 名称 | 值 |\n| --- | --- |\n| 指标 | 12 |\n\n'
                       '```python\ndef calculate(x):\n    return x + 1\n```\n\n'
                       '[源码](source.py)\n\n![图片](chart.png)\n\n'
                       '<script>window.filePwned=1</script>\n\n'
                       '[bad](javascript:alert(1))\n')
        source = root / 'source.py'; source.write_text('def example():\n    return "中文"\n')
        image = root / 'chart.png'; image.write_bytes(PNG)
        node = start_node('a' * 32, 'FileNode'); node.RequestHandlerClass = FileNode
        node.state['row']['cwd'] = str(root)
        node.state['messages'] = [{'role':'assistant', 'text': '\n'.join(f'`{p}`' for p in [root, doc, source, image])}]
        registry = hub.Registry(root / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name':'FileNode', 'url':f'http://127.0.0.1:{node.server_port}', 'token':node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True; central.registry = registry; central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with patch.object(file_manager, '_manager', file_manager.Manager(root / 'state')), \
                    patch.object(server.index, 'get', side_effect=lambda uid: node.state['row'] if uid == node.state['row']['uid'] else None), \
                    patch.object(server.index, 'messages_for', side_effect=lambda view, **kw: {'messages':node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [False, True]:
                    base = f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped else f'http://127.0.0.1:{node.server_port}/'
                    uid = 'claude:' + 'a' * 32 + '~same-file-hash' if scoped else node.state['row']['uid']
                    prefix = 'agenthub.hub./agenthub/.' if scoped else 'agenthub.'
                    context = browser.new_context(viewport={'width':1280, 'height':850})
                    context.add_init_script(f'localStorage.setItem({json.dumps(prefix + "font")}, JSON.stringify("cascadia"))')
                    page = context.new_page(); errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    url = base + 'api/session/file?' + urlencode({'uid':uid, 'ref':str(doc)})
                    # API consumers retain byte-for-byte raw text, even with raw=1 browser Accept.
                    raw = context.request.get(url)
                    assert raw.status == 200 and raw.text() == doc.read_text()
                    assert context.request.get(url + '&raw=1', headers={'Accept':'text/html'}).text() == doc.read_text()
                    page.goto(url)
                    page.locator('.reader-markdown h1').wait_for()
                    assert urlparse(page.url).path.endswith('/file.html')
                    assert page.locator('.reader-markdown table td').count() == 2
                    page.wait_for_function('document.querySelector(".reader-markdown code .hljs-keyword") !== null')
                    page.wait_for_function('document.querySelector(".reader-markdown img").naturalWidth === 1')
                    assert page.locator('.reader-markdown script,a[href^="javascript:"]').count() == 0
                    assert not page.evaluate('window.filePwned')
                    font = page.locator('.reader-markdown p').first.evaluate('(el)=>getComputedStyle(el).fontFamily')
                    assert 'sans-serif' in font and 'monospace' not in font
                    code_font = page.locator('.reader-markdown pre code').evaluate('(el)=>getComputedStyle(el).fontFamily')
                    assert 'AgentHub Cascadia Mono' in code_font
                    page.get_by_role('button', name='源码', exact=True).click()
                    assert page.locator('.reader-source code').inner_text() == doc.read_text()
                    assert page.locator('.reader-source code').evaluate('(el)=>getComputedStyle(el).fontFamily') == code_font
                    page.get_by_role('button', name='自动换行').click()
                    assert page.locator('.reader-source').evaluate('(el)=>getComputedStyle(el).whiteSpace') == 'pre-wrap'
                    page.get_by_role('button', name='预览', exact=True).click()
                    # Font preference is the same one read by the main terminal UI.
                    other = context.new_page(); other.goto(base)
                    other.wait_for_function('typeof configuredTermFont === "function"')
                    assert 'AgentHub Cascadia Mono' in other.evaluate('configuredTermFont()')
                    other.evaluate('(key)=>localStorage.setItem(key, JSON.stringify("ubuntu"))', prefix + 'font')
                    page.wait_for_function('getComputedStyle(document.querySelector(".reader-markdown pre code")).fontFamily.includes("AgentHub Ubuntu Sans Mono")')
                    # Repaint when syntax highlighting finishes after the document is displayed.
                    page.evaluate('document.querySelector(".reader-markdown pre code").textContent="return 42"; dispatchEvent(new Event("agenthub-highlight-ready"))')
                    assert page.locator('.reader-markdown pre .hljs-keyword').count() > 0
                    page.set_viewport_size({'width':390, 'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    # Existing file manager dialogs use the same reader and fonts.
                    page.goto(base + 'files.html?' + urlencode({'uid':uid, 'ref':str(root)}))
                    page.get_by_role('link', name=doc.name, exact=True).dblclick()
                    page.locator('#preview-content .reader-markdown h1').wait_for()
                    assert page.locator('#preview-content .reader-markdown table td').count() == 2
                    with page.expect_popup() as opened:
                        page.locator('#preview-content .reader-markdown').get_by_role('link', name='源码', exact=True).click()
                    child = opened.value; child.locator('.reader-source code').wait_for()
                    assert child.locator('.reader-source code').inner_text() == source.read_text()
                    child.close()
                    if scoped:
                        direct = base + 'api/nodes/' + 'a' * 32 + '/api/session/file?' + urlencode({'uid':node.state['row']['uid'], 'ref':str(doc)})
                        page.goto(direct); page.locator('.reader-markdown h1').wait_for()
                        assert urlparse(page.url).path == '/agenthub/file.html'
                    assert not errors, errors
                    context.close()
                browser.close()
        finally:
            stop(central); stop(node)
    print('file preview: node + mounted Hub, fonts, source toggle, safe Markdown, mobile, links OK')


if __name__ == '__main__':
    main()
