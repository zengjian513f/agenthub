"""User-opened tool groups must stay open when later tools/results append."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from hub_fixture import start_node, stop


EVAL = """() => {
  const tool = n => ({role:'tool', name:'exec', summary:`$ echo ${n}`,
                      text:`{"n":${n}}`, call_id:`call-${n}`});
  const box = document.createElement('div');
  appendMessages(box, [tool(1)], null, {openTail:true});
  const firstIsSingle = box.children.length === 1
    && box.firstElementChild.matches('.tool-msg');
  appendMessages(box, [tool(2)], null, {openTail:true});
  let group = box.querySelector(':scope > .grp');
  const twoBecomeOpenGroup = box.children.length === 1 && !!group
    && !group.classList.contains('folded') && group._toolItems.length === 2;
  appendMessages(box, [tool(3)], null, {openTail:true});
  group = box.querySelector(':scope > .grp');
  const nextBatchJoinsTail = box.children.length === 1
    && group._toolItems.length === 3 && !group.classList.contains('folded');
  appendMessages(box, [{role:'assistant', text:'工具段结束'}], null, {openTail:false});
  group = box.querySelector(':scope > .grp');
  const nonToolSealsGroup = group.classList.contains('folded')
    && group.nextElementSibling?.dataset.role === 'assistant';

  const idle = document.createElement('div');
  appendMessages(idle, [tool(4), tool(5)], null, {openTail:true});
  const openBeforeIdle = !idle.querySelector('.grp').classList.contains('folded');
  sealToolTail(idle);
  const idleSealsGroup = idle.querySelector('.grp').classList.contains('folded');

  const manual = document.createElement('div');
  appendMessages(manual, [tool(6), tool(7)], null, {openTail:false});
  const foldedAtRest = manual.querySelector('.grp').classList.contains('folded');
  const sameNode = manual.querySelector('.grp');
  sameNode.querySelector('.fold-toggle').click();
  const openedByUser = !sameNode.classList.contains('folded') && sameNode._userOpened === true;
  appendMessages(manual, [tool(8)], null, {openTail:false});
  const afterAppend = manual.querySelector('.grp');
  const staysOpenOnAppend = afterAppend === sameNode
    && !afterAppend.classList.contains('folded')
    && afterAppend._userOpened === true
    && afterAppend._toolItems.length === 3;
  const innerKept = afterAppend.querySelectorAll(':scope > .tool-entry').length === 3;
  sealToolTail(manual);
  const sealRespectsUser = !manual.querySelector('.grp').classList.contains('folded')
    && manual.querySelector('.grp')._userOpened === true;
  manual.querySelector('.grp .disclosure').click();
  const userCanRefold = manual.querySelector('.grp').classList.contains('folded')
    && !manual.querySelector('.grp')._userOpened;

  const pairing = document.createElement('div');
  appendMessages(pairing, [tool(9), tool(10)], null, {openTail:false});
  const pairGroup = pairing.querySelector('.grp');
  pairGroup.querySelector('.fold-toggle').click();
  appendMessages(pairing, [{role:'tool_result', call_id:'call-10', text:'done 10'}],
                 null, {openTail:false});
  const afterResult = pairing.querySelector('.grp');
  const resultKeepsUserOpen = afterResult === pairGroup
    && !afterResult.classList.contains('folded')
    && afterResult._userOpened === true
    && afterResult._toolItems.length === 2
    && !!afterResult._toolItems[1].result
    && afterResult.querySelectorAll(':scope > .tool-entry').length === 2
    && afterResult.querySelectorAll(':scope > .tool-entry .tool-status').length === 1;
  return {firstIsSingle, twoBecomeOpenGroup, nextBatchJoinsTail,
          nonToolSealsGroup, openBeforeIdle, idleSealsGroup,
          foldedAtRest, openedByUser, staysOpenOnAppend, innerKept,
          sealRespectsUser, userCanRefold, resultKeepsUserOpen};
}"""


def main():
    node = start_node('a' * 32, 'ToolGroupFold')
    node.state['pause_stream'] = True
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{node.server_port}/?sid=same-native-id')
            page.wait_for_selector('#msgs .msg')
            result = page.evaluate(EVAL)
            failed = [name for name, ok in result.items() if not ok]
            assert not failed, (failed, result)
            assert not errors, errors
            print('ok', result)
    finally:
        stop(node)


if __name__ == '__main__':
    main()
