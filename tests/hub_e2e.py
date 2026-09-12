"""Free browser integration tests against isolated simulated machines."""
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import federation, hub, server
from hub_fixture import start_node, stop


class MountedHub(hub.HubHandler):
    """Emulate Nginx stripping /agenthub/ while the browser retains its base URL."""
    def dispatch(self):
        if self.path.startswith('/agenthub/'):
            self.path = self.path[len('/agenthub'):]
        return super().dispatch()


def contrast(fg, bg):
    """WCAG contrast ratio of two ``rgb()`` / ``rgba()`` strings."""
    def luminance(css):
        channels = [float(v) for v in css[css.index('(') + 1:css.index(')')].replace('/', ',').split(',')[:3]]
        linear = [c / 255 / 12.92 if c / 255 <= .03928 else ((c / 255 + .055) / 1.055) ** 2.4 for c in channels]
        return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + .05) / (min(a, b) + .05)


def main():
    with tempfile.TemporaryDirectory() as root:
        nodes = [start_node(char * 32, name) for char, name in [('a', 'NodeA'), ('b', 'NodeB'), ('c', 'Vega')]]
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        for n in nodes:
            registry.register({'name': n.state['name'], 'url': f'http://127.0.0.1:{n.server_port}', 'token': n.state['token']})
        srv = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        srv.daemon_threads = True; srv.registry = registry; srv.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1280, 'height': 900})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                base = f'http://127.0.0.1:{srv.server_port}/agenthub/'
                page.goto(base)
                page.wait_for_function('S.sessions.length === 3 && Nodes.list.length === 3 && !!Nodes.capabilities["' + 'b' * 32 + '"]')
                assert page.locator('.ghead').count() == 3
                assert all(name in page.locator('#side').inner_text() for name in ['NodeA', 'NodeB', 'Vega'])
                assert len(set(page.evaluate('S.sessions.map(s => s.uid)'))) == 3
                # Scope is a single choice; zero activity still leaves both counts visible.
                page.wait_for_function('document.querySelector("#session-total").textContent === "3"')
                assert page.locator('#session-active').inner_text() == '0'
                page.locator('#livecount').click()
                page.locator('#livecount').click()
                assert page.locator('#livecount').get_attribute('aria-checked') == 'true'
                assert page.locator('#side .item').count() == 0
                page.reload()
                page.wait_for_function('S.sessions.length === 3 && Nodes.list.length === 3')
                assert page.locator('#livecount').get_attribute('aria-checked') == 'true'
                page.locator('#livecount').press('ArrowRight')
                assert page.locator('#allcount').get_attribute('aria-checked') == 'true'
                assert page.locator('#side .item').count() == 3
                page.locator('#allcount').click()
                assert page.locator('#session-scope [aria-checked="true"]').count() == 1
                def check_toolbar(wide=True):
                    # 宽屏机器 chips 与其余筛选平铺成一排；窄屏收成下拉按钮，展开后才是同一组 chips
                    groups = ['#node-chips' if wide else '#node-pick', '#chips', '#view']
                    styles = page.evaluate("""groups => groups.map(q=>{
                      const group=document.querySelector(q), r=group.getBoundingClientRect();
                      const s=getComputedStyle(group.querySelector('button.on') || group);
                      return {top:r.top,height:r.height,
                        radius:getComputedStyle(group).borderRadius,buttonHeight:s.height};
                    })""", groups)
                    assert max(r['top'] for r in styles)-min(r['top'] for r in styles) < 1, styles
                    assert all(r['height'] == styles[0]['height'] for r in styles), styles
                    assert page.locator('header #node-chips').count() == 1
                    assert page.locator('#node-chips').get_by_role('button', name='全部', exact=True).count() == 0
                    assert page.locator('#node-pick').is_visible() != wide
                    assert page.locator('#node-chips').is_visible() == wide
                    assert page.locator('.brand-name + #side-toggle + #session-scope').count() == 1
                    assert page.locator('header').bounding_box()['height'] <= 52
                    assert page.locator('#session-scope [role=radio]').count() == 2
                    if wide:
                        assert page.locator('#session-scope').bounding_box()['width'] == 98
                        assert all(b.bounding_box()['width'] == 48 for b in page.locator('#session-scope button').all())
                        return
                    page.locator('#node-pick').click()
                    assert page.locator('#node-chips').is_visible()
                    assert page.locator('#node-chips button').count() == 3
                    assert page.locator('#node-chips button.on').count() == 3
                    page.locator('#node-chips button').nth(1).click()
                    assert page.locator('#node-chips').is_visible()   # 多选：点一项不收起
                    assert page.locator('#node-chips button.on').count() == 2
                    assert page.locator('#node-pick .node-pick-label').inner_text() == '2 台'
                    page.locator('#node-chips button').nth(1).click()
                    assert page.locator('#node-pick .node-pick-label').inner_text() == '全部'
                    page.keyboard.press('Escape')
                    assert not page.locator('#node-chips').is_visible()
                    # 右侧按钮全部折进 ⋯
                    assert not page.locator('#settings').is_visible()
                    page.locator('#header-more-btn').click()
                    assert page.locator('#header-menu #settings').is_visible()
                    assert page.locator('#header-menu #reload').is_visible()
                    page.keyboard.press('Escape')
                    assert not page.locator('#header-menu').is_visible()
                check_toolbar()
                page.locator('header').screenshot(path='/tmp/agenthub-toolbar-after-desktop.png')
                # Single/multi node filters and independent Agent Type intersection.
                page.get_by_role('button', name='NodeB 1', exact=True).dblclick()
                page.wait_for_function('visible().length === 1')
                assert page.locator('#session-total').inner_text() == '1'
                page.get_by_role('button', name='NodeA 1', exact=True).click()
                page.wait_for_function('visible().length === 2')
                assert page.locator('#session-total').inner_text() == '2'
                page.locator('#chips button[data-source="claude"]').click()
                assert page.locator('#session-total').inner_text() == '0'
                page.locator('#chips button[data-source="claude"]').click()
                page.locator('#q').fill('needle'); page.locator('#q').press('Enter')
                page.wait_for_function('S.results?.length === 2')
                assert {r['node_name'] for r in page.evaluate('S.results')} == {'NodeA', 'NodeB'}
                assert '命中' in page.locator('.side-search #stat').inner_text()
                assert page.locator('#session-total').inner_text() == '2'
                # Cold searches exceeding the old 15s cutoff keep visible progress.
                nodes[1].state.update(search_steps=17, search_delay=1)
                seq = page.locator('#stat').get_attribute('data-seq')
                page.locator('#q').press('Enter')
                page.wait_for_function('document.querySelector("#search-progress b").textContent.includes(" / ")')
                assert page.locator('#search-progress').is_visible()
                page.wait_for_function('(seq) => document.querySelector("#stat").dataset.seq !== seq',
                                       arg=seq, timeout=25000)
                assert page.evaluate('S.results.length') == 2
                assert not page.locator('#node-notice').is_visible()
                nodes[1].state.pop('search_steps'); nodes[1].state.pop('search_delay')
                nodes[1].state['search_error'] = True
                seq = page.locator('#stat').get_attribute('data-seq')
                page.locator('#q').press('Enter')
                page.wait_for_function('(seq) => document.querySelector("#stat").dataset.seq !== seq', arg=seq)
                assert 'NodeB 全文搜索失败' in page.locator('#node-notice').inner_text()
                assert '离线' not in page.locator('#node-notice').inner_text()
                assert '结果不完整' in page.locator('#stat').inner_text()
                assert page.evaluate('Nodes.list.find(n => n.name === "NodeB").online')
                nodes[1].state.pop('search_error')
                page.locator('#q').press('Enter')
                page.wait_for_function('S.results.length === 2 && !document.querySelector("#search-progress").classList.contains("on")')
                assert not page.locator('#node-notice').is_visible()
                # Open identical native IDs on distinct nodes, render media and SSE.
                for i, name in enumerate(['NodeA', 'NodeB']):
                    uid = federation.qualify(chr(97 + i) * 32, 'claude:same-file-hash', True)
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('(name) => document.querySelector("#msgs")?.textContent.includes("reply " + name)', arg=name)
                    def check_session_identity():
                        # 元信息按 消息数、大小、时间、机器、目录、来源、会话号 的顺序放在标题后，
                        # 放不下的才进 ⋯ 菜单；两处合起来正好一份
                        brief = page.locator('.dbrief > span').all_text_contents()
                        meta = page.locator('.dmeta > span').all_text_contents()
                        fields = brief + meta
                        assert fields[-4:] == [name, nodes[i].state['row']['cwd'], 'Claude',
                                               nodes[i].state['row']['sid']], fields
                        assert fields.count(name) == 1, fields
                    check_session_identity()
                    page.wait_for_function('Array.from(document.querySelectorAll("#msgs img")).some(i => i.complete && i.naturalWidth > 0)')
                    nodes[i].state['messages'].append({'role': 'assistant', 'text': name + ' streamed update', 'ts': '2026-09-07T00:01:00Z'})
                    page.wait_for_function('(name) => document.querySelector("#msgs")?.textContent.includes(name + " streamed update")', arg=name)
                    check_session_identity()
                # Real nodes heartbeat every 20s: an idle stream must outlive HTTP's 10s timeout.
                nodes[1].state['pause_stream'] = True
                watch_count = len([p for p, _ in nodes[1].state['gets'] if p == '/api/watch'])
                page.wait_for_timeout(12000)
                assert len([p for p, _ in nodes[1].state['gets'] if p == '/api/watch']) == watch_count
                nodes[1].state['pause_stream'] = False
                # New-session machine changes both capabilities and directory suggestions.
                page.locator('#new-session').click()
                # Chromium 的原生下拉弹层用 select 自身的 background-color 做底色，弹层文档的 body
                # 固定为白色：机器下拉在两种主题下都必须是与外框同色的不透明面板、字色可读
                # （BUG-20260912-091845：深色主题下透明背景成了白底浅字）。
                for theme in ['dark', 'light']:
                    page.evaluate('(theme) => applyTheme(theme, true)', theme)
                    picker = page.evaluate('''() => {
                      const select = document.querySelector('#new-node');
                      const own = getComputedStyle(select), wrap = getComputedStyle(select.parentElement);
                      return {bg: own.backgroundColor, fg: own.color, wrap: wrap.backgroundColor,
                              scheme: own.colorScheme, theme: document.documentElement.dataset.theme};
                    }''')
                    assert picker['theme'] == theme and picker['scheme'] == theme, picker
                    assert picker['bg'].startswith('rgb('), picker   # 不透明，不是 rgba(0, 0, 0, 0)
                    assert picker['bg'] == picker['wrap'], picker
                    assert contrast(picker['fg'], picker['bg']) >= 7, picker
                page.evaluate('applyTheme("system", true)')
                page.locator('#new-node').select_option('b' * 32)
                page.locator('#new-cwd').fill('/home/')
                page.wait_for_function('cwdCompletion.rows.includes("/home/NodeB/work/")')
                page.locator('#new-cwd').fill('/home/NodeB/work')
                page.locator('#new-session-go').click()
                page.wait_for_function('String(S.sel).startsWith("tmux:' + 'b' * 32 + '~")')
                assert nodes[1].state['writes'][-1][0] in {'/api/term/create', '/api/term/claim', '/api/audit/browser'}
                creates = [b for p, b in nodes[1].state['writes'] if p == '/api/term/create']
                assert creates[-1]['cwd'] == '/home/NodeB/work' and creates[-1]['request_id']
                assert not any(p == '/api/term/create' for p, _ in nodes[0].state['writes'])
                # WebSocket bidirectional transport (no tmux/CLI involved).
                echoed = page.evaluate('''async () => {
                  const url = new URL(appUrl('api/term/attach')); url.protocol = 'ws:';
                  url.searchParams.set('name', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa~same-terminal');
                  return await new Promise((resolve, reject) => {
                    const ws = new WebSocket(url); ws.binaryType = 'arraybuffer';
                    const timer = setTimeout(() => {ws.close(); reject(new Error('WS timeout'));}, 5000);
                    ws.onopen = () => ws.send('terminal-echo');
                    ws.onmessage = e => {if(e.data === 'terminal-echo') {clearTimeout(timer); ws.close(); resolve(e.data);}};
                    ws.onerror = () => reject(new Error('WS failed'));
                  });
                }''')
                assert echoed == 'terminal-echo'
                # Binary upload routing preserves bytes and filename, scopes media URL.
                uploaded = page.evaluate('''async () => {
                  const u = new URL(appUrl('api/session/attachment'));
                  u.searchParams.set('uid', 'claude:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa~same-file-hash');
                  u.searchParams.set('name', 'same-name.png');
                  return await (await fetch(u, {method:'POST', body: new Uint8Array([0,255,17,0])})).json();
                }''')
                assert uploaded['name'] == 'same-name.png' and 'a' * 32 in uploaded['media']['src']
                assert nodes[0].state['uploads'][-1][1] == bytes([0, 255, 17, 0])
                assert page.locator('#nodes-dialog, #nodes-form').count() == 0
                assert page.get_by_role('button', name='管理机器', exact=True).count() == 0
                # Explicit node deep link resolves duplicate native IDs and survives reload.
                deep = context.new_page()
                deep.goto(base + '?node=' + 'b' * 32 + '&sid=claude:same-native-id')
                deep.wait_for_function('S.sel === "claude:' + 'b' * 32 + '~same-file-hash"')
                deep.reload()
                deep.wait_for_function('S.sel === "claude:' + 'b' * 32 + '~same-file-hash"')
                deep.close()
                # A successful term response slower than the 3s poll interval
                # must eventually apply, not be superseded forever by new polls.
                nodes[0].state['term_delay'] = 3.6
                slow = context.new_page()
                slow.goto(base + '?node=' + 'a' * 32 + '&sid=claude:same-native-id')
                slow.wait_for_function('Nodes.capabilities["' + 'a' * 32 + '"]?.enabled',
                                       timeout=12000)
                assert slow.locator('#a-term').is_visible()
                slow.close()
                nodes[0].state['term_delay'] = 0
                # Offline machine doesn't hide healthy results; local UI stays independent.
                for button in page.locator('#node-chips button[aria-pressed="false"]').all():
                    button.click()
                nodes[2].state['offline'] = True
                page.evaluate('() => loadSessions()')
                page.wait_for_function('S.sessions.some(s => s.node_name === "Vega" && s.stale)')
                # 刚失联时「列表失败」提示是合理的；离线判定落地后才要求没有单独的离线横幅。
                page.wait_for_function(
                    'Nodes.list.find(n => n.name === "Vega")?.online === false', timeout=20000)
                assert not page.locator('#node-notice').is_visible()
                assert page.locator('#node-chips button.node-offline').is_visible()
                # Bug reports with no selected session go to an online machine, and an
                # offline-only selection is refused before any attachment upload.
                report_target = page.evaluate('''() => {
                  const offline = Nodes.list.filter(n => n.online === false).map(n => n.id);
                  const selectedBefore = S.sel;
                  const fromSession = bugReportNode();
                  S.sel = null;
                  const chosen = bugReportNode();
                  const onlyOffline = new Set(Nodes.list.filter(n => n.online !== false).map(n => n.id));
                  const saved = new Set(Nodes.off);
                  for (const id of onlyOffline) Nodes.off.add(id);
                  const refused = bugReportNodeError(bugReportNode());
                  Nodes.off.clear(); for (const id of saved) Nodes.off.add(id);
                  S.sel = selectedBefore;
                  return {offline, chosen, refused, fromSession, selected: S.sel, sessionNode: nodeOf(S.sel)};
                }''')
                assert report_target['fromSession'] == report_target['sessionNode'], report_target
                assert report_target['offline'] and report_target['chosen'] not in report_target['offline'], report_target
                assert report_target['chosen'] in [n.state['id'] for n in nodes], report_target
                assert '离线' in report_target['refused'], report_target
                page.locator('#q').fill('needle'); page.locator('#q').press('Enter')
                page.wait_for_function('S.results?.length === 2')
                # Mobile: same machine controls, no horizontal document overflow.
                page.set_viewport_size({'width': 390, 'height': 844})
                # headless 只有渲染一帧后才派发媒体查询 change，顶栏折叠靠它驱动
                page.evaluate('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
                page.evaluate('showMobileList()')
                check_toolbar(wide=False)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('header').screenshot(path='/tmp/agenthub-toolbar-after-mobile.png')
                page.screenshot(path='/tmp/agenthub-hub-mobile.png')
                local = context.new_page(); local.on('pageerror', lambda e: errors.append(str(e)))
                local.goto(f'http://127.0.0.1:{nodes[0].server_port}')
                local.wait_for_function('S.sessions.length === 1')
                assert not local.locator('#node-chips').is_visible()
                assert local.evaluate('S.sessions[0].uid') == 'claude:same-file-hash'
                assert not errors, errors
                browser.close()
                print('PASS: three-node DOM, filtering, search, SSE, media, create routing, WebSocket, upload, offline, mobile, standalone')
        finally:
            for item in [srv, *nodes]: stop(item)


if __name__ == '__main__':
    main()
