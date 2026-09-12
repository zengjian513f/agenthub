'use strict';

const SOURCES = Object.freeze(Object.fromEntries(
  Object.values(AGENTHUB_CLIS).map(cli => [cli.source, {
    name: cli.name, icon: cli.icon, color: cli.color,
  }])));

// 所有界面状态都落 localStorage, 刷新后原样恢复
const store = {
  get(k, d) {
    try {
      const v = localStorage.getItem(STORAGE_PREFIX + k);
      return v === null ? d : JSON.parse(v);
    } catch { return d; }
  },
  set: (k, v) => localStorage.setItem(STORAGE_PREFIX + k, JSON.stringify(v)),
};

// 存储结构的版本只由公共层调度；每种 CLI 自己决定怎样迁移旧队列。
const QUEUED_MESSAGES_VERSION = 5;
function loadQueuedMessages() {
  const saved = store.get('queuedMessages', []);
  const valid = Array.isArray(saved) ? saved : [];
  const fromVersion = +store.get('queuedMessagesVersion', 1) || 1;
  if (fromVersion !== QUEUED_MESSAGES_VERSION) {
    const migrated = valid.flatMap(([uid, items]) => {
      const kept = agenthubCli(uid)?.migrateQueuedMessages(
        items, fromVersion, QUEUED_MESSAGES_VERSION) || [];
      return kept.length ? [[uid, kept]] : [];
    });
    store.set('queuedMessages', migrated);
    store.set('queuedMessagesVersion', QUEUED_MESSAGES_VERSION);
    return migrated;
  }
  return valid;
}

const FONT_CHOICES = AgentHubTypography.choices;
const themeMedia = matchMedia('(prefers-color-scheme: dark)');

function applyTheme(choice = store.get('theme', 'system'), persist = false) {
  if (!['system', 'light', 'dark'].includes(choice)) choice = 'system';
  if (persist) store.set('theme', choice);
  document.documentElement.dataset.theme = choice === 'system'
    ? (themeMedia.matches ? 'dark' : 'light') : choice;
  if (typeof refreshTerminalPreferences === 'function') refreshTerminalPreferences(true);
}

function applyFont(choice = store.get('font', 'ubuntu'), persist = false) {
  if (!FONT_CHOICES[choice]) choice = 'ubuntu';
  if (persist) store.set('font', choice);
  document.documentElement.style.setProperty('--terminal-font', FONT_CHOICES[choice]);
  if (typeof refreshTerminalPreferences === 'function') refreshTerminalPreferences(false);
}

function applyToolIcons(choice = store.get('toolIcons', 'brand'), persist = false) {
  if (!['brand', 'boss'].includes(choice)) choice = 'brand';
  if (persist) store.set('toolIcons', choice);
  document.documentElement.dataset.toolIcons = choice;
}

themeMedia.addEventListener('change', () => {
  if (store.get('theme', 'system') === 'system') applyTheme('system');
});
applyTheme();
applyFont();
applyToolIcons();

const S = {
  sessions: [],
  view: store.get('view', 'tree'),
  nest: store.get('nest', false),  // 左栏分层：子代理和由会话发起的会话缩进在发起者下
  nestClosed: new Set(store.get('nestClosed', [])),  // 分层里手动收起的发起者 uid
  off: new Set(store.get('off', [])),
  closed: new Set(store.get('closed', [])),
  sel: null,
  term: '',           // 当前要高亮的词 (= 搜索框内容)
  opts: Object.assign({ case: false, word: false, regex: false }, store.get('opts', {})),
  cur: -1,            // 匹配跳转游标
  autoOpen: 0,        // 本次渲染已自动展开的命中消息数
  markCapped: false,  // 高亮是否因数量上限被截断
  results: null,      // 全文搜索结果, null 表示未处于搜索态
  agent: null,        // 当前查看的子代理 id；null = 主会话
  syncGap: 350,       // 当前会话的同步间隔, 随有无新内容自适应
  live: new Set(),    // 仍在运行的会话 uid
  liveTmux: new Set(),// 其中运行在 tmux 里的会话 uid
  liveStarted: new Map(), // uid → 当前 CLI 主进程启动时间（Unix 秒）
  activeOnly: store.get('activeOnly', false), // 左栏只显示仍在运行的会话
  compactTurns: store.get('compactTurns', true), // 已完成回合只保留过程合集与最终结论
  unread: new Map(store.get('unread', [])),   // uid → {count, tmux}; 只计代理产生的新内容
  cursors: new Map(), // 主会话/子代理 EOF 游标；用于后台会话的精确未读增量
  queued: new Map(loadQueuedMessages()),       // uid → 尚未写入原生会话记录的已发送消息
  outboxVersions: new Map(), // uid → 最近接受的服务端发送账本快照版本
  retiredOutboxEpochs: new Set(), // 服务重启后拒收仍在网络中滞留的旧进程快照
  starBusy: new Set(), // 正在持久化星标的会话，避免多个网页请求在服务端乱序
  picking: false,     // 左栏多选模式；刻意不持久化，刷新后回到普通浏览
  sig: null,          // 列表对应的磁盘签名
  lastSync: 0,
};

const $ = s => document.querySelector(s);
const MOBILE = matchMedia('(max-width: 720px)');
// 顶栏和会话头按三级宽度排版：窄屏 ≤720，中屏 721–1199，宽屏 ≥1200。断点与 style.css 一致。
const MEDIUM = matchMedia('(max-width: 1199px)');
function layoutTier() { return MOBILE.matches ? 'narrow' : MEDIUM.matches ? 'medium' : 'wide'; }
// 页面既可挂在站点根目录，也可由反代放到 /agenthub/ 之类的子路径。
const APP_BASE = new URL('.', location.href);
const DEBUG_RUN = /^[A-Za-z0-9_-]{1,64}$/.test(
  new URLSearchParams(location.search).get('debug_run') || '')
  ? new URLSearchParams(location.search).get('debug_run') : '';
