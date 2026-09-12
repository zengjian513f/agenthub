"""PTY colors survive WebSocket batching and theme changes; no CLI is launched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from hub_fixture import start_node, stop


# Light Codex diffs must stay native (BUG-20260911-213006-ddc5ef). Dark Claude
# user prompts, indexed dark greens and SGR 40/OSC 11 black must lighten only
# in the light web theme (BUG-20260912-125351-a8c06b). Literal SGR-like text
# is not a CSI sequence and must not be rewritten.
SCREEN = (
    '\x1b[0m\x1b[H\x1b[2J'
    '\x1b[48;2;218;251;225;38;2;76;79;105m+ added\x1b[0m\r\n'
    '\x1b[48;2;255;235;233;38;2;31;35;40m- removed\x1b[0m\r\n'
    '\x1b[48;2;234;236;238;38;2;37;42;50m> prompt\x1b[0m\r\n'
    '\x1b[48;2;3;48;14;38;2;255;123;114mdark\x1b[0m\r\n'
    '\x1b[48;5;22;38;5;231mindexed\x1b[0m\r\n'
    '\x1b[48:2::218:251:225m\x1b[38:2::76:79:105mcolon\x1b[0m\r\n'
    'literal 38;2;255;123;114 and 48;5;22\r\n'
    '\x1b[31mred\x1b[0m default\r\n'
    '\x1b[48;2;55;55;55;38;2;255;255;255muser\x1b[0m\r\n'
    '\x1b[40mblackbg\x1b[0m\r\n'
    '\x1b]11;#000000\x07'
)
LIGHT_NATIVE = [(0, 0xDAFBE1, 0x4C4F69), (1, 0xFFEBE9, 0x1F2328),
                (2, 0xEAECEE, 0x252A32), (5, 0xDAFBE1, 0x4C4F69)]
LIGHT_REMAPPED = [(3, 0xC1FBCF, 0xA90B00), (8, 0xBBBBBB, 0x000000)]
DARK_NATIVE = [(0, 0xDAFBE1, 0x4C4F69), (1, 0xFFEBE9, 0x1F2328),
               (2, 0xEAECEE, 0x252A32), (3, 0x03300E, 0xFF7B72),
               (5, 0xDAFBE1, 0x4C4F69), (8, 0x373737, 0xFFFFFF)]


def main():
    node = start_node('a' * 32, 'NodeA')
    node.state['term_sessions'] = [{
        'name': 'color-terminal', 'uid': node.state['row']['uid'],
        'created': 1, 'attached': False, 'pid': 1, 'cwd': '/same/project',
        'cmd': 'sh', 'cols': 80, 'rows': 24, 'owned': True,
        'server': 'ptyhost', 'backend': 'ptyhost',
    }]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1249, 'height': 1105},
                                    color_scheme='light')
            errors, sockets = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route_web_socket('**/api/term/attach*', lambda ws: sockets.append(ws))
            page.goto(f'http://127.0.0.1:{node.server_port}/')
            page.wait_for_function('typeof T !== "undefined" && T.listLoaded && S.sessions.length')
            page.evaluate('openSession(S.sessions[0].uid)')
            page.locator('#a-term').click()
            page.wait_for_function('T.ws?.readyState === WebSocket.OPEN')

            for theme in ['light', 'dark', 'light']:
                previous = len(sockets)
                page.evaluate('(theme) => applyTheme(theme, true)', theme)
                page.wait_for_function('T.ws?.readyState === WebSocket.OPEN')
                assert len(sockets) > previous, 'theme change must replay from source'
                for fragmented in [False, True]:
                    # Separate browser writes exercise xterm's own streaming parser,
                    # including a chunk ending with ESC and one inside a color number.
                    cuts = ([0, 1, 17, 25, 44, 180, 249, 346, 400, len(SCREEN)]
                            if fragmented else [0, len(SCREEN)])
                    for start, end in zip(cuts, cuts[1:]):
                        sockets[-1].send(SCREEN[start:end].encode())
                        page.wait_for_timeout(45)
                    page.wait_for_function(
                        'T.term.buffer.active.getLine(9)?.translateToString(true).includes("blackbg")')
                    actual = page.evaluate('''() => {
                        const b = T.term.buffer.active;
                        const rows = Array.from({length: 10}, (_, y) => {
                            const line = b.getLine(y), c = line.getCell(0);
                            return {text: line.translateToString(true), fg: c.getFgColor(),
                                bg: c.getBgColor(), rgb: !!c.isBgRGB(),
                                palette: !!c.isFgPalette()};
                        });
                        return {rows, theme: T.term.options.theme,
                            filter: getComputedStyle(document.querySelector('#xterm')).filter};
                    }''')
                    expected_rgb = LIGHT_NATIVE + LIGHT_REMAPPED if theme == 'light' else DARK_NATIVE
                    for y, bg, fg in expected_rgb:
                        row = actual['rows'][y]
                        assert (row['bg'], row['fg'], row['rgb']) == (bg, fg, True), (
                            theme, fragmented, y, row)
                    if theme == 'light':
                        row = actual['rows'][4]
                        assert (row['bg'], row['fg'], row['rgb']) == (0x88FF88, 0x000000, True), row
                        assert actual['rows'][9]['bg'] == 0xC3CAD1 and actual['rows'][9]['rgb']
                    else:
                        assert actual['rows'][4]['bg'] == 22 and actual['rows'][4]['fg'] == 231
                        assert actual['rows'][4]['palette']
                        assert actual['rows'][9]['bg'] == 0 and not actual['rows'][9]['rgb']
                    assert actual['rows'][6]['text'] == 'literal 38;2;255;123;114 and 48;5;22'
                    assert actual['rows'][7]['fg'] == 1 and actual['rows'][7]['palette']
                    expected_bg, expected_red = (('#f4f6f8', '#a8323b') if theme == 'light'
                                                else ('#000000', '#ff7b72'))
                    assert actual['theme']['background'] == expected_bg
                    assert actual['theme']['red'] == expected_red
                    assert actual['filter'] == 'none'
                    assert page.locator('#a-term').is_enabled()
                print(f'PASS: {theme}, whole/split PTY frames, RGB/indexed/default palette, literal text')
            assert not errors, errors
            browser.close()
    finally:
        stop(node)


if __name__ == '__main__':
    main()
