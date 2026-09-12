"""顶栏与会话标题栏在连续 N 个宽度上的折叠顺序：只在放不下时折，先折最不重要的。

隔离 hub + 模拟节点，不起 CLI、tmux 或真实会话。宽度从 1698 逐步降到 320（含 608 与各断点两侧），
每个宽度都核对两条不变量，再核对整段扫描里折起的先后顺序：
  顶栏：折起的一定是优先级末尾连续几个（设置 → 报告 → 回收站 → 重新扫描 → 新建），
        菜单保持平铺顺序，筛选条不被挤压（除非五个都折了），折了就再放一个也放不下，
        全平铺时 ⋯ 不占位；同一档位内宽度越窄折得只多不少；
        三档顶栏同高，跨过 1200px、720px 都不跳高。
  标题栏：平铺的一定是「操作（星标 → 折叠过程 → 报告 → 停止/删除）→ 元信息（消息数、大小、时间、
        机器、目录、来源、模型、会话号、分支）」这条优先级的前缀，放不下的从末尾起进 ⋯（元信息先折、
        按钮后折）；菜单空了 ⋯ 不显示；窄屏标题不让位；同一档位内宽度越窄平铺得只少不多。
拖分割线只改详情区宽度，标题栏按同一顺序进出，宽屏与中屏各扫一遍。
"""
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from agenthub import hub, server
from hub_e2e import HEADER_ACTIONS, HEADER_FOLD_JS, MountedHub
from hub_fixture import start_node, stop
from session_menu_e2e import ACTION_ORDER, MenuNode, tier_of

# 元信息的重要程度就是它的固定顺序；模拟会话补上模型，八项齐全（git 分支不再显示）
META_ORDER = ['mcount-total', 'size', 'time', 'meta-node', 'cwd', 'meta-source', 'model', 'session-id']
META_KEY = '''e => e.id === 'mcount-total' ? e.id
  : e.classList.contains('session-id') ? 'session-id'
  : e.classList.contains('meta-node') ? 'meta-node'
  : e.classList.contains('meta-source') ? 'meta-source'
  : e.querySelector('code') ? 'cwd' : e.textContent.includes('→') ? 'time'
  : /^[0-9.]+[BKM]$/.test(e.textContent.trim()) ? 'size' : 'model' '''
HEAD_STATE_JS = f'''() => {{
  const id = b => b.id || (b.hasAttribute('data-report-bug') ? 'report-bug' : '');
  const key = {META_KEY};
  const h2 = document.querySelector('.dhead h2'), brief = document.querySelector('.dbrief');
  const actions = document.querySelector('.dhead-actions'), more = document.querySelector('#a-more');
  const last = brief && !brief.hidden ? brief : h2;
  const text = h2.querySelector('.session-view-switch > span, :scope > span');
  return {{
    inline: [...actions.querySelectorAll('button')].filter(b => !b.hidden && b.offsetWidth).map(id),
    menu_actions: [...document.querySelectorAll('#session-actions-menu [role="menu"] > *')].map(id),
    brief: [...document.querySelectorAll('.dbrief > *')].map(key),
    menu_meta: [...document.querySelectorAll('#session-actions-menu .dmeta > *')].map(key),
    more: !!more && !more.hidden && more.offsetWidth > 0,
    free: actions.getBoundingClientRect().left - last.getBoundingClientRect().right,
    gap: parseFloat(getComputedStyle(document.querySelector('.dtitle')).columnGap),
    brief_gap: brief ? parseFloat(getComputedStyle(brief).columnGap) : 0,
    action_gap: parseFloat(getComputedStyle(actions).columnGap),
    title_clipped: text.scrollWidth > text.clientWidth + 1,
    height: document.querySelector('.dhead').offsetHeight,
    overflow: document.documentElement.scrollWidth > innerWidth,
    detail: document.querySelector('#detail').clientWidth,
  }};
}}'''
FIRST_MENU_WIDTH_JS = '''() => {
  const menu = document.querySelector('#session-actions-menu');
  const was = menu.hidden; menu.hidden = false;
  const item = document.querySelector('#session-actions-menu [role="menu"] > *')
    || document.querySelector('#session-actions-menu .dmeta > *');
  const width = item ? item.getBoundingClientRect().width : 0;
  menu.hidden = was;
  return width;
}'''
SLACK = 24   # app.js HEAD_BRIEF_SLACK：给消息数变宽留的余量


