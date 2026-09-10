"""Timeline directory identity and responsive rendering; isolated nodes, no CLI."""
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


class DirectoryNode(NodeHandler):
    def do_GET(self):
        if urlparse(self.path).path == '/api/sessions':
            return self._json({'sessions': self.state['rows'], 'sig': 'directories'})
        if urlparse(self.path).path == '/api/term/list':
            return self._json({'enabled': False, 'sessions': [], 'pending': []})
        return super().do_GET()


def main():
    paths = [
        '/srv/common-workspace/platform-alpha/build/cache/output/report',
        '/srv/common-workspace/platform-beta/build/cache/output/report',
        '/srv/common-workspace/platform-alpha/build/cache/output/summary',
        '/srv/common-workspace/platform-beta/build/cache/output/notes',
        '/home/example/projects/中文目录-<script>&很长的末级目录名/',
        '/', '/home/example', '',
    ]
    with tempfile.TemporaryDirectory() as tmp:
        node = start_node('a' * 32, 'Lyra')
        node.RequestHandlerClass = DirectoryNode
        node.state['rows'] = [{**node.state['row'], 'uid': f'claude:directory-{i}',
                               'sid': f'directory-{i}', 'title': f'Directory {i}', 'cwd': cwd}
                              for i, cwd in enumerate(paths)]
        registry = hub.Registry(Path(tmp) / 'nodes.json', ['127.0.0.0/8'])
        registry.register({'name': 'Lyra', 'url': f'http://127.0.0.1:{node.server_port}',
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
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(base)
                    page.wait_for_function('S.sessions.length === 8')
                    page.locator('#view [data-v="date"]').click()
                    alpha = page.locator('.item').filter(has=page.locator('.t', has_text='Directory 0')).locator('.cwd')
                    beta = page.locator('.item').filter(has=page.locator('.t', has_text='Directory 1')).locator('.cwd')
                    page.wait_for_function('document.querySelector(\'.item[data-uid$="directory-0"] .cwd-path\').textContent.includes("…")')
                    label = alpha.locator('.cwd-path').inner_text()
                    assert 'platform-alpha' in label and label.endswith('/report'), label
                    assert 'platform-beta' in beta.inner_text() and alpha.inner_text() != beta.inner_text()
                    assert alpha.get_attribute('title') == paths[0]
                    assert alpha.locator('.node-badge').count() == int(scoped)
                    assert sorted(page.locator('.cwd-leaf').all_text_contents()) == sorted([
                        'report', 'report', 'summary', 'notes',
                        '中文目录-<script>&很长的末级目录名', '/', '~', '(未知)'])
                    assert page.locator('.cwd script').count() == 0
                    for theme in ['light', 'dark']:
                        page.evaluate('(theme) => applyTheme(theme)', theme)
                        assert alpha.locator('.cwd-leaf').evaluate('''e =>
                          getComputedStyle(e).color === getComputedStyle(e.parentElement).color &&
                          getComputedStyle(e).fontWeight === getComputedStyle(e.parentElement).fontWeight''')
                        assert alpha.locator('.cwd-path').evaluate('''e =>
                          getComputedStyle(e).color === getComputedStyle(e.parentElement).color''')

                    # Filtering and duplicate sessions must not change the global plan.
                    page.evaluate('''S.sessions.sort((a, b) => a.title.localeCompare(b.title));
                      S.term = "Directory 0"; renderSide()''')
                    assert page.locator('.cwd-path').inner_text() == label
                    page.evaluate('''() => {
                      S.results = [S.sessions[0]]; S.term = '';
                      S.sessions.push(...Array.from({length: 30}, (_, i) =>
                        ({...S.sessions[2], uid: 'claude:duplicate-' + i})));
                      renderSide();
                    }''')
                    assert page.locator('.cwd-path').inner_text() == label

                    # Polling must recompute when a hidden peer adds a distinction,
                    # even when the visible row and date group stay the same.
                    page.evaluate('''() => {
                      window.originalCwd = S.sessions[0].cwd;
                      window.originalItem = document.querySelector('.item');
                      S.sessions.push({...S.sessions[0], uid:'claude:new-peer',
                        cwd:S.sessions[0].cwd.replace('/output/', '/different-output/')});
                      if (!patchSide(visible())) throw new Error('expected an in-place patch');
                    }''')
                    assert page.evaluate('document.querySelector(".item") === window.originalItem')
                    assert '/output/' in page.locator('.cwd-path').inner_text()
                    page.evaluate('''() => {
                      S.sessions[0].cwd = '/srv/updated/project';
                      S.results = [S.sessions[0]]; patchSide(visible());
                    }''')
                    assert page.locator('.cwd-leaf').inner_text() == 'project'
                    assert page.locator('.cwd').get_attribute('title') == '/srv/updated/project'
                    page.evaluate('''() => {
                      S.sessions[0].cwd = window.originalCwd;
                      S.sessions = S.sessions.slice(0, 8); S.results = null; renderSide();
                    }''')

                    # Resize and closed-group reopening use the actual available width.
                    page.evaluate('setSideWidth(1000)')
                    page.wait_for_function('(path) => document.querySelector(\'.item[data-uid$="directory-0"] .cwd-path\').textContent === path', arg=paths[0])
                    page.locator('.ghead').first.click()
                    page.evaluate('setSideWidth(240)')
                    page.locator('.ghead').first.click()
                    page.wait_for_function('document.querySelector(\'.item[data-uid$="directory-0"] .cwd-path\').textContent.includes("…")')
                    for width in [1440, 720, 390]:
                        page.set_viewport_size({'width': width, 'height': 1000})
                        page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                        assert page.locator('.cwd').evaluate_all('''rows => rows.every(e =>
                          e.scrollWidth <= e.clientWidth + 1 &&
                          e.querySelector('.cwd-leaf').getBoundingClientRect().right <= e.getBoundingClientRect().right + 1)''')
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.locator('#left').screenshot(path=f'/tmp/agenthub-timeline-fixture-{scoped}.png')
                    page.locator('#view [data-v="tree"]').click()
                    assert page.locator('.cwd-leaf').count() == 0
                    assert '/srv/common-workspace/platform-alpha/build/cache/output/report' in ' '.join(page.locator('.gname').all_text_contents())
                    # More common directories than available colors, plus a singleton.
                    original_rows = node.state['rows']
                    color_paths = ['/srv/team-alpha/report', '/srv/team-beta/report'] + [
                        f'/srv/colors/directory-{i}' for i in range(2, 10)]
                    node.state['rows'] = [
                        {**node.state['row'], 'uid': f'claude:color-{i}-{j}',
                         'sid': f'color-{i}-{j}', 'title': f'Color {i} session {j}', 'cwd': cwd}
                        for i, cwd in enumerate(color_paths) for j in range(12 - i)
                    ] + [{**node.state['row'], 'uid': 'claude:rare', 'title': 'Rare directory',
                          'cwd': '/srv/colors/one-off'}]
                    page.evaluate('loadSessions()')
                    page.locator('#view [data-v="date"]').click()
                    assigned = page.evaluate('Object.fromEntries(timelineColors)')
                    assert set(assigned) == set(color_paths[:8]), assigned
                    assert len(set(assigned.values())) == 8, assigned
                    assert assigned[color_paths[0]] != assigned[color_paths[1]]
                    for theme in ['light', 'dark']:
                        page.evaluate('(theme) => applyTheme(theme)', theme)
                        rendered = page.locator('.cwd-path').evaluate_all('''rows => rows.map(e => {
                          const s = getComputedStyle(e), leaf = getComputedStyle(e.querySelector('.cwd-leaf'));
                          return {path:e.dataset.path,slot:e.dataset.cwdColor,color:s.color,
                            muted:getComputedStyle(e.parentElement).color,uniform:s.color === leaf.color && s.fontWeight === leaf.fontWeight};
                        })''')
                        assert all(row['uniform'] for row in rendered)
                        assert all((row['color'] != row['muted']) == bool(row['slot']) for row in rendered)
                        assert all(row['slot'] == assigned.get(row['path'], '') for row in rendered)
                        assert len({row['color'] for row in rendered if row['slot']}) == 8
                        page.locator('#left').screenshot(path=f'/tmp/agenthub-directory-colors-{scoped}-{theme}.png')
                    # A filtered/search view cannot promote a rare directory or
                    # double-count search results. Existing slots survive reordering.
                    page.evaluate('''() => {
                      S.term = 'Rare directory'; renderSide();
                      S.results = [...S.sessions]; S.term = ''; renderSide();
                    }''')
                    assert page.evaluate('Object.fromEntries(timelineColors)') == assigned
                    page.evaluate('''() => {
                      S.results = null;
                      const row = S.sessions.find(s => s.cwd === '/srv/team-beta/report');
                      S.sessions.push(...Array.from({length:20}, (_, i) => ({...row, uid:'claude:extra-' + i})));
                      patchSide(visible()) || renderSide();
                    }''')
                    assert page.evaluate('Object.fromEntries(timelineColors)') == assigned
                    page.reload()
                    page.wait_for_function('S.sessions.length > 8 && document.querySelector(".cwd-path")')
                    assert page.evaluate('Object.fromEntries(timelineColors)') == assigned
                    # A newcomer reuses the departed slot without recoloring peers.
                    page.evaluate('''() => {
                      S.sessions = S.sessions.filter(s => s.cwd !== '/srv/team-alpha/report');
                      renderSide();
                    }''')
                    changed = page.evaluate('Object.fromEntries(timelineColors)')
                    assert color_paths[0] not in changed and color_paths[8] in changed
                    assert all(changed[path] == color for path, color in assigned.items() if path != color_paths[0])
                    assert len(set(changed.values())) == 8
                    node.state['rows'] = original_rows
                    assert not errors, errors
                    context.close()
                browser.close()
            print('PASS: global path distinction, whole-path palette, gray fallback, stable assignments, filters, reload, duplicate sessions, polling, resize, mobile, escaping, standalone and hub')
        finally:
            for srv in [central, node]:
                stop(srv)


if __name__ == '__main__':
    main()
