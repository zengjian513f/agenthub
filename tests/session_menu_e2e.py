"""Session menu hit testing over xterm; isolated HTTP/SSE/WS, no CLI or tmux."""
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import NodeHandler, start_node, stop


class MenuNode(NodeHandler):
    def do_GET(self):
        if urlparse(self.path).path == '/api/term/list':
            return self._json({'enabled': True, 'sources': {'claude': True},
                               'sessions': [{'name': 'menu-terminal',
                                             'uid': self.state['row']['uid']}],
                               'pending': []})
        return super().do_GET()

    def messages(self, q):
        data = super().messages(q)
        agent = q.get('agent', [''])[0]
        if agent:
            data['meta'] = {**data['meta'], 'agent_id': agent,
                            'parent_title': data['meta']['title'], 'title': agent}
        return data


def assert_menu_hits(page):
    # Visibility alone passes for clipped elements. Test actual pointer targets,
    # including the first row crossing the terminal's resize handle.
    hits = page.locator('#session-view-menu button').evaluate_all('''items =>
      items.map(e => {
        const r = e.getBoundingClientRect();
        return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      })''')
    assert hits == [True] * 6, hits


def tier_of(width):
    return 'narrow' if width <= 720 else 'medium' if width <= 1199 else 'wide'


def open_actions(page):
    """The ⋯ menu exists at every tier; on wide screens it only holds the full metadata."""
    page.locator('#a-more').click()


def header_ids(page):
    return page.locator('.dhead-actions button:visible').evaluate_all(
        'buttons => buttons.map(b => b.id || (b.hasAttribute("data-report-bug") ? "report-bug" : ""))')


INLINE_IDS = {
    'wide': ['a-term', 'a-star', 'a-turns', 'report-bug', 'a-session-action', 'a-more'],
    'medium': ['a-term', 'a-star', 'a-more'],
    'narrow': ['a-term', 'a-more'],
}
# 标题后的简要元信息；机器徽章只有中央站的会话才有
BRIEF_IDS = {'wide': ['mcount-total', 'dlive', 'meta-node', 'meta-source'],
             'medium': ['meta-node'], 'narrow': []}