// 深链：?sid=<source>:<sid> 或 ?sid=<sid>，打开指定会话（labdesk 的会话台账用它跳过来）。
// 用 CLI 原生会话号而不是 uid —— uid 是会话文件路径的散列，换目录就变。
const DEEP_SID = (new URLSearchParams(location.search).get('sid') || '').trim().slice(0, 128);
const DEEP_NODE = new URLSearchParams(location.search).get('node') || '';
const appUrl = path => {
  const url = new URL(String(path).replace(/^\//, ''), APP_BASE);
  if (DEBUG_RUN && url.pathname.includes('/api/')) {
    url.searchParams.set('debug_run', DEBUG_RUN);
  }
  if (HUB_MODE && !url.searchParams.has('nodes') && /\/api\/(search|trash|trash\/purge)$/.test(url.pathname)) {
    url.searchParams.set('nodes', selectedNodeIds().join(','));
  }
  if (HUB_MODE && url.pathname.endsWith('/api/term/complete-dir')) url.searchParams.set('node', newNodeId());
  return url.toString();
};
const BUILD_ID = document.querySelector('meta[name="agenthub-build"]')?.content || '';
// One ephemeral page identity joins HTTP, SSE, terminal and final DOM receipts.
// It intentionally is not persisted: duplicated/restored tabs must remain distinct.
const AUDIT_PAGE_ID = globalThis.crypto?.randomUUID?.()
  || [...globalThis.crypto.getRandomValues(new Uint8Array(16))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
window.__agenthubPageId = AUDIT_PAGE_ID;

let browserAuditQueue = [];
let browserAuditTimer = 0;
let browserAuditSending = false;
let browserAuditFailCount = 0;
const AUDIT_BATCH_BYTES = 48 * 1024;
const AUDIT_BATCH_COUNT = 20;
const AUDIT_CONTENT_LIMIT = 32 * 1024;

// 按 UTF-8 字节算：sendBeacon 的 64 KB 配额是字节数，中文一个字占 3 字节
const auditEncoder = new TextEncoder();
function auditEventBytes(event) {
  try { return auditEncoder.encode(JSON.stringify(event)).length; } catch { return AUDIT_BATCH_BYTES + 1; }
}

function spliceAuditBatch(queue, limit = AUDIT_BATCH_BYTES, count = AUDIT_BATCH_COUNT) {
  if (!queue.length) return [];
  if (auditEventBytes(queue[0]) > limit) return queue.splice(0, 1);
  const batch = [];
  let bytes = 0;
  while (queue.length && batch.length < count) {
    const size = auditEventBytes(queue[0]);
    if (bytes + size > limit) break;
    batch.push(queue.shift());
    bytes += size;
  }
  return batch;
}

function capAuditQueue() {
  if (browserAuditQueue.length > 500) browserAuditQueue.splice(0, browserAuditQueue.length - 500);
}

function browserAuditEvent(event, data = {}, content = null, fields = {}) {
  try {
    let auditContent = content;
    let auditData = data;
    try {
      if (content != null) {
        const serialized = JSON.stringify(content);
        if (serialized.length > AUDIT_CONTENT_LIMIT) {
          auditContent = {truncated: true, bytes: serialized.length,
            head: serialized.slice(0, 8 * 1024)};
          auditData = {...data, content_truncated: true};
        }
      }
    } catch {
      auditContent = null;
    }
    browserAuditQueue.push({
      event, ts: new Date().toISOString(), uid: fields.uid ?? S.sel ?? '',
      trace_id: fields.traceId || '', request_id: fields.requestId || '',
      connection_id: fields.connectionId || '', severity: fields.severity || 'info',
      data: auditData, content: auditContent,
    });
    capAuditQueue();
    if (!browserAuditTimer) browserAuditTimer = setTimeout(flushBrowserAudit, 750);
  } catch { /* diagnostics never change UI behavior */ }
}

function auditPayload(events) {
  return JSON.stringify({
    page_id: AUDIT_PAGE_ID, uid: S.sel || '', _build: BUILD_ID, events,
  });
}

async function flushBrowserAudit() {
  clearTimeout(browserAuditTimer);
  browserAuditTimer = 0;
  if (browserAuditSending || !browserAuditQueue.length) return;
  const events = spliceAuditBatch(browserAuditQueue);
  if (!events.length) return;
  browserAuditSending = true;
  try {
    const response = await fetch(appUrl('api/audit/browser'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json', 'X-AgentHub-Page': AUDIT_PAGE_ID,
        'X-AgentHub-Build': BUILD_ID,
      },
      body: auditPayload(events),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    browserAuditFailCount = 0;
  } catch (error) {
    // 断网期间只是攒着，不算失败次数；连续三次在线失败才认定这一批本身有问题
    if (navigator.onLine) browserAuditFailCount += 1;
    if (browserAuditFailCount >= 3) {
      browserAuditFailCount = 0;
      const bytes = events.reduce((n, ev) => n + auditEventBytes(ev), 0);
      browserAuditQueue.push({
        event: 'audit.dropped', ts: new Date().toISOString(), uid: S.sel ?? '',
        trace_id: '', request_id: '', connection_id: '', severity: 'info',
        data: {events: events.length, bytes,
          reason: String(error?.message || error || '').slice(0, 200)},
        content: null,
      });
      capAuditQueue();
    } else {
      browserAuditQueue.unshift(...events);
      if (browserAuditQueue.length > 500) browserAuditQueue.length = 500;
    }
  } finally {
    browserAuditSending = false;
    if (browserAuditQueue.length && !browserAuditTimer) {
      browserAuditTimer = setTimeout(flushBrowserAudit, 1500);
    }
  }
}

// 卸载时整个页面只有 64 KB 的 beacon 配额（和 keepalive 同一个池），超出的包
// sendBeacon 直接返回 false。所以先把各事件的 content 丢掉保住事件本身，只给
// page.hidden 那条快照留 content；分两包发，合计不超过 60 KB。发不出去的留在
// 队列里，页面若只是进了 bfcache 还能补发。
function flushBrowserAuditBeacon() {
  clearTimeout(browserAuditTimer);
  browserAuditTimer = 0;
  if (!navigator.sendBeacon || !browserAuditQueue.length) return;
  const slim = browserAuditQueue.map(ev => ev.event === 'page.hidden' || ev.content == null ? ev
    : {...ev, content: null, data: {...ev.data, content_dropped: true}});
  browserAuditQueue = [];
  for (const budget of [40 * 1024, 20 * 1024]) {
    const events = spliceAuditBatch(slim, budget, 100);
    if (!events.length) break;
    const ok = navigator.sendBeacon(appUrl('api/audit/browser'),
      new Blob([auditPayload(events)], {type: 'application/json'}));
    if (!ok) { slim.unshift(...events); break; }
  }
  if (slim.length) {
    browserAuditQueue.unshift(...slim);
    capAuditQueue();
  }
}

const nativeAlert = window.alert.bind(window);
const nativeConfirm = window.confirm.bind(window);
window.alert = message => {
  const text = String(message ?? '').slice(0, 500);
  browserAuditEvent('dialog.shown', {kind: 'alert', text});
  try { return nativeAlert(message); }
  finally { browserAuditEvent('dialog.closed', {kind: 'alert', result: null}); }
};
window.confirm = message => {
  const text = String(message ?? '').slice(0, 500);
  browserAuditEvent('dialog.shown', {kind: 'confirm', text});
  let result = null;
  try { return (result = nativeConfirm(message)); }
  finally { browserAuditEvent('dialog.closed', {kind: 'confirm', result}); }
};

function browserStateSnapshot(reason = '') {
  const box = $('#msgs');
  const nodes = box ? [...box.querySelectorAll('.msg, #activity, .client-outbox')].slice(-40) : [];
  const entry = S.sel ? cache.get(viewKey(S.sel, S.agent)) : null;
  const termState = typeof T === 'undefined' ? null : {
    name: T.name, uid: T.uid, mode: T.mode,
    visible: !$('#termpane')?.classList.contains('hidden'),
    connected: T.ws?.readyState ?? null,
  };
  return {
    data: {
      reason, selected: S.sel, agent: S.agent, mobile: MOBILE.matches,
      mobile_detail: document.body.classList.contains('mobile-detail'),
      visibility: document.visibilityState, online: navigator.onLine,
      viewport: {width: innerWidth, height: innerHeight,
        visual_width: window.visualViewport?.width,
        visual_height: window.visualViewport?.height},
      cache: entry ? {messages: entry.msgs?.length || 0, end: entry.end,
        anchor: entry.anchor, activity: entry.activity?.state || '',
        outbox: queuedMessages(S.sel).map(item => ({id: item.id, state: item.state}))} : null,
      dom_messages: nodes.length, terminal: termState,
      header: consoleButtonState(),
    },
    content: {
      composer: $('#cinput')?.value || '',
      messages: nodes.map(node => ({
        id: node.id || '', role: node.dataset?.role || '',
        call_id: node.dataset?.callId || '', classes: node.className,
        text: (node.textContent || '').slice(0, 4000),
      })),
    },
  };
}

let browserSnapshotTimer = 0;
let lastBrowserSnapshot = '';
function scheduleBrowserSnapshot(reason = 'render') {
  clearTimeout(browserSnapshotTimer);
  browserSnapshotTimer = setTimeout(() => {
    try {
      const snapshot = browserStateSnapshot(reason);
      const signature = JSON.stringify(snapshot);
      if (signature === lastBrowserSnapshot) return;
      lastBrowserSnapshot = signature;
      browserAuditEvent('dom.snapshot', snapshot.data, snapshot.content);
    } catch { /* page can be between detail teardown and rebuild */ }
  }, 100);
}

// ---- 会话标题栏 / 控制台按钮的存在性审计 ----
// 控制台按钮"偶尔消失"一直没抓到现场：快照只看消息，不看标题栏。这里独立记录
// 按钮的三层状态——在不在 DOM、几何上有没有被裁掉/盖住（elementFromPoint）、
// #right 有没有被滚走——任何一层不成立就记一条 console.button.missing，附上
// 标题栏 HTML 和布局数据；恢复时记 console.button.restored。
// 常规状态变化（文案、接管态、灰态、位置）按签名去重记 console.button.state。
function consoleButtonState() {
  const button = $('#a-term');
  const right = $('#right');
  const detail = $('#detail');
  const head = detail?.querySelector(':scope > .dhead');
  const shown = !!right && right.offsetWidth > 0;   // 手机列表页 #right 整个 display:none
  const state = {
    present: !!button, head: !!head, shown,
    detail_children: detail ? [...detail.children].map(
      node => node.id || node.className.split(' ')[0] || node.tagName.toLowerCase()).slice(0, 8) : null,
    right_scroll: right ? [right.scrollLeft, right.scrollTop] : null,
    right_overflow: right ? [right.scrollWidth - right.clientWidth, right.scrollHeight - right.clientHeight] : null,
    tier: layoutTier(), mobile_detail: document.body.classList.contains('mobile-detail'),
  };
  if (button) {
    const style = getComputedStyle(button);
    state.label = button.ariaLabel || '';
    state.on = button.classList.contains('on');
    state.unavailable = button.dataset.unavailable === 'true';
    state.hidden = button.hidden || style.display === 'none' || style.visibility !== 'visible'
      || parseFloat(style.opacity) === 0;
  }
  if (button && shown) {
    const r = button.getBoundingClientRect(), rr = right.getBoundingClientRect();
    const hit = r.width && r.height && document.visibilityState === 'visible'
      ? document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) : undefined;
    state.rect = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
    state.in_right = r.width > 0 && r.right <= rr.right + .5 && r.left >= rr.left - .5
      && r.top >= rr.top - .5 && r.bottom <= rr.bottom + .5;
    // 页面不可见时 elementFromPoint 一律 null，不算被遮
    state.hit = hit === undefined ? null : !!hit && button.contains(hit);
    // SVG 元素的 className 是 SVGAnimatedString，只能从 classList 取
    state.hit_target = hit ? (hit.id ? '#' + hit.id : hit.classList?.[0] ? '.' + hit.classList[0]
      : hit.tagName.toLowerCase()) : null;
  }
  // 不在 DOM 一定算丢；在 DOM 但页面可见时被隐藏/裁掉/盖住也算丢
  state.ok = state.present && (!shown || (!state.hidden && state.in_right && state.hit !== false));
  return state;
}

let consoleButtonMissingSince = 0;
let consoleButtonSignature = '';
let consoleButtonTimer = 0;
function auditConsoleButton(reason = '') {
  let state;
  try { state = consoleButtonState(); } catch { return; }
  const content = () => {
    const head = $('#detail > .dhead');
    return {dhead: head ? head.outerHTML.slice(0, 12000) : null,
      detail: $('#detail')?.innerHTML.slice(0, 2000) || null};
  };
  const term = typeof T === 'undefined' ? null : {
    name: T.name, uid: T.uid, mode: T.mode, views: [...T.views.keys()],
    visible: !$('#termpane')?.classList.contains('hidden'),
  };
  if (!state.ok) {
    // 丢失期间最多 5 秒记一次，免得 MutationObserver/定时器把库刷爆
    const now = Date.now();
    if (consoleButtonMissingSince && now - consoleButtonMissingSince < 5000) return;
    consoleButtonMissingSince = consoleButtonMissingSince || now;
    browserAuditEvent('console.button.missing', {reason, ...state, selected: S.sel, agent: S.agent,
      terminal: term}, content(), {severity: 'error'});
    return;
  }
  if (consoleButtonMissingSince) {
    browserAuditEvent('console.button.restored', {reason, ...state,
      missing_ms: Date.now() - consoleButtonMissingSince, terminal: term}, null, {severity: 'warning'});
    consoleButtonMissingSince = 0;
  }
  const signature = JSON.stringify([state.label, state.on, state.unavailable, state.rect, state.tier,
    state.hit_target, state.right_scroll]);
  if (signature === consoleButtonSignature) return;
  consoleButtonSignature = signature;
  browserAuditEvent('console.button.state', {reason, ...state, terminal: term});
}
function scheduleConsoleButtonAudit(reason = '') {
  clearTimeout(consoleButtonTimer);
  consoleButtonTimer = setTimeout(() => auditConsoleButton(reason), 150);
}
{
  // #detail 换内容（读取中/失败/正式渲染/新会话页）时重新盯住新的标题栏；标题栏
  // 内部的增删和 class/hidden/style 变化也触发检查。#msgs 的海量变动不在观察范围内。
  const headObserver = new MutationObserver(() => scheduleConsoleButtonAudit('head-mutation'));
  let observedHead = null;
  const watchHead = () => {
    const head = $('#detail > .dhead');
    if (head === observedHead) return;
    headObserver.disconnect();
    observedHead = head;
    if (head) headObserver.observe(head, {childList: true, subtree: true, attributes: true,
      attributeFilter: ['class', 'hidden', 'style', 'aria-label']});
  };
  new MutationObserver(() => { watchHead(); scheduleConsoleButtonAudit('detail-mutation'); })
    .observe($('#detail'), {childList: true});
  watchHead();
  // 裁切/滚走这类不改 DOM 的情况靠 #right 的 scroll 和低频巡检兜底
  $('#right')?.addEventListener('scroll', () => scheduleConsoleButtonAudit('right-scroll'), {passive: true});
  setInterval(() => { if (!document.hidden) auditConsoleButton('periodic'); }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) scheduleConsoleButtonAudit('visible');
  });
}

/** #detail 每次换内容都记一笔：谁换的、换完有没有标题栏和控制台按钮。 */
function auditDetailRendered(source, extra = {}) {
  const detail = $('#detail');
  browserAuditEvent('detail.rendered', {
    source, selected: S.sel, agent: S.agent, ...extra,
    has_head: !!detail?.querySelector(':scope > .dhead'), has_console_button: !!$('#a-term'),
    children: detail ? [...detail.children].map(
      node => node.id || node.className.split(' ')[0] || node.tagName.toLowerCase()).slice(0, 8) : null,
  });
}

queueMicrotask(() => browserAuditEvent('page.loaded', {
  url: location.pathname + location.search, referrer: document.referrer,
  user_agent: navigator.userAgent, language: navigator.language,
  viewport: {width: innerWidth, height: innerHeight}, theme: document.documentElement.dataset.theme,
}));
window.addEventListener('error', event => browserAuditEvent('error', {
  message: event.message, filename: event.filename, line: event.lineno, column: event.colno,
}, null, {severity: 'error'}));
window.addEventListener('unhandledrejection', event => browserAuditEvent('unhandledrejection', {
  reason: String(event.reason?.stack || event.reason || 'unknown'),
}, null, {severity: 'error'}));
window.addEventListener('online', () => browserAuditEvent('network.online'));
window.addEventListener('offline', () => browserAuditEvent('network.offline', {}, null,
  {severity: 'warning'}));
window.addEventListener('pagehide', () => {
  const snapshot = browserStateSnapshot('pagehide');
  browserAuditEvent('page.hidden', snapshot.data, snapshot.content);
  flushBrowserAuditBeacon();
});
document.addEventListener('visibilitychange', () => {
  browserAuditEvent('visibility.changed', {visibility: document.visibilityState});
  if (!document.hidden) scheduleBrowserSnapshot('visible');
});
document.addEventListener('click', event => {
  const target = event.target?.closest?.(
    'button, a, [role="button"], .item, .ghead, summary, label, input, select, textarea, [data-action], [data-term-key], [data-term-modifier], .fold-preview, .turn-toolbar');
  if (!target) return;
  browserAuditEvent('ui.clicked', {
    tag: target.tagName, id: target.id || '', classes: target.className || '',
    title: target.getAttribute('title') || '', uid: target.dataset?.uid || '',
    action: target.dataset?.v || target.dataset?.termKey || target.dataset?.attach || '',
    text: (target.textContent || '').trim().slice(0, 80),
    x: Math.round(event.clientX), y: Math.round(event.clientY),
  });
}, true);
let auditResizeTimer = 0;
window.addEventListener('resize', () => {
  clearTimeout(auditResizeTimer);
  auditResizeTimer = setTimeout(() => {
    browserAuditEvent('viewport.resized', {
      width: innerWidth, height: innerHeight,
      visual_width: window.visualViewport?.width,
      visual_height: window.visualViewport?.height,
    });
    scheduleBrowserSnapshot('resized');
  }, 200);
});
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const icon = src => `<svg class="ico source-icon" data-source="${src}" aria-hidden="true" style="color:${SOURCES[src].color}"><use href="#${SOURCES[src].icon}"/></svg>`;
// 会话头的图标：右上角的运行点和左栏列表一致（绿=直接进程，蓝=tmux）
const sessionIconMarkup = (src, live, tmux) => `<span class="ico">${icon(src)}<span
  class="item-status${live ? ' visible' : ''}${tmux ? ' tmux' : ''}" id="dlive"
  title="${tmux ? '运行于 tmux' : '运行中'}" aria-label="${tmux ? '运行于 tmux' : '运行中'}"></span></span>`;
const uiIcon = name => `<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

let staleBuildShown = false;
function markStaleBuild(serverBuild = '') {
  if (staleBuildShown) return;
  staleBuildShown = true;
  document.body.classList.add('stale-build');
  const notice = el('div', 'version-stale');
  notice.setAttribute('role', 'alert');
  notice.innerHTML = '<span>agenthub 已更新。当前页面已停止发送，请重新加载。</span>';
  const reload = el('button', 'btn', '重新加载');
  reload.type = 'button';
  reload.title = serverBuild ? `服务器版本 ${serverBuild}` : '加载新版本';
  reload.onclick = () => location.reload();
  notice.appendChild(reload);
  document.body.appendChild(notice);
  const send = $('#csend');
  if (send) send.disabled = true;
}

async function checkServerBuild() {
  try {
    const response = await fetch(appUrl('api/meta'), {cache: 'no-store'});
    const data = await response.json();
    if (data.build && BUILD_ID && data.build !== BUILD_ID) markStaleBuild(data.build);
  } catch { /* 网络恢复后再检查 */ }
}
setInterval(checkServerBuild, 30000);
queueMicrotask(checkServerBuild);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkServerBuild();
});

// Android 默认只缩小 visual viewport；iOS 也不会让 100dvh 可靠地避开软键盘。
// 把应用高度钉到真正可见区域，并在 Safari 产生 viewport 偏移时跟着移动。
let viewportFrame = 0;
function syncMobileViewport() {
  cancelAnimationFrame(viewportFrame);
  viewportFrame = requestAnimationFrame(() => {
    const root = document.documentElement.style;
    if (!MOBILE.matches) {
      root.removeProperty('--visual-viewport-height');
      root.removeProperty('--visual-viewport-top');
      return;
    }
    const viewport = window.visualViewport;
    const height = Math.max(1, Math.round(viewport?.height || window.innerHeight));
    const top = Math.max(0, Math.round(viewport?.offsetTop || 0));
    root.setProperty('--visual-viewport-height', `${height}px`);
    root.setProperty('--visual-viewport-top', `${top}px`);
    if (typeof layoutTermPane === 'function') {
      layoutTermPane();
      fitTerm();
    }
  });
}
window.visualViewport?.addEventListener('resize', syncMobileViewport);
window.visualViewport?.addEventListener('scroll', syncMobileViewport);
window.addEventListener('resize', syncMobileViewport);
syncMobileViewport();

function showMobileDetail() {
  if (MOBILE.matches) {
    document.body.classList.add('mobile-detail');
    store.set('mobilePage', 'detail');
  }
}

function showMobileList() {
  if (typeof T !== 'undefined' && !$('#termpane').classList.contains('hidden')) closeTermPane(true);
  document.body.classList.remove('mobile-detail');
  if (MOBILE.matches) store.set('mobilePage', 'list');
}

function fmtSize(n) {
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(0) + 'K';
  return (n / 1048576).toFixed(1) + 'M';
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  const p = n => String(n).padStart(2, '0');
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
  const y = new Date(now - 86400000);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}-${p(d.getDate())} ${hm}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
/** 起止时间：同一天只写一次日期前缀；没有结束时间就是还在跑。 */
function fmtSpan(start, end) {
  const from = fmtTime(start);
  if (!end) return from ? `${from} → 运行中` : '运行中';
  const to = fmtTime(end);
  if (!from) return to;   // 旧节点的列表项还没有开始时间
  const split = s => { const i = s.lastIndexOf(' '); return i < 0 ? [s, ''] : [s.slice(0, i), s.slice(i + 1)]; };
  const [fromDay, fromClock] = split(from), [toDay, toClock] = split(to);
  return fromClock && toClock && fromDay === toDay ? `${from} → ${toClock}` : `${from} → ${to}`;
}
/** 家目录缩成 ~; 过长的路径中间省略, 首尾都是有信息量的部分。
 *  不能用 CSS direction:rtl 来截左边 —— bidi 会把开头的 "/" 挪到末尾。 */
function shortCwd(p, max = 40) {
  p = (p || '(未知)').replace(/^\/home\/[^/]+/, '~');
  if (p.length <= max) return p;
  const seg = p.split('/');
  return seg.length > 3 ? `${seg[0]}/${seg[1]}/…/${seg.slice(-2).join('/')}` : p;
}

const timelinePath = cwd => (cwd || '(未知)').replace(/^\/home\/[^/]+/, '~').replace(/\/+$/, '') || '/';
const TIMELINE_COLORS = ['blue', 'teal', 'violet', 'amber', 'rose', 'olive', 'rust', 'cyan'];
let timelineColors = new Map();
try {
  for (const [path, color] of store.get('timelineDirectoryColors', [])) {
    if (typeof path === 'string' && TIMELINE_COLORS.includes(color)
        && ![...timelineColors.values()].includes(color)) timelineColors.set(path, color);
  }
} catch { /* Ignore invalid saved assignments. */ }

function timelineDirectoryColors(rows) {
  const counts = new Map(), seen = new Set();
  for (const row of rows) {
    if (!row.cwd || row.cwd === '(未知)' || seen.has(row.uid)) continue;
    seen.add(row.uid); // Search results also contain sessions in the main list.
    const path = timelinePath(row.cwd);
    counts.set(path, (counts.get(path) || 0) + 1);
  }
  const common = [...counts].filter(([, count]) => count >= 2)
    .sort(([a, ac], [b, bc]) => bc - ac || (a < b ? -1 : a > b ? 1 : 0))
    .slice(0, TIMELINE_COLORS.length).map(([path]) => path);
  const assigned = new Map(common.filter(path => timelineColors.has(path))
    .map(path => [path, timelineColors.get(path)]));
  const free = TIMELINE_COLORS.filter(color => ![...assigned.values()].includes(color));
  for (const path of common) {
    if (!assigned.has(path)) assigned.set(path, free.shift());
  }
  // Preserve slots when session counts reorder the common directories. Filters
  // use the full pool, and reloads reuse the assignment instead of recoloring it.
  if (assigned.size !== timelineColors.size
      || [...assigned].some(([path, color]) => timelineColors.get(path) !== color)) {
    timelineColors = assigned;
    store.set('timelineDirectoryColors', [...assigned]);
  }
  return timelineColors;
}

/** Count directories, not sessions: a busy project must not change which parts
 *  identify a path. Keep the full pool even while filtering the sidebar. */
function timelinePathPlans(rows) {
  const paths = [...new Set(rows.map(s => timelinePath(s.cwd)))];
  const frequency = new Map(), peers = new Map();
  for (const path of paths) {
    const parts = path.split('/'), leaf = parts.at(-1);
    for (const part of new Set(parts)) frequency.set(part, (frequency.get(part) || 0) + 1);
    if (!peers.has(leaf)) peers.set(leaf, []);
    peers.get(leaf).push(path);
  }
  return new Map(paths.map(path => {
    const parts = path === '/' ? ['/'] : path.split('/');
    const leaf = parts.at(-1), hidden = new Set(), labels = [path];
    // Repeated ancestors carry less information. Ties favor keeping the deeper
    // context. Never remove the root anchor or any part of the final directory.
    const order = parts.map((_, i) => i).slice(1, -1).sort((a, b) =>
      frequency.get(parts[b]) - frequency.get(parts[a]) || a - b);
    for (const i of order) {
      hidden.add(i);
      const tokens = parts.flatMap((part, j) => hidden.has(j)
        ? (hidden.has(j - 1) ? [] : [null]) : [part]);
      // An ellipsis represents whole directories. If it could also stand for
      // another path with the same basename, retain the distinguishing ancestor.
      const pattern = new RegExp('^' + tokens.map(token => token === null
        ? '(?:[^/]+/)*[^/]+' : token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('/') + '$');
      if ((peers.get(leaf) || []).some(other => other !== path && pattern.test(other))) {
        hidden.delete(i);
        continue;
      }
      const label = tokens.map(token => token === null ? '…' : token).join('/');
      labels.push(label);
    }
    return [path, {leaf, labels}];
  }));
}

function timelinePathMarkup(path, leaf) {
  return esc(path.slice(0, path.length - leaf.length))
    + `<span class="cwd-leaf">${esc(leaf)}</span>`;
}

function timelineDirectoryMarkup(row) {
  const path = timelinePath(row.cwd), leaf = path === '/' ? '/' : path.split('/').at(-1);
  return (row.node_name ? `<span class="cwd-machine">${nodeBadge(row.node_name)}</span>` : '')
    + `<span class="cwd-path" data-path="${esc(path)}">${timelinePathMarkup(path, leaf)}</span>`;
}

function fitTimelineDirectories() {
  if (S.view !== 'date') return;
  const elements = [...document.querySelectorAll('#side .cwd-path')];
  if (!elements.length) return;
  const rows = [...S.sessions, ...pendingTmuxSessions(), ...(S.results || [])];
  const plans = timelinePathPlans(rows), colors = timelineDirectoryColors(rows);
  const measure = document.createElement('canvas').getContext('2d');
  const font = getComputedStyle(elements[0]);
  measure.font = `${font.fontWeight} ${font.fontSize} ${font.fontFamily}`;
  const widths = new Map();
  // Batch layout reads before writes; repeated rows share measured labels.
  const updates = elements.map(element => {
    const plan = plans.get(element.dataset.path);
    if (!plan) return null;
    const color = colors.get(element.dataset.path) || '';
    const width = element.clientWidth;
    if (!width) return {element, color}; // Fit a closed date group when opened.
    const measured = label => {
      if (!widths.has(label)) {
        widths.set(label, measure.measureText(label).width);
      }
      return widths.get(label);
    };
    const label = plan.labels.find(label => measured(label) <= width)
      || plan.labels.reduce((best, label) => measured(label) < measured(best) ? label : best);
    return {element, color, markup: timelinePathMarkup(label, plan.leaf)};
  });
  for (const update of updates) {
    if (!update) continue;
    if (update.element.dataset.cwdColor !== update.color) update.element.dataset.cwdColor = update.color;
    if (update.markup && update.element.innerHTML !== update.markup) update.element.innerHTML = update.markup;
  }
}

let timelineFitFrame = 0;
function scheduleTimelineFit() {
  cancelAnimationFrame(timelineFitFrame);
  timelineFitFrame = requestAnimationFrame(fitTimelineDirectories);
}
new ResizeObserver(scheduleTimelineFit).observe($('#side'));
document.fonts.ready.then(scheduleTimelineFit);

const dayKey = iso => {
  const d = new Date(iso), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

// ---------------------------------------------------------------- 消息缓存
// 一次读完整个会话, 结果按 LRU 留在内存; 会话是 append-only 的,
// 再次打开时只向服务端要新追加的部分。
let cacheLimitMb = Math.max(0, +store.get('cacheMb', 256) || 0);
let CACHE_MAX_BYTES = cacheLimitMb ? cacheLimitMb * 1024 * 1024 : Infinity;
const RENDER_BATCH = 250;
const SYNC_MS = 10000;      // 没在运行的会话, 偶尔看一眼就行
const LIVE_MS = 3000;       // 活跃探测(扫 /proc)的间隔
// 正在看的活跃会话用自适应间隔: 有新内容就贴到最快, 静下来逐步退避
const FAST_MIN = 350;
const FAST_MAX = 3000;
const TICK_MS = 200;
const BACKUP_MS = 20000;    // SSE 正常时的兜底对账间隔
const LIST_MS = 8000;       // 会话列表跟进磁盘变化的间隔
const PENDING_RECONCILE_MS = 5000; // 只在有服务端 pending 时巡检小账本
// 主动增量读取可能因浏览器连接池、网络切换或代理半开而既不成功也不失败。
// 只要响应头或正文仍有进展就续期；真正静止到这个时长才中止，让下一次
// SSE/对账从同一游标重试。用 let 是为了浏览器 E2E 能把分钟级故障压缩到毫秒。
let SYNC_STALL_MS = 12000;
const cache = new Map();          // viewKey → {meta, msgs, version, end, bytes}
// 多题题卡会被 SSE、兜底对账和完整重绘反复替换 DOM。未提交选择必须独立于
// 节点保存，否则下一次后台刷新就会让用户刚点的答案消失。
const questionFormDrafts = new Map(); // `${uid}\0${tool id}` → option index[]
const viewKey = (uid, agent = null) => agent ? `${uid}::${agent}` : uid;
const INCOMING_ROLES = new Set([
  'assistant', 'assistant·subagent', 'thinking', 'tool', 'tool_result', 'question',
]);
const incomingCount = msgs => msgs.filter(m => m.counted !== false && INCOMING_ROLES.has(m.role)).length;
const messageCount = msgs => msgs.filter(m => m.counted !== false).length;
const entryTotal = entry => Number.isFinite(+entry?.total)
  ? +entry.total : messageCount(entry?.msgs || []);

function saveQueuedMessages() {
  for (const [uid, items] of S.queued) {
    if (!Array.isArray(items) || !items.length) S.queued.delete(uid);
  }
  // 极长 prompt 可能超过浏览器的 localStorage 配额；不能让持久化失败反过来阻止发送。
  const localOnly = [...S.queued].flatMap(([uid, items]) => {
    const kept = items.filter(item => !item?.server);
    return kept.length ? [[uid, kept]] : [];
  });
  try { store.set('queuedMessages', localOnly); } catch { /* 本页内存回显仍然有效 */ }
}

function queuedMessages(uid) {
  const items = S.queued.get(uid);
  return Array.isArray(items) ? items : [];
}

function queuedAfterTimestamp(uid) {
  const entry = cache.get(viewKey(uid));
  let latest = Date.parse(entry?.activity?.ts || '');
  for (let i = (entry?.msgs?.length || 0) - 1; i >= 0; i--) {
    const at = Date.parse(entry.msgs[i]?.ts || '');
    if (!Number.isFinite(at)) continue;
    latest = Number.isFinite(latest) ? Math.max(latest, at) : at;
    break;
  }
  return Number.isFinite(latest) ? new Date(latest).toISOString() : null;
}

/** Codex 在忙时只把新输入留在 TUI 内存里，轮到它之前 rollout 没有任何记录。
 *  先持久化并回显；原生 user/command 记录出现后再按正文和时间精确消重。 */
function queuePendingUserMessage(uid, text, media = []) {
  text = String(text || '');
  if (!uid || !text.trim()) return null;
  const cli = agenthubCli(uid);
  if (!cli) return null;
  const created = Date.now();
  const item = cli.createQueuedMessage({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    text, created, afterTs: queuedAfterTimestamp(uid),
    // 附件消息在 CLI 真正写入 rollout 前也要能显示图片。这里只保存服务端
    // 签发的短媒体 token，不把 File/blob URL 或本地绝对路径塞进 localStorage。
    media: Array.isArray(media) ? media.filter(x => x?.src).map(x => ({ ...x })) : [],
  });
  const items = queuedMessages(uid).slice();
  items.push(item);
  S.queued.set(uid, items);
  browserAuditEvent('outbox.local_queued', {id: item.id, state: item.state},
    {text: item.text, media: item.media}, {uid, requestId: item.id});
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
  return item.id;
}

function discardQueuedUserMessage(uid, id) {
  const items = queuedMessages(uid).filter(item => item.id !== id);
  if (items.length) S.queued.set(uid, items); else S.queued.delete(uid);
  browserAuditEvent('outbox.local_discarded', {id, remaining: items.length}, null,
    {uid, requestId: id});
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
}

function validOutboxVersion(version) {
  return version && typeof version.epoch === 'string' && version.epoch
    && Number.isFinite(+version.revision);
}

function staleServerOutbox(uid, version) {
  if (!validOutboxVersion(version)) return false;
  const current = S.outboxVersions.get(uid);
  if (!current) return S.retiredOutboxEpochs.has(version.epoch);
  if (current.epoch === version.epoch) return +version.revision < +current.revision;
  return S.retiredOutboxEpochs.has(version.epoch);
}

function acceptServerOutboxVersion(uid, version) {
  if (!validOutboxVersion(version)) return;
  const current = S.outboxVersions.get(uid);
  if (current?.epoch && current.epoch !== version.epoch) {
    S.retiredOutboxEpochs.add(current.epoch);
  }
  S.outboxVersions.set(uid, {
    epoch: version.epoch,
    revision: +version.revision,
  });
}

function syncServerOutbox(uid, items, version = null, { retireMissing = false } = {}) {
  if (!['claude', 'codex'].includes(agenthubCli(uid)?.source)
      || !Array.isArray(items)) return false;
  if (staleServerOutbox(uid, version)) {
    browserAuditEvent('outbox.snapshot_rejected', {version, reason: 'stale'}, items, {uid});
    return false;
  }
  acceptServerOutboxVersion(uid, version);
  const next = items.map(item => ({ ...item, server: true }));
  const current = queuedMessages(uid);
  if (!retireMissing) {
    const ids = new Set(next.map(item => item?.id).filter(Boolean));
    // 服务端账本消失只证明原生记录已经被服务端看见，不证明这个标签页也
    // 收到了正文 diff。占位必须等匹配的 user/command 被本页接受后，再由
    // reconcileQueuedMessages 原子替换；否则空账本包先到就会让消息消失。
    for (const item of current) {
      if (item?.server && item.id && !ids.has(item.id)) next.push(item);
    }
  }
  next.sort((a, b) => (+a?.created || 0) - (+b?.created || 0)
    || String(a?.id || '').localeCompare(String(b?.id || '')));
  const before = JSON.stringify(current);
  if (next.length) S.queued.set(uid, next); else S.queued.delete(uid);
  const changed = before !== JSON.stringify(next);
  browserAuditEvent('outbox.snapshot_applied', {
    version, changed, retire_missing: retireMissing, before: current.length, after: next.length,
  }, next, {uid});
  if (changed && S.sel === uid && !S.agent) {
    renderConversationTail(cache.get(viewKey(uid))?.activity, uid);
  }
  return changed;
}

function confirmTerminalDraftOverwrite() {
  return confirm('终端草稿中有内容，是否覆盖？');
}

async function retryServerQueuedMessage(uid, id) {
  const activity = cache.get(viewKey(uid))?.activity || null;
  let overwriteDraft = '';
  let d = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    d = await post('api/session/outbox/retry', {
      uid, id, activity, overwrite_draft: overwriteDraft,
    });
    if (!d.draft_conflict) break;
    if (!confirmTerminalDraftOverwrite()) {
      await discardServerQueuedMessage(uid, id);
      return;
    }
    overwriteDraft = d.draft_token || '';
  }
  if (d?.draft_conflict) return alert('终端草稿持续变化，消息未发送');
  if (d.error) return alert('重试失败: ' + d.error);
  syncServerOutbox(uid, d.outbox || [], d.outbox_version);
}

async function discardServerQueuedMessage(uid, id) {
  const d = await post('api/session/outbox/discard', { uid, id });
  if (d.error) return alert('移除失败: ' + d.error);
  // 用户明确点了撤销/移除，不需要等待一条永远不会出现的原生正文。
  syncServerOutbox(uid, d.outbox || [], d.outbox_version, {retireMissing: true});
}

function updateClientQueuedMessage(uid, id, update) {
  const items = queuedMessages(uid).slice();
  const at = items.findIndex(item => !item.server && item.id === id);
  if (at < 0) return null;
  items[at] = { ...items[at], ...update };
  S.queued.set(uid, items);
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
  return items[at];
}

async function retryClientQueuedMessage(uid, id) {
  const item = queuedMessages(uid).find(x => !x.server && x.id === id);
  if (!item) return;
  const name = takenOver(uid);
  if (!name) return alert('重试失败: 会话终端未连接');
  updateClientQueuedMessage(uid, id, {
    state: 'sending', expiresAt: Date.now() + 8000, error: null,
  });
  let d;
  try {
    d = await post('api/term/send', { name, text: item.text });
  } catch (error) {
    d = { error: error.message || String(error) };
  }
  if (d.error) {
    updateClientQueuedMessage(uid, id, {
      state: 'failed', error: d.error, expiresAt: null,
    });
    return alert('重试失败: ' + d.error);
  }
  S.live.add(uid);
  S.liveTmux.add(uid);
  paintLive();
  S.syncGap = FAST_MIN;
  S.lastSync = 0;
}

function reconcileQueuedMessages(uid, messages) {
  const items = queuedMessages(uid).slice();
  if (!items.length) return false;
  const cli = agenthubCli(uid);
  if (!cli) return false;
  let changed = false;
  for (const message of messages || []) {
    const action = cli.queueAction(message);
    if (!action) continue;
    const recorded = Date.parse(message.ts || '');
    const followsBoundary = item => {
      const boundary = Date.parse(item.afterTs || '');
      return !Number.isFinite(recorded) || !Number.isFinite(boundary)
        || recorded > boundary;
    };
    if (action.type === 'promote-first') {
      // 完整缓存兜底会重放历史 dequeue。只能提升该事件发生前已经存在的
      // 排队项，不能让几小时前的 dequeue 改写刚刚确认的新消息。
      const at = items.findIndex(item => item.state === 'queued'
        && followsBoundary(item));
      if (at >= 0) {
        items[at] = { ...items[at], state: 'sending', expiresAt: Date.now() + 8000 };
        changed = true;
      }
      continue;
    }
    if (action.type === 'promote-all') {
      for (let i = 0; i < items.length; i++) {
        if (items[i].state !== 'queued' || !followsBoundary(items[i])) continue;
        items[i] = { ...items[i], state: 'sending', expiresAt: Date.now() + 8000 };
        changed = true;
      }
      continue;
    }
    const at = items.findIndex(item => {
      if (!cli.queuedTextMatches(item.text, action.text)) return false;
      // 同文指令可能连续排队；enqueue 应依次确认尚未确认的副本，
      // 不能反复命中第一条已确认项。
      if (action.type === 'confirm' && item.state === 'queued') return false;
      // 不比较浏览器 Date.now() 和 CLI 时间：手机/电脑时钟偏差、CLI 排队延迟
      // 都可能超过数秒。发送时的最后原生消息才是可靠的因果边界。
      // 必须严格晚于边界；否则用户连续发送两条完全相同的内容时，上一条
      // 正式消息会误删下一条尚未落盘的乐观副本。
      // 旧版遗留项没有 afterTs，首次完整对账时按同文迁移清理。
      return followsBoundary(item);
    });
    if (at < 0) continue;
    if (action.type === 'confirm') {
      items[at] = { ...items[at], state: 'queued' };
      delete items[at].expiresAt;
      delete items[at].error;
    } else {
      items.splice(at, 1);
    }
    changed = true;
  }
  if (!changed) return false;
  if (items.length) S.queued.set(uid, items); else S.queued.delete(uid);
  saveQueuedMessages();
  return true;
}

/** Claude 的 Esc 编辑会把原输入留在 JSONL 的旧分支，再从同一父节点提交
 *  新输入。服务端已经确认旧输入后 outbox 会消失；若当前活动时间线出现了
 *  因果更晚的另一条 user/command，它不是“仍待确认”，而是已被新分支取代。 */
function retireSupersededClaudeMessages(uid, ids, messages) {
  if (agenthubCli(uid)?.source !== 'claude' || !ids?.size) return false;
  const laterInputs = (messages || []).filter(message =>
    ['user', 'command'].includes(message?.role)
    && Number.isFinite(Date.parse(message.ts || '')));
  if (!laterInputs.length) return false;
  const current = queuedMessages(uid);
  const next = current.filter(item => {
    if (!item?.server || !ids.has(item.id)) return true;
    const submitted = +item.created;
    if (!Number.isFinite(submitted)) return true;
    return !laterInputs.some(message => Date.parse(message.ts) > submitted);
  });
  if (next.length === current.length) return false;
  if (next.length) S.queued.set(uid, next); else S.queued.delete(uid);
  saveQueuedMessages();
  return true;
}

/** 超时未获 CLI 原生回执时保留消息，并明确标成“发送未确认”。 */
function expireQueuedMessages(now = Date.now()) {
  let changed = false;
  let selectedChanged = false;
  for (const [uid, current] of S.queued) {
    const cli = agenthubCli(uid);
    if (!cli || !Array.isArray(current)) continue;
    const hasNativeHistory = cache.has(viewKey(uid));
    const settled = current.map(item => cli.settleQueuedMessage(
      item, now, hasNativeHistory));
    if (settled.every((item, i) => item === current[i])) continue;
    changed = true;
    selectedChanged ||= uid === S.sel;
    S.queued.set(uid, settled);
  }
  if (!changed) return false;
  saveQueuedMessages();
  if (selectedChanged && !S.agent) {
    renderConversationTail(cache.get(viewKey(S.sel))?.activity, S.sel);
  }
  return true;
}

function migrateQueuedMessages(fromUid, toUid) {
  if (!fromUid || !toUid || fromUid === toUid) return;
  const moved = queuedMessages(fromUid);
  if (!moved.length) return;
  S.queued.set(toUid, [...queuedMessages(toUid), ...moved]);
  S.queued.delete(fromUid);
  saveQueuedMessages();
}

function cacheGet(uid) {
  const e = cache.get(uid);
  if (e) { cache.delete(uid); cache.set(uid, e); }   // 命中即移到队尾
  return e;
}

function cacheEntryUid(key, entry) {
  // 子代理视图的 key 是 uid::agent，但它和主会话共用同一个 tmux 生命周期。
  return entry?.meta?.uid || String(key).split('::', 1)[0];
}

function cacheEntryPinned(key, entry) {
  const uid = cacheEntryUid(key, entry);
  // 详情 DOM 直接由当前缓存生成。若容量整理把正在看的这一份删掉，页面仍
  // 看似正常，却再也没有游标可供 SSE/outbox 补读，最终会留下永久 pending。
  return key === viewKey(S.sel, S.agent)
    || S.liveTmux.has(uid)
    || (typeof T !== 'undefined' && T.list?.some(x => x.uid === uid));
}

function trimCache() {
  // tmux 对话必须随时切回即见，因此不计入容量、也不参与淘汰。
  // 普通历史对话单独共享用户设置的容量，并延续“至少保留最新一份”的旧行为。
  const evictable = [...cache].filter(([key, entry]) => !cacheEntryPinned(key, entry));
  let total = evictable.reduce((n, [, entry]) => n + (+entry.bytes || 0), 0);
  let remaining = evictable.length;
  for (const [key, entry] of evictable) {
    if (total <= CACHE_MAX_BYTES || remaining <= 1) break;
    cache.delete(key);
    total -= +entry.bytes || 0;
    remaining--;
  }
}

function cachePut(uid, e) {
  cache.delete(uid);
  cache.set(uid, e);
  trimCache();
}

/** 带下载进度的取消息。start/head 给定时服务端只回新增部分。 */
async function fetchMessages(uid, opts = {}) {
  const p = new URLSearchParams();
  if (opts.start) {
    p.set('start', opts.start);
    p.set('head', opts.head);
    p.set('anchor', opts.anchor || '');     // 没有锚点服务端会拒绝续读, 直接给整份
  }
  if (opts.agent) p.set('agent', opts.agent);
  if (opts.appendOnly) p.set('append', '1');
  if (opts.windowed) p.set('window', '1');
  const traceId = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const url = `api/messages/${encodeURIComponent(uid)}?${p}`;
  const started = performance.now();
  browserAuditEvent('http.request.started', {
    url, method: 'GET', start: opts.start || 0, agent: opts.agent || '',
  }, null, {uid, traceId});
  let r;
  try {
    r = await fetch(appUrl(url), {
      signal: opts.signal,
      headers: {'X-AgentHub-Trace': traceId, 'X-AgentHub-Page': AUDIT_PAGE_ID,
        'X-AgentHub-Build': BUILD_ID},
    });
  } catch (error) {
    browserAuditEvent('http.request.failed', {
      url, error: String(error?.name || error),
      duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
    }, null, {uid, traceId, severity: error?.name === 'AbortError' ? 'warning' : 'error'});
    throw error;
  }
  opts.onActivity?.();
  if (!r.ok) {
    browserAuditEvent('http.response.received', {url, status: r.status, ok: false},
      null, {uid, traceId, severity: 'warning'});
    throw new Error('HTTP ' + r.status);
  }
  // Fetch 自动解压 gzip：reader.read() 统计的是解压后字节，而标准
  // Content-Length 仍可能是压缩后大小。优先用服务端给出的同口径长度；
  // 连到旧服务端时，压缩响应改显示不定进度，也不伪造一个较小的分母。
  const contentTotal = +r.headers.get('Content-Length') || 0;
  const decodedTotal = +r.headers.get('X-AgentHub-Decoded-Length') || 0;
  const encoded = !!r.headers.get('Content-Encoding');
  const total = decodedTotal || (encoded ? 0 : contentTotal);
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    opts.onActivity?.();
    if (encoded && contentTotal && decodedTotal) {
      // Fetch 不暴露实时压缩字节数。用解压进度映射到已知的
      // 压缩总量：中途值明确标为估算，最后一帧则精确等于响应体流量。
      const transferred = Math.min(contentTotal,
        Math.round(got / decodedTotal * contentTotal));
      opts.onProgress?.(transferred, contentTotal, {
        compressed: true, estimated: got < decodedTotal,
      });
    } else {
      opts.onProgress?.(got, total);
    }
  }
  const buf = new Uint8Array(got);
  let at = 0;
  for (const c of chunks) { buf.set(c, at); at += c.length; }
  const data = JSON.parse(new TextDecoder().decode(buf));
  browserAuditEvent('http.response.parsed', {
    url, status: r.status, bytes: got, reset: !!data.reset,
    start: data.start, end: data.end, messages: data.messages?.length || 0,
    duration_ms: Math.round((performance.now() - started) * 1000) / 1000,
  }, null, {uid, traceId});
  return { data, bytes: got,
    networkBytes: encoded && contentTotal ? contentTotal : got };
}

// ---- 进度条 ----
function progress(done, total, label, detail = {}) {
  const bar = $('#prog');
  bar.classList.add('on');
  const pct = total ? Math.min(100, done / total * 100) : 0;
  bar.querySelector('.bar').style.width = (total ? pct : 12) + '%';
  bar.querySelector('.bar').classList.toggle('idle', !total);
  const estimate = detail.estimated ? '≈' : '';
  const compressed = detail.compressed ? '（压缩）' : '';
  bar.querySelector('.txt').textContent = total
    ? `${label} ${label === '渲染' ? `${done}/${total}`
      : estimate + fmtSize(done) + ' / ' + fmtSize(total) + compressed}`
    : `${label}…`;
}

function progressDone() {
  const bar = $('#prog');
  bar.classList.remove('on');
  bar.querySelector('.bar').style.width = '0%';
}

/** 把服务端给的一份 diff 应用到缓存和界面上。
 *  两种情况: 追加(接到末尾) 或 reset(整份重来) —— 和服务端的判定一一对应。 */
function normalizedQuestionAnswer(text) {
  const raw = String(text || '');
  if (/^aborted by user(?:\s|$)/i.test(raw.trim())
      || raw.startsWith("The user doesn't want to proceed with this tool use.")) {
    return '已取消回答';
  }
  try {
    const answers = JSON.parse(raw)?.answers;
    if (!answers || typeof answers !== 'object') return raw;
    const rows = Object.values(answers).map(answer => {
      const values = answer && typeof answer === 'object' ? answer.answers : answer;
      return Array.isArray(values) ? values.join('、') : String(values || '').trim();
    }).filter(Boolean);
    return rows.join('\n') || raw;
  } catch { return raw; }
}

/** 正文游标可能已经由并行 fetch 推进，但后到的 SSE 仍可能携带更新的活动态。
 *  活动态有自己的时间线：接收较新的状态，绝不让旧 Working 覆盖已中断。 */
function activityFollows(current, incoming) {
  if (!current) return true;
  if (!incoming) return ['working', 'waiting'].includes(current.state);
  const currentAt = Date.parse(current.ts || '');
  const incomingAt = Date.parse(incoming.ts || '');
  if (Number.isFinite(currentAt) && Number.isFinite(incomingAt)) {
    return incomingAt >= currentAt;
  }
  const currentBusy = ['working', 'waiting'].includes(current.state);
  const incomingBusy = ['working', 'waiting'].includes(incoming.state);
  if (currentBusy !== incomingBusy) return currentBusy && !incomingBusy;
  return true;
}

function applyCoveredActivity(uid, agent, entry, data) {
  if (!data.activity_changed || !activityFollows(entry.activity, data.activity)) return;
  entry.activity = data.activity;
  markInterruptedTurn(entry.msgs, entry.activity);
  if (S.sel !== uid || S.agent !== agent) return;
  const box = $('#msgs');
  $('#activity')?.remove();
  if (box && entry.activity?.state !== 'working') sealTurnTail(box, entry);
  renderConversationTail(entry.activity, uid);
}

async function applyDiff(uid, data, bytes = 0, agent = null) {
  const key = viewKey(uid, agent);
  const e = cache.get(key);
  if (!e) return 0;
  if (data.prompt_only) {
    if (!agent && Object.prototype.hasOwnProperty.call(data, 'prompt')) {
      e.prompt = data.prompt || null;
      globalThis.revealConversationForPrompt?.(uid, e.prompt);
    }
    if (S.sel === uid && !S.agent) renderConversationTail(e.activity, uid);
    return 0;
  }
  if (data.outbox_only) {
    if (!agent && Array.isArray(data.outbox)) {
      if (staleServerOutbox(uid, data.outbox_version)) return 0;
      const ids = new Set(data.outbox.map(item => item?.id).filter(Boolean));
      // 后台确认线程可能先删掉服务端终端回执，稍后 watch 才读到对应的
      // rollout user 记录。此时直接照空 outbox 清 UI，会让刚发的消息
      // 短暂消失。先从当前浏览器游标补一次正文，再原子完成替换。
      const removesPending = queuedMessages(uid).some(
        item => item.server && item.id && !ids.has(item.id));
      syncServerOutbox(uid, data.outbox, data.outbox_version);
      if (removesPending) scheduleDiffRecovery(uid, agent);
    }
    return 0;
  }
  // 正文 diff 受游标约束。SSE 与兜底拉取可能同时从同一旧游标出发；
  // 乱序包必须在修改 outbox、prompt 或乐观消息之前丢弃，否则正文没被
  // 接收，发送占位却已先清掉。
  if (!data.reset && data.start !== e.end) {
    const packetStart = Number(data.start), packetEnd = Number(data.end);
    const currentEnd = Number(e.end);
    if (Number.isFinite(packetStart) && Number.isFinite(packetEnd)
        && Number.isFinite(currentEnd)
        && packetStart < currentEnd && packetEnd <= currentEnd) {
      // SSE 与主动 fetch 从同一旧游标出发时，后到者可能是一份已被前者
      // 完整覆盖的重复正文。活动态使用独立修订，仍须接收其中较新的
      // aborted/failed；否则页面要等兜底拉取才会清掉 Working。
      applyCoveredActivity(uid, agent, e, data);
      return 0;
    }
    scheduleDiffRecovery(uid, agent);
    return 0;
  }
  if (!agent && Object.prototype.hasOwnProperty.call(data, 'prompt')) {
    e.prompt = data.prompt || null;
    globalThis.revealConversationForPrompt?.(uid, e.prompt);
  }
  let missingOutboxIds = null;
  if (!agent && Array.isArray(data.outbox)) {
    const ids = new Set(data.outbox.map(item => item?.id).filter(Boolean));
    missingOutboxIds = new Set(queuedMessages(uid)
      .filter(item => item.server && item.id && !ids.has(item.id))
      .map(item => item.id));
    syncServerOutbox(uid, data.outbox, data.outbox_version);
  }
  if (!agent) reconcileQueuedMessages(uid, data.messages);
  if (missingOutboxIds?.size) {
    // reset 时旧缓存属于被回退的分支，不能拿来判断；普通追加则当前缓存和
    // 本批共同构成活动时间线。只有更晚的用户输入才能证明旧项已被取代。
    const activeMessages = data.reset ? data.messages : [...e.msgs, ...data.messages];
    retireSupersededClaudeMessages(uid, missingOutboxIds, activeMessages);
  }
  if (missingOutboxIds?.size && queuedMessages(uid).some(
      item => missingOutboxIds.has(item.id))) {
    // 本批没有带来匹配正文；主动补读，但补读期间仍保留占位。
    scheduleDiffRecovery(uid, agent);
  }
  const questionCalls = new Set([...e.msgs, ...(data.messages || [])]
    .filter(m => m.role === 'question' && m.call_id).map(m => m.call_id));
  data.messages = (data.messages || []).map(m => {
    if (m.role !== 'tool_result' || !questionCalls.has(m.call_id)) return m;
    return { ...m, role: 'answer', name: m.name || 'AskUserQuestion',
      text: normalizedQuestionAnswer(m.text) };
  });
  if (data.reset) {                         // 回滚 / 重写过, 缓存作废
    markInterruptedTurn(data.messages, data.activity);
    cachePut(key, { meta: data.meta, msgs: data.messages, version: data.version,
                    end: data.end, anchor: data.anchor, activity: data.activity, bytes,
                    prompt: data.prompt || null,
                    total: data.message_total, partial: data.partial || null });
    S.cursors.set(key, { end: data.end, head: data.version.head, anchor: data.anchor });
    if (S.sel === uid && S.agent === agent) {
      await renderSession(data.meta, data.messages, data.activity);
    }
    return data.messages.length;
  }
  e.version = data.version;
  e.end = data.end;
  e.anchor = data.anchor;
  S.cursors.set(key, { end: data.end, head: data.version.head, anchor: data.anchor });
  e.bytes += bytes;
  if (data.activity_changed) {
    e.activity = data.activity;
    // turn_aborted 可能单独成为一个无正文的 SSE 包，也可能与末批正文一起
    // 到达；两边都补标，不能依赖恰好落在同一次 JSONL 增量读取中。
    markInterruptedTurn(e.msgs, e.activity);
    markInterruptedTurn(data.messages, e.activity);
  }
  else if (e.activity?.state === 'waiting' && data.messages.some(
      m => m.role === 'tool_result' || m.role === 'answer')) {
    // 问题和回答可能分属两次增量读取，第二次已没有 call_id 映射。
    const answerAt = data.messages.findIndex(m => m.role === 'tool_result');
    data.messages = data.messages.map((m, i) => i === answerAt && m.role === 'tool_result'
      ? { ...m, role: 'answer' } : m);
    e.activity = { role: 'status', state: 'working', text: 'working', ts: new Date().toISOString() };
  }
  if (!data.messages.length) {
    if (S.sel === uid && S.agent === agent) {
      const box = $('#msgs');
      $('#activity')?.remove();
      box?.querySelectorAll('.client-outbox').forEach(node => node.remove());
      if (box && e.activity?.state !== 'working') sealTurnTail(box, e);
      renderConversationTail(e.activity, uid);
    }
    return 0;
  }
  e.total = entryTotal(e) + messageCount(data.messages);
  e.msgs = e.msgs.concat(data.messages);
  const incoming = incomingCount(data.messages);
  const detailVisible = S.sel === uid && S.agent === agent
    && (!MOBILE.matches || document.body.classList.contains('mobile-detail'));
  if (incoming && !detailVisible) addUnread(uid, incoming);
  if (S.sel !== uid || S.agent !== agent) return data.messages.length;
  const box = $('#msgs');
  if (!box) return data.messages.length;
  $('#activity')?.remove();
  box.querySelectorAll('.client-outbox').forEach(node => node.remove());
  const built = appendMessages(box, data.messages, null,
    {openTail: e.activity?.state === 'working'});
  const sealed = sealTurnTail(box, e);
  if (!sealed) {
    built.forEach(markMatches);
    if (S.term) updateMatchNav();
  }
  renderConversationTail(e.activity, uid);
  const c = $('#mcount-total');
  const total = entryTotal(e);
  if (c) c.textContent = `${total} 条消息`;
  const mc = $('.mobile-msg-count');
  if (mc) {
    mc.textContent = total;
    mc.setAttribute('aria-label', `${total} 条消息`);
  }
  return data.messages.length;
}

/** 兜底用的主动拉取。正常情况下更新由服务端 SSE 推过来, 这里只在
 *  连接还没建起来或断了的时候补一手。 */
const syncingViews = new Map();

function syncSession(uid, agent = S.agent) {
  const key = viewKey(uid, agent);
  const e = cache.get(key);
  if (!e) return Promise.resolve(0);
  const current = syncingViews.get(key);
  if (current) return current;
  const task = (async () => {
    const ac = new AbortController();
    let stallTimer = null;
    const keepAlive = () => {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(() => ac.abort(), SYNC_STALL_MS);
    };
    try {
      keepAlive();
      const { data, bytes } = await fetchMessages(uid, {
        agent, start: e.end, head: e.version.head, anchor: e.anchor,
        signal: ac.signal, onActivity: keepAlive });
      return await applyDiff(uid, data, bytes, agent);
    } catch {
      return 0;
    } finally {
      clearTimeout(stallTimer);
    }
  })().finally(() => {
    if (syncingViews.get(key) === task) syncingViews.delete(key);
  });
  syncingViews.set(key, task);
  return task;
}

// SSE 负责低延迟更新，但标签页休眠、网络切换和浏览会话切换都可能让某次
// 账本/正文更新错过。只要本页仍有服务端 pending，就定期读取很小的 outbox
// 快照；若服务端项已消失，再用缓存和一次增量正文读取判断它是正式落盘、
// Claude 分支取代，还是仍应保留为“未确认”。
const pendingReconciliations = new Map();
const pendingWindowRecoveries = new Map();
const recoveredPendingWindows = new Map();

function reconcilePendingSnapshot(uid, data) {
  if (!data || !Array.isArray(data.outbox)) return false;
  const ids = new Set(data.outbox.map(item => item?.id).filter(Boolean));
  const missing = new Set(queuedMessages(uid)
    .filter(item => item?.server && item.id && !ids.has(item.id))
    .map(item => item.id));
  let changed = syncServerOutbox(uid, data.outbox, data.outbox_version);
  const entry = cache.get(viewKey(uid));
  if (entry?.msgs?.length) {
    changed = reconcileQueuedMessages(uid, entry.msgs) || changed;
    changed = retireSupersededClaudeMessages(uid, missing, entry.msgs) || changed;
  }
  if (changed && S.sel === uid && !S.agent) {
    renderConversationTail(entry?.activity || null, uid);
  }
  return changed;
}

/**
 * 服务端已经从 outbox 移除一项，说明原生记录曾被确认；但浏览器可能在
 * SSE/主动补读竞争或移动端休眠期间把游标推进到了正文之后，同时漏掉正文。
 * 从当前 EOF 继续补读永远找不回来，因此只对同一组缺失 id 做一次有界窗口
 * 重载（最早 100 + 最新 500），而不是反复下载完整长会话。
 */
function recoverPendingWindow(uid, ids) {
  const key = viewKey(uid);
  const fingerprint = [...ids].sort().join('\0');
  if (!fingerprint || recoveredPendingWindows.get(uid) === fingerprint) {
    return Promise.resolve(false);
  }
  const current = pendingWindowRecoveries.get(uid);
  if (current) return current;
  const task = (async () => {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), SYNC_STALL_MS);
    try {
      const {data, bytes} = await fetchMessages(uid, {
        windowed: true, signal: ac.signal,
      });
      if (!data?.reset) return false;
      recoveredPendingWindows.set(uid, fingerprint);
      if (Array.isArray(data.outbox)) {
        syncServerOutbox(uid, data.outbox, data.outbox_version);
      }
      reconcileQueuedMessages(uid, data.messages);
      retireSupersededClaudeMessages(uid, ids, data.messages);
      cachePut(key, {
        meta: data.meta, msgs: data.messages, version: data.version,
        end: data.end, anchor: data.anchor, activity: data.activity, bytes,
        prompt: data.prompt || null, total: data.message_total,
        partial: data.partial || null,
      });
      S.cursors.set(key, {
        end: data.end, head: data.version.head, anchor: data.anchor,
      });
      if (!queuedMessages(uid).some(item => item?.server)) {
        recoveredPendingWindows.delete(uid);
      }
      if (S.sel === uid && !S.agent) {
        await renderSession(data.meta, data.messages, data.activity);
      }
      return true;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  })().finally(() => {
    if (pendingWindowRecoveries.get(uid) === task) {
      pendingWindowRecoveries.delete(uid);
    }
  });
  pendingWindowRecoveries.set(uid, task);
  return task;
}

function reconcilePendingUid(uid) {
  uid = String(uid || '');
  if (!queuedMessages(uid).some(item => item?.server)) return Promise.resolve(false);
  const current = pendingReconciliations.get(uid);
  if (current) return current;
  const task = (async () => {
    try {
      const query = new URLSearchParams({uid});
      const response = await fetch(appUrl(`api/session/outbox?${query}`), {
        cache: 'no-store',
      });
      if (!response.ok) return false;
      const data = await response.json();
      let changed = reconcilePendingSnapshot(uid, data);
      // 账本状态只能说明服务端是否还在追踪。正文决定 pending 是被同文
      // 正式消息取代，还是 Claude Esc 后被新分支取代；缓存存在时补一次
      // 小型增量读取，未打开的会话则等打开时读取，绝不拉几十 MB 全量。
      const serverIds = new Set(data.outbox.map(item => item?.id).filter(Boolean));
      let missing = new Set(queuedMessages(uid)
        .filter(item => item?.server && item.id && !serverIds.has(item.id))
        .map(item => item.id));
      if (missing.size && cache.has(viewKey(uid))) {
        changed = !!(await syncSession(uid, null)) || changed;
        missing = new Set(queuedMessages(uid)
          .filter(item => item?.server && item.id && !serverIds.has(item.id))
          .map(item => item.id));
      }
      // 当前详情的缓存可能被旧版本 LRU 清掉；DOM 还在并不代表仍有可续读
      // 游标。窗口恢复同时覆盖“有缓存但游标已经越过正文”和“详情缓存丢失”。
      if (missing.size && (cache.has(viewKey(uid))
                           || (S.sel === uid && !S.agent))) {
        changed = !!(await recoverPendingWindow(uid, missing)) || changed;
      }
      return changed;
    } catch {
      return false;
    }
  })().finally(() => {
    if (pendingReconciliations.get(uid) === task) pendingReconciliations.delete(uid);
  });
  pendingReconciliations.set(uid, task);
  return task;
}

async function reconcileAllPendingMessages() {
  expireQueuedMessages();
  if (document.hidden) return [];
  const uids = [...S.queued]
    .filter(([, items]) => items?.some(item => item?.server))
    .map(([uid]) => uid);
  return Promise.all(uids.map(reconcilePendingUid));
}

setInterval(reconcileAllPendingMessages, PENDING_RECONCILE_MS);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) reconcileAllPendingMessages();
});

// ---- 服务端推送 ----
// 服务端盯着会话文件, 一变就把 diff 推过来, 不用客户端反复问。
let _es = null, _esUid = null, _esRetry = null;
const diffRecoveries = new Map();

/** 游标冲突或队列先于正文确认时，从当前已接受游标重新取一次并重建 watch。 */
function scheduleDiffRecovery(uid, agent = null) {
  const key = viewKey(uid, agent);
  if (diffRecoveries.has(key)) return;
  const timer = setTimeout(async () => {
    try {
      // 若冲突发生时已有主动拉取在途，先等旧请求收尾，再保证至少发出
      // 一次基于最新本地游标的新请求。
      const inFlight = syncingViews.get(key);
      if (inFlight) await inFlight;
      if (!cache.has(key)) return;
      await syncSession(uid, agent);
      if (S.sel === uid && S.agent === agent) watchSession(uid, agent);
    } finally {
      if (diffRecoveries.get(key) === timer) diffRecoveries.delete(key);
    }
  }, 0);
  diffRecoveries.set(key, timer);
}

function watchSession(uid, agent = S.agent) {
  closeWatch();
  const e = cache.get(viewKey(uid, agent));
  if (!e || !window.EventSource) return;
  // watch 从当前缓存游标开始，建立过程中不需要 tickSync 立刻再发一条相同
  // 增量请求。否则 CONNECTING 尚未变 OPEN 的几百毫秒会产生一次竞争包。
  if (S.sel === uid && S.agent === agent) S.lastSync = Date.now();
  const connectionId = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const p = new URLSearchParams({ uid, start: e.end, head: e.version.head,
    anchor: e.anchor || '', page: AUDIT_PAGE_ID, connection: connectionId });
  if (agent) p.set('agent', agent);
  const es = new EventSource(appUrl('api/watch?' + p));
  _es = es;
  _esUid = uid;
  es.__agenthubConnectionId = connectionId;
  let received = 0;
  browserAuditEvent('sse.connecting', {start: e.end, agent: agent || ''}, null,
    {uid, connectionId});
  es.onopen = () => browserAuditEvent('sse.opened', {ready_state: es.readyState}, null,
    {uid, connectionId});
  es.onmessage = ev => {
    // close() 后浏览器仍可能派发已经排队的旧事件，不能让旧 watch 改新视图。
    if (_es !== es || _esUid !== uid || S.agent !== agent) return;
    let data;
    try { data = JSON.parse(ev.data); }
    catch (error) {
      browserAuditEvent('sse.parse_failed', {error: String(error), bytes: ev.data.length},
        ev.data.slice(0, 4000), {uid, connectionId, severity: 'error'});
      return;
    }
    received++;
    const packetId = data._audit?.packet_id || `${connectionId}:client-${received}`;
    browserAuditEvent('sse.received', {
      packet_id: packetId, kind: data._audit?.kind || '', bytes: ev.data.length,
      reset: !!data.reset, start: data.start, end: data.end,
      messages: data.messages?.length || 0, outbox: data.outbox?.length || 0,
    }, null, {uid, traceId: packetId, connectionId});
    Promise.resolve(applyDiff(uid, data, 0, agent)).then(applied => {
      const snapshot = browserStateSnapshot('sse-applied');
      browserAuditEvent('sse.applied', {
        packet_id: packetId, applied_messages: applied,
        cache_end: cache.get(viewKey(uid, agent))?.end,
      }, snapshot.content, {uid, traceId: packetId, connectionId});
      scheduleBrowserSnapshot('sse-applied');
    }).catch(error => browserAuditEvent('sse.apply_failed', {
      packet_id: packetId, error: String(error?.stack || error),
    }, null, {uid, traceId: packetId, connectionId, severity: 'error'}));
  };
  es.onerror = () => {
    // EventSource 自带的重连会沿用旧 URL(旧偏移), 所以自己关掉重开, 带上新偏移
    es.close();
    browserAuditEvent('sse.error', {ready_state: es.readyState, received}, null,
      {uid, connectionId, severity: 'warning'});
    if (_es !== es) return;
    _es = null;
    clearTimeout(_esRetry);
    _esRetry = setTimeout(() => {
      if (S.sel === uid && S.agent === agent) watchSession(uid, agent);
    }, 1500);
  };
}

function closeWatch() {
  clearTimeout(_esRetry);
  if (_es) {
    browserAuditEvent('sse.closed_by_page', {ready_state: _es.readyState}, null,
      {uid: _esUid, connectionId: _es.__agenthubConnectionId || ''});
    _es.close(); _es = null; _esUid = null;
  }
}

function unreadRow(uid) {
  const value = S.unread.get(uid);
  if (typeof value === 'number') return { count: value, tmux: false };
  return value && typeof value === 'object'
    ? { count: Math.max(0, +value.count || 0), tmux: !!value.tmux }
    : { count: 0, tmux: false };
}

function saveUnread() {
  store.set('unread', [...S.unread].filter(([, row]) => (+row?.count || +row || 0) > 0));
}

function paintItemStatus(node) {
  if (!node) return;
  const badge = node.querySelector(':scope > .ico > .item-status');
  if (!badge) return;
  const row = unreadRow(node.dataset.uid);
  const pending = !!node.dataset.tmuxName;
  const active = pending || S.live.has(node.dataset.uid);
  const tmux = pending || S.liveTmux.has(node.dataset.uid) || (row.count > 0 && row.tmux);
  badge.textContent = row.count > 99 ? '99+' : (row.count || '');
  badge.classList.toggle('visible', active || row.count > 0);
  badge.classList.toggle('counted', row.count > 0);
  badge.classList.toggle('tmux', tmux);
  badge.title = badge.ariaLabel = row.count
    ? `${row.count} 条新内容${tmux ? '，tmux 会话' : ''}`
    : (tmux ? 'tmux 会话运行中' : '会话运行中');
}

function addUnread(uid, count) {
  if (!uid || count <= 0) return;
  const row = unreadRow(uid);
  S.unread.set(uid, { count: row.count + count, tmux: S.liveTmux.has(uid) || row.tmux });
  saveUnread();
  paintItemStatus(document.querySelector(`.item[data-uid="${CSS.escape(uid)}"]`));
}

function clearUnread(uid) {
  if (!uid || !S.unread.has(uid)) return;
  S.unread.delete(uid);
  saveUnread();
  paintItemStatus(document.querySelector(`.item[data-uid="${CSS.escape(uid)}"]`));
}

/** 兜底轮询: SSE 连着的时候只是很慢地对一下账, 断了才回到自适应的快节奏。 */
function tickSync() {
  expireQueuedMessages();
  if (!S.sel || document.hidden) return;
  const pushing = _es && _esUid === S.sel && _es.readyState === 1;
  const gap = pushing ? BACKUP_MS : (S.live.has(S.sel) ? S.syncGap : SYNC_MS);
  if (Date.now() - (S.lastSync || 0) < gap) return;
  S.lastSync = Date.now();
  const uid = S.sel;
  const agent = S.agent;
  syncSession(uid, agent).then(n => {
    if (uid !== S.sel || agent !== S.agent || pushing) return;
    S.syncGap = n ? FAST_MIN : Math.min(FAST_MAX, Math.round(S.syncGap * 1.5));
  });
}

setInterval(tickSync, TICK_MS);

// ---- 活跃会话 ----
async function refreshLive(force = false) {
  const d = await (await fetch(appUrl('api/live' + (force ? '?force=1' : '')))).json();
  applyNodeState(d, 'live');
  const next = new Set(d.uids);
  const nextTmux = new Set((d.tmux_uids || []).filter(u => next.has(u)));
  const nextStarted = new Map(Object.entries(d.started_at || {}).map(([u, t]) => [u, +t]));
  const setChanged = (a, b) => a.size !== b.size || [...a].some(u => !b.has(u));
  const mapChanged = (a, b) => a.size !== b.size
    || [...a].some(([u, t]) => b.get(u) !== t);
  const changed = setChanged(next, S.live) || setChanged(nextTmux, S.liveTmux)
    || mapChanged(nextStarted, S.liveStarted);
  S.live = next;
  S.liveTmux = nextTmux;
  S.liveStarted = nextStarted;
  trimCache();
  if (changed) {
    paintLive();
  }
}

let livePollRequest = null;
function pollLive(force = false) {
  if (document.hidden) return Promise.resolve();
  if (livePollRequest) {
    // Timer ticks share the current cycle; explicit refreshes still get a
    // fresh snapshot after it, and awaiters never observe a half-finished poll.
    return force ? livePollRequest.then(() => pollLive(true)) : livePollRequest;
  }
  livePollRequest = runLivePoll(force).finally(() => { livePollRequest = null; });
  return livePollRequest;
}

async function runLivePoll(force) {
  try {
    await refreshLive(force);
    if (typeof loadTermList === 'function') {   // tmux 会话可能在外部被结束
      const before = (T.list || []).map(x => x.name).join();
      await loadTermList();
      if ((T.list || []).map(x => x.name).join() !== before) {
        await refreshLive(true);                // 绕过 3 秒缓存，绿点立即跟着 tmux 消失
        renderTakeoverBtn();
        if (T.name && !T.list.some(x => x.name === T.name)) closeTermPane();
      }
    }
  } catch { /* 服务端没起来就下轮再说 */ }
}

/** 只改小圆点, 不重渲染整个列表 —— 否则每几秒就会打断滚动和选中。 */
function paintLive() {
  for (const n of document.querySelectorAll('.item.agent')) paintAgentStatus(n);
  for (const n of document.querySelectorAll('.item[data-uid]')) {
    const pendingRunning = n.dataset.tmuxName
      && typeof T !== 'undefined' && T.list?.some(t => t.name === n.dataset.tmuxName && !t.stale);
    n.classList.toggle('live', !!pendingRunning || S.live.has(n.dataset.uid));
    n.classList.toggle('live-tmux', !!pendingRunning || S.liveTmux.has(n.dataset.uid));
    paintItemStatus(n);
  }
  const h = $('#dlive');
  if (h) {
    // 临时会话（还没有 JSONL）按左栏同样的规则算运行中
    const row = document.querySelector(`.item[data-uid="${CSS.escape(S.sel || '')}"]`);
    const live = row ? row.classList.contains('live') : S.live.has(S.sel);
    const tmux = row ? row.classList.contains('live-tmux') : S.liveTmux.has(S.sel);
    h.classList.toggle('visible', live);
    h.classList.toggle('tmux', tmux);
    h.title = h.ariaLabel = tmux ? '运行于 tmux' : '运行中';
  }
  const selected = S.sessions.find(x => x.uid === S.sel);
  if (selected) {
    renderSessionAction(selected);
    renderConversationTail(cache.get(viewKey(selected.uid, S.agent))?.activity, selected.uid);
  }
  renderSessionCounts();
  syncActiveOnlyList();
}

/** 活动筛选开启时，进程启停会改变列表成员；集合没变就不动 DOM。 */
function syncActiveOnlyList() {
  if (!S.activeOnly) return;
  const wanted = new Set(visible().map(s => s.uid));
  const shown = new Set([...document.querySelectorAll('#side .item[data-uid]')].map(n => n.dataset.uid));
  if (wanted.size === shown.size && [...wanted].every(uid => shown.has(uid))) return;
  const side = $('#side'), top = side?.scrollTop || 0;
  renderSide();
  if (side) side.scrollTop = top;
}

function selectSessionScope(activeOnly) {
  S.activeOnly = activeOnly;
  store.set('activeOnly', S.activeOnly);
  renderSide();
  paintLive();
}
$('#livecount').onclick = () => selectSessionScope(true);
$('#allcount').onclick = () => selectSessionScope(false);
$('#session-scope').onkeydown = e => {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
  e.preventDefault();
  const active = e.key === 'Home' ? true : e.key === 'End' ? false : !S.activeOnly;
  selectSessionScope(active);
  $(active ? '#livecount' : '#allcount').focus();
};

setInterval(pollLive, LIVE_MS);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { closeWatch(); return; }
  pollLive();
  if (S.sel && cache.get(viewKey(S.sel, S.agent))) {
    syncSession(S.sel, S.agent).then(() => watchSession(S.sel, S.agent));
  }
});

// ---------------------------------------------------------------- 数据加载
function renderSessionCounts() {
  const pool = sidebarSessions().filter(s => !S.off.has(s.source) && nodeSelected(s));
  const active = pool.filter(s => s.pending || S.live.has(s.uid)).length;
  $('#session-active').textContent = active;
  $('#session-total').textContent = pool.length;
  for (const [id, selected, label] of [
    ['#livecount', S.activeOnly, `${active} 个活跃会话`],
    ['#allcount', !S.activeOnly, `${pool.length} 个总会话`],
  ]) {
    const button = $(id);
    button.classList.toggle('on', selected);
    button.setAttribute('aria-checked', String(selected));
    button.setAttribute('aria-label', label);
    button.tabIndex = selected ? 0 : -1;
  }
}

function showSessionCount() {
  renderSessionCounts();
  $('#stat').textContent = '';
}

const pendingUid = name => `tmux:${name}`;

/** agenthub 自己启动、但还没有对话文件的 tmux，也是一条可重新进入的临时会话。 */
function pendingTmuxSessions() {
  if (typeof T === 'undefined' || !Array.isArray(T.pending)) return [];
  return T.pending.flatMap(t => {
    if (!SOURCES[t.source] || (t.sid && S.sessions.some(s =>
      s.source === t.source && s.node_id === t.node_id && String(s.sid) === String(t.sid)))) return [];
    const source = t.source;
    return [{
      node_id: t.node_id, node_name: t.node_name, stale: t.stale,
      uid: pendingUid(t.name), pending: true, name: t.name, tmuxName: t.name, source,
      title: t.title || `新建 ${SOURCES[source].name} 会话`,
      kind: t.kind || '', report_id: t.report_id || '', cwd: t.cwd || '(未知)',
      created: new Date((t.started || Date.now() / 1000) * 1000).toISOString(),
      updated: new Date((t.started || Date.now() / 1000) * 1000).toISOString(),
      size: 0,
    }];
  });
}

const sessionHidden = session => !!session?.fork_parent && !session.fork_parent_visible;
// 沿 forked_from_id 往上追整条父会话链（近的在前）。只在同来源、同机器内按
// 原生 sid 匹配；记录已不存在的一级保留占位并到此为止。
function forkAncestors(session) {
  const chain = [];
  const seen = new Set();
  let sid = String(session?.forked_from_id || '');
  while (sid && !seen.has(sid)) {
    seen.add(sid);
    const row = S.sessions.find(s => s.source === session.source
      && (s.node_id || '') === (session.node_id || '') && String(s.sid) === sid);
    chain.push({ sid, row: row || null });
    sid = String(row?.forked_from_id || '');
  }
  return chain;
}
const sidebarSessions = () => [...pendingTmuxSessions(), ...S.sessions]
  .filter(s => !sessionHidden(s));

function cursorViews(sessions) {
  const rows = [];
  for (const session of sessions) {
    if (session.cursor) rows.push({uid: session.uid, agent: null, cursor: session.cursor});
    for (const item of session.agent_items || []) {
      if (item.cursor) rows.push({uid: session.uid, agent: item.id, cursor: item.cursor});
    }
  }
  return rows;
}

function cleanCursor(value) {
  return value && Number.isFinite(+value.end) && value.head
    ? {end: +value.end, head: String(value.head), anchor: String(value.anchor || '')}
    : null;
}

function seedSidebarCursors(sessions) {
  for (const row of cursorViews(sessions)) {
    const cursor = cleanCursor(row.cursor);
    if (cursor) S.cursors.set(viewKey(row.uid, row.agent), cursor);
  }
}

const sidebarSyncing = new Set();

async function syncSidebarView(row, base, latest, attempt = 0) {
  const key = viewKey(row.uid, row.agent);
  if (sidebarSyncing.has(key)) return;
  sidebarSyncing.add(key);
  try {
    const {data, bytes} = await fetchMessages(row.uid, {
      agent: row.agent, start: base.end, head: base.head, anchor: base.anchor,
      appendOnly: true,
    });
    if (data.reset) {
      cache.delete(key);                    // 历史被回滚/改写，旧缓存已不可信
      S.cursors.set(key, cleanCursor({end: data.end, head: data.version.head,
                                     anchor: data.anchor}) || latest);
      return;
    }
    const entry = cache.get(key);
    if (entry && entry.end === data.start) {
      await applyDiff(row.uid, data, bytes, row.agent);
    } else {
      const incoming = incomingCount(data.messages);
      if (incoming) addUnread(row.uid, incoming);
      S.cursors.set(key, cleanCursor({end: data.end, head: data.version.head,
                                     anchor: data.anchor}) || latest);
    }
  } catch {
    // 列表签名可能不会再变化；短暂断网后主动补两次，仍不影响其他会话。
    if (attempt < 2) setTimeout(() => syncSidebarView(row, base, latest, attempt + 1), 1500);
  }
  finally { sidebarSyncing.delete(key); }
}

/** 列表发现其他会话文件增长时，只读取追加区间并累加左栏未读数。 */
function syncSidebarUpdates(sessions) {
  for (const row of cursorViews(sessions)) {
    const key = viewKey(row.uid, row.agent);
    const latest = cleanCursor(row.cursor);
    if (!latest) continue;
    const entry = cache.get(key);
    const base = entry
      ? cleanCursor({end: entry.end, head: entry.version?.head, anchor: entry.anchor})
      : cleanCursor(S.cursors.get(key));
    if (!base) { S.cursors.set(key, latest); continue; }
    if (base.end === latest.end && base.head === latest.head && base.anchor === latest.anchor) {
      S.cursors.set(key, latest);
      continue;
    }
    const detailVisible = S.sel === row.uid && S.agent === row.agent
      && (!MOBILE.matches || document.body.classList.contains('mobile-detail'));
    if (detailVisible) {                     // 当前正在看的新增内容直接视为已读
      S.cursors.set(key, latest);
      continue;
    }
    if (latest.end < base.end) {
      cache.delete(key);                      // 明确回滚，不尝试整份后台下载
      S.cursors.set(key, latest);
      continue;
    }
    void syncSidebarView(row, base, latest);
  }
}

function mergeSessionMetaEvent(entry, session) {
  if (entry.meta.agent_id || !session.renamed_at || !session.renamed_to) return false;
  const eventId = `rename:${session.sid}:${session.renamed_at}`;
  if (entry.msgs.some(m => m.event_id === eventId)) return false;
  const event = {
    role: 'command', text: `/rename ${session.renamed_to}`, ts: session.renamed_at,
    counted: false, inferred: true, event_id: eventId,
  };
  const at = entry.msgs.findIndex(m => m.ts && m.ts > event.ts);
  entry.msgs.splice(at < 0 ? entry.msgs.length : at, 0, event);
  return true;
}

/** 列表元数据变更后同步缓存和当前详情标题，不重绘消息正文。 */
function refreshSessionMeta() {
  const headerKey = m => JSON.stringify([
    m.title, m.parent_title, m.sid, m.agent_type, !!m.starred, m.spawned_by || null,
    (m.agent_items || []).map(a => [a.id, a.title, a.type]),
  ]);
  const before = cache.get(viewKey(S.sel, S.agent));
  const beforeKey = before ? headerKey(before.meta) : '';
  let currentEventAdded = false;
  for (const s of S.sessions) {
    for (const e of cache.values()) {
      if (e.meta.uid !== s.uid) continue;
      const agent = e.meta.agent_id;
      if (!agent) {
        e.meta = { ...e.meta, ...s };
        if (mergeSessionMetaEvent(e, s) && e === before) currentEventAdded = true;
        continue;
      }
      const item = (s.agent_items || []).find(a => a.id === agent);
      if (!item) continue;
      const childPath = e.meta.path;
      e.meta = {
        ...e.meta, ...s, path: childPath,
        cwd: item.cwd ?? e.meta.cwd, model: item.model ?? e.meta.model,
        created: item.created ?? e.meta.created,
        sid: agent, title: item.title, size: item.size, updated: item.updated,
        agent_id: agent, agent_type: item.type, parent_title: s.title,
      };
    }
  }
  const current = cache.get(viewKey(S.sel, S.agent));
  const oldHead = $('#detail > .dhead');
  if (currentEventAdded && current) {
    renderSession(current.meta, current.msgs, current.activity);
  } else if (current && oldHead && headerKey(current.meta) !== beforeKey) {
    oldHead.replaceWith(head(current.meta, entryTotal(current)));
    layoutSessionHead();
    auditDetailRendered('meta-refresh');
  }
}

let sessionLoadRun = 0;
let sessionLoadRetry = null;

async function loadSessions(force) {
  const run = ++sessionLoadRun;
  clearTimeout(sessionLoadRetry);
  $('#stat').textContent = force ? ' 重新扫描…' : ' 加载中…';
  const ac = new AbortController();
  const timeout = setTimeout(() => ac.abort(), 15000);
  let d;
  try {
    const r = await fetch(appUrl('api/sessions' + (force ? '?force=1' : '')), { signal: ac.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    d = await r.json();
    if (!Array.isArray(d.sessions)) throw new Error('会话列表格式错误');
  } catch (e) {
    if (run !== sessionLoadRun) return false;
    $('#stat').textContent = ' 加载失败';
    $('#stat').classList.add('err');
    $('#side').innerHTML = `<div class="empty load-failed">
      <p>会话列表暂时无法加载</p><button class="btn load-retry">重试</button></div>`;
    $('.load-retry').onclick = () => loadSessions(false);
    sessionLoadRetry = setTimeout(() => loadSessions(false), 3000);
    return false;
  } finally {
    clearTimeout(timeout);
  }
  if (run !== sessionLoadRun) return false;
  $('#stat').classList.remove('err');
  const seedCursors = S.cursors.size === 0;
  S.sig = d.sig;
  S.sessions = d.sessions;
  applyNodeState(d, 'sessions');
  refreshSessionMeta();
  renderChips();
  renderSide();
  showSessionCount(sidebarSessions().length);
  seedCursors ? seedSidebarCursors(d.sessions) : syncSidebarUpdates(d.sessions);
  return true;
}

/** 列表自动跟进磁盘变化。签名没变时服务端只回一个 unchanged, 成本为个位数毫秒。 */
async function pollSessions() {
  if (document.hidden || !S.sig) return;
  try {
    const d = await (await fetch(appUrl('api/sessions?sig=' + encodeURIComponent(S.sig)))).json();
    applyNodeState(d, 'sessions');
    if (d.unchanged || !d.sessions) return;
    S.sig = d.sig;
    S.sessions = d.sessions;
    renderNodes();
    refreshSessionMeta();
    renderChips();
    syncSidebarUpdates(d.sessions);
    if (S.results) {
      // 搜索结果集合保持不变，只合入 rename 等最新元数据。
      const fresh = new Map(S.sessions.map(s => [s.uid, s]));
      S.results = S.results.map(r => fresh.has(r.uid) ? { ...r, ...fresh.get(r.uid) } : r);
      if (!patchSide(visible())) renderSide();
      return;
    }
    showSessionCount(sidebarSessions().length);
    if (patchSide(visible())) return;   // 能就地更新就不重建, 否则会一直闪
    const side = $('#side');
    const top = side.scrollTop;
    renderSide();                       // 选中态由 S.sel 恢复
    side.scrollTop = top;               // 别打断正在看的位置
    paintLive();
  } catch { /* 下轮再说 */ }
}

setInterval(pollSessions, LIST_MS);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollSessions(); });

function visible() {
  let pool = (S.results || sidebarSessions()).filter(s => !sessionHidden(s)
    && !S.off.has(s.source) && nodeSelected(s));
  if (S.activeOnly) pool = pool.filter(s => s.pending || S.live.has(s.uid));
  if (!S.term || S.results) return pool;          // 搜索态下服务端已经筛过
  return pool.filter(s => hasTerm(s.title) || hasTerm(s.cwd) || hasTerm(s.node_name || ''));
}

// ---------------------------------------------------------------- 左栏
const sessionStarred = uid => !!S.sessions.find(s => s.uid === uid)?.starred;

/* ---------- 左栏多选删除 ---------- */
// 已有记录移入回收站；未落盘的新建会话停止并丢弃，正式运行会话由服务端拒绝删除。
const pickedSessions = new Set();
const sessionPickable = session => !session.fork_parent;
let sessionDeleteBusy = false;

/** 列表会被 SSE/轮询整份重画，选择集合里只保留仍然存在且可删的会话。 */
function syncPickedSessions() {
  if (!S.picking) {
    pickedSessions.clear();
    return pickedSessions;
  }
  const alive = new Set(sidebarSessions().filter(sessionPickable).map(s => s.uid));
  for (const uid of [...pickedSessions]) if (!alive.has(uid)) pickedSessions.delete(uid);
  return pickedSessions;
}

function setPicking(on) {
  S.picking = !!on;
  if (!S.picking) pickedSessions.clear();
  renderPickBar();
  const side = $('#side'), top = side.scrollTop;
  renderSide();
  side.scrollTop = top;       // 进出选择模式不该把列表弹回顶部
}

function toggleSessionPick(uid) {
  pickedSessions.has(uid) ? pickedSessions.delete(uid) : pickedSessions.add(uid);
  const row = $(`#side .item[data-uid="${CSS.escape(uid)}"]`);
  if (row) {
    paintItemPick(row);
    paintGroupPick(row.closest('.group'));
  }
  renderPickBar();
}

