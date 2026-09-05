'use strict';

// 接管会话: 在服务端把它用 tmux resume 起来, 然后把终端嵌在会话详情底部。
// 会话跑在 tmux 里, 所以关掉页面/重启 sesman 都不会打断它。
const TERM_RENDER_BATCH_MS = 20;
const TERM_RENDER_BATCH_MAX = 32 * 1024;
const TERM_LAYOUT_POLICY_VERSION = 2;
// 每次页面加载独立生成；不写 local/sessionStorage，复制标签页也不会复制归属。
const TERM_PAGE_ID = window.__sesmanPageId || crypto.randomUUID?.()
  || [...crypto.getRandomValues(new Uint8Array(16))]
    .map(value => value.toString(16).padStart(2, '0')).join('');

const T = {
  term: null,      // xterm 实例
  ws: null,
  name: null,      // 当前挂着的 tmux 会话名
  uid: null,       // 对应的 sesman 会话
  views: new Map(), // 已打开过且仍存活的 tmux → xterm/WebSocket；切会话只隐藏
  enabled: false,
  height: store.get('termh', 320),
  mode: store.get('termmode', 'full'), // normal(手动分屏) | collapsed(对话) | full(终端)
  ctrlArmed: false,                         // 手机 Ctrl / 桌面右 Ctrl：只修饰下一次输入
  sources: {},
  home: '',
  pending: [],
  pendingModes: new Map(), // 临时会话名 → 打开前的终端布局；退出未落盘时恢复
  resolving: new Set(),
  resolveControllers: new Map(),
  openViews: new Map(store.get('termviews', [])), // tmux 名 → {mode, height}
};
// 旧版把 normal 当默认布局，无法区分“系统默认分屏”和“用户手动分屏”。
// 升级时只迁移一次；此后 normal 只会由拖动分界线产生并照常按会话保存。
if (+store.get('termLayoutPolicyVersion', 0) < TERM_LAYOUT_POLICY_VERSION) {
  if (T.mode === 'normal') T.mode = 'full';
  T.openViews = new Map([...T.openViews].map(([name, layout]) => [
    name, layout?.mode === 'normal' ? { ...layout, mode: 'full' } : layout,
  ]));
  store.set('termmode', T.mode);
  store.set('termviews', [...T.openViews]);
  store.set('termLayoutPolicyVersion', TERM_LAYOUT_POLICY_VERSION);
}
// Claude 双 Esc 的最终叶子有时只保存在 TUI 进程内，不会追加 JSONL。
// 以 tmux 名为键跟踪原生选择器，确认后再让服务端从当前屏幕同步时间线。
const claudeRewinds = new Map();

const TERM_FONT_SAMPLE = 'MW0il中文，。！？（）【】';
let resolvedTermFont = '';
let resolvedTermFontKey = '';
let termFontResolveEpoch = 0;

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

function configuredTermFont() {
  return getComputedStyle(document.documentElement).getPropertyValue('--terminal-font').trim();
}

function termFont() {
  return resolvedTermFont || configuredTermFont();
}

function termFontSize() {
  const value = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--terminal-font-size'));
  return Number.isFinite(value) ? value : 14.04;
}

function terminalFontGridRatio(family, size) {
  const context = document.createElement('canvas').getContext('2d');
  if (!context) return 0;
  context.font = `400 ${size}px ${family}`;
  const latin = context.measureText('0').width;
  const cjk = context.measureText('中').width;
  return latin > 0 ? cjk / latin : 0;
}

/** Resolve one font face whose CJK glyph is exactly two Latin cells wide.
 * Ubuntu keeps its original glyph size: xterm reserves two cells for CJK and
 * rescaleOverlappingGlyphs prevents wide outlines from crossing cell bounds.
 * Other mixed stacks still prefer a locally available exact 1:2 font. */
async function prepareTerminalFont() {
  const configured = configuredTermFont();
  const size = termFontSize();
  const key = `${size}\n${configured}`;
  if (resolvedTermFont && resolvedTermFontKey === key) return resolvedTermFont;
  // 设置项刚切换时先停止返回旧字体；异步探测期间至少立即使用新选择。
  if (resolvedTermFontKey !== key) {
    resolvedTermFont = '';
    resolvedTermFontKey = '';
  }
  const epoch = ++termFontResolveEpoch;
  let resolved = configured;
  try {
    await document.fonts?.load(`${size}px ${configured}`, TERM_FONT_SAMPLE);
    const configuredRatio = terminalFontGridRatio(configured, size);
    const keepUbuntuGlyphs = configured.includes('"Sesman Ubuntu Sans Mono"');
    if (!keepUbuntuGlyphs && Math.abs(configuredRatio - 2) > .025) {
      const grid = '"Sesman CJK Mono Grid"';
      const faces = await document.fonts?.load(`${size}px ${grid}`, TERM_FONT_SAMPLE);
      const ratio = faces?.length ? terminalFontGridRatio(grid, size) : 0;
      if (Math.abs(ratio - 2) <= .025) resolved = `${grid}, ${configured}`;
    }
  } catch { /* 本机没有 Noto/Sarasa 时保留原字体回退 */ }
  if (epoch === termFontResolveEpoch) {
    resolvedTermFont = resolved;
    resolvedTermFontKey = key;
  }
  return resolved;
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
  terminalFontReady = prepareTerminalFont();
  try { await terminalFontReady; } catch {}
  const views = T.views ? T.views.values() : (T.term ? [{ term: T.term }] : []);
  const liveNames = new Set([...(T.list || []), ...(T.pending || [])]
    .map(item => item.name));
  const redrawNames = [];
  for (const view of views) {
    view.term.options.fontFamily = termFont();
    view.term.options.theme = termTheme();
    // 已退出的临时会话可能在下一次列表轮询前仍留有一个 xterm 视图。
    // 配色切换只重连目前确实存在的 tmux，不能拿陈旧视图去 claim 404。
    const currentConnected = T.name === view.name && view.ws?.readyState === WebSocket.OPEN;
    if (redraw && view.name && (liveNames.has(view.name) || currentConnected)) {
      redrawNames.push(view.name);
    }
  }
  for (const name of redrawNames) attachTerm(name);
  setTimeout(() => { fitTerm(); }, 0);
}

// xterm 先测量网页字体再创建 DOM 行，避免按回退字体计算出错误的字符宽度。
let terminalFontReady = prepareTerminalFont();
addEventListener('resize', () => { layoutTermPane(); fitTerm(); });

