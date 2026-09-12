"""Subagent menu: start/end times, end-time ordering, running dots. Isolated HTTP, no CLI or tmux."""
import sys
import tempfile
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import NodeHandler, start_node, stop


class LiveNode(NodeHandler):
    def do_GET(self):
        if urlparse(self.path).path == '/api/live':
            uid = self.state['row']['uid']
            live = self.state.get('live')
            return self._json({'uids': [uid] if live else [], 'tmux_uids': [], 'started_at': {}})
        return super().do_GET()

    def messages(self, q):
        data = super().messages(q)
        agent = q.get('agent', [''])[0]
        if agent:
            data['meta'] = {**data['meta'], 'agent_id': agent,
                            'parent_title': data['meta']['title'], 'title': 'view ' + agent}
        return data


def today(hour, minute):
    return datetime.now().astimezone().replace(
        hour=hour, minute=minute, second=0, microsecond=0).isoformat()


# 服务端给的顺序故意打乱；early 最早结束，late 最晚结束，worker 还在跑但最后一条记录早于 late。
AGENTS = [
    {'id': 'early', 'title': 'Early finished', 'type': 'Explore',
     'created': today(9, 0), 'updated': today(9, 30), 'active': False},
    {'id': 'worker', 'title': 'Still working', 'type': 'general-purpose',
     'created': today(9, 20), 'updated': today(9, 40), 'active': True},
    {'id': 'late', 'title': 'Late finished', 'type': 'general-purpose',
     'created': today(9, 10), 'updated': today(10, 0), 'active': False},
]


def rows(page):
    return page.locator('#session-view-menu button').evaluate_all('''items => items.map(b => ({
      agent: b.dataset.agent, on: b.classList.contains('on'), running: b.classList.contains('running'),
      dot: b.querySelectorAll('.view-live').length, kind: b.querySelector('.view-kind')?.textContent.trim(),
      span: b.querySelector('.view-span')?.textContent.trim() ?? '', title: b.querySelector('b').textContent}))''')


def open_menu(page):
    switch = page.locator('#a-view-switch')
    if switch.get_attribute('aria-expanded') == 'true':
        switch.click()
        page.wait_for_function('document.querySelector("#session-view-menu").hidden')
    switch.click()
    page.wait_for_function('!document.querySelector("#session-view-menu").hidden')
    return rows(page)


def assert_geometry(page):
    """起止时间和绿点都要落在自己那一行按钮里，窄屏也不能被裁掉或挤出去。"""
    boxes = page.locator('#session-view-menu button[data-agent]:not([data-agent=""])').evaluate_all('''items =>
      items.map(b => {
        const r = b.getBoundingClientRect(), menu = document.querySelector('#session-view-menu').getBoundingClientRect();
        const inside = el => { if (!el) return true; const e = el.getBoundingClientRect();
          return e.left >= r.left - .5 && e.right <= r.right + .5 && e.top >= r.top - .5 && e.bottom <= r.bottom + .5; };
        const span = b.querySelector('.view-span'), dot = b.querySelector('.view-live');
        return {span: inside(span), dot: inside(dot), fits: r.right <= menu.right + .5 && r.left >= menu.left - .5,
                hit: b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)),
                dotVisible: !dot || (dot.offsetWidth > 0 && dot.offsetHeight > 0)};
      })''')
    assert all(all(b.values()) for b in boxes), boxes


