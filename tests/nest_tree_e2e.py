"""左栏分层：子代理和由会话发起的会话缩进在发起者之下。隔离 HTTP 节点，不启动 CLI 或 tmux。

不变量（对直连节点和经中央两种入口、宽窄两种视口都成立）：
  - 关掉分层时列表与从前一样：没有子代理行，所有行 depth 0，孩子按自己的目录归组。
  - 开分层后孩子紧跟在发起者后面、depth 加一，跨目录的孩子离开自己的组；
    还在跑的子代理排最前，其余按活动时间倒序；发起者不在列表里的会话仍作根。
  - 子代理行可点开、单独高亮；三角收起整棵子树并持久化；回到发起者走左栏。
"""
import sys
import tempfile
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import MountedHub
from hub_fixture import NodeHandler, start_node, stop


def today(hour, minute):
    return datetime.now().astimezone().replace(
        hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def row(uid, source, sid, title, cwd, updated, **extra):
    return {'uid': uid, 'source': source, 'sid': sid, 'title': title, 'cwd': cwd,
            'size': 20, 'created': today(8, 0), 'updated': updated, 'agents': 0, **extra}


AGENTS = [
    {'id': 'x', 'title': 'Agent X still working', 'type': 'Explore',
     'created': today(9, 0), 'updated': today(9, 30), 'active': True},
    {'id': 'y', 'title': 'Agent Y finished', 'type': 'general-purpose',
     'created': today(10, 0), 'updated': today(10, 30), 'active': False},
]
ROWS = [
    row('claude:aaa', 'claude', 'sid-a', 'Root A', '/proj/alpha', today(10, 0),
        agent_items=AGENTS, agents=2),
    # B 是 A 在别的目录里派出去的 Grok；C 又是 B 派出去的 Codex
    row('grok:bbb', 'grok', 'sid-b', 'Child B', '/proj/beta', today(11, 0),
        spawned_by={'source': 'claude', 'sid': 'sid-a'}),
    row('codex:ccc', 'codex', 'sid-c', 'Grandchild C', '/proj/alpha', today(9, 0),
        spawned_by={'source': 'grok', 'sid': 'sid-b'}),
    # 发起者已经不在列表里（被删了）：仍是根
    row('claude:ddd', 'claude', 'sid-d', 'Orphan D', '/proj/alpha', today(8, 30),
        spawned_by={'source': 'claude', 'sid': 'sid-gone'}),
    row('claude:eee', 'claude', 'sid-e', 'Standalone E', '/proj/gamma', today(7, 0)),
]


class TreeNode(NodeHandler):
    def do_GET(self):
        u = urlparse(self.path)
        s = self.state
        if u.path == '/api/sessions':
            return self._json({'sessions': s['rows'], 'sig': 'tree-' + str(s['rev']), 'built_at': 0})
        if u.path == '/api/live':
            return self._json({'uids': list(s.get('live', [])), 'tmux_uids': [], 'started_at': {}})
        if u.path.startswith('/api/messages/'):
            uid = unquote(u.path.rsplit('/', 1)[1])
            match = next((r for r in s['rows'] if r['uid'] == uid), None)
            if not match:
                return self._json({'error': 'wrong UID'}, 404)
            s['row'] = match
            return self._json(self.messages(parse_qs(u.query)))
        return super().do_GET()

    def messages(self, q):
        data = super().messages(q)
        agent = q.get('agent', [''])[0]
        if agent:
            data['meta'] = {**data['meta'], 'agent_id': agent,
                            'parent_title': data['meta']['title'], 'title': 'view ' + agent}
        return data


ROWS_JS = '''() => [...document.querySelectorAll('#side .item')].map(n => ({
  key: n.dataset.key, uid: n.dataset.uid || null, agent: n.dataset.agent || null,
  depth: +n.dataset.depth, group: n.closest('.group').dataset.key,
  sel: n.classList.contains('sel'), closed: n.classList.contains('nest-closed'),
  caret: !!n.querySelector('.nest-caret'),
  pad: n.querySelector(':scope > .ico').getBoundingClientRect().left - n.getBoundingClientRect().left,
  caretX: (c => c ? c.getBoundingClientRect().left + c.getBoundingClientRect().width / 2 : null)(n.querySelector('.nest-caret')),
  gheadCaretX: (c => c.getBoundingClientRect().left + c.getBoundingClientRect().width / 2)(n.closest('.group').querySelector('.ghead .caret')),
  dot: !!n.querySelector('.item-status.visible')}))'''


def rows(page):
    return page.evaluate(ROWS_JS)


def uid(scoped, local):
    source, _, rest = local.partition(':')
    return f"{source}:{'a' * 32 + '~' if scoped else ''}{rest}"


def to_list(page):
    """窄屏打开会话后只剩详情页，要先返回列表才能再点左栏。"""
    if page.evaluate('document.body.classList.contains("mobile-detail")'):
        page.locator('.dhead .mobile-back').first.click()
        page.wait_for_function('!document.body.classList.contains("mobile-detail")')


def check_page(page, node, scoped, width):
    q = lambda local: uid(scoped, local)  # noqa: E731
    toggle = page.locator('#nest-toggle')
    assert toggle.is_visible(), '分层开关必须在顶栏可见'
    assert not page.evaluate('S.nest') and toggle.get_attribute('aria-pressed') == 'false'

    # 关着：平铺，没有子代理行，B 在自己的目录组里
    flat = rows(page)
    assert all(r['depth'] == 0 and r['agent'] is None and not r['caret'] for r in flat), flat
    assert [r['uid'] for r in flat] == [q(x) for x in ('grok:bbb', 'claude:aaa', 'codex:ccc',
                                                        'claude:ddd', 'claude:eee')], flat
    assert len({r['group'] for r in flat}) == 3, flat
    base_pad = flat[0]['pad']       # 平铺时图标离行左沿的距离

    # 开分层：A 活着，子代理 x 在跑、排最前；B 带着 C 从 beta 组搬到 A 下面；D、E 仍是根
    node.state['live'] = ['claude:aaa']            # 节点回本地 uid，经中央时由它加机器前缀
    page.evaluate('refreshLive(true)')
    page.wait_for_function('S.live.size === 1')
    toggle.click()
    assert page.evaluate('S.nest') and toggle.get_attribute('aria-pressed') == 'true'
    assert page.evaluate('JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k => k.endsWith("nest"))))') is True
    tree = rows(page)
    expect = [(q('claude:aaa'), None, 0), (q('claude:aaa'), 'x', 1), (q('grok:bbb'), None, 1),
              (q('codex:ccc'), None, 2), (q('claude:aaa'), 'y', 1),
              (q('claude:ddd'), None, 0), (q('claude:eee'), None, 0)]
    got = [(r['uid'] or r['key'].split('#')[0], r['agent'], r['depth']) for r in tree]
    assert got == expect, got
    assert len({r['group'] for r in tree}) == 2 and tree[0]['group'] == tree[5]['group'], tree
    assert [r['group'] for r in tree[:5]] == [tree[0]['group']] * 5, tree
    assert page.evaluate('(key) => [...document.querySelectorAll(".group")].find(g => g.dataset.key === key).querySelector(".gcount").textContent', tree[0]['group']) == '4'
    assert [r['caret'] for r in tree] == [True, False, True, False, False, False, False], tree
    assert tree[1]['dot'] and not tree[4]['dot'], '在跑的子代理带点，结束的不带'
    # 缩进：图标位置随深度递增、同深度对齐；根行的三角与分组标题的三角同一列，不能一前一后
    pads = [r['pad'] for r in tree]
    assert pads[0] > base_pad and pads[1] > pads[0] and pads[3] > pads[2] == pads[1] == pads[4], pads
    assert pads[5] == pads[6] == pads[0], pads
    assert all(r['pad'] < width / 3 for r in tree), pads
    assert abs(tree[0]['caretX'] - tree[0]['gheadCaretX']) < 1, (tree[0]['caretX'], tree[0]['gheadCaretX'])
    assert abs(tree[2]['caretX'] - (tree[0]['caretX'] + (pads[1] - pads[0]))) < 1, (tree[2]['caretX'], tree[0]['caretX'], pads)
    assert not [r for r in tree if r['sel']]

    # 点子代理行：打开它，只有它那一行亮
    page.locator('#side .item.agent[data-agent="y"]').click()
    page.wait_for_function('document.querySelector(".dhead h2")?.textContent.includes("view y")')
    assert page.evaluate('[S.sel, S.agent]') == [q('claude:aaa'), 'y']
    assert [r['key'] for r in rows(page) if r['sel']] == [q('claude:aaa') + '#y']

    # 打开孙辈 C：标题栏不再放发起者链接，左栏可以点回 B
    to_list(page)
    page.evaluate('(uid) => openSession(uid)', q('codex:ccc'))
    page.wait_for_function('document.querySelector(".dhead h2")?.textContent.includes("Grandchild C")')
    assert page.locator('.dhead .meta-spawner').count() == 0
    to_list(page)
    page.locator(f'#side .item[data-uid="{q("grok:bbb")}"]').click()
    page.wait_for_function('document.querySelector(".dhead h2")?.textContent.includes("Child B")')
    assert page.evaluate('[S.sel, S.agent]') == [q('grok:bbb'), None]

    # 三角：收起 A 的整棵子树（4 项），状态持久化；再点展开
    to_list(page)
    caret = page.locator(f'#side .item[data-uid="{q("claude:aaa")}"] .nest-caret')
    assert caret.get_attribute('aria-expanded') == 'true' and '4 项' in caret.get_attribute('title')
    caret.click()
    folded = rows(page)
    assert [r['depth'] for r in folded] == [0, 0, 0] and folded[0]['closed'], folded
    assert caret.get_attribute('aria-expanded') == 'false'
    assert page.evaluate('[...S.nestClosed]') == [q('claude:aaa')]
    page.reload()
    page.wait_for_function('S.sessions.length === 5')
    assert page.evaluate('S.nest') and [r['depth'] for r in rows(page)] == [0, 0, 0]
    page.locator(f'#side .item[data-uid="{q("claude:aaa")}"] .nest-caret').click()
    assert [r['depth'] for r in rows(page)] == [0, 1, 1, 2, 1, 0, 0]

    # 只看活跃：B 活着而 A 不活，B 没有可挂的父亲就当根
    node.state['live'] = ['grok:bbb']
    page.evaluate('refreshLive(true)')
    page.wait_for_function(f'S.live.has({q("grok:bbb")!r}) && S.live.size === 1')
    page.locator('#livecount').click()
    active = rows(page)
    assert [(r['uid'], r['depth']) for r in active] == [(q('grok:bbb'), 0)], active
    page.locator('#allcount').click()

    # 列表刷新只就地更新：树形不变时节点保留
    page.evaluate(f'document.querySelector(\'#side .item[data-uid="{q("codex:ccc")}"]\').__mark = 1')
    node.state['rows'][2]['title'] = 'Grandchild C renamed'
    node.state['rev'] += 1
    page.evaluate('pollSessions()')
    page.wait_for_function('S.sessions.some(s => s.title === "Grandchild C renamed")')
    assert page.evaluate(f'document.querySelector(\'#side .item[data-uid="{q("codex:ccc")}"]\').__mark') == 1
    # 此时 B 在跑而 A 不在：在跑的孩子排最前，子代理 x 失去运行态后按时间排到 y 之后
    assert [(r['key'], r['depth']) for r in rows(page)] == [
        (q('claude:aaa'), 0), (q('grok:bbb'), 1), (q('codex:ccc'), 2),
        (q('claude:aaa') + '#y', 1), (q('claude:aaa') + '#x', 1), (q('claude:ddd'), 0), (q('claude:eee'), 0)]
    node.state['rows'][2]['title'] = 'Grandchild C'
    node.state['rev'] += 1

    # compact/continue 只保留链上最新会话；旧文件不进左栏，点旧 uid 跟到新会话
    extra = [
        row('claude:old', 'claude', 'sid-old', 'Old continued', '/proj/alpha', today(12, 0),
            continued_in='claude:new'),
        row('claude:new', 'claude', 'sid-new', 'New continued', '/proj/alpha', today(12, 30),
            spawned_by={'source': 'claude', 'sid': 'sid-old'}),
    ]
    node.state['rows'] = [dict(r) for r in ROWS] + extra
    node.state['rev'] += 1
    page.evaluate('pollSessions()')
    page.wait_for_function('S.sessions.some(s => s.sid === "sid-new")')
    continued = rows(page)
    new_uid, old_uid = q('claude:new'), q('claude:old')
    listed = [r['uid'] for r in continued if not r['agent']]
    assert new_uid in listed and old_uid not in listed, listed
    assert next(r['depth'] for r in continued if r['uid'] == new_uid) == 0
    to_list(page)
    page.evaluate('(uid) => openSession(uid)', old_uid)
    page.wait_for_function(f'S.sel === {new_uid!r}')
    node.state['rows'] = [dict(r) for r in ROWS]
    node.state['rev'] += 1
    page.evaluate('pollSessions()')
    page.wait_for_function('S.sessions.length === 5')
    to_list(page)

    # 关掉分层回到平铺
    page.locator('#nest-toggle').click()
    back = rows(page)
    assert [r['uid'] for r in back] == [r['uid'] for r in flat] and all(r['depth'] == 0 for r in back)
    assert not page.locator('#side .item.agent').count()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'TreeNode')
        node.RequestHandlerClass = TreeNode
        node.state['rows'] = [dict(r) for r in ROWS]
        node.state['rev'] = 0
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'TreeNode', 'url': f'http://127.0.0.1:{node.server_port}',
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
                    for width, height in ((1400, 900), (390, 760)):
                        context = browser.new_context(viewport={'width': width, 'height': height},
                                                      color_scheme='dark' if scoped else 'light')
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(base)
                        page.wait_for_function('S.sessions.length === 5')
                        node.state['live'] = []
                        check_page(page, node, scoped, width)
                        assert not errors, errors
                        context.close()
                browser.close()
        finally:
            central.shutdown()
            central.server_close()
            stop(node)
    print('nest tree e2e ok')


if __name__ == '__main__':
    main()