def settle(page):
    # headless 只有渲染一帧后才派发媒体查询 change 和 ResizeObserver，折叠布局靠它们驱动
    page.evaluate('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
    page.wait_for_timeout(40)


def sweep_widths():
    widths = set(range(320, 1501, 10)) | {608, 720, 721, 1199, 1200, 1698}
    return sorted(widths, reverse=True)


def check_header(page, width, tiers):
    """一个宽度上的顶栏不变量；tiers 记录各档位上一次的折叠数用于单调性。"""
    fold = page.evaluate(HEADER_FOLD_JS)
    where = f'header@{width}'
    assert fold['inline'] + fold['menu'] == HEADER_ACTIONS, (where, fold)
    assert fold['more'] == bool(fold['menu']), (where, fold)
    if fold['menu'] != HEADER_ACTIONS:
        assert not fold['squeezed'], (where, fold)
    if fold['menu']:
        assert fold['free'] < fold['unit'], (where, fold)
    else:
        assert not page.locator('#header-more-btn').is_visible(), where
    fold['height'] = page.locator('header').bounding_box()['height']
    assert fold['height'] <= 52, where
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), where
    tier = tier_of(width)
    folded = len(fold['menu'])
    assert folded >= tiers.get(tier, 0), (where, 'unfolded while narrowing', fold, tiers)
    tiers[tier] = folded
    return fold


def head_priority(tier, meta_order):
    """标题栏一行上的重要程度：先操作（菜单顺序），后元信息；各档位相同。"""
    return ACTION_ORDER + meta_order


def check_head(page, width, tier, tiers, key, meta_order):
    """一个宽度（或分割线位置）上的标题栏不变量；tiers 按档位记录平铺项数用于单调性。"""
    state = page.evaluate(HEAD_STATE_JS)
    where = f'{key}@{width}'
    priority = head_priority(tier, meta_order)
    inline_actions = [i for i in state['inline'] if i not in ('a-term', 'a-more')]
    placed = inline_actions + state['brief']
    assert placed == priority[:len(placed)], (where, placed, priority, state)
    assert state['brief'] + state['menu_meta'] == meta_order, (where, state)
    assert inline_actions + state['menu_actions'] == ACTION_ORDER, (where, state)
    remaining = bool(state['menu_meta'] or state['menu_actions'])
    assert state['more'] == remaining, (where, state)
    assert ('a-more' in state['inline']) == remaining, (where, state)
    assert state['height'] <= 46 and not state['overflow'], (where, state)
    if tier == 'narrow':
        assert not state['title_clipped'], (where, '窄屏标题不为元信息让位', state)
    if remaining:
        # 菜单里的第一项确实放不下：剩余空位小于它的宽度加上间距和给消息数变宽留的余量
        width_next = page.evaluate(FIRST_MENU_WIDTH_JS)
        gap = state['action_gap'] if state['menu_actions'] else state['brief_gap']
        assert state['free'] < width_next + gap + state['gap'] * 2 + SLACK, (where, state, width_next)
    n = len(placed)
    assert n <= tiers.get(key + tier, n), (where, 'unfolded while narrowing', state, tiers)
    tiers[key + tier] = n
    return state


def fold_events(rows, priority_of):
    """按扫描顺序（宽→窄）列出每次新折起的项，用来核对先折的确实是更不重要的。"""
    events = []
    previous = None
    for width, folded, tier in rows:
        if previous is not None and previous[1] == tier:
            newly = [i for i in folded if i not in previous[0]]
            for item in newly:
                events.append((width, tier, item))
        previous = (folded, tier)
    for width, tier, item in events:
        order = priority_of(tier)
        later = [i for i in order if order.index(i) > order.index(item)]
        # 折起 item 时，比它更不重要的一定已经折了
        state = next(f for w, f, t in rows if w == width and t == tier)
        assert all(i in state for i in later), (width, tier, item, state)
    return events