/** 整组一起勾/取消：组内还有没选中的就补齐，已经全选才清空。 */
function toggleGroupPick(uids, group) {
  const all = uids.length && uids.every(uid => pickedSessions.has(uid));
  for (const uid of uids) all ? pickedSessions.delete(uid) : pickedSessions.add(uid);
  for (const row of group.querySelectorAll('.item[data-uid]')) paintItemPick(row);
  paintGroupPick(group);
  renderPickBar();
}

function paintItemPick(row) {
  const on = pickedSessions.has(row.dataset.uid);
  row.classList.toggle('picked', on);
  const box = row.querySelector('.item-pick');
  if (box) box.checked = on;
}

function paintGroupPick(group) {
  const box = group?.querySelector('.ghead-pick');
  if (!box) return;
  const rows = [...group.querySelectorAll('.item[data-uid]')]
    .filter(row => row.querySelector('.item-pick'));
  const picked = rows.filter(row => pickedSessions.has(row.dataset.uid)).length;
  box.checked = !!rows.length && picked === rows.length;
  box.indeterminate = picked > 0 && picked < rows.length;
}

function pickAllVisible() {
  const rows = visible().filter(sessionPickable);
  const all = rows.length && rows.every(s => pickedSessions.has(s.uid));
  pickedSessions.clear();
  if (!all) rows.forEach(s => pickedSessions.add(s.uid));
  const side = $('#side'), top = side.scrollTop;
  renderSide();
  side.scrollTop = top;              // 全选不该把列表弹回顶部
  renderPickBar();
}

function renderPickBar() {
  const picked = S.picking ? pickedSessions.size : 0;
  $('#side-tools').hidden = !S.picking;   // 不在选择模式时整条不占高度
  $('#side').classList.toggle('picking', S.picking);
  $('#side-picked').textContent = picked ? `已选 ${picked} 项` : '点会话行勾选';
  const pending = pendingTmuxSessions().filter(s => pickedSessions.has(s.uid)).length;
  const action = pending ? (pending === picked ? '丢弃' : '删除 / 丢弃') : '删除';
  $('#side-pick-delete').textContent = picked ? `${action} (${picked})` : action;
  $('#side-pick-delete').disabled = !picked || sessionDeleteBusy;
  const rows = S.picking ? visible().filter(sessionPickable) : [];
  $('#side-pick-all').disabled = !rows.length;
  $('#side-pick-all').textContent =
    rows.length && rows.every(s => pickedSessions.has(s.uid)) ? '全不选' : '全选';
}

async function deleteSessions(uids, button = null) {
  if (!uids.length || sessionDeleteBusy) return null;
  const pending = pendingTmuxSessions().filter(s => uids.includes(s.uid));
  const pendingIds = new Set(pending.map(s => s.uid));
  const recorded = uids.filter(uid => !pendingIds.has(uid));
  const action = pending.length ? (recorded.length ? '删除 / 丢弃' : '丢弃') : '删除';
  const only = uids.length === 1
    ? (sidebarSessions().find(x => x.uid === uids[0])?.title || '') : '';
  const running = recorded.filter(uid => S.live.has(uid)).length;
  if (!confirm((uids.length === 1
      ? `${action}会话「${only}」?\n\n` : `${action}选中的 ${uids.length} 个会话?\n\n`)
    + (pending.length ? `${pending.length} 个新建会话将停止并丢弃，未发送的草稿也会清除；若已生成会话记录，记录会保留。` : '')
    + (pending.length && recorded.length ? '\n' : '')
    + (recorded.length ? '文件会移入回收站 ~/.local/share/agenthub/trash/, 不会真删。' : '')
    + (running ? `\n其中 ${running} 个还在运行，会被跳过，需要先停止。` : '')))
    return null;
  sessionDeleteBusy = true;
  if (button) button.disabled = true;
  const watched = recorded.includes(S.sel) ? S.sel : null;
  if (watched) closeWatch();   // 文件即将移走，先停掉这条 SSE
  const d = { deleted: [], errors: [] };
  try {
    for (const info of pending) {
      try {
        await discardPendingSession(info);
        d.deleted.push({ uid: info.uid });
      } catch (e) {
        d.errors.push({ uid: info.uid, title: info.title, error: e.message });
      }
    }
    if (recorded.length) {
      try {
        const r = await fetch(appUrl('api/sessions/delete'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ uids: recorded }),
        });
        const result = await r.json();
        if (!r.ok || result.error) throw new Error(result.error || `HTTP ${r.status}`);
        d.deleted.push(...(result.deleted || []));
        d.errors.push(...(result.errors || []));
      } catch (e) {
        d.errors.push(...recorded.map(uid => ({ uid, error: e.message })));
      }
    }
    if (pending.length) await loadTermList();
  } finally {
    sessionDeleteBusy = false;
    if (button) button.disabled = false;
  }
  const gone = new Set((d.deleted || []).map(x => x.uid));
  const failed = d.errors || [];
  if (watched && S.sel === watched && !gone.has(watched)) watchSession(watched, S.agent);
  if (gone.size) {
    S.sessions = S.sessions.filter(x => !gone.has(x.uid));
    if (S.results) S.results = S.results.filter(x => !gone.has(x.uid));
    if (gone.has(S.sel)) {
      S.sel = null;
      store.set('sel', null);
      $('#detail').innerHTML = '<div class="empty">已移入回收站'
        + '<br><button type="button" class="btn" id="detail-open-trash">打开回收站</button></div>';
      $('#detail-open-trash').onclick = openTrash;
      ensureConsolePlaceholder();
      auditDetailRendered('trashed');
      showMobileList();
    }
  }
  renderChips();
  renderSide();
  if (failed.length === 1 && uids.length === 1) {
    alert(`${action}失败: ` + failed[0].error);
  } else if (failed.length) {
    const lines = failed.slice(0, 5).map(x => `· ${x.title || x.uid}: ${x.error}`);
    alert(`已${action} ${gone.size} 个，${failed.length} 个操作失败:\n\n`
      + lines.join('\n') + (failed.length > 5 ? '\n…' : ''));
  }
  return { gone, failed };
}

async function deletePickedSessions() {
  const result = await deleteSessions([...pickedSessions], $('#side-pick-delete'));
  if (!result) return;
  // 删不掉的（多半还在运行）留在选择里，用户停掉会话后可以直接再点删除。
  pickedSessions.clear();
  result.failed.forEach(x => pickedSessions.add(x.uid));
  if (result.failed.length) { renderSide(); renderPickBar(); } else setPicking(false);
}

$('#side-pick-cancel').onclick = () => setPicking(false);
$('#side-pick-all').onclick = pickAllVisible;
$('#side-pick-delete').onclick = deletePickedSessions;

/* ---------- 会话行的右键 / 长按菜单 ---------- */
// 删除入口不再常驻占位：右键（手机长按）某条会话，才给出删除和进入多选。
const LONG_PRESS_MS = 480;
const LONG_PRESS_SLOP = 12;   // 手指按住时的自然微动不该算滑动
let menuUid = '';
let longPress = { timer: 0, x: 0, y: 0 };
let suppressItemClick = false;

function openItemMenu(uid, x, y) {
  const menu = $('#item-menu');
  menuUid = uid;
  const row = sidebarSessions().find(session => session.uid === uid);
  const parent = !!row?.fork_parent;
  const running = S.live.has(uid);
  menu.querySelector('[data-act="stop"]').hidden = parent || row?.pending || !running;
  menu.querySelector('[data-act="hide"]').hidden = !parent;
  menu.querySelector('[data-act="delete"]').hidden = parent || (!row?.pending && running);
  menu.querySelector('[data-act="delete"]').textContent = row?.pending ? '丢弃会话' : '删除会话';
  menu.querySelector('[data-act="pick"]').hidden = parent;
  menu.hidden = false;
  const box = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - box.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - box.height - 8))}px`;
  menu.querySelector('button:not([hidden])')?.focus({ preventScroll: true });
}

function closeItemMenu() {
  $('#item-menu').hidden = true;
  menuUid = '';
}

function cancelLongPress() {
  clearTimeout(longPress.timer);
  longPress.timer = 0;
}

const menuTarget = e => e.target.closest('#side .item');

$('#side').addEventListener('contextmenu', e => {
  const row = menuTarget(e);
  if (!row || S.picking) return;      // 选择模式里点选就够了，不再叠一层菜单
  e.preventDefault();
  openItemMenu(row.dataset.uid, e.clientX, e.clientY);
});

$('#side').addEventListener('pointerdown', e => {
  if (e.pointerType === 'mouse') return;             // 鼠标走 contextmenu
  const row = menuTarget(e);
  if (!row || S.picking) return;
  longPress = { timer: 0, x: e.clientX, y: e.clientY };
  longPress.timer = setTimeout(() => {
    longPress.timer = 0;
    suppressItemClick = true;                        // 长按不该顺手打开会话
    navigator.vibrate?.(12);
    openItemMenu(row.dataset.uid, longPress.x, longPress.y);
  }, LONG_PRESS_MS);
});

$('#side').addEventListener('pointermove', e => {
  if (!longPress.timer) return;
  if (Math.abs(e.clientX - longPress.x) > LONG_PRESS_SLOP
      || Math.abs(e.clientY - longPress.y) > LONG_PRESS_SLOP) cancelLongPress();
});
for (const type of ['pointerup', 'pointercancel', 'pointerleave']) {
  $('#side').addEventListener(type, cancelLongPress);
}
$('#side').addEventListener('scroll', () => { cancelLongPress(); closeItemMenu(); });

// 长按结束时浏览器仍会补一次 click，必须在捕获阶段吃掉。
$('#side').addEventListener('click', e => {
  if (!suppressItemClick) return;
  suppressItemClick = false;
  e.stopPropagation();
  e.preventDefault();
}, true);

$('#item-menu').onclick = async e => {
  const button = e.target.closest('button[data-act]');
  if (!button) return;
  const uid = menuUid;
  closeItemMenu();
  if (!uid) return;
  if (button.dataset.act === 'hide') {
    await setForkParentVisibility([uid], false);
    return;
  }
  if (button.dataset.act === 'pick') {
    pickedSessions.add(uid);       // 从哪条进入多选，就先勾上哪条
    setPicking(true);
    return;
  }
  if (button.dataset.act === 'stop') {
    const row = S.sessions.find(x => x.uid === uid);
    if (row) await stopSession(row);
    return;
  }
  await deleteSessions([uid]);
};

document.addEventListener('pointerdown', e => {
  if (!$('#item-menu').hidden && !e.target.closest('#item-menu')) closeItemMenu();
}, true);
addEventListener('resize', closeItemMenu);

function starButtonMarkup(uid, starred, cls = '', id = '') {
  const label = starred ? '取消星标' : '标为星标';
  return `<button type="button"${id ? ` id="${id}"` : ''}
    class="star-toggle ${cls}${starred ? ' on' : ''}" data-star-uid="${esc(uid)}"
    title="${label}" aria-label="${label}" aria-pressed="${starred}"
    ${S.starBusy.has(uid) ? 'disabled' : ''}>${uiIcon(starred ? 'star-filled' : 'star')}</button>`;
}

function paintStarButton(button, starred, busy = false) {
  if (!button) return;
  const label = starred ? '取消星标' : '标为星标';
  button.classList.toggle('on', starred);
  button.title = button.ariaLabel = label;
  button.setAttribute('aria-pressed', String(starred));
  button.disabled = busy;
  button.innerHTML = uiIcon(starred ? 'star-filled' : 'star');
  labelSessionAction(button);
}

function applySessionStar(uid, starred, starredAt = null) {
  for (const rows of [S.sessions, S.results || []]) {
    const row = rows.find(s => s.uid === uid);
    if (!row) continue;
    row.starred = starred;
    if (starredAt) row.starred_at = starredAt;
    else delete row.starred_at;
  }
  for (const entry of cache.values()) {
    if (entry.meta?.uid !== uid) continue;
    entry.meta.starred = starred;
    if (starredAt) entry.meta.starred_at = starredAt;
    else delete entry.meta.starred_at;
  }
}

function applyForkParentVisibility(uid, visible) {
  for (const rows of [S.sessions, S.results || []]) {
    const row = rows.find(session => session.uid === uid);
    if (row?.fork_parent) row.fork_parent_visible = visible;
  }
  for (const entry of cache.values()) {
    if (entry.meta?.uid === uid && entry.meta.fork_parent) {
      entry.meta.fork_parent_visible = visible;
    }
  }
}

async function setForkParentVisibility(uids, visible, button = null) {
  const unique = [...new Set(uids)].filter(Boolean);
  if (!unique.length) return {updated: [], errors: []};
  if (button) button.disabled = true;
  try {
    const response = await fetch(appUrl('api/sessions/fork-visibility'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({uids: unique, visible}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    for (const row of data.updated || []) {
      applyForkParentVisibility(row.uid, !!row.fork_parent_visible);
    }
    renderNodes();
    renderChips();
    renderSide();
    showSessionCount();
    const selected = S.sessions.find(session => session.uid === S.sel);
    if (selected) renderSessionAction(selected);
    renderForkChainMenu();
    if (data.errors?.length) {
      alert(`父会话显示状态有 ${data.errors.length} 项未保存：${data.errors[0].error}`);
    }
    return data;
  } catch (error) {
    alert('父会话显示状态保存失败: ' + error.message);
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

function refreshStarPresentation(uid) {
  const side = $('#side');
  const top = side?.scrollTop || 0;
  renderSide();
  if (side) side.scrollTop = top;
  if (S.sel === uid) paintStarButton($('#a-star'), sessionStarred(uid), S.starBusy.has(uid));
}

async function toggleSessionStar(uid) {
  if (!uid || S.starBusy.has(uid)) return;
  const before = sessionStarred(uid);
  const wanted = !before;
  S.starBusy.add(uid);
  applySessionStar(uid, wanted);
  refreshStarPresentation(uid);
  try {
    const response = await fetch(appUrl('api/session/star'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({uid, starred: wanted}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    applySessionStar(uid, !!data.starred, data.starred_at || null);
  } catch (error) {
    applySessionStar(uid, before);
    const stat = $('#stat');
    if (stat) {
      stat.textContent = ' 星标保存失败';
      stat.classList.add('err');
      setTimeout(() => { stat.classList.remove('err'); showSessionCount(sidebarSessions().length); }, 1800);
    }
    console.error('星标保存失败', error);
  } finally {
    S.starBusy.delete(uid);
    refreshStarPresentation(uid);
  }
}

function renderChips() {
  const box = $('#chips');
  const wanted = new Set(Object.keys(SOURCES));
  for (const old of box.querySelectorAll(':scope > .chip[data-source]')) {
    if (!wanted.has(old.dataset.source)) old.remove();
  }
  for (const [k, v] of Object.entries(SOURCES)) {
    const n = sidebarSessions().filter(s => s.source === k && nodeSelected(s)).length;
    let c = box.querySelector(`:scope > .chip[data-source="${CSS.escape(k)}"]`);
    if (c && c.tagName !== 'BUTTON') { c.remove(); c = null; }
    if (!c) {
      c = el('button', 'chip', `${icon(k)}<span>${v.name}</span><b></b>`);
      c.type = 'button';
      c.dataset.source = k;
      c.onclick = () => {
        S.off.has(k) ? S.off.delete(k) : S.off.add(k);
        store.set('off', [...S.off]);
        renderChips(); renderSide();
        if (HUB_MODE && S.results !== null) void runSearch();
      };
      box.appendChild(c);
    }
    c.classList.toggle('off', S.off.has(k));
    c.classList.toggle('on', !S.off.has(k));
    c.setAttribute('aria-pressed', String(!S.off.has(k)));
    c.querySelector('.ico').style.color = v.color;
    c.querySelector(':scope > b').textContent = n;
    c.title = v.name;
    c.setAttribute('aria-label', `${v.name}，${n} 个会话`);
  }
}

/* ---------- 分层：发起关系 ---------- */
// 会话由谁发起（spawned_by）是服务端从进程树看出来并记住的；这里只在同机器、
// 同来源内按原生 sid 解析成列表里的那一行，和 forkAncestors 一个规矩。
const spawnKey = (nodeId, source, sid) => JSON.stringify([nodeId || '', source, String(sid)]);
function spawnParentOf(session, byKey) {
  const parent = session.spawned_by;
  if (!parent?.source || !parent.sid) return null;
  const row = byKey.get(spawnKey(session.node_id, parent.source, parent.sid));
  return row && row !== session ? row : null;
}

/** 分层模式下的树：每条会话直接发起的会话，以及哪些会话已挂在别人下面。
 *  发起者不在当前列表里（被筛掉、已删除）的会话仍作根显示。 */
function nestTree(list) {
  const children = new Map(), nested = new Set();
  if (!S.nest) return {children, nested};
  const byKey = new Map(list.map(s => [spawnKey(s.node_id, s.source, s.sid), s]));
  const parentOf = new Map();
  for (const s of list) {
    const parent = spawnParentOf(s, byKey);
    if (!parent) continue;
    // compact/continue 会另写一份 JSONL，新进程常继承旧会话的环境变量，
    // spawned_by 会误把「同一条对话的续写」当成派出去的孩子。
    if (parent.continued_in === s.uid) continue;
    // 数据出环（A 由 B 发起、B 又由 A 发起）时后处理的那条留作根，树不会吞掉它
    let cur = parent, looped = false;
    for (let i = 0; cur && i < list.length; i++) {
      if (cur === s) { looped = true; break; }
      cur = parentOf.get(cur.uid) || null;
    }
    if (looped) continue;
    parentOf.set(s.uid, parent);
    if (!children.has(parent.uid)) children.set(parent.uid, []);
    children.get(parent.uid).push(s);
    nested.add(s.uid);
  }
  return {children, nested};
}

/** 一条会话连同它的子代理和它发起的会话，按活动时间倒序、还在跑的在前。 */
function nestStamp(s, children, memo = new Map()) {
  if (memo.has(s.uid)) return memo.get(s.uid);
  let stamp = +new Date(s.updated) || 0;
  memo.set(s.uid, stamp);   // 先占位，环再深也只算一遍
  for (const a of s.agent_items || []) stamp = Math.max(stamp, +new Date(a.updated) || 0);
  for (const c of children.get(s.uid) || []) stamp = Math.max(stamp, nestStamp(c, children, memo));
  memo.set(s.uid, stamp);
  return stamp;
}

function expandRows(s, depth, children, out, seen) {
  const row = {s, agent: null, depth, kids: 0, closed: false};
  out.push(row);
  if (!S.nest) return;
  const when = v => +new Date(v) || 0;
  const kids = [
    ...(s.agent_items || []).map(agent => ({agent, running: agentRunning(s.uid, agent),
      when: when(agent.updated)})),
    ...(children.get(s.uid) || []).map(c => ({session: c, running: S.live.has(c.uid),
      when: nestStamp(c, children)})),
  ].sort((a, b) => (b.running - a.running) || (b.when - a.when));
  row.closed = kids.length > 0 && S.nestClosed.has(s.uid);
  const start = out.length;
  for (const k of kids) {
    if (k.agent) out.push({s, agent: k.agent, depth: depth + 1});
    else if (!seen.has(k.session.uid)) {
      seen.add(k.session.uid);
      expandRows(k.session, depth + 1, children, out, seen);
    }
  }
  // 三角上写的是收起后消失的整棵子树行数；收起时这些行只用来数数，不进列表
  row.kids = out.length - start;
  if (row.closed) out.length = start;
}

const rowKey = row => row.agent ? `${row.s.uid}#${row.agent.id}` : row.s.uid;

