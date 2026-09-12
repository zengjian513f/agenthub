'use strict';

const HUB_MODE = document.querySelector('meta[name="agenthub-mode"]')?.content === 'hub';
const STORAGE_PREFIX = HUB_MODE ? `agenthub.hub.${location.pathname}.` : 'agenthub.';
// list 只有启用的机器；停用的机器只在设置页露面（disabled），别处视同不存在
const Nodes = {list: [], disabled: [], off: new Set(), capabilities: {}, errors: new Map()};
try { Nodes.off = new Set(JSON.parse(localStorage.getItem(STORAGE_PREFIX + 'nodesOff')) || []); } catch {}

function nodeOf(uid) {
  return HUB_MODE ? String(uid || '').match(/^[^:]+:([a-f0-9]{32})~/)?.[1] || '' : '';
}
function nodeSelected(row) { return !HUB_MODE || !Nodes.off.has(row.node_id); }
function selectedNodeIds() { return Nodes.list.filter(n => !Nodes.off.has(n.id)).map(n => n.id); }
function nodeDirectory(row, length = 999) {
  return (row.node_name ? row.node_name + ' ' : '') + shortCwd(row.cwd || '(未知)', length);
}
// 机器配色由中央的注册表按机器配置，前端只负责显示。
function nodeColor(name) {
  return Nodes.list.find(n => n.name === name)?.color || '';
}
function nodeBadge(name) {
  return `<span class="node-badge" data-node-color="${esc(nodeColor(name))}">${esc(name)}</span>`;
}
function nodeDirectoryMarkup(row, length = 999) {
  return (row.node_name ? nodeBadge(row.node_name) + ' ' : '') + esc(shortCwd(row.cwd || '(未知)', length));
}
function newNodeId() { return HUB_MODE ? document.querySelector('#new-node')?.value || '' : ''; }
function newDirsKey() { return HUB_MODE ? 'newDirs.' + newNodeId() : 'newDirs'; }
function newNodeCapabilities() { return HUB_MODE ? Nodes.capabilities[newNodeId()] || {} : T; }
function sessionTerminalEnabled(uid) {
  return typeof T !== 'undefined' && (HUB_MODE ? !!Nodes.capabilities[nodeOf(uid)]?.enabled : T.enabled);
}

const ConsoleUI = {errors: new Map(), busy: new Set()};

function consoleUnavailableReason(uid, agent = null, lastError = true) {
  if (!uid) return '请先选择一个会话，再打开控制台。';
  if (agent) return '子代理没有独立控制台，请切换到主会话后打开控制台。';
  if (typeof T === 'undefined' || typeof takeover !== 'function')
    return '控制台组件尚未加载完成或加载失败，请稍后重试；持续失败时请刷新页面。';
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined')
    return '浏览器终端组件加载失败，无法显示控制台，请刷新页面重新加载。';
  if (ConsoleUI.busy.has(uid)) return '正在打开控制台，请等待当前连接请求完成。';
  if (T.listError) return T.listError;
  if (!T.listLoaded) return '正在读取控制台状态，请稍后重试。';
  const nid = nodeOf(uid), node = Nodes.list.find(n => n.id === nid);
  const cap = HUB_MODE ? Nodes.capabilities[nid] : T;
  if (HUB_MODE) {
    if (!node) return '会话所属机器尚未加载或已被移除，无法连接控制台。';
    const error = Nodes.errors.get('term')?.find(e => e.node_id === nid);
    if (error) return `${node.name} 终端列表请求失败：${error.error || '服务器未返回原因'}。`;
    if (!cap) return `${node.name} 的控制台状态尚未返回，请稍后重试。`;
  }
  if (!cap?.enabled) return `${node ? node.name + '：' : ''}${cap?.unavailable_reason || '服务报告控制台不可用，但未返回具体原因。'}`;
  const linked = linkedTermSession(uid, {followReplacement: true});
  const source = sessionTermMeta(uid)?.source || String(uid).split(':')[0];
  if (!linked && !cap.sources?.[source])
    return `此机器未找到可用的 ${SOURCES[source]?.name || source} 命令，无法启动该会话的控制台。`;
  return lastError ? ConsoleUI.errors.get(uid) || '' : '';
}

function showConsoleToast(reason) {
  const toast = document.querySelector('#console-toast');
  if (!toast) return;
  toast.textContent = reason;
  toast.hidden = !reason;
}

function paintConsoleAvailability(button, uid, agent = null) {
  // 只有结构性不可用（离线机器 / 缺 CLI / 终端禁用 / 子代理 / 列表出错）才把按钮
  // 打成灰色并给出解释。上一次连接失败是可重试状态：按钮保持正常，点击直接重连，
  // 失败原因写进终端本身，不再用灰按钮 + 悬停问号 + 确认框拦住用户。
  const reason = consoleUnavailableReason(uid, agent, false);
  button.classList.toggle('console-unavailable', !!reason);
  button.dataset.unavailable = String(!!reason);
  button.disabled = false; // The explanation must remain reachable by mouse and keyboard.
  if (reason) {
    button.title = '';
    button.ariaLabel = '控制台不可用：' + reason;
  }
  if (button.matches(':hover') || document.activeElement === button) showConsoleToast(reason);
}