def check_page(page, node, uid):
    page.evaluate('(uid) => openSession(uid)', uid)
    page.locator('#a-view-switch').wait_for()

    # 父进程没在跑：没有绿点，纯按结束时间倒序
    node.state['live'] = None
    page.evaluate('refreshLive(true)')
    page.wait_for_function('!S.live.size')
    menu = open_menu(page)
    assert [r['agent'] for r in menu] == ['', 'late', 'worker', 'early'], menu
    assert [r['dot'] for r in menu] == [0, 0, 0, 0], menu
    assert menu[0]['on'] and menu[0]['kind'] == '主会话', menu
    assert [r['span'] for r in menu[1:]] == [
        '今天 09:10 → 10:00', '今天 09:20 → 09:40', '今天 09:00 → 09:30'], menu
    assert [r['kind'] for r in menu[1:]] == [
        '子代理 · general-purpose', '子代理 · general-purpose', '子代理 · Explore'], menu
    assert_geometry(page)

    # 父进程跑起来后再打开：还在跑的排最前、带绿点、没有结束时间；标题栏本身不重建
    head_id = page.evaluate('document.querySelector(".dhead").__probe = Math.random()')
    node.state['live'] = 'direct'
    page.evaluate('refreshLive(true)')
    page.wait_for_function('S.live.size === 1')
    menu = open_menu(page)
    assert page.evaluate('document.querySelector(".dhead").__probe') == head_id
    assert [r['agent'] for r in menu] == ['', 'worker', 'late', 'early'], menu
    assert [r['dot'] for r in menu] == [0, 1, 0, 0], menu
    assert [r['running'] for r in menu] == [False, True, False, False], menu
    assert menu[1]['span'] == '今天 09:20 → 运行中', menu
    assert page.locator('#session-view-menu .view-live').get_attribute('title') == '运行中'
    dot_color, side_color = page.evaluate('''() => [
      getComputedStyle(document.querySelector('#session-view-menu .view-live')).backgroundColor,
      getComputedStyle(document.querySelector('#dlive')).backgroundColor]''')
    assert dot_color == side_color, (dot_color, side_color)
    assert_geometry(page)

    # 列表刷新带来的新状态在下次打开时生效：worker 结束了
    node.state['row']['agent_items'] = [
        {**a, 'active': False, 'updated': today(10, 5)} if a['id'] == 'worker' else a for a in AGENTS]
    page.evaluate('loadSessions(true)')
    page.wait_for_function('S.sessions[0].agent_items.every(a => !a.active)')
    menu = open_menu(page)
    assert [r['agent'] for r in menu] == ['', 'worker', 'late', 'early'], menu
    assert [r['dot'] for r in menu] == [0, 0, 0, 0], menu
    assert menu[1]['span'] == '今天 09:20 → 10:05', menu
    node.state['row']['agent_items'] = list(AGENTS)
    page.evaluate('loadSessions(true)')
    page.wait_for_function('S.sessions[0].agent_items.some(a => a.active)')

    # 切进子代理视图后重新打开，选中标记跟着走，顺序不变
    page.locator('#session-view-menu button[data-agent="late"]').click()
    page.wait_for_function('document.querySelector(".dhead h2")?.textContent.includes("view late")')
    menu = open_menu(page)
    assert [r['agent'] for r in menu] == ['', 'worker', 'late', 'early'], menu
    assert [r['on'] for r in menu] == [False, False, True, False], menu
    page.locator('#session-view-menu button[data-agent=""]').click()
    page.wait_for_function('!document.querySelector(".dhead h2")?.textContent.includes("view late")')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'LiveNode')
        node.RequestHandlerClass = LiveNode
        node.state['row']['agent_items'] = list(AGENTS)
        node.state['row']['agents'] = len(AGENTS)
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'LiveNode', 'url': f'http://127.0.0.1:{node.server_port}',
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
                for scoped in (False, True):
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped
                            else f'http://127.0.0.1:{node.server_port}/')
                    uid = 'claude:' + ('a' * 32 + '~' if scoped else '') + 'same-file-hash'
                    for width, height in ((1400, 900), (390, 760)):
                        context = browser.new_context(viewport={'width': width, 'height': height},
                                                      color_scheme='dark' if scoped else 'light')
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(base)
                        page.wait_for_function('S.sessions.length > 0')
                        check_page(page, node, uid)
                        assert not errors, errors
                        context.close()
                browser.close()
        finally:
            central.shutdown()
            central.server_close()
            stop(node)
    print('agent menu e2e ok')


if __name__ == '__main__':
    main()