/** 左栏分组：[组键, 行数组]。行 = {s, agent, depth}；平铺模式下 depth 恒为 0 且没有子代理行。
 *  分层模式按整棵子树的最新活动排位和归组，发起的孩子刚有动静时父亲跟着浮上来。 */
function groupBy(list) {
  const {children, nested} = nestTree(list);
  const memo = new Map();
  const stamp = s => S.nest ? nestStamp(s, children, memo) : (+new Date(s.updated) || 0);
  const m = new Map();
  for (const s of list) {
    if (nested.has(s.uid)) continue;
    const k = S.view === 'tree' ? (s.node_id ? JSON.stringify([s.node_id, s.cwd || '(未知)']) : (s.cwd || '(未知)'))
      : dayKey(new Date(stamp(s)).toISOString());
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(s);
  }
  const keys = [...m.keys()];
  if (S.view === 'date') keys.sort().reverse();
  else keys.sort((a, b) => {
    const ta = Math.max(...m.get(a).map(stamp));
    const tb = Math.max(...m.get(b).map(stamp));
    return tb - ta;
  });
  for (const k of keys) m.get(k).sort((a, b) =>
    // 项目树的顺序只表达真实活动时间，点星不应让会话突然跳位。
    // 时间轴才在同一日期内将收藏置前；收藏时间不参与排序。
    (S.view === 'date' ? Number(!!b.starred) - Number(!!a.starred) : 0)
    || stamp(b) - stamp(a));
  const seen = new Set();
  return keys.map(k => {
    const rows = [];
    for (const s of m.get(k)) {
      if (seen.has(s.uid)) continue;
      seen.add(s.uid);
      expandRows(s, 0, children, rows, seen);
    }
    return [k, rows];
  });
}

const itemMeta = s => (s.stale ? '离线缓存 · ' : '') + (s.pending ? `${fmtTime(s.updated)} · 等待首条消息`
  : [fmtTime(s.updated), fmtSize(s.size), s.model || '',
                       s.continued_in && S.sessions.some(x => x.uid === s.continued_in) ? '已续写' : '',
                       S.sessions.some(x => x.continued_in === s.uid) ? '续写' : '',
                       s.agents ? `⑂${s.agents}` : '',
                       s.hits ? `命中 ${s.hits}${s.hits_capped ? '+' : ''}` : '']
                      .filter(Boolean).join(' · '));

/** 就地更新左栏, 成功返回 true。
 *
 *  只要分组和成员没变, 就复用现有节点: 顺序变了就把节点挪一挪, 文字变了就改文字。
 *  这一步很要紧 —— 活跃会话每隔几秒就更新一次 updated, 排序跟着来回换,
 *  每次都重建整棵子树的话, 看起来就是列表一直在闪。
 *  只有真的多了/少了会话(或分组变了)才回去整体重渲染。 */
function patchSide(list) {
  const side = $('#side');
  const groups = groupBy(list);
  const have = new Map([...side.querySelectorAll(':scope > .group')].map(g => [g.dataset.key, g]));
  if (have.size !== groups.length || groups.some(([k]) => !have.has(k))) return false;

  for (const [key, rows] of groups) {
    const g = have.get(key);
    const ul = g.querySelector('.glist');
    if (!ul) return false;
    const nodes = new Map([...ul.children].map(n => [n.dataset.key, n]));
    if (nodes.size !== rows.length || rows.some(r => !nodes.has(rowKey(r)))) return false;
    for (const r of rows) {
      const n = nodes.get(rowKey(r));
      if (n.dataset.depth !== String(r.depth)) return false;   // 挂到别人下面去了，整体重画
      ul.appendChild(n);                     // 按新顺序挪位置, 节点本身不动
      if (r.agent) { patchAgentRow(n, r); continue; }
      if (!!n.querySelector(':scope > .nest-caret') !== r.kids > 0) return false;
      const s = r.s;
      const m = n.querySelector('.m');
      const t = itemMeta(s);
      if (m && m.textContent !== t) m.textContent = t;
      const title = n.querySelector('.t');
      if (title && title.textContent !== s.title) {
        title.title = s.title;
        title.innerHTML = hl(s.title);
      }
      paintStarButton(n.querySelector('.item-star'), !!s.starred, S.starBusy.has(s.uid));
      const cwd = n.querySelector('.cwd');
      if (cwd && (cwd.title !== (s.cwd || '') || cwd.dataset.nodeName !== (s.node_name || ''))) {
        cwd.title = s.cwd || '';
        cwd.dataset.nodeName = s.node_name || '';
        cwd.innerHTML = timelineDirectoryMarkup(s);
      }
    }
    const count = rows.filter(r => !r.agent).length;
    const c = g.querySelector('.gcount');
    if (c && c.textContent !== String(count)) c.textContent = count;
  }
  fitTimelineDirectories();
  return true;
}

/* ---------- 子代理行 ---------- */
const agentMeta = (uid, a) => `子代理 · ${a.type} · ${fmtSpan(a.created, agentRunning(uid, a) ? null : a.updated)}`;

function agentRow(s, a, depth) {
  const it = el('div', 'item agent',
    `<span class="ico" aria-hidden="true">⑂<span class="item-status"></span></span>
     <div class="body">
       <div class="t" title="${esc(a.title)}">${hl(a.title)}</div>
       <div class="m">${esc(agentMeta(s.uid, a))}</div>
     </div>`);
  it.dataset.key = rowKey({s, agent: a});
  it.dataset.owner = s.uid;
  it.dataset.agent = a.id;
  it.dataset.depth = depth;
  it.style.setProperty('--depth', depth);
  it.onclick = () => { if (!S.picking) openSession(s.uid, a.id); };
  paintAgentStatus(it);
  return it;
}

function patchAgentRow(node, row) {
  const title = node.querySelector('.t');
  if (title && title.textContent !== row.agent.title) {
    title.title = row.agent.title;
    title.innerHTML = hl(row.agent.title);
  }
  const m = node.querySelector('.m'), t = agentMeta(row.s.uid, row.agent);
  if (m && m.textContent !== t) m.textContent = t;
  paintAgentStatus(node);
}

/** 子代理行的绿点和选中态：跑没跑由服务端按 transcript 判断，父进程不在则一律不算。 */
function paintAgentStatus(node) {
  const uid = node.dataset.owner, id = node.dataset.agent;
  const item = (S.sessions.find(s => s.uid === uid)?.agent_items || []).find(a => a.id === id);
  const running = !!item && agentRunning(uid, item);
  node.classList.toggle('live', running);
  node.classList.toggle('sel', S.sel === uid && S.agent === id);
  const badge = node.querySelector(':scope > .ico > .item-status');
  if (!badge) return;
  badge.classList.toggle('visible', running);
  badge.title = badge.ariaLabel = running ? '子代理运行中' : '';
}

function renderSide() {
  renderSessionCounts();
  const side = $('#side');
  side.innerHTML = '';
  const list = visible();
  const picked = syncPickedSessions();
  renderPickBar();
  if (!list.length) {
    const text = S.activeOnly
      ? (S.results ? '没有活动的匹配会话' : '没有活动会话')
      : (S.results ? '没有匹配的会话' : '没有会话');
    side.appendChild(el('div', 'empty', text));
    return;
  }
  for (const [key, rows] of groupBy(list)) {
    const items = rows.filter(r => !r.agent).map(r => r.s);
    const g = el('div', 'group' + (S.closed.has(key) ? ' closed' : ''));
    g.dataset.key = key;
    const label = S.view === 'tree' ? nodeDirectory(items[0]) : key;   // 分组标题不缩写, 只换 ~
    const groupUids = items.filter(sessionPickable).map(x => x.uid);
    const head = el('div', 'ghead',
      `${S.picking ? `<input type="checkbox" class="ghead-pick"
         aria-label="选中「${esc(label)}」下的全部会话">` : ''}
       <span class="caret">▼</span><span class="gname" title="${esc(key)}">${S.view === 'tree' ? nodeDirectoryMarkup(items[0]) : esc(label)}</span>
       <span class="gcount">${items.length}</span>`);
    head.onclick = () => {
      S.closed.has(key) ? S.closed.delete(key) : S.closed.add(key);
      store.set('closed', [...S.closed]);
      g.classList.toggle('closed');
      scheduleTimelineFit();
    };
    const groupBox = head.querySelector('.ghead-pick');
    if (groupBox) {
      groupBox.onclick = event => {
        event.stopPropagation();      // 勾整组，不要顺手把分组折叠了
        toggleGroupPick(groupUids, g);
      };
    }
    g.appendChild(head);
    const ul = el('div', 'glist');
    for (const r of rows) {
      if (r.agent) { ul.appendChild(agentRow(r.s, r.agent, r.depth)); continue; }
      const s = r.s;
      const meta = itemMeta(s);
      const pickable = S.picking && sessionPickable(s);
      // 分层时正在看的子代理有自己那一行，主会话行不再一起亮
      const selected = S.sel === s.uid && !(S.nest && S.agent);
      const it = el('div', 'item' + (selected ? ' sel' : '') + (r.closed ? ' nest-closed' : '')
                              + (s.pending ? (s.stale ? ' pending' : ' pending live live-tmux') : '')
                              + (!s.pending && S.live.has(s.uid) ? ' live' : '')
                              + (!s.pending && S.liveTmux.has(s.uid) ? ' live-tmux' : '')
                              + (pickable && picked.has(s.uid) ? ' picked' : ''),
        `${r.kids ? `<button type="button" class="nest-caret" aria-expanded="${!r.closed}"
           title="${r.closed ? '展开' : '收起'} ${r.kids} 项" aria-label="${r.closed ? '展开' : '收起'}「${esc(s.title)}」下的 ${r.kids} 项"></button>` : ''}
         ${pickable ? `<input type="checkbox" class="item-pick" tabindex="-1"
           ${picked.has(s.uid) ? 'checked' : ''} aria-label="选中「${esc(s.title)}」">` : ''}
         <span class="ico">${icon(s.source)}<span class="item-status"></span></span>
         <div class="body">
           <div class="t" title="${esc(s.title)}">${hl(s.title)}</div>
           <div class="m">${esc(meta)}</div>
           ${S.view === 'date'
             ? `<div class="cwd" title="${esc(s.cwd)}" data-node-name="${esc(s.node_name || '')}">${timelineDirectoryMarkup(s)}</div>` : ''}
           ${s.snippet ? `<div class="snip">${hl(s.snippet)}</div>` : ''}
         </div>
         ${s.pending ? '' : starButtonMarkup(s.uid, !!s.starred, 'item-star')}`);
      it.dataset.uid = s.uid;
      it.dataset.key = s.uid;
      it.dataset.depth = r.depth;
      it.style.setProperty('--depth', r.depth);
      if (s.pending) it.dataset.tmuxName = s.tmuxName;
      it.onclick = () => {
        if (pickable) return toggleSessionPick(s.uid);
        if (S.picking) return;
        s.pending ? openPendingSession(s) : openSession(s.uid);
      };
      const star = it.querySelector('.item-star');
      if (star) star.onclick = event => {
        event.stopPropagation();
        toggleSessionStar(s.uid);
      };
      const caret = it.querySelector('.nest-caret');
      if (caret) caret.onclick = event => {
        event.stopPropagation();
        toggleNestFold(s.uid);
      };
      paintItemStatus(it);
      ul.appendChild(it);
    }
    g.appendChild(ul);
    side.appendChild(g);
    if (groupBox) paintGroupPick(g);
  }
  fitTimelineDirectories();
}

// 与后端 build_pattern 保持同一套规则: 全词用环视而非 \b, 中文才能正常匹配
function reTerm(global) {
  let src = S.opts.regex ? S.term : S.term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  if (S.opts.word) src = `(?<!\\w)(?:${src})(?!\\w)`;
  try {
    return new RegExp(src, (S.opts.case ? '' : 'i') + (global ? 'g' : ''));
  } catch {
    return null;   // 正则写到一半是常态, 不该炸掉整个界面
  }
}

function hasTerm(t) {
  if (!S.term) return false;
  const re = reTerm(false);
  return re ? re.test(t) : false;
}

function hl(text) {
  const re = S.term && reTerm(true);
  if (!re) return esc(text);
  return esc(text).replace(re, m => `<mark>${m}</mark>`);
}

/** 在已渲染的 DOM 里给命中词套 <mark>, 走文本节点所以不会破坏标签。 */
function markMatches(root) {
  if (!S.term) return 0;
  const re = reTerm(true);
  if (!re) return 0;
  const messageBox = $('#msgs');
  const existing = messageBox && root !== messageBox && messageBox.contains(root)
    ? messageBox.querySelectorAll('mark').length : 0;
  const budget = Math.max(0, MARK_MAX - existing);
  // 只高亮正文: 折叠预览是正文副本, 高亮在那里会造成重复计数。
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => {
      const msg = n.parentElement.closest('.msg');
      return n.parentElement.closest('mark, .fold-preview, .katex')
        || !msg || !SEARCH_ROLES.has(msg.dataset.role)
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    re.lastIndex = 0;                 // 带 g 的 test 会推进 lastIndex
    if (re.test(n.nodeValue)) targets.push(n);
  }
  let count = 0;
  for (const t of targets) {
    if (count >= budget) { S.markCapped = true; break; }   // 搜 "a" 会生成上万节点, 会卡死
    const frag = document.createDocumentFragment();
    let last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(t.nodeValue))) {
      if (!m[0]) { re.lastIndex++; continue; }
      frag.append(t.nodeValue.slice(last, m.index));
      const mk = document.createElement('mark');
      mk.textContent = m[0];
      frag.appendChild(mk);
      last = m.index + m[0].length;
      if (++count >= budget) { S.markCapped = true; break; }
    }
    frag.append(t.nodeValue.slice(last));
    t.parentNode.replaceChild(frag, t);
  }
  return count;
}

function jumpMark(delta) {
  const marks = [...document.querySelectorAll('#msgs mark')].filter(
    m => m.checkVisibility ? m.checkVisibility({ visibilityProperty: true }) : m.offsetParent);
  if (!marks.length) return;
  S.cur = (S.cur + delta + marks.length) % marks.length;
  marks.forEach(m => m.classList.remove('cur'));
  marks[S.cur].classList.add('cur');
  marks[S.cur].scrollIntoView({ block: 'center', behavior: 'smooth' });
  const c = $('#mcount');
  if (c) c.textContent = `${S.cur + 1}/${marks.length}${S.markCapped ? '+' : ''} 处匹配`;
}

function updateMatchNav({jump = false} = {}) {
  const c = $('#mcount');
  if (!c) return 0;
  const hits = document.querySelectorAll('#msgs mark').length;
  c.textContent = hits ? `${hits}${S.markCapped ? '+' : ''} 处匹配` : '本页无匹配';
  const capped = S.markCapped || S.autoOpen >= AUTO_OPEN_MAX;
  c.classList.toggle('capped', capped);
  if (capped) {
    c.title = `命中过多：只标注前 ${MARK_MAX} 处、自动展开前 ${AUTO_OPEN_MAX} 条，其余标 ● 需手动展开`;
  } else {
    c.removeAttribute('title');
  }
  $('#m-prev').onclick = () => jumpMark(-1);
  $('#m-next').onclick = () => jumpMark(1);
  if (jump && hits) jumpMark(1);
  return hits;
}

// ---------------------------------------------------------------- 详情
let inflight = null;

function ensureConsolePlaceholder() {
  if ($('#a-term')) return;
  const heading = el('div', 'dhead');
  heading.innerHTML = `<div class="dtitle"><h2>控制台</h2><div class="dhead-actions">${consoleButtonMarkup()}</div></div>`;
  $('#detail').prepend(heading);
  bindConsoleButton(heading.querySelector('#a-term'), S.sel, S.agent);
  showConsoleToast('');
}

function continuedPredecessor(uid) {
  return S.sessions.find(s => s.continued_in === uid) || null;
}

async function openSession(uid, agent = null) {
  const selectedAgent = agent || null;
  browserAuditEvent('session.opened', {agent: selectedAgent || '', cached: cache.has(viewKey(uid, selectedAgent))},
    null, {uid});
  showMobileDetail();
  inflight?.abort();            // 连点列表时, 放弃上一个还没回来的请求
  const ac = inflight = new AbortController();
  closeWatch();
  if (typeof T !== 'undefined') {
    if (T.uid && (T.uid !== uid || selectedAgent)) closeTermPane(true);
    $('#composer').classList.add('hidden');    // 先收起, 渲染完再按新会话的状态决定
  }
  S.sel = uid;
  S.agent = selectedAgent;
  clearUnread(uid);
  store.set('sel', uid);
  store.set('agent', S.agent ? { uid, id: S.agent } : null);
  renderSide();

  const key = viewKey(uid, selectedAgent);
  const hit = cacheGet(key);
  if (hit) {
    // 先把缓存立即画出来，但暂不占一个长期 SSE 连接。补齐缓存游标之后再
    // 建 watch，避免 HTTP/1 连接池紧张时增量 fetch 永远排在 EventSource 后。
    await renderSession(hit.meta, hit.msgs, hit.activity, { startWatch: false });
    if (S.sel === uid && S.agent === selectedAgent) {
      await syncSession(uid, selectedAgent);
      if (S.sel === uid && S.agent === selectedAgent) watchSession(uid, selectedAgent);
    }
    return;
  }

  $('#detail').innerHTML = '<div class="spin">正在读取会话…</div>';
  ensureConsolePlaceholder();
  auditDetailRendered('loading');
  progress(0, 0, '下载');
  let res;
  try {
    res = await fetchMessages(uid, {
      agent: selectedAgent, signal: ac.signal,
      windowed: true,
      onProgress: (a, b, detail) => progress(a, b, '下载', detail),
    });
  } catch (e) {
    if (e.name === 'AbortError') return;      // 已经切到别的会话了
    progressDone();
    $('#detail').innerHTML = `<div class="empty">读取失败: ${esc(e.message)}</div>`;
    ensureConsolePlaceholder();
    auditDetailRendered('load-failed', {error: String(e.message || e).slice(0, 300)});
    return;
  }
  if (S.sel !== uid || S.agent !== selectedAgent) return; // 期间切了别的视图
  const { data, bytes } = res;
  if (!selectedAgent && Array.isArray(data.outbox)) {
    syncServerOutbox(uid, data.outbox, data.outbox_version);
  }
  cachePut(key, { meta: data.meta, msgs: data.messages, version: data.version,
                  end: data.end, anchor: data.anchor, activity: data.activity, bytes,
                  prompt: data.prompt || null,
                  total: data.message_total, partial: data.partial || null });
  S.cursors.set(key, {end: data.end, head: data.version.head, anchor: data.anchor});
  await renderSession(data.meta, data.messages, data.activity);
}

// ---- 保持贴底 ----
// 只要用户没有主动往上翻, 消息区就一直停在最新一条: 新消息追加、消息展开/折叠、
// 图片或表格撑高、窗口缩放, 都要跟着走。
const BOTTOM_SLACK = 48;
let _stick = true, _lastTop = 0, _lockUntil = 0, _selfScroll = false;

/** 窗口缩放会让浏览器自己调整滚动位置, 那一小段时间内的滚动不算用户意图。
 *  只在 window resize 时用 —— 别在内容变化时也锁, 否则锁会被不断续期,
 *  用户主动往上翻都会被忽略。 */
function lockStick(ms = 300) { _lockUntil = performance.now() + ms; }

function atBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= BOTTOM_SLACK;
}

function stickBottom(box, force) {
  if (force) _stick = true;
  if (!_stick) return;
  _selfScroll = true;
  box.scrollTop = box.scrollHeight;
  // 必须读回来: 浏览器会把它夹到 maxScroll, 直接记 scrollHeight 的话
  // 下一次 scroll 事件会把这当成"用户往上翻了一大截", 跟随就断了
  _lastTop = box.scrollTop;
  requestAnimationFrame(() => { _selfScroll = false; });
}

/** 展开/收起大块内容时把触发控件钉在原来的视口坐标。用户点开过程是在看
 *  这一段，不再属于“持续跟随最底部”；否则 ResizeObserver 会把按钮直接
 *  推出屏幕。补两帧可覆盖语法高亮等紧随其后的同步布局变化。 */
function mutateKeepingMessageAnchor(anchor, mutate) {
  const box = $('#msgs');
  if (!box || !box.contains(anchor)) return mutate();
  const top = anchor.getBoundingClientRect().top;
  _stick = false;
  _lastTop = box.scrollTop;
  const restore = () => {
    if (!anchor.isConnected || box !== $('#msgs')) return;
    const delta = anchor.getBoundingClientRect().top - top;
    if (Math.abs(delta) < .5) return;
    _selfScroll = true;
    box.scrollTop += delta;
    _lastTop = box.scrollTop;
    requestAnimationFrame(() => { _selfScroll = false; });
  };
  const result = mutate();
  restore();
  requestAnimationFrame(() => {
    restore();
    requestAnimationFrame(restore);
  });
  return result;
}

function jumpWithinConversation(target, block = 'center') {
  const box = $('#msgs');
  if (!target || !box?.contains(target)) return;
  _stick = false;
  _lastTop = box.scrollTop;
  target.scrollIntoView({block, behavior: 'smooth'});
}

function watchBottom(box) {
  _stick = true;
  _lastTop = box.scrollTop;
  // 按滚动"方向"判断意图, 而不是按当前是否贴底 —— 窗口缩小、内容展开都会让
  // "是否贴底"瞬间变假, 那不是用户想离开底部。
  // 直接听用户的动作来判断"想离开底部"。不能只靠 scroll 事件推方向:
  // 布局重排、程序自身的滚动都会产生 scroll, 混在一起分不清谁是谁。
  const leave = () => { _stick = false; };
  box.addEventListener('wheel', e => { if (e.deltaY < 0) leave(); }, { passive: true });
  box.addEventListener('touchmove', leave, { passive: true });
  box.addEventListener('keydown', e => {
    if (['PageUp', 'ArrowUp', 'Home'].includes(e.key)) leave();
  });
  box.addEventListener('scroll', () => {
    const top = box.scrollTop;
    // 拖滚动条没有 wheel 事件, 靠方向补一手 (排除程序自己滚的那些)
    if (!_selfScroll && performance.now() >= _lockUntil && top < _lastTop - 2) _stick = false;
    else if (atBottom(box)) {
      _stick = true;                               // 回到底部就恢复跟随
      if (box._turnSealPending) queueMicrotask(() => flushPendingTurnSeal(box));
    }
    _lastTop = top;
  });
  // 内容高度变化 (展开消息、渲染完成、字体加载…) 时跟随
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => settle(box));
    ro.observe(box);
    // 普通消息只盯底部 30 条，避免上万节点的观察成本；但图片、公式、表格即使
    // 位于很早的消息里，加载或窄屏重排也会改变总高度，必须额外观察。
    const observeRich = root => {
      if (root.matches?.('img, .katex, .tw')) ro.observe(root);
      root.querySelectorAll?.('img, .katex, .tw').forEach(n => ro.observe(n));
    };
    for (const c of [...box.children].slice(-30)) ro.observe(c);
    observeRich(box);
    box._ro?.disconnect();
    box._ro = ro;
    const mo = new MutationObserver(ms => {
      for (const m of ms) for (const n of m.addedNodes) {
        if (n.nodeType === 1) { ro.observe(n); observeRich(n); }
      }
      settle(box);
    });
    mo.observe(box, { childList: true });
    box._mo?.disconnect();
    box._mo = mo;
  }
}

/** 布局要好几帧才稳: 窗口变窄会让消息重新折行, scrollHeight 一路涨,
 *  只修一次会差一截。补几帧, 直到不再变化。 */
let _settleTick = 0;

function settle(box) {
  if (!_stick) return;
  stickBottom(box);
  if (_settleTick) return;                   // 已经有补偿在跑, 别叠加
  let n = 0, last = -1;
  const tick = () => {
    _settleTick = 0;
    // 布局稳定(高度不再变)就收手 —— 一直空转的话 _selfScroll 长期为真,
    // 用户主动往上翻会被当成程序自己滚的而忽略掉
    if (!_stick || n++ > 10 || box !== $('#msgs') || box.scrollHeight === last) return;
    last = box.scrollHeight;
    stickBottom(box);
    _settleTick = requestAnimationFrame(tick);
  };
  _settleTick = requestAnimationFrame(tick);
}

addEventListener('resize', () => {
  const box = $('#msgs');
  if (!box) return;
  lockStick();
  settle(box);
  setTimeout(() => settle(box), 160);      // 兜住 resize 之后的异步重排
  setTimeout(() => settle(box), 400);
});

/** 整份渲染。消息可能上万条, 分批交还主线程, 否则页面会卡住不动。 */
let renderSeq = 0;

function historyGapNode(info) {
  const gap = el('div', 'history-gap');
  const button = el('button', 'history-gap-load',
    `加载中间 ${Number(info.omitted || 0).toLocaleString()} 条消息`);
  button.type = 'button';
  button.onclick = () => loadFullHistory(info.uid, info.agent, button);
  gap.appendChild(button);
  return gap;
}

async function loadFullHistory(uid, agent, button) {
  const key = viewKey(uid, agent);
  const old = cache.get(key);
  if (!old?.partial) return;
  inflight?.abort();
  const ac = inflight = new AbortController();
  closeWatch();
  button.disabled = true;
  button.textContent = '正在载入完整历史…';
  progress(0, 0, '下载完整历史');
  try {
    const {data, bytes} = await fetchMessages(uid, {
      agent, signal: ac.signal,
      onProgress: (a, b, detail) => progress(a, b, '下载完整历史', detail),
    });
    cachePut(key, {meta: data.meta, msgs: data.messages, version: data.version,
                   end: data.end, anchor: data.anchor, activity: data.activity, bytes,
                   prompt: data.prompt || null,
                   total: data.message_total, partial: null});
    S.cursors.set(key, {end: data.end, head: data.version.head, anchor: data.anchor});
    if (S.sel === uid && S.agent === agent) {
      await renderSession(data.meta, data.messages, data.activity);
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      button.disabled = false;
      button.textContent = '载入失败，点击重试';
    }
  } finally {
    progressDone();
    const current = cache.get(key);
    if (S.sel === uid && S.agent === agent && current?.partial && !_es) {
      watchSession(uid, agent);
    }
  }
}

async function renderSession(meta, msgs, activity = null, { startWatch = true } = {}) {
  const seq = ++renderSeq;
  const uid = meta.uid;
  const agent = meta.agent_id || null;
  if (S.sel !== uid || S.agent !== agent) return;
  const d = $('#detail');
  d.innerHTML = '';
  const entry = cache.get(viewKey(uid, agent));
  if (!agent) reconcileQueuedMessages(uid, msgs);
  d.appendChild(head(meta, entryTotal(entry || {msgs})));
  layoutSessionHead();
  const box = el('div', 'msgs');
  box.id = 'msgs';
  d.appendChild(box);
  auditDetailRendered('render', {messages: msgs.length, seq});
  S.cur = -1; S.autoOpen = 0; S.markCapped = false;

  // 关键: 建在游离的 fragment 里, 最后一次性挂上。
  // 若逐批插入已在文档中的容器, 每批都会触发一次全量 layout, 上万条时是 O(n²) —— 实测 0.2s 变 14s。
  // 批间让出主线程用 setTimeout 而不是 rAF: rAF 会等一次绘制, 又把 layout 成本引回来。
  const partial = entry?.partial;
  const split = partial ? Math.min(+partial.head || 0, msgs.length) : 0;
  const openTail = activity?.state === 'working';
  const tailComplete = (activity && !['working', 'waiting'].includes(activity.state))
    || (!activity && !S.live.has(uid));
  const plan = partial
    ? [...planTurns(msgs.slice(0, split), {tailComplete: false, foldTail: true}),
       {gap: {uid, agent, omitted: partial.omitted}},
       ...planTurns(msgs.slice(split), {openTail, tailComplete})]
    : planTurns(msgs, {openTail, tailComplete});
  const frag = document.createDocumentFragment();
  for (let i = 0; i < plan.length; i += RENDER_BATCH) {
    buildPlan(frag, plan.slice(i, i + RENDER_BATCH), null);
    if (i + RENDER_BATCH < plan.length) {
      progress(i + RENDER_BATCH, plan.length, '渲染');
      await new Promise(r => setTimeout(r, 0));
      if (seq !== renderSeq || S.sel !== uid || S.agent !== agent) return;
    }
  }
  box.appendChild(frag);
  renderConversationTail(activity, uid);
  stickBottom(box, true);                // 默认停在最新的一条
  watchBottom(box);
  progressDone();

  markMatches(box);
  updateMatchNav({jump: true});
  if (typeof renderComposer === 'function') {
    if (S.agent) $('#composer').classList.add('hidden');
    else renderComposer();
  }
  if (seq === renderSeq && S.sel === uid && S.agent === agent) {
    if (startWatch) watchSession(meta.uid, agent); // 之后的更新由服务端推过来
    if (typeof restoreTermPane === 'function') restoreTermPane(uid, agent);
    if (!agent) queueMicrotask(reconcileAllPendingMessages);
    scheduleBrowserSnapshot('rendered');
  }
}

function labelSessionAction(button) {
  if (!button?.classList.contains('session-menu-action')) return;
  let label = button.querySelector('span');
  if (!label) button.appendChild(label = el('span'));
  label.textContent = button.ariaLabel;
}

function sessionActionsMarkup(actions, metadata = '') {
  return `<div class="session-actions">
    <button class="iconbtn" id="a-more" type="button" title="更多会话操作"
      aria-label="更多会话操作" aria-haspopup="menu" aria-expanded="false"
      aria-controls="session-actions-menu"><span aria-hidden="true">⋯</span></button>
    <div id="session-actions-menu" class="session-actions-menu" hidden>
      <div role="menu" aria-label="会话操作">${actions}</div>${metadata}
    </div>
  </div>`;
}