function consoleButtonMarkup() {
  return `<button class="iconbtn" id="a-term" type="button" title="打开控制台" aria-label="打开控制台">${uiIcon('terminal')}</button>`;
}

function bindConsoleButton(button, uid, agent = null) {
  if (!button) return;
  button.onmouseenter = button.onfocus = () => showConsoleToast(consoleUnavailableReason(uid, agent, false));
  button.onmouseleave = button.onblur = () => showConsoleToast('');
  button.onclick = async () => {
    showConsoleToast('');
    const reason = consoleUnavailableReason(uid, agent, false);
    if (reason) return alert('控制台不可用：\n' + reason);
    // 上次连接失败不再拦一道确认框：点击直接重新接入并显示 pty，失败原因由终端呈现。
    ConsoleUI.busy.add(uid);
    ConsoleUI.errors.delete(uid);
    paintConsoleAvailability(button, uid, agent);
    try {
      if (linkedTermSession(uid, {followReplacement: true})) await toggleLinkedTermSession(uid);
      else await takeover(uid, button);
    } catch (error) {
      const message = error.message || String(error);
      ConsoleUI.errors.set(uid, message);
      alert('打开控制台失败：\n' + message);
    } finally {
      ConsoleUI.busy.delete(uid);
      if (typeof renderTakeoverBtn === 'function') renderTakeoverBtn();
    }
  };
  paintConsoleAvailability(button, uid, agent);
}

function applyNodeState(data, context = 'nodes') {
  if (!HUB_MODE || !data) return;
  if (Array.isArray(data.nodes)) Nodes.list = data.nodes;
  if (Array.isArray(data.disabled)) Nodes.disabled = data.disabled;
  if (data.capabilities) Nodes.capabilities = data.capabilities;
  if (Array.isArray(data.errors)) Nodes.errors.set(context, data.errors);
  renderNodes();
}

