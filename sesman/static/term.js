'use strict';

// 接管会话: 在服务端把它用 tmux resume 起来, 然后把终端嵌在会话详情底部。
// 会话跑在 tmux 里, 所以关掉页面/重启 sesman 都不会打断它。
const TERM_RENDER_BATCH_MS = 20;
const TERM_RENDER_BATCH_MAX = 32 * 1024;

const T = {
  term: null,      // xterm 实例
  ws: null,
  name: null,      // 当前挂着的 tmux 会话名
  uid: null,       // 对应的 sesman 会话
  views: new Map(), // 已打开过且仍存活的 tmux → xterm/WebSocket；切会话只隐藏
  enabled: false,
  height: store.get('termh', 320),
  mode: store.get('termmode', 'normal'), // normal | collapsed | full
  localMouse: store.get('tmouse', false),   // true = 鼠标归浏览器, 可以框选复制
  ctrlArmed: false,                         // 手机 Ctrl / 桌面右 Ctrl：只修饰下一次输入
  sources: {},
  home: '',
  pending: [],
  pendingModes: new Map(), // 临时会话名 → 打开前的终端布局；退出未落盘时恢复
  resolving: new Set(),
  resolveControllers: new Map(),
  openViews: new Map(store.get('termviews', [])), // tmux 名 → {mode, height}
};