function closeSessionActions(restoreFocus = false) {
  const menu = $('#session-actions-menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  const button = $('#a-more');
  button?.setAttribute('aria-expanded', 'false');
  if (restoreFocus) button?.focus();
}

function bindSessionActions(heading) {
  const button = heading.querySelector('#a-more');
  const menu = heading.querySelector('#session-actions-menu');
  if (!button || !menu) return;
  menu.querySelectorAll('button').forEach(item => {
    item.setAttribute('role', 'menuitem');
    labelSessionAction(item);
  });
  // 记住菜单里的原始顺序，layoutSessionHead 在各级排版之间搬动后还能按它归位
  [...menu.querySelector('[role="menu"]').children].forEach((node, i) => { node.dataset.order = i; });
  [...menu.querySelector('.dmeta')?.children || []].forEach((item, i) => { item.dataset.order = i; });
  const items = () => [...menu.querySelectorAll('button:not(:disabled)')];
  const open = () => {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    const views = heading.querySelector('#session-view-menu');
    if (views) views.hidden = true;
    heading.querySelector('#a-view-switch')?.setAttribute('aria-expanded', 'false');
    closeForkChainMenu();
  };
  button.onclick = () => menu.hidden ? open() : closeSessionActions();
  button.onkeydown = event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    open();
    const rows = items();
    (event.key === 'ArrowUp' ? rows.at(-1) : rows[0])?.focus();
  };
  menu.onclick = event => {
    if (event.target.closest('button')) closeSessionActions(true);
  };
  menu.onkeydown = event => {
    const rows = items(), index = rows.indexOf(document.activeElement);
    const next = {ArrowDown: (index + 1) % rows.length,
      ArrowUp: (index - 1 + rows.length) % rows.length, Home: 0, End: rows.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    rows[next]?.focus();
  };
  button.parentElement.addEventListener('focusout', event => {
    // 在菜单的元信息上按下鼠标选字时焦点落到 body（relatedTarget 为空），不算离开菜单；
    // 点到菜单外面由 document 的 click 兜底关闭
    if (event.relatedTarget && !button.parentElement?.contains(event.relatedTarget)) closeSessionActions();
  });
  layoutSessionHead(heading);
}

// 会话头任何宽度都只占一行，且不因折叠留白。一行上的重要程度：标题 → 操作按钮（按菜单顺序：
// 星标、折叠过程、报告、搜索、停止/删除）→ 元信息（消息数、大小、起止时间、机器、目录、来源、
// 模型、会话号、分支）。宽屏/中屏长标题让到标题行的 40%（不少于 8em）为止，窄屏标题不让位；
// 标题之后先按顺序平铺操作，全放下了再把元信息按顺序跟在标题后面（.dbrief）；从放不下的那一项起
// 后面的全部收进 ⋯ 菜单（放不下某个按钮时元信息也不放，免得次要的露着、重要的反而折了）；
// 菜单空了 ⋯ 不显示。
// 消息数会随新消息变宽，留一点余量免得刚好放下的一项被裁掉
const HEAD_BRIEF_SLACK = 24;
function layoutSessionHead(heading = $('#detail .dhead')) {
  const wrap = heading?.querySelector('.session-actions');
  const menu = heading?.querySelector('#session-actions-menu');
  const list = menu?.querySelector('[role="menu"]');
  if (!wrap || !menu || !list) return;
  const actions = wrap.parentElement;
  const tier = layoutTier();
  const menuButtons = node => node.matches('button') ? [node] : [...node.querySelectorAll('button')];
  closeSessionActions();
  // 先把平铺出去的操作和元信息都收回菜单（保持原始顺序），量过宽度再按顺序往外放
  for (const node of actions.querySelectorAll(':scope > [data-from-menu]')) {
    delete node.dataset.fromMenu;
    for (const item of menuButtons(node)) {
      item.setAttribute('role', 'menuitem');
      labelSessionAction(item);
    }
    list.appendChild(node);
  }
  list.replaceChildren(...[...list.children].sort((a, b) => a.dataset.order - b.dataset.order));
  const more = wrap.querySelector('#a-more');
  if (more) more.hidden = false;   // 量宽度时按 ⋯ 在场算，免得它的显隐反过来改变放得下的项数
  const meta = menu.querySelector('.dmeta');
  let brief = heading.querySelector('.dbrief');
  if (meta && !brief) {
    brief = el('div', 'dbrief');
    actions.before(brief);
  }
  const items = meta ? [...meta.children, ...brief.children]
    .sort((a, b) => a.dataset.order - b.dataset.order) : [];
  for (const item of items) meta.appendChild(item);
  if (brief) brief.hidden = true;
  let keep = 0;
  if (heading.isConnected) {
    const title = heading.querySelector('.dtitle');
    const h2 = title.querySelector('h2');
    const gap = parseFloat(getComputedStyle(title).columnGap) || 0;
    // 宽屏/中屏长标题最多占标题行的 40%（不少于 8em），其余让给操作和元信息；短标题只占自己的宽度。
    // 窄屏标题优先，操作和元信息只填标题右边剩下的空白
    const titleMax = tier === 'narrow' ? Infinity
      : Math.max(8 * parseFloat(getComputedStyle(h2).fontSize), title.clientWidth * 0.4);
    const titleWidth = Math.min(h2.getBoundingClientRect().width, titleMax);
    const room = actions.getBoundingClientRect().left - h2.getBoundingClientRect().left
      - titleWidth - gap * 2 - HEAD_BRIEF_SLACK;
    let used = 0;
    // 操作按钮先放：按菜单顺序逐个平铺，到放不下的那一个为止，它和后面的都留在菜单里
    const actionsGap = parseFloat(getComputedStyle(actions).columnGap) || 0;
    for (const node of [...list.children]) {
      node.dataset.fromMenu = '1';
      actions.insertBefore(node, wrap);
      const width = node.getBoundingClientRect().width + actionsGap;
      if (used + width > room) {
        delete node.dataset.fromMenu;
        list.prepend(node);
        break;
      }
      used += width;
      for (const item of menuButtons(node)) item.removeAttribute('role');
    }
    // 操作全放下了，元信息再按固定顺序往标题后放，放不下的留在菜单里
    if (brief && !list.children.length) {
      brief.hidden = false;
      for (const item of items) brief.appendChild(item);
      const briefGap = parseFloat(getComputedStyle(brief).columnGap) || 0;
      for (const item of items) {
        const width = item.getBoundingClientRect().width + (keep ? briefGap : 0);
        if (used + width > room) break;
        used += width;
        keep++;
      }
      for (const item of items.slice(keep)) meta.appendChild(item);
    }
  }
  if (brief) brief.hidden = !keep;
  heading.classList.toggle('head-flat', !list.children.length);
  if (more) {
    const menuEmpty = !list.children.length;
    more.title = more.ariaLabel = menuEmpty ? '会话信息' : '更多会话操作';
    more.hidden = menuEmpty && !meta?.children.length;
  }
  auditHeaderLayout(heading, tier, actions);
}

// 排版结果按签名去重记 header.layout：档位、平铺了哪些操作、标题后放了几项元信息、
// 标题行有没有横向溢出（溢出就意味着右侧按钮会被 #right 裁掉）。
let headerLayoutSignature = '';
function auditHeaderLayout(heading, tier, actions) {
  if (!heading.isConnected) return;   // head() 构建时先量一次，挂上之后才是真实排版
  try {
    const title = heading.querySelector('.dtitle');
    const brief = heading.querySelector('.dbrief');
    const data = {
      tier,
      inline: [...actions.querySelectorAll('button')].filter(b => !b.hidden && b.offsetWidth)
        .map(b => b.id || (b.dataset.reportBug !== undefined ? 'report-bug' : b.className.split(' ')[0])),
      brief: brief && !brief.hidden ? brief.children.length : 0,
      menu_meta: heading.querySelector('#session-actions-menu .dmeta')?.children.length ?? null,
      title_overflow: title ? title.scrollWidth - title.clientWidth : null,
      width: title?.clientWidth ?? null, head_height: heading.offsetHeight,
    };
    const signature = JSON.stringify(data);
    if (signature === headerLayoutSignature) return;
    headerLayoutSignature = signature;
    browserAuditEvent('header.layout', data);
  } catch { /* 审计不能影响排版 */ }
}

document.addEventListener('click', event => {
  if (!event.target.closest('.session-actions')) closeSessionActions();
}, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && $('#session-actions-menu')?.hidden === false) {
    event.preventDefault();
    event.stopImmediatePropagation();
    closeSessionActions(true);
  }
}, true);
addEventListener('resize', () => closeSessionActions());
for (const media of [MOBILE, MEDIUM]) media.addEventListener('change', () => layoutSessionHead());
// 拖分割线、开合左栏、改窗口都会改变详情区宽度，元信息随之在标题后和 ⋯ 之间进出
{
  let detailWidth = -1;
  new ResizeObserver(entries => {
    const width = entries.at(-1)?.contentRect.width ?? -1;
    if (width === detailWidth) return;
    detailWidth = width;
    layoutSessionHead();
  }).observe($('#detail'));
  document.fonts?.ready.then(() => layoutSessionHead());   // 字体换过之后文字宽度会变
}

// 顶栏右侧按钮只在放不下时才折进 ⋯ 菜单，任何宽度都不因折叠留白：先全部平铺量一次，
// 筛选条被挤压（内容比可见宽度宽）或整条顶栏横向溢出，才从最不常用的一头逐个折起，
// 直到不再挤压。平铺顺序即重要程度：新建、重新扫描、回收站、报告问题、设置。
const HEADER_ACTIONS = ['new-session', 'reload', 'trash', 'report-bug', 'settings'];
function closeHeaderMenu(restoreFocus = false) {
  const menu = $('#header-menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  const button = $('#header-more-btn');
  button?.setAttribute('aria-expanded', 'false');
  if (restoreFocus) button?.focus();
}
function layoutHeader() {
  const header = $('header'), filters = header?.querySelector('.header-filters');
  const more = $('#header-more'), menu = $('#header-menu');
  if (!filters || !more || !menu) return;
  const squeezed = () => filters.scrollWidth > filters.clientWidth
    || header.scrollWidth > header.clientWidth;
  // 现状仍成立（没被挤压，且折起的第一个按钮拿出来也放不下）就不动 DOM：
  // 计数变宽之类的重排大多如此，别把用户正开着的菜单或键盘焦点弄丢
  const actions = more.parentElement;
  if (!squeezed()) {
    const free = actions.getBoundingClientRect().left - filters.getBoundingClientRect().right
      - (parseFloat(getComputedStyle(header).columnGap) || 0);
    const unit = more.getBoundingClientRect().width + (parseFloat(getComputedStyle(actions).columnGap) || 0);
    if (!menu.children.length || free < unit) return;
  }
  closeHeaderMenu();
  const buttons = HEADER_ACTIONS.map(id => document.getElementById(id)).filter(Boolean);
  for (const button of buttons) {
    button.querySelector(':scope > span.menu-label')?.remove();
    button.removeAttribute('role');
    more.before(button);
  }
  more.hidden = true;
  // 折起第一个按钮只是把它换成 ⋯，宽度没省出来；所以真要折就至少折两个，循环自然做到
  let folded = 0;
  while (folded < buttons.length && squeezed()) {
    more.hidden = false;
    const button = buttons[buttons.length - 1 - folded++];
    let label = button.querySelector(':scope > span.menu-label');
    if (!label) button.appendChild(label = el('span', 'menu-label'));
    label.textContent = button.ariaLabel || button.title;
    button.setAttribute('role', 'menuitem');
    menu.prepend(button);   // 从末尾往前折，菜单里仍是平铺时的顺序
  }
  more.hidden = !folded;
}
{
  const button = $('#header-more-btn'), menu = $('#header-menu');
  const items = () => [...menu.querySelectorAll('button:not(:disabled):not(.hidden)')];
  const open = () => { menu.hidden = false; button.setAttribute('aria-expanded', 'true'); };
  button.onclick = () => menu.hidden ? open() : closeHeaderMenu();
  button.onkeydown = event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    open();
    const rows = items();
    (event.key === 'ArrowUp' ? rows.at(-1) : rows[0])?.focus();
  };
  menu.addEventListener('click', event => {
    if (event.target.closest('button')) closeHeaderMenu(true);
  });
  menu.onkeydown = event => {
    const rows = items(), index = rows.indexOf(document.activeElement);
    const next = {ArrowDown: (index + 1) % rows.length,
      ArrowUp: (index - 1 + rows.length) % rows.length, Home: 0, End: rows.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    rows[next]?.focus();
  };
  $('#header-more').addEventListener('focusout', event => {
    if (event.relatedTarget && !$('#header-more').contains(event.relatedTarget)) closeHeaderMenu();
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('#header-more')) closeHeaderMenu();
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.hidden === false) {
      event.preventDefault();
      event.stopImmediatePropagation();
      closeHeaderMenu(true);
    }
  }, true);
  addEventListener('resize', () => closeHeaderMenu());
  for (const media of [MOBILE, MEDIUM]) media.addEventListener('change', layoutHeader);
  // 视口、断点换挡、机器/来源筛选增减、会话计数变宽都会改变筛选条的内容宽度；
  // 观察的都是 flex: none 的组，重排只搬右侧按钮，不会改它们的尺寸而形成回环。
  // 新建按钮随终端能力出现/消失时由 term.js 直接调 layoutHeader()。
  const observer = new ResizeObserver(() => layoutHeader());
  for (const node of [$('header'), $('.brand'), ...$('.header-filters').children]) observer.observe(node);
  layoutHeader();
}

/** 子代理是否仍在跑：服务端按 transcript 与父会话的停止通知判断，父进程不在则一律不算。 */
const agentRunning = (uid, item) => !!item.active && S.live.has(uid);

/** 主会话/子代理下拉的行：主会话固定在前，子代理按结束时间倒序，还在跑的没有结束时间、排最前并带绿点。 */
function sessionViewRows(m) {
  const row = S.sessions.find(s => s.uid === m.uid);
  const items = [...((row || m).agent_items || [])];
  const running = a => agentRunning(m.uid, a);
  const when = value => Date.parse(value || '') || 0;
  items.sort((a, b) => (running(b) - running(a)) || (when(b.updated) - when(a.updated)));
  const mainTitle = m.parent_title || m.title;
  return `
      <button type="button" data-agent="" class="${m.agent_id ? '' : 'on'}" role="menuitem">
        <small><span class="view-kind">主会话</span></small><b>${esc(mainTitle)}</b>
      </button>
      ${items.map(a => `<button type="button" data-agent="${esc(a.id)}"
        class="${m.agent_id === a.id ? 'on' : ''}${running(a) ? ' running' : ''}" role="menuitem">
        <small><span class="view-kind">${running(a)
          ? '<i class="view-live" title="运行中" aria-label="运行中"></i>' : ''}子代理 · ${esc(a.type)}</span>
          <span class="view-span">${esc(fmtSpan(a.created, running(a) ? null : a.updated))}</span></small>
        <b>${esc(a.title)}</b>
      </button>`).join('')}`;
}

function head(m, total) {
  const h = el('div', 'dhead');
  const tmuxLive = S.liveTmux.has(m.uid);
  const hasAgents = (m.agent_items || []).length > 0;
  const titleView = hasAgents ? `
    <button class="session-view-switch" id="a-view-switch" type="button"
      title="切换主会话/子代理" aria-label="切换主会话/子代理" aria-expanded="false">
      <span>${esc(m.title)}</span><i>⌄</i>
    </button>` : `<span>${esc(m.title)}</span>`;
  const menuView = hasAgents ? `
    <div class="session-view-menu" id="session-view-menu" hidden role="menu">${sessionViewRows(m)}</div>` : '';
  h.innerHTML = `
    <div class="dtitle">
      <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
      <h2 class="${hasAgents ? 'has-session-views' : ''}">${sessionIconMarkup(m.source,
        S.live.has(m.uid), tmuxLive)}${titleView}</h2>
      ${menuView}
      <div class="dhead-actions" aria-label="会话操作">
        ${forkChainButtonMarkup(m)}
        ${consoleButtonMarkup()}
        ${sessionActionsMarkup(`
        ${starButtonMarkup(m.uid, !!m.starred, 'session-menu-action', 'a-star')}
        <button class="session-menu-action turn-mode${S.compactTurns ? '' : ' on'}" id="a-turns"
          title="${S.compactTurns ? '展开所有过程' : '折叠已完成过程'}"
          aria-label="${S.compactTurns ? '展开所有过程' : '折叠已完成过程'}"
          aria-pressed="${!S.compactTurns}">${uiIcon('process')}</button>
        <button class="session-menu-action" data-report-bug title="报告当前会话问题"
          aria-label="报告当前会话问题">${uiIcon('bug')}</button>
        ${S.term ? `<div class="session-menu-search"><b id="mcount">…</b>
          <button class="session-menu-action" id="m-prev" title="上一处" aria-label="上一处匹配">↑</button>
          <button class="session-menu-action" id="m-next" title="下一处" aria-label="下一处匹配">↓</button></div>` : ''}
        ${m.agent_id ? '' : '<button class="session-menu-action danger" id="a-session-action"></button>'}
        `, `
    <div class="dmeta">
      <span id="mcount-total">${total} 条消息</span>
      <span class="meta-secondary">${fmtSize(m.size)}</span>
      <span class="meta-secondary">${esc(fmtTime(m.created))} → ${esc(fmtTime(m.updated))}</span>
      ${m.node_name ? `<span class="meta-node node-badge" data-node-color="${nodeColor(m.node_name)}">${esc(m.node_name)}</span>` : ''}
      <span class="meta-secondary"><code>${esc(shortCwd(m.cwd || '(未知)', 999))}</code></span>
      <span class="meta-source">${esc(m.agent_type || SOURCES[m.source].name)}</span>
      ${continueMarkup(m)}
      ${spawnerMarkup(m)}
      ${m.model ? `<span class="meta-secondary">${esc(m.model)}</span>` : ''}
      <span class="meta-secondary session-id"><code>${esc(m.sid)}</code></span>
      ${m.branch ? `<span class="meta-secondary">⑂ ${esc(m.branch)}</span>` : ''}
    </div>`)}
      </div>
    </div>`;
  h.querySelector('.mobile-back').onclick = showMobileList;
  h.addEventListener('click', event => {
    const link = event.target.closest('.meta-spawner, .meta-continue');
    if (link) openSession(link.dataset.uid);
  });
  h.querySelector('#a-star').onclick = () => toggleSessionStar(m.uid);
  const turnMode = h.querySelector('#a-turns');
  turnMode.onclick = () => {
    S.compactTurns = !S.compactTurns;
    store.set('compactTurns', S.compactTurns);
    turnMode.classList.toggle('on', !S.compactTurns);
    turnMode.setAttribute('aria-pressed', String(!S.compactTurns));
    const label = S.compactTurns ? '展开所有过程' : '折叠已完成过程';
    turnMode.title = turnMode.ariaLabel = label;
    labelSessionAction(turnMode);
    document.querySelectorAll('#msgs > .turn-process').forEach(
      node => S.compactTurns ? node._fold?.() : node._open?.());
    refreshMessageTimeDividers();
    settle($('#msgs'));
  };
  const viewSwitch = h.querySelector('#a-view-switch');
  const viewMenu = h.querySelector('#session-view-menu');
  if (viewSwitch && viewMenu) {
    const close = () => {
      viewMenu.hidden = true;
      viewSwitch.setAttribute('aria-expanded', 'false');
    };
    viewSwitch.onclick = e => {
      e.stopPropagation();
      // 起止时间和运行点按打开时的列表数据重画；列表刷新不重建标题栏，
      // 否则正在看的菜单会被换掉。
      if (viewMenu.hidden) viewMenu.innerHTML = sessionViewRows(m);
      viewMenu.hidden = !viewMenu.hidden;
      viewSwitch.setAttribute('aria-expanded', String(!viewMenu.hidden));
      if (!viewMenu.hidden) setTimeout(() => document.addEventListener('click', close, { once: true }), 0);
    };
    viewMenu.onclick = e => {
      e.stopPropagation();
      const b = e.target.closest('button[data-agent]');
      if (!b) return;
      close();
      openSession(m.uid, b.dataset.agent || null);
    };
  }
  bindForkChainMenu(h, m);
  const tb = h.querySelector('#a-term');
  bindConsoleButton(tb, m.uid, m.agent_id);
  showConsoleToast('');
  if (typeof renderTakeoverBtn === 'function') setTimeout(renderTakeoverBtn, 0);
  renderSessionAction(m, h.querySelector('#a-session-action'));
  bindSessionActions(h);
  return h;
}

/** compact/continue 另起的 JSONL：两边标题栏互相给入口，左栏两行都可点。 */
function continueMarkup(m) {
  if (m.agent_id) return '';
  if (m.continued_in && S.sessions.some(s => s.uid === m.continued_in)) {
    return `<span class="meta-secondary"><button type="button" class="meta-continue" data-uid="${esc(m.continued_in)}"
      title="打开续写后的当前会话">已续写</button></span>`;
  }
  const prev = continuedPredecessor(m.uid);
  if (prev) {
    return `<span class="meta-secondary"><button type="button" class="meta-continue" data-uid="${esc(prev.uid)}"
      title="查看续写前的完整记录">续写前的记录</button></span>`;
  }
  return '';
}

/** 标题栏元信息里的发起者：由哪条会话把它派出来的，点击就跳过去。 */
function spawnerMarkup(m) {
  if (m.agent_id || !m.spawned_by) return '';
  const byKey = new Map(S.sessions.map(s => [spawnKey(s.node_id, s.source, s.sid), s]));
  const parent = spawnParentOf(m, byKey);
  if (!parent || parent.continued_in === m.uid) return '';
  return `<span class="meta-secondary"><button type="button" class="meta-spawner" data-uid="${esc(parent.uid)}"
    title="由「${esc(parent.title)}」发起，点击打开">↰ ${esc(SOURCES[parent.source].name)} · ${esc(parent.title)}</button></span>`;
}

/* ---------- 回退父会话链 ---------- */
// Codex 回退会生成子会话，原会话默认从左栏隐藏。子会话标题栏给一个图标，
// 下拉列出整条父会话链，每一级可单独显示到左栏或再次隐藏。
function forkChainButtonMarkup(m) {
  if (m.agent_id || !m.forked_from_id) return '';
  return `<button class="iconbtn" id="a-fork-chain" type="button" title="父会话链"
      aria-label="父会话链" aria-haspopup="menu" aria-expanded="false"
      aria-controls="fork-chain-menu">${uiIcon('fork')}</button>
    <div class="session-view-menu fork-chain-menu" id="fork-chain-menu" hidden role="menu"
      aria-label="父会话链"></div>`;
}

function closeForkChainMenu() {
  const menu = $('#fork-chain-menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  $('#a-fork-chain')?.setAttribute('aria-expanded', 'false');
}

function renderForkChainMenu() {
  const menu = $('#fork-chain-menu');
  if (!menu || menu.hidden) return;
  const current = S.sessions.find(session => session.uid === S.sel)
    || (S.results || []).find(session => session.uid === S.sel) || menu._meta;
  const chain = current ? forkAncestors(current) : [];
  menu.innerHTML = chain.map(({ sid, row }, i) => {
    const level = i === 0 ? '父会话' : `上 ${i + 1} 级父会话`;
    if (!row) return `<div class="chain-row gone" role="none">
        <span><small>${level} · 记录已不存在</small><b><code>${esc(sid)}</code></b></span>
      </div>`;
    const shown = !row.fork_parent || !!row.fork_parent_visible;
    const when = `${esc(fmtTime(row.created))} → ${esc(fmtTime(row.updated))}`;
    return `<div class="chain-row${shown ? ' shown' : ''}" role="none" data-uid="${esc(row.uid)}">
        <button type="button" class="chain-open" role="menuitem" title="打开这条会话">
          <small>${level} · ${when}${shown ? ' · 已在左栏' : ''}</small><b>${esc(row.title || row.sid)}</b>
        </button>
        ${row.fork_parent ? `<button type="button" class="btn chain-toggle" role="menuitem"
          data-visible="${shown ? 0 : 1}">${shown ? '隐藏' : '显示'}</button>` : ''}
      </div>`;
  }).join('') || '<div class="chain-row gone" role="none"><span><small>没有父会话</small></span></div>';
}

function bindForkChainMenu(heading, m) {
  const button = heading.querySelector('#a-fork-chain');
  const menu = heading.querySelector('#fork-chain-menu');
  if (!button || !menu) return;
  menu._meta = m;
  const open = () => {
    closeSessionActions();
    const views = heading.querySelector('#session-view-menu');
    if (views) views.hidden = true;
    heading.querySelector('#a-view-switch')?.setAttribute('aria-expanded', 'false');
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    renderForkChainMenu();
  };
  button.onclick = e => {
    e.stopPropagation();
    menu.hidden ? open() : closeForkChainMenu();
  };
  menu.onclick = async e => {
    e.stopPropagation();
    const row = e.target.closest('.chain-row[data-uid]');
    if (!row) return;
    const toggle = e.target.closest('.chain-toggle');
    if (toggle) {
      const visible = toggle.dataset.visible === '1';
      await setForkParentVisibility([row.dataset.uid], visible, toggle);
      if (visible) {
        // 新显示的会话在左栏滚到可见处，让“显示”有个看得见的结果
        $(`#side .item[data-uid="${CSS.escape(row.dataset.uid)}"]`)
          ?.scrollIntoView({ block: 'nearest' });
      }
      return;
    }
    if (e.target.closest('.chain-open')) {
      closeForkChainMenu();
      openSession(row.dataset.uid);
    }
  };
}
document.addEventListener('click', event => {
  if (!event.target.closest('#fork-chain-menu, #a-fork-chain')) closeForkChainMenu();
}, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && $('#fork-chain-menu')?.hidden === false) {
    event.preventDefault();
    event.stopImmediatePropagation();
    closeForkChainMenu();
    $('#a-fork-chain')?.focus();
  }
}, true);

function renderSessionAction(m, button = $('#a-session-action')) {
  if (!button || m.uid !== S.sel) return;
  if (m.fork_parent) {
    const shown = !!m.fork_parent_visible;
    const label = shown ? '隐藏父会话' : '显示父会话';
    button.innerHTML = uiIcon('eye-off');
    button.title = button.ariaLabel = label;
    labelSessionAction(button);
    button.onclick = () => setForkParentVisibility([m.uid], !shown, button);
    return;
  }
  const running = S.live.has(m.uid);
  const label = running ? '停止会话' : '删除会话';
  button.innerHTML = uiIcon(running ? 'power' : 'trash');
  button.title = button.ariaLabel = label;
  labelSessionAction(button);
  button.onclick = () => running ? stopSession(m, button) : del(m);
}

async function stopSession(m, button = null) {
  if (!confirm(`停止会话「${m.title}」?\n\n停止后才可以删除会话记录。`)) return;
  if (button) button.disabled = true;
  try {
    const r = await fetch(appUrl('api/session/stop'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uid: m.uid }),
    });
    const d = await r.json();
    if (!r.ok) return alert('停止失败: ' + (d.error || r.status));
    await refreshLive(true);
    if (typeof loadTermList === 'function') await loadTermList();
    paintLive();
  } finally {
    if (button) button.disabled = false;
  }
}

async function requestSessionDelete(uid) {
  const response = await fetch(appUrl('api/session/' + encodeURIComponent(uid)),
    { method: 'DELETE' });
  return { response, data: await response.json() };
}

async function del(m) {
  if (!confirm(`删除会话「${m.title}」?\n\n文件会移入回收站 ~/.local/share/agenthub/trash/, 不会真删。`)) return;
  closeWatch();                         // 先停 SSE，避免文件移走后 EventSource 自动重连 404
  const { response, data } = await requestSessionDelete(m.uid);
  if (!response.ok) {
    watchSession(m.uid);                // 删除失败，会话仍在，恢复实时同步
    return alert('删除失败: ' + (data.error || response.status));
  }
  S.sessions = S.sessions.filter(x => x.uid !== m.uid);
  if (S.results) S.results = S.results.filter(x => x.uid !== m.uid);
  S.sel = null;
  store.set('sel', null);
  renderChips(); renderSide();
  $('#detail').innerHTML = `<div class="empty">已移入回收站<br><code>${esc(data.trash)}</code>`
    + `<br><button type="button" class="btn" id="detail-open-trash">打开回收站</button></div>`;
  $('#detail-open-trash').onclick = openTrash;
  ensureConsolePlaceholder();
  auditDetailRendered('trashed');
  showMobileList();
}

// 连续工具调用/输出合并成一个可折叠的组；正在增长的时间线尾段保持展开，
// 等后面出现普通对话或任务结束后再自动封口。
const TOOL_ROLES = new Set(['tool', 'tool_result']);
const SEARCH_ROLES = new Set(['user', 'assistant', 'user·subagent', 'assistant·subagent',
                              'thinking', 'question', 'answer', 'command']);
const TURN_START_ROLES = new Set(['user', 'user·subagent']);
const GROUP_MIN = 2;
const MESSAGE_TIME_GAP_MS = 5 * 60 * 1000;
const MESSAGE_TIME_CADENCE_MS = 20 * 60 * 1000;

const isGroupableTool = m => TOOL_ROLES.has(m?.role) && !m.changes?.length;

/** 调用与其输出按 call_id 就近配对成一个视觉单元(对标 codex TUI 的 `$ 命令 + 输出`)。
 *  只在本批消息内配对；增量批里落单的输出保持原样渲染，不会丢。 */
function pairTools(msgs) {
  const out = [], open = new Map();
  for (const m of msgs) {
    if (m.role === 'tool') {
      const copy = { ...m };
      out.push(copy);
      if (m.call_id) open.set(m.call_id, copy);
      continue;
    }
    if (m.role === 'tool_result' && m.call_id && open.has(m.call_id)) {
      const owner = open.get(m.call_id);
      open.delete(m.call_id);
      owner.result = m;
      continue;
    }
    out.push(m);
  }
  return out;
}

/** 先算分组(纯计算, 很快), 再分批建 DOM —— 分批不会把一个组切成两半。 */
function planMessages(msgs, { openTail = false } = {}) {
  const plan = [];
  let run = [];
  const flush = open => {
    if (run.length >= GROUP_MIN) plan.push({ g: run, open: !!open });
    else for (const m of run) plan.push({ m });
    run = [];
  };
  for (const m of pairTools(msgs)) {
    // 调用与结果跨增量批次时，空结果配不到上面的调用。它仍参与消息计数，
    // 但不能凭空生成一块黑色空卡片。
    if (m.role === 'tool_result' && !String(m.text || '').trim()
        && !m.media?.length && !m.changes?.length) {
      flush(false);
      plan.push({ m: { ...m, silent: true } });
      continue;
    }
    // 文件修改本身是用户关心的工作记录，始终作为可见卡片留在时间线；
    // 普通工具协议继续按原规则合并折叠。
    if (isGroupableTool(m)) { run.push(m); continue; }
    flush(false);
    plan.push({ m });
  }
  flush(openTail);
  return plan;
}

const baseMessageRole = role => String(role || '').split('·', 1)[0];
const isTurnStart = m => TURN_START_ROLES.has(m?.role);
const sameNativeTurn = (a, b) => a?.turn_id != null && b?.turn_id != null
  && String(a.turn_id) === String(b.turn_id);
const isTurnAssistant = m => baseMessageRole(m?.role) === 'assistant';
const isFinalAssistant = m => isTurnAssistant(m)
  && ['final', 'final_answer', 'end_turn'].includes(m?.phase);
const isTaskTurnBoundary = m => m?.role === 'event' && m?.event_kind === 'task';
// rename/compact 等不计入消息数的辅助记录可能写在 final 之后；它们继续留在
// 时间线，但不应让前面的原生最终答复失去“结论”资格。
const isPassiveTurnTail = m => m?.counted === false;

/** 增量中断状态来自 activity 包，可能与最后一条 commentary 分批到达。 */
function markInterruptedTurn(messages, activity) {
  const turnId = activity?.state === 'aborted' && activity?.turn_id != null
    ? String(activity.turn_id) : '';
  if (!turnId) return false;
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const message = messages[i];
    if (String(message?.turn_id || '') !== turnId || !isTurnAssistant(message)) continue;
    if (!isFinalAssistant(message)) {
      message.interrupted = true;
      message.interrupt_reason = activity.reason || '本轮在最终答复前被中断';
      return true;
    }
    return false;
  }
  return false;
}

/** 找一轮中需要永久露出的结论块。原生 final 标记优先；明确中断的轮次
 *  保留最后一条 commentary 作为末次状态。老会话只有在回合已确定结束、
 *  且最后一个有效节点就是 assistant 时才回退到“最后一条”。 */
function turnConclusion(body, { complete = false, interrupted = false } = {}) {
  let meaningfulEnd = body.length;
  while (meaningfulEnd && isPassiveTurnTail(body[meaningfulEnd - 1])) meaningfulEnd--;
  if (!meaningfulEnd) return null;
  // Claude 的后台 task-notification 是一轮新的原生 user 输入，但在时间线里会
  // 转成不打扰主线的 task 事件。若它前面已有主助手 final，那条 final 已经结束
  // 了用户回合；后续监控短报不能反过来把它降成可折叠的“进展”。
  for (let boundary = 1; boundary < meaningfulEnd; boundary++) {
    if (!isTaskTurnBoundary(body[boundary])) continue;
    let end = boundary;
    while (end && isPassiveTurnTail(body[end - 1])) end--;
    if (!end || body[end - 1]?.role !== 'assistant'
        || !isFinalAssistant(body[end - 1])) continue;
    let start = end - 1;
    while (start > 0 && isFinalAssistant(body[start - 1])) start--;
    return {start, end};
  }
  const last = body[meaningfulEnd - 1];
  if (isFinalAssistant(last)) {
    let start = meaningfulEnd - 1;
    while (start > 0 && isFinalAssistant(body[start - 1])) start--;
    return {start, end: meaningfulEnd};
  }
  if (complete && interrupted) {
    for (let i = meaningfulEnd - 1; i >= 0; i--) {
      if (!isTurnAssistant(body[i]) || !body[i]?.interrupted) continue;
      return {start: i, end: i + 1, tailStart: meaningfulEnd,
              inferred: true, interrupted: true};
    }
  }
  if (complete && !interrupted && isTurnAssistant(last) && !last.phase) {
    return {start: meaningfulEnd - 1, end: meaningfulEnd, inferred: true};
  }
  return null;
}

function visiblePlanSize(plan) {
  return plan.reduce((n, item) => n + (item.m?.silent ? 0 : (item.m || item.g ? 1 : 0)), 0);
}

function turnKey(messages, conclusion) {
  const values = [...messages, ...(conclusion || [])];
  const native = values.find(m => m?.turn_id)?.turn_id;
  if (native) return native;
  const first = messages[0] || conclusion?.[0] || {};
  return `${first.ts || 'turn'}:${String(first.text || '').slice(0, 80)}`;
}

/** 单轮外层折叠。用户输入和最终结论仍是普通顶层气泡，中间过程才进入合集；
 *  合集内部继续复用 planMessages 的工具配对/分组规则。 */
function planTurnSegment(messages, promptEnd,
                         {complete = false, foldable = complete, openTail = false} = {}) {
  const prompts = messages.slice(0, promptEnd);
  const body = messages.slice(promptEnd);
  const conclusion = turnConclusion(body, {
    complete, interrupted: messages.some(m => m?.interrupted),
  });
  // 历史段可能因用户在同一次原生 turn 中追加要求，或上一轮被中断，而没有
  // 自己的 final。它已经被后续 user 明确封口，仍应作为过程折叠；只是不能
  // 把最后一条 commentary 猜成结论。尚在增长的尾段继续完整铺开。
  if (!conclusion && !foldable) return planMessages(messages, {openTail});
  // 中断时最后一条助手状态后面还可能有工具结果。它们仍属于过程；把这条
  // 状态提升到合集后作为“末次进展”，既不丢工具，也不把工具散回顶层。
  const process = conclusion?.interrupted
    ? [...body.slice(0, conclusion.start),
       ...body.slice(conclusion.end, conclusion.tailStart)]
    : conclusion ? body.slice(0, conclusion.start) : body;
  const processPlan = planMessages(process);
  // 一项换成一项不会节省空间，还会徒增一次点击。
  const processSize = visiblePlanSize(processPlan);
  // 中断轮已经要保留末次状态；即便只剩一个工具单元，也应进过程合集，
  // 否则恰好较短的中断轮会再次把工具卡散在对话主线里。
  if (!processSize || (processSize < 2 && !conclusion?.interrupted)) {
    return planMessages(messages);
  }
  const finalBlock = conclusion ? body.slice(conclusion.start, conclusion.end) : [];
  const passiveTail = conclusion
    ? body.slice(conclusion.tailStart ?? conclusion.end) : [];
  return [
    ...prompts.map((m, i) => ({m, sealedTurnHead: i === 0})),
    {turn: {items: process, plan: processPlan,
            key: turnKey(messages, finalBlock), hasConclusion: !!conclusion,
            inferred: !!conclusion?.inferred,
            interrupted: !!conclusion?.interrupted},
     open: !S.compactTurns},
    ...planMessages(finalBlock),
    ...planMessages(passiveTail),
  ];
}