let termListRequestSeq = 0;
async function loadTermList() {
  const requestSeq = ++termListRequestSeq;
  const openEpoch = termOpenEpoch;
  const fingerprint = () => [
    ...(T.list || []).map(x => `${x.name}\t${x.cwd}`),
    ...(T.pending || []).map(x => `pending\t${x.name}\t${x.cwd}`),
  ].join('\n');
  const before = fingerprint();
  let loaded = false;
  let data = null;
  try {
    data = await (await fetch(appUrl('api/term/list'))).json();
    loaded = true;
  } catch { /* 下方统一应用失败状态 */ }
  // 只允许最后发出的请求改状态；否则慢响应会覆盖更新的 tmux 列表。
  if (requestSeq !== termListRequestSeq) return;
  // 请求在终端打开/切换之前发出时，它的“没有该视图”结论已经过期。丢弃
  // 整份结果并立刻重取，不能先销毁刚打开的常驻 xterm 再补回来。
  if (openEpoch !== termOpenEpoch) {
    void loadTermList();
    return;
  }
  if (loaded) {
    T.enabled = !!data.enabled;
    T.list = data.sessions || [];
    T.sources = data.sources || {};
    T.home = data.home || '';
    T.pending = data.pending || [];
  } else {
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
    // Codex 回退会创建新分支 UUID，但原生进程与 tmux pane 都不变。
    // term/list 已经把 pane 映射到当前叶子；若本页还选中旧叶子，
    // 必须连同草稿和终端归属一起跟进，不能继续向已消失的 uid 请求接管。
    await rebindSelectedTermSession();
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

function sessionTermMeta(uid) {
  return S.sessions.find(x => x.uid === uid)
    || (typeof cache !== 'undefined' ? cache.get(viewKey(uid))?.meta : null)
    || null;
}

/** 返回会话所在的稳定 tmux pane 以及 pane 当前对应的 uid。 */
function linkedTermSession(uid) {
  const panes = [...(T.list || []), ...(T.pending || [])];
  if (String(uid || '').startsWith('tmux:')) {
    const name = String(uid).slice(5);
    const pane = panes.find(x => x.name === name);
    return pane ? { name, uid: pane.uid || uid } : null;
  }
  const direct = panes.find(x => x.uid === uid);
  if (direct) return { name: direct.name, uid: direct.uid || uid };

  // 终端已经在本页打开时，pane 名是跨分支的稳定身份。
  // 服务端映射出的 pane.uid 才是当前原生叶子。
  if (T.uid === uid && T.name) {
    const active = panes.find(x => x.name === T.name);
    if (active) return { name: active.name, uid: active.uid || uid };
  }

  const session = sessionTermMeta(uid);
  if (!session) return null;
  // Codex 分支的 sid 会变，而接管时的 tmux 名由根会话 sid 生成。
  // 优先保留普通会话的叶子名，再用 root_sid 追溯回同一 pane。
  const ids = [...new Set([session.sid, session.root_sid].filter(Boolean))];
  for (const sid of ids) {
    const name = `sesman-${session.source}-${String(sid).slice(0, 8)}`;
    const pane = panes.find(x => x.name === name);
    if (pane) return { name, uid: pane.uid || uid };
  }
  return null;
}

/** 某个会话是否已经被接管 (存在对应的 tmux 会话)。 */
function takenOver(uid) {
  return linkedTermSession(uid)?.name || null;
}

function adoptLinkedTermSession(fromUid, linked, reason) {
  const toUid = linked?.uid;
  if (!toUid || toUid === fromUid || String(toUid).startsWith('tmux:')) return fromUid;
  browserAuditEvent?.('terminal.session_rebound', {
    name: linked.name, from_uid: fromUid, to_uid: toUid, reason,
  }, null, { uid: toUid });
  migrateComposerDraft(fromUid, toUid);
  T.uid = toUid;
  return toUid;
}

/** tmux 列表已指向新分支时，原子跟进当前详情与输入状态。 */
async function rebindSelectedTermSession() {
  const fromUid = T.uid;
  if (!fromUid || S.sel !== fromUid || S.agent
      || String(fromUid).startsWith('tmux:')) return false;
  const linked = linkedTermSession(fromUid);
  if (!linked?.uid || linked.uid === fromUid) return false;
  const toUid = adoptLinkedTermSession(fromUid, linked, 'term-list');
  await openSession(toUid);
  return true;
}

/** 顶栏切换前先跟进 pane 的当前分支，然后再执行原本的对话/终端切换。 */
async function toggleLinkedTermSession(uid) {
  const linked = linkedTermSession(uid);
  if (!linked) return false;
  const toUid = adoptLinkedTermSession(uid, linked, 'user-toggle');
  if (toUid !== uid && S.sel === uid && !S.agent) {
    await openSession(toUid);
    // 读取新分支期间用户可能已经切到别处，不再抢回终端。
    if (S.sel !== toUid || S.agent) return true;
  } else {
    T.uid = toUid;
  }
  toggleTermPane(linked.name);
  return true;
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
  const traceId = String(body?.request_id || globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const payload = { ...body, _build: BUILD_ID, _trace_id: traceId,
    _page_id: TERM_PAGE_ID };
  browserAuditEvent?.('http.request.started', {url, method: 'POST'}, null, {
    uid: body?.uid || '', traceId, requestId: body?.request_id || '',
  });
  const started = performance.now();
  try {
    const r = await fetch(appUrl(url), {
      method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-Sesman-Trace': traceId,
        'X-Sesman-Page': TERM_PAGE_ID, 'X-Sesman-Build': BUILD_ID,
      },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    browserAuditEvent?.('http.response.received', {
      url, status: r.status, ok: r.ok,
      duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
    }, data, {uid: body?.uid || '', traceId, requestId: body?.request_id || '',
      severity: r.ok ? 'info' : 'warning'});
    if (data?.reload) markStaleBuild(data.build);
    return data;
  } catch (error) {
    browserAuditEvent?.('http.request.failed', {
      url, error: String(error?.stack || error),
      duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
    }, null, {uid: body?.uid || '', traceId, requestId: body?.request_id || '',
      severity: 'error'});
    throw error;
  }
}

// ---------------------------------------------------------------- 缺陷报告
let bugReportToastTimer = 0;

function showBugReportToast(report, worker) {
  const toast = $('#bug-report-toast');
  clearTimeout(bugReportToastTimer);
  toast.replaceChildren();
  const text = document.createElement('span');
  text.textContent = `${report} 已保存，Codex 处理会话正在启动`;
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'btn';
  open.textContent = '打开';
  open.onclick = async () => {
    toast.classList.add('hidden');
    await loadTermList();
    const pending = (T.pending || []).find(item => item.name === worker.name) || worker;
    await openPendingSession(pending);
  };
  toast.append(text, open);
  toast.classList.remove('hidden');
  bugReportToastTimer = setTimeout(() => toast.classList.add('hidden'), 20000);
}

function openBugReportDialog() {
  const dialog = $('#bug-report-dialog');
  $('#bug-report-error').textContent = '';
  $('#bug-report-go').disabled = false;
  dialog.showModal();
  setTimeout(() => $('#bug-report-description').focus(), 0);
}

// 详情标题栏是动态生成的，使用委托让列表页、普通会话和尚未落盘的
// 新会话共用同一个入口；手机进入详情后列表顶栏会被完整隐藏。
document.addEventListener('click', event => {
  if (!event.target.closest('[data-report-bug]')) return;
  openBugReportDialog();
});
$('#bug-report-dialog .modal-close').onclick = () => $('#bug-report-dialog').close();
$('#bug-report-dialog .modal-cancel').onclick = () => $('#bug-report-dialog').close();
$('#bug-report-dialog').addEventListener('click', event => {
  if (event.target === $('#bug-report-dialog')) $('#bug-report-dialog').close();
});
$('#bug-report-form').onsubmit = async event => {
  event.preventDefault();
  const description = $('#bug-report-description').value.trim();
  const error = $('#bug-report-error');
  if (!description) {
    error.textContent = '请先描述遇到的问题';
    $('#bug-report-description').focus();
    return;
  }
  const button = $('#bug-report-go');
  button.disabled = true;
  error.textContent = '';
  const snapshot = browserStateSnapshot('bug-report');
  browserAuditEvent('bug_report.requested', snapshot.data, snapshot.content);
  try {
    const terminalName = takenOver(S.sel) || (T.uid === S.sel ? T.name : '') || '';
    const d = await post('api/bug-report', {
      description, uid: S.sel || '', page_id: TERM_PAGE_ID,
      terminal_name: terminalName, snapshot,
      cols: Math.max(80, T.term?.cols || 120), rows: Math.max(24, T.term?.rows || 36),
    });
    if (d.error) {
      error.textContent = d.error;
      return;
    }
    $('#bug-report-dialog').close();
    $('#bug-report-description').value = '';
    await loadTermList();
    showBugReportToast(d.report_id, d.worker);
  } catch (failure) {
    error.textContent = `提交失败：${failure.message || failure}`;
  } finally {
    button.disabled = false;
  }
};

// ---------------------------------------------------------------- 新建会话
function suggestedSessionDir(cwd) {
  const path = String(cwd || '').replace(/\/+$/, '') || '/';
  // CLI/SDK 经常在这些易失根目录里生成一次性测试会话。它们仍属于会话
  // 历史，但不该因一次自动任务污染“最近使用”的新建目录建议。
  return path.startsWith('/') && !['/tmp', '/var/tmp', '/dev/shm'].some(
    root => path === root || path.startsWith(root + '/'));
}

function commonSessionDirs() {
  const dirs = new Map();
  for (const s of S.sessions) {
    const cwd = String(s.cwd || '');
    if (!suggestedSessionDir(cwd)) continue;
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
    || b.updated.localeCompare(a.updated) || a.cwd.localeCompare(b.cwd));
}

const CWD_COMPLETION_DELAY = 120;
const cwdCompletion = {
  timer: null, abort: null, sequence: 0,
  rows: [], completions: [], forValue: '', active: -1, mode: 'common', common: [],
};

function canCompleteCwd(value) {
  const path = String(value || '').trim();
  return path.startsWith('/') || path === '~' || path.startsWith('~/');
}

function cancelCwdCompletionRequest() {
  if (cwdCompletion.timer) clearTimeout(cwdCompletion.timer);
  cwdCompletion.timer = null;
  cwdCompletion.abort?.abort();
  cwdCompletion.abort = null;
  cwdCompletion.sequence++;
}

function closeCwdPicker() {
  const input = $('#new-cwd'), picker = $('#new-cwd-picker');
  cancelCwdCompletionRequest();
  cwdCompletion.rows = [];
  cwdCompletion.completions = [];
  cwdCompletion.forValue = '';
  cwdCompletion.active = -1;
  $('#new-cwd-options').replaceChildren();
  picker.hidden = true;
  input.setAttribute('aria-expanded', 'false');
  input.removeAttribute('aria-activedescendant');
}

function cwdOption(path, meta = '', kind = 'recent') {
  return { path: String(path || ''), meta: String(meta || ''), kind };
}

function cwdPathKey(path) {
  const value = String(path || '');
  return value === '/' ? value : value.replace(/\/+$/, '');
}

function matchingRecentCwdOptions(value = '') {
  const query = String(value || '').trim().toLocaleLowerCase();
  return cwdCompletion.common
    .filter(row => !query || String(row.cwd || '').toLocaleLowerCase().includes(query))
    .map(row => cwdOption(row.cwd, row.count ? `${row.count} 个会话` : '', 'recent'));
}

function renderCwdOptions(value, recentRows, completionRows = [], completionNote = '') {
  const input = $('#new-cwd'), picker = $('#new-cwd-picker');
  const box = $('#new-cwd-options');
  const rawRecent = recentRows.map(row => typeof row === 'string'
    ? cwdOption(row, '', 'recent') : row);
  const rawCompletions = completionRows.map(row => typeof row === 'string'
    ? cwdOption(row, '', 'completion') : row);
  const completionFirst = String(value || '').startsWith('/');
  const seen = new Set();
  const unique = rows => rows.filter(row => {
    const key = cwdPathKey(row.path);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const completions = completionFirst ? unique(rawCompletions) : [];
  const recent = unique(rawRecent);
  if (!completionFirst) completions.push(...unique(rawCompletions));
  const options = completionFirst
    ? [...completions, ...recent] : [...recent, ...completions];
  cwdCompletion.mode = value ? 'matching' : 'common';
  cwdCompletion.rows = options.map(row => row.path);
  // recent 与建议中的同一路径只画一次，但它仍是文件系统补全候选；
  // Tab 计算公共前缀时不能因为视觉去重而把它漏掉。
  cwdCompletion.completions = rawCompletions.map(row => row.path);
  cwdCompletion.forValue = value;
  cwdCompletion.active = -1;
  box.replaceChildren();
  $('#new-cwd-options-title').textContent = value ? '匹配目录' : '最近使用';
  picker.hidden = false;

  const section = label => {
    const heading = document.createElement('div');
    heading.className = 'new-cwd-section';
    heading.setAttribute('role', 'presentation');
    heading.textContent = label;
    box.appendChild(heading);
  };
  const note = message => {
    const messageNode = document.createElement('div');
    messageNode.className = 'new-cwd-empty';
    messageNode.textContent = message;
    box.appendChild(messageNode);
  };
  const addOption = ({path, meta, kind}, index) => {
    const option = document.createElement('button');
    option.type = 'button';
    option.id = `new-cwd-option-${index}`;
    option.className = 'new-cwd-option';
    option.dataset.cwdKind = kind;
    option.dataset.cwdOption = String(index);
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', 'false');
    option.title = path;
    const label = document.createElement('span');
    label.className = 'new-cwd-option-path';
    label.textContent = path;
    option.appendChild(label);
    if (meta) {
      const detail = document.createElement('span');
      detail.className = 'new-cwd-option-meta';
      detail.textContent = meta;
      option.appendChild(detail);
    }
    box.appendChild(option);
  };

  if (!value) {
    recent.forEach(addOption);
    if (!recent.length) note('还没有使用过的目录');
  } else {
    let offset = 0;
    const addGroup = (label, rows, empty = '') => {
      if (!rows.length && !empty) return;
      section(label);
      rows.forEach((row, index) => addOption(row, offset + index));
      offset += rows.length;
      if (!rows.length && empty) note(empty);
    };
    if (completionFirst) {
      addGroup('补全建议', completions, completionNote);
      addGroup('最近匹配', recent);
    } else {
      addGroup('最近匹配', recent);
      addGroup('补全建议', completions, completionNote);
    }
    if (!recent.length && !completions.length && !completionNote) note('没有匹配的目录');
  }
  input.setAttribute('aria-expanded', 'true');
  input.removeAttribute('aria-activedescendant');
  $('#new-cwd-completion-status').textContent = options.length
    ? (value ? `${recent.length} 个最近匹配，${completions.length} 个补全建议`
             : `${recent.length} 个最近目录`)
    : (completionNote || '没有匹配的目录');
}

function renderCommonCwdOptions() {
  renderCwdOptions('', matchingRecentCwdOptions());
}

function setCwdCompletionActive(step) {
  const rows = cwdCompletion.rows;
  if (!rows.length) return;
  const old = cwdCompletion.active;
  const next = old < 0
    ? (step > 0 ? 0 : rows.length - 1)
    : (old + step + rows.length) % rows.length;
  cwdCompletion.active = next;
  const options = [...$('#new-cwd-options').querySelectorAll('[data-cwd-option]')];
  options.forEach((option, index) => {
    const active = index === next;
    option.classList.toggle('active', active);
    option.setAttribute('aria-selected', String(active));
  });
  const option = options[next];
  $('#new-cwd').setAttribute('aria-activedescendant', option.id);
  option.scrollIntoView({ block: 'nearest' });
}

function setCwdValue(value, refresh = true) {
  const input = $('#new-cwd');
  input.value = value;
  $('#new-session-error').textContent = '';
  input.focus();
  input.setSelectionRange(value.length, value.length);
  if (refresh) scheduleCwdCompletions();
}

function longestCommonPrefix(values) {
  if (!values.length) return '';
  let prefix = values[0];
  for (const value of values.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < value.length && prefix[i] === value[i]) i++;
    prefix = prefix.slice(0, i);
    if (!prefix) break;
  }
  return prefix;
}

function applyCwdTabCompletion() {
  const input = $('#new-cwd');
  if (!cwdCompletion.rows.length) return;
  if (cwdCompletion.active >= 0) {
    setCwdValue(cwdCompletion.rows[cwdCompletion.active]);
    return;
  }
  const rows = cwdCompletion.completions;
  if (!rows.length) return;
  if (rows.length === 1) {
    setCwdValue(rows[0]);
    return;
  }
  const value = input.value.trim();
  const prefix = longestCommonPrefix(rows);
  if (prefix.length > value.length) {
    setCwdValue(prefix, false);
    cwdCompletion.forValue = prefix;
    $('#new-cwd-completion-status').textContent =
      `已补全公共前缀，仍有 ${rows.length} 个补全建议`;
  }
}

async function loadCwdCompletions(complete = false) {
  cancelCwdCompletionRequest();
  const input = $('#new-cwd');
  const value = input.value.trim();
  const recent = matchingRecentCwdOptions(value);
  if (!canCompleteCwd(value)) {
    renderCwdOptions(value, recent);
    return;
  }
  const controller = new AbortController();
  const sequence = ++cwdCompletion.sequence;
  cwdCompletion.abort = controller;
  try {
    const params = new URLSearchParams({ path: value });
    const response = await fetch(appUrl(`api/term/complete-dir?${params}`),
      { signal: controller.signal, cache: 'no-store' });
    const data = await response.json();
    if (sequence !== cwdCompletion.sequence || input.value.trim() !== value) return;
    const rows = response.ok && Array.isArray(data.directories)
      ? data.directories.filter(path => typeof path === 'string' && canCompleteCwd(path)).slice(0, 24)
      : [];
    renderCwdOptions(value, recent, rows, response.ok
      ? (rows.length ? '' : '没有补全建议')
      : (data.error || '目录补全暂不可用'));
    if (complete) applyCwdTabCompletion();
  } catch (error) {
    if (error.name !== 'AbortError' && sequence === cwdCompletion.sequence) {
      renderCwdOptions(value, recent, [], '目录补全暂不可用');
    }
  } finally {
    if (cwdCompletion.abort === controller) cwdCompletion.abort = null;
  }
}

function scheduleCwdCompletions() {
  cancelCwdCompletionRequest();
  const value = $('#new-cwd').value.trim();
  if (!value) {
    renderCommonCwdOptions();
    return;
  }
  const recent = matchingRecentCwdOptions(value);
  if (!canCompleteCwd(value)) {
    renderCwdOptions(value, recent);
    return;
  }
  renderCwdOptions(value, recent, [], '正在查找目录…');
  cwdCompletion.timer = setTimeout(() => loadCwdCompletions(false), CWD_COMPLETION_DELAY);
}

function openNewSessionDialog() {
  const dialog = $('#new-session-dialog');
  closeCwdPicker();
  const rows = commonSessionDirs();
  cwdCompletion.common = rows;
  for (const input of dialog.querySelectorAll('input[name="new-source"]')) {
    input.disabled = !T.sources[input.value];
  }
  const checked = dialog.querySelector('input[name="new-source"]:checked');
  if (!checked || checked.disabled) dialog.querySelector('input[name="new-source"]:not(:disabled)')?.click();
  const selected = S.sessions.find(s => s.uid === S.sel)?.cwd;
  const cwd = selected || store.get('newDirs', [])[0] || rows[0]?.cwd || T.home || '';
  $('#new-cwd').value = cwd;
  $('#new-session-error').textContent = '';
  $('#new-session-go').disabled = false;
  dialog.showModal();
  renderCommonCwdOptions();
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
  const pendingTitle = info.title || `新建 ${src.name} 会话`;
  $('#detail').innerHTML = `<div class="dhead"><div class="dtitle">
    <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
    <h2>${icon(info.source)}<span>${esc(pendingTitle)}</span></h2>
    <div class="dhead-actions" aria-label="会话操作">
      <span class="mobile-msg-summary"><span class="mobile-msg-count" aria-label="0 条消息">0</span></span>
      <button class="iconbtn" id="a-term" title="切换到终端" aria-label="切换到终端">${uiIcon('terminal')}</button>
      <button class="iconbtn" data-report-bug title="报告当前会话问题"
        aria-label="报告当前会话问题">${uiIcon('bug')}</button>
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
    const request = { source, cwd, cols: 120, rows: termRows() };
    let d = await post('api/term/create', request);
    if (d.needs_create) {
      const target = String(d.cwd || cwd);
      if (!confirm(`启动目录不存在：\n${target}\n\n是否创建该目录并继续？`)) {
        $('#new-cwd').focus();
        return;
      }
      go.textContent = '创建目录中…';
      d = await post('api/term/create', { ...request, cwd: target, create_cwd: true });
    }
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
$('#new-cwd').oninput = () => {
  $('#new-session-error').textContent = '';
  scheduleCwdCompletions();
};
$('#new-cwd').onkeydown = e => {
  if (e.isComposing) return;
  if (e.key === 'Tab' && !e.shiftKey && canCompleteCwd(e.currentTarget.value)) {
    e.preventDefault();
    if (cwdCompletion.mode === 'matching' && cwdCompletion.completions.length
        && cwdCompletion.forValue === e.currentTarget.value.trim()) {
      applyCwdTabCompletion();
    } else {
      loadCwdCompletions(true);
    }
  } else if (e.key === 'ArrowDown' && cwdCompletion.rows.length) {
    e.preventDefault();
    setCwdCompletionActive(1);
  } else if (e.key === 'ArrowUp' && cwdCompletion.rows.length) {
    e.preventDefault();
    setCwdCompletionActive(-1);
  } else if (e.key === 'Enter' && cwdCompletion.active >= 0) {
    e.preventDefault();
    setCwdValue(cwdCompletion.rows[cwdCompletion.active]);
  }
};
$('#new-cwd-options').onclick = e => {
  const option = e.target.closest('[data-cwd-option]');
  if (!option) return;
  e.preventDefault();
  const path = cwdCompletion.rows[Number(option.dataset.cwdOption)];
  if (path) setCwdValue(path);
};
$('#new-session-dialog').addEventListener('close', closeCwdPicker);
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
  const switchToChat = paneOpen && (MOBILE.matches || T.mode === 'full');
  const terminalVisible = paneOpen && (MOBILE.matches || T.mode !== 'collapsed');
  const label = !name ? '接管会话'
    : MOBILE.matches ? (paneOpen ? '切换到对话' : '切换到终端')
    : switchToChat ? '切换到对话' : '切换到终端';
  b.innerHTML = uiIcon(switchToChat ? 'chat' : 'terminal');
  b.title = b.ariaLabel = label;
  b.setAttribute('aria-expanded', String(terminalVisible));
  b.classList.toggle('on', !!name);
  b.classList.toggle('session-live', S.live.has(S.sel));
  b.classList.toggle('session-tmux', S.liveTmux.has(S.sel));
  renderComposer();
}

// ---------------------------------------------------------------- 终端面板
function currentTermViewObject() {
  return T.name ? T.views.get(T.name) || null : null;
}

function syncTermAliases(view = null) {
  T.term = view?.term || null;
  T.ws = view?.ws || null;
}

let termOpenEpoch = 0;

function activateTermView(view) {
  for (const cached of T.views.values()) cached.host.hidden = cached !== view;
  view.host.hidden = false;
  T.name = view.name;
  syncTermAliases(view);
  setScrollPos(view.scrollPos);
}

/** 只有显式打开终端的动作才能请求焦点；异步连接期间若用户已经点到别处，
 *  请求自动作废。这样 tmux 列表/会话正文的后台刷新不会打断搜索或编辑。 */
function requestTermFocus(view, source = document.activeElement) {
  view.focusRequest = source || document.body;
}

function focusTermIfRequested(view) {
  const source = view?.focusRequest;
  if (!source) return false;
  view.focusRequest = null;
  const active = document.activeElement;
  const inside = active && view.host.contains(active);
  const neutral = !active || active === document.body || active === document.documentElement;
  const editing = !inside && active?.matches?.(
    'input, textarea, select, [contenteditable="true"], [contenteditable="plaintext-only"]');
  if (editing || (!inside && !neutral && active !== source)) return false;
  view.term.focus();
  return true;
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

function termSelectionMouseDown(event) {
  return new MouseEvent('mousedown', {
    bubbles: true, cancelable: true, composed: true, view: window,
    detail: event.detail,
    screenX: event.screenX, screenY: event.screenY,
    clientX: event.clientX, clientY: event.clientY,
    ctrlKey: event.ctrlKey, altKey: event.altKey, metaKey: event.metaKey,
    shiftKey: false, button: event.button, buttons: event.buttons,
  });
}

function ensureTerm(name) {
  let view = T.views.get(name);
  if (view) return view;
  const host = el('div', 'xterm-view');
  host.hidden = true;
  $('#xterm').appendChild(host);
  const term = new Terminal({
    allowProposedApi: true,
    fontFamily: termFont(),
    fontSize: termFontSize(), fontWeight: '400', fontWeightBold: '600',
    rescaleOverlappingGlyphs: true,
    cursorBlink: true, scrollback: 10000,
    scrollOnUserInput: true, theme: termTheme(),
  });
  const fit = new FitAddon.FitAddon();
  view = {
    name, host, term, fit, ws: null, reconnectTimer: null,
    reconnectDelay: 500, scrollPos: 0, ansiTail: '',
    outputBuffer: '', outputTimer: null, fitFrame: null,
    lastResizeKey: '', lastResizeWs: null,
    activationEpoch: 0,
    attachPromise: null, revoked: false,
    focusRequest: null, resumeFocus: false,
    renderer: 'dom', webgl: null, unicode11: null,
    selectionLocked: false, selectionSnapshot: null, restoringSelection: false,
  };
  T.views.set(name, view);
  term.loadAddon(fit);
  if (globalThis.Unicode11Addon?.Unicode11Addon) {
    try {
      view.unicode11 = new Unicode11Addon.Unicode11Addon();
      term.loadAddon(view.unicode11);
      term.unicode.activeVersion = '11';
    } catch { view.unicode11 = null; }
  }
  term.open(host);
  // Codex 用 DEC ?2026 同步输出重画输入框。DOM renderer 在 Chromium/Wayland
  // 会偶发只提交清行的中间图层；WebGL 把整帧画进同一纹理。不可用或 context
  // loss 时官方 addon 会被 dispose，xterm 自动恢复 DOM renderer。
  if (globalThis.WebglAddon?.WebglAddon) {
    try {
      const webgl = new WebglAddon.WebglAddon();
      webgl.onContextLoss(() => {
        if (view.webgl !== webgl) return;
        view.webgl = null;
        view.renderer = 'dom';
        webgl.dispose();
        requestAnimationFrame(() => term.refresh(0, term.rows - 1));
      });
      term.loadAddon(webgl);
      view.webgl = webgl;
      view.renderer = 'webgl';
    } catch { /* WebGL2/硬件加速不可用时保留 DOM renderer */ }
  }
  const forwardedSelectionStarts = new WeakSet();
  host.addEventListener('mousedown', e => {
    if (forwardedSelectionStarts.has(e) || e.button !== 0) return;
    view.selectionLocked = e.shiftKey;
    if (!e.shiftKey) return;
    view.selectionSnapshot = null;
    // VT mouse 开启时 xterm 自己用 Shift 强制进入本地选择，必须保留原事件，
    // 才不会把鼠标发给 vim/less 等里面的程序。
    if (term.modes.mouseTrackingMode !== 'none') return;
    // xterm 把 Shift+拖拽解释为“扩展已有选区”，没有旧选区时结果为空。
    // tmux 用户则用 Shift 绕过终端鼠标模式并开始一次新框选。拦住原事件，
    // 以普通左键事件启动 xterm 自己的选择器；后续 move/up 仍由它原样处理。
    e.preventDefault();
    e.stopImmediatePropagation();
    const forwarded = termSelectionMouseDown(e);
    forwardedSelectionStarts.add(forwarded);
    e.target.dispatchEvent(forwarded);
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
    browserAuditEvent('terminal.input', {name, bytes: new TextEncoder().encode(d).length},
      d, {uid: T.uid || '', connectionId: view.auditConnectionId || ''});
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
    if (claudeRewinds.has(name) && /[\r\n]/.test(d)) {
      scheduleClaudeRewindSync(name);
    }
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

function termPaneRenderable(view = currentTermViewObject()) {
  if (!view || view !== currentTermViewObject()) return false;
  const pane = $('#termpane');
  if (pane.classList.contains('hidden')) return false;
  if (!MOBILE.matches && T.mode === 'collapsed') return false;
  if (pane.classList.contains('term-collapsed')) return false;
  // 手机从桌面布局切回会话列表时，#right 会由祖先的 display:none 隐藏，
  // 但 #termpane 本身没有 hidden 类。FitAddon 在这种容器上会返回内部最小值
  // 10×5；先确认当前 host 真正参与布局，不能让这组伪尺寸污染 xterm/PTY。
  const host = view.host;
  if (!host || host.hidden || !host.isConnected) return false;
  const rect = host.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function repaintTermView(view) {
  try { view.term.refresh(0, Math.max(0, view.term.rows - 1)); } catch { /* disposed */ }
}

function performTermFit(view, forceSync = false) {
  if (!termPaneRenderable(view)) return;
  let dimensions;
  try { dimensions = view.fit.proposeDimensions(); } catch { return; }
  if (!dimensions || !Number.isFinite(dimensions.cols) || !Number.isFinite(dimensions.rows)) return;
  // FitAddon.fit() 会先调用私有 _renderService.clear()，DOM renderer 因而在每次
  // 窗口缩放时先变空再重画。直接使用公开 resize API 保留旧行，并平滑增删行列。
  const resized = view.term.cols !== dimensions.cols || view.term.rows !== dimensions.rows;
  if (resized) {
    view.term.resize(dimensions.cols, dimensions.rows);
  }
  const ws = view.ws;
  const key = `${view.term.cols}x${view.term.rows}`;
  // 同一个 socket 的相同尺寸不再反复通知 tmux，避免 TUI 收到无效 SIGWINCH。
  if (ws?.readyState === 1 && (forceSync
      || view.lastResizeWs !== ws || view.lastResizeKey !== key)) {
    ws.send(JSON.stringify({ t: 'resize', cols: view.term.cols, rows: view.term.rows }));
    view.lastResizeWs = ws;
    view.lastResizeKey = key;
  }
  // display:none 下缓存的 WebGL/DOM surface 可能失去内容；若行列数碰巧没变，
  // Terminal.resize 不会触发 renderer。重新激活时必须显式画回整个 viewport。
  if (resized || forceSync) repaintTermView(view);
}

function fitTerm(immediate = false, forceSync = false) {
  const view = currentTermViewObject();
  if (!termPaneRenderable(view)) return;
  if (view.fitFrame) cancelAnimationFrame(view.fitFrame);
  view.fitFrame = null;
  if (immediate) {
    performTermFit(view, forceSync);
    return;
  }
  // 浏览器最大化、拖边界和软键盘动画都会连续发 resize；每个动画帧最多 fit
  // 一次，既跟手又不在同一帧重复测量和重排。
  view.fitFrame = requestAnimationFrame(() => {
    view.fitFrame = null;
    performTermFit(view, forceSync);
  });
}

function settleActivatedTermView(view) {
  const epoch = ++view.activationEpoch;
  const verify = () => {
    if (view.activationEpoch !== epoch || !termPaneRenderable(view)) return;
    performTermFit(view);
    repaintTermView(view);
  };
  // 先等浏览器提交“hidden → visible”和详情页布局，再复核一次；字体或滚动条
  // 稍晚稳定的浏览器再由短定时器兜底。两次都受 activationEpoch 约束。
  requestAnimationFrame(() => requestAnimationFrame(verify));
  setTimeout(verify, 120);
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
  const prompt = cache.get(viewKey(uid))?.prompt || null;
  // 刷新页面时可能先恢复了一个仍在等待回答的原生题目，再恢复终端布局。
  // 先登记并呈现题目，不能让旧的纯终端偏好随后把题卡重新盖住。
  const promptId = String(prompt?.id || '');
  const waitingPrompt = !!promptId && prompt?.questions?.length
    && (prompt.state || 'waiting') === 'waiting';
  if (waitingPrompt && $('#termpane').classList.contains('hidden')) {
    revealedTermPrompts.set(uid, promptId);
    if (MOBILE.matches) {
      renderTakeoverBtn();
      return;
    }
    const savedMode = T.openViews.get(name)?.mode;
    if (savedMode !== 'normal') {
      openTermPane(name, false, 'collapsed');
      return;
    }
  }
  if (revealConversationForPrompt(uid, prompt)) return;
  // loadTermList 和会话 reset 都会走这里。已打开时无需反复 fit/聚焦；首次
  // 自动恢复也只恢复视图，不应抢走用户正在使用的搜索框或输入框。
  if (!$('#termpane').classList.contains('hidden') && T.name === name) {
    renderTakeoverBtn();
    return;
  }
  openTermPane(name, false);
}

async function openTermPane(name, autoFocus = true, requestedMode = null) {
  const openEpoch = ++termOpenEpoch;
  const focusSource = autoFocus ? document.activeElement : null;
  const saved = T.openViews.get(name);
  if (!MOBILE.matches && ['normal', 'collapsed', 'full'].includes(requestedMode)) {
    T.mode = requestedMode;
  } else if (saved) {
    if (['normal', 'collapsed', 'full'].includes(saved.mode)) T.mode = saved.mode;
    if (Number.isFinite(saved.height) && saved.height > 0) T.height = saved.height;
  } else if (!MOBILE.matches) {
    // 新接管或从未保存过布局的会话默认只显示终端；分屏只能由拖动产生。
    T.mode = 'full';
  }
  rememberTermOpen(name, true);
  rememberTermLayout(name);
  const pane = $('#termpane');
  pane.classList.remove('hidden');
  layoutTermPane();
  renderTakeoverBtn();
  try { await terminalFontReady; } catch { /* 字体失败时继续用 Consola/monospace */ }
  if (openEpoch !== termOpenEpoch || pane.classList.contains('hidden')) return false;
  const view = ensureTerm(name);
  if (autoFocus) requestTermFocus(view, focusSource);
  activateTermView(view);
  // 桌面“纯对话”吸附态高度为 0。此时保留 xterm 对象和已有连接，但不要
  // 新连或 fit；否则内部最小尺寸会把真实 tmux pane 压成 10×6。
  if (termPaneRenderable(view)) {
    // 缓存 view 即使行列数相同也可能丢了 renderer surface；强制同步并重绘。
    fitTerm(true, true);                 // 连接前先确定尺寸，避免 80×24 → 实际尺寸的首屏跳变
    settleActivatedTermView(view);
    if (view.ws?.readyState !== 1) await attachTerm(name);
    else focusTermIfRequested(view);
  }
  return true;
}

/** 顶栏按钮只切纯对话/纯终端；normal 分屏只能由用户拖动分界线产生。 */
function toggleTermPane(name) {
  const pane = $('#termpane');
  if (pane.classList.contains('hidden')) {
    openTermPane(name, true, MOBILE.matches ? null : 'full');
    return;
  }
  if (!MOBILE.matches) {
    // 分屏状态点按钮也进入纯终端；下一次再切到纯对话。
    T.mode = T.mode === 'full' ? 'collapsed' : 'full';
    store.set('termmode', T.mode);
    rememberTermLayout(name);
    layoutTermPane();
    renderTakeoverBtn();
    if (T.mode === 'full') openTermPane(name);
    return;
  }
  closeTermPane();
}

// 同一个题目只自动呈现一次。用户看过以后仍可以主动切回原生终端；后台的
// 0.4 秒审批轮询和 tmux 列表刷新不能再把界面强行切回来。
const revealedTermPrompts = new Map();

/** 纯终端会遮住对话题卡。当前会话第一次收到待回答题目时切到对话；手动
 *  split 本来就能同时看到题卡，不改变它。prompt 结束后允许同一命令再次提问。 */
function revealConversationForPrompt(uid, prompt) {
  const id = String(prompt?.id || '');
  const waiting = !!id && prompt?.questions?.length
    && (prompt.state || 'waiting') === 'waiting';
  if (!waiting) {
    revealedTermPrompts.delete(uid);
    return false;
  }
  if (S.sel !== uid || S.agent) return false;
  const pane = $('#termpane');
  if (!pane || pane.classList.contains('hidden')) return false;
  if (revealedTermPrompts.get(uid) === id) return false;
  revealedTermPrompts.set(uid, id);
  if (MOBILE.matches) {
    closeTermPane(true);
    return true;
  }
  if (T.mode !== 'full') return false;
  T.mode = 'collapsed';
  store.set('termmode', T.mode);
  rememberTermLayout(T.name || takenOver(uid));
  layoutTermPane();
  renderTakeoverBtn();
  return true;
}

function closeTermPane(preserveView = false) {
  termOpenEpoch++;                       // 令仍在等待字体/连接的旧 openTermPane 作废
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
  const paneOpen = !pane.classList.contains('hidden');
  right.classList.toggle('term-full', desktop && paneOpen && T.mode === 'full');
  pane.classList.toggle('term-collapsed', desktop && paneOpen && T.mode === 'collapsed');
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
    else {
      // T.height 是跨窗口尺寸保存的用户偏好。在较高窗口拖大的终端切到较矮
      // 窗口后，不能让旧高度占满整个 #right；否则 detail 会被压成 0，标题
      // 与 composer 重叠，termpane 还会被推出视口（再 resize 才看似恢复）。
      // 普通模式始终给详情头和 composer 留出它们当前实际需要的空间。
      const detailHeadHeight = $('#detail > .dhead')?.offsetHeight || 0;
      const composerHeight = $('#composer')?.offsetHeight || 0;
      const maxHeight = Math.max(0, right.clientHeight - detailHeadHeight - composerHeight);
      pane.style.height = Math.min(T.height, maxHeight) + 'px';
    }
  }
}

async function claimTermOwnership(name) {
  let result = await post('api/term/claim', {name, page: TERM_PAGE_ID});
  if (result.conflict) {
    const ownerIp = result.owner?.ip || '另一地址';
    if (!confirm(`该终端正由 ${ownerIp} 控制。\n\n是否抢占终端？`)) return null;
    result = await post('api/term/claim', {name, page: TERM_PAGE_ID, force: true});
  }
  if (result.error || !result.token) {
    alert('打开终端失败：' + (result.error || '无法取得终端控制权'));
    return null;
  }
  return result.token;
}

function handleTermRevoked(view, ip = '') {
  if (view.revoked) return;
  view.revoked = true;
  cancelTermReconnect(view);
  if (T.name === view.name) closeTermPane();
  try { view.ws?.close(); } catch {}
  alert(`终端已被 ${ip || '另一页面'} 接管，本页面的终端已关闭。`);
}

function attachTerm(name) {
  const view = ensureTerm(name);
  if (view.attachPromise) return view.attachPromise;
  const job = attachOwnedTerm(view).finally(() => {
    if (view.attachPromise === job) view.attachPromise = null;
  });
  view.attachPromise = job;
  return job;
}

async function attachOwnedTerm(view) {
  const name = view.name;
  const active = !$('#termpane').classList.contains('hidden') && T.name === name;
  if (active) activateTermView(view);
  cancelTermReconnect(view);
  dropTermSocket(view);
  const token = await claimTermOwnership(name);
  if (!token) {
    view.revoked = true;
    view.focusRequest = null;
    if (T.name === name) closeTermPane();
    return false;
  }
  view.revoked = false;
  clearTermOutput(view);
  view.ansiTail = '';
  view.selectionLocked = false;
  view.selectionSnapshot = null;
  view.term.reset();
  setTimeout(() => { if (T.name === name) fitTerm(); }, 0);
  const wsUrl = new URL(appUrl('api/term/attach'));
  wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cols = view.term.cols || 120, rows = view.term.rows || termRows();
  const connectionId = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  view.auditConnectionId = connectionId;
  wsUrl.search = new URLSearchParams({name, page: TERM_PAGE_ID, token,
                                      connection: connectionId,
                                      cols: String(cols), rows: String(rows)});
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  view.ws = ws;
  if (T.name === name) T.ws = ws;
  const dec = new TextDecoder();
  let outputBytes = 0, outputChunks = 0, outputTimer = 0;
  const flushOutputAudit = () => {
    clearTimeout(outputTimer);
    outputTimer = 0;
    if (!outputChunks) return;
    browserAuditEvent('terminal.output_received', {
      name, bytes: outputBytes, chunks: outputChunks,
    }, null, {uid: T.uid || '', connectionId});
    outputBytes = 0;
    outputChunks = 0;
  };
  ws.onmessage = e => {
    if (view.ws !== ws) return;           // 已替换连接的尾包不能重画新终端
    if (typeof e.data === 'string') {
      try {
        const message = JSON.parse(e.data);
        if (message?.t === 'revoked') {
          handleTermRevoked(view, message.ip);
          return;
        }
      } catch { /* 普通终端字符串按原样渲染 */ }
    }
    const s = typeof e.data === 'string' ? e.data : dec.decode(e.data, { stream: true });
    outputBytes += typeof e.data === 'string'
      ? new TextEncoder().encode(e.data).length : e.data.byteLength;
    outputChunks++;
    if (!outputTimer) outputTimer = setTimeout(flushOutputAudit, 750);
    queueTermOutput(view, s);
  };
  ws.onopen = () => {
    browserAuditEvent('terminal.opened', {name, cols, rows}, null,
      {uid: T.uid || '', connectionId});
    view.reconnectDelay = 500;
    if (T.name === name) {
      syncTermAliases(view);
      fitTerm(true, true);
      settleActivatedTermView(view);
      setScrollPos(0);
      focusTermIfRequested(view);
    }
  };
  ws.onclose = event => {
    if (view.ws !== ws) return;           // 主动换 socket 后，旧 close 事件作废
    queueTermOutput(view, dec.decode());
    flushTermOutput(view);
    flushOutputAudit();
    browserAuditEvent('terminal.closed', {
      name, code: event.code, reason: event.reason, clean: event.wasClean,
    }, null, {uid: T.uid || '', connectionId,
      severity: event.code === 1000 ? 'info' : 'warning'});
    view.ws = null;
    if (T.name === name) {
      T.ws = null;
    }
    if (event.code === 4001 && event.reason.startsWith('revoked:')) {
      handleTermRevoked(view, event.reason.slice('revoked:'.length));
      return;
    }
    if (view.revoked) return;
    // 先刷新 tmux 列表再决定是否重连。若进程刚退出，旧 T.list 仍会短暂把它
    // 判为存活；先排一个重连定时器会向已消失的会话握手，产生 404/close race。
    Promise.resolve(pollLive(true)).finally(() => {
      if (T.views.get(name) === view && !view.ws) scheduleTermReconnect(view);
    });
  };
  ws.onerror = () => browserAuditEvent('terminal.error', {name}, null,
    {uid: T.uid || '', connectionId, severity: 'error'});
  return true;
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
  if (!view || view.revoked || document.hidden || !navigator.onLine || view.reconnectTimer) return;
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
  if (view === currentTermViewObject() && !termPaneRenderable(view)) return;
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
const composerInputHistoryCache = new Map();
const composerHistoryPicker = {
  open: false, uid: null, items: [], index: -1, seq: 0,
};
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

function composerHistoryStamp(entry) {
  return `${entry?.end || 0}:${entry?.version?.head || ''}:${entry?.anchor || ''}`;
}

function nativeComposerHistory(messages) {
  return (messages || []).filter(message =>
    ['user', 'command'].includes(message?.role)
    && message.counted !== false && String(message.text || '').trim()
  ).map((message, index) => ({
    id: `native-${index}`, text: String(message.text), ts: message.ts || null,
  }));
}

function withQueuedComposerHistory(uid, items) {
  const result = items.map(item => ({ ...item }));
  for (const [index, item] of queuedMessages(uid).entries()) {
    if (!String(item?.text || '').trim()) continue;
    result.push({
      id: `queued-${item.id || index}`, text: String(item.text),
      ts: item.created || null,
    });
  }
  return result;
}

async function composerHistoryItems(uid) {
  const entry = cache.get(viewKey(uid));
  let items;
  if (entry && !entry.partial) {
    items = nativeComposerHistory(entry.msgs);
  } else {
    const stamp = composerHistoryStamp(entry);
    const cached = composerInputHistoryCache.get(uid);
    if (cached?.stamp === stamp) {
      items = cached.items.map(item => ({ ...item }));
    } else {
      const query = new URLSearchParams({uid});
      const response = await fetch(appUrl(`api/session/input-history?${query}`));
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
      items = (data.history || []).map((item, index) => ({
        id: `native-${index}`, text: String(item.text || ''), ts: item.ts || null,
      })).filter(item => item.text.trim());
      const resultStamp = `${data.end || 0}:${data.version?.head || ''}:${data.anchor || ''}`;
      composerInputHistoryCache.set(uid, {
        stamp: resultStamp, items: items.map(item => ({ ...item })),
      });
    }
  }
  return withQueuedComposerHistory(uid, items);
}

function closeComposerHistory() {
  const box = $('#input-history');
  composerHistoryPicker.open = false;
  composerHistoryPicker.uid = null;
  composerHistoryPicker.seq++;
  box.classList.add('hidden');
  box.replaceChildren();
  $('#cinput').setAttribute('aria-expanded', 'false');
  $('#cinput').removeAttribute('aria-activedescendant');
}

function setComposerHistoryIndex(index) {
  const picker = composerHistoryPicker;
  if (!picker.open || !picker.items.length) return;
  picker.index = Math.max(0, Math.min(index, picker.items.length - 1));
  const box = $('#input-history');
  box.querySelectorAll('.input-history-item.selected').forEach(node => {
    node.classList.remove('selected');
    node.setAttribute('aria-selected', 'false');
  });
  const selected = box.querySelector(`[data-history-index="${picker.index}"]`);
  selected?.classList.add('selected');
  selected?.setAttribute('aria-selected', 'true');
  box.querySelector('.input-history-position').textContent =
    `${picker.index + 1} / ${picker.items.length}`;
  if (selected?.id) $('#cinput').setAttribute('aria-activedescendant', selected.id);
  selected?.scrollIntoView({block: 'nearest'});
}

function renderComposerHistory(state = 'ready') {
  const picker = composerHistoryPicker;
  const box = $('#input-history');
  box.replaceChildren();
  box.classList.remove('hidden');
  $('#cinput').setAttribute('aria-expanded', 'true');
  const head = el('div', 'input-history-head');
  head.appendChild(el('span', '', '输入历史'));
  const position = el('span', 'input-history-position',
    state === 'loading' ? '加载中…'
      : `${Math.max(0, picker.index + 1)} / ${picker.items.length}`
        + (state === 'refreshing' ? ' · 加载全部…' : ''));
  head.appendChild(position);
  box.appendChild(head);
  if (state === 'loading' || !picker.items.length) {
    box.appendChild(el('div', 'input-history-empty',
      state === 'loading' ? '正在加载输入历史…' : '暂无输入历史'));
    return;
  }
  const list = el('div', 'input-history-list');
  picker.items.forEach((item, index) => {
    const button = el('button', 'input-history-item');
    button.type = 'button';
    button.id = `input-history-option-${index}`;
    button.dataset.historyIndex = index;
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', 'false');
    const text = el('span', 'input-history-text');
    text.textContent = item.text.slice(0, 600);
    const meta = document.createElement('small');
    meta.textContent = item.ts ? fmtTime(item.ts) : `${index + 1}`;
    button.append(text, meta);
    button.onmouseenter = () => setComposerHistoryIndex(index);
    button.onmousedown = event => event.preventDefault();
    button.onclick = () => {
      setComposerHistoryIndex(index);
      acceptComposerHistory();
    };
    list.appendChild(button);
  });
  box.appendChild(list);
  setComposerHistoryIndex(picker.index);
}

async function openComposerHistory() {
  const ta = $('#cinput');
  const uid = composerUid;
  if (!uid || composerSending || ta.value !== '') return;
  closeAttachMenu();
  const seq = ++composerHistoryPicker.seq;
  Object.assign(composerHistoryPicker, {
    open: true, uid, items: [], index: -1,
  });
  const entry = cache.get(viewKey(uid));
  const seed = withQueuedComposerHistory(uid, nativeComposerHistory(entry?.msgs));
  if (seed.length) {
    composerHistoryPicker.items = seed;
    composerHistoryPicker.index = seed.length - 1;
    renderComposerHistory(entry?.partial ? 'refreshing' : 'ready');
  } else {
    renderComposerHistory('loading');
  }
  try {
    const items = await composerHistoryItems(uid);
    if (!composerHistoryPicker.open || composerHistoryPicker.uid !== uid
        || composerHistoryPicker.seq !== seq || composerUid !== uid || ta.value !== '') return;
    const selected = composerHistoryPicker.items[composerHistoryPicker.index];
    composerHistoryPicker.items = items;
    const preserved = selected ? items.findLastIndex(item =>
      item.text === selected.text && item.ts === selected.ts) : -1;
    composerHistoryPicker.index = preserved >= 0 ? preserved : items.length - 1;
    renderComposerHistory();
  } catch (error) {
    if (!composerHistoryPicker.open || composerHistoryPicker.seq !== seq) return;
    composerHistoryPicker.items = [];
    composerHistoryPicker.index = -1;
    renderComposerHistory();
    $('#input-history .input-history-empty').textContent =
      `读取失败：${error.message || error}`;
  }
}

function acceptComposerHistory() {
  const picker = composerHistoryPicker;
  const item = picker.items[picker.index];
  if (!picker.open || !item) return false;
  const ta = $('#cinput');
  closeComposerHistory();
  ta.value = item.text;
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  return true;
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
  closeComposerHistory();
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

async function prepareTerminalDraft(uid) {
  const name = takenOver(uid);
  const cli = sesmanCli(uid);
  if (!name || !['claude', 'codex'].includes(cli?.source) || uid.startsWith('tmux:')) {
    return { proceed: true, overwriteDraft: '' };
  }
  let d;
  try {
    d = await post('api/session/draft-status', { uid, name });
  } catch (error) {
    alert('发送失败: ' + (error.message || error));
    return { proceed: false, overwriteDraft: '' };
  }
  if (d.error) {
    alert('发送失败: ' + d.error);
    return { proceed: false, overwriteDraft: '' };
  }
  if (!d.draft_conflict) return { proceed: true, overwriteDraft: '' };
  if (!confirmTerminalDraftOverwrite()) {
    return { proceed: false, overwriteDraft: '' };
  }
  return { proceed: true, overwriteDraft: d.draft_token || '' };
}

async function sendToSession(text, keys, uid = S.sel, media = [], options = {}) {
  const name = takenOver(uid);
  if (!name) return false;
  const cli = sesmanCli(uid);
  const serverQueued = !!text && ['claude', 'codex'].includes(cli?.source)
    && !uid.startsWith('tmux:');
  const queuedId = text && !serverQueued && typeof queuePendingUserMessage === 'function'
    ? queuePendingUserMessage(uid, text, media) : null;
  let d;
  try {
    if (serverQueued) {
      const entry = cache.get(viewKey(uid));
      const activity = entry?.activity || null;
      const requestId = String(options.requestId || '')
        || globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      let overwriteDraft = String(options.overwriteDraft || '');
      for (let attempt = 0; attempt < 3; attempt++) {
        d = await post('api/session/send', {
          uid, name, text, media, activity, request_id: requestId,
          page_id: TERM_PAGE_ID,
          overwrite_draft: overwriteDraft,
          cursor: entry ? {
            start: entry.end, head: entry.version?.head, anchor: entry.anchor,
          } : null,
        });
        if (!d.draft_conflict) break;
        if (!confirmTerminalDraftOverwrite()) return false;
        overwriteDraft = d.draft_token || '';
      }
      if (d?.draft_conflict) {
        alert('终端草稿持续变化，消息未发送');
        return false;
      }
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
    syncServerOutbox(uid, d.outbox || [], d.outbox_version);
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
  if (keys?.includes('Enter') && claudeRewinds.has(name)) {
    scheduleClaudeRewindSync(name);
  }
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
    const kindName = attachment.kind === 'file' ? '文件'
      : ({ image: '图片', video: '视频', audio: '音频' }[attachment.kind]);
    const summary = `${ref} · ${kindName} · ${fmtSize(attachment.file.size)}`;
    meta.textContent = attachment.status === 'uploading' ? `${summary} · 正在上传…`
      : attachment.status === 'failed' ? `${summary} · ${attachment.error || '上传失败'}`
        : summary;
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

function clipboardAttachmentFiles(data) {
  const files = [];
  const seen = new Set();
  const add = file => {
    if (!(file instanceof File)) return;
    const key = `${file.name}\0${file.type}\0${file.size}\0${file.lastModified}`;
    if (seen.has(key)) return;
    seen.add(key);
    files.push(file);
  };
  // Chromium 通常把文件放在 files；部分浏览器/桌面剪贴板只在 items
  // 暴露非图片文件（CSV 尤其常见）。两边都读并去重。
  for (const file of [...(data?.files || [])]) add(file);
  for (const item of [...(data?.items || [])]) {
    if (item.kind === 'file') add(item.getAsFile?.());
  }
  return files;
}

function clipboardDirectoryNames(data) {
  const names = [];
  for (const item of [...(data?.items || [])]) {
    if (item.kind !== 'file') continue;
    const getEntry = item.getAsEntry || item.webkitGetAsEntry;
    let entry = null;
    try { entry = getEntry?.call(item); } catch { /* 浏览器不允许读取该项 */ }
    if (entry?.isDirectory) names.push(entry.name || '文件夹');
  }
  return names;
}

function clipboardCsvFile(data, callback) {
  const csvTypes = new Set([
    'text/csv', 'text/comma-separated-values', 'application/csv',
    'application/vnd.ms-excel',
  ]);
  const item = [...(data?.items || [])].find(x =>
    x.kind === 'string' && csvTypes.has(String(x.type || '').toLowerCase()));
  if (!item) return false;
  const mime = String(item.type || 'text/csv').toLowerCase();
  const accept = text => {
    if (typeof text === 'string' && text.length) {
      callback(new File([text], 'clipboard.csv', { type: mime, lastModified: Date.now() }));
    }
  };
  // getData 是同步的，但有些 DataTransfer 实现只支持 getAsString。
  const immediate = data.getData?.(item.type);
  if (immediate) accept(immediate);
  else item.getAsString?.(accept);
  return true;
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
  closeComposerHistory();
  composerSending = true;
  button.disabled = true;
  add.disabled = true;
  renderComposerItems();
  try {
    const draftPolicy = await prepareTerminalDraft(uid);
    if (!draftPolicy.proceed) return;
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
    // Keep one idempotency key while retrying the exact same draft after a lost
    // HTTP response.  Editing the prompt intentionally starts a new submission.
    if (draft.requestText !== prompt || !draft.requestId) {
      draft.requestText = prompt;
      draft.requestId = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    const sent = await sendToSession(
      prompt, null, uid, sentMedia,
      { overwriteDraft: draftPolicy.overwriteDraft, requestId: draft.requestId });
    // 请求失败时保留草稿；等待响应期间若用户继续编辑，也不能抹掉新内容。
    if (sent) {
      delete draft.requestId;
      delete draft.requestText;
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
  if (composerHistoryPicker.open && e.target.value !== '') closeComposerHistory();
  autoGrow(e.target);
});
$('#cinput').addEventListener('keydown', e => {
  if (e.isComposing) return;
  if (composerHistoryPicker.open) {
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setComposerHistoryIndex(composerHistoryPicker.index - 1);
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setComposerHistoryIndex(composerHistoryPicker.index + 1);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      acceptComposerHistory();
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeComposerHistory();
      return;
    }
    if (!['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) closeComposerHistory();
  } else if (e.key === 'ArrowUp' && e.currentTarget.value === '') {
    e.preventDefault();
    openComposerHistory();
    return;
  }
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

function scheduleClaudeRewindSync(name, delay = 450) {
  const state = claudeRewinds.get(name);
  if (!state) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => syncClaudeRewind(name), delay);
}

async function syncClaudeRewind(name) {
  const state = claudeRewinds.get(name);
  if (!state || state.syncing) return;
  state.syncing = true;
  try {
    const result = await post('api/session/rewind', {
      action: 'sync', uid: state.uid, name,
    });
    if (result.error) return;
    if (!result.pending) claudeRewinds.delete(name);
    if (result.changed) {
      // timeline pin 会改变 cursor 的逻辑叶子，即便 JSONL 一个字节都没变；
      // 用现有增量接口拿 reset，原子替换缓存和当前 DOM。
      S.lastSync = 0;
      await syncSession(state.uid);
    }
  } catch { /* 终端仍可继续使用；下一次 Enter 会重试同步 */ }
  finally {
    const current = claudeRewinds.get(name);
    if (current) current.syncing = false;
  }
}

async function revealNativeTerminal(uid = S.sel) {
  const name = takenOver(uid);
  if (!name || S.sel !== uid) return false;
  T.uid = uid;
  await openTermPane(name, true, MOBILE.matches ? null : 'full');
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

async function answerCliQuestionForm(uid, optionIndexes) {
  const prompt = activeCliQuestion(uid);
  const cli = sesmanCli(uid);
  if (!cli?.canAnswerQuestionForm(prompt)) return false;
  const groups = cli.questionFormAnswerKeyGroups(prompt, optionIndexes);
  if (!groups?.length) return false;
  for (let i = 0; i < groups.length; i++) {
    if (!await sendToSession(null, groups[i], uid)) return false;
    // Enter 会让 Claude 卸载当前题并渲染下一题或 Review。分开发送并留出
    // 一个短事件循环间隔，避免后一题按键被旧题的输入处理器吞掉。
    if (i < groups.length - 1) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  return true;
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
  // 揭示原生 TUI；这里只记住进入选择器前的叶子，最终选择仍服从原生菜单。
  try {
    const began = await post('api/session/rewind', {action: 'begin', uid, name});
    if (began.ok) claudeRewinds.set(name, {uid, timer: null, syncing: false});
  } catch { /* 记录失败不应阻止原生回滚 */ }
  await revealNativeTerminal(uid);
  return sent;
}
$('#cesc').onclick = () => sendComposerEscape();

$('#cadd').onclick = e => {
  e.stopPropagation();
  closeComposerHistory();
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
  if (!e.target.closest('.composer-input-wrap')) closeComposerHistory();
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
  const directories = clipboardDirectoryNames(e.clipboardData);
  const files = clipboardAttachmentFiles(e.clipboardData);
  if (directories.length) {
    e.preventDefault();
    if (files.length) addComposerFiles(files);
    alert(`暂不支持直接粘贴文件夹：${directories.join('、')}。请先压缩后再粘贴。`);
    return;
  }
  if (!files.length) {
    // 表格软件偶尔只提供 text/csv 剪贴板项而不提供 File。此时保留其
    // 二进制附件语义；普通 text/plain 粘贴仍完全交给浏览器。
    if (!clipboardCsvFile(e.clipboardData, file => addComposerFiles([file]))) return;
    e.preventDefault();
    return;
  }
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
  for (const view of T.views.values()) {
    view.resumeFocus = !!document.activeElement && view.host.contains(document.activeElement);
  }
  suspendTerm();
}
function foregroundTerm(force = false) {
  if (document.hidden || (!force && !termWasBackgrounded)) return;
  termWasBackgrounded = false;
  for (const view of T.views.values()) {
    if (view.webgl) {
      try { view.term.clearTextureAtlas(); } catch {}
    }
    if (view.resumeFocus) requestTermFocus(view, document.body);
    view.resumeFocus = false;
    reconnectTerm(view);
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) backgroundTerm();
  else foregroundTerm();
});
addEventListener('pagehide', backgroundTerm);
addEventListener('pageshow', e => foregroundTerm(e.persisted));
addEventListener('online', () => foregroundTerm(true));

loadTermList();
