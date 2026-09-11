"""Sidebar divider drag on touch screens; isolated node and hub, no real CLI.

A phone held sideways (or a tablet) is wider than the 720px mobile breakpoint,
so it gets the desktop split layout with the ``#drag`` divider between the
session list and the detail pane. A finger on that divider produces
pointer/touch events only — the browser never synthesises ``mousemove`` for a
touch drag, and without ``touch-action: none`` it turns the gesture into a
scroll and fires ``pointercancel``. The divider must move under a real touch
drag, keep working with a mouse, and never leave the page stuck in the
dragging state.
"""
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

PHONE_LANDSCAPE = {'width': 814, 'height': 380}   # iPhone 15 Pro Max, CSS px
DESKTOP = {'width': 1280, 'height': 900}
PROBE = '''() => {
  const left = $('#left').getBoundingClientRect();
  const drag = $('#drag').getBoundingClientRect();
  return {left: left.width, drag: {x: drag.x, y: drag.y, w: drag.width, h: drag.height},
          dragging: document.body.classList.contains('dragging'),
          sideVar: getComputedStyle(document.documentElement).getPropertyValue('--side-width').trim(),
          stored: localStorage.getItem(STORAGE_PREFIX + 'width'),
          touchAction: getComputedStyle($('#drag')).touchAction,
          coarse: matchMedia('(pointer: coarse)').matches};
}'''


def touch_drag(page, x0, y0, x1, y1, steps=8, end=True):
    """Real touch input through CDP so the browser's own gesture handling runs."""
    cdp = page.context.new_cdp_session(page)
    point = lambda x, y: {'x': x, 'y': y, 'radiusX': 8, 'radiusY': 8, 'force': 1}
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [point(x0, y0)]})
    for i in range(1, steps + 1):
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [
            point(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)]})
        page.wait_for_timeout(20)
    if end:
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    else:
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchCancel', 'touchPoints': []})
    cdp.detach()


def open_first_session(page, base):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base)
    page.wait_for_function('S.sessions.length && T.listLoaded')
    page.evaluate('openSession(S.sessions[0].uid)')
    page.wait_for_selector('.dhead h2')
    page.evaluate('setSideWidth(340, true)')
    page.wait_for_timeout(100)
    return errors


def check_touch(browser, base):
    context = browser.new_context(viewport=PHONE_LANDSCAPE, has_touch=True, is_mobile=True,
                                  device_scale_factor=3)
    page = context.new_page()
    errors = open_first_session(page, base)
    before = page.evaluate(PROBE)
    assert before['drag']['w'] > 0 and before['left'] == 340, before
    cx = before['drag']['x'] + before['drag']['w'] / 2
    cy = before['drag']['y'] + before['drag']['h'] / 2

    # A finger dragging the divider 120px to the right widens the list by 120px.
    touch_drag(page, cx, cy, cx + 120, cy)
    page.wait_for_timeout(150)
    after = page.evaluate(PROBE)
    assert abs(after['left'] - 460) < 3, (before, after)
    assert after['sideVar'] == f"{round(after['left'])}px" and after['stored'] == str(round(after['left'])), after
    assert not after['dragging'], after
    assert after['touchAction'] == 'none', after

    # Fingers are wider than 5px: on a coarse pointer the hit zone extends a few
    # px to either side without changing the visible divider.
    if before['coarse']:
        hit = page.evaluate('''([x, y]) => [x - 4, x + 4].map(px =>
          document.elementFromPoint(px, y)?.id)''', [cx + 120, cy])
        assert hit == ['drag', 'drag'], hit
        assert page.evaluate(PROBE)['drag']['w'] == before['drag']['w']
        touch_drag(page, cx + 120 + 4, cy, cx + 40, cy)
        page.wait_for_timeout(150)
        assert abs(page.evaluate(PROBE)['left'] - 380) < 3, page.evaluate(PROBE)
        page.evaluate('setSideWidth(460, true)')

    # A cancelled touch (the OS taking over the gesture) must not leave the
    # page stuck in the dragging state with the divider following nothing.
    touch_drag(page, cx + 120, cy, cx + 60, cy, end=False)
    page.wait_for_timeout(150)
    cancelled = page.evaluate(PROBE)
    assert not cancelled['dragging'], cancelled
    assert abs(cancelled['left'] - 400) < 3, cancelled
    touch_drag(page, cx + 200, cy + 20, cx + 200, cy - 20)
    page.wait_for_timeout(100)
    assert abs(page.evaluate(PROBE)['left'] - 400) < 3, page.evaluate(PROBE)

    # Dragging must not scroll the session list underneath.
    page.evaluate('$("#side").scrollTop = 0')
    touch_drag(page, cx + 60, cy + 60, cx + 60, cy - 60)
    page.wait_for_timeout(100)
    assert page.evaluate('$("#side").scrollTop') == 0
    assert not errors, errors
    context.close()


def check_mouse(browser, base):
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    errors = open_first_session(page, base)
    before = page.evaluate(PROBE)
    cx = before['drag']['x'] + before['drag']['w'] / 2
    cy = before['drag']['y'] + 200
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 130, cy, steps=6)
    mid = page.evaluate(PROBE)
    assert mid['dragging'] and abs(mid['left'] - 470) < 3, mid
    page.mouse.up()
    after = page.evaluate(PROBE)
    assert not after['dragging'] and abs(after['left'] - 470) < 3, after
    assert after['stored'] == str(round(after['left'])), after
    assert page.evaluate('getSelection().toString()') == ''
    page.dblclick('#drag')
    page.wait_for_timeout(100)
    assert page.evaluate(PROBE)['left'] == 340
    # Coarse-pointer hit zone must not exist for a mouse: 5px is the whole target.
    fine = page.evaluate('''([x, y]) => [x - 6, x + 6].map(px =>
      document.elementFromPoint(px, y)?.id)''', [cx, cy])
    assert 'drag' not in fine, fine
    assert not errors, errors
    context.close()


def main():
    with tempfile.TemporaryDirectory() as root:
        node = start_node('a' * 32, 'NodeA')
        registry = hub.Registry(Path(root) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'NodeA', 'url': f'http://127.0.0.1:{node.server_port}',
                           'token': node.state['token']})
        central = ThreadingHTTPServer(('127.0.0.1', 0), MountedHub)
        central.daemon_threads = True; central.registry = registry; central.hub_mode = True
        server.ALLOWED_IPS.add('127.0.0.1')
        threading.Thread(target=central.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for base in [f'http://127.0.0.1:{node.server_port}/',
                             f'http://127.0.0.1:{central.server_port}/agenthub/']:
                    check_touch(browser, base)
                    check_mouse(browser, base)
                browser.close()
        finally:
            central.shutdown(); central.server_close()
            stop(node)
    print('side_drag_e2e ok')


if __name__ == '__main__':
    main()
