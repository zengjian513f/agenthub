'use strict';

// 接管会话: 在服务端把它用 tmux resume 起来, 然后把终端嵌在会话详情底部。
// 会话跑在 tmux 里, 所以关掉页面/重启 sesman 都不会打断它。

const T = {
  term: null,      // xterm 实例
  fit: null,
  ws: null,
  name: null,      // 当前挂着的 tmux 会话名
  uid: null,       // 对应的 sesman 会话
  enabled: false,
  height: store.get('termh', 320),
  mode: store.get('termmode', 'normal'), // normal | collapsed | full
  localMouse: store.get('tmouse', false),   // true = 鼠标归浏览器, 可以框选复制
  scrollPos: 0,                             // tmux 里往上翻了多少行
  sources: {},
  home: '',
  pending: [],
  resolving: new Set(),
};

// 应用(claude/codex 的 TUI)申请接管鼠标的那些序列。选择模式下要拦掉,
// 否则 xterm 会把拖拽当成给应用的鼠标事件, 没法框选。
const MOUSE_ON = /\x1b\[\?(1000|1002|1003|1005|1006|1015)h/g;
const MOUSE_OFF = '\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l';

function termTheme() {
  const css = getComputedStyle(document.documentElement);
  const read = name => css.getPropertyValue(name).trim();
  return {
    background: read('--terminal-bg'), foreground: read('--terminal-fg'),
    cursor: read('--terminal-cursor'), selectionBackground: read('--terminal-selection'),
  };
}

function termFont() {
  return getComputedStyle(document.documentElement).getPropertyValue('--terminal-font').trim();
}

function termFontSize() {
  const value = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--terminal-font-size'));
  return Number.isFinite(value) ? value : 14.04;
}

// xterm 用 canvas 绘字；先等网页字体就绪，避免它按回退字体计算字符宽度。
const terminalFontReady = document.fonts
  ? document.fonts.load(`${termFontSize()}px ${termFont()}`, 'MW0il')
  : Promise.resolve();

async function loadTermList() {
  const fingerprint = () => [
    ...(T.list || []).map(x => `${x.name}\t${x.cwd}`),
    ...(T.pending || []).map(x => `pending\t${x.name}\t${x.cwd}`),
  ].join('\n');
  const before = fingerprint();
  try {
    const d = await (await fetch(appUrl('api/term/list'))).json();
    T.enabled = !!d.enabled;
    T.list = d.sessions || [];
    T.sources = d.sources || {};
    T.home = d.home || '';
    T.pending = d.pending || [];
  } catch {
    T.enabled = false;
    T.list = [];
    T.sources = {};
    T.pending = [];
  }
  $('#new-session')?.classList.toggle('hidden', !T.enabled);
  const after = fingerprint();
  if (after !== before && typeof renderSide === 'function') {
    const side = $('#side'), top = side?.scrollTop || 0;
    renderChips();
    if (!S.results) showSessionCount(sidebarSessions().length);
    renderSide();
    if (side) side.scrollTop = top;
    paintLive();
  }
  // 刷新页面后仍从持久化 meta 恢复关联轮询；Set 防止重复启动。
  for (const pending of pendingTmuxSessions()) resolveNewSession(pending);
}

/** 某个会话是否已经被接管 (存在对应的 tmux 会话)。 */
function takenOver(uid) {
  const s = S.sessions.find(x => x.uid === uid);
  if (!s || !T.list) return null;
  const name = `sesman-${s.source}-${String(s.sid).slice(0, 8)}`;
  return T.list.some(x => x.name === name) ? name : null;
}

// ---------------------------------------------------------------- 接管
async function takeover(uid, btn) {
  const setBtn = (t, dis) => {
    if (btn) { btn.title = btn.ariaLabel = t; btn.disabled = dis; }
  };
  setBtn('接管中…', true);
  try {
    let d = await post('api/term/takeover', { uid, cols: 120, rows: termRows() });
    if (d.needs_confirm) {
      const n = (d.pids || []).length;
      const ok = confirm(
        `这个会话正在运行中（${n} 个进程），而且不在 tmux 里，无法直接接入。\n\n`
        + `接管会先结束正在运行的实例，再用 tmux 重新打开它。\n`
        + `未保存的输入会丢失，已完成的对话不受影响。\n\n继续吗？`);
      if (!ok) return;
      setBtn('结束旧实例…', true);
      d = await post('api/term/takeover', { uid, force: true, cols: 120, rows: termRows() });
    }
    if (d.error) return alert('接管失败: ' + d.error);
    await loadTermList();
    T.uid = uid;
    S.live.add(uid);
    S.liveTmux.add(uid);
    paintLive();
    openTermPane(d.name);
    if (d.action === 'killed') newBadge('已结束原实例并接管');
  } finally {
    setBtn('接管会话', false);
    renderTakeoverBtn();
    renderComposer();
  }
}

async function post(url, body) {
  const r = await fetch(appUrl(url), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---------------------------------------------------------------- 新建会话
function commonSessionDirs() {
  const dirs = new Map();
  for (const s of S.sessions) {
    const cwd = String(s.cwd || '');
    if (!cwd.startsWith('/')) continue;
    const row = dirs.get(cwd) || { cwd, count: 0, updated: '' };
    row.count++;
    if ((s.updated || '') > row.updated) row.updated = s.updated || '';
    dirs.set(cwd, row);
  }
  for (const [i, cwd] of store.get('newDirs', []).entries()) {
    if (!cwd?.startsWith('/')) continue;
    const row = dirs.get(cwd) || { cwd, count: 0, updated: '' };
    row.recent = 20 - i;
    dirs.set(cwd, row);
  }
  if (T.home && !dirs.has(T.home)) dirs.set(T.home, { cwd: T.home, count: 0, updated: '' });
  return [...dirs.values()].sort((a, b) =>
    (b.recent || 0) - (a.recent || 0) || b.count - a.count
    || b.updated.localeCompare(a.updated) || a.cwd.localeCompare(b.cwd)).slice(0, 18);
}

function openNewSessionDialog() {
  const dialog = $('#new-session-dialog');
  const list = $('#new-cwd-list');
  const rows = commonSessionDirs();
  list.innerHTML = '';
  for (const d of rows) {
    const o = document.createElement('option');
    o.value = d.cwd;
    o.textContent = d.count ? `${d.cwd}  ·  ${d.count} 个会话` : d.cwd;
    list.appendChild(o);
  }
  for (const input of dialog.querySelectorAll('input[name="new-source"]')) {
    input.disabled = !T.sources[input.value];
  }
  const checked = dialog.querySelector('input[name="new-source"]:checked');
  if (!checked || checked.disabled) dialog.querySelector('input[name="new-source"]:not(:disabled)')?.click();
  const selected = S.sessions.find(s => s.uid === S.sel)?.cwd;
  const cwd = selected || store.get('newDirs', [])[0] || rows[0]?.cwd || T.home || '';
  $('#new-cwd').value = cwd;
  list.value = cwd;
  $('#new-session-error').textContent = '';
  $('#new-session-go').disabled = false;
  dialog.showModal();
  setTimeout(() => { $('#new-cwd').focus(); $('#new-cwd').select(); }, 0);
}

function showNewSessionStage(info) {
  S.results = null;
  S.filter = S.term = '';
  $('#q').value = '';
  S.sel = pendingUid(info.name);
  store.set('sel', S.sel);
  renderSide();
  showSessionCount(sidebarSessions().length);
  $('#composer').classList.add('hidden');
  const src = SOURCES[info.source];
  $('#detail').innerHTML = `<div class="dhead"><div class="dtitle">
    <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
    <h2>${icon(info.source)} 新建 ${esc(src.name)} 会话</h2></div>
    <div class="dmeta"><span class="meta-source">${esc(src.name)}</span><span class="meta-secondary"><code>${esc(info.cwd)}</code></span></div>
  </div><div class="empty new-session-wait">终端已启动，正在等待会话记录落盘…</div>`;
  $('#detail .mobile-back').onclick = showMobileList;
  showMobileDetail();
  T.uid = null;
  T.mode = 'full';
}

async function openPendingSession(info) {
  const pending = { ...info, name: info.tmuxName || info.name };
  showNewSessionStage(pending);
  await openTermPane(pending.name);
  resolveNewSession(pending);
}

async function resolveNewSession(info) {
  const pendingId = pendingUid(info.name);
  if (T.resolving.has(info.name)) return;
  T.resolving.add(info.name);
  try {
    for (let i = 0; i < 160; i++) {         // TUI 等用户首次输入时可能较久，最多等两分钟
      await new Promise(r => setTimeout(r, 750));
      let d;
      try {
        d = await (await fetch(appUrl(`api/term/new-status?name=${encodeURIComponent(info.name)}`))).json();
      } catch { continue; }
      if (d.error) {
        const wait = $('.new-session-wait');
        if (wait && S.sel === pendingId) wait.textContent = `会话关联失败：${d.error}`;
        return;
      }
      if (d.waiting) {
        const wait = $('.new-session-wait');
        if (wait && S.sel === pendingId && !d.running) wait.textContent = 'CLI 已退出，尚未生成会话记录';
        continue;
      }
      const active = S.sel === pendingId;
      await loadSessions(true);
      await loadTermList();
      if (!active) return;                  // 用户已看别处，只更新列表，不抢走右侧页面
      T.uid = d.uid;
      if (d.running) {
        S.live.add(d.uid);
        S.liveTmux.add(d.uid);
      }
      await openSession(d.uid);
      if (d.running) await openTermPane(d.name);
      else closeTermPane();
      paintLive();
      return;
    }
    const wait = $('.new-session-wait');
    if (wait && S.sel === pendingId) wait.textContent = '会话仍在终端中运行；产生首条记录后会出现在列表里';
  } finally {
    T.resolving.delete(info.name);
  }
}

async function createNewSession(e) {
  e.preventDefault();
  const source = $('#new-session-dialog input[name="new-source"]:checked')?.value;
  const cwd = $('#new-cwd').value.trim();
  const go = $('#new-session-go'), error = $('#new-session-error');
  error.textContent = '';
  if (!source) { error.textContent = '没有可用的会话类型'; return; }
  if (!cwd) { error.textContent = '请选择启动目录'; return; }
  go.disabled = true;
  go.textContent = '创建中…';
  try {
    const d = await post('api/term/create', { source, cwd, cols: 120, rows: termRows() });
    if (d.error) { error.textContent = d.error; return; }
    const recent = [d.cwd, ...store.get('newDirs', []).filter(x => x !== d.cwd)].slice(0, 8);
    store.set('newDirs', recent);
    $('#new-session-dialog').close();
    showNewSessionStage(d);
    await loadTermList();
    await openTermPane(d.name);
    resolveNewSession(d);
  } catch (err) {
    error.textContent = err.message || '创建失败';
  } finally {
    go.disabled = false;
    go.textContent = '创建并打开';
  }
}

$('#new-session').onclick = openNewSessionDialog;
$('#new-session-form').onsubmit = createNewSession;
$('#new-session-dialog .modal-close').onclick = () => $('#new-session-dialog').close();
$('#new-session-dialog .modal-cancel').onclick = () => $('#new-session-dialog').close();
$('#new-cwd-list').onchange = e => { $('#new-cwd').value = e.target.value; };
$('#new-cwd-list').ondblclick = () => $('#new-session-form').requestSubmit();
$('#new-session-dialog').addEventListener('click', e => {
  if (e.target === $('#new-session-dialog')) $('#new-session-dialog').close();
});

const termRows = () => Math.max(10, Math.floor((T.height - 34) / (termFontSize() * 1.31)));

/** 详情页头部那个按钮的文案随状态变。 */
function renderTakeoverBtn() {
  const b = $('#a-term');
  if (!b) return;
  const name = takenOver(S.sel);
  const paneOpen = !!name && !$('#termpane').classList.contains('hidden');
  const snapped = !MOBILE.matches && paneOpen && (T.mode === 'collapsed' || T.mode === 'full');
  const terminalVisible = paneOpen && (MOBILE.matches || T.mode !== 'collapsed');
  const label = !name ? '接管会话'
    : snapped ? (T.mode === 'collapsed' ? '切换到终端' : '切换到对话')
      : (paneOpen ? '收起终端' : '展开终端');
  b.innerHTML = uiIcon('terminal');
  b.title = b.ariaLabel = label;
  b.setAttribute('aria-expanded', String(terminalVisible));
  b.classList.toggle('on', !!name);
  b.classList.toggle('session-live', S.live.has(S.sel));
  b.classList.toggle('session-tmux', S.liveTmux.has(S.sel));
  renderComposer();
}

// ---------------------------------------------------------------- 终端面板
function ensureTerm() {
  if (T.term) return T.term;
  T.term = new Terminal({
    fontFamily: termFont(),
    fontSize: termFontSize(), cursorBlink: true, scrollback: 8000, theme: termTheme(),
  });
  T.fit = new FitAddon.FitAddon();
  T.term.loadAddon(T.fit);
  T.term.open($('#xterm'));
  T.term.onData(d => {
    if (T.ws?.readyState !== 1) return;
    if (T.scrollPos || _wheelRequests.size || _resumeInput) {
      // 等所有已经发出的滚轮请求落地，再由一个服务端请求原子执行
      // 「退出 copy-mode → 写入字符」。直接向 attach 发 q 不可靠，而把
      // cancel 和字符分走 HTTP/WS 两条通道又会乱序。
      abortWheel();
      setScrollPos(0);
      const name = T.name;
      const before = _resumeInput || Promise.allSettled([..._wheelRequests]);
      const job = before.then(() => post('api/term/send', { name, text: d, enter: false }));
      _resumeInput = job;
      job.then(
        () => { if (_resumeInput === job) _resumeInput = null; },
        () => { if (_resumeInput === job) _resumeInput = null; },
      );
      return;
    }
    T.ws.send(new TextEncoder().encode(d));
  });
  // 滚轮翻 tmux 的历史, 而不是发给应用 —— 这样才像普通终端
  T.term.attachCustomWheelEventHandler(e => {
    if (!T.name) return true;
    wheelBy(e.deltaY);
    return false;
  });
  addEventListener('resize', () => { layoutTermPane(); fitTerm(); });
  return T.term;
}

function fitTerm() {
  if (!T.fit || $('#termpane').classList.contains('hidden')) return;
  try { T.fit.fit(); } catch { return; }
  if (T.ws?.readyState === 1) {
    T.ws.send(JSON.stringify({ t: 'resize', cols: T.term.cols, rows: T.term.rows }));
  }
}

async function openTermPane(name) {
  const pane = $('#termpane');
  pane.classList.remove('hidden');
  layoutTermPane();
  renderTakeoverBtn();
  try { await terminalFontReady; } catch { /* 字体失败时继续用 Consola/monospace */ }
  if (pane.classList.contains('hidden')) return;
  ensureTerm();
  setTimeout(fitTerm, 20);
  if (T.name !== name) attachTerm(name);
  else T.term.focus();
}

/** 普通高度下按钮仍是展开/收起；吸附到边缘后改为纯对话/纯终端切换。 */
function toggleTermPane(name) {
  const pane = $('#termpane');
  if (pane.classList.contains('hidden')) {
    if (!MOBILE.matches && T.mode === 'collapsed') {
      T.mode = 'full';
      store.set('termmode', T.mode);
    }
    openTermPane(name);
    return;
  }
  if (!MOBILE.matches && (T.mode === 'collapsed' || T.mode === 'full')) {
    T.mode = T.mode === 'collapsed' ? 'full' : 'collapsed';
    store.set('termmode', T.mode);
    layoutTermPane();
    renderTakeoverBtn();
    if (T.mode === 'full') setTimeout(fitTerm, 0);
    return;
  }
  closeTermPane();
}

function closeTermPane() {
  detachTerm();
  const pane = $('#termpane');
  pane.classList.add('hidden');
  pane.classList.remove('term-collapsed');
  $('#right').classList.remove('term-full');
  renderTakeoverBtn();
}

function layoutTermPane() {
  const pane = $('#termpane');
  const right = $('#right');
  const desktop = !MOBILE.matches;
  right.classList.toggle('term-full', desktop && T.mode === 'full');
  pane.classList.toggle('term-collapsed', desktop && T.mode === 'collapsed');
  if (MOBILE.matches) {
    pane.style.removeProperty('height');
    pane.style.removeProperty('--mobile-terminal-top');
  } else {
    pane.style.removeProperty('--mobile-terminal-top');
    if (T.mode === 'collapsed') pane.style.height = '0px';
    else if (T.mode === 'full') {
      pane.style.height = Math.max(0, right.clientHeight - $('#detail').offsetHeight) + 'px';
    }
    else pane.style.height = Math.min(T.height, right.clientHeight) + 'px';
  }
}

function attachTerm(name) {
  detachTerm();
  ensureTerm();
  T.name = name;
  T.term.reset();
  setTimeout(() => T.fit?.fit(), 0);
  const wsUrl = new URL(appUrl('api/term/attach'));
  wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cols = T.term.cols || 120, rows = T.term.rows || termRows();
  wsUrl.search = `name=${encodeURIComponent(name)}&cols=${cols}&rows=${rows}`;
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  T.ws = ws;
  const dec = new TextDecoder();
  ws.onmessage = e => {
    let s = typeof e.data === 'string' ? e.data : dec.decode(e.data);
    if (T.localMouse) s = s.replace(MOUSE_ON, '');   // 别让应用把鼠标要回去
    T.term.write(s);
  };
  ws.onopen = () => {
    setTermStatus(`已接管 · ${name}`, true, name);
    fitTerm();
    setScrollPos(0);
    if (T.localMouse) T.term.write(MOUSE_OFF);
    T.term.focus();
  };
  ws.onclose = () => {
    if (T.ws !== ws) return;             // 用户主动收起时 detachTerm 已经清掉引用
    setTermStatus('已断开', false);
    T.ws = null;
    pollLive(true);                      // tmux 内程序退出时立即清理绿点和输入区
  };
  ws.onerror = () => setTermStatus('连接失败', false);
}

// ---- 滚轮翻历史 ----
let _wheelAcc = 0, _wheelTimer = null, _wheelSeq = 0, _resumeInput = null;
const _wheelRequests = new Set();

function wheelBy(deltaY) {
  _wheelAcc += deltaY;
  if (_wheelTimer) return;
  _wheelTimer = setTimeout(async () => {
    _wheelTimer = null;
    const lines = Math.max(1, Math.min(30, Math.round(Math.abs(_wheelAcc) / 40)));
    const up = _wheelAcc < 0;
    _wheelAcc = 0;
    const seq = ++_wheelSeq;
    const req = post('api/term/scroll', { name: T.name, up, lines });
    _wheelRequests.add(req);
    let d;
    try { d = await req; }
    finally { _wheelRequests.delete(req); }
    if (seq !== _wheelSeq) return;       // 期间已经退出滚动了, 这个响应作废
    if (typeof d.pos === 'number') setScrollPos(d.pos);
  }, 40);
}

/** 退出滚动状态: 连带作废还没发出去和还在路上的滚动请求, 否则它们落地后
 *  会把 tmux 又推回 copy-mode。 */
function abortWheel() {
  _wheelSeq++;
  clearTimeout(_wheelTimer);
  _wheelTimer = null;
  _wheelAcc = 0;
}

function setScrollPos(n) {
  T.scrollPos = n;
  const b = $('#tscroll');
  if (!b) return;
  b.classList.toggle('on', n > 0);
  b.textContent = n > 0 ? `↑ 已上翻 ${n} 行 · 点此回到最新` : '';
}

async function leaveScroll() {
  if (!T.name) return;
  abortWheel();
  // 一路 scroll-down 不靠谱(tmux 不吃很大的 -N), 直接退出 copy-mode 就回到实时画面
  await post('api/term/scroll', { name: T.name, cancel: true });
  setScrollPos(0);
}

// ---- 鼠标: 交给应用 还是 用来框选 ----
function setLocalMouse(on) {
  T.localMouse = on;
  store.set('tmouse', on);
  const b = $('#tmouse');
  if (b) {
    b.classList.toggle('on', on);
    b.title = on ? '鼠标用于框选复制（点击切回交给应用）' : '鼠标交给应用（点击改为框选复制）';
    b.setAttribute('aria-label', on ? '关闭框选复制，把鼠标交给应用' : '启用框选复制');
    b.setAttribute('aria-pressed', String(on));
  }
  if (!T.term) return;
  if (on) T.term.write(MOUSE_OFF);        // 直接告诉 xterm: 应用不要鼠标了
  else if (T.name) attachTerm(T.name);    // 恢复应用的真实状态最省事的办法是重连
}

function detachTerm() {
  if (T.ws) { try { T.ws.close(); } catch {} T.ws = null; }
  T.name = null;
  setTermStatus('未连接', false);
}

async function stopTermSession() {
  if (!T.name) return;
  if (!confirm(`结束「${T.name}」？\n\n里面运行的 CLI 会被终止，会话记录保留。`)) return;
  const name = T.name;
  closeTermPane();
  await post('api/term/kill', { name });
  await loadTermList();
  renderTakeoverBtn();
  renderComposer();
}

function setTermStatus(text, on, mobileText) {
  const s = $('#tstatus');
  if (s) {
    s.textContent = text;
    s.classList.toggle('on', on);
    if (mobileText == null) delete s.dataset.mobileText;
    else s.dataset.mobileText = mobileText;
  }
}

// ---------------------------------------------------------------- 输入框
// 已接管的会话在消息流底部给个输入框, 不必展开整个终端就能说话。
function renderComposer() {
  const name = T.enabled ? takenOver(S.sel) : null;
  const box = $('#composer');
  box.classList.toggle('hidden', !name);
  if (name) autoGrow($('#cinput'));
}

function autoGrow(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(180, Math.max(34, ta.scrollHeight)) + 'px';
}

async function sendToSession(text, keys) {
  const name = takenOver(S.sel);
  if (!name) return;
  const d = await post('api/term/send', keys ? { name, keys } : { name, text });
  if (d.error) return alert('发送失败: ' + d.error);
  S.live.add(S.sel);            // 发完立刻按最快节奏拉新消息
  S.liveTmux.add(S.sel);
  paintLive();
  S.syncGap = FAST_MIN;
  S.lastSync = 0;
}

$('#cinput').addEventListener('input', e => autoGrow(e.target));
$('#cinput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const ta = e.target;
    const text = ta.value;
    if (!text.trim()) return;
    ta.value = '';
    autoGrow(ta);
    sendToSession(text);
  }
});
$('#csend').onclick = () => {
  const ta = $('#cinput');
  if (!ta.value.trim()) return;
  const text = ta.value;
  ta.value = '';
  autoGrow(ta);
  sendToSession(text);
};
$('#cesc').onclick = () => sendToSession(null, ['Escape']);
$('.term-keys').onclick = e => {
  const b = e.target.closest('[data-term-key]');
  if (b) sendToSession(null, [b.dataset.termKey]);
};

// ---- 高度拖动 ----
let tdrag = false;
let tdragPointer = null;
let tdragTopSnap = 48;
$('#tgrip').addEventListener('pointerdown', e => {
  tdrag = true;
  tdragPointer = e.pointerId;
  const composer = $('#composer');
  tdragTopSnap = Math.max(48, composer.offsetHeight || 0);
  e.currentTarget.setPointerCapture?.(e.pointerId);
  document.body.classList.add('dragging-v');
  e.preventDefault();
});
document.addEventListener('pointermove', e => {
  if (!tdrag || e.pointerId !== tdragPointer) return;
  const right = $('#right');
  const top = right.getBoundingClientRect().top;
  const y = Math.max(0, Math.min(right.clientHeight, e.clientY - top));
  const h = right.clientHeight - y;
  if (h <= 32) {
    T.mode = 'collapsed';
  } else if (y <= tdragTopSnap) {
    T.mode = 'full';
  } else {
    T.mode = 'normal';
    T.height = Math.round(h);
  }
  layoutTermPane();
});
function finishTermDrag(e) {
  if (!tdrag || e.pointerId !== tdragPointer) return;
  tdrag = false;
  tdragPointer = null;
  document.body.classList.remove('dragging-v');
  store.set('termh', T.height);
  store.set('termmode', T.mode);
  renderTakeoverBtn();
  fitTerm();
}
document.addEventListener('pointerup', finishTermDrag);
document.addEventListener('pointercancel', finishTermDrag);

$('#tmouse').onclick = () => setLocalMouse(!T.localMouse);
$('#tscroll').onclick = () => leaveScroll();
$('#tchat').onclick = () => closeTermPane();
$('#tstop').onclick = () => stopTermSession();

loadTermList();
