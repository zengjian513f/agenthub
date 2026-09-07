'use strict';

const HUB_MODE = document.querySelector('meta[name="agenthub-mode"]')?.content === 'hub';
const STORAGE_PREFIX = HUB_MODE ? `agenthub.hub.${location.pathname}.` : 'agenthub.';
const Nodes = {list: [], off: new Set(), capabilities: {}, errors: new Map()};
try { Nodes.off = new Set(JSON.parse(localStorage.getItem(STORAGE_PREFIX + 'nodesOff')) || []); } catch {}

function nodeOf(uid) {
  return HUB_MODE ? String(uid || '').match(/^[^:]+:([a-f0-9]{32})~/)?.[1] || '' : '';
}
function nodeSelected(row) { return !HUB_MODE || !Nodes.off.has(row.node_id); }
function selectedNodeIds() { return Nodes.list.filter(n => !Nodes.off.has(n.id)).map(n => n.id); }
function nodeDirectory(row, length = 999) {
  return (row.node_name ? row.node_name + ' · ' : '') + shortCwd(row.cwd || '(未知)', length);
}
function newNodeId() { return HUB_MODE ? document.querySelector('#new-node')?.value || '' : ''; }
function newDirsKey() { return HUB_MODE ? 'newDirs.' + newNodeId() : 'newDirs'; }
function newNodeCapabilities() { return HUB_MODE ? Nodes.capabilities[newNodeId()] || {} : T; }
function sessionTerminalEnabled(uid) {
  return typeof T !== 'undefined' && (HUB_MODE ? !!Nodes.capabilities[nodeOf(uid)]?.enabled : T.enabled);
}

function applyNodeState(data, context = 'nodes') {
  if (!HUB_MODE || !data) return;
  if (Array.isArray(data.nodes)) Nodes.list = data.nodes;
  if (data.capabilities) Nodes.capabilities = data.capabilities;
  if (Array.isArray(data.errors)) Nodes.errors.set(context, data.errors);
  renderNodes();
}

function renderNodes() {
  if (!HUB_MODE) return;
  const host = document.querySelector('#node-chips');
  if (!host) return;
  const scroll = host.scrollLeft;
  const toolbarScroll = host.parentElement.scrollLeft;
  host.hidden = false;
  host.replaceChildren();
  const button = (text, on, click, title = '') => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = on ? 'on' : '';
    b.textContent = text; b.title = title;
    b.setAttribute('aria-pressed', String(on)); b.onclick = click;
    host.appendChild(b);
    return b;
  };
  const change = () => {
    store.set('nodesOff', [...Nodes.off]);
    renderNodes(); renderChips(); renderSide();
    showSessionCount(sidebarSessions().filter(nodeSelected).length);
    if (S.results !== null) void runSearch();
  };
  for (const n of Nodes.list) {
    const count = S.sessions.filter(s => s.node_id === n.id).length;
    const item = button(`${n.name} ${count}`,
      !Nodes.off.has(n.id), e => { Nodes.off.has(n.id) ? Nodes.off.delete(n.id) : Nodes.off.add(n.id); change(); },
      n.online === false ? '离线；列表可能是缓存，运行状态未知' : '点击选择或取消；双击只选这台机器');
    const countLabel = document.createElement('b');
    countLabel.className = 'node-count'; countLabel.textContent = count;
    item.replaceChildren(document.createTextNode(`${n.name} `), countLabel);
    item.dataset.node = n.id;
    item.ondblclick = () => {
      Nodes.off = new Set(Nodes.list.filter(x => x.id !== n.id).map(x => x.id)); change();
    };
  }
  host.scrollLeft = scroll;
  host.parentElement.scrollLeft = toolbarScroll;
  const notice = document.querySelector('#node-notice');
  const errors = [...Nodes.errors.values()].flat().filter(e => !Nodes.off.has(e.node_id));
  const names = [...new Set(errors.map(e => e.name))];
  notice.hidden = !names.length && !!Nodes.list.length;
  notice.textContent = names.length
    ? `${names.join('、')} 请求失败或超时；当前结果可能不完整，离线机器的运行状态未知。`
    : '暂无可用机器。';
}

async function loadNodes() {
  if (!HUB_MODE) return;
  const r = await fetch(appUrl('api/nodes'));
  if (!r.ok) throw new Error('无法读取机器列表');
  applyNodeState(await r.json());
}

function prepareNewNode() {
  if (!HUB_MODE) return;
  const select = document.querySelector('#new-node');
  const previous = select.value;
  const selected = selectedNodeIds();
  const preferred = selected.length === 1 ? selected[0]
    : nodeOf(S.sel) || previous || store.get('newNode', '');
  select.replaceChildren();
  for (const n of Nodes.list) {
    const option = document.createElement('option');
    option.value = n.id;
    option.textContent = n.name + (Nodes.capabilities[n.id]?.enabled ? '' : '（离线或未启用终端）');
    option.disabled = !Nodes.capabilities[n.id]?.enabled;
    select.appendChild(option);
  }
  if ([...select.options].some(o => o.value === preferred && !o.disabled)) select.value = preferred;
  else select.value = [...select.options].find(o => !o.disabled)?.value || '';
  document.querySelector('#new-node-label').hidden = false;
}

function refreshNewNodeFields() {
  closeCwdPicker();
  const cap = newNodeCapabilities();
  cwdCompletion.common = commonSessionDirs();
  for (const input of document.querySelectorAll('input[name="new-source"]')) input.disabled = !cap.sources?.[input.value];
  const checked = document.querySelector('input[name="new-source"]:checked');
  if (!checked || checked.disabled) document.querySelector('input[name="new-source"]:not(:disabled)')?.click();
  const selected = S.sessions.find(s => s.uid === S.sel && (!HUB_MODE || s.node_id === newNodeId()));
  document.querySelector('#new-cwd').value = selected?.cwd || store.get(newDirsKey(), [])[0]
    || cwdCompletion.common[0]?.cwd || cap.home || '';
  document.querySelector('#new-session-error').textContent = '';
  document.querySelector('#new-session-go').disabled = !cap.enabled;
  renderCommonCwdOptions();
}

document.addEventListener('DOMContentLoaded', () => {
  if (!HUB_MODE) return;
  document.querySelector('#new-node').onchange = () => {
    store.set('newNode', newNodeId()); refreshNewNodeFields();
  };
  void loadNodes().catch(() => {});
});
