"""Native PDF viewer regression. Requires full Chromium or installed Chrome.

The minimal Chromium headless shell does not include the native PDF viewer.
Uses isolated fixture data and does not start a CLI.
"""
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import file_manager, hub, server
from file_links_e2e import FileNode
from hub_fixture import start_node, stop
from hub_e2e import MountedHub


def pdf_fixture():
    stream = b'BT /F1 24 Tf 30 100 Td (PDF preview fixture) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data = b'%PDF-1.4\n'
    offsets = []
    for number, item in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode()+b' 0 obj\n'+item+b'\nendobj\n'
    xref = len(data)
    return data+b'xref\n0 6\n0000000000 65535 f \n'+b''.join(
        f'{offset:010d} 00000 n \n'.encode() for offset in offsets
    )+b'trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root/'fixture.pdf').write_bytes(pdf_fixture())
        (root/'fake.pdf').write_text('<html><script>window.pwned=1</script></html>')
        node = start_node('a'*32, 'PDFNode')
        node.RequestHandlerClass = FileNode
        node.state['row']['cwd'] = str(root)
        node.state['messages'] = [{'role':'assistant','text':f'`{root}`'}]
        registry = hub.Registry(root/'nodes.json',['127.0.0.0/8'])
        registry.register({'name':'PDFNode','url':f'http://127.0.0.1:{node.server_port}','token':node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1',0),MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever,daemon=True).start()
        try:
            with patch.object(file_manager,'_manager',file_manager.Manager(root/'state')), \
                    patch.object(server.index,'get',return_value=node.state['row']), \
                    patch.object(server.index,'messages_for',return_value={'messages':node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True,channel='chrome' if shutil.which('google-chrome') else 'chromium')
                for scoped in [False,True]:
                    base = f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped else f'http://127.0.0.1:{node.server_port}/'
                    uid = 'claude:'+'a'*32+'~same-file-hash' if scoped else node.state['row']['uid']
                    query = {'uid':uid,'ref':str(root)}
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(base+'files.html?'+urlencode(query))
                    page.get_by_role('link',name='fixture.pdf',exact=True).dblclick()
                    page.locator('#preview-content iframe').wait_for()
                    for _ in range(60):
                        if any(frame.locator('pdf-viewer').count() for frame in page.frames):
                            break
                        page.wait_for_timeout(100)
                    assert any(frame.locator('pdf-viewer').count() for frame in page.frames)
                    assert not any(frame.url.startswith('chrome-error:') for frame in page.frames)
                    response = context.request.get(base+'api/session/files?'+urlencode({**query,'path':str(root/'fake.pdf'),'mode':'preview'}))
                    assert response.status == 400
                    assert 'PDF' in response.json()['error']
                    print(('Hub' if scoped else 'Node')+': native PDF viewer loaded; fake PDF rejected',flush=True)
                    context.close()
                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