def main():
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'Sweep')
        node.RequestHandlerClass = MenuNode
        node.state['row'].update({'model': 'claude-opus-5', 'branch': 'feat/fold-sweep'})   # branch 是 API 字段，标题栏不再显示
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'Sweep', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        # 再挂三台长名字的机器：宽屏机器 chips 平铺，1200–1400px 顶栏就真的放不下，宽屏档也折得起来
        extra = []
        for i, name in enumerate(['Workstation-Alpha', 'Workstation-Beta', 'Workstation-Gamma']):
            other = start_node(chr(98 + i) * 32, name)
            other.state['token'] = (name.split('-')[1].lower() + '0123456789abcdef') * 2
            other.state['deleted'] = True   # 不贡献会话，只占筛选条宽度
            registry.register({'name': name, 'url': f'http://127.0.0.1:{other.server_port}',
                               'token': other.state['token']})
            extra.append(other)
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True
        central.registry = registry
        central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for scoped in [True, False]:
                    base = (f'http://127.0.0.1:{central.server_port}/agenthub/' if scoped
                            else f'http://127.0.0.1:{node.server_port}/')
                    uid = 'claude:' + ('a' * 32 + '~' if scoped else '') + 'same-file-hash'
                    context = browser.new_context(viewport={'width': 1698, 'height': 900})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(base)
                    page.wait_for_function('S.sessions.length && T.enabled')
                    if scoped:
                        page.wait_for_function('Nodes.list.length === 4')
                    page.wait_for_function('document.querySelector("#new-session") && !document.querySelector("#new-session").classList.contains("hidden")')
                    page.evaluate('(uid) => openSession(uid)', uid)
                    page.wait_for_function('document.querySelector("#msgs")?.textContent.includes("reply Sweep")')
                    page.evaluate('setSideWidth(340, true)')
                    meta_order = [k for k in META_ORDER if scoped or k != 'meta-node']   # 机器徽章只有中央站才有
                    priority_of = lambda tier: head_priority(tier, meta_order)
                    header_rows, head_rows, header_heights = [], [], []
                    header_tiers, head_tiers = {}, {}
                    for width in sweep_widths():
                        page.set_viewport_size({'width': width, 'height': 900})
                        settle(page)
                        tier = tier_of(width)
                        if width <= 720:
                            page.evaluate('showMobileList()')
                            settle(page)
                        fold = check_header(page, width, header_tiers)
                        header_rows.append((width, fold['menu'], tier))
                        header_heights.append((width, fold['height'], tier))
                        if width <= 720:
                            page.evaluate('showMobileDetail()')
                            settle(page)
                        state = check_head(page, width, tier, head_tiers, 'viewport', meta_order)
                        head_rows.append((width, state['menu_meta'] + state['menu_actions'], tier))
                    header_events = fold_events(header_rows, lambda tier: HEADER_ACTIONS)
                    head_events = fold_events(head_rows, priority_of)
                    # 608px（报告里的平板宽度）五个顶栏按钮必须全平铺
                    assert next(f for w, f, t in header_rows if w == 608) == [], header_rows
                    # 三档顶栏同高：713px 与 765px、1698px 与 713px 的截图都不能差出一截
                    assert len({h for w, h, t in header_heights}) == 1, header_heights
                    # 扫描要真的经过折叠与全平铺两种状态：窄屏一定折；中央站四台机器的 chips 让宽屏也折
                    # （中屏机器收成下拉、来源只剩图标，五个按钮放得下是对的）；每个档位都要有全平铺的宽度
                    for tier in ['narrow'] + (['wide'] if scoped else []):
                        assert any(t == tier and f for w, f, t in header_rows), (tier, header_rows)
                    for tier in ['narrow', 'medium', 'wide']:
                        assert any(t == tier and not f for w, f, t in header_rows), (tier, header_rows)
                    assert header_events, header_rows
                    assert any(f for w, f, t in head_rows) and any(not f for w, f, t in head_rows), head_rows
                    assert head_events, head_rows
                    # 拖分割线：宽屏和中屏各从最宽的详情区一路收窄
                    drag_rows = []
                    for width in [1698, 1100]:
                        page.set_viewport_size({'width': width, 'height': 900})
                        settle(page)
                        tier = tier_of(width)
                        drag_tiers = {}
                        for side in range(200, width - 320, 20):
                            page.evaluate('(w) => setSideWidth(w, true)', side)
                            settle(page)
                            state = check_head(page, side, tier, drag_tiers, f'drag{width}', meta_order)
                            drag_rows.append((width - side, state['menu_meta'] + state['menu_actions'], tier))
                        page.evaluate('setSideWidth(340, true)')
                    drag_events = fold_events(drag_rows, priority_of)
                    assert drag_events, drag_rows
                    assert not errors, errors
                    context.close()
                    label = 'Hub' if scoped else 'Node'
                    print(f'{label}: {len(header_rows)} widths, header folds '
                          + ', '.join(f'{item}≤{w}' for w, t, item in header_events), flush=True)
                    print(f'{label}: title bar folds by viewport '
                          + ', '.join(f'{item}≤{w}' for w, t, item in head_events), flush=True)
                    print(f'{label}: title bar folds by drag '
                          + ', '.join(f'{item}≤{w}' for w, t, item in drag_events), flush=True)
                browser.close()
        finally:
            stop(central)
            for item in [node, *extra]:
                stop(item)
    print('PASS: fold order over N widths for the header and the title bar')


if __name__ == '__main__':
    main()
