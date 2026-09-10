"""Native mouse selection/copy and disclosure controls; isolated server, no CLI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from hub_fixture import start_node, stop


# Use glyph geometry for native drags, including highlighted code with nested spans.
POINTS = """async e => {
  const settled = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  await settled();
  e.scrollIntoView({block:'center'});
  await settled();
  getSelection().removeAllRanges();
  const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
  let text;
  while (text = walker.nextNode()) {
    if (text.textContent.trim().length < 2) continue;
    const range = document.createRange();
    range.setStart(text, 0); range.setEnd(text, 1);
    const first = range.getBoundingClientRect();
    range.setStart(text, Math.min(text.length - 1, 4));
    range.setEnd(text, Math.min(text.length, 5));
    const last = range.getBoundingClientRect();
    if (first.width && first.top === last.top) return {
      x:first.left + 1, y:first.top + first.height / 2, end:last.right - 1};
  }
  throw new Error('No visible text to select');
}"""


def select_and_copy(page, locator):
    points = locator.evaluate(POINTS)
    page.mouse.move(points['x'], points['y'])
    page.mouse.down()
    page.mouse.move(points['end'], points['y'], steps=12)
    page.mouse.up()
    selected = page.evaluate('getSelection().toString()')
    assert selected.strip(), (locator.evaluate('e => e.outerHTML')[:400], points, page.evaluate('(p)=>document.elementFromPoint(p.x,p.y)?.outerHTML',points))
    page.keyboard.press('Control+c')
    assert page.evaluate('navigator.clipboard.readText()') == selected
    # Double/triple clicks must select words/lines without hiding the content.
    for clicks in [2, 3]:
        page.mouse.click(points['x'] + 2, points['y'], click_count=clicks)
        assert page.evaluate('getSelection().toString().trim().length > 0')
        assert locator.is_visible()
    return selected


def seed(page):
    page.evaluate("""() => {
      const box = document.querySelector('#msgs');
      const tool = {role:'tool', name:'exec', summary:'$ printf selection_probe',
        text:'command: printf selection_probe\\nworking_directory: /tmp/example',
        result:{role:'tool_result', text:'selection_probe output\\nsecond output line', exit_code:0}};
      const nodes = ['user','assistant','thinking','system','command','answer'].map(role => {
        const node = msgNode({role, text:role + ' selection_probe content'});
        node.dataset.probe = role; return node;
      });
      const single = msgNode(tool); single.dataset.probe = 'tool'; nodes.push(single);
      const orphan = msgNode({role:'tool_result', text:'orphan selection_probe output'});
      orphan.dataset.probe = 'orphan'; nodes.push(orphan);
      const group = groupNode([tool, {...tool, summary:'$ printf another_probe'}]);
      group.dataset.probe = 'group'; nodes.push(group);
      const turn = turnProcessNode({items:[{role:'assistant', text:'progress selection_probe'}, tool],
        hasConclusion:false});
      turn.dataset.probe = 'turn'; nodes.push(turn);
      const long = msgNode({role:'assistant', text:Array(100).fill('Long selection_probe line with enough text for clipping.').join('\\n\\n')});
      long.dataset.probe = 'long'; nodes.push(long);
      const rich = msgNode({role:'assistant', text:'```python\\nprint("selection_probe")\\n```\\n\\n| Column heading | Other heading |\\n| --- | --- |\\n| table selection_probe | value selection_probe |'});
      rich.dataset.probe = 'rich'; nodes.push(rich);
      const diff = fileChangeNode({role:'tool', changes:[{path:'/tmp/example.py', operation:'update',
        patch:'--- a/example.py\\n+++ b/example.py\\n@@ -1 +1 @@\\n-old_selection_probe\\n+new_selection_probe',
        before_available:true, after_available:true, added:1, removed:1}]});
      diff.dataset.probe = 'diff'; nodes.push(diff);
      const question = questionNode({role:'question', text:'Question selection_probe',
        questions:[{question:'Question selection_probe', options:[{label:'Historical selection_probe answer'}]}]});
      question.dataset.probe = 'question'; nodes.push(question);
      const event = eventNode({role:'event', event_kind:'task', text:'Event selection_probe', details:'Details selection_probe'});
      event.dataset.probe = 'event'; nodes.push(event);
      box.replaceChildren(...nodes); _stick = false; paintSyntax(box);
    }""")


def main():
    node = start_node('a' * 32, 'SelectionFixture')
    node.state['pause_stream'] = True
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, theme in [(1440, 'light'), (1440, 'dark'), (390, 'light')]:
                context = browser.new_context(viewport={'width': width, 'height': 1000},
                    has_touch=width < 500, permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{node.server_port}/?sid=same-native-id')
                page.wait_for_selector('#msgs .msg')
                page.evaluate('(theme) => applyTheme(theme)', theme)
                seed(page)
                page.wait_for_function('document.querySelector("[data-probe=tool] .tool-command")?.dataset.syntaxDone')
                for role in ['user', 'assistant', 'thinking', 'system', 'command', 'answer']:
                    select_and_copy(page, page.locator(f'[data-probe={role}] .mb'))
                card = page.locator('[data-probe=tool] .tool-entry')
                toggle = card.locator('.tool-toggle')
                for expanded in [False, True]:
                    if expanded:
                        toggle.focus(); page.keyboard.press('Enter')
                    assert toggle.get_attribute('aria-expanded') == str(expanded).lower()
                    select_and_copy(page, card.locator('.tool-head > code'))
                    assert toggle.get_attribute('aria-expanded') == str(expanded).lower()
                    select_and_copy(page, card.locator('.tool-out'))
                    # Tool name is also data, not the action button.
                    select_and_copy(page, card.locator('.tool-meta'))
                    if expanded:
                        select_and_copy(page, card.locator('.tool-args'))
                        assert card.locator('.tool-args').is_visible()
                toggle.focus(); page.keyboard.press('Space')
                assert not card.locator('.tool-args').is_visible()
                toggle.click(); card.locator('.tool-args-close').click()
                assert not card.locator('.tool-args').is_visible()
                select_and_copy(page, page.locator('[data-probe=orphan] .tool-out'))
                group = page.locator('[data-probe=group]')
                select_and_copy(page, group.locator('.group-outline code').first)
                assert 'folded' in group.get_attribute('class')
                group.locator('.fold-toggle').click()
                select_and_copy(page, group.locator('.tool-head > code').first)
                assert 'folded' not in group.get_attribute('class')
                group.locator(':scope > .disclosure').click()
                assert 'folded' in group.get_attribute('class')
                turn = page.locator('[data-probe=turn]')
                for expanded in [False, True]:
                    select_and_copy(page, turn.locator('.turn-stats'))
                    assert ('folded' not in turn.get_attribute('class')) == expanded, (expanded, turn.get_attribute('class'), errors)
                    turn.locator('.fold-toggle').focus(); page.keyboard.press('Enter')
                for selector in ['[data-probe=rich] pre code', '[data-probe=rich] td',
                                 '[data-probe=diff] .file-change-head > b',
                                 '[data-probe=diff] .diff-line.add > code',
                                 '[data-probe=question] .question-text', '[data-probe=event] > span']:
                    select_and_copy(page, page.locator(selector).first)
                page.locator('[data-probe=diff] [data-diff-view=split]').click()
                select_and_copy(page, page.locator('[data-probe=diff] .diff-split .diff-line.add > code'))
                page.locator('[data-probe=event] summary').click()
                select_and_copy(page, page.locator('[data-probe=event] .event-detail-body'))
                body = page.locator('[data-probe=long] .mb')
                body.evaluate("e => e.scrollIntoView({block:'center'})")
                # Real hit-testing at a text glyph beneath the decorative fade.
                assert body.evaluate("""e => {
                  const b=e.getBoundingClientRect();
                  const text=document.caretRangeFromPoint(b.left+20,b.bottom-20);
                  return text && e.contains(text.startContainer) && text.startContainer.nodeType === Node.TEXT_NODE;
                }""")
                assert body.evaluate("e => getComputedStyle(e,'::after').pointerEvents") == 'none'
                page.locator('[data-probe=long] .disclosure').click()
                select_and_copy(page, body.locator('p').last)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                assert all(path == '/api/audit/browser' for path, _ in node.state['writes'])
                print(f'PASS native selection/copy, disclosures, bubbles, clipping: {width}px {theme}')
                context.close()
            browser.close()
    finally:
        stop(node)


if __name__ == '__main__':
    main()