function nodeClock(ts) {
  return new Date(ts * 1000).toLocaleString('zh-CN',
    {hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}
function nodeAgo(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
  return `${Math.floor(s / 86400)} 天 ${Math.floor(s % 86400 / 3600)} 小时`;
}
function nodeOfflineReason(node) {
  if (node?.online !== false) return '';
  const fallback = [...Nodes.errors.values()].flat().find(e => e.node_id === node.id)?.error;
  const path = {'/api/live': '运行状态', '/api/sessions': '会话列表', '/api/term/list': '终端列表'}[node.failed_path];
  const lines = [`${node.name} 离线：${node.error || fallback || '中央站未能连接该机器，暂未收到具体错误原因。'}`];
  if (path) lines.push(`失败请求：${path}。`);
  if (node.offline_since) lines.push(`已离线 ${nodeAgo(node.offline_since)}（自 ${nodeClock(node.offline_since)}）。`);
  if (node.checked_at) lines.push(`上次检测：${nodeAgo(node.checked_at)}前；中央每 10 秒自动重试，机器恢复后会自动回到列表。`);
  if (node.last_seen) lines.push(`当前显示的是 ${nodeClock(node.last_seen)} 同步的离线缓存会话。`);
  return lines.join('\n');
}

// 中窄屏机器筛选收成一个下拉按钮，按钮上概括当前选中的机器。
function paintNodePick() {
  const label = document.querySelector('#node-pick .node-pick-label');
  if (!label) return;
  const narrow = typeof MOBILE !== 'undefined' && MOBILE.matches;
  const picked = Nodes.list.filter(n => !Nodes.off.has(n.id));
  let text, color = '', none = false;
  if (!Nodes.list.length) { text = '无机器'; none = true; }
  else if (picked.length === Nodes.list.length) text = narrow ? '全部' : '全部机器';
  else if (!picked.length) { text = '未选机器'; none = true; }
  else if (picked.length === 1) { text = picked[0].name; color = picked[0].color || ''; }
  else if (picked.length === 2 && !narrow) text = picked.map(n => n.name).join('、');
  else text = `${picked.length} 台${narrow ? '' : '机器'}`;
  label.textContent = text;
  label.dataset.nodeColor = color;
  label.classList.toggle('node-none', none);
  const button = label.parentElement;
  button.title = button.ariaLabel = `选择机器：${text}`;
}

function closeNodePick() {
  const picker = document.querySelector('#node-picker');
  if (!picker?.classList.contains('open')) return;
  picker.classList.remove('open');
  document.querySelector('#node-pick')?.setAttribute('aria-expanded', 'false');
}

function toggleNodePick(open) {
  const picker = document.querySelector('#node-picker');
  const button = document.querySelector('#node-pick');
  if (!picker || !button) return;
  if (open === undefined) open = !picker.classList.contains('open');
  if (!open) return closeNodePick();
  // 列表定位在 header 下，与按钮左缘对齐；超出右边界时贴右
  const header = button.closest('header');
  const left = button.getBoundingClientRect().left - header.getBoundingClientRect().left;
  const menu = document.querySelector('#node-chips');
  menu.style.setProperty('--node-menu-left', `${Math.round(left)}px`);
  picker.classList.add('open');
  button.setAttribute('aria-expanded', 'true');
  requestAnimationFrame(() => {
    const overflow = menu.getBoundingClientRect().right - (innerWidth - 8);
    if (overflow > 0) menu.style.setProperty('--node-menu-left', `${Math.round(Math.max(8, left - overflow))}px`);
  });
}

function renderNodes() {
  if (!HUB_MODE) return;
  const host = document.querySelector('#node-chips');
  if (!host) return;
  const scroll = host.scrollLeft;
  const toolbarScroll = host.parentElement.scrollLeft;
  const picker = document.querySelector('#node-picker');
  if (picker) picker.hidden = false;
  else host.hidden = false;
  const existing = new Map([...host.children].map(b => [b.dataset.node, b]));
  let position = 0;
  const button = (id, text, on, click, title = '') => {
    const b = existing.get(id) || document.createElement('button');
    b.type = 'button'; b.className = on ? 'on' : '';
    b.textContent = text; b.title = title;
    b.setAttribute('aria-pressed', String(on)); b.onclick = click;
    if (host.children[position] !== b) host.insertBefore(b, host.children[position] || null);
    position++;
    return b;
  };
  const change = () => {
    store.set('nodesOff', [...Nodes.off]);
    renderNodes(); renderChips(); renderSide();
    showSessionCount(sidebarSessions().filter(nodeSelected).length);
    if (S.results !== null) void runSearch();
  };
  for (const n of Nodes.list) {
    const count = S.sessions.filter(s => s.node_id === n.id && !sessionHidden(s)).length;
    const item = button(n.id, `${n.name} ${count}`,
      !Nodes.off.has(n.id), e => {
        if (n.online === false) return alert(nodeOfflineReason(n));
        Nodes.off.has(n.id) ? Nodes.off.delete(n.id) : Nodes.off.add(n.id);
        change();
      }, n.online === false ? '' : '点击选择或取消；双击只选这台机器');
    const countLabel = document.createElement('b');
    countLabel.className = 'node-count'; countLabel.textContent = count;
    item.replaceChildren(document.createTextNode(`${n.name} `), countLabel);
    item.dataset.node = n.id;
    item.dataset.nodeColor = n.color || '';
    item.classList.toggle('node-offline', n.online === false);
    item.ariaLabel = `${n.name} ${count}` + (n.online === false ? `，${nodeOfflineReason(n)}` : '');
    item.ondblclick = () => {
      if (n.online === false) return;
      Nodes.off = new Set(Nodes.list.filter(x => x.id !== n.id).map(x => x.id)); change();
    };
  }
  for (const [id, item] of existing) if (!Nodes.list.some(n => n.id === id)) item.remove();
  host.scrollLeft = scroll;
  host.parentElement.scrollLeft = toolbarScroll;
  paintNodePick();
  const notice = document.querySelector('#node-notice');
  const labels = {search: '全文搜索', sessions: '会话列表', live: '运行状态', term: '终端列表'};
  const failures = [...Nodes.errors].flatMap(([context, errors]) => {
    const names = [...new Set(errors.filter(e => !Nodes.off.has(e.node_id)
      && Nodes.list.find(n => n.id === e.node_id)?.online !== false).map(e => e.name))];
    return names.length ? [`${names.join('、')} ${labels[context] || '请求'}失败或超时`] : [];
  });
  notice.hidden = !failures.length && !!Nodes.list.length;
  notice.textContent = failures.length
    ? `${failures.join('；')}；相关结果可能不完整或未更新。`
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
  const pick = document.querySelector('#node-pick');
  if (pick) {
    pick.onclick = () => toggleNodePick();
    pick.onkeydown = event => {
      if (event.key !== 'ArrowDown') return;
      event.preventDefault();
      toggleNodePick(true);
      document.querySelector('#node-chips button')?.focus();
    };
    document.querySelector('#node-picker').addEventListener('focusout', event => {
      if (!event.currentTarget.contains(event.relatedTarget)) closeNodePick();
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('#node-picker')) closeNodePick();
    }, true);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.querySelector('#node-picker.open')) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeNodePick();
        pick.focus();
      }
    }, true);
    addEventListener('resize', closeNodePick);
    if (typeof MOBILE !== 'undefined') MOBILE.addEventListener('change', paintNodePick);
  }
  void loadNodes().catch(() => {});
});