// 应用(claude/codex 的 TUI)申请接管鼠标的那些序列。选择模式下要拦掉,
// 否则 xterm 会把拖拽当成给应用的鼠标事件, 没法框选。
const MOUSE_ON = /\x1b\[\?(1000|1002|1003|1005|1006|1015)h/g;
const MOUSE_OFF = '\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l';

function termTheme() {
  const css = getComputedStyle(document.querySelector('#xterm') || document.documentElement);
  const read = name => css.getPropertyValue(name).trim();
  return {
    background: read('--terminal-bg'), foreground: read('--terminal-fg'),
    cursor: read('--terminal-cursor'), selectionBackground: read('--terminal-selection'),
    black: read('--terminal-black'), red: read('--terminal-red'),
    green: read('--terminal-green'), yellow: read('--terminal-yellow'),
    blue: read('--terminal-blue'), magenta: read('--terminal-magenta'),
    cyan: read('--terminal-cyan'), white: read('--terminal-white'),
    brightBlack: read('--terminal-bright-black'), brightRed: read('--terminal-bright-red'),
    brightGreen: read('--terminal-bright-green'), brightYellow: read('--terminal-bright-yellow'),
    brightBlue: read('--terminal-bright-blue'), brightMagenta: read('--terminal-bright-magenta'),
    brightCyan: read('--terminal-bright-cyan'), brightWhite: read('--terminal-bright-white'),
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

function reflectedLightRgb(r, g, b, background = false) {
  r /= 255; g /= 255; b /= 255;
  const hi = Math.max(r, g, b), lo = Math.min(r, g, b);
  const sourceLight = (hi + lo) / 2;
  let h = 0, s = 0;
  if (hi !== lo) {
    const d = hi - lo;
    s = d / (1 - Math.abs(2 * sourceLight - 1));
    if (hi === r) h = ((g - b) / d) % 6;
    else if (hi === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
  }
  // 保持色相、反射亮度；轻微曲线把中间色拉回 50%，避免彩色文字过艳。
  const reflected = 1 - sourceLight;
  const sign = Math.sign(reflected - .5);
  let l = .5 + sign * .5 * Math.pow(Math.abs(reflected - .5) / .5, 1.35);
  if (background) l = Math.min(l, .96);   // 显式黑底随亮色方案恢复为接近白色
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let rr = 0, gg = 0, bb = 0;
  if (h < 60) [rr, gg, bb] = [c, x, 0];
  else if (h < 120) [rr, gg, bb] = [x, c, 0];
  else if (h < 180) [rr, gg, bb] = [0, c, x];
  else if (h < 240) [rr, gg, bb] = [0, x, c];
  else if (h < 300) [rr, gg, bb] = [x, 0, c];
  else [rr, gg, bb] = [c, 0, x];
  return [rr, gg, bb].map(v => Math.max(0, Math.min(255, Math.round((v + m) * 255))));
}

function indexedTerminalRgb(n) {
  if (n >= 232 && n <= 255) {
    const v = 8 + (n - 232) * 10;
    return [v, v, v];
  }
  if (n < 16 || n > 231) return null;    // 前 16 色直接由 xterm theme 精确控制
  const steps = [0, 95, 135, 175, 215, 255];
  n -= 16;
  return [steps[Math.floor(n / 36)], steps[Math.floor(n / 6) % 6], steps[n % 6]];
}

function lightTerminalAnsi(s) {
  const rgb = (kind, r, g, b) => {
    const out = reflectedLightRgb(+r, +g, +b, kind === '48');
    return `${kind};2;${out.join(';')}`;
  };
  s = s.replace(/(38|48);2;(\d{1,3});(\d{1,3});(\d{1,3})/g, (_, ...v) => rgb(...v.slice(0, 4)));
  s = s.replace(/(38|48):2(?::\d*)?:(\d{1,3}):(\d{1,3}):(\d{1,3})/g,
    (_, ...v) => rgb(...v.slice(0, 4)));
  return s.replace(/(38|48);5;(\d{1,3})/g, (all, kind, value) => {
    const source = indexedTerminalRgb(+value);
    if (!source) return all;
    return rgb(kind, ...source);
  });
}

function terminalColorChunk(view, s) {
  s = (view.ansiTail || '') + s;
  view.ansiTail = '';
  if (document.documentElement.dataset.theme !== 'light') return s;
  // PTY/WebSocket 可能恰好在 CSI 中间断包，留下不完整尾巴等下一块再处理。
  const tail = s.match(/\x1b\[[0-9;:]*$/)?.[0] || '';
  if (tail) {
    view.ansiTail = tail;
    s = s.slice(0, -tail.length);
  }
  return lightTerminalAnsi(s);
}

async function refreshTerminalPreferences(redraw = false) {
  try { await document.fonts?.load(`${termFontSize()}px ${termFont()}`, 'MW0il'); } catch {}
  const views = T.views ? T.views.values() : (T.term ? [{ term: T.term }] : []);
  const redrawNames = [];
  for (const view of views) {
    view.term.options.fontFamily = termFont();
    view.term.options.theme = termTheme();
    if (redraw && view.name) redrawNames.push(view.name);
  }
  for (const name of redrawNames) attachTerm(name);
  if (redraw && !redrawNames.length && T.name) attachTerm(T.name);
  setTimeout(() => { fitTerm(); }, 0);
}

// xterm 先测量网页字体再创建 DOM 行，避免按回退字体计算出错误的字符宽度。
const terminalFontReady = document.fonts
  ? document.fonts.load(`${termFontSize()}px ${termFont()}`, 'MW0il')
  : Promise.resolve();
addEventListener('resize', () => { layoutTermPane(); fitTerm(); });

async function loadTermList() {
  const fingerprint = () => [
    ...(T.list || []).map(x => `${x.name}\t${x.cwd}`),
    ...(T.pending || []).map(x => `pending\t${x.name}\t${x.cwd}`),
  ].join('\n');
  const before = fingerprint();
  let loaded = false;
  try {
    const d = await (await fetch(appUrl('api/term/list'))).json();
    loaded = true;
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
  if (loaded) {
    const valid = new Set([...T.list, ...T.pending].map(x => x.name));
    const kept = new Map([...T.openViews].filter(([name]) => valid.has(name)));
    if (kept.size !== T.openViews.size) {
      T.openViews = kept;
      store.set('termviews', [...kept]);
    }
    for (const name of T.views.keys()) {
      if (!valid.has(name)) disposeTermView(name);
    }
    // tmux 结束后，对应的消息缓存才重新回到普通 LRU 容量池。
    if (typeof trimCache === 'function') trimCache();
  }
  $('#new-session')?.classList.toggle('hidden', !T.enabled);
  // app.js 先于体积较大的终端库执行。若详情已在终端库就绪前打开，
  // 重新生成一次标题栏，把接管/切换入口补上。
  const current = typeof cache !== 'undefined' ? cache.get(viewKey(S.sel, S.agent)) : null;
  const oldHead = $('#detail > .dhead');
  if (T.enabled && current && oldHead && !S.agent && !oldHead.querySelector('#a-term')) {
    oldHead.replaceWith(head(current.meta, messageCount(current.msgs)));
  }
  const after = fingerprint();
  if (after !== before && S.sig && typeof renderSide === 'function') {
    const side = $('#side'), top = side?.scrollTop || 0;
    renderChips();
    if (!S.results) showSessionCount(sidebarSessions().length);
    renderSide();
    if (side) side.scrollTop = top;
    paintLive();
  }
  // 刷新页面后仍从持久化 meta 恢复关联轮询；Set 防止重复启动。
  for (const pending of pendingTmuxSessions()) resolveNewSession(pending);
  restoreTermPane(S.sel, S.agent);
}

/** 某个会话是否已经被接管 (存在对应的 tmux 会话)。 */
function takenOver(uid) {
  if (String(uid || '').startsWith('tmux:')) {
    const name = String(uid).slice(5);
    return [...(T.list || []), ...(T.pending || [])].some(x => x.name === name) ? name : null;
  }
  const s = S.sessions.find(x => x.uid === uid);
  if (!s || !T.list) return null;
  const name = `sesman-${s.source}-${String(s.sid).slice(0, 8)}`;
  return T.list.find(x => x.uid === uid)?.name
    || (T.list.some(x => x.name === name) ? name : null);
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
  if (!T.pendingModes.has(info.name)) T.pendingModes.set(info.name, T.mode);
  // create 返回后 term/list 可能还没拉完；先把服务端刚确认的新 tmux 放进本地
  // pending，详情页的终端切换、输入框和附件可以立即使用。
  if (!T.pending.some(x => x.name === info.name)) T.pending.push({ ...info, started: Date.now() / 1000 });
  S.results = null;
  S.term = '';
  $('#q').value = '';
  S.sel = pendingUid(info.name);
  store.set('sel', S.sel);
  renderSide();
  showSessionCount(sidebarSessions().length);
  const src = SOURCES[info.source];
  $('#detail').innerHTML = `<div class="dhead"><div class="dtitle">
    <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
    <h2>${icon(info.source)}<span>新建 ${esc(src.name)} 会话</span></h2>
    <div class="dhead-actions" aria-label="会话操作">
      <span class="mobile-msg-summary"><span class="mobile-msg-count" aria-label="0 条消息">0</span></span>
      <button class="iconbtn" id="a-term" title="切换到终端" aria-label="切换到终端">${uiIcon('terminal')}</button>
      <button class="iconbtn danger" id="a-session-action" title="停止会话" aria-label="停止会话">${uiIcon('power')}</button>
    </div></div>
    <div class="dmeta"><span class="meta-source">${esc(src.name)}</span><span id="mcount-total">0 条消息</span>
      <span id="dlive" class="dlive on tmux" title="运行于 tmux" aria-label="运行于 tmux">●</span>
      <span class="meta-secondary"><code>${esc(info.cwd)}</code></span></div>
  </div><div class="empty new-session-wait">终端已启动，正在等待会话记录落盘…</div>`;
  $('#detail .mobile-back').onclick = showMobileList;
  $('#a-term').onclick = () => {
    T.uid = S.sel;
    toggleTermPane(info.name);
  };
  $('#a-session-action').onclick = () => stopPendingSession(info, $('#a-session-action'));
  showMobileDetail();
  T.uid = S.sel;
  if (!MOBILE.matches) T.mode = 'full';   // 手机本来就是终端覆盖层，不污染桌面保存的高度模式
  renderComposer();
  renderTakeoverBtn();
}

async function stopPendingSession(info, button) {
  if (!confirm(`停止并移除「新建 ${SOURCES[info.source].name} 会话」?`)) return;
  button.disabled = true;
  try {
    T.resolveControllers.get(info.name)?.abort();
    const d = await post('api/term/kill', { name: info.name });
    if (d.error) return alert('停止失败: ' + d.error);
    const uid = pendingUid(info.name);
    if (S.sel === uid) {
      const previousMode = T.pendingModes.get(info.name);
      if (['normal', 'collapsed', 'full'].includes(previousMode)) T.mode = previousMode;
      closeTermPane();
      S.sel = null;
      store.set('sel', null);
      $('#composer').classList.add('hidden');
      $('#detail').innerHTML = '<div class="empty">会话已停止</div>';
      showMobileList();
    }
    T.pendingModes.delete(info.name);
    await loadTermList();
    renderSide();
    showSessionCount(sidebarSessions().length);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function openPendingSession(info) {
  const pending = { ...info, name: info.tmuxName || info.name };
  showNewSessionStage(pending);
  await openTermPane(pending.name);
  resolveNewSession(pending);
}

function discardAbandonedNewSession(info) {
  const uid = pendingUid(info.name);
  T.pending = (T.pending || []).filter(x => x.name !== info.name);
  T.openViews.delete(info.name);
  store.set('termviews', [...T.openViews]);
  disposeTermView(info.name);

  const draft = composerDrafts.get(uid);
  for (const attachment of draft?.attachments || []) {
    if (attachment.preview) URL.revokeObjectURL(attachment.preview);
  }
  composerDrafts.delete(uid);
  if (composerUid === uid) composerUid = null;

  if (S.sel === uid) {
    const previousMode = T.pendingModes.get(info.name);
    if (['normal', 'collapsed', 'full'].includes(previousMode)) T.mode = previousMode;
    S.sel = null;
    T.uid = null;
    store.set('sel', null);
    $('#composer').classList.add('hidden');
    $('#detail').innerHTML = '<div class="empty">从左侧选择一个会话</div>';
    showMobileList();
  }
  T.pendingModes.delete(info.name);
  renderSide();
  showSessionCount(sidebarSessions().length);
  paintLive();
}

async function resolveNewSession(info) {
  const pendingId = pendingUid(info.name);
  if (T.resolving.has(info.name)) return;
  T.resolving.add(info.name);
  const controller = new AbortController();
  T.resolveControllers.set(info.name, controller);
  try {
    for (let i = 0; i < 160; i++) {         // TUI 等用户首次输入时可能较久，最多等两分钟
      await new Promise(r => setTimeout(r, 750));
      let d;
      try {
        const response = await fetch(appUrl(`api/term/new-status?name=${encodeURIComponent(info.name)}`),
          { signal: controller.signal });
        d = await response.json();
      } catch {
        if (controller.signal.aborted) return;
        continue;
      }
      // 另一浏览器可能已经先清理了同一临时记录；gone 与本页观察到
      // exited 的收尾动作完全相同，不能退化成一个关联失败的孤儿页。
      if (d.exited || d.gone) {
        discardAbandonedNewSession(info);
        return;
      }
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
      migrateComposerDraft(pendingId, d.uid);
      T.uid = d.uid;
      if (d.running) {
        S.live.add(d.uid);
        S.liveTmux.add(d.uid);
      }
      await openSession(d.uid);
      if (d.running) await openTermPane(d.name);
      else closeTermPane();
      T.pendingModes.delete(info.name);
      paintLive();
      return;
    }
    const wait = $('.new-session-wait');
    if (wait && S.sel === pendingId) wait.textContent = '会话仍在终端中运行；产生首条记录后会出现在列表里';
  } finally {
    T.resolving.delete(info.name);
    if (T.resolveControllers.get(info.name) === controller) T.resolveControllers.delete(info.name);
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

const termRows = () => Math.max(10, Math.floor(T.height / (termFontSize() * 1.31)));

/** 详情页头部那个按钮的文案随状态变。 */
function renderTakeoverBtn() {
  const b = $('#a-term');
  if (!b) return;
  const name = takenOver(S.sel);
  const paneOpen = !!name && !$('#termpane').classList.contains('hidden');
  const mobileSwitch = MOBILE.matches && paneOpen;
  const snapped = !MOBILE.matches && paneOpen && (T.mode === 'collapsed' || T.mode === 'full');
  const terminalVisible = paneOpen && (MOBILE.matches || T.mode !== 'collapsed');
  const label = !name ? '接管会话'
    : MOBILE.matches ? (paneOpen ? '切换到对话' : '切换到终端')
    : snapped ? (T.mode === 'collapsed' ? '切换到终端' : '切换到对话')
      : (paneOpen ? '收起终端' : '展开终端');
  b.innerHTML = uiIcon(mobileSwitch ? 'chat' : 'terminal');
  b.title = b.ariaLabel = label;
  b.setAttribute('aria-expanded', String(terminalVisible));
  b.classList.toggle('on', !!name);
  b.classList.toggle('session-live', S.live.has(S.sel));
  b.classList.toggle('session-tmux', S.liveTmux.has(S.sel));
  renderTermMouseButton(name, b);
  renderComposer();
}

/** 终端不再另设状态栏；框选开关跟切换/停止按钮共用会话顶栏。 */
function renderTermMouseButton(name, takeoverButton = $('#a-term')) {
  let b = $('#tmouse');
  if (!name || !takeoverButton) {
    b?.remove();
    return;
  }
  if (!b) {
    b = document.createElement('button');
    b.className = 'iconbtn';
    b.id = 'tmouse';
    b.innerHTML = uiIcon('select');
    takeoverButton.after(b);
    b.onclick = () => setLocalMouse(!T.localMouse);
  }
  paintTermMouseButton(b);
}

// ---------------------------------------------------------------- 终端面板
function currentTermViewObject() {
  return T.name ? T.views.get(T.name) || null : null;
}

function syncTermAliases(view = null) {
  T.term = view?.term || null;
  T.ws = view?.ws || null;
}

function activateTermView(view) {
  for (const cached of T.views.values()) cached.host.hidden = cached !== view;
  view.host.hidden = false;
  T.name = view.name;
  syncTermAliases(view);
  setScrollPos(view.scrollPos);
}

function legacyCopyText(text, term) {
  const input = document.createElement('textarea');
  input.value = text;
  input.setAttribute('readonly', '');
  Object.assign(input.style, {
    position: 'fixed', left: '-10000px', top: '0', opacity: '0',
  });
  document.body.appendChild(input);
  input.select();
  try { document.execCommand('copy'); } finally {
    input.remove();
    term.focus();
  }
}

function copyTermSelection(term) {
  const text = term.getSelection();
  if (!text) return false;
  try {
    const copying = navigator.clipboard?.writeText(text);
    if (copying) copying.catch(() => legacyCopyText(text, term));
    else legacyCopyText(text, term);
  } catch {
    legacyCopyText(text, term);
  }
  return true;
}

function rememberTermSelection(view) {
  const position = view.term.getSelectionPosition();
  if (!position || !view.term.getSelection()) return;
  view.selectionSnapshot = {
    start: { ...position.start }, end: { ...position.end },
  };
}

function restoreTermSelection(view) {
  if (!view.selectionLocked || !view.selectionSnapshot || view.term.hasSelection()
      || view.restoringSelection) return;
  view.restoringSelection = true;
  queueMicrotask(() => {
    try {
      if (!view.selectionLocked || view.term.hasSelection()) return;
      const { start, end } = view.selectionSnapshot;
      const length = (end.y - start.y) * view.term.cols - start.x + end.x;
      if (length > 0) view.term.select(start.x, start.y, length);
    } catch {
      // scrollback 已被裁掉时旧坐标可能失效，此时正常放弃锁定。
      view.selectionLocked = false;
      view.selectionSnapshot = null;
    } finally {
      view.restoringSelection = false;
    }
  });
}

function ensureTerm(name) {
  let view = T.views.get(name);
  if (view) return view;
  const host = el('div', 'xterm-view');
  host.hidden = true;
  $('#xterm').appendChild(host);
  const term = new Terminal({
    fontFamily: termFont(),
    fontSize: termFontSize(), fontWeight: '400', fontWeightBold: '600',
    cursorBlink: true, scrollback: 10000,
    scrollOnUserInput: true, theme: termTheme(),
  });
  const fit = new FitAddon.FitAddon();
  view = {
    name, host, term, fit, ws: null, reconnectTimer: null,
    reconnectDelay: 500, scrollPos: 0, ansiTail: '',
    outputBuffer: '', outputTimer: null, fitFrame: null,
    lastResizeKey: '', lastResizeWs: null,
    selectionLocked: false, selectionSnapshot: null, restoringSelection: false,
  };
  T.views.set(name, view);
  term.loadAddon(fit);
  term.open(host);
  host.addEventListener('mousedown', e => {
    const selecting = e.shiftKey || T.localMouse;
    view.selectionLocked = selecting;
    if (selecting) view.selectionSnapshot = null;
  }, true);
  term.onSelectionChange(() => {
    if (term.hasSelection()) rememberTermSelection(view);
    else restoreTermSelection(view);       // Claude 重绘清选区时，恢复刚才的框选
  });
  term.attachCustomKeyEventHandler(e => {
    if (e.code === 'ControlRight') {
      if (e.type === 'keydown' && !e.repeat) setTermCtrl(true);
      return false;                        // 右 Ctrl 只锁定下一键，不交给 xterm
    }
    const copy = (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'c';
    if (copy && term.hasSelection()) {
      if (e.type === 'keydown' && !e.repeat) copyTermSelection(term);
      return false;                       // 有选区时绝不能把 Ctrl+C 送给 Claude/Codex
    }
    if (e.type === 'keydown' && !['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) {
      view.selectionLocked = false;
      view.selectionSnapshot = null;
    }
    return true;
  });
  term.onData(d => {
    if (T.name !== name) return;
    d = applyTermCtrl(d);
    if (view.ws?.readyState !== 1) return;
    if (view.scrollPos || _wheelRequests.size || _resumeInput) {
      // 等所有已经发出的滚轮请求落地，再由一个服务端请求原子执行
      // 「退出 copy-mode → 写入字符」。直接向 attach 发 q 不可靠，而把
      // cancel 和字符分走 HTTP/WS 两条通道又会乱序。
      abortWheel();
      setScrollPos(0);
      const before = _resumeInput || Promise.allSettled([..._wheelRequests]);
      const job = before.then(() => post('api/term/send', { name, text: d, enter: false }));
      _resumeInput = job;
      job.then(
        () => { if (_resumeInput === job) _resumeInput = null; },
        () => { if (_resumeInput === job) _resumeInput = null; },
      );
      return;
    }
    view.ws.send(new TextEncoder().encode(d));
  });
  // 专用 server 不让 tmux 接管滚动：外层不进 alternate screen，直接使用
  // xterm 的正常 scrollback。改造前遗留在默认 server 的会话仍走旧兼容路径。
  term.attachCustomWheelEventHandler(e => {
    if (T.name !== name) return true;
    if (T.list?.find(x => x.name === name)?.server === 'sesman') return true;
    wheelBy(e.deltaY);
    return false;
  });
  return view;
}

function flushTermOutput(view) {
  if (!view) return;
  if (view.outputTimer) clearTimeout(view.outputTimer);
  view.outputTimer = null;
  let s = view.outputBuffer;
  view.outputBuffer = '';
  if (!s) return;
  if (T.localMouse) s = s.replace(MOUSE_ON, '');
  view.term.write(terminalColorChunk(view, s));
}

function queueTermOutput(view, chunk) {
  if (!chunk) return;
  view.outputBuffer += chunk;
  // 重连时可能一次回放上万行历史；大块数据立即交给 xterm 分片解析，不能让
  // 随后的实时输入输出排在一个巨型合帧后面。
  if (view.outputBuffer.length >= TERM_RENDER_BATCH_MAX) {
    flushTermOutput(view);
    return;
  }
  if (view.outputTimer) return;
  // Claude TUI 的一次重画常拆成多个 PTY 包（先清行、再写新内容）。合并到同一
  // 浏览器帧，避免把清除后的中间态画出来，看上去像终端忽宽忽窄。
  view.outputTimer = setTimeout(() => flushTermOutput(view), TERM_RENDER_BATCH_MS);
}

function clearTermOutput(view) {
  if (!view) return;
  if (view.outputTimer) clearTimeout(view.outputTimer);
  view.outputTimer = null;
  view.outputBuffer = '';
}

function performTermFit(view) {
  if (!view || view !== currentTermViewObject()
      || $('#termpane').classList.contains('hidden')) return;
  let dimensions;
  try { dimensions = view.fit.proposeDimensions(); } catch { return; }
  if (!dimensions || !Number.isFinite(dimensions.cols) || !Number.isFinite(dimensions.rows)) return;
  // FitAddon.fit() 会先调用私有 _renderService.clear()，DOM renderer 因而在每次
  // 窗口缩放时先变空再重画。直接使用公开 resize API 保留旧行，并平滑增删行列。
  if (view.term.cols !== dimensions.cols || view.term.rows !== dimensions.rows) {
    view.term.resize(dimensions.cols, dimensions.rows);
  }
  const ws = view.ws;
  const key = `${view.term.cols}x${view.term.rows}`;
  // 同一个 socket 的相同尺寸不再反复通知 tmux，避免 TUI 收到无效 SIGWINCH。
  if (ws?.readyState === 1 && (view.lastResizeWs !== ws || view.lastResizeKey !== key)) {
    ws.send(JSON.stringify({ t: 'resize', cols: view.term.cols, rows: view.term.rows }));
    view.lastResizeWs = ws;
    view.lastResizeKey = key;
  }
}

function fitTerm(immediate = false) {
  const view = currentTermViewObject();
  if (!view || $('#termpane').classList.contains('hidden')) return;
  if (view.fitFrame) cancelAnimationFrame(view.fitFrame);
  view.fitFrame = null;
  if (immediate) {
    performTermFit(view);
    return;
  }
  // 浏览器最大化、拖边界和软键盘动画都会连续发 resize；每个动画帧最多 fit
  // 一次，既跟手又不在同一帧重复测量和重排。
  view.fitFrame = requestAnimationFrame(() => {
    view.fitFrame = null;
    performTermFit(view);
  });
}

function currentTermView() {
  return { mode: T.mode, height: T.height };
}

function rememberTermLayout(name = T.name) {
  if (!name || !T.openViews.has(name)) return;
  T.openViews.set(name, currentTermView());
  store.set('termviews', [...T.openViews]);
}

function rememberTermOpen(name, open) {
  if (!name) return;
  const changed = open ? !T.openViews.has(name) : T.openViews.has(name);
  if (!changed) return;
  if (open) T.openViews.set(name, currentTermView());
  else T.openViews.delete(name);
  store.set('termviews', [...T.openViews]);
}

function restoreTermPane(uid, agent = null) {
  if (!uid || agent || S.sel !== uid || !T.enabled || !$('#a-term')) return;
  const name = takenOver(uid);
  if (!name || !T.openViews.has(name)) return;
  T.uid = uid;
  openTermPane(name);
}

async function openTermPane(name) {
  const saved = T.openViews.get(name);
  if (saved) {
    if (['normal', 'collapsed', 'full'].includes(saved.mode)) T.mode = saved.mode;
    if (Number.isFinite(saved.height) && saved.height > 0) T.height = saved.height;
  }
  rememberTermOpen(name, true);
  const pane = $('#termpane');
  pane.classList.remove('hidden');
  layoutTermPane();
  renderTakeoverBtn();
  try { await terminalFontReady; } catch { /* 字体失败时继续用 Consola/monospace */ }
  if (pane.classList.contains('hidden')) return;
  const view = ensureTerm(name);
  activateTermView(view);
  fitTerm(true);                         // 连接前先确定尺寸，避免 80×24 → 实际尺寸的首屏跳变
  if (view.ws?.readyState !== 1) attachTerm(name);
  else view.term.focus();
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
    rememberTermLayout(name);
    layoutTermPane();
    renderTakeoverBtn();
    if (T.mode === 'full') setTimeout(fitTerm, 0);
    return;
  }
  closeTermPane();
}

function closeTermPane(preserveView = false) {
  if (preserveView) rememberTermLayout();
  else rememberTermOpen(T.name, false);
  deactivateTermView();
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
    const rightTop = right.getBoundingClientRect().top;
    const headBottom = $('#detail > .dhead')?.getBoundingClientRect().bottom ?? rightTop;
    pane.style.setProperty('--mobile-terminal-top', `${Math.max(0, Math.round(headBottom - rightTop))}px`);
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
  const view = ensureTerm(name);
  const active = !$('#termpane').classList.contains('hidden') && T.name === name;
  if (active) activateTermView(view);
  cancelTermReconnect(view);
  dropTermSocket(view);
  clearTermOutput(view);
  view.ansiTail = '';
  view.selectionLocked = false;
  view.selectionSnapshot = null;
  view.term.reset();
  setTimeout(() => { if (T.name === name) fitTerm(); }, 0);
  const wsUrl = new URL(appUrl('api/term/attach'));
  wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cols = view.term.cols || 120, rows = view.term.rows || termRows();
  wsUrl.search = `name=${encodeURIComponent(name)}&cols=${cols}&rows=${rows}`;
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  view.ws = ws;
  if (T.name === name) T.ws = ws;
  const dec = new TextDecoder();
  ws.onmessage = e => {
    if (view.ws !== ws) return;           // 已替换连接的尾包不能重画新终端
    const s = typeof e.data === 'string' ? e.data : dec.decode(e.data, { stream: true });
    queueTermOutput(view, s);
  };
  ws.onopen = () => {
    view.reconnectDelay = 500;
    if (T.name === name) {
      syncTermAliases(view);
      fitTerm(true);
      setScrollPos(0);
      if (T.localMouse) view.term.write(MOUSE_OFF);
      view.term.focus();
    }
  };
  ws.onclose = () => {
    if (view.ws !== ws) return;           // 主动换 socket 后，旧 close 事件作废
    queueTermOutput(view, dec.decode());
    flushTermOutput(view);
    view.ws = null;
    if (T.name === name) {
      T.ws = null;
    }
    // 先刷新 tmux 列表再决定是否重连。若进程刚退出，旧 T.list 仍会短暂把它
    // 判为存活；先排一个重连定时器会向已消失的会话握手，产生 404/close race。
    Promise.resolve(pollLive(true)).finally(() => {
      if (T.views.get(name) === view && !view.ws) scheduleTermReconnect(view);
    });
  };
  ws.onerror = () => {};
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
  const view = currentTermViewObject();
  if (view) view.scrollPos = n;
}

// ---- 鼠标: 交给应用 还是 用来框选 ----
function setLocalMouse(on) {
  T.localMouse = on;
  store.set('tmouse', on);
  paintTermMouseButton();
  if (!T.term) return;
  if (on) T.term.write(MOUSE_OFF);        // 直接告诉 xterm: 应用不要鼠标了
  else if (T.name) attachTerm(T.name);    // 恢复应用的真实状态最省事的办法是重连
}

function paintTermMouseButton(b = $('#tmouse')) {
  if (b) {
    b.classList.toggle('on', T.localMouse);
    b.title = T.localMouse ? '鼠标用于框选复制（点击切回交给应用）' : '鼠标交给应用（点击改为框选复制）';
    b.setAttribute('aria-label', T.localMouse ? '关闭框选复制，把鼠标交给应用' : '启用框选复制');
    b.setAttribute('aria-pressed', String(T.localMouse));
  }
}

function cancelTermReconnect(view = currentTermViewObject()) {
  if (!view) return;
  clearTimeout(view.reconnectTimer);
  view.reconnectTimer = null;
}

function dropTermSocket(view = currentTermViewObject()) {
  if (!view) return;
  const ws = view.ws;
  view.ws = null;                        // 先失效引用，close 回调便不会误判成意外断线
  if (T.name === view.name) T.ws = null;
  if (ws) { try { ws.close(); } catch {} }
}

/** 网络短断后自动恢复。tmux 才是会话本体，WebSocket 只是可随时重建的视图。 */
function scheduleTermReconnect(view = currentTermViewObject()) {
  if (!view || document.hidden || !navigator.onLine || view.reconnectTimer) return;
  const stillAlive = [...(T.list || []), ...(T.pending || [])].some(x => x.name === view.name);
  if (!stillAlive) return;
  const delay = view.reconnectDelay;
  view.reconnectTimer = setTimeout(() => {
    view.reconnectTimer = null;
    if (!T.views.has(view.name) || document.hidden) return;
    view.reconnectDelay = Math.min(8000, Math.round(view.reconnectDelay * 1.8));
    attachTerm(view.name);
  }, delay);
}

/** 手机锁屏会冻结一个看似仍 OPEN、实际已经失效的 socket；恢复时必须强制换新。 */
function reconnectTerm(view = currentTermViewObject()) {
  if (!view || document.hidden || !navigator.onLine) return;
  attachTerm(view.name);
  if (T.name === view.name) setTimeout(() => { layoutTermPane(); fitTerm(); }, 20);
}

function suspendTerm() {
  for (const view of T.views.values()) {
    cancelTermReconnect(view);
    dropTermSocket(view);
  }
}

function deactivateTermView() {
  const view = currentTermViewObject();
  if (view) view.host.hidden = true;
  T.name = null;
  syncTermAliases();
  setTermCtrl(false);
}

function disposeTermView(name) {
  const view = T.views.get(name);
  if (!view) return;
  const active = T.name === name;
  cancelTermReconnect(view);
  dropTermSocket(view);
  try { view.term.dispose(); } catch { /* 已被浏览器清理 */ }
  view.host.remove();
  T.views.delete(name);
  if (active) {
    deactivateTermView();
    $('#termpane').classList.add('hidden');
    $('#termpane').classList.remove('term-collapsed');
    $('#right').classList.remove('term-full');
  }
}

// ---------------------------------------------------------------- 输入框
// 已接管的会话在消息流底部给个输入框, 不必展开整个终端就能说话。
const COMPOSER_MAX_FILES = 12;
const COMPOSER_MAX_FILE_BYTES = 512 * 1024 * 1024;
const ATTACH_ACCEPT = { image: 'image/*', video: 'video/*', audio: 'audio/*', file: '' };
const composerDrafts = new Map();
let composerUid = null;
let composerDraftSeq = 0;
let lastMessageSelection = '';
let lastMessageSelectionUid = null;

const newComposerDraft = () => ({ text: '', attachments: [], quotes: [], nextAttachmentNumber: 1 });
function composerDraft(uid = composerUid, create = true) {
  if (!uid) return null;
  if (!composerDrafts.has(uid) && create) composerDrafts.set(uid, newComposerDraft());
  const draft = composerDrafts.get(uid) || null;
  if (draft) ensureComposerAttachmentNumbers(draft);
  return draft;
}

function ensureComposerAttachmentNumbers(draft) {
  draft.attachments ||= [];
  const used = new Set();
  let next = Number.isInteger(draft.nextAttachmentNumber) && draft.nextAttachmentNumber > 0
    ? draft.nextAttachmentNumber : 1;
  for (const attachment of draft.attachments) {
    if (!Number.isInteger(attachment.number) || attachment.number < 1 || used.has(attachment.number)) {
      while (used.has(next)) next++;
      attachment.number = next++;
    }
    used.add(attachment.number);
    next = Math.max(next, attachment.number + 1);
  }
  draft.nextAttachmentNumber = next;
  return draft;
}

function remapAttachmentReferences(text, remap) {
  if (!remap.size) return text;
  return String(text || '').replace(/\[附件([1-9]\d*)\]/g, (token, raw) => {
    const number = remap.get(Number(raw));
    return number ? `[附件${number}]` : token;
  });
}

function migrateComposerDraft(fromUid, toUid) {
  if (!fromUid || !toUid || fromUid === toUid) return;
  if (typeof migrateQueuedMessages === 'function') migrateQueuedMessages(fromUid, toUid);
  const draft = composerDrafts.get(fromUid);
  if (!draft) return;
  const target = composerDrafts.get(toUid);
  if (target) {
    ensureComposerAttachmentNumbers(target);
    ensureComposerAttachmentNumbers(draft);
    const used = new Set(target.attachments.map(x => x.number));
    const remap = new Map();
    for (const attachment of draft.attachments) {
      if (used.has(attachment.number)) {
        let number = target.nextAttachmentNumber;
        while (used.has(number)) number++;
        remap.set(attachment.number, number);
        attachment.number = number;
        target.nextAttachmentNumber = number + 1;
      }
      used.add(attachment.number);
    }
    draft.text = remapAttachmentReferences(draft.text, remap);
    if (draft.text) target.text = target.text ? `${target.text}\n${draft.text}` : draft.text;
    target.attachments.push(...draft.attachments);
    target.quotes.push(...draft.quotes);
    ensureComposerAttachmentNumbers(target);
  } else {
    ensureComposerAttachmentNumbers(draft);
    composerDrafts.set(toUid, draft);
  }
  for (const attachment of draft.attachments) {
    // pending 与正式会话只有 cwd 一致时才会关联，已经落盘的路径仍然有效。
    if (attachment.uploaded?.uid === fromUid) attachment.uploaded.uid = toUid;
  }
  composerDrafts.delete(fromUid);
  if (composerUid === fromUid) composerUid = toUid;
}

function switchComposerDraft(uid) {
  const ta = $('#cinput');
  if (composerUid && ta) composerDraft(composerUid).text = ta.value;
  if (composerUid === uid) return;
  composerUid = uid;
  const draft = composerDraft(uid, !!uid);
  ta.value = draft?.text || '';
  renderComposerItems();
  autoGrow(ta);
}

function renderComposer() {
  const name = T.enabled ? takenOver(S.sel) : null;
  const box = $('#composer');
  box.classList.toggle('hidden', !name);
  switchComposerDraft(name ? S.sel : null);
  if (name) syncComposerMode();
}

function autoGrow(ta) {
  ta.style.height = 'auto';
  const wanted = ta.scrollHeight;
  ta.style.height = Math.min(180, Math.max(36, wanted)) + 'px';
  ta.style.overflowY = wanted > 180 ? 'auto' : 'hidden';
}

function syncComposerMode() {
  const ta = $('#cinput');
  ta.placeholder = MOBILE.matches
    ? '输入内容'
    : '输入内容，Enter 发送，Shift+Enter 换行';
  autoGrow(ta);
}

async function sendToSession(text, keys, uid = S.sel, media = []) {
  const name = takenOver(uid);
  if (!name) return false;
  const cli = sesmanCli(uid);
  const serverQueued = !!text && cli?.source === 'codex' && !uid.startsWith('tmux:');
  const queuedId = text && !serverQueued && typeof queuePendingUserMessage === 'function'
    ? queuePendingUserMessage(uid, text, media) : null;
  let d;
  try {
    if (serverQueued) {
      const entry = cache.get(viewKey(uid));
      const activity = entry?.activity || null;
      const requestId = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      d = await post('api/session/send', {
        uid, name, text, media, activity, request_id: requestId,
        cursor: entry ? {
          start: entry.end, head: entry.version?.head, anchor: entry.anchor,
        } : null,
      });
    } else {
      d = await post('api/term/send', keys ? { name, keys, uid } : { name, text });
    }
  } catch (e) {
    if (queuedId) discardQueuedUserMessage(uid, queuedId);
    alert('发送失败: ' + (e.message || e));
    return false;
  }
  if (d.error) {
    if (queuedId) discardQueuedUserMessage(uid, queuedId);
    alert('发送失败: ' + d.error);
    return false;
  }
  if (serverQueued && typeof syncServerOutbox === 'function') {
    syncServerOutbox(uid, d.outbox || []);
  }
  if (Object.prototype.hasOwnProperty.call(d, 'activity')) {
    const entry = cache.get(viewKey(uid));
    if (entry) entry.activity = d.activity || null;
    if (S.sel === uid && !S.agent && typeof renderConversationTail === 'function') {
      renderConversationTail(entry?.activity || null, uid);
    }
  }
  S.live.add(uid);            // 发完立刻按最快节奏拉新消息
  S.liveTmux.add(uid);
  paintLive();
  S.syncGap = FAST_MIN;
  S.lastSync = 0;
  return true;
}

function composerFileKind(file) {
  const prefix = String(file.type || '').split('/', 1)[0];
  return ['image', 'video', 'audio'].includes(prefix) ? prefix : 'file';
}

function composerKindIcon(kind) {
  return { image: '▧', video: '▶', audio: '♪', file: '⌑' }[kind] || '⌑';
}

function closeAttachMenu() {
  $('#attach-menu').classList.add('hidden');
  $('#cadd').classList.remove('on');
  $('#cadd').setAttribute('aria-expanded', 'false');
}

function renderComposerItems() {
  const box = $('#compose-items');
  box.replaceChildren();
  const draft = composerDraft();
  if (!draft) return;
  for (const attachment of draft.attachments) {
    const card = el('div', `draft-card ${attachment.status || ''}`);
    card.dataset.draftId = attachment.id;
    card.title = `点击插入 [附件${attachment.number}]`;
    card.onclick = e => {
      if (!e.target.closest('.draft-remove')) insertComposerReference(attachment.number);
    };
    const thumb = el('span', 'draft-thumb');
    if (attachment.kind === 'image') {
      const image = document.createElement('img');
      image.src = attachment.preview;
      image.alt = '';
      thumb.appendChild(image);
    } else {
      thumb.textContent = composerKindIcon(attachment.kind);
    }
    const info = el('span', 'draft-info');
    const name = document.createElement('b');
    name.textContent = attachment.uploaded?.name || attachment.file.name || 'attachment';
    const meta = document.createElement('small');
    const ref = `[附件${attachment.number}]`;
    meta.textContent = attachment.status === 'uploading' ? `${ref} · 正在上传…`
      : attachment.status === 'failed' ? `${ref} · ${attachment.error || '上传失败'}`
        : `${ref} · ${attachment.kind === 'file' ? '文件' : ({ image: '图片', video: '视频', audio: '音频' }[attachment.kind])} · ${fmtSize(attachment.file.size)}`;
    info.append(name, meta);
    const remove = el('button', 'draft-remove', '×');
    remove.type = 'button';
    remove.title = remove.ariaLabel = '移除附件';
    remove.disabled = composerSending;
    remove.onclick = e => {
      e.stopPropagation();
      removeComposerAttachment(attachment.id);
    };
    card.append(thumb, info, remove);
    box.appendChild(card);
  }
  for (const quote of draft.quotes) {
    const card = el('div', 'draft-card draft-quote');
    card.dataset.draftId = quote.id;
    const mark = el('span', '', '❝');
    const text = document.createElement('textarea');
    text.value = quote.text;
    text.maxLength = 16000;
    text.placeholder = '粘贴或输入要引用的文字';
    text.setAttribute('aria-label', '引用文字');
    text.oninput = () => { quote.text = text.value; };
    const remove = el('button', 'draft-remove', '×');
    remove.type = 'button';
    remove.title = remove.ariaLabel = '移除引用';
    remove.disabled = composerSending;
    remove.onclick = () => removeComposerQuote(quote.id);
    card.append(mark, text, remove);
    box.appendChild(card);
  }
}

function addComposerFiles(files) {
  const draft = composerDraft();
  if (!draft) return;
  for (const file of files) {
    if (draft.attachments.length >= COMPOSER_MAX_FILES) {
      alert(`一次最多添加 ${COMPOSER_MAX_FILES} 个附件`);
      break;
    }
    if (!file.size || file.size > COMPOSER_MAX_FILE_BYTES) {
      alert(`「${file.name || '附件'}」为空或超过 512 MB`);
      continue;
    }
    const kind = composerFileKind(file);
    draft.attachments.push({
      id: `attachment-${++composerDraftSeq}`, number: draft.nextAttachmentNumber++, file, kind,
      preview: kind === 'image' ? URL.createObjectURL(file) : '',
      status: '', uploaded: null, error: '',
    });
  }
  renderComposerItems();
}

function insertComposerReference(number) {
  const ta = $('#cinput');
  if (!ta) return;
  const token = `[附件${number}]`;
  ta.focus();
  const start = Number.isInteger(ta.selectionStart) ? ta.selectionStart : ta.value.length;
  const end = Number.isInteger(ta.selectionEnd) ? ta.selectionEnd : start;
  ta.setRangeText(token, start, end, 'end');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

function removeComposerAttachment(id, draft = composerDraft()) {
  if (!draft || composerSending) return;
  const at = draft.attachments.findIndex(x => x.id === id);
  if (at < 0) return;
  const [removed] = draft.attachments.splice(at, 1);
  if (removed.preview) URL.revokeObjectURL(removed.preview);
  renderComposerItems();
}

function addComposerQuote(text = '') {
  const draft = composerDraft();
  if (!draft) return;
  if (draft.quotes.length >= 4) return alert('一次最多添加 4 段引用');
  draft.quotes.push({ id: `quote-${++composerDraftSeq}`, text: String(text).trim().slice(0, 16000) });
  renderComposerItems();
  boxFocusLastQuote();
}

function boxFocusLastQuote() {
  requestAnimationFrame(() => {
    const nodes = document.querySelectorAll('#compose-items .draft-quote textarea');
    nodes[nodes.length - 1]?.focus();
  });
}

function removeComposerQuote(id, draft = composerDraft()) {
  if (!draft || composerSending) return;
  const at = draft.quotes.findIndex(x => x.id === id);
  if (at >= 0) draft.quotes.splice(at, 1);
  renderComposerItems();
}

function buildComposerPrompt(text, attachments = [], quotes = []) {
  const attachmentPath = attachment => {
    const relative = String(attachment.relative_path || '').replace(/^\.\//, '');
    return relative ? `./${relative}` : attachment.path;
  };
  const body = String(text || '');
  const quoted = quotes.map(x => String(x.text ?? x).trim()).filter(Boolean);
  if (!attachments.length && !quoted.length) return text;
  const blocks = [];
  if (attachments.length) {
    blocks.push(attachments.map((a, i) =>
      `附件${Number.isInteger(a.number) ? a.number : i + 1}: ${attachmentPath(a)}`).join('\n'));
  }
  if (quoted.length) blocks.push(quoted.map((q, i) => `引用${i + 1}:\n${q}`).join('\n'));
  let prompt = body;
  for (const block of blocks) {
    if (prompt) {
      const trailingNewlines = prompt.match(/\n*$/)?.[0].length || 0;
      prompt += '\n'.repeat(Math.max(0, 2 - trailingNewlines));
    }
    prompt += block;
  }
  return prompt;
}

async function uploadComposerAttachment(attachment, uid, attachmentId = null) {
  if (attachment.uploaded?.uid === uid) return attachment.uploaded;
  attachment.status = 'uploading';
  attachment.error = '';
  renderComposerItems();
  const url = new URL(appUrl('api/session/attachment'));
  url.searchParams.set('uid', uid);
  url.searchParams.set('name', attachment.file.name || 'attachment');
  if (attachmentId) url.searchParams.set('id', attachmentId);
  try {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': attachment.file.type || 'application/octet-stream' },
      body: attachment.file,
    });
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    attachment.uploaded = { ...data, uid };
    attachment.status = 'ready';
    renderComposerItems();
    return attachment.uploaded;
  } catch (error) {
    attachment.status = 'failed';
    attachment.error = error.message || String(error);
    renderComposerItems();
    throw error;
  }
}

let composerSending = false;
async function submitComposer() {
  const ta = $('#cinput');
  const button = $('#csend');
  const add = $('#cadd');
  const uid = composerUid;
  const draft = composerDraft(uid);
  const text = ta.value;
  const attachments = [...(draft?.attachments || [])];
  const quotes = (draft?.quotes || []).map(x => ({ id: x.id, text: x.text })).filter(x => x.text.trim());
  if (composerSending || (!text.trim() && !attachments.length && !quotes.length)) return;
  composerSending = true;
  button.disabled = true;
  add.disabled = true;
  renderComposerItems();
  try {
    const uploaded = [];
    let attachmentId = attachments.find(x => x.uploaded?.uid === uid)?.uploaded?.attachment_id || null;
    for (let i = 0; i < attachments.length; i++) {
      button.textContent = `上传 ${i + 1}/${attachments.length}`;
      const result = await uploadComposerAttachment(attachments[i], uid, attachmentId);
      attachmentId ||= result.attachment_id;
      uploaded.push({ ...result, number: attachments[i].number });
    }
    button.textContent = '发送中…';
    const prompt = buildComposerPrompt(text, uploaded, quotes);
    const sentMedia = uploaded.flatMap(a => a.media ? [{ ...a.media, gallery: true }] : []);
    const sent = await sendToSession(prompt, null, uid, sentMedia);
    // 请求失败时保留草稿；等待响应期间若用户继续编辑，也不能抹掉新内容。
    if (sent) {
      if (draft.text === text || (composerUid === uid && ta.value === text)) draft.text = '';
      const sentFiles = new Set(attachments.map(x => x.id));
      const sentQuotes = new Map(quotes.map(x => [x.id, x.text]));
      for (const attachment of draft.attachments.filter(x => sentFiles.has(x.id))) {
        if (attachment.preview) URL.revokeObjectURL(attachment.preview);
      }
      draft.attachments = draft.attachments.filter(x => !sentFiles.has(x.id));
      draft.quotes = draft.quotes.filter(x => sentQuotes.get(x.id) !== x.text);
      if (!draft.text && !draft.attachments.length && !draft.quotes.length) {
        draft.nextAttachmentNumber = 1;
      }
      if (composerUid === uid) {
        ta.value = draft.text;
        renderComposerItems();
      }
    }
  } catch (error) {
    alert('附件上传失败: ' + (error.message || error));
  } finally {
    composerSending = false;
    button.disabled = false;
    add.disabled = false;
    button.textContent = '发送';
    renderComposerItems();
    autoGrow(ta);
  }
}

$('#cinput').addEventListener('input', e => {
  const draft = composerDraft();
  if (draft) draft.text = e.target.value;
  autoGrow(e.target);
});
$('#cinput').addEventListener('keydown', e => {
  // 手机软键盘没有方便的 Shift+Enter：Enter 始终换行，只允许按钮发送。
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !MOBILE.matches) {
    e.preventDefault();
    submitComposer();
  }
});
MOBILE.addEventListener('change', () => {
  syncComposerMode();
  renderTakeoverBtn();
});
syncComposerMode();
$('#csend').onclick = () => {
  submitComposer();
};
let composerEscAt = -Infinity;

async function revealNativeTerminal(uid = S.sel) {
  const name = takenOver(uid);
  if (!name || S.sel !== uid) return false;
  T.uid = uid;
  await openTermPane(name);
  if (!MOBILE.matches && T.mode === 'collapsed') {
    T.mode = 'full';
    store.set('termmode', T.mode);
    rememberTermLayout(name);
    layoutTermPane();
    renderTakeoverBtn();
    setTimeout(fitTerm, 0);
  }
  return true;
}

function activeCliQuestion(uid) {
  const entry = cache.get(viewKey(uid));
  if (entry?.prompt?.questions?.length) return entry.prompt;
  const question = typeof pendingHistoryQuestion === 'function'
    ? pendingHistoryQuestion(entry) : null;
  return question ? {id: question.call_id, questions: question.questions} : null;
}

async function answerCliQuestion(uid, optionIndex) {
  const prompt = activeCliQuestion(uid);
  const rows = prompt?.questions;
  if (rows?.length !== 1 || rows[0].multiple || !rows[0].options?.[optionIndex]) return false;
  // 不同 CLI 的菜单定位语义不同（Claude 用方向键，Codex 用数字直选），
  // 具体按键必须由各自实现决定，不能在公共交互层猜测当前光标位置。
  const keys = sesmanCli(uid)?.questionAnswerKeys(prompt, optionIndex);
  if (!keys?.length) return false;
  return sendToSession(null, keys, uid);
}

async function cancelCliQuestion(uid) {
  composerEscAt = -Infinity;
  const keys = sesmanCli(uid)?.questionCancelKeys(activeCliQuestion(uid));
  return keys?.length ? sendToSession(null, keys, uid) : false;
}

async function sendComposerEscape(now = performance.now()) {
  const uid = S.sel;
  const entry = cache.get(viewKey(uid));
  const visibleActivity = $('#activity')?.dataset.state;
  // activity 缓存可能来自上一个已结束的 Claude 进程；renderActivity 会把它
  // 隐藏。Esc 必须服从用户眼前的交互态，不能被这条旧 working 永久挡住回滚。
  const busy = !!entry?.prompt
    || !!pendingHistoryQuestion(entry)
    || ['working', 'waiting'].includes(visibleActivity);
  const draft = composerDraft(uid, false);
  const empty = !String($('#cinput')?.value || '').trim()
    && !(draft?.attachments?.length) && !(draft?.quotes?.some(q => q.text?.trim()));
  const escape = sesmanCli(uid)?.repeatedEscape(now, composerEscAt, { busy, empty })
    || { rewind: false, nextAt: -Infinity };
  const rewind = escape.rewind;
  composerEscAt = escape.nextAt;
  const name = takenOver(uid);
  const sent = await sendToSession(null, ['Escape'], uid);
  if (!rewind || !sent || !name || S.sel !== uid) return sent;

  // 回滚点、恢复代码/对话的选项都由原生 CLI 自己维护。第二次 Esc 后直接
  // 揭示原生 TUI，不在网页里根据 transcript 猜一个可能不一致的菜单。
  await revealNativeTerminal(uid);
  return sent;
}
$('#cesc').onclick = () => sendComposerEscape();

$('#cadd').onclick = e => {
  e.stopPropagation();
  const menu = $('#attach-menu');
  const open = menu.classList.toggle('hidden');
  $('#cadd').classList.toggle('on', !open);
  $('#cadd').setAttribute('aria-expanded', String(!open));
};
$('#attach-menu').onclick = e => {
  const button = e.target.closest('button[data-attach]');
  if (!button) return;
  const type = button.dataset.attach;
  closeAttachMenu();
  if (type === 'quote') {
    addComposerQuote(lastMessageSelectionUid === composerUid ? lastMessageSelection : '');
    lastMessageSelection = '';
    lastMessageSelectionUid = null;
    return;
  }
  const input = $('#cfile');
  input.accept = ATTACH_ACCEPT[type];
  input.dataset.kind = type;
  input.click();
};
$('#cfile').onchange = e => {
  addComposerFiles([...e.target.files]);
  e.target.value = '';
};
document.addEventListener('click', e => {
  if (!e.target.closest('.attach-picker')) closeAttachMenu();
});
document.addEventListener('selectionchange', () => {
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !selection.anchorNode || !selection.focusNode) return;
  const messages = $('#msgs');
  if (messages?.contains(selection.anchorNode) && messages.contains(selection.focusNode)) {
    lastMessageSelection = selection.toString().trim().slice(0, 16000);
    lastMessageSelectionUid = S.sel;
  }
});
$('#cinput').addEventListener('paste', e => {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  // 带附件的剪贴板常同时携带 text/plain；交给浏览器会把那份文字再粘贴一次。
  e.preventDefault();
  addComposerFiles(files);
});
$('#composer').addEventListener('dragenter', e => {
  if (e.dataTransfer?.types?.includes('Files')) $('#composer').classList.add('dragover');
});
$('#composer').addEventListener('dragover', e => {
  if (!e.dataTransfer?.types?.includes('Files')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
$('#composer').addEventListener('dragleave', e => {
  if (!$('#composer').contains(e.relatedTarget)) $('#composer').classList.remove('dragover');
});
$('#composer').addEventListener('drop', e => {
  $('#composer').classList.remove('dragover');
  const files = [...(e.dataTransfer?.files || [])];
  if (!files.length) return;
  e.preventDefault();
  addComposerFiles(files);
});

function setTermCtrl(on) {
  T.ctrlArmed = !!on;
  $('#termpane').classList.toggle('ctrl-locked', T.ctrlArmed);
  const b = $('[data-term-modifier="ctrl"]');
  if (b) {
    b.classList.toggle('on', T.ctrlArmed);
    b.setAttribute('aria-pressed', String(T.ctrlArmed));
  }
}

/** 把 Ctrl 修饰的单个 ASCII 键转换为终端控制字节。多字符粘贴和中文不改写。 */
function applyTermCtrl(data) {
  if (!T.ctrlArmed) return data;
  // focus-events、鼠标和粘贴都会产生多字节数据，它们不是用户要修饰的“下一键”。
  if (data.length !== 1) return data;
  setTermCtrl(false);
  const code = data.charCodeAt(0);
  if ((code >= 64 && code <= 95) || (code >= 97 && code <= 122)) {
    return String.fromCharCode(code & 31);
  }
  const aliases = { ' ': 0, '2': 0, '3': 27, '4': 28, '5': 29, '6': 30, '7': 31, '8': 127, '?': 127 };
  return Object.hasOwn(aliases, data) ? String.fromCharCode(aliases[data]) : data;
}

$('.term-keys').onclick = e => {
  const modifier = e.target.closest('[data-term-modifier]');
  if (modifier) {
    setTermCtrl(!T.ctrlArmed);
    T.term?.focus();
    return;
  }
  const b = e.target.closest('[data-term-key]');
  if (!b) return;
  const key = T.ctrlArmed ? `C-${b.dataset.termKey}` : b.dataset.termKey;
  setTermCtrl(false);
  sendToSession(null, [key]);
  T.term?.focus();
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
  rememberTermLayout();
  renderTakeoverBtn();
  fitTerm();
}
document.addEventListener('pointerup', finishTermDrag);
document.addEventListener('pointercancel', finishTermDrag);

// 移动浏览器锁屏后常保留一个 readyState=OPEN 的僵尸 WebSocket。进入后台时主动
// 放弃这条传输，回到前台/pageshow/网络恢复时重新 attach；tmux 进程不会受影响。
let termWasBackgrounded = false;
function backgroundTerm() {
  termWasBackgrounded = true;
  suspendTerm();
}
function foregroundTerm(force = false) {
  if (document.hidden || (!force && !termWasBackgrounded)) return;
  termWasBackgrounded = false;
  for (const view of T.views.values()) reconnectTerm(view);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) backgroundTerm();
  else foregroundTerm();
});
addEventListener('pagehide', backgroundTerm);
addEventListener('pageshow', e => foregroundTerm(e.persisted));
addEventListener('online', () => foregroundTerm(true));

loadTermList();
