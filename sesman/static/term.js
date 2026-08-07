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
  try {
    const d = await (await fetch('/api/term/list')).json();
    T.enabled = !!d.enabled;
    T.list = d.sessions || [];
  } catch {
    T.enabled = false;
    T.list = [];
  }
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
    let d = await post('/api/term/takeover', { uid, cols: 120, rows: termRows() });
    if (d.needs_confirm) {
      const n = (d.pids || []).length;
      const ok = confirm(
        `这个会话正在运行中（${n} 个进程），而且不在 tmux 里，无法直接接入。\n\n`
        + `接管会先结束正在运行的实例，再用 tmux 重新打开它。\n`
        + `未保存的输入会丢失，已完成的对话不受影响。\n\n继续吗？`);
      if (!ok) return;
      setBtn('结束旧实例…', true);
      d = await post('/api/term/takeover', { uid, force: true, cols: 120, rows: termRows() });
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
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

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
      const job = before.then(() => post('/api/term/send', { name, text: d, enter: false }));
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
    pane.style.setProperty('--mobile-terminal-top', `${$('.dhead')?.offsetHeight || 0}px`);
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
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const cols = T.term.cols || 120, rows = T.term.rows || termRows();
  const ws = new WebSocket(`${proto}://${location.host}/api/term/attach`
    + `?name=${encodeURIComponent(name)}&cols=${cols}&rows=${rows}`);
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
    const req = post('/api/term/scroll', { name: T.name, up, lines });
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
  await post('/api/term/scroll', { name: T.name, cancel: true });
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
  await post('/api/term/kill', { name });
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
  const d = await post('/api/term/send', keys ? { name, keys } : { name, text });
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
$('#tstop').onclick = () => stopTermSession();

loadTermList();