function planTurn(messages, options = {}) {
  if (!messages.length || !isTurnStart(messages[0])) {
    return planMessages(messages, {openTail: options.openTail});
  }
  // Claude 会把同一次含文字/图片的 user 记录拆成多个规范化气泡。相邻且
  // turn_id 相同的部分是一份输入，全部留在顶层，不能把图片折进“过程”。
  let promptEnd = 1;
  while (promptEnd < messages.length && isTurnStart(messages[promptEnd])
         && sameNativeTurn(messages[0], messages[promptEnd])) promptEnd++;
  return planTurnSegment(messages, promptEnd, options);
}

/** 历史缺口两侧会分别调用，绝不跨缺口猜轮次。answer 是代理提问的回答，
 *  留在同一轮过程内；只有真正的 user/user·subagent 开新轮。 */
function planTurns(msgs, {
  openTail = false, tailComplete = false, foldTail = tailComplete,
} = {}) {
  const out = [];
  let start = msgs.findIndex(isTurnStart);
  // 窗口缺口可能截在一轮正中：缺口前的尾段可以折叠，但不能据此猜结论；
  // 缺口后的前缀若被下一条 user 封口，也按无输入的历史过程片段处理。
  if (start < 0) {
    return planTurnSegment(msgs, 0, {
      complete: tailComplete, foldable: foldTail, openTail,
    });
  }
  out.push(...planTurnSegment(msgs.slice(0, start), 0, {
    complete: true, foldable: true,
  }));
  while (start < msgs.length) {
    let next = start + 1;
    while (next < msgs.length && isTurnStart(msgs[next])
           && sameNativeTurn(msgs[start], msgs[next])) next++;
    while (next < msgs.length && !isTurnStart(msgs[next])) next++;
    const historical = next < msgs.length;
    out.push(...planTurn(msgs.slice(start, next), {
      complete: historical || tailComplete,
      foldable: historical || foldTail,
      openTail: !historical && openTail,
    }));
    start = next;
  }
  return out;
}

/** 一个工具卡可能同时包含调用和结果，工具组又包含多张卡。
 *  分隔线用这个视觉单元的最早/最晚时间，不会把一次长时间工具调用
 *  误判成与下一条消息的空档。 */
function messageTimeRange(messages) {
  const values = [];
  for (const message of messages || []) {
    for (const item of [message, message?.result]) {
      const value = Date.parse(item?.ts || '');
      if (Number.isFinite(value)) values.push(value);
    }
  }
  return values.length ? {start: Math.min(...values), end: Math.max(...values)} : null;
}

function stampMessageTime(node, messages) {
  if (!node?.matches('.msg')) return node;
  const range = messageTimeRange(messages);
  if (range) {
    node.dataset.timeStart = range.start;
    node.dataset.timeEnd = range.end;
  }
  return node;
}

function formatMessageDateTime(value) {
  const date = new Date(value);
  const pad = number => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + ` ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function messageTimeDivider(value) {
  const node = el('div', 'message-time-divider');
  const time = document.createElement('time');
  time.dateTime = new Date(value).toISOString();
  time.textContent = formatMessageDateTime(value);
  node.appendChild(time);
  return node;
}

/** 相邻气泡空档超过 5 分钟时显示时间；对连续的密集对话，也每超过
 *  20 分钟补一条。历史缺口会重新起算；任务事件和 Working 状态行只中断
 *  “相邻”判断，不中断 20 分钟周期；隐藏的协议消息不参与。 */
function refreshMessageTimeDividers(box = $('#msgs')) {
  if (!box) return;
  box.querySelectorAll(':scope > .message-time-divider').forEach(node => node.remove());
  let previousEnd = null;
  let lastShownAt = null;
  for (const node of [...box.children]) {
    if (node.matches('.silent-tool-result, .question-live-shadowed') || node.hidden) continue;
    if (!node.matches('.msg')) {
      previousEnd = null;
      if (node.matches('.history-gap')) lastShownAt = null;
      continue;
    }
    const start = Number(node.dataset.timeStart);
    const end = Number(node.dataset.timeEnd);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      previousEnd = null;
      lastShownAt = null;
      continue;
    }
    if (lastShownAt === null) lastShownAt = start;
    const afterGap = previousEnd !== null && start - previousEnd > MESSAGE_TIME_GAP_MS;
    const afterCadence = start - lastShownAt > MESSAGE_TIME_CADENCE_MS;
    if (afterGap || afterCadence) {
      box.insertBefore(messageTimeDivider(start), node);
      lastShownAt = start;
    }
    previousEnd = Math.max(start, end);
  }
}

function buildPlan(box, plan, before) {
  const built = [];
  for (let i = 0; i < plan.length; i++) {
    const p = plan[i];
    const n = p.gap ? historyGapNode(p.gap)
      : (p.turn ? turnProcessNode(p.turn, p.open)
        : (p.g ? groupNode(p.g, p.open) : msgNode(p.m)));
    // 整页渲染出来的已折叠回合已经封口。后续只带 activity 的 SSE 不应
    // 再把它拆掉重建；否则搜索时刚自动展开并高亮的懒加载正文会被替换掉。
    if (p.sealedTurnHead || (p.m && isTurnStart(p.m) && plan[i + 1]?.turn)) {
      n._turnSealed = true;
    }
    if (p.m?.turn_id != null) n.dataset.turnId = String(p.m.turn_id);
    stampMessageTime(n, p.turn?.items || p.g || (p.m ? [p.m] : []));
    before ? box.insertBefore(n, before) : box.appendChild(n);
    built.push(n);
  }
  return built;
}

function trailingToolNodes(box, before = null) {
  const nodes = [];
  let node = before ? before.previousElementSibling : box.lastElementChild;
  while (node && Array.isArray(node._toolItems)) {
    nodes.unshift(node);
    node = node.previousElementSibling;
  }
  return nodes;
}

function trailingGroupOpenState(trailing) {
  return {
    userOpened: trailing.some(node => node._userOpened),
    wasOpen: trailing.some(node => node.matches('.grp') && !node.classList.contains('folded')),
  };
}

function restoreGroupOpen(nodes, {userOpened = false, keepOpen = false} = {}) {
  for (const node of nodes) {
    if (!node.matches?.('.grp')) continue;
    if (userOpened) node._userOpened = true;
    if (keepOpen || userOpened) node._open?.();
  }
}

/** 增量批次可能把同一段工具输出切开。把现有尾段取回来一起规划，保证它们
 *  仍是一组；一旦本批出现普通消息，这个尾段立即变成已完成的折叠组。
 *  用户亲手展开过的组不能因为后续工具结果或封口而被拆掉重建成折叠态。 */
function appendMessages(box, msgs, before = null, { openTail = false } = {}) {
  let rest = [...msgs];
  const trailing = trailingToolNodes(box, before);
  let lead = 0;
  while (lead < rest.length && isGroupableTool(rest[lead])) lead++;
  const built = [];
  if (trailing.length) {
    const extra = rest.slice(0, lead);
    const combined = [...trailing.flatMap(node => node._toolItems), ...extra];
    const onlyTools = lead === rest.length;
    const {userOpened, wasOpen} = trailingGroupOpenState(trailing);
    const keepOpen = onlyTools && (openTail || wasOpen || userOpened);
    const planned = planMessages(combined, {openTail: keepOpen});
    const grp = trailing.length === 1 && trailing[0].matches('.grp') ? trailing[0] : null;
    if (grp && planned.length === 1 && planned[0].g) {
      syncGroupNode(grp, planned[0].g);
      stampMessageTime(grp, planned[0].g);
      if (keepOpen || userOpened) grp._open?.();
      else if (!onlyTools && !userOpened) grp._fold?.();
      if (userOpened) grp._userOpened = true;
      built.push(grp);
    } else {
      const anchor = before || trailing[trailing.length - 1].nextElementSibling;
      trailing.forEach(node => node.remove());
      const nodes = buildPlan(box, planned, anchor);
      restoreGroupOpen(nodes, {userOpened, keepOpen: keepOpen || userOpened});
      built.push(...nodes);
    }
    rest = rest.slice(lead);
  }
  built.push(...buildPlan(box, planMessages(rest, {openTail}), before));
  return built;
}

function sealToolTail(box) {
  const trailing = trailingToolNodes(box);
  if (!trailing.length) return [];
  if (trailing.length === 1 && trailing[0].matches('.grp')) {
    if (!trailing[0]._userOpened) trailing[0]._fold?.();
    return trailing;
  }
  const items = trailing.flatMap(node => node._toolItems);
  if (items.length < GROUP_MIN) return trailing;
  const {userOpened} = trailingGroupOpenState(trailing);
  const anchor = trailing[trailing.length - 1].nextElementSibling;
  trailing.forEach(node => node.remove());
  const nodes = buildPlan(box, planMessages(items), anchor);
  restoreGroupOpen(nodes, {userOpened, keepOpen: userOpened});
  return nodes;
}

function lastRawTurn(messages) {
  let start = -1;
  for (let i = 0; i < messages.length; i++) {
    if (!isTurnStart(messages[i])) continue;
    if (i > 0 && isTurnStart(messages[i - 1])
        && sameNativeTurn(messages[i - 1], messages[i])) continue;
    start = i;
  }
  return start < 0 ? [] : messages.slice(start);
}

function lastRenderedTurnStart(box) {
  const children = [...box.children];
  for (let i = children.length - 1; i >= 0; i--) {
    const node = children[i];
    if (node.classList?.contains('client-outbox')) continue;
    if (!node.matches?.('.msg') || !TURN_START_ROLES.has(node.dataset.role)) continue;
    // 同一原生 user 记录可能是相邻的“文字 + 图片”多个气泡；重建回合时
    // 从第一块开始移除，避免把文字留在旧 DOM、图片再复制一份。
    let head = node, j = i;
    const turnId = node.dataset.turnId;
    while (turnId && j > 0) {
      let k = j - 1;
      while (k >= 0 && children[k].classList?.contains('message-time-divider')) k--;
      const previous = children[k];
      if (!previous?.matches?.('.msg')
          || !TURN_START_ROLES.has(previous.dataset.role)
          || previous.dataset.turnId !== turnId) break;
      head = previous;
      j = k;
    }
    return head;
  }
  return null;
}

const isConversationTailNode = node => node?.id === 'activity'
  || node?.classList?.contains('client-outbox')
  || node?.classList?.contains('live-question');

/** 增量期间先按现有方式铺开活动回合；final/idle 到达后只重建最后一轮。
 *  用户正在上翻时延迟封口，避免阅读中的内容突然从脚下消失。 */
function sealTurnTail(box, entry, {defer = true} = {}) {
  if (!box || !entry?.msgs?.length) return false;
  const raw = lastRawTurn(entry.msgs);
  if (!raw.length) {
    if (entry.activity?.state !== 'working') sealToolTail(box);
    return false;
  }
  const state = entry.activity?.state;
  const tailComplete = (state && !['working', 'waiting'].includes(state))
    || (!entry.activity && !S.live.has(entry.meta?.uid));
  const plan = planTurn(raw, {complete: tailComplete, openTail: state === 'working'});
  if (!plan.some(item => item.turn)) {
    if (state !== 'working') sealToolTail(box);
    return false;
  }
  const start = lastRenderedTurnStart(box);
  if (!start || start._turnSealed) return false;
  if (defer && !_stick) {
    box._turnSealPending = true;
    return false;
  }
  let anchor = start;
  while (anchor && !isConversationTailNode(anchor)) anchor = anchor.nextElementSibling;
  for (let node = start; node && node !== anchor;) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
  const built = buildPlan(box, plan, anchor);
  const rebuiltStart = built.find(node => TURN_START_ROLES.has(node.dataset?.role));
  if (rebuiltStart) rebuiltStart._turnSealed = true;
  built.forEach(markMatches);
  if (S.term) updateMatchNav();
  box._turnSealPending = false;
  refreshMessageTimeDividers(box);
  settle(box);
  return true;
}

function flushPendingTurnSeal(box) {
  if (!box?._turnSealPending || box !== $('#msgs')) return;
  box._turnSealPending = false;
  const entry = cache.get(viewKey(S.sel, S.agent));
  if (!entry) return;
  // 离开底部期间可能完成了不止一轮；此时用户已经主动回到底部，整页按缓存
  // 重新规划可一次补齐所有轮次，并仍然停在最新结论。
  renderSession(entry.meta, entry.msgs, entry.activity, {startWatch: false});
}

// 折叠工具组只显示语义提纲，不显示角色/时间 header。
function addFoldPreview(n, text, aria) {
  const preview = el('div', 'fold-preview');
  const toggle = el('button', 'fold-toggle');
  toggle.type = 'button';
  toggle.title = `展开${aria}`;
  toggle.setAttribute('aria-label', toggle.title);
  toggle.setAttribute('aria-expanded', 'false');
  const peek = el('span', 'peek');
  peek.textContent = text;
  preview.append(toggle, peek);
  n.appendChild(preview);
  return preview;
}

function addAction(n) {
  const action = el('button', 'more disclosure');
  action.type = 'button';
  action.hidden = true;
  n.appendChild(action);
  return (label, fn, expanded = null) => {
    action.hidden = !label;
    action.textContent = label || '';
    action.onclick = fn || null;
    if (label) {
      action.title = label;
      action.setAttribute('aria-label', label);
    } else {
      action.removeAttribute('title');
      action.removeAttribute('aria-label');
    }
    if (expanded === null) action.removeAttribute('aria-expanded');
    else action.setAttribute('aria-expanded', String(expanded));
  };
}

// 输出预览: 前几行足够判断结果, 大段日志靠"展开全文"。
const OUT_LINES = 8;
const OUT_CHARS = 1600;
function outputStats(t) {
  const body = String(t || '').replace(/\n+$/, '');
  return { lines: body ? body.split('\n').length : 0, chars: String(t || '').length };
}

function outPreviewInfo(t) {
  const body = t.replace(/\n+$/, '');
  const lines = body ? body.split('\n') : [];
  let preview = lines.length > OUT_LINES ? lines.slice(0, OUT_LINES).join('\n') : t;
  const lineCut = lines.length > OUT_LINES;
  if (preview.length > OUT_CHARS) preview = preview.slice(0, OUT_CHARS);
  const truncated = preview !== t;
  return {
    text: truncated ? preview.replace(/\s+$/, '') + '\n…' : t,
    omittedLines: lineCut ? Math.max(0, outputStats(t).lines - OUT_LINES) : 0,
    omittedChars: truncated ? Math.max(0, t.length - preview.length) : 0,
  };
}

function toolOutputPath(m) {
  const name = String(m?.name || '').toLowerCase();
  if (!/(?:^|[_:./-])(?:read|read_file|notebookread|open)$/.test(name)) return '';
  const raw = String(m?.text || '');
  try {
    const value = JSON.parse(raw);
    for (const key of ['file_path', 'path', 'filename']) {
      if (typeof value?.[key] === 'string') return value[key];
    }
  } catch { /* 某些适配器传的是 Python repr 或纯命令，继续用文本提取 */ }
  const field = raw.match(/["'](?:file_path|path|filename)["']\s*[:=]\s*["']([^"']+)["']/);
  if (field) return field[1];
  const summary = String(m?.summary || '');
  const match = summary.match(/(?:^|\s)([.~\w/-]+\.[A-Za-z0-9]+)(?=\s|$|[,:;)]|$)/);
  return match?.[1] || '';
}

function clearSyntaxPaint(node) {
  delete node.dataset.syntaxDone;
  delete node.dataset.detected;
  delete node.dataset.codeLanguage;
  delete node.dataset.syntaxLanguages;
  node.classList.remove('hljs');
  for (const cls of [...node.classList]) if (cls.startsWith('language-')) node.classList.remove(cls);
}

function setToolOutput(pre, text) {
  clearSyntaxPaint(pre);
  pre.textContent = text;
  paintToolOutputDiff(pre);
  paintSyntax(pre);
}

function addClippedPre(entry, cls, text, codePath = '') {
  const pre = el('pre', cls);
  if (codePath) pre.dataset.codePath = codePath;
  const info = outPreviewInfo(text);
  setToolOutput(pre, info.text);
  entry.appendChild(pre);
  const actions = el('div', 'tool-out-actions');
  let wrap = null;
  if (String(text).split('\n').some(line => line.length > 160)) {
    wrap = el('button', 'tool-wrap', '自动换行');
    wrap.type = 'button';
    wrap.setAttribute('aria-pressed', 'false');
    wrap.onclick = () => {
      const on = pre.classList.toggle('wrap');
      wrap.classList.toggle('on', on);
      wrap.setAttribute('aria-pressed', String(on));
      wrap.textContent = on ? '保持原行' : '自动换行';
    };
    actions.appendChild(wrap);
  }
  if (info.text !== text) {
    const rest = info.omittedLines > 0
      ? `另有 ${info.omittedLines.toLocaleString()} 行`
      : `另有 ${info.omittedChars.toLocaleString()} 字符`;
    const expandLabel = `展开全文（${rest}）`;
    const more = el('button', 'more', expandLabel);
    let expanded = false;
    more.onclick = () => {
      expanded = !expanded;
      setToolOutput(pre, expanded ? text : info.text);
      more.textContent = expanded ? '收起' : expandLabel;
      more.setAttribute('aria-expanded', String(expanded));
    };
    more.setAttribute('aria-expanded', 'false');
    actions.appendChild(more);
  }
  if (actions.childElementCount) entry.appendChild(actions);
  return pre;
}

function toolEntry(m) {
  const entry = el('div', 'tool-entry');
  entry.dataset.role = m.role;
  if (m.counted === false) entry.dataset.counted = 'false';
  if (m.role !== 'tool') {
    // 落单的工具输出(没配到调用): 保持独立块
    addClippedPre(entry, 'tool-out' + (m.error ? ' err' : ''),
                  m.name ? `${m.name}\n${m.text}` : m.text, toolOutputPath(m));
    if (m.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(m.media));
    return entry;
  }
  // 摘要是可选文本；只有独立按钮切换参数，拖选/双击不触发折叠。
  const head = el('div', 'tool-head');
  head.innerHTML = `<code>${esc(m.summary || m.name || 'tool')}</code>`
    + (m.name && m.summary ? `<span class="tool-meta">${esc(m.name)}</span>` : '');
  const headCode = head.querySelector(':scope > code');
  const toggle = el('button', 'tool-toggle');
  toggle.type = 'button';
  head.appendChild(toggle);
  if (/^\s*(?:\$|❯)\s+/.test(m.summary || '')) headCode.classList.add('tool-command');
  paintSyntax(head);
  entry.appendChild(head);
  const args = el('pre', 'tool-args');
  args.hidden = !!m.summary;   // 识别不了的工具直接铺参数, 不藏
  const closeArgs = el('button', 'tool-args-close', '收起参数');
  closeArgs.type = 'button';
  closeArgs.hidden = args.hidden;
  let painted = false;
  const paintArgs = () => { if (!painted) { args.textContent = m.text; painted = true; } };
  const setArgsOpen = open => {
    if (open) paintArgs();
    args.hidden = !open;
    closeArgs.hidden = !open;
    entry.classList.toggle('args-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.title = toggle.ariaLabel = open ? '收起原始参数' : '展开原始参数';
  };
  if (!args.hidden) paintArgs();
  toggle.onclick = () => setArgsOpen(args.hidden);
  closeArgs.onclick = () => setArgsOpen(false);
  entry.appendChild(args);
  entry.appendChild(closeArgs);
  setArgsOpen(!args.hidden);
  if (m.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(m.media));
  appendToolResult(entry, m.result, m);
  return entry;
}

function appendToolResult(entry, r, m) {
  if (!entry || !r || entry.dataset.result === '1') return;
  if (r.counted !== false) entry.dataset.result = '1'; // 吸收的结果单独补入计数
  const status = [r.error ? '✗ 出错' : '✓ 完成'];
  if (Number.isInteger(r.exit_code)) status.push(`exit ${r.exit_code}`);
  if (Number.isFinite(+r.duration_s)) status.push(formatDuration(+r.duration_s * 1000));
  const stats = outputStats(r.text || '');
  status.push(stats.lines > 1 ? `${stats.lines.toLocaleString()} 行`
    : `${stats.chars.toLocaleString()} 字符`);
  entry.appendChild(el('div', 'tool-status' + (r.error ? ' err' : ''), status.join(' · ')));
  if (String(r.text || '').trim()) {
    addClippedPre(entry, 'tool-out' + (r.error ? ' err' : ''), r.text, toolOutputPath(m));
  }
  if (r.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(r.media));
}

const CHANGE_LABEL = {
  add: '新建', update: '修改', delete: '删除', edit: '修改', write: '写入',
};

function diffKind(line) {
  if (/^(diff --git |index |---|\+\+\+|\*\*\*)/.test(line)) return 'meta';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}

function diffCodePath(lines) {
  for (const prefix of ['+++ ', '--- ']) {
    const header = lines.find(line => line.startsWith(prefix));
    if (!header) continue;
    const path = header.slice(prefix.length).split('\t', 1)[0].trim().replace(/^(?:a|b)\//, '');
    if (path && path !== '/dev/null') return path;
  }
  return '';
}

function diffCodeParts(line, kind) {
  if (kind === 'add' || kind === 'del') return {marker: line[0], source: line.slice(1)};
  if (kind === 'ctx') return {marker: line.startsWith(' ') ? ' ' : '', source: line.startsWith(' ') ? line.slice(1) : line};
  return null;
}

/** 给普通工具输出里夹带的 unified/git diff 上色；前置命令状态仍按原样显示。 */
function paintToolOutputDiff(pre) {
  if (!(pre instanceof HTMLElement) || pre.querySelector(':scope > .tool-diff-line')) return;
  const lines = pre.textContent.split('\n');
  const first = lines.findIndex(line => line.startsWith('diff --git '));
  const unified = first >= 0 ? first : lines.findIndex((line, i) =>
    line.startsWith('--- ') && lines.slice(i + 1, i + 4).some(x => x.startsWith('+++ ')));
  if (unified < 0) return;
  const path = diffCodePath(lines.slice(unified));
  const fragment = document.createDocumentFragment();
  lines.forEach((line, i) => {
    const row = el('span', 'tool-diff-line');
    const kind = i >= unified ? diffKind(line) : '';
    if (kind) row.classList.add(kind);
    const parts = path && diffCodeParts(line, kind);
    if (parts) {
      row.classList.add('code');
      row.appendChild(el('i', '', parts.marker));
      const code = el('code', '', parts.source || ' ');
      code.dataset.codePath = path;
      row.appendChild(code);
    } else {
      row.textContent = line || ' ';
    }
    fragment.appendChild(row);
  });
  pre.replaceChildren(fragment);
}

function diffRows(lines, path) {
  return lines.map(line => {
    const kind = diffKind(line);
    const parts = diffCodeParts(line, kind);
    const attr = parts && path ? ` data-code-path="${esc(path)}"` : '';
    return `<div class="diff-line ${kind}"><i>${esc(parts?.marker ?? line[0] ?? ' ')}</i>`
      + `<code${attr}>${esc(parts?.source ?? line)}</code></div>`;
  }).join('');
}

function diffSides(change) {
  const before = [], after = [];
  for (const line of String(change.patch || '').split('\n')) {
    if (/^(---|\+\+\+|\*\*\*)/.test(line)) continue;
    if (line.startsWith('@@')) {
      before.push({ text: line, kind: 'meta' });
      after.push({ text: line, kind: 'meta' });
    } else if (line.startsWith('+')) {
      after.push({ text: line.slice(1), kind: 'add' });
    } else if (line.startsWith('-')) {
      before.push({ text: line.slice(1), kind: 'del' });
    } else {
      const text = line.startsWith(' ') ? line.slice(1) : line;
      before.push({ text, kind: 'ctx' });
      after.push({ text, kind: 'ctx' });
    }
  }
  return { before, after };
}

function sideRows(rows, unavailable, path) {
  if (unavailable) return '<div class="diff-unavailable">原内容没有记录，无法可靠还原</div>';
  return rows.map(row => {
    const attr = row.kind === 'meta' ? '' : ` data-code-path="${esc(path)}"`;
    return `<div class="diff-line ${row.kind}"><code${attr}>${esc(row.text)}</code></div>`;
  }).join('')
    || '<div class="diff-empty">（空文件）</div>';
}

function fileDiffMarkup(change, view) {
  const path = change.new_path || change.path || '';
  if (view === 'unified') {
    return `<div class="diff-unified">${diffRows(String(change.patch || '').split('\n'), path)}</div>`;
  }
  const sides = diffSides(change);
  return `<div class="diff-split">
    <section><b>修改前${change.before_complete ? '（完整）' : '（片段）'}</b>
      <div>${sideRows(sides.before, !change.before_available, change.path || path)}</div></section>
    <section><b>修改后${change.after_complete ? '（完整）' : '（片段）'}</b>
      <div>${sideRows(sides.after, !change.after_available, path)}</div></section>
  </div>`;
}

function paintInlineFileDiff(card, change, view) {
  card.dataset.diffView = view;
  for (const button of card.querySelectorAll('[data-diff-view]')) {
    const on = button.dataset.diffView === view;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
  }
  const body = card.querySelector('.file-change-body');
  body.innerHTML = fileDiffMarkup(change, view);
  paintSyntax(body);
}

function setInlineFileDiffWrap(card, on) {
  on = !!on;
  card.classList.toggle('diff-wrap', on);
  card.dataset.diffWrap = String(on);
  const button = card.querySelector('[data-diff-wrap]');
  if (!button) return;
  button.classList.toggle('on', on);
  button.setAttribute('aria-pressed', String(on));
  button.textContent = on ? '原行' : '换行';
  button.title = on ? '保持 diff 原始行宽' : '长行自动换行';
}

function fileChangeNode(m) {
  const n = el('div', 'msg file-change-msg');
  n.dataset.role = 'tool';
  if (m.result && m.result.counted !== false) n.dataset.result = '1'; // 修改确认输出并入卡片
  if (m.counted === false) n.dataset.counted = 'false';
  const body = el('div', 'file-change-list');
  for (const change of m.changes) {
    const card = el('section', 'file-change-card');
    const path = change.new_path ? `${change.path} → ${change.new_path}` : change.path;
    const complete = change.before_complete || change.after_complete;
    const scope = complete ? '包含可确定的完整文件内容' : '会话只记录了修改片段';
    card.innerHTML = `<div class="file-change-head"><b title="${esc(path)}">${esc(path)}</b>
      <span class="file-change-meta"><em title="${esc(scope)}">${esc(CHANGE_LABEL[change.operation] || '修改')} · ${complete ? '完整' : '片段'}</em>
      <i class="add">+${change.added || 0}</i><i class="del">−${change.removed || 0}</i>
      <span class="file-change-toolbar" role="group" aria-label="Diff 显示选项">
        <button type="button" data-diff-view="unified" aria-pressed="true">统一</button>
        <button type="button" data-diff-view="split" aria-pressed="false">并排</button>
        <button type="button" data-diff-wrap aria-pressed="false" title="长行自动换行">换行</button>
      </span></span></div>
      <div class="file-change-body"></div>`;
    card.querySelector('.file-change-toolbar').onclick = e => {
      const wrap = e.target.closest('button[data-diff-wrap]');
      if (wrap) {
        setInlineFileDiffWrap(card, !card.classList.contains('diff-wrap'));
        return;
      }
      const button = e.target.closest('[data-diff-view]');
      if (button) paintInlineFileDiff(card, change, button.dataset.diffView);
    };
    setInlineFileDiffWrap(card, false);
    paintInlineFileDiff(card, change, 'unified');
    body.appendChild(card);
  }
  n.appendChild(body);
  return n;
}

function turnProcessSummary(items) {
  const assistant = items.filter(isTurnAssistant).length;
  const thinking = items.filter(m => m.role === 'thinking').length;
  const calls = items.filter(m => m.role === 'tool').length;
  const orphanResults = calls ? 0 : items.filter(m => m.role === 'tool_result').length;
  const questions = items.filter(m => m.role === 'question').length;
  const changes = items.flatMap(m => Array.isArray(m.changes) ? m.changes : []);
  const paths = [...new Set(changes.map(change => change.path).filter(Boolean))];
  const added = changes.reduce((n, change) => n + (+change.added || 0), 0);
  const removed = changes.reduce((n, change) => n + (+change.removed || 0), 0);
  const errors = items.filter(m => m.error || (Number.isFinite(+m.exit_code) && +m.exit_code !== 0)).length;
  const times = items.flatMap(m => {
    const value = Date.parse(m.ts || '');
    return Number.isFinite(value) ? [value] : [];
  });
  const duration = times.length > 1 ? Math.max(...times) - Math.min(...times) : 0;
  const stats = [];
  if (assistant) stats.push(`${assistant} 条进展`);
  if (thinking) stats.push(`${thinking} 段思考`);
  if (calls || orphanResults) stats.push(`🔧 ${calls || orphanResults}`);
  if (paths.length) stats.push(`修改 ${paths.length} 个文件${added || removed ? ` +${added} −${removed}` : ''}`);
  if (questions) stats.push(`${questions} 次确认`);
  if (errors) stats.push(`⚠ ${errors}`);
  if (duration >= 1000) stats.push(formatDuration(duration));
  if (!stats.length) stats.push(`${items.length} 条记录`);
  return {stats, paths, errors};
}

/** 已完成回合的外层过程合集。折叠态只造摘要 DOM；第一次展开才渲染 Markdown、
 *  diff 和现有工具组，大会话既减少高度，也避免为不可见过程支付首屏成本。 */
function turnProcessNode(turn, initiallyOpen = false) {
  const items = turn.items || [];
  const summary = turnProcessSummary(items);
  const n = el('div', 'msg turn-process folded');
  n.dataset.role = 'process';
  // 这是多个原始消息的虚拟容器，不能让 DOM 计数把容器本身再算一条。
  n.dataset.counted = 'false';
  if (turn.key) n.dataset.turnId = turn.key;
  n._turnItems = items;
  const toolbar = el('div', 'turn-toolbar');
  n.appendChild(toolbar);
  const preview = addFoldPreview(toolbar, '', '本轮过程');
  preview.classList.add('turn-preview');
  const toggle = preview.querySelector('.fold-toggle');
  const peek = preview.querySelector('.peek');
  peek.classList.add('turn-peek');
  const label = el('b', 'turn-label', uiIcon('process'));
  const stats = el('span', 'turn-stats');
  summary.stats.forEach(value => stats.appendChild(el('span', '', value)));
  peek.replaceChildren(label, stats);
  const nav = el('div', 'turn-nav');
  const toStart = el('button', 'turn-nav-btn turn-to-start', '↑ 开头');
  const toConclusion = el('button', 'turn-nav-btn turn-to-conclusion', '结论 ↓');
  for (const button of [toStart, toConclusion]) button.type = 'button';
  toStart.title = toStart.ariaLabel = '回到本轮过程开头';
  if (turn.interrupted) {
    n.dataset.interrupted = 'true';
    toConclusion.textContent = '末次进展 ↓';
  }
  toConclusion.title = toConclusion.ariaLabel = turn.interrupted
    ? '跳到本轮中断前的末次进展' : '跳到本轮最终结论';
  toConclusion.hidden = turn.hasConclusion === false;
  nav.append(toStart, toConclusion);
  nav.hidden = true;
  toolbar.appendChild(nav);
  const expandTitle = summary.paths.length
    ? `展开过程\n修改文件：${summary.paths.join('\n')}` : '展开过程';
  if (summary.errors) n.classList.add('has-error');
  if (turn.inferred) n.dataset.inferred = 'true';
  const body = el('div', 'turn-process-body');
  body.hidden = true;
  n.appendChild(body);
  let materialized = false;
  const materialize = () => {
    if (materialized) return false;
    buildPlan(body, turn.plan || planMessages(items), null);
    refreshMessageTimeDividers(body);
    materialized = true;
    return true;
  };
  const fold = () => {
    n.classList.add('folded');
    body.hidden = true;
    nav.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', '展开本轮过程');
    toggle.title = expandTitle;
  };
  const open = () => {
    const built = materialize();
    n.classList.remove('folded');
    body.hidden = false;
    nav.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', '收起本轮过程');
    toggle.title = '收起过程';
    if (built && n.isConnected && S.term) {
      markMatches(body);
      updateMatchNav();
    }
  };
  n._fold = fold;
  n._open = open;
  const foldAtAnchor = () => mutateKeepingMessageAnchor(toolbar, fold);
  const openAtAnchor = () => mutateKeepingMessageAnchor(toolbar, open);
  n._foldAtAnchor = foldAtAnchor;
  n._openAtAnchor = openAtAnchor;
  toggle.onclick = () => n.classList.contains('folded') ? openAtAnchor() : foldAtAnchor();
  toStart.onclick = () => jumpWithinConversation(n, 'start');
  toConclusion.onclick = () => {
    let target = n.nextElementSibling;
    while (target && !target.matches?.('.msg')) target = target.nextElementSibling;
    jumpWithinConversation(target);
  };
  const found = items.some(m => SEARCH_ROLES.has(m.role) && hasTerm(m.text));
  if (found && S.autoOpen >= AUTO_OPEN_MAX) n.classList.add('hashit');
  if (initiallyOpen || (found && S.autoOpen < AUTO_OPEN_MAX)) open();
  else fold();
  return n;
}

function paintGroupPreview(peek, items) {
  const calls = items.filter(m => m.role === 'tool');
  const visible = calls.length ? calls : items;
  const heads = visible.slice(0, 3).map(m => m.summary || m.name || 'tool');
  const hasErr = items.some(m => m.result?.error || (m.role === 'tool_result' && m.error));
  const count = el('span', 'group-count', `🔧 ×${visible.length}${hasErr ? ' ⚠' : ''}`);
  const outline = el('span', 'group-outline');
  heads.forEach((head, i) => {
    const row = el('span');
    row.appendChild(el('i', 'group-index', `${i + 1}.`));
    row.append(' ');
    const code = el('code', /^\s*(?:\$|❯)\s+/.test(head) ? 'tool-command' : '');
    code.textContent = head;
    row.appendChild(code);
    outline.appendChild(row);
  });
  if (visible.length > heads.length) outline.appendChild(el('span', 'group-rest',
    `… 另有 ${visible.length - heads.length} 项`));
  peek.replaceChildren(count, outline);
  paintSyntax(outline);
}

/** 同一组继续增长时复用现有 DOM，避免拆掉重建把用户展开和内部“展开全文”冲掉。 */
function syncGroupNode(n, items) {
  const old = n._toolItems || [];
  const entries = [...n.querySelectorAll(':scope > .tool-entry')];
  const byId = new Map();
  old.forEach((m, i) => {
    if (!entries[i]) return;
    byId.set(m.call_id || `#${i}`, {m, entry: entries[i]});
  });
  const action = n.querySelector(':scope > .disclosure');
  const next = [];
  const reused = new Set();
  items.forEach((m, i) => {
    const prev = byId.get(m.call_id || `#${i}`);
    if (prev?.entry && prev.m.role === m.role && !reused.has(prev.entry)) {
      if (m.role === 'tool' && m.result && !prev.m.result) {
        appendToolResult(prev.entry, m.result, m);
      }
      reused.add(prev.entry);
      next.push(prev.entry);
    } else {
      next.push(toolEntry(m));
    }
  });
  entries.forEach(entry => { if (!reused.has(entry)) entry.remove(); });
  next.forEach(entry => n.insertBefore(entry, action));
  n._toolItems = items;
  const peek = n.querySelector(':scope > .fold-preview .group-peek');
  if (peek) paintGroupPreview(peek, items);
}

function groupNode(items, initiallyOpen = false) {
  // 工具协议不属于对话正文搜索范围。历史段默认折叠；正在增长的尾段展开。
  const n = el('div', 'msg grp' + (initiallyOpen ? '' : ' folded'));
  n.dataset.role = 'toolgroup';
  n._toolItems = items;
  const preview = addFoldPreview(n, '', '工具调用组');
  preview.classList.add('group-preview');
  const toggle = preview.querySelector('.fold-toggle');
  const peek = preview.querySelector('.peek');
  peek.classList.add('group-peek');
  paintGroupPreview(peek, items);
  items.forEach(m => n.appendChild(toolEntry(m))); // 直接铺在组内，不再套 grp-body + 内层 msg
  const setAction = addAction(n);
  const fold = () => {
    n.classList.add('folded');
    toggle.setAttribute('aria-expanded', 'false');
    setAction();
  };
  const open = () => {
    n.classList.remove('folded');
    toggle.setAttribute('aria-expanded', 'true');
    setAction('收起', () => {
      n._userOpened = false;
      fold();
    }, true);
  };
  n._fold = fold;
  n._open = open;
  toggle.onclick = () => {
    n._userOpened = true;
    open();
  };
  initiallyOpen ? open() : fold();
  return n;
}