def assert_actions_menu(page, tier, scoped):
    menu = page.locator('#session-actions-menu')
    max_height = 46   # 会话头任何宽度都只有一行
    wide = tier == 'wide'
    assert header_ids(page) == INLINE_IDS[tier], header_ids(page)
    brief = page.locator('.dbrief > *').evaluate_all(
        'items => items.map(e => e.id || e.className.split(" ")[0])')
    assert brief == [x for x in BRIEF_IDS[tier] if scoped or x != 'meta-node'], brief
    assert page.locator('.dbrief').is_visible() == bool(brief)
    assert page.locator('#a-more').get_attribute('aria-label') == ('会话信息' if wide else '更多会话操作')
    assert page.locator('.dhead').bounding_box()['height'] <= max_height
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    assert page.evaluate('''limit => {
      const e = document.querySelector('.dhead h2');
      const text = e.querySelector('.session-view-switch > span');
      const saved = text.textContent;
      text.textContent = 'A very long session title '.repeat(20);
      const r = e.getBoundingClientRect(), controls = document.querySelector('.dhead-actions').getBoundingClientRect();
      const fits = r.right <= controls.left && document.querySelector('.dhead').offsetHeight <= limit;
      text.textContent = saved;
      return fits;
    }''', max_height)
    open_actions(page)
    scope = page.locator('.dhead-actions') if wide else menu
    assert menu.is_visible()
    assert page.locator('#mcount-total').is_visible()
    assert page.locator('.dmeta .session-id').is_visible()
    hits = scope.locator('button:visible').evaluate_all('''items => items.map(e => {
      const r = e.getBoundingClientRect();
      return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    })''')
    assert hits and all(hits), hits
    labels = scope.locator('button:visible').evaluate_all(
        'items => items.map(e => (e.ariaLabel || e.textContent).trim())')
    assert labels and all(labels), labels
    before = page.evaluate('S.compactTurns')
    page.locator('#a-turns').click()
    assert menu.is_hidden() and page.evaluate('S.compactTurns') != before
    more = page.locator('#a-more')
    if not wide:
        first, second = ('a-star', 'a-turns') if tier == 'narrow' else ('a-turns', 'report-bug')
        more.press('ArrowDown')
        assert page.locator('#' + first).evaluate('e => e === document.activeElement')
        page.keyboard.press('End')
        assert page.locator('#a-session-action').evaluate('e => e === document.activeElement')
        page.keyboard.press('Home')
        page.keyboard.press('ArrowDown')
        assert page.locator('.dhead').locator(f'#{second}, [data-report-bug]').first.evaluate(
            'e => e === document.activeElement')
        page.keyboard.press('Escape')
        assert menu.is_hidden() and more.evaluate('e => e === document.activeElement')
        more.press('ArrowUp')
        page.keyboard.press('Tab')
        assert menu.is_hidden()
    else:
        # 平铺后每个操作自己就是 Tab 序列里的一站，不再需要菜单的方向键导航。
        page.locator('#a-star').focus()
        page.keyboard.press('Tab')
        assert page.locator('#a-turns').evaluate('e => e === document.activeElement')
    open_actions(page)
    page.locator('#a-view-switch').click()
    assert menu.is_hidden() and page.locator('#session-view-menu').is_visible()
    open_actions(page)
    assert menu.is_visible()
    assert page.locator('#session-view-menu').is_hidden()
    page.locator('.dhead h2 > .ico').click()
    assert menu.is_hidden()
    open_actions(page)
    page.locator('#a-star').click()
    page.wait_for_function('document.querySelector("#a-star").ariaPressed === "true"')
    open_actions(page)
    assert page.locator('#a-star').get_attribute('aria-label') == '取消星标'
    page.locator('#a-star').click()
    page.wait_for_function('document.querySelector("#a-star").ariaPressed === "false"')
    open_actions(page)
    page.locator('.dhead [data-report-bug]').click()
    assert page.locator('#bug-report-dialog').is_visible() and menu.is_hidden()
    page.keyboard.press('Escape')
    open_actions(page)
    page.once('dialog', lambda d: d.dismiss())
    page.locator('#a-session-action').click()
    page.wait_for_function('document.querySelector("#session-actions-menu").hidden')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'MenuNode')
        node.RequestHandlerClass = MenuNode
        node.state['row']['agent_items'] = [
            {'id': f'agent-{i}', 'title': f'Agent task {i}', 'type': 'Explore'}
            for i in range(5)]
        node.state['row']['agents'] = 5
        node.state['messages'] *= 50  # Exercise independent message scrolling.
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'MenuNode', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [False, True]:
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped
                            else f'http://127.0.0.1:{node.server_port}/')
                    uid = 'claude:' + ('a' * 32 + '~' if scoped else '') + 'same-file-hash'
                    name = ('a' * 32 + '~' if scoped else '') + 'menu-terminal'
                    for width, height in [(1698, 986), (900, 650), (721, 650), (720, 650), (375, 406), (320, 620)]:
                        context = browser.new_context(viewport={'width': width, 'height': height},
                                                      color_scheme='dark' if scoped else 'light')
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(base)
                        page.wait_for_function('S.sessions.length && T.enabled')
                        page.evaluate('(uid) => openSession(uid)', uid)
                        page.locator('#a-view-switch').wait_for()
                        page.evaluate('''async ([uid, name]) => {
                          T.uid = uid; await openTermPane(name, false, 'full');
                        }''', [uid, name])
                        page.wait_for_function('T.ws?.readyState === 1')
                        tier = tier_of(width)
                        wide = tier == 'wide'
                        assert_actions_menu(page, tier, scoped)
                        for mode in (['full', 'normal', 'collapsed'] if width > 720 else ['full']):
                            page.evaluate('''mode => {
                              T.mode = mode; T.height = 10000; layoutTermPane();
                            }''', mode)
                            page.locator('#a-view-switch').click()
                            if mode == 'collapsed':
                                assert page.locator('#msgs').evaluate('''e => {
                                  e.scrollTop = 20;
                                  return e.scrollHeight > e.clientHeight && e.scrollTop === 20;
                                }''')
                            assert_menu_hits(page)
                            page.locator('#a-view-switch').click()
                            if mode != 'collapsed' and width > 720:
                                assert page.locator('.term-resizer').evaluate('''e => {
                                  const r = e.getBoundingClientRect();
                                  return e === document.elementFromPoint(r.right - 20, r.top + 3);
                                }''')
                        # Click an actual submenu entry, then return to the main
                        # view and verify the retained terminal layout restores.
                        page.evaluate("T.mode = 'full'; rememberTermLayout(T.name); layoutTermPane()")
                        page.locator('#a-view-switch').click()
                        page.locator('[data-agent="agent-4"]').click()
                        page.wait_for_function("S.agent === 'agent-4' && document.querySelector('.dhead h2')?.textContent.includes('agent-4')")
                        assert page.locator('#termpane').is_hidden()
                        assert page.locator('#a-term').is_visible()
                        open_actions(page)
                        assert page.locator('#a-session-action').count() == 0
                        assert page.locator('#a-star').is_visible()
                        page.keyboard.press('Escape')
                        page.locator('#a-view-switch').click()
                        page.locator('#session-view-menu [data-agent=""]').click()
                        page.wait_for_function("!S.agent && !document.querySelector('#termpane').classList.contains('hidden')")
                        page.locator('#a-view-switch').click()
                        assert_menu_hits(page)
                        # Native modal dialogs must still cover the menu.
                        page.evaluate("document.querySelector('#settings-dialog').showModal()")
                        assert page.locator('#settings-dialog').evaluate('''e => {
                          const r = e.getBoundingClientRect();
                          return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
                        }''')
                        page.locator('#settings-dialog').evaluate('e => e.close()')
                        page.evaluate('(uid) => {closeTermPane(); S.term = "MenuNode"; openSession(uid)}', uid)
                        page.locator('#mcount').wait_for(state='attached')
                        open_actions(page)
                        assert page.locator('#mcount').is_visible()
                        page.locator('#m-next').click()
                        assert page.evaluate('S.cur >= 0')
                        assert page.locator('#session-actions-menu').is_hidden()
                        open_actions(page)
                        page.locator('#m-prev').click()
                        assert page.locator('#session-actions-menu').is_hidden()
                        # Pending sessions share the compact header and retain
                        # report/stop actions before any native JSONL exists.
                        page.evaluate('''() => showNewSessionStage({name: 'pending-menu',
                          source: 'claude', title: 'New session', cwd: '/example/project', node_name: 'MenuNode'})''')
                        assert header_ids(page) == (
                            ['a-term', 'report-bug', 'a-session-action', 'a-more'] if wide
                            else ['a-term', 'a-more']), header_ids(page)
                        assert page.locator('.dhead').bounding_box()['height'] <= 46
                        open_actions(page)
                        assert page.locator('#a-session-action').get_attribute('aria-label') == '停止会话'
                        assert page.locator('.dhead [data-report-bug]').is_visible()
                        page.keyboard.press('Escape')
                        assert not errors, errors
                        print(f'{"Hub" if scoped else "Node"} {width}x{height}: menu hit targets, view switching, resize handle and modal passed', flush=True)
                        context.close()
                browser.close()
        finally:
            stop(central)
            stop(node)


if __name__ == '__main__':
    main()
