"""Free, isolated Explorer workflows through both a node and a mounted Hub."""
import json
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
from hub_fixture import PNG, start_node, stop
from hub_e2e import MountedHub


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        node = start_node('a'*32, 'ExNode')
        node.RequestHandlerClass = FileNode
        registry = hub.Registry(root/'nodes.json', ['127.0.0.0/8'])
        registry.register({'name':'ExplorerNode','url':f'http://127.0.0.1:{node.server_port}','token':node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1',0),MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever,daemon=True).start()
        try:
            with patch.object(file_manager,'_manager',file_manager.Manager(root/'manager')), \
                    patch.object(server,'TERMINAL',True), \
                    patch.object(server.index,'get',side_effect=lambda uid: node.state['row'] if uid == node.state['row']['uid'] else None), \
                    patch.object(server.index,'messages_for',side_effect=lambda view,**kw:{'messages':node.state['messages']}), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [False,True]:
                    work = root/('hub-work' if scoped else 'node-work')
                    work.mkdir()
                    (work/'folder').mkdir()
                    (work/'alpha.txt').write_text('alpha contents')
                    (work/'beta.txt').write_text('beta')
                    (work/'.hidden').write_text('hidden')
                    (work/'image.png').write_bytes(PNG)
                    (work/'unsafe.html').write_text('<script>window.pwned=1</script>')
                    node.state['row']['cwd'] = str(work)
                    node.state['messages'] = [{'role':'assistant','text':f'目录 `{work}`'}]
                    base = f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped else f'http://127.0.0.1:{node.server_port}/'
                    uid = 'claude:'+'a'*32+'~same-file-hash' if scoped else node.state['row']['uid']
                    context = browser.new_context(accept_downloads=True, viewport={'width':1280,'height':850})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    query = urlencode({'uid':uid,'ref':str(work)})
                    page.goto(base+'files.html?'+query)
                    def item(name):
                        return page.locator('#entries').get_by_role('link',name=name,exact=True)
                    def command(action):
                        page.locator(f'.commandbar [data-action="{action}"]').click()
                    def confirm():
                        page.locator('#action-submit').click()
                    def finish(name):
                        page.wait_for_function('name => {const t=document.querySelector(".task");return t?.querySelector("small")?.textContent===name && t.querySelector(".task-title span")?.textContent==="已完成"}',arg=name)
                        page.get_by_role('button',name='关闭任务',exact=True).click()
                        page.locator('#file-table[aria-busy="false"]').wait_for()
                    item('alpha.txt').wait_for()
                    assert page.locator('aside,input[type=search],[role=tab]').count() == 0
                    assert page.locator('#trash-open, #trash-dialog').count() == 0
                    assert page.locator('.nav-buttons').inner_text().split() == ['←', '→', '↑', '↻']
                    assert page.locator('#parent').get_attribute('aria-label') == '上级目录'
                    assert page.locator('.commandbar [data-action="new"]').inner_text() == '新建'
                    assert page.locator('.commandbar [data-action="new"] *, .commandbar [data-action="upload"] *').count() == 0
                    assert page.locator('.commandbar [data-action="upload"]').inner_text() == '上传'
                    item('alpha.txt').click()
                    assert page.locator('#entries .selected').count() == 1
                    item('beta.txt').click(modifiers=['Control'])
                    assert page.locator('#entries .selected').count() == 2
                    page.locator('#workspace').press('Control+a')
                    assert page.locator('#entries .selected').count() == 6
                    page.keyboard.press('Escape')
                    assert page.locator('#entries .selected').count() == 0
                    item('folder').dblclick()
                    page.get_by_text('此目录为空',exact=True).first.wait_for()
                    page.locator('#parent').click()
                    item('alpha.txt').wait_for()
                    page.locator('#back').click()
                    page.get_by_text('此目录为空',exact=True).first.wait_for()
                    page.locator('#forward').click()
                    item('alpha.txt').wait_for()
                    page.locator('#edit-address').click()
                    page.locator('#address').fill(str(work/'folder'))
                    page.locator('#address').press('Enter')
                    page.get_by_text('此目录为空',exact=True).first.wait_for()
                    page.locator('#parent').click()
                    item('alpha.txt').wait_for()
                    page.locator('#hidden').uncheck()
                    item('.hidden').wait_for(state='detached')
                    page.locator('#view').select_option('grid')
                    page.wait_for_function('document.querySelector("#file-table").classList.contains("grid")')
                    page.wait_for_function('document.querySelector(".thumbnail")?.naturalWidth > 0')
                    page.locator('#view').select_option('list')
                    page.locator('#sort').select_option('size')
                    item('alpha.txt').wait_for()
                    page.locator('#sort').select_option('name')
                    item('alpha.txt').wait_for()
                    # New directory and file, followed by inline name dialog.
                    command('new')
                    page.locator('#field-name').fill('新目录')
                    confirm(); finish('新目录')
                    item('新目录').wait_for()
                    command('new')
                    page.locator('#field-action').select_option('new-file')
                    page.locator('#field-name').fill('新文件.txt')
                    confirm(); finish('新文件.txt')
                    item('新文件.txt').click()
                    page.keyboard.press('F2')
                    page.locator('#field-name').fill('重命名.txt')
                    confirm(); finish('重命名.txt')
                    item('重命名.txt').wait_for()
                    assert not (work/'新文件.txt').exists()
                    # Clipboard across directory navigation and conflicts.
                    item('alpha.txt').click()
                    page.keyboard.press('Control+c')
                    item('folder').dblclick()
                    page.get_by_text('此目录为空',exact=True).first.wait_for()
                    page.keyboard.press('Control+v')
                    confirm(); finish('alpha.txt')
                    assert (work/'folder/alpha.txt').read_text() == 'alpha contents'
                    page.keyboard.press('Control+v')
                    page.locator('#field-conflict').select_option('keep')
                    confirm(); finish('alpha.txt')
                    item('alpha (1).txt').wait_for()
                    item('alpha (1).txt').click()
                    command('cut')
                    page.locator('#parent').click()
                    item('alpha.txt').wait_for()
                    command('paste'); confirm(); finish('alpha (1).txt')
                    assert (work/'alpha (1).txt').exists()
                    assert not (work/'folder/alpha (1).txt').exists()
                    # Drag and drop uses the selected source set and target directory.
                    item('重命名.txt').drag_to(item('新目录'))
                    confirm(); finish('重命名.txt')
                    assert (work/'新目录/重命名.txt').is_file()
                    assert not (work/'重命名.txt').exists()
                    # Context menu, safe code preview and properties.
                    item('unsafe.html').click(button='right')
                    page.get_by_role('menuitem',name='打开',exact=True).click()
                    page.locator('#preview-content pre').wait_for()
                    assert page.locator('#preview-content pre').inner_text() == '<script>window.pwned=1</script>'
                    assert page.evaluate('window.pwned') is None
                    page.get_by_role('button',name='关闭预览',exact=True).click()
                    item('alpha.txt').click(); command('info')
                    page.locator('.properties').wait_for()
                    assert 'alpha.txt' in page.locator('.properties').inner_text()
                    page.get_by_role('button',name='关闭预览',exact=True).click()
                    item('image.png').dblclick()
                    page.wait_for_function('document.querySelector("#preview-content img")?.naturalWidth===1')
                    page.get_by_role('button',name='关闭预览',exact=True).click()
                    # Native file download; multi-file ZIP download is a job.
                    item('alpha.txt').click()
                    with page.expect_download() as received:
                        command('download')
                    assert Path(received.value.path()).read_text() == 'alpha contents'
                    item('folder').click()
                    with page.expect_download() as received:
                        command('download')
                    assert received.value.suggested_filename == 'folder.zip'
                    finish('folder')
                    # Upload a real file and check the backend filesystem.
                    with page.expect_file_chooser() as chooser:
                        command('upload')
                    assert page.locator('#address').input_value() == str(work)
                    chooser.value.set_files({'name':'上传.txt','mimeType':'text/plain','buffer':'上传内容'.encode()})
                    confirm(); finish('上传.txt')
                    assert (work/'上传.txt').read_text() == '上传内容'
                    # A failed request retains the upload and exposes a working resume button.
                    page.route('**/api/session/files/upload?*',lambda route: route.abort(),times=1)
                    with page.expect_file_chooser() as chooser:
                        command('upload')
                    chooser.value.set_files({'name':'续传.txt','mimeType':'text/plain','buffer':b'resume-content'})
                    confirm()
                    page.get_by_role('button',name='继续上传',exact=True).click()
                    finish('续传.txt')
                    assert (work/'续传.txt').read_bytes() == b'resume-content'
                    # ZIP compress and extract through dialogs.
                    item('alpha.txt').click(); command('compress')
                    page.locator('#field-name').fill('package.zip')
                    confirm(); finish('package.zip')
                    item('package.zip').click(); command('extract')
                    confirm(); finish('package.zip')
                    assert (work/'package/alpha.txt').read_text() == 'alpha contents'
                    # Delete is permanent, with confirmation; cancelling does nothing.
                    scope = file_manager.scope_for(node.state['row'])
                    previous_trash = file_manager.manager().trash_list(scope)
                    item('beta.txt').click(); command('delete')
                    assert page.locator('#action-title').inner_text() == '永久删除'
                    assert '无法撤销' in page.locator('#action-description').inner_text()
                    assert str(work/'beta.txt') in page.locator('#action-description').inner_text()
                    assert (work/'beta.txt').exists()
                    page.locator('#action-cancel').click()
                    assert (work/'beta.txt').exists()
                    item('beta.txt').click(); page.keyboard.press('Delete')
                    confirm(); finish('beta.txt')
                    assert not (work/'beta.txt').exists()
                    item('package').click(button='right')
                    page.get_by_role('menuitem',name='永久删除',exact=True).click()
                    confirm(); finish('package')
                    assert not (work/'package').exists()
                    assert file_manager.manager().trash_list(scope) == previous_trash
                    media_response = context.request.get(base+'api/session/files?'+query+'&'+urlencode({'path':str(work/'image.png'),'mode':'preview'}),headers={'Range':'bytes=2-9'})
                    assert media_response.status == 206
                    assert media_response.headers['content-range'] == f'bytes 2-9/{len(PNG)}'
                    assert media_response.body() == PNG[2:10]
                    # A stale task list survives reload and a narrow viewport.
                    page.reload(); item('alpha.txt').wait_for()
                    page.locator('#tasks-open').click()
                    page.locator('.task').first.wait_for()
                    page.get_by_role('button',name='关闭任务',exact=True).click()
                    page.set_viewport_size({'width':390,'height':740})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path='/tmp/agenthub-explorer-mobile-'+('hub' if scoped else 'node')+'.png')
                    page.set_viewport_size({'width':1280,'height':850})
                    page.screenshot(path='/tmp/agenthub-explorer-desktop-'+('hub' if scoped else 'node')+'.png')
                    # POST cannot be driven by an unrelated browser origin.
                    response = context.request.post(base+'api/session/files/action',headers={'Origin':'https://unrelated.example'},data={'uid':uid,'ref':str(work),'action':'mkdir','destination':str(work),'name':'forbidden'})
                    assert response.status == 403
                    assert not (work/'forbidden').exists()
                    assert not errors, errors
                    print(('Hub' if scoped else 'Node')+': Explorer selection, navigation, clipboard, new/rename, upload/download, ZIP, preview, permanent delete and origin protection passed',flush=True)
                    context.close()
                browser.close()
        finally:
            stop(central); stop(node)


if __name__ == '__main__':
    main()