function safeMediaSrc(src) {
  src = String(src || '');
  if (/^\/api\/media\/[0-9a-f]{32}$/.test(src)) return appUrl(src);
  if (HUB_MODE && /^\/api\/nodes\/[0-9a-f]{32}\/api\/media\/[0-9a-f]{32}$/.test(src)) return appUrl(src);
  if (!/^https?:\/\//i.test(src)) return '';
  try {
    const u = new URL(src);
    return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '';
  } catch { return ''; }
}

function imageHtml(m, inline = false) {
  const src = safeMediaSrc(m?.src);
  if (!src) return '';
  const alt = esc(m.alt || '图片');
  const w = Number.isFinite(+m.width) && +m.width > 0 ? ` width="${Math.round(+m.width)}"` : '';
  const h = Number.isFinite(+m.height) && +m.height > 0 ? ` height="${Math.round(+m.height)}"` : '';
  return `<a class="media-link${inline ? ' inline' : ''}" href="${esc(src)}" target="_blank" rel="noopener noreferrer">
    <img loading="lazy" decoding="async" referrerpolicy="no-referrer" src="${esc(src)}" alt="${alt}"${w}${h}>
    ${inline ? '' : `<span>${alt}</span>`}
  </a>`;
}

function mediaGallery(items) {
  const html = (items || []).filter(x => x.gallery || !x.ref).map(x => imageHtml(x)).filter(Boolean);
  return html.length ? `<div class="media-gallery">${html.join('')}</div>` : '';
}

function renderFormulae(root) {
  if (typeof renderMathInElement !== 'function') return;
  try {
    renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false },
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
      throwOnError: false,
      strict: 'ignore',
      trust: false,
    });
  } catch { /* 单个坏公式按原文保留，不能拖垮整条消息 */ }
}

/** KaTeX 延后加载；库就绪时补渲染首屏期间已经打开的消息。 */
function refreshFormulae() {
  for (const root of document.querySelectorAll('#msgs .mb')) {
    if (!root.querySelector('.katex')) renderFormulae(root);
  }
}

function msgNode(m) {
  if (m.silent) {
    const n = el('span', 'silent-tool-result');
    n.hidden = true;
    n.dataset.role = m.role;
    if (m.counted === false) n.dataset.counted = 'false';
    return n;
  }
  if (m.role === 'event') return eventNode(m);
  if (m.changes?.length) return fileChangeNode(m);
  if (m.role === 'question') return questionNode(m);
  if (TOOL_ROLES.has(m.role)) {
    // 单发工具调用与组内同款紧凑卡片: 摘要头 + 状态 + 输出预览
    const n = el('div', 'msg tool-msg');
    n.dataset.role = m.role;
    n._toolItems = [m];
    if (m.counted === false) n.dataset.counted = 'false';
    n.appendChild(toolEntry(m));
    return n;
  }
  // 命中的消息展开且不截断, 保证高亮可见; 但设上限, 否则搜 "a" 会把整个会话全量展开
  const found = SEARCH_ROLES.has(m.role) && hasTerm(m.text);
  const hit = found && S.autoOpen < AUTO_OPEN_MAX;
  if (hit) S.autoOpen++;
  // 对话内容从不整泡折叠；长内容只在泡内提供“展开全文”。
  const n = el('div', 'msg' + (found && !hit ? ' hashit' : ''));
  n.dataset.role = m.role;
  if (m.counted === false) n.dataset.counted = 'false';
  const body = el('div', 'mb');
  const linkContext = {uid: S.sel, agent: S.agent};
  const render = full => md(m.text, full, m.media, linkContext) + mediaGallery(m.media);
  const paint = full => {
    body.innerHTML = render(full); renderFormulae(body); paintSyntax(body);
  };
  paint(hit);
  n.appendChild(body);
  const setAction = addAction(n);
  const long = m.text.length > CLIP;
  const full = () => {
    body.classList.remove('clip'); paint(true);
    setAction(long ? '收起' : '', long ? clipped : null, long ? true : null);
  };
  const clipped = () => {
    body.classList.add('clip'); paint(false);
    setAction(`展开全文 (${m.text.length.toLocaleString()} 字符)`, full, false);
  };
  if (long) hit ? full() : clipped();
  if (m.interrupted) {
    n.classList.add('native-interrupted');
    const state = el('small', 'native-message-state', '已中断');
    if (m.interrupt_reason) state.title = m.interrupt_reason;
    n.appendChild(state);
  }
  return n;
}

function formatDuration(ms) {
  let seconds = Math.max(0, Number(ms) || 0) / 1000;
  if (seconds < 10) return `${seconds.toFixed(seconds < 1 ? 1 : 0)} 秒`;
  seconds = Math.round(seconds);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return [hours ? `${hours} 小时` : '', minutes ? `${minutes} 分` : '',
          rest || (!hours && !minutes) ? `${rest} 秒` : ''].filter(Boolean).join(' ');
}

function eventNode(m) {
  const kind = m.event_kind || 'session';
  const n = el('div', `timeline-event ${kind}${m.event_status ? ` ${m.event_status}` : ''}`);
  n.dataset.role = 'event';
  n.dataset.counted = 'false';
  if (kind === 'duration') {
    n.innerHTML = `<span>耗时 ${esc(formatDuration(m.duration_ms))}</span>`;
    return n;
  }
  const label = kind === 'recap' ? '回顾'
    : (kind === 'task' ? '任务' : (kind === 'compact' ? '上下文' : '会话'));
  n.innerHTML = `<b>${label}</b><span>${esc(m.text || '')}</span>`;
  if (m.details) {
    n.classList.add('has-details');
    const disclosure = el('details', 'event-details');
    const stats = outputStats(m.details);
    disclosure.appendChild(el('summary', '', `查看结果 · ${stats.lines.toLocaleString()} 行`));
    disclosure.ontoggle = () => {
      if (!disclosure.open || disclosure.querySelector('.event-detail-body')) return;
      const body = el('div', 'event-detail-body');
      body.innerHTML = md(m.details, true);
      renderFormulae(body); paintSyntax(body);
      disclosure.appendChild(body);
    };
    n.appendChild(disclosure);
  }
  return n;
}

function questionNode(m) {
  const n = el('div', 'msg question');
  n.dataset.role = 'question';
  if (m.counted === false) n.dataset.counted = 'false';
  if (m.call_id) n.dataset.callId = m.call_id;
  const live = !!m.live;
  if (live) n.classList.add('live-question');
  const promptState = m.state || 'waiting';
  if (live && promptState !== 'waiting') n.classList.add('settling');
  const body = el('div', 'mb question-body');
  const rows = Array.isArray(m.questions) && m.questions.length
    ? m.questions : [{ question: m.text, options: [] }];
  body.innerHTML = rows.map((q, i) => `
    <section class="question-item">
      ${q.header ? `<div class="question-header">${esc(q.header)}</div>` : ''}
      <div class="question-text">${esc(q.question || m.text)}</div>
      ${q.multiple ? '<div class="question-multiple">可多选</div>' : ''}
      ${(q.options || []).length ? `<div class="question-options">${q.options.map((o, j) => `
        <${live ? 'button' : 'div'} ${live ? `type="button" data-question-index="${i}" data-question-option="${j}" aria-pressed="false"` : ''}
          class="question-option"><span>${j + 1}</span><div><b>${esc(o.label)}</b>
          ${o.description ? `<small>${esc(o.description)}</small>` : ''}</div></${live ? 'button' : 'div'}>`).join('')}</div>` : ''}
    </section>`).join('');
  if (live) {
    const cli = agenthubCli(m.uid);
    const cliName = cli?.name || 'CLI';
    const waiting = promptState === 'waiting';
    const direct = waiting && rows.length === 1 && !rows[0].multiple
      && !!rows[0].options?.length;
    const formDirect = waiting && cli?.canAnswerQuestionForm({...m, questions: rows});
    const draftKey = formDirect && m.uid && m.call_id ? `${m.uid}\0${m.call_id}` : '';
    let selections = draftKey ? questionFormDrafts.get(draftKey) : null;
    const validDraft = Array.isArray(selections) && selections.length === rows.length
      && selections.every((optionIndex, questionIndex) => optionIndex === null
        || (Number.isInteger(optionIndex) && !!rows[questionIndex]?.options?.[optionIndex]));
    if (!validDraft) {
      selections = Array(rows.length).fill(null);
      if (draftKey) questionFormDrafts.set(draftKey, selections);
    }
    let formSubmit = null;
    body.querySelectorAll('[data-question-option]').forEach(button => {
      button.disabled = !direct && !formDirect;
      const questionIndex = +button.dataset.questionIndex;
      const optionIndex = +button.dataset.questionOption;
      const selected = formDirect && selections[questionIndex] === optionIndex;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.onclick = async () => {
        if (formDirect) {
          selections[questionIndex] = optionIndex;
          body.querySelectorAll(`[data-question-index="${questionIndex}"]`).forEach(x => {
            const selected = +x.dataset.questionOption === optionIndex;
            x.classList.toggle('selected', selected);
            x.setAttribute('aria-pressed', String(selected));
          });
          if (formSubmit) formSubmit.disabled = selections.some(x => x === null);
          return;
        }
        body.querySelectorAll('button').forEach(x => { x.disabled = true; });
        button.classList.add('submitting');
        const ok = await answerCliQuestion(m.uid, optionIndex);
        if (!ok) body.querySelectorAll('button').forEach(x => { x.disabled = false; });
      };
    });
    const actions = el('div', 'question-actions');
    if (!waiting) {
      actions.appendChild(el('small', 'question-settling',
        promptState === 'cancelled' ? `正在取消，等待 ${cliName} 记录…`
          : `答案已提交，等待 ${cliName} 记录…`));
    } else if (formDirect) {
      actions.appendChild(el('small', '', '请为每题选择一个答案'));
      formSubmit = el('button', 'question-submit', '提交答案');
      formSubmit.type = 'button';
      formSubmit.disabled = selections.some(x => x === null);
      formSubmit.onclick = async () => {
        body.querySelectorAll('button').forEach(x => { x.disabled = true; });
        formSubmit.classList.add('submitting');
        const ok = await answerCliQuestionForm(m.uid, selections);
        if (!ok) {
          body.querySelectorAll('[data-question-option]').forEach(
            x => { x.disabled = false; });
          formSubmit.disabled = selections.some(x => x === null);
          terminal.disabled = false;
          cancel.disabled = false;
        }
      };
      actions.appendChild(formSubmit);
    } else if (!direct) {
      actions.appendChild(el('small', '', '多选或多题请在原生终端回答'));
    }
    const terminal = el('button', '', '打开终端');
    terminal.type = 'button';
    terminal.onclick = () => revealNativeTerminal(m.uid);
    const cancel = el('button', 'question-cancel', '取消');
    cancel.type = 'button';
    cancel.disabled = !waiting;
    cancel.onclick = () => cancelCliQuestion(m.uid);
    actions.append(terminal, cancel);
    body.appendChild(actions);
  }
  n.appendChild(body);
  return n;
}

function renderActivity(activity) {
  const box = $('#msgs');
  if (!box) return;
  $('#activity')?.remove();
  if (!activity || activity.state === 'idle') return;
  if (activity.state === 'working') {
    if (!S.live.has(S.sel)) return;
    const processStart = S.liveStarted.get(S.sel);
    const activityAt = Date.parse(activity.ts) / 1000;
    // resume 出来的新 CLI 停在输入提示符时，旧 transcript 可能仍以一条未回答的
    // user 消息结尾。那条 working 属于上一进程，不能带进当前进程。
    if (Number.isFinite(processStart) && Number.isFinite(activityAt)
        && activityAt < processStart - 2) return;
  }
  const labels = {
    working: 'Working…', waiting: '等待回答',
    aborted: '已中断', failed: '执行失败',
  };
  const label = labels[activity.state];
  if (!label) return;
  const n = el('div', `activity ${activity.state}`);
  n.id = 'activity';
  n.dataset.state = activity.state;
  n.setAttribute('role', 'status');
  n.setAttribute('aria-live', 'polite');
  n.innerHTML = `<i></i><span>${label}</span>`;
  if (activity.reason) n.title = activity.reason;
  box.appendChild(n);
}

function renderQueuedMessages(uid = S.sel) {
  const box = $('#msgs');
  if (!box || S.agent || uid !== S.sel) return;
  for (const item of queuedMessages(uid)) {
    const cli = agenthubCli(uid);
    const queuedMessage = {role: 'user', text: item.text, media: item.media,
      counted: false, ts: item.created_iso || item.created_at || item.ts};
    const node = stampMessageTime(msgNode(queuedMessage), [queuedMessage]);
    const interrupted = ['aborted', 'restored'].includes(item.state);
    node.classList.add('client-outbox',
      interrupted ? 'client-aborted' : 'client-pending');
    node.classList.toggle('failed', item.state === 'failed');
    node.dataset.queuedId = item.id;
    const footer = el('div', 'client-pending-footer');
    footer.appendChild(el('small', 'client-pending-state',
      cli?.queuedMessageLabel(item) || '排队中'));
    const codexNeedsInspection = item.server && cli?.source === 'codex'
      && (item.state === 'confirming'
          || (item.state === 'failed' && +item.attempts > 0));
    if (item.server && !interrupted
        && (cli?.source === 'claude' || codexNeedsInspection)) {
      const actions = el('span', 'client-pending-actions');
      const inspect = el('button', '', '检查终端');
      inspect.type = 'button';
      inspect.title = `消息可能已经被 ${cli.name} 接收；打开终端核对，不会重复发送`;
      inspect.onclick = () => globalThis.revealNativeTerminal?.(uid);
      actions.append(inspect);
      if (item.state === 'failed' || item.state === 'confirming') {
        // 回执只说明粘贴和回车已到达终端；移除它不会重发。终端把它并进
        // 草稿后原生记录永远不会出现，用户必须能自己收掉这条回执。
        const discard = el('button', '', '移除');
        discard.type = 'button';
        discard.title = '只移除这条回执，不会重新发送';
        discard.onclick = () => discardServerQueuedMessage(uid, item.id);
        actions.append(discard);
      }
      footer.appendChild(actions);
    } else if ((item.server
                && ['injecting', 'failed', 'aborted', 'restored'].includes(item.state))
               || (!item.server && item.state === 'failed')) {
      const actions = el('span', 'client-pending-actions');
      if (item.state === 'failed') {
        const retry = el('button', '', '重试');
        retry.type = 'button';
        retry.title = '仅在终端确实没有收到时重试，避免重复发送';
        retry.onclick = () => item.server
          ? retryServerQueuedMessage(uid, item.id)
          : retryClientQueuedMessage(uid, item.id);
        actions.append(retry);
      }
      const discard = el('button', '', item.state === 'injecting' ? '撤销' : '移除');
      discard.type = 'button';
      discard.onclick = () => item.server
        ? discardServerQueuedMessage(uid, item.id)
        : discardQueuedUserMessage(uid, item.id);
      actions.append(discard);
      footer.appendChild(actions);
    }
    if (item.error) node.title = item.error;
    node.appendChild(footer);
    box.appendChild(node);
  }
}

/** Activity 和提交回执都是时间线尾部状态；每次重画都固定保持回执在最下方。 */
function pendingHistoryQuestion(entry) {
  if (entry?.meta?.source !== 'codex' || entry?.activity?.state !== 'waiting') return null;
  const answered = new Set((entry.msgs || [])
    .filter(m => ['answer', 'tool_result'].includes(m.role) && m.call_id)
    .map(m => m.call_id));
  return [...(entry.msgs || [])].reverse().find(
    m => m.role === 'question' && m.call_id && !answered.has(m.call_id)) || null;
}

function pruneQuestionFormDrafts(uid, activeId = '') {
  const prefix = `${uid}\0`;
  const keep = activeId ? `${prefix}${activeId}` : '';
  for (const key of questionFormDrafts.keys()) {
    if (key.startsWith(prefix) && key !== keep) questionFormDrafts.delete(key);
  }
}

function renderConversationTail(activity, uid = S.sel) {
  const box = $('#msgs');
  if (!box) return;
  const entry = cache.get(viewKey(uid));
  // SSE 与主动补读从同一旧游标出发时，携带正式 user 的那一批可能因游标
  // 冲突被丢弃；随后正文 reset 已把它放进完整缓存，但队尾重画过去只看
  // 增量，乐观副本便会永久残留。Claude 有本地副本时，每次画队尾都用
  // 已接受的完整缓存兜底对账一次。通常只有一条、几千项，且仅发送期间执行。
  if (agenthubCli(uid)?.source === 'claude'
      && queuedMessages(uid).length && entry?.msgs?.length) {
    reconcileQueuedMessages(uid, entry.msgs);
  }
  $('#activity')?.remove();
  box.querySelectorAll('.live-question').forEach(node => node.remove());
  box.querySelectorAll('.question-live-shadowed').forEach(
    node => node.classList.remove('question-live-shadowed'));
  box.querySelectorAll('.client-outbox').forEach(node => node.remove());
  const prompt = entry?.prompt;
  const nativeQuestion = prompt?.questions?.length ? null : pendingHistoryQuestion(entry);
  const activeQuestion = prompt?.questions?.length ? prompt : nativeQuestion;
  const activeDraftId = activeQuestion
    && (activeQuestion.state || 'waiting') === 'waiting'
    ? String(activeQuestion.id || activeQuestion.call_id || '') : '';
  pruneQuestionFormDrafts(uid, activeDraftId);
  if (prompt?.questions?.length) {
    if (prompt.id) {
      [...box.querySelectorAll('.msg[data-role="question"][data-call-id]')]
        .find(node => node.dataset.callId === prompt.id)
        ?.classList.add('question-live-shadowed');
    }
    const liveQuestion = {
      role: 'question', call_id: prompt.id, questions: prompt.questions,
      text: prompt.questions.map(q => q.question).join('\n\n'), live: true,
      state: prompt.state, uid, ts: prompt.ts || prompt.created_at,
    };
    box.appendChild(stampMessageTime(questionNode(liveQuestion), [liveQuestion]));
  } else if (nativeQuestion) {
    [...box.querySelectorAll('.msg[data-role="question"][data-call-id]')]
      .find(node => node.dataset.callId === nativeQuestion.call_id)
      ?.classList.add('question-live-shadowed');
    const liveQuestion = { ...nativeQuestion, live: true, uid };
    box.appendChild(stampMessageTime(questionNode(liveQuestion), [liveQuestion]));
  } else {
    renderActivity(activity);
  }
  renderQueuedMessages(uid);
  refreshMessageTimeDividers(box);
  scheduleBrowserSnapshot('conversation-tail');
}

const CLIP = 4000;
const AUTO_OPEN_MAX = 40;    // 最多自动展开这么多条命中消息, 其余只标记
const MARK_MAX = 3000;       // 单页高亮节点上限
const clipText = t => t.length > CLIP ? t.slice(0, CLIP) + '\n… (点下方按钮展开全文)' : t;

let syntaxLoading = false;
function ensureSyntax() {
  if (syntaxLoading || window.agenthubHighlight) return;
  syntaxLoading = true;
  const script = document.createElement('script');
  script.type = 'module';
  script.src = appUrl('syntax.js');
  script.onerror = () => { syntaxLoading = false; script.remove(); };
  document.head.appendChild(script);
}

