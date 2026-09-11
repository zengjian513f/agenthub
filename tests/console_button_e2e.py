"""Console availability explanations on isolated nodes; no real CLI or tmux."""
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
        node = start_node('a' * 32, 'NodeA')
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'NodeA', 'url': f'http://127.0.0.1:{node.server_port}', 'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True; central.registry = registry; central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for hub_mode in [False, True]:
                    context = browser.new_context(viewport={'width': 1280, 'height': 900})
                    page = context.new_page(); errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('dialog', lambda d: d.dismiss() if d.type == 'confirm' else d.accept())
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if hub_mode else
                            f'http://127.0.0.1:{node.server_port}/')
                    page.goto(base)
                    page.wait_for_function('S.sessions.length && T.listLoaded')
                    uid = page.evaluate('S.sessions[0].uid')
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('document.querySelector("#a-star") && document.querySelector("#a-term").dataset.unavailable === "false"')
                    button = page.locator('#a-term')

                    def unavailable(reason):
                        page.wait_for_function('document.querySelector("#a-term")?.dataset.unavailable === "true"')
                        assert button.is_visible() and button.is_enabled()
                        assert button.locator('svg').count() == 1
                        assert '!' not in button.inner_text()
                        button.hover()
                        assert page.locator('#console-toast').is_visible()
                        assert reason in page.locator('#console-toast').inner_text()
                        with page.expect_event('dialog') as opened:
                            button.click()
                        assert reason in opened.value.message, opened.value.message
                        page.mouse.move(0, 0)
                        page.locator('#q').focus()
                        button.focus()
                        assert reason in page.locator('#console-toast').inner_text()
                        page.locator('#q').focus()
                        assert not page.locator('#console-toast').is_visible()

                    node.state.update(term_enabled=False, term_reason='服务器未安装 tmux，无法打开控制台。')
                    page.evaluate('loadTermList()')
                    unavailable('未安装 tmux')
                    node.state['term_enabled'] = True
                    node.state['term_sources'] = {'claude': False, 'codex': True}
                    page.evaluate('loadTermList()')
                    unavailable('Claude 命令')
                    node.state.pop('term_sources'); node.state.pop('term_reason')
                    page.evaluate('loadTermList()')
                    page.wait_for_function('document.querySelector("#a-term").dataset.unavailable === "false"')

                    # An HTTP failure must preserve its actual reason, including recovery.
                    page.route('**/api/term/list*', lambda r: r.fulfill(status=503, json={'error': '终端服务正在维护'}))
                    page.evaluate('loadTermList()')
                    unavailable('HTTP 503')
                    assert '终端服务正在维护' in button.get_attribute('aria-label')
                    page.unroute('**/api/term/list*')
                    page.evaluate('loadTermList()')
                    page.wait_for_function('document.querySelector("#a-term").dataset.unavailable === "false"')

                    # Child views and failed message loads still retain the control.
                    page.evaluate('(uid) => openSession(uid, "child")', uid)
                    unavailable('子代理没有独立控制台')
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.route('**/api/messages/**', lambda r: r.fulfill(status=502, json={'error': '会话读取暂时失败'}))
                    page.evaluate('(uid) => {cache.delete(viewKey(uid)); return openSession(uid)}', uid)
                    assert button.is_visible()
                    page.unroute('**/api/messages/**')
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('document.querySelector("#a-star") !== null')

                    # An attempted takeover reports backend errors without losing its button.
                    page.route('**/api/term/takeover', lambda r: r.fulfill(status=409, json={'error': '会话文件已不存在'}))
                    with page.expect_event('dialog') as opened:
                        button.click()
                    assert '会话文件已不存在' in opened.value.message
                    page.wait_for_function('document.querySelector("#a-term").dataset.unavailable === "true"')
                    button.hover()
                    assert '会话文件已不存在' in page.locator('#console-toast').inner_text()
                    assert button.is_visible() and button.is_enabled()
                    with page.expect_event('dialog') as opened:
                        button.click()
                    assert '会话文件已不存在' in opened.value.message
                    assert opened.value.type == 'confirm'

                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.evaluate('showMobileDetail()')
                    assert button.is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=f'/tmp/agenthub-console-{hub_mode}-mobile.png')
                    assert not errors, errors

                    # A session that is listed but whose host refuses attach (BUG-20260911-170830):
                    # the socket upgrades and then closes 1011 before any frame. That must not
                    # count as a connection — the button keeps its explanation instead of
                    # flashing, retries back off, and the first real frame clears the state.
                    page.set_viewport_size({'width': 1280, 'height': 900})
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('document.querySelector("#a-term") !== null')
                    button = page.locator('#a-term')
                    node.state['term_sessions'] = [{
                        'name': 'same-terminal', 'uid': node.state['row']['uid'], 'created': 1,
                        'attached': False, 'pid': 1, 'cwd': '/same/project', 'cmd': 'sh', 'cols': 80,
                        'rows': 24, 'owned': True, 'server': 'ptyhost', 'backend': 'ptyhost'}]
                    node.state['attach_error'] = 'attach 失败: 连接已关闭'
                    sockets = []
                    page.on('websocket', lambda ws: sockets.append(ws))
                    page.evaluate('loadTermList()')
                    page.wait_for_function('T.list.some(x => x.name.endsWith("same-terminal"))')
                    view = 'T.views.get(T.list.find(x => x.name.endsWith("same-terminal")).name)'
                    page.evaluate('ConsoleUI.errors.delete(S.sel); renderTakeoverBtn()')
                    button.click()
                    page.wait_for_function('!document.querySelector("#termpane").classList.contains("hidden")')
                    page.wait_for_function(
                        '(ConsoleUI.errors.get(S.sel) || "").includes("attach failed: attach 失败: 连接已关闭")')
                    page.wait_for_timeout(4000)
                    attempts = len(sockets)
                    assert 2 <= attempts <= 5, f'4 秒内重连 {attempts} 次，退避没有生效'
                    assert page.evaluate(f'{view}.reconnectDelay') > 900
                    assert button.get_attribute('data-unavailable') == 'true'
                    assert 'attach 失败' in button.get_attribute('aria-label')
                    node.state['attach_error'] = ''
                    page.wait_for_function(
                        f'!ConsoleUI.errors.has(S.sel) && {view}.reconnectDelay === 500', timeout=15000)
                    page.wait_for_function('document.querySelector("#a-term").dataset.unavailable === "false"')
                    page.evaluate('closeTermPane()')
                    node.state['term_sessions'] = []
                    assert not errors, errors
                    context.close()

                # App UI must explain a terminal script load failure on its own.
                page = browser.new_page()
                page.on('dialog', lambda d: d.accept())
                page.route('**/term.js*', lambda r: r.abort())
                page.goto(f'http://127.0.0.1:{node.server_port}')
                page.wait_for_function('S.sessions.length > 0')
                page.evaluate('openSession(S.sessions[0].uid)')
                page.locator('#a-term').wait_for()
                with page.expect_event('dialog') as opened:
                    page.locator('#a-term').click()
                assert '控制台组件' in opened.value.message
                browser.close()
            print('PASS: persistent console, gray state, hover/focus toast, click errors, recovery, subagents, loading failure, mobile, missing script')
        finally:
            for srv in [central, node]: stop(srv)


if __name__ == '__main__':
    main()