function paintSyntax(root = document) {
  const select = selector => [
    ...(root.matches?.(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ];
  const blocks = select('code.code-block:not([data-syntax-done])');
  const summaries = select('code.tool-command:not([data-syntax-done])');
  const tools = select('pre.tool-out:not([data-syntax-done])').filter(
    pre => !pre.querySelector(':scope > .tool-diff-line'));
  const diffLines = select(
    '.diff-line > code[data-code-path]:not([data-syntax-done]), '
    + '.tool-diff-line > code[data-code-path]:not([data-syntax-done])');
  const nodes = [...new Set([...blocks, ...summaries, ...tools, ...diffLines])];
  if (!nodes.length) return;
  if (!window.agenthubHighlight) { ensureSyntax(); return; }
  for (const code of nodes) {
    code.dataset.syntaxDone = '1';
    const result = code.matches('code.tool-command') && window.agenthubHighlightShellCommand
      ? window.agenthubHighlightShellCommand(code.textContent)
      : (code.matches('pre.tool-out') && window.agenthubHighlightSegments
          ? window.agenthubHighlightSegments(code.textContent, code.dataset.codePath || '')
          : window.agenthubHighlight(code.textContent, code.dataset.codeLang || '', code.dataset.codePath || ''));
    if (!result?.html) continue;
    code.innerHTML = result.html;
    code.classList.add('hljs');
    if (result.language) code.classList.add(`language-${result.language}`);
    if (result.languages?.length) code.dataset.syntaxLanguages = result.languages.join(',');
    if (result.detected) code.dataset.detected = result.language;
    const host = code.tagName === 'PRE' ? code
      : (code.classList.contains('code-block') && code.parentElement?.tagName === 'PRE'
          ? code.parentElement : null);
    if (result.language && host) {
      host.dataset.codeLanguage = result.language;
    }
  }
}

addEventListener('agenthub-highlight-ready', () => paintSyntax(document));

// 轻量 markdown: 代码块 / 表格 / 列表 / 引用 / 标题 / 行内标记
function md(text, full, media = [], context = {}) {
  const source = full ? text : clipText(text);
  const lines = source.split('\n');
  const output = [], prose = [];
  const flush = () => {
    if (!prose.length) return;
    output.push(blocks(prose.join('\n'), media, context));
    prose.length = 0;
  };
  for (let i = 0; i < lines.length;) {
    // 只有独立行上的 Markdown 围栏才是代码块。旧的 split(/```/)
    // 会把句子里提到的 ```python 也当成开头，后半条消息全吞进 pre。
    const open = lines[i].match(/^\s{0,3}(`{3,}|~{3,})[^\S\n]*(.*)$/);
    if (!open || (open[1][0] === '`' && open[2].includes('`'))) {
      prose.push(lines[i++]);
      continue;
    }
    const marker = open[1][0], width = open[1].length;
    let close = i + 1;
    for (; close < lines.length; close++) {
      const found = lines[close].match(/^\s{0,3}(`+|~+)\s*$/);
      if (found && found[1][0] === marker && found[1].length >= width) break;
    }
    // 未闭合围栏只在“展开全文”预览恰好截断时按代码处理；
    // 原文本本身未闭合时保留原样，不再把整条消息误判为代码。
    if (close === lines.length && (full || text.length <= CLIP)) {
      prose.push(lines[i++]);
      continue;
    }
    flush();
    const info = open[2].trim();
    const language = info.match(/^[\w+.-]+/)?.[0] || '';
    const code = lines.slice(i + 1, close).join('\n');
    output.push(`<pre><code class="code-block" data-code-lang="${esc(language)}">${esc(code)}</code></pre>`);
    i = close < lines.length ? close + 1 : close;
  }
  flush();
  return output.join('') || '<p></p>';
}

const RE_LIST = /^\s*([-*+]|\d+[.)])\s+/;
const RE_HEAD = /^\s*(#{1,6})\s+(.*)$/;
const RE_QUOTE = /^\s*>\s?/;
const RE_HR = /^\s*([-*_])\s*(\1\s*){2,}$/;
// 表格分隔行: |---|:--:|  至少一个竖线和一串短横
const isSep = s => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(s) && s.includes('-');
const cells = s => s.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
const isTable = (ls, i) => ls[i].includes('|') && i + 1 < ls.length && isSep(ls[i + 1]);

function blocks(src, media = [], context = {}) {
  const ls = src.split('\n');
  let out = '', i = 0;
  while (i < ls.length) {
    const line = ls[i];
    if (!line.trim()) { i++; continue; }

    if (isTable(ls, i)) {
      const head = cells(line);
      const align = cells(ls[i + 1]).map(c =>
        /^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : 'left');
      const at = j => `style="text-align:${align[j] || 'left'}"`;
      i += 2;
      const rows = [];
      while (i < ls.length && ls[i].trim() && ls[i].includes('|')) rows.push(cells(ls[i++]));
      out += `<div class="tw"><table><thead><tr>${head.map((c, j) => `<th ${at(j)}>${inline(c, media, context)}</th>`).join('')}</tr></thead>`
        + `<tbody>${rows.map(r => `<tr>${r.map((c, j) => `<td ${at(j)}>${inline(c, media, context)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      continue;
    }

    const h = line.match(RE_HEAD);
    if (h) { out += `<h3 class="h${h[1].length}">${inline(h[2], media, context)}</h3>`; i++; continue; }

    if (RE_HR.test(line)) { out += '<hr>'; i++; continue; }

    if (RE_LIST.test(line)) {
      const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
      const items = [];
      while (i < ls.length && RE_LIST.test(ls[i])) {
        let item = ls[i++].replace(RE_LIST, '');
        while (i < ls.length && ls[i].trim() && !RE_LIST.test(ls[i]) && /^\s{2,}/.test(ls[i])) {
          item += '\n' + ls[i++].trim();     // 续行并入当前条目
        }
        items.push(`<li>${inline(item, media, context)}</li>`);
      }
      out += `<${tag}>${items.join('')}</${tag}>`;
      continue;
    }

    if (RE_QUOTE.test(line)) {
      const qs = [];
      while (i < ls.length && RE_QUOTE.test(ls[i])) qs.push(ls[i++].replace(RE_QUOTE, ''));
      out += `<blockquote>${inline(qs.join('\n'), media, context)}</blockquote>`;
      continue;
    }

    const para = [];
    while (i < ls.length && ls[i].trim() && !RE_LIST.test(ls[i]) && !RE_HEAD.test(ls[i])
           && !RE_QUOTE.test(ls[i]) && !RE_HR.test(ls[i]) && !isTable(ls, i)) {
      para.push(ls[i++]);
    }
    if (para.length) out += `<p>${inline(para.join('\n'), media, context)}</p>`;
    else i++;                                 // 兜底: 保证 i 一定前进
  }
  return out;
}

const RE_MD_IMAGE = /!\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+["'][^"']*["'])?\s*\)/g;
const RE_CODE_SPAN = /(^|[^`])(`+)(?!`)([^\n]*?)(?<!`)\2(?!`)/g;

const RE_REFERENCE = /(?<![A-Za-z0-9_@/:.-])(?:(?:https?:\/\/|www\.)[^\s<>"'`\u0000，。；、！？]+|(?:~\/|\.\.?\/|\/|[A-Za-z0-9_.-]+\/)[^\s<>"'`\u0000，。；、！？()[\]{}]+|[A-Za-z0-9_-][A-Za-z0-9_.-]*\.[A-Za-z][A-Za-z0-9_-]*(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)?)/gi;

function trimReference(raw) {
  let ref = raw.replace(/[.,;:!?]+$/, '');
  for (const [left, right] of [['(', ')'], ['[', ']']]) {
    while (ref.endsWith(right) && ref.split(right).length > ref.split(left).length) ref = ref.slice(0, -1);
  }
  return ref;
}

function referenceLink(ref, label, context, explicit = false) {
  let href = '';
  if (/^(https?:\/\/|www\.)/i.test(ref)) {
    try {
      const url = new URL(/^www\./i.test(ref) ? 'https://' + ref : ref);
      if (!['http:', 'https:'].includes(url.protocol)) return '';
      href = url.href;
    } catch { return ''; }
  } else {
    // Browser file:// navigation cannot reach a remote node. Resolve only
    // references present in this session, through its authenticated API.
    const withoutLine = ref.replace(/(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)$/, '');
    if (!context.uid || /^[a-z][a-z0-9+.-]*:/i.test(withoutLine) || ref.startsWith('//')) return '';
    if (/^[A-Z0-9]+(?:\/[A-Z0-9]+)+$/.test(ref)) return '';
    // Rendering is lexical only. Resolve history, existence and ambiguity on
    // explicit navigation/menu actions, never once per render or SSE update.
    const filename = /^[^\s/<>"'`=;|{}\[\]]+\.[a-zA-Z][\w.-]*$/.test(withoutLine);
    const path = /^(?:~\/|\.\.?\/|\/)[^\n]+$/.test(withoutLine)
      || (!/[\s<>"'`=;|{}\[\]]/.test(withoutLine) && withoutLine.includes('/')
          && (withoutLine.endsWith('/') || /^[^/]+\.[a-zA-Z][\w.-]*$/.test(withoutLine.split('/').pop())))
      || filename;
    if (!path && !explicit) return '';
    const query = new URLSearchParams({uid: context.uid, ref});
    if (context.agent) query.set('agent', context.agent);
    href = appUrl('/api/session/file') + '?' + query;
    const open = appUrl('file.html') + '?' + query + '&open=1';
    return `<a href="${esc(open)}" data-file-ref="${esc(ref)}" data-file-href="${esc(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  }
  return `<a href="${esc(href)}" data-reference-kind="web" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

const fileMenu = document.createElement('div');
fileMenu.id = 'file-menu'; fileMenu.className = 'ctx-menu'; fileMenu.hidden = true;
fileMenu.setAttribute('role', 'menu'); fileMenu.setAttribute('aria-label', '文件操作');
const fileMenuTargetText = document.createElement('div');
fileMenuTargetText.id = 'file-menu-target'; fileMenuTargetText.className = 'ctx-menu-target';
fileMenuTargetText.dir = 'auto';
fileMenu.appendChild(fileMenuTargetText);
fileMenu.setAttribute('aria-describedby', fileMenuTargetText.id);
for (const [action, label] of [['copy-path', '复制完整路径'],
  ['copy-directory', '复制所在目录路径'], ['download', '下载'],
  ['copy-url', '复制链接地址'], ['open-web', '在新标签页打开']]) {
  const button = document.createElement('button');
  button.type = 'button'; button.dataset.action = action; button.textContent = label;
  button.setAttribute('role', 'menuitem'); fileMenu.appendChild(button);
}
document.body.appendChild(fileMenu);
let fileMenuTarget = null;
function closeFileMenu() { fileMenu.hidden = true; fileMenuTarget = null; }

async function copyFileText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* HTTP fallback */ }
  }
  const input = document.createElement('textarea');
  input.value = text; input.style.cssText = 'position:fixed;left:-10000px;top:0';
  document.body.appendChild(input); input.select();
  try { if (!document.execCommand('copy')) throw new Error('复制失败'); }
  finally { input.remove(); }
}

document.addEventListener('contextmenu', async event => {
  const link = event.target.closest('.mb a[data-file-ref], .mb a[data-local-path], .mb a[data-reference-kind="web"]');
  if (!link) return;
  event.preventDefault(); closeItemMenu();
  const target = fileMenuTarget = {path: link.dataset.localPath, href: link.dataset.fileHref || link.href,
    kind: link.dataset.referenceKind === 'web' ? 'web' : link.dataset.fileKind};
  const place = () => {
    fileMenu.hidden = false;
    const box = fileMenu.getBoundingClientRect();
    fileMenu.style.left = `${Math.max(8, Math.min(event.clientX, innerWidth - box.width - 8))}px`;
    fileMenu.style.top = `${Math.max(8, Math.min(event.clientY, innerHeight - box.height - 8))}px`;
  };
  if (link.dataset.fileRef) {
    fileMenuTargetText.textContent = '正在读取文件信息…';
    for (const button of fileMenu.querySelectorAll('button')) button.hidden = true;
    place();
    try {
      const query = new URL(target.href).searchParams, ref = query.get('ref');
      const response = await fetch(appUrl('/api/session/resolve-files'), {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({uid:query.get('uid'), agent:query.get('agent') || '', refs:[ref]}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法读取文件信息');
      const detail = data.targets?.find(item => item.ref === ref);
      const path = detail?.path || data.resolved?.[ref];
      if (typeof path !== 'string' || !path.startsWith('/')) throw new Error('文件不存在或有多个同名文件，请使用完整路径。');
      if (fileMenuTarget !== target || !link.isConnected) return;
      target.path = path;
      target.kind = ['file', 'directory'].includes(detail?.kind) ? detail.kind : 'unknown';
    } catch (error) {
      if (fileMenuTarget === target) { fileMenuTargetText.textContent = error.message || '无法读取文件信息'; place(); }
      return;
    }
  }
  fileMenuTargetText.textContent = fileMenuTarget.kind === 'web' ? fileMenuTarget.href : fileMenuTarget.path;
  // 目录和类型未确认的目标不能下载，直接不显示该项，不留灰色菜单项。
  const actions = fileMenuTarget.kind === 'web'
    ? ['copy-url', 'open-web']
    : fileMenuTarget.kind === 'file' ? ['copy-path', 'copy-directory', 'download']
    : ['copy-path'];
  fileMenu.setAttribute('aria-label', fileMenuTarget.kind === 'web' ? '链接操作' : '文件操作');
  for (const button of fileMenu.querySelectorAll('button')) {
    button.hidden = !actions.includes(button.dataset.action);
  }
  place();
  fileMenu.querySelector('button:not([hidden])').focus({preventScroll: true});
});
document.addEventListener('pointerdown', event => {
  if (!fileMenu.contains(event.target)) closeFileMenu();
}, true);
addEventListener('resize', closeFileMenu);
document.addEventListener('scroll', event => {
  if (!fileMenu.contains(event.target)) closeFileMenu();
}, true);
fileMenu.addEventListener('keydown', event => {
  const buttons = [...fileMenu.querySelectorAll('button:not([hidden])')];
  const index = buttons.indexOf(document.activeElement);
  if (event.key === 'Escape') { event.preventDefault(); closeFileMenu(); }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
  }
});
fileMenu.addEventListener('click', async event => {
  const action = event.target.closest('button')?.dataset.action;
  const target = fileMenuTarget;
  if (!action || !target) return;
  closeFileMenu();
  try {
    if (action === 'copy-path') await copyFileText(target.path);
    else if (action === 'copy-directory' && target.kind === 'file') {
      await copyFileText(target.path.slice(0, target.path.lastIndexOf('/')) || '/');
    }
    else if (action === 'copy-url') await copyFileText(target.href);
    else if (action === 'open-web') window.open(target.href, '_blank', 'noopener,noreferrer');
    else if (action === 'download') {
      const url = new URL(target.href); url.searchParams.set('download', '1');
      const link = document.createElement('a'); link.href = url.href;
      link.download = ''; document.body.appendChild(link); link.click(); link.remove();
    }
  } catch (error) { alert(error.message || '文件操作失败'); }
});

function inline(s, media = [], context = {}) {
  s = String(s).replace(/\u0000/g, '');
  const codeSpans = [], codeLabels = [], codeText = [];
  s = s.replace(RE_CODE_SPAN, (_, prefix, _ticks, raw) => {
    // Markdown 代码跨度允许内容中出现更长的反引号串，例如用单反引号
    // 包住 ```python。先占位再处理图片/粗体，避免代码内容被二次解析。
    const content = raw.startsWith(' ') && raw.endsWith(' ') && /\S/.test(raw)
      ? raw.slice(1, -1) : raw;
    const code = `<code>${esc(content)}</code>`;
    codeText.push(content);
    codeLabels.push(code);
    codeSpans.push(code);
    return `${prefix}\u0000CODE${codeSpans.length - 1}\u0000`;
  });
  const images = [];
  s = s.replace(RE_MD_IMAGE, (_, alt, raw) => {
    const ref = raw.replace(/^<|>$/g, '');
    const found = media.find(x => x.ref === ref);
    const src = found?.src || ref;
    const html = imageHtml({ ...(found || {}), src, alt: alt || found?.alt || '图片' }, true);
    if (!html) return `[图片: ${alt || ref}]`;
    images.push(html);
    return `\u0000IMG${images.length - 1}\u0000`;
  });
  const links = [];
  const keepLink = html => {
    links.push(html);
    return `\u0000LINK${links.length - 1}\u0000`;
  };
  const emphasis = text => text
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<i>$2</i>');
  // Standard Markdown links display their label only. Keep the destination
  // in link metadata for navigation/menu actions, never append it to the label.
  s = s.replace(/\[([^\]\n]+)\]\(\s*(<[^>\n]+>|(?:[^\s()]|\([^\s()]*\))+)(?:\s+["']([^"']*)["'])?\s*\)/g,
    (_raw, label, target, title) => {
      const content = emphasis(esc(label)).replace(/\u0000CODE(\d+)\u0000/g,
        (_, i) => codeLabels[+i] || '');
      const ref = target.replace(/^<|>$/g, '');
      let html = referenceLink(ref, content, context, true) || content;
      if (title !== undefined && html.startsWith('<a ')) html = html.replace('<a ', `<a title="${esc(title)}" `);
      return keepLink(html);
    });
  const linkCandidate = (raw, offset, source) => {
    // Do not link a suffix of a scheme, identifier or email address.
    if (offset && /[\w@/:.-]/.test(source[offset - 1])) return raw;
    const ref = trimReference(raw);
    const html = referenceLink(ref, esc(ref), context);
    return html ? keepLink(html) + raw.slice(ref.length) : raw;
  };
  // Parentheses opt prose into linkification. Code spans are explicit
  // references too, including when they appear outside parentheses.
  const parenthesized = (part, explicit) => {
    // A complete target may itself contain balanced parentheses. Do not split
    // a URL or a filename such as report(final).pdf into several links.
    // Preserve every delimiter, space and optional Markdown title. Only wrap
    // the existing target substring; never synthesize a label or new text.
    const match = part.match(/^(\s*)(<([^<>\n]+)>|(?:[^\s()]|\([^\s()]*\))+)(\s+(?:["'][^"']*["'])\s*|\s*)$/);
    if (match && !match[2].includes('\u0000')) {
      const ref = match[3] || match[2];
      const html = referenceLink(ref, esc(ref), context, explicit);
      if (html) return match[1] + (match[3] ? '<' : '') + keepLink(html)
        + (match[3] ? '>' : '') + match[4];
    }
    return part.replace(/\u0000CODE(\d+)\u0000/g, (raw, i) => {
      const code = codeLabels[+i];
      return keepLink(referenceLink(codeText[+i], code, context) || code);
    }).replace(RE_REFERENCE, linkCandidate);
  };
  const parts = [], stack = [];
  let start = 0, open = -1;
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '(' || s[i] === '（') {
      if (!stack.length) open = i;
      stack.push(s[i] === '(' ? ')' : '）');
    } else if (stack.length && s[i] === stack[stack.length - 1]) {
      stack.pop();
      if (!stack.length) {
        parts.push(s.slice(start, open + 1), parenthesized(s.slice(open + 1, i), s[open - 1] === ']'), s[i]);
        start = i + 1;
      }
    }
  }
  parts.push(s.slice(start));
  s = parts.join('');
  s = s.replace(/\u0000CODE(\d+)\u0000/g, (raw, i) =>
    keepLink(referenceLink(codeText[+i], codeLabels[+i], context) || codeLabels[+i]));
  return emphasis(esc(s))
    .replace(/\u0000LINK(\d+)\u0000/g, (_, i) => links[+i] || '')
    .replace(/\u0000CODE(\d+)\u0000/g, (_, i) => codeSpans[+i] || '')
    .replace(/\u0000IMG(\d+)\u0000/g, (_, i) => images[+i] || '');
}

// ---------------------------------------------------------------- 事件
$('#view').onclick = e => {
  const b = e.target.closest('button');
  if (!b) return;
  S.view = b.dataset.v;
  store.set('view', S.view);
  renderView();
  renderSide();
};

function renderView() {
  for (const b of $('#view').children) b.classList.toggle('on', b.dataset.v === S.view);
  const nest = $('#nest-toggle');
  nest.classList.toggle('on', S.nest);
  nest.setAttribute('aria-pressed', String(S.nest));
}
$('#nest-toggle').onclick = () => {
  S.nest = !S.nest;
  store.set('nest', S.nest);
  renderView();
  renderSide();
};

/** 收起/展开一条发起者下面的整棵子树；列表重画但不跳滚动位置。 */
function toggleNestFold(uid) {
  S.nestClosed.has(uid) ? S.nestClosed.delete(uid) : S.nestClosed.add(uid);
  store.set('nestClosed', [...S.nestClosed]);
  const side = $('#side'), top = side?.scrollTop || 0;
  renderSide();
  if (side) side.scrollTop = top;
}

// ---------------------------------------------------------------- 栏宽拖动
const SIDE_DEFAULT = 340;

function setSideWidth(px, save) {
  if (MOBILE.matches) {
    $('#left').style.removeProperty('width');
    return;
  }
  const w = Math.round(Math.max(200, Math.min(px, window.innerWidth - 320)));
  $('#left').style.width = w + 'px';
  document.documentElement.style.setProperty('--side-width',
    document.body.classList.contains('side-collapsed') ? '0px' : w + 'px');
  if (save) store.set('width', w);
}

function setSideCollapsed(collapsed, save = true) {
  collapsed = !!collapsed && !MOBILE.matches;
  document.body.classList.toggle('side-collapsed', collapsed);
  const button = $('#side-toggle');
  const label = collapsed ? '展开会话列表' : '收起会话列表';
  button.title = button.ariaLabel = label;
  button.setAttribute('aria-expanded', String(!collapsed));
  if (save) store.set('sideCollapsed', collapsed);
  const width = parseInt($('#left').style.width, 10) || store.get('width', SIDE_DEFAULT);
  document.documentElement.style.setProperty('--side-width', collapsed ? '0px' : width + 'px');
  requestAnimationFrame(() => {
    if (typeof fitTerm === 'function' && T?.term) fitTerm();
  });
}

$('#side-toggle').onclick = () => setSideCollapsed(
  !document.body.classList.contains('side-collapsed'));

// 横屏手机和平板也走这套桌面分栏；手指拖动只产生 pointer/touch 事件，
// 浏览器不会为触摸合成 mousemove，所以和 #tgrip 一样用 pointer 事件加捕获。
let dragging = false;
let dragPointer = null;
$('#drag').addEventListener('pointerdown', e => {
  dragging = true;
  dragPointer = e.pointerId;
  e.currentTarget.setPointerCapture?.(e.pointerId);
  document.body.classList.add('dragging');
  e.preventDefault();                 // 否则拖动会选中文本、输入框失焦
});
document.addEventListener('pointermove', e => {
  if (dragging && e.pointerId === dragPointer) setSideWidth(e.clientX);
});
function finishSideDrag(e) {
  if (!dragging || e.pointerId !== dragPointer) return;
  dragging = false;
  dragPointer = null;
  document.body.classList.remove('dragging');
  setSideWidth(parseInt($('#left').style.width, 10), true);
}
document.addEventListener('pointerup', finishSideDrag);
document.addEventListener('pointercancel', finishSideDrag);
$('#drag').addEventListener('dblclick', () => setSideWidth(SIDE_DEFAULT, true));
window.addEventListener('resize', () => setSideWidth(
  parseInt($('#left').style.width, 10) || store.get('width', SIDE_DEFAULT)));
MOBILE.addEventListener?.('change', e => {
  if (e.matches) {
    const detailVisible = !!S.sel && store.get('mobilePage', 'list') === 'detail';
    // 桌面终端跨进手机断点、而手机上次停在列表时，右栏即将被 CSS 隐藏。
    // 走与返回列表相同的暂存/停用流程，详情重新出现后由 restoreTermPane
    // 按可见尺寸激活；不能把仍活跃的 xterm 留在 display:none 的祖先下面。
    if (!detailVisible && typeof T !== 'undefined'
        && !$('#termpane').classList.contains('hidden')) closeTermPane(true);
    document.body.classList.toggle('mobile-detail', detailVisible);
  } else {
    document.body.classList.remove('mobile-detail');
  }
  syncMobileViewport();
  setSideWidth(store.get('width', SIDE_DEFAULT));
  setSideCollapsed(store.get('sideCollapsed', false), false);
});

$('#q').oninput = e => {
  cancelSearch();
  S.term = e.target.value.trim();
  renderSide();
};

$('#q').onkeydown = async e => {
  if (e.key === 'Escape') { cancelSearch(true); showSessionCount(); renderSide(); return; }
  if (e.key !== 'Enter') return;
  runSearch();
};

let searchSeq = 0, searchRun = 0, searchAbort = null;

function cancelSearch(clearQuery = false) {
  ++searchRun;
  searchAbort?.abort();
  searchAbort = null;
  searchProgressDone();
  S.results = null;
  if (clearQuery) { S.term = ''; $('#q').value = ''; }
  $('#stat').textContent = ''; $('#stat').classList.remove('err');
  if (HUB_MODE) { Nodes.errors.delete('search'); renderNodes(); }
}

function searchProgress(done, total, nodes = null, totalKnown = total !== null) {
  const box = $('#search-progress');
  const known = totalKnown && total > 0;
  const percent = known ? Math.min(100, Math.floor(done / total * 100)) : 0;
  box.classList.add('on');
  box.querySelector('b').textContent = known ? `${done} / ${total} 个会话 · ${percent}%`
    : done ? `已扫描 ${done} 个会话` : totalKnown ? '已扫描 0 个会话' : '正在读取会话数量…';
  const track = box.querySelector('.search-progress-track');
  track.hidden = !known;
  if (known) track.setAttribute('aria-valuenow', String(percent));
  else track.removeAttribute('aria-valuenow');
  track.setAttribute('aria-valuetext', box.querySelector('b').textContent);
  box.querySelector('i').style.width = known ? `${Math.min(100, done / total * 100)}%` : '0%';
  const rows = box.querySelector('.search-progress-nodes');
  if (nodes) rows.innerHTML = nodes.map(node => {
    const count = node.total !== null ? `${node.done} / ${node.total}` : `已扫描 ${node.done}`;
    const status = {preparing: '准备中', offline: '离线跳过', error: '搜索失败',
      limited: `${count} · 达到结果上限`, done: `${count} · 已完成`, scanning: `${count} 个会话`}[node.state] || count;
    return `<div class="search-progress-node" data-state="${esc(node.state)}"><span>${esc(node.name)}</span><span>${esc(status)}</span></div>`;
  }).join('');
}

function searchProgressDone() {
  const box = $('#search-progress');
  box.classList.remove('on');
  box.querySelector('i').style.width = '0%';
  box.querySelector('.search-progress-nodes').replaceChildren();
}

$('#search-cancel').onclick = () => { cancelSearch(true); showSessionCount(); renderSide(); };

function showSearchMatches(rows) {
  const found = new Map((S.results || []).map(row => [row.uid, row]));
  for (const row of rows) found.set(row.uid, row);
  S.results = [...found.values()].sort((a, b) => String(b.updated).localeCompare(String(a.updated))
    || b.uid.localeCompare(a.uid));
  $('#stat').textContent = ` 已找到 ${S.results.length} 个会话，继续搜索…`;
  renderSide();
}

async function fetchSearch(params, signal) {
  params.set('progress', '1');
  const r = await fetch(appUrl('api/search?' + params), { signal });
  if (!r.ok || !r.headers.get('Content-Type')?.includes('application/x-ndjson')) {
    return { ok: r.ok, data: await r.json() };
  }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = '', result = null, error = null;
  for (;;) {
    const { done, value } = await reader.read();
    buf += dec.decode(value || new Uint8Array(), { stream: !done });
    const lines = buf.split('\n');
    buf = done ? '' : lines.pop();
    for (const line of lines) {
      if (!line) continue;
      const event = JSON.parse(line);
      if (signal.aborted) return { ok: false, data: {} };
      if (event.type === 'progress') searchProgress(event.done, event.total, event.nodes,
        event.total_known ?? Number.isFinite(event.total));
      else if (event.type === 'matches') showSearchMatches(event.results || []);
      else if (event.type === 'result') result = event.data;
      else if (event.type === 'error') error = event.error;
    }
    if (done) break;
  }
  return error ? { ok: false, data: { error } }
    : result ? { ok: true, data: result }
      : { ok: false, data: { error: '搜索响应不完整' } };
}

async function runSearch() {
  const q = $('#q').value.trim();
  cancelSearch();
  S.term = q;
  const run = searchRun;
  if (!q) { S.results = null; showSessionCount(); renderSide(); return; }
  if (S.opts.regex && !reTerm(false)) {   // 本地先验一次, 省掉一次全盘扫描
    S.results = [];
    $('#stat').textContent = ' 正则无效';
    $('#stat').classList.add('err');
    renderSide();
    $('#stat').dataset.seq = ++searchSeq;
    return;
  }
  const p = new URLSearchParams({ q });
  if (HUB_MODE) p.set('source', Object.keys(SOURCES).filter(x => !S.off.has(x)).join(','));
  for (const k of ['case', 'word', 'regex']) if (S.opts[k]) p.set(k, '1');
  const ac = searchAbort = new AbortController();
  S.results = [];
  $('#stat').textContent = ' 正在搜索…';
  renderSide();
  if (HUB_MODE) { Nodes.errors.delete('search'); renderNodes(); }
  const searchNodes = HUB_MODE ? Nodes.list.filter(node => selectedNodeIds().includes(node.id)).map(node => ({
    id: node.id, name: node.name, done: 0, total: node.online === false ? 0 : null,
    state: node.online === false ? 'offline' : 'preparing',
  })) : null;
  searchProgress(0, null, searchNodes, false);
  let response;
  try {
    response = await fetchSearch(p, ac.signal);
  } catch (e) {
    if (e.name === 'AbortError') return;
    response = { ok: false, data: { error: e.message || '搜索失败' } };
  }
  if (run !== searchRun) return;
  searchAbort = null;
  searchProgressDone();
  const { ok, data: d } = response;
  applyNodeState(d, 'search');
  if (!ok) {                       // 兜底: 前端漏判的非法模式或网络失败
    $('#stat').textContent = ` 已找到 ${S.results?.length || 0} 个会话；` + (d.error || '搜索失败');
    $('#stat').classList.add('err');
  } else {
    $('#stat').classList.remove('err');
    S.results = d.results;
    $('#stat').textContent = d.truncated
      ? ` 命中超过 ${d.results.length} 个会话（已截断，请细化条件）`
      : ` 全文命中 ${d.results.length} 个会话`;
    if (d.partial) {
      const offline = (d.errors || []).filter(e => d.nodes?.some(n => n.id === e.node_id && n.online === false));
      const failed = (d.errors || []).filter(e => !offline.includes(e));
      if (offline.length) $('#stat').textContent += `（${offline.map(e => e.name).join('、')} 离线，未搜索）`;
      if (failed.length) $('#stat').textContent += `（${failed.map(e => e.name).join('、')} 搜索失败，结果不完整）`;
    }
  }
  renderSide();
  // 全文搜索只筛左侧列表；右侧会话的内容、滚动位置和展开状态保持原样。
  $('#stat').dataset.seq = ++searchSeq;              // 供测试判定"这一轮搜索已结束"
}

$('#opts').onclick = e => {
  const b = e.target.closest('button[data-o]');
  if (!b) return;
  const k = b.dataset.o;
  S.opts[k] = !S.opts[k];
  store.set('opts', S.opts);
  renderOpts();
  if (S.results) runSearch(); else renderSide();
};

function renderOpts() {
  for (const b of $('#opts').children) b.classList.toggle('on', !!S.opts[b.dataset.o]);
  $('#q').placeholder = S.opts.regex ? '正则搜索…  Enter 搜索对话正文'
                                     : '搜索标题…  Enter 搜索对话正文';
}

$('#reload').onclick = () => { cancelSearch(true); loadSessions(true); };

/* ---------- 回收站 ---------- */
// 删除只是把会话文件移进 ~/.local/share/agenthub/trash/，这里是它唯一的出口：
// 看还剩什么、放回原处、或者真的删掉。
let trashItems = [];
let trashBusy = false;
let trashScope = [];

function openTrash() {
  const dlg = $('#trash-dialog');
  if (!dlg.open) dlg.showModal();
  loadTrash();
}

async function loadTrash({ keepNote = false } = {}) {
  if (!keepNote) setTrashNote('');   // 刷新列表不能把刚做完那件事的回执抹掉
  $('#trash-list').innerHTML = '<div class="trash-empty">正在读取回收站…</div>';
  try {
    const scope = selectedNodeIds();
    const r = await fetch(appUrl('api/trash' + (HUB_MODE ? '?nodes=' + scope.join(',') : '')));
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    applyNodeState(d, 'trash');
    trashScope = scope;
    trashItems = Array.isArray(d.items) ? d.items : [];
    renderTrash(d);
    return true;
  } catch (e) {
    trashItems = [];
    $('#trash-list').innerHTML = '<div class="trash-empty">读取失败</div>';
    setTrashNote('读取回收站失败: ' + e.message, true);
    return false;
  }
}

function renderTrash(info) {
  const dir = info?.dir || '';
  $('#trash-sub').textContent = trashItems.length
    ? `${trashItems.length} 个已删除会话 · 共 ${fmtSize(info?.size || 0)} · ${dir}`
    : `回收站是空的 · ${dir}`;
  $('#trash-purge-all').disabled = !trashItems.length;
  $('#trash-list').innerHTML = trashItems.length
    ? trashItems.map(trashRow).join('')
    : '<div class="trash-empty">没有已删除的会话</div>';
}


function trashRow(it) {
  const badge = SOURCES[it.source] ? icon(it.source) : '';
  const where = it.restorable
    ? `<div class="trash-origin" title="${esc(it.origin)}">恢复到 ${esc(shortCwd(it.origin, 200))}</div>`
    : `<div class="trash-origin warn">${esc(it.reason || '无法恢复')}</div>`;
  return `<div class="trash-item" data-id="${esc(it.id)}">
    <div class="trash-main">
      <div class="trash-title">${badge}<span>${esc(it.title)}</span></div>
      <div class="trash-meta">
        <span>${esc(fmtTime(it.deleted_at))} 删除</span>
        <span>${fmtSize(it.size)}</span>
        <span class="trash-cwd" title="${esc(it.cwd)}">${esc(nodeDirectory(it, 34))}</span>
      </div>
      ${where}
    </div>
    <div class="trash-acts">
      <button type="button" class="btn" data-act="restore"${it.restorable ? '' : ' disabled'}>恢复</button>
      <button type="button" class="btn danger" data-act="purge">彻底删除</button>
    </div>
  </div>`;
}

function setTrashNote(text, isError = false) {
  const box = $('#trash-note');
  box.textContent = text || '';
  box.classList.toggle('err', !!text && isError);
}

async function trashPost(path, body, btn) {
  trashBusy = true;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(appUrl(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { setTrashNote(d.error || `请求失败: ${r.status}`, true); return null; }
    return d;
  } catch (e) {
    setTrashNote('请求失败: ' + e.message, true);
    return null;
  } finally {
    trashBusy = false;
    if (btn) btn.disabled = false;
  }
}

$('#trash-list').onclick = async e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn || trashBusy) return;
  const id = btn.closest('.trash-item')?.dataset.id;
  const item = trashItems.find(x => x.id === id);
  if (!item) return;
  if (btn.dataset.act === 'restore') {
    const d = await trashPost('api/trash/restore', { id: item.id }, btn);
    if (!d) return;
    if (await loadTrash({ keepNote: true })) {
      setTrashNote(`已恢复「${item.title}」到 ${d.path}`);
    }
    cancelSearch(true);
    await loadSessions(true);       // 恢复的会话立即回到左侧列表
    return;
  }
  if (!confirm(`彻底删除「${item.title}」?\n\n文件将从磁盘移除, 不可恢复。`)) return;
  const d = await trashPost('api/trash/purge', { id: item.id }, btn);
  if (!d) return;
  if (await loadTrash({ keepNote: true })) {
    setTrashNote(`已彻底删除「${item.title}」, 释放 ${fmtSize(d.freed || 0)}`);
  }
};

async function purgeAllTrash() {
  if (!trashItems.length || trashBusy) return;
  if (!confirm(`清空回收站?\n\n将从磁盘彻底删除 ${trashItems.length} 个会话, 不可恢复。`)) return;
  const d = await trashPost('api/trash/purge' + (HUB_MODE ? '?nodes=' + trashScope.join(',') : ''),
    { all: true }, $('#trash-purge-all'));
  if (!d) return;
  const failed = (d.errors || []).length;
  if (await loadTrash({ keepNote: true })) {
    setTrashNote(`已彻底删除 ${d.removed || 0} 个会话, 释放 ${fmtSize(d.freed || 0)}`
      + (failed ? `; ${failed} 个失败: ${d.errors[0]}` : ''), !!failed);
  }
}

$('#trash').onclick = openTrash;
$('#trash-reload').onclick = () => loadTrash();
$('#trash-purge-all').onclick = purgeAllTrash;
$('#trash-close').onclick = $('#trash-done').onclick = () => $('#trash-dialog').close();
$('#trash-dialog').addEventListener('click', e => {
  if (e.target === $('#trash-dialog')) $('#trash-dialog').close();
});

// 终端后端是每台机器的服务端设置，不进 localStorage：换个浏览器看到的必须是同一份。
// 机器的名称、配色和终端后端都保存在中央服务端；接入和移除机器仍是服务器操作。
const MACHINE_COLORS = [
  ['', '默认'], ['blue', '蓝'], ['violet', '紫'], ['amber', '琥珀'], ['teal', '青'],
  ['rose', '玫红'], ['lime', '青柠'], ['cyan', '天蓝'], ['fuchsia', '品红'],
];

function machineTargets() {
  if (typeof T === 'undefined' || !T.listLoaded) return [];
  if (!HUB_MODE) {
    return T.enabled && (T.backends || []).length
      ? [{id: '', name: '本机', color: '', online: true, local: true,
          backends: T.backends, backend: T.backend}] : [];
  }
  // 按注册表顺序列全部机器，停用的原位留着（只剩勾选框能把它接回来），不往后挪
  const machines = Nodes.machines.length ? Nodes.machines : Nodes.list;
  return machines.map(node => {
    const on = node.enabled !== false;
    const cap = on ? Nodes.capabilities[node.id] || {} : {};
    return {id: node.id, name: node.name, color: node.color || '',
            online: on ? node.online : null, local: false, enabled: on,
            backends: cap.backends || [], backend: cap.backend || ''};
  });
}

function setMachineNote(text, isError = false) {
  const note = $('#machine-note');
  note.textContent = text || '';
  note.hidden = !text;
  note.dataset.state = isError ? 'error' : 'ok';
}

function renderMachineSettings() {
  const rows = $('#machine-rows');
  const targets = machineTargets();
  const active = document.activeElement;
  // 正在输入机器名时不要重绘，否则光标和未提交的文字都会没；拖动中也不能换 DOM
  if (active && rows.contains(active) && active.tagName === 'INPUT' && active.type === 'text') return;
  if (machineDrag) return;
  closeMachinePalette();
  const focusedGrip = active?.dataset?.machineGrip;
  rows.textContent = '';
  if (!targets.length) {
    const empty = document.createElement('p');
    empty.className = 'setting-note';
    empty.textContent = '暂无可用机器。';
    rows.append(empty);
    return;
  }
  for (const target of targets) {
    rows.append(machineRow(target));
  }
  if (focusedGrip) $(`[data-machine-grip="${focusedGrip}"]`)?.focus();   // 方向键挪完焦点留在原行
}

// 机器的配色就是名字左边那个色块：点开直接挑，右边不再重复一栏下拉。
let machinePalette = null;

function closeMachinePalette() {
  if (!machinePalette) return;
  machinePalette.palette.hidden = true;
  machinePalette.button.setAttribute('aria-expanded', 'false');
  machinePalette = null;
}

function machineColor(target) {
  const wrap = document.createElement('div');
  wrap.className = 'machine-color';
  if (target.local) {                  // 本机没有注册表，也就没有可改的配色
    const swatch = document.createElement('span');
    swatch.className = 'machine-swatch';
    swatch.dataset.nodeColor = target.color;
    wrap.append(swatch);
    return wrap;
  }

  const label = (MACHINE_COLORS.find(([value]) => value === target.color) || ['', '默认'])[1];
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'machine-swatch';
  button.dataset.nodeColor = target.color;
  button.dataset.machineColor = target.id;
  button.setAttribute('aria-haspopup', 'true');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-label', `${target.name} 的配色：${label}`);

  const palette = document.createElement('div');
  palette.className = 'machine-palette';
  palette.setAttribute('role', 'listbox');
  palette.setAttribute('aria-label', `${target.name} 的配色`);
  palette.hidden = true;
  for (const [value, name] of MACHINE_COLORS) {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'machine-swatch';
    option.dataset.nodeColor = value;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', String(value === target.color));
    option.setAttribute('aria-label', name);
    option.title = name;
    option.onclick = async () => {
      closeMachinePalette();
      await saveMachine(target, {color: value}, button);
      $(`[data-machine-color="${target.id}"]`)?.focus();
    };
    palette.append(option);
  }

  button.onclick = () => {
    const wasOpen = !palette.hidden;
    closeMachinePalette();
    if (wasOpen) return;
    palette.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    machinePalette = {wrap, button, palette};
    palette.querySelector('[aria-selected="true"]')?.focus();
  };
  wrap.append(button, palette);
  return wrap;
}

document.addEventListener('pointerdown', event => {
  if (machinePalette && !machinePalette.wrap.contains(event.target)) closeMachinePalette();
});

$('#settings-dialog').addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !machinePalette) return;
  const button = machinePalette.button;   // Esc 先收调色板，别顺手把设置框也关了
  closeMachinePalette();
  button.focus();
  event.preventDefault();
  event.stopPropagation();
});

function machineRow(target) {
  const row = document.createElement('div');
  row.className = 'machine-row' + (target.enabled === false ? ' machine-off' : '');
  row.dataset.machine = target.id;

  const head = document.createElement('div');
  head.className = 'machine-head';
  if (!target.local) {
    // 顺序由用户拖出来（键盘用方向键），机器筛选和新建会话下拉都按这个顺序
    const grip = document.createElement('button');
    grip.type = 'button';
    grip.className = 'machine-grip';
    grip.dataset.machineGrip = target.id;
    grip.title = '拖动调整顺序；也可按 ↑ ↓';
    grip.setAttribute('aria-label', `调整 ${target.name} 的顺序：拖动，或按上下方向键`);
    grip.innerHTML = '<span aria-hidden="true">⋮⋮</span>';
    grip.onpointerdown = event => startMachineDrag(event, row, grip);
    grip.onkeydown = event => {
      const step = {ArrowUp: -1, ArrowDown: 1}[event.key];
      if (!step) return;
      event.preventDefault();
      moveMachineRow(row, step);
    };
    head.append(grip);
    // 双系统的两台机器一台开着另一台必然关着：没勾选的不显示、不检查，就当不存在
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.className = 'machine-enabled';
    toggle.checked = target.enabled !== false;
    toggle.title = toggle.checked ? '取消勾选后这台机器不显示、不检查' : '勾选后重新接入这台机器';
    toggle.setAttribute('aria-label', `启用 ${target.name}`);
    toggle.onchange = () => void saveMachine(target, {enabled: toggle.checked}, toggle);
    head.append(toggle);
  }
  head.append(machineColor(target));

  if (target.local) {
    const label = document.createElement('b');
    label.textContent = target.name;
    head.append(label);
  } else {
    const name = document.createElement('input');
    name.type = 'text';
    name.value = target.name;
    name.maxLength = 80;
    name.setAttribute('aria-label', `${target.name} 的名称`);
    name.onchange = () => void saveMachine(target, {name: name.value}, name);
    name.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); name.blur(); } };
    head.append(name);
  }
  row.append(head);

  const fields = document.createElement('div');
  fields.className = 'machine-fields';
  const backend = document.createElement('select');
  backend.className = 'machine-backend';
  backend.setAttribute('aria-label', `${target.name} 的终端后端`);
  if (!target.backends.length) {
    const option = document.createElement('option');
    option.textContent = target.enabled === false ? '已停用'
      : target.online === false ? '离线' : '控制台未启用';
    backend.append(option);
    backend.disabled = true;
  } else {
    for (const item of target.backends) {
      const option = document.createElement('option');
      option.value = item.name;
      option.textContent = item.label + (item.available ? '' : '（不可用）');
      option.disabled = !item.available && item.name !== target.backend;
      backend.append(option);
    }
    backend.value = target.backend || '';
    backend.onchange = () => void chooseBackend(target, backend);
  }
  // 一行就是全部：说明都进悬停提示，不占高度
  const blocked = target.backends.filter(b => !b.available);
  backend.title = target.enabled === false
    ? '已停用：不显示、不检查，视同不存在；勾选后重新接入'
    : target.online === false
    ? (typeof nodeOfflineReason === 'function'
        ? nodeOfflineReason(Nodes.list.find(n => n.id === target.id) || {}) : '离线')
    : ['新建会话用的控制台后端', ...blocked.map(b => `${b.label}不可用：${b.unavailable_reason}`)].join('；');
  fields.append(backend);
  row.append(fields);
  return row;
}

// 两类请求的路径不同：展示属性是中央自己的接口，终端后端要转发给那台机器。
async function machinePost(url, body, control) {
  control.disabled = true;
  setMachineNote('正在保存…');
  try {
    const response = await fetch(appUrl(url), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error) throw new Error(
      data.error || `请求失败（HTTP ${response.status}）`);
    return data;
  } finally {
    control.disabled = false;
  }
}

// ---- 机器顺序：指针拖动 + 方向键 ----
let machineDrag = null;
let machineOrderTimer = 0;

function startMachineDrag(event, row, grip) {
  if (event.button !== 0 || machineDrag) return;
  event.preventDefault();
  const rows = $('#machine-rows');
  const before = [...rows.children].map(r => r.dataset.machine);
  machineDrag = {row, grip, before};
  row.classList.add('dragging');
  // 监听放在 document 上：行一被 insertBefore 挪动，浏览器就会释放指针捕获，
  // 捕获在把手上的话第一次换位之后就收不到 pointermove 了
  const move = e => {
    if (e.pointerId !== event.pointerId) return;
    // 拿指针所在的那一行当目标：在它上半就插到它前面，下半就插到它后面
    const y = e.clientY;
    for (const other of rows.children) {
      if (other === row) continue;
      const r = other.getBoundingClientRect();
      if (y < r.top || y > r.bottom) continue;
      const after = y > r.top + r.height / 2;
      const want = after ? other.nextSibling : other;
      if (want !== row && want !== row.nextSibling) rows.insertBefore(row, want);
      break;
    }
  };
  const finish = e => {
    if (e.pointerId !== event.pointerId) return;
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', finish);
    document.removeEventListener('pointercancel', finish);
    row.classList.remove('dragging');
    const dragged = machineDrag;
    machineDrag = null;
    const after = [...rows.children].map(r => r.dataset.machine);
    if (after.join() !== dragged.before.join()) void saveMachineOrder(after);
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', finish);
  document.addEventListener('pointercancel', finish);
}

function moveMachineRow(row, step) {
  const rows = $('#machine-rows');
  const target = step < 0 ? row.previousElementSibling : row.nextElementSibling?.nextElementSibling;
  if (step < 0 && !row.previousElementSibling) return;
  if (step > 0 && !row.nextElementSibling) return;
  rows.insertBefore(row, target || null);
  row.querySelector('.machine-grip')?.focus();
  clearTimeout(machineOrderTimer);   // 连按几下只提交最后的顺序
  machineOrderTimer = setTimeout(() => {
    void saveMachineOrder([...rows.children].map(r => r.dataset.machine));
  }, 400);
}

async function saveMachineOrder(ids) {
  const rows = $('#machine-rows');
  try {
    const response = await fetch(appUrl('api/nodes/order'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
    Nodes.machines = data.machines;
    setMachineNote('顺序已保存。');
    await loadNodes();                  // 机器筛选和新建会话下拉跟着换顺序
    renderMachineSettings();
  } catch (error) {
    setMachineNote(`顺序保存失败：${error.message || error}`, true);
    await loadNodes().catch(() => {});
    renderMachineSettings();            // 回到服务端仍然认的顺序
  }
}

async function saveMachine(target, patch, control) {
  if (target.local) return;                 // 本机模式没有注册表，没有可改的机器身份
  const before = {name: target.name, color: target.color, enabled: target.enabled !== false};
  if ((patch.name ?? target.name) === target.name && (patch.color ?? target.color) === target.color
      && (patch.enabled ?? before.enabled) === before.enabled) return;
  try {
    const data = await machinePost(`api/nodes/${target.id}/display`, patch, control);
    Object.assign(target, {name: data.node.name, color: data.node.color, enabled: data.node.enabled});
    const node = Nodes.list.find(n => n.id === target.id);
    if (node) Object.assign(node, {name: data.node.name, color: data.node.color});
    if (data.node.enabled !== before.enabled) {
      // 机器出现或消失：机器筛选、会话列表、终端能力都要按新的机器集合重来
      setMachineNote(data.node.enabled ? `已重新接入 ${data.node.name}，正在检查…`
        : `已停用 ${data.node.name}：不显示、不检查，视同不存在。`);
      await loadNodes();
      renderMachineSettings();
      await Promise.allSettled([loadSessions(true), refreshLive(true),
        typeof loadTermList === 'function' ? loadTermList() : null]);
      renderMachineSettings();
      return;
    }
    setMachineNote(`已保存 ${data.node.name}。`);
    if (typeof renderNodes === 'function') renderNodes();
    renderMachineSettings();
  } catch (error) {
    if (control.type === 'checkbox') control.checked = before.enabled;
    else if (control.tagName === 'INPUT') control.value = before.name;
    else renderMachineSettings();       // 色块回到服务端仍然认的那个颜色
    setMachineNote(`保存失败：${error.message || error}`, true);
  }
}

async function chooseBackend(target, select) {
  const previous = target.backend;
  if (select.value === previous) return;
  try {
    const data = await machinePost(
      (HUB_MODE && target.id ? `api/nodes/${target.id}/` : '') + 'api/term/backend',
      {backend: select.value}, select);
    target.backend = data.backend;
    if (Array.isArray(data.backends)) target.backends = data.backends;
    const label = (target.backends.find(b => b.name === data.backend) || {}).label || data.backend;
    setMachineNote(`${target.name}：新建会话改用 ${label}；已在运行的会话不受影响。`);
    if (typeof loadTermList === 'function') void loadTermList();
  } catch (error) {
    select.value = previous;
    setMachineNote(`${target.name}：切换失败：${error.message || error}`, true);
  }
}

function showSettingsTab(name) {
  for (const tab of document.querySelectorAll('.settings-tab')) {
    const on = tab.dataset.tab === name;
    tab.classList.toggle('on', on);
    tab.ariaSelected = String(on);
  }
  $('#settings-appearance').hidden = name !== 'appearance';
  $('#settings-machines').hidden = name !== 'machines';
  $('#settings-sub').textContent = name === 'machines'
    ? '机器设置保存在中央服务端，所有浏览器一致'
    : '界面偏好保存在浏览器';
  store.set('settingsTab', name);
  if (name === 'machines') renderMachineSettings();
}

for (const tab of document.querySelectorAll('.settings-tab')) {
  tab.onclick = () => showSettingsTab(tab.dataset.tab);
}

function openSettings() {
  $('#setting-font').value = store.get('font', 'ubuntu');
  $('#setting-theme').value = store.get('theme', 'system');
  $('#setting-tool-icons').value = document.documentElement.dataset.toolIcons;
  $('#setting-cache').value = String(cacheLimitMb);
  setMachineNote('');
  showSettingsTab(store.get('settingsTab', 'appearance'));
  $('#settings-dialog').showModal();
}

$('#settings').onclick = openSettings;
$('#settings-dialog').addEventListener('click', e => {
  if (e.target === $('#settings-dialog')) $('#settings-dialog').close();
});
$('#setting-font').onchange = e => applyFont(e.target.value, true);
$('#setting-theme').onchange = e => applyTheme(e.target.value, true);
$('#setting-tool-icons').onchange = e => applyToolIcons(e.target.value, true);
$('#setting-cache').onchange = e => {
  cacheLimitMb = Math.max(0, +e.target.value || 0);
  CACHE_MAX_BYTES = cacheLimitMb ? cacheLimitMb * 1024 * 1024 : Infinity;
  store.set('cacheMb', cacheLimitMb);
  trimCache();
};

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('#item-menu').hidden) return closeItemMenu();
  if (S.picking) return setPicking(false);
  $('#q').blur();
});

setSideWidth(store.get('width', SIDE_DEFAULT));
setSideCollapsed(store.get('sideCollapsed', false), false);
renderOpts();
renderPickBar();
renderView();
ensureConsolePlaceholder();
pollLive();   // 终端面板由 term.js 自己初始化 (它在本文件之后加载)
function uidOfDeepLink(spec) {
  if (!spec) return null;
  const cut = spec.indexOf(':');
  const source = cut > 0 ? spec.slice(0, cut) : null;
  const sid = cut > 0 ? spec.slice(cut + 1) : spec;
  const matches = S.sessions.filter(s => s.sid === sid && (!source || s.source === source) && (!DEEP_NODE || s.node_id === DEEP_NODE));
  const hit = (matches.length === 1 ? matches[0] : null)
    || S.sessions.find(s => s.uid === spec);
  return hit ? hit.uid : null;
}
loadSessions(false).then(ok => {
  if (!ok) return;
  const deep = uidOfDeepLink(DEEP_SID);
  if (deep) { openSession(deep); return; }   // 深链优先于上次浏览位置
  const last = store.get('sel', null);       // 恢复上次看的会话
  const savedAgent = store.get('agent', null);
  const restoreDetail = !MOBILE.matches || store.get('mobilePage', 'list') === 'detail';
  if (restoreDetail && last && S.sessions.some(s => s.uid === last)) {
    openSession(last, savedAgent?.uid === last ? savedAgent.id : null);
  }
});
