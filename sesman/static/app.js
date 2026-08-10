'use strict';

const SOURCES = Object.freeze(Object.fromEntries(
  Object.values(SESMAN_CLIS).map(cli => [cli.source, {
    name: cli.name, icon: cli.icon, color: cli.color,
  }])));

// 所有界面状态都落 localStorage, 刷新后原样恢复
const store = {
  get(k, d) {
    try {
      const v = localStorage.getItem('sesman.' + k);
      return v === null ? d : JSON.parse(v);
    } catch { return d; }
  },
  set: (k, v) => localStorage.setItem('sesman.' + k, JSON.stringify(v)),
};

// 存储结构的版本只由公共层调度；每种 CLI 自己决定怎样迁移旧队列。
const QUEUED_MESSAGES_VERSION = 4;
function loadQueuedMessages() {
  const saved = store.get('queuedMessages', []);
  const valid = Array.isArray(saved) ? saved : [];
  const fromVersion = +store.get('queuedMessagesVersion', 1) || 1;
  if (fromVersion !== QUEUED_MESSAGES_VERSION) {
    const migrated = valid.flatMap(([uid, items]) => {
      const kept = sesmanCli(uid)?.migrateQueuedMessages(
        items, fromVersion, QUEUED_MESSAGES_VERSION) || [];
      return kept.length ? [[uid, kept]] : [];
    });
    store.set('queuedMessages', migrated);
    store.set('queuedMessagesVersion', QUEUED_MESSAGES_VERSION);
    return migrated;
  }
  return valid;
}

const FONT_CHOICES = {
  cascadia: '"Sesman CJK Sans", "Sesman Cascadia Mono", "Cascadia Mono", "Adwaita Mono", "Ubuntu Mono", Consola, Consolas, sans-serif',
  system: '"Sesman CJK Sans", ui-monospace, "SFMono-Regular", "Cascadia Mono", "Adwaita Mono", "Ubuntu Mono", "Liberation Mono", Consolas, sans-serif',
  consolas: '"Sesman CJK Sans", Consolas, Consola, "Cascadia Mono", "Liberation Mono", sans-serif',
};
const themeMedia = matchMedia('(prefers-color-scheme: dark)');

function applyTheme(choice = store.get('theme', 'system'), persist = false) {
  if (!['system', 'light', 'dark'].includes(choice)) choice = 'system';
  if (persist) store.set('theme', choice);
  document.documentElement.dataset.theme = choice === 'system'
    ? (themeMedia.matches ? 'dark' : 'light') : choice;
  if (typeof refreshTerminalPreferences === 'function') refreshTerminalPreferences(true);
}

function applyFont(choice = store.get('font', 'cascadia'), persist = false) {
  if (!FONT_CHOICES[choice]) choice = 'cascadia';
  if (persist) store.set('font', choice);
  document.documentElement.style.setProperty('--terminal-font', FONT_CHOICES[choice]);
  if (typeof refreshTerminalPreferences === 'function') refreshTerminalPreferences(false);
}

themeMedia.addEventListener('change', () => {
  if (store.get('theme', 'system') === 'system') applyTheme('system');
});
applyTheme();
applyFont();

const S = {
  sessions: [],
  view: store.get('view', 'tree'),
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
  syncing: false,     // 增量同步进行中
  syncGap: 350,       // 当前会话的同步间隔, 随有无新内容自适应
  live: new Set(),    // 仍在运行的会话 uid
  liveTmux: new Set(),// 其中运行在 tmux 里的会话 uid
  liveStarted: new Map(), // uid → 当前 CLI 主进程启动时间（Unix 秒）
  activeOnly: store.get('activeOnly', false), // 左栏只显示仍在运行的会话
  unread: new Map(store.get('unread', [])),   // uid → {count, tmux}; 只计代理产生的新内容
  cursors: new Map(), // 主会话/子代理 EOF 游标；用于后台会话的精确未读增量
  queued: new Map(loadQueuedMessages()),       // uid → 尚未写入原生会话记录的已发送消息
  starBusy: new Set(), // 正在持久化星标的会话，避免多个网页请求在服务端乱序
  sig: null,          // 列表对应的磁盘签名
  lastSync: 0,
};

const $ = s => document.querySelector(s);
const MOBILE = matchMedia('(max-width: 720px)');
// 页面既可挂在站点根目录，也可由反代放到 /sesman/ 之类的子路径。
const APP_BASE = new URL('.', location.href);
const appUrl = path => new URL(String(path).replace(/^\//, ''), APP_BASE).toString();
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const icon = src => `<svg class="ico" style="color:${SOURCES[src].color}"><use href="#${SOURCES[src].icon}"/></svg>`;
const uiIcon = name => `<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

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
/** 家目录缩成 ~; 过长的路径中间省略, 首尾都是有信息量的部分。
 *  不能用 CSS direction:rtl 来截左边 —— bidi 会把开头的 "/" 挪到末尾。 */
function shortCwd(p, max = 40) {
  p = (p || '(未知)').replace(/^\/home\/[^/]+/, '~');
  if (p.length <= max) return p;
  const seg = p.split('/');
  return seg.length > 3 ? `${seg[0]}/${seg[1]}/…/${seg.slice(-2).join('/')}` : p;
}
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
const cache = new Map();          // viewKey → {meta, msgs, version, end, bytes}
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
  const cli = sesmanCli(uid);
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
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
  return item.id;
}

function discardQueuedUserMessage(uid, id) {
  const items = queuedMessages(uid).filter(item => item.id !== id);
  if (items.length) S.queued.set(uid, items); else S.queued.delete(uid);
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
}

function discardAllQueuedUserMessages(uid) {
  if (!queuedMessages(uid).length) return false;
  S.queued.delete(uid);
  saveQueuedMessages();
  if (S.sel === uid && !S.agent) renderConversationTail(
    cache.get(viewKey(uid))?.activity, uid);
  return true;
}

function syncServerOutbox(uid, items) {
  if (sesmanCli(uid)?.source !== 'codex' || !Array.isArray(items)) return false;
  const next = items.map(item => ({ ...item, server: true }));
  const before = JSON.stringify(queuedMessages(uid));
  if (next.length) S.queued.set(uid, next); else S.queued.delete(uid);
  const changed = before !== JSON.stringify(next);
  if (changed && S.sel === uid && !S.agent) {
    renderConversationTail(cache.get(viewKey(uid))?.activity, uid);
  }
  return changed;
}

async function retryServerQueuedMessage(uid, id) {
  const activity = cache.get(viewKey(uid))?.activity || null;
  const d = await post('api/session/outbox/retry', { uid, id, activity });
  if (d.error) return alert('重试失败: ' + d.error);
  syncServerOutbox(uid, d.outbox || []);
}

async function discardServerQueuedMessage(uid, id) {
  const d = await post('api/session/outbox/discard', { uid, id });
  if (d.error) return alert('移除失败: ' + d.error);
  syncServerOutbox(uid, d.outbox || []);
}

function reconcileQueuedMessages(uid, messages) {
  const items = queuedMessages(uid).slice();
  if (!items.length) return false;
  const cli = sesmanCli(uid);
  if (!cli) return false;
  let changed = false;
  for (const message of messages || []) {
    const action = cli.queueAction(message);
    if (!action) continue;
    const at = items.findIndex(item => {
      if (item.text !== action.text) return false;
      // 同文指令可能连续排队；enqueue 应依次确认尚未确认的副本，
      // 不能反复命中第一条已确认项。
      if (action.type === 'confirm' && item.state === 'queued') return false;
      const recorded = Date.parse(message.ts || '');
      const boundary = Date.parse(item.afterTs || '');
      // 不比较浏览器 Date.now() 和 CLI 时间：手机/电脑时钟偏差、CLI 排队延迟
      // 都可能超过数秒。发送时的最后原生消息才是可靠的因果边界。
      // 旧版遗留项没有 afterTs，首次完整对账时按同文迁移清理。
      return !Number.isFinite(recorded) || !Number.isFinite(boundary)
        || recorded >= boundary - 1000;
    });
    if (at < 0) continue;
    if (action.type === 'confirm') {
      items[at] = { ...items[at], state: 'queued' };
      delete items[at].expiresAt;
      delete items[at].legacy;
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

/** 清掉只有 HTTP/tmux 成功、始终没有 CLI 原生回执的乐观消息。 */
function expireQueuedMessages(now = Date.now()) {
  let changed = false;
  let selectedChanged = false;
  for (const [uid, current] of S.queued) {
    const cli = sesmanCli(uid);
    if (!cli || !Array.isArray(current)) continue;
    const hasNativeHistory = cache.has(viewKey(uid));
    const kept = current.filter(item => !cli.queuedMessageExpired(
      item, now, hasNativeHistory));
    if (kept.length === current.length) continue;
    changed = true;
    selectedChanged ||= uid === S.sel;
    if (kept.length) S.queued.set(uid, kept); else S.queued.delete(uid);
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
  return S.liveTmux.has(uid)
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
  const r = await fetch(appUrl(`api/messages/${encodeURIComponent(uid)}?${p}`), { signal: opts.signal });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const total = +r.headers.get('Content-Length') || 0;
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    opts.onProgress?.(got, total);
  }
  const buf = new Uint8Array(got);
  let at = 0;
  for (const c of chunks) { buf.set(c, at); at += c.length; }
  return { data: JSON.parse(new TextDecoder().decode(buf)), bytes: got };
}

// ---- 进度条 ----
function progress(done, total, label) {
  const bar = $('#prog');
  bar.classList.add('on');
  const pct = total ? Math.min(100, done / total * 100) : 0;
  bar.querySelector('.bar').style.width = (total ? pct : 12) + '%';
  bar.querySelector('.bar').classList.toggle('idle', !total);
  bar.querySelector('.txt').textContent = total
    ? `${label} ${label === '渲染' ? `${done}/${total}` : fmtSize(done) + ' / ' + fmtSize(total)}`
    : `${label}…`;
}

function progressDone() {
  const bar = $('#prog');
  bar.classList.remove('on');
  bar.querySelector('.bar').style.width = '0%';
}

/** 把服务端给的一份 diff 应用到缓存和界面上。
 *  两种情况: 追加(接到末尾) 或 reset(整份重来) —— 和服务端的判定一一对应。 */
async function applyDiff(uid, data, bytes = 0, agent = null) {
  const key = viewKey(uid, agent);
  const e = cache.get(key);
  if (!e) return 0;
  if (!agent && Array.isArray(data.outbox)) syncServerOutbox(uid, data.outbox);
  if (data.outbox_only) return 0;
  if (!agent) reconcileQueuedMessages(uid, data.messages);
  if (data.reset) {                         // 回滚 / 重写过, 缓存作废
    cachePut(key, { meta: data.meta, msgs: data.messages, version: data.version,
                    end: data.end, anchor: data.anchor, activity: data.activity, bytes,
                    total: data.message_total, partial: data.partial || null });
    S.cursors.set(key, { end: data.end, head: data.version.head, anchor: data.anchor });
    if (S.sel === uid && S.agent === agent) {
      await renderSession(data.meta, data.messages, data.activity);
    }
    return data.messages.length;
  }
  if (data.start !== e.end) return 0;       // 不是接着当前位置的(重连/乱序), 丢掉
  e.version = data.version;
  e.end = data.end;
  e.anchor = data.anchor;
  S.cursors.set(key, { end: data.end, head: data.version.head, anchor: data.anchor });
  e.bytes += bytes;
  if (data.activity_changed) e.activity = data.activity;
  else if (e.activity?.state === 'waiting' && data.messages.some(
      m => m.role === 'tool_result' || m.role === 'answer')) {
    // 问题和回答可能分属两次增量读取，第二次已没有 call_id 映射。
    const answerAt = data.messages.findIndex(m => m.role === 'tool_result');
    data.messages = data.messages.map((m, i) => i === answerAt && m.role === 'tool_result'
      ? { ...m, role: 'answer' } : m);
    e.activity = { role: 'status', state: 'working', text: 'working', ts: new Date().toISOString() };
  }
  if (!data.messages.length) {
    if (S.sel === uid && S.agent === agent) renderConversationTail(e.activity, uid);
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
  box.querySelectorAll('.client-pending').forEach(node => node.remove());
  const mark = el('span');
  box.appendChild(mark);
  appendMessages(box, data.messages, null);
  for (let n = mark.nextSibling; n; n = n.nextSibling) markMatches(n);
  mark.remove();
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
async function syncSession(uid, agent = S.agent) {
  const e = cache.get(viewKey(uid, agent));
  if (!e || S.syncing) return 0;
  S.syncing = true;
  try {
    const { data, bytes } = await fetchMessages(uid, {
      agent, start: e.end, head: e.version.head, anchor: e.anchor });
    return await applyDiff(uid, data, bytes, agent);
  } catch {
    return 0;
  } finally {
    S.syncing = false;
  }
}

// ---- 服务端推送 ----
// 服务端盯着会话文件, 一变就把 diff 推过来, 不用客户端反复问。
let _es = null, _esUid = null, _esRetry = null;

function watchSession(uid, agent = S.agent) {
  closeWatch();
  const e = cache.get(viewKey(uid, agent));
  if (!e || !window.EventSource) return;
  const p = new URLSearchParams({ uid, start: e.end, head: e.version.head, anchor: e.anchor || '' });
  if (agent) p.set('agent', agent);
  const es = new EventSource(appUrl('api/watch?' + p));
  _es = es;
  _esUid = uid;
  es.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    applyDiff(uid, data, 0, agent);
  };
  es.onerror = () => {
    // EventSource 自带的重连会沿用旧 URL(旧偏移), 所以自己关掉重开, 带上新偏移
    es.close();
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
  if (_es) { _es.close(); _es = null; _esUid = null; }
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

async function pollLive(force = false) {
  if (document.hidden) return;
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
  for (const n of document.querySelectorAll('.item')) {
    const pendingRunning = n.dataset.tmuxName
      && typeof T !== 'undefined' && T.list?.some(t => t.name === n.dataset.tmuxName);
    n.classList.toggle('live', !!pendingRunning || S.live.has(n.dataset.uid));
    n.classList.toggle('live-tmux', !!pendingRunning || S.liveTmux.has(n.dataset.uid));
    paintItemStatus(n);
  }
  const h = $('#dlive');
  if (h) {
    const tmux = S.liveTmux.has(S.sel);
    h.classList.toggle('on', S.live.has(S.sel));
    h.classList.toggle('tmux', tmux);
    h.textContent = '●';
    h.title = h.ariaLabel = tmux ? '运行于 tmux' : '运行中';
  }
  const termButton = $('#a-term');
  if (termButton) {
    termButton.classList.toggle('session-live', S.live.has(S.sel));
    termButton.classList.toggle('session-tmux', S.liveTmux.has(S.sel));
  }
  const selected = S.sessions.find(x => x.uid === S.sel);
  if (selected) {
    renderSessionAction(selected);
    renderConversationTail(cache.get(viewKey(selected.uid, S.agent))?.activity, selected.uid);
  }
  const c = $('#livecount');
  if (c) {
    const pending = pendingTmuxSessions().length;
    const tmux = S.liveTmux.size + pending, direct = Math.max(0, S.live.size - S.liveTmux.size);
    const total = direct + tmux;
    c.innerHTML = total
      ? `${direct ? `<span class="live-direct">● ${direct}</span>` : ''}`
        + `${tmux ? `<span class="live-tmux-count">● ${tmux}</span>` : ''}`
      : '<span class="live-direct">● 0</span>';
    c.setAttribute('aria-label', `${total} 个活动会话；${S.activeOnly ? '正在只显示活动会话' : '点击只显示活动会话'}`);
    c.setAttribute('aria-pressed', String(S.activeOnly));
    c.title = S.activeOnly ? '显示全部会话' : '只显示活动会话';
    c.classList.toggle('visible', total > 0 || S.activeOnly);
    c.classList.toggle('active-only', S.activeOnly);
  }
  syncActiveOnlyList();
}

/** 活动筛选开启时，进程启停会改变列表成员；集合没变就不动 DOM。 */
function syncActiveOnlyList() {
  if (!S.activeOnly) return;
  const wanted = new Set(visible().map(s => s.uid));
  const shown = new Set([...document.querySelectorAll('#side .item')].map(n => n.dataset.uid));
  if (wanted.size === shown.size && [...wanted].every(uid => shown.has(uid))) return;
  const side = $('#side'), top = side?.scrollTop || 0;
  renderSide();
  if (side) side.scrollTop = top;
}

$('#livecount').onclick = () => {
  S.activeOnly = !S.activeOnly;
  store.set('activeOnly', S.activeOnly);
  renderSide();
  paintLive();
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
function showSessionCount(n) {
  $('#stat').innerHTML = `${n}<span class="stat-unit"> 个会话</span>`;
}

const pendingUid = name => `tmux:${name}`;

/** sesman 自己启动、但还没有对话文件的 tmux，也是一条可重新进入的临时会话。 */
function pendingTmuxSessions() {
  if (typeof T === 'undefined' || !Array.isArray(T.pending)) return [];
  return T.pending.flatMap(t => {
    if (!SOURCES[t.source] || (t.sid && S.sessions.some(s =>
      s.source === t.source && String(s.sid) === String(t.sid)))) return [];
    const source = t.source;
    return [{
      uid: pendingUid(t.name), pending: true, name: t.name, tmuxName: t.name, source,
      title: `新建 ${SOURCES[source].name} 会话`, cwd: t.cwd || '(未知)',
      created: new Date((t.started || Date.now() / 1000) * 1000).toISOString(),
      updated: new Date((t.started || Date.now() / 1000) * 1000).toISOString(),
      size: 0,
    }];
  });
}

const sidebarSessions = () => [...pendingTmuxSessions(), ...S.sessions];

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
    m.title, m.parent_title, m.sid, m.agent_type, !!m.starred,
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
    if (d.unchanged || !d.sessions) return;
    S.sig = d.sig;
    S.sessions = d.sessions;
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
  let pool = (S.results || sidebarSessions()).filter(s => !S.off.has(s.source));
  if (S.activeOnly) pool = pool.filter(s => s.pending || S.live.has(s.uid));
  if (!S.term || S.results) return pool;          // 搜索态下服务端已经筛过
  return pool.filter(s => hasTerm(s.title) || hasTerm(s.cwd));
}

// ---------------------------------------------------------------- 左栏
const sessionStarred = uid => !!S.sessions.find(s => s.uid === uid)?.starred;

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
  box.innerHTML = '';
  for (const [k, v] of Object.entries(SOURCES)) {
    const n = sidebarSessions().filter(s => s.source === k).length;
    const c = el('div', 'chip' + (S.off.has(k) ? ' off' : ''),
      `${icon(k)}<span>${v.name}</span><b>${n}</b>`);
    c.title = v.name;
    c.setAttribute('aria-label', `${v.name}，${n} 个会话`);
    c.onclick = () => {
      S.off.has(k) ? S.off.delete(k) : S.off.add(k);
      store.set('off', [...S.off]);
      renderChips(); renderSide();
    };
    box.appendChild(c);
  }
}

function groupBy(list) {
  const m = new Map();
  for (const s of list) {
    const k = S.view === 'tree' ? (s.cwd || '(未知)') : dayKey(s.updated);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(s);
  }
  const keys = [...m.keys()];
  if (S.view === 'date') keys.sort().reverse();
  else keys.sort((a, b) => {
    const ta = Math.max(...m.get(a).map(x => +new Date(x.updated)));
    const tb = Math.max(...m.get(b).map(x => +new Date(x.updated)));
    return tb - ta;
  });
  for (const k of keys) m.get(k).sort((a, b) =>
    Number(!!b.starred) - Number(!!a.starred)
    || new Date(b.updated) - new Date(a.updated));
  return keys.map(k => [k, m.get(k)]);
}

const itemMeta = s => s.pending ? `${fmtTime(s.updated)} · 等待首条消息`
  : [fmtTime(s.updated), fmtSize(s.size), s.model || '',
                       s.agents ? `⑂${s.agents}` : '',
                       s.hits ? `命中 ${s.hits}${s.hits_capped ? '+' : ''}` : '']
                      .filter(Boolean).join(' · ');

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

  for (const [key, items] of groups) {
    const g = have.get(key);
    const ul = g.querySelector('.glist');
    if (!ul) return false;
    const nodes = new Map([...ul.children].map(n => [n.dataset.uid, n]));
    if (nodes.size !== items.length || items.some(s => !nodes.has(s.uid))) return false;
    for (const s of items) {
      const n = nodes.get(s.uid);
      ul.appendChild(n);                     // 按新顺序挪位置, 节点本身不动
      const m = n.querySelector('.m');
      const t = itemMeta(s);
      if (m && m.textContent !== t) m.textContent = t;
      const title = n.querySelector('.t');
      if (title && title.textContent !== s.title) {
        title.title = s.title;
        title.innerHTML = hl(s.title);
      }
      paintStarButton(n.querySelector('.item-star'), !!s.starred, S.starBusy.has(s.uid));
    }
    const c = g.querySelector('.gcount');
    if (c && c.textContent !== String(items.length)) c.textContent = items.length;
  }
  return true;
}

function renderSide() {
  const side = $('#side');
  side.innerHTML = '';
  const list = visible();
  if (!list.length) {
    const text = S.activeOnly
      ? (S.results ? '没有活动的匹配会话' : '没有活动会话')
      : (S.results ? '没有匹配的会话' : '没有会话');
    side.appendChild(el('div', 'empty', text));
    return;
  }
  for (const [key, items] of groupBy(list)) {
    const g = el('div', 'group' + (S.closed.has(key) ? ' closed' : ''));
    g.dataset.key = key;
    const label = S.view === 'tree' ? shortCwd(key, 999) : key;   // 分组标题不缩写, 只换 ~
    const head = el('div', 'ghead',
      `<span class="caret">▼</span><span class="gname" title="${esc(key)}">${esc(label)}</span>
       <span class="gcount">${items.length}</span>`);
    head.onclick = () => {
      S.closed.has(key) ? S.closed.delete(key) : S.closed.add(key);
      store.set('closed', [...S.closed]);
      g.classList.toggle('closed');
    };
    g.appendChild(head);
    const ul = el('div', 'glist');
    for (const s of items) {
      const meta = itemMeta(s);
      const it = el('div', 'item' + (S.sel === s.uid ? ' sel' : '')
                              + (s.pending ? ' pending live live-tmux' : '')
                              + (!s.pending && S.live.has(s.uid) ? ' live' : '')
                              + (!s.pending && S.liveTmux.has(s.uid) ? ' live-tmux' : ''),
        `<span class="ico">${icon(s.source)}<span class="item-status"></span></span>
         <div class="body">
           <div class="t" title="${esc(s.title)}">${hl(s.title)}</div>
           <div class="m">${esc(meta)}</div>
           ${S.view === 'date'
             ? `<div class="cwd" title="${esc(s.cwd)}">${esc(shortCwd(s.cwd))}</div>` : ''}
           ${s.snippet ? `<div class="snip">${hl(s.snippet)}</div>` : ''}
         </div>
         ${s.pending ? '' : starButtonMarkup(s.uid, !!s.starred, 'item-star')}`);
      it.dataset.uid = s.uid;
      if (s.pending) it.dataset.tmuxName = s.tmuxName;
      it.onclick = () => s.pending ? openPendingSession(s) : openSession(s.uid);
      const star = it.querySelector('.item-star');
      if (star) star.onclick = event => {
        event.stopPropagation();
        toggleSessionStar(s.uid);
      };
      paintItemStatus(it);
      ul.appendChild(it);
    }
    g.appendChild(ul);
    side.appendChild(g);
  }
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
  // 只高亮正文: 折叠预览是正文副本, 高亮在那里会造成重复计数。
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => {
      const msg = n.parentElement.closest('.msg');
      return n.parentElement.closest('.fold-preview, .katex')
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
    if (count >= MARK_MAX) { S.markCapped = true; break; }   // 搜 "a" 会生成上万节点, 会卡死
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
      if (++count >= MARK_MAX) { S.markCapped = true; break; }
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

// ---------------------------------------------------------------- 详情
let inflight = null;

async function openSession(uid, agent = null) {
  const selectedAgent = agent || null;
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
    await renderSession(hit.meta, hit.msgs, hit.activity);
    if (S.sel === uid && S.agent === selectedAgent) syncSession(uid, selectedAgent);
    return;
  }

  $('#detail').innerHTML = '<div class="spin">正在读取会话…</div>';
  progress(0, 0, '读取');
  let res;
  try {
    res = await fetchMessages(uid, {
      agent: selectedAgent, signal: ac.signal,
      windowed: true,
      onProgress: (a, b) => progress(a, b, '读取'),
    });
  } catch (e) {
    if (e.name === 'AbortError') return;      // 已经切到别的会话了
    progressDone();
    $('#detail').innerHTML = `<div class="empty">读取失败: ${esc(e.message)}</div>`;
    return;
  }
  if (S.sel !== uid || S.agent !== selectedAgent) return; // 期间切了别的视图
  const { data, bytes } = res;
  if (!selectedAgent && Array.isArray(data.outbox)) syncServerOutbox(uid, data.outbox);
  cachePut(key, { meta: data.meta, msgs: data.messages, version: data.version,
                  end: data.end, anchor: data.anchor, activity: data.activity, bytes,
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
    else if (atBottom(box)) _stick = true;         // 回到底部就恢复跟随
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
  progress(0, 0, '读取完整历史');
  try {
    const {data, bytes} = await fetchMessages(uid, {
      agent, signal: ac.signal,
      onProgress: (a, b) => progress(a, b, '读取完整历史'),
    });
    cachePut(key, {meta: data.meta, msgs: data.messages, version: data.version,
                   end: data.end, anchor: data.anchor, activity: data.activity, bytes,
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

async function renderSession(meta, msgs, activity = null) {
  const seq = ++renderSeq;
  const uid = meta.uid;
  const agent = meta.agent_id || null;
  if (S.sel !== uid || S.agent !== agent) return;
  const d = $('#detail');
  d.innerHTML = '';
  const entry = cache.get(viewKey(uid, agent));
  if (!agent) reconcileQueuedMessages(uid, msgs);
  d.appendChild(head(meta, entryTotal(entry || {msgs})));
  const box = el('div', 'msgs');
  box.id = 'msgs';
  d.appendChild(box);
  S.cur = -1; S.autoOpen = 0; S.markCapped = false;

  // 关键: 建在游离的 fragment 里, 最后一次性挂上。
  // 若逐批插入已在文档中的容器, 每批都会触发一次全量 layout, 上万条时是 O(n²) —— 实测 0.2s 变 14s。
  // 批间让出主线程用 setTimeout 而不是 rAF: rAF 会等一次绘制, 又把 layout 成本引回来。
  const partial = entry?.partial;
  const split = partial ? Math.min(+partial.head || 0, msgs.length) : 0;
  const plan = partial
    ? [...planMessages(msgs.slice(0, split)),
       {gap: {uid, agent, omitted: partial.omitted}},
       ...planMessages(msgs.slice(split))]
    : planMessages(msgs);
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

  const hits = markMatches(box);
  const c = $('#mcount');
  if (c) {
    c.textContent = hits ? `${hits}${S.markCapped ? '+' : ''} 处匹配` : '本页无匹配';
    if (S.markCapped || S.autoOpen >= AUTO_OPEN_MAX) {
      c.title = `命中过多：只标注前 ${MARK_MAX} 处、自动展开前 ${AUTO_OPEN_MAX} 条，其余标 ● 需手动展开`;
      c.classList.add('capped');
    }
    $('#m-prev').onclick = () => jumpMark(-1);
    $('#m-next').onclick = () => jumpMark(1);
    if (hits) jumpMark(1);
  }
  if (typeof renderComposer === 'function') {
    if (S.agent) $('#composer').classList.add('hidden');
    else renderComposer();
  }
  if (seq === renderSeq && S.sel === uid && S.agent === agent) {
    watchSession(meta.uid, agent); // 之后的更新由服务端推过来
    if (typeof restoreTermPane === 'function') restoreTermPane(uid, agent);
  }
}

function head(m, total) {
  const h = el('div', 'dhead');
  const tmuxLive = S.liveTmux.has(m.uid);
  const agentItems = m.agent_items || [];
  const hasAgents = agentItems.length > 0;
  const mainTitle = m.parent_title || m.title;
  const titleView = hasAgents ? `
    <button class="session-view-switch" id="a-view-switch" type="button"
      title="切换主会话/子代理" aria-label="切换主会话/子代理" aria-expanded="false">
      <span>${esc(m.title)}</span><i>⌄</i>
    </button>` : `<span>${esc(m.title)}</span>`;
  const menuView = hasAgents ? `
    <div class="session-view-menu" id="session-view-menu" hidden role="menu">
      <button type="button" data-agent="" class="${m.agent_id ? '' : 'on'}" role="menuitem">
        <small>主会话</small><b>${esc(mainTitle)}</b>
      </button>
      ${agentItems.map(a => `<button type="button" data-agent="${esc(a.id)}"
        class="${m.agent_id === a.id ? 'on' : ''}" role="menuitem">
        <small>子代理 · ${esc(a.type)}</small><b>${esc(a.title)}</b>
      </button>`).join('')}
    </div>` : '';
  h.innerHTML = `
    <div class="dtitle">
      <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
      <h2 class="${hasAgents ? 'has-session-views' : ''}">${icon(m.source)}${titleView}</h2>
      ${menuView}
      <div class="dhead-actions" aria-label="会话操作">
        <span class="mobile-msg-summary">
          <span class="mobile-msg-count" aria-label="${total} 条消息">${total}</span>
        </span>
        ${starButtonMarkup(m.uid, !!m.starred, 'iconbtn', 'a-star')}
        ${/* const 声明的全局不会挂到 window 上, 只能这样探 */
          (!m.agent_id && typeof T !== 'undefined' && T.enabled)
            ? `<button class="iconbtn" id="a-term" title="接管会话" aria-label="接管会话">${uiIcon('terminal')}</button>` : ''}
        ${S.term ? `<span class="mnav"><b id="mcount">…</b>
          <button class="iconbtn" id="m-prev" title="上一处" aria-label="上一处">↑</button>
          <button class="iconbtn" id="m-next" title="下一处" aria-label="下一处">↓</button></span>` : ''}
        ${m.agent_id ? '' : '<button class="iconbtn danger" id="a-session-action"></button>'}
      </div>
    </div>
    <div class="dmeta">
      <span class="meta-source">${esc(m.agent_type || SOURCES[m.source].name)}</span>
      <span id="mcount-total">${total} 条消息</span>
      <span id="dlive" class="dlive${S.live.has(m.uid) ? ' on' : ''}${tmuxLive ? ' tmux' : ''}"
        title="${tmuxLive ? '运行于 tmux' : '运行中'}" aria-label="${tmuxLive ? '运行于 tmux' : '运行中'}">●</span>
      <span class="meta-secondary">${esc(fmtTime(m.created))} → ${esc(fmtTime(m.updated))}</span>
      <span class="meta-secondary">${fmtSize(m.size)}</span>
      ${m.model ? `<span class="meta-secondary">${esc(m.model)}</span>` : ''}
      ${m.branch ? `<span class="meta-secondary">⑂ ${esc(m.branch)}</span>` : ''}
      <span class="meta-secondary"><code>${esc(m.cwd)}</code></span>
      <span class="meta-secondary session-id"><code>${esc(m.sid)}</code></span>
    </div>`;
  h.querySelector('.mobile-back').onclick = showMobileList;
  h.querySelector('#a-star').onclick = () => toggleSessionStar(m.uid);
  const viewSwitch = h.querySelector('#a-view-switch');
  const viewMenu = h.querySelector('#session-view-menu');
  if (viewSwitch && viewMenu) {
    const close = () => {
      viewMenu.hidden = true;
      viewSwitch.setAttribute('aria-expanded', 'false');
    };
    viewSwitch.onclick = e => {
      e.stopPropagation();
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
  const tb = h.querySelector('#a-term');
  if (tb) {
    tb.onclick = () => {
      const name = takenOver(m.uid);
      if (name) {
        T.uid = m.uid;
        toggleTermPane(name);
      }
      else takeover(m.uid, tb);
    };
    setTimeout(renderTakeoverBtn, 0);
  }
  renderSessionAction(m, h.querySelector('#a-session-action'));
  return h;
}

function renderSessionAction(m, button = $('#a-session-action')) {
  if (!button || m.uid !== S.sel) return;
  const running = S.live.has(m.uid);
  const label = running ? '停止会话' : '删除会话';
  button.innerHTML = uiIcon(running ? 'power' : 'trash');
  button.title = button.ariaLabel = label;
  button.onclick = () => running ? stopSession(m, button) : del(m);
}

async function stopSession(m, button) {
  if (!confirm(`停止会话「${m.title}」?\n\n停止后才可以删除会话记录。`)) return;
  button.disabled = true;
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
    button.disabled = false;
  }
}

async function del(m) {
  if (!confirm(`删除会话「${m.title}」?\n\n文件会移入回收站 ~/.local/share/sesman/trash/, 不会真删。`)) return;
  closeWatch();                         // 先停 SSE，避免文件移走后 EventSource 自动重连 404
  const r = await fetch(appUrl('api/session/' + encodeURIComponent(m.uid)), { method: 'DELETE' });
  const d = await r.json();
  if (!r.ok) {
    watchSession(m.uid);                // 删除失败，会话仍在，恢复实时同步
    return alert('删除失败: ' + (d.error || r.status));
  }
  S.sessions = S.sessions.filter(x => x.uid !== m.uid);
  if (S.results) S.results = S.results.filter(x => x.uid !== m.uid);
  S.sel = null;
  store.set('sel', null);
  renderChips(); renderSide();
  $('#detail').innerHTML = `<div class="empty">已移入回收站<br><code>${esc(d.trash)}</code></div>`;
  showMobileList();
}

const ROLE_LABEL = {
  user: '👤 用户', assistant: '🤖 助手', 'user·subagent': '👤 子代理输入',
  'assistant·subagent': '🤖 子代理', thinking: '💭 思考', system: '⚙️ 系统',
  tool: '🔧 工具调用', tool_result: '📄 工具输出', context: '📎 注入上下文',
  question: '❓ 询问', answer: '💬 回答', command: '⌘ 命令', event: '⚙️ 会话事件',
};
// 连续 3 条以上的工具调用/输出合并成一个可折叠的组, 避免刷屏
const TOOL_ROLES = new Set(['tool', 'tool_result']);
const SEARCH_ROLES = new Set(['user', 'assistant', 'user·subagent', 'assistant·subagent',
                              'thinking', 'question', 'answer', 'command']);
const GROUP_MIN = 3;

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
function planMessages(msgs) {
  const plan = [];
  let run = [];
  const flush = () => {
    if (run.length >= GROUP_MIN) plan.push({ g: run });
    else for (const m of run) plan.push({ m });
    run = [];
  };
  for (const m of pairTools(msgs)) {
    // 调用与结果跨增量批次时，空结果配不到上面的调用。它仍参与消息计数，
    // 但不能凭空生成一块黑色空卡片。
    if (m.role === 'tool_result' && !String(m.text || '').trim()
        && !m.media?.length && !m.changes?.length) {
      flush();
      plan.push({ m: { ...m, silent: true } });
      continue;
    }
    // 文件修改本身是用户关心的工作记录，始终作为可见卡片留在时间线；
    // 普通工具协议继续按原规则合并折叠。
    if (TOOL_ROLES.has(m.role) && !m.changes?.length) { run.push(m); continue; }
    flush();
    plan.push({ m });
  }
  flush();
  return plan;
}

function buildPlan(box, plan, before) {
  for (const p of plan) {
    const n = p.gap ? historyGapNode(p.gap) : (p.g ? groupNode(p.g) : msgNode(p.m));
    before ? box.insertBefore(n, before) : box.appendChild(n);
  }
}

const appendMessages = (box, msgs, before) => buildPlan(box, planMessages(msgs), before);

// 折叠态只是一行正文预览，不显示角色/时间 header。
function addFoldPreview(n, text, aria, hasHiddenHit = false) {
  n.classList.add('foldable');
  const preview = el('button', 'fold-preview');
  preview.type = 'button';
  preview.title = '展开';
  preview.setAttribute('aria-label', `展开${aria}`);
  const peek = el('span', 'peek');
  peek.textContent = text;
  preview.appendChild(peek);
  if (hasHiddenHit) preview.appendChild(el('i', 'dot', '●'));
  n.appendChild(preview);
  return preview;
}

function addAction(n) {
  const action = el('button', 'more disclosure');
  action.type = 'button';
  action.hidden = true;
  n.appendChild(action);
  return (label, fn) => {
    action.hidden = !label;
    action.textContent = label || '';
    action.onclick = fn || null;
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

function outPreview(t) { return outPreviewInfo(t).text; }

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
  if (m.role !== 'tool') {
    // 落单的工具输出(没配到调用): 保持独立块
    addClippedPre(entry, 'tool-out' + (m.error ? ' err' : ''),
                  m.name ? `${m.name}\n${m.text}` : m.text, toolOutputPath(m));
    if (m.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(m.media));
    return entry;
  }
  // 调用头: 一行语义摘要(如 `$ git status`), 点击展开原始参数
  const head = el('button', 'tool-head');
  head.type = 'button';
  head.title = '展开原始参数';
  head.setAttribute('aria-expanded', 'false');
  head.innerHTML = `<code>${esc(m.summary || m.name || 'tool')}</code>`
    + (m.name && m.summary ? `<span class="tool-meta">${esc(m.name)}</span>` : '');
  const headCode = head.querySelector(':scope > code');
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
    head.setAttribute('aria-expanded', String(open));
    head.title = open ? '收起原始参数' : '展开原始参数';
  };
  if (!args.hidden) paintArgs();
  head.onclick = () => setArgsOpen(args.hidden);
  closeArgs.onclick = () => setArgsOpen(false);
  entry.appendChild(args);
  entry.appendChild(closeArgs);
  setArgsOpen(!args.hidden);
  if (m.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(m.media));
  const r = m.result;
  if (r) {
    entry.dataset.result = '1';   // 吸收了一条 tool_result, 计数对账用
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
  return entry;
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
  if (m.result) n.dataset.result = '1';   // 修改类调用的确认输出并入卡片
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

function groupNode(items) {
  // 工具协议不属于对话正文搜索范围，工具组始终按默认规则折叠。
  const n = el('div', 'msg grp folded');
  n.dataset.role = 'toolgroup';
  const calls = items.filter(m => m.role === 'tool');
  // 预览行直接给前几条语义摘要(`$ cmd` 一类), 比"Bash ×3"信息量大
  const visible = calls.length ? calls : items;
  const heads = visible.slice(0, 3).map(m => m.summary || m.name || 'tool');
  const hasErr = items.some(m => m.result?.error || (m.role === 'tool_result' && m.error));
  const preview = addFoldPreview(n, '', '工具调用组');
  preview.classList.add('group-preview');
  const peek = preview.querySelector('.peek');
  peek.classList.add('group-peek');
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
  items.forEach(m => n.appendChild(toolEntry(m))); // 直接铺在组内，不再套 grp-body + 内层 msg
  const setAction = addAction(n);
  const fold = () => { n.classList.add('folded'); setAction(); };
  const open = () => { n.classList.remove('folded'); setAction('收起', fold); };
  n._fold = fold;
  n._open = open;
  preview.onclick = open;
  if (!n.classList.contains('folded')) open();
  return n;
}

function safeMediaSrc(src) {
  src = String(src || '');
  if (/^\/api\/media\/[0-9a-f]{32}$/.test(src)) return appUrl(src);
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
    if (m.counted === false) n.dataset.counted = 'false';
    n.appendChild(toolEntry(m));
    return n;
  }
  // 命中的消息展开且不截断, 保证高亮可见; 但设上限, 否则搜 "a" 会把整个会话全量展开
  const found = SEARCH_ROLES.has(m.role) && hasTerm(m.text);
  const hit = found && S.autoOpen < AUTO_OPEN_MAX;
  if (hit) S.autoOpen++;
  const raw = TOOL_ROLES.has(m.role);
  // 只有多行工具内容允许整条折叠；所有对话内容只可能出现“展开全文”。
  const foldable = raw && String(m.text || '').includes('\n');
  const n = el('div', 'msg' + (foldable && !hit ? ' folded' : '')
                            + (found && !hit ? ' hashit' : ''));
  n.dataset.role = m.role;
  if (m.counted === false) n.dataset.counted = 'false';
  const label = (ROLE_LABEL[m.role] || m.role) + (m.name ? ` · ${m.name}` : '');
  const peek = m.text.replace(/\s+/g, ' ').slice(0, 200);
  const preview = foldable ? addFoldPreview(n, peek, label, found && !hit) : null;
  const body = el('div', 'mb');
  const render = full => (raw
    ? `<pre>${esc(full ? m.text : clipText(m.text))}</pre>`
    : md(m.text, full, m.media)) + mediaGallery(m.media);
  const paint = full => { body.innerHTML = render(full); renderFormulae(body); paintSyntax(body); };
  paint(hit);
  n.appendChild(body);
  const setAction = addAction(n);
  const long = m.text.length > CLIP;
  const fold = () => { n.classList.add('folded'); body.classList.remove('clip'); setAction(); };
  const full = () => {
    n.classList.remove('folded'); body.classList.remove('clip'); paint(true);
    setAction(foldable || long ? '收起' : '', foldable ? fold : (long ? clipped : null));
  };
  const clipped = () => {
    n.classList.remove('folded'); body.classList.add('clip'); paint(false);
    setAction(`展开全文 (${m.text.length.toLocaleString()} 字符)`, full);
  };
  const open = () => long ? clipped() : full();
  if (foldable) {
    n._fold = fold;
    n._open = open;
    preview.onclick = open;
    if (hit) full();
  } else if (long) {
    hit ? full() : clipped();
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
  const label = kind === 'recap' ? '回顾' : (kind === 'task' ? '任务' : '会话');
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
  const body = el('div', 'mb question-body');
  const rows = Array.isArray(m.questions) && m.questions.length
    ? m.questions : [{ question: m.text, options: [] }];
  body.innerHTML = rows.map((q, i) => `
    <section class="question-item">
      ${q.header ? `<div class="question-header">${esc(q.header)}</div>` : ''}
      <div class="question-text">${esc(q.question || m.text)}</div>
      ${q.multiple ? '<div class="question-multiple">可多选</div>' : ''}
      ${(q.options || []).length ? `<div class="question-options">${q.options.map((o, j) => `
        <div class="question-option"><span>${j + 1}</span><div><b>${esc(o.label)}</b>
          ${o.description ? `<small>${esc(o.description)}</small>` : ''}</div></div>`).join('')}</div>` : ''}
    </section>`).join('');
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
    const cli = sesmanCli(uid);
    const node = msgNode({role: 'user', text: item.text, media: item.media, counted: false});
    node.classList.add('client-pending');
    node.classList.toggle('failed', item.state === 'failed');
    node.dataset.queuedId = item.id;
    node.appendChild(el('small', 'client-pending-state', cli?.queuedMessageLabel(item) || '排队中'));
    if (item.server && ['queued', 'failed'].includes(item.state)) {
      const actions = el('span', 'client-pending-actions');
      if (item.state === 'failed') {
        const retry = el('button', '', '重试');
        retry.type = 'button';
        retry.onclick = () => retryServerQueuedMessage(uid, item.id);
        actions.append(retry);
      }
      const discard = el('button', '', item.state === 'queued' ? '撤销' : '移除');
      discard.type = 'button';
      discard.onclick = () => discardServerQueuedMessage(uid, item.id);
      actions.append(discard);
      node.appendChild(actions);
      if (item.error) node.title = item.error;
    }
    box.appendChild(node);
  }
}

/** Activity 和乐观消息都是时间线尾部状态；每次重画都固定保持排队消息在最下方。 */
function renderConversationTail(activity, uid = S.sel) {
  const box = $('#msgs');
  if (!box) return;
  $('#activity')?.remove();
  box.querySelectorAll('.client-pending').forEach(node => node.remove());
  renderActivity(activity);
  renderQueuedMessages(uid);
}

const CLIP = 4000;
const AUTO_OPEN_MAX = 40;    // 最多自动展开这么多条命中消息, 其余只标记
const MARK_MAX = 3000;       // 单页高亮节点上限
const clipText = t => t.length > CLIP ? t.slice(0, CLIP) + '\n… (点下方按钮展开全文)' : t;

let syntaxLoading = false;
function ensureSyntax() {
  if (syntaxLoading || window.sesmanHighlight) return;
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
  if (!window.sesmanHighlight) { ensureSyntax(); return; }
  for (const code of nodes) {
    code.dataset.syntaxDone = '1';
    const result = code.matches('code.tool-command') && window.sesmanHighlightShellCommand
      ? window.sesmanHighlightShellCommand(code.textContent)
      : (code.matches('pre.tool-out') && window.sesmanHighlightSegments
          ? window.sesmanHighlightSegments(code.textContent, code.dataset.codePath || '')
          : window.sesmanHighlight(code.textContent, code.dataset.codeLang || '', code.dataset.codePath || ''));
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

addEventListener('sesman-highlight-ready', () => paintSyntax(document));

// 轻量 markdown: 代码块 / 表格 / 列表 / 引用 / 标题 / 行内标记
function md(text, full, media = []) {
  const source = full ? text : clipText(text);
  const lines = source.split('\n');
  const output = [], prose = [];
  const flush = () => {
    if (!prose.length) return;
    output.push(blocks(prose.join('\n'), media));
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

function blocks(src, media = []) {
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
      out += `<div class="tw"><table><thead><tr>${head.map((c, j) => `<th ${at(j)}>${inline(c, media)}</th>`).join('')}</tr></thead>`
        + `<tbody>${rows.map(r => `<tr>${r.map((c, j) => `<td ${at(j)}>${inline(c, media)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      continue;
    }

    const h = line.match(RE_HEAD);
    if (h) { out += `<h3 class="h${h[1].length}">${inline(h[2], media)}</h3>`; i++; continue; }

    if (RE_HR.test(line)) { out += '<hr>'; i++; continue; }

    if (RE_LIST.test(line)) {
      const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
      const items = [];
      while (i < ls.length && RE_LIST.test(ls[i])) {
        let item = ls[i++].replace(RE_LIST, '');
        while (i < ls.length && ls[i].trim() && !RE_LIST.test(ls[i]) && /^\s{2,}/.test(ls[i])) {
          item += '\n' + ls[i++].trim();     // 续行并入当前条目
        }
        items.push(`<li>${inline(item, media)}</li>`);
      }
      out += `<${tag}>${items.join('')}</${tag}>`;
      continue;
    }

    if (RE_QUOTE.test(line)) {
      const qs = [];
      while (i < ls.length && RE_QUOTE.test(ls[i])) qs.push(ls[i++].replace(RE_QUOTE, ''));
      out += `<blockquote>${inline(qs.join('\n'), media)}</blockquote>`;
      continue;
    }

    const para = [];
    while (i < ls.length && ls[i].trim() && !RE_LIST.test(ls[i]) && !RE_HEAD.test(ls[i])
           && !RE_QUOTE.test(ls[i]) && !RE_HR.test(ls[i]) && !isTable(ls, i)) {
      para.push(ls[i++]);
    }
    if (para.length) out += `<p>${inline(para.join('\n'), media)}</p>`;
    else i++;                                 // 兜底: 保证 i 一定前进
  }
  return out;
}

const RE_MD_IMAGE = /!\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+["'][^"']*["'])?\s*\)/g;
const RE_CODE_SPAN = /(^|[^`])(`+)(?!`)([^\n]*?)(?<!`)\2(?!`)/g;

function inline(s, media = []) {
  const codeSpans = [];
  s = s.replace(RE_CODE_SPAN, (_, prefix, _ticks, raw) => {
    // Markdown 代码跨度允许内容中出现更长的反引号串，例如用单反引号
    // 包住 ```python。先占位再处理图片/粗体，避免代码内容被二次解析。
    const content = raw.startsWith(' ') && raw.endsWith(' ') && /\S/.test(raw)
      ? raw.slice(1, -1) : raw;
    codeSpans.push(`<code>${esc(content)}</code>`);
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
  return esc(s)
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<i>$2</i>')
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

let dragging = false;
$('#drag').addEventListener('mousedown', e => {
  dragging = true;
  document.body.classList.add('dragging');
  e.preventDefault();                 // 否则拖动会选中文本
});
document.addEventListener('mousemove', e => { if (dragging) setSideWidth(e.clientX); });
document.addEventListener('mouseup', () => {
  if (!dragging) return;
  dragging = false;
  document.body.classList.remove('dragging');
  setSideWidth(parseInt($('#left').style.width, 10), true);
});
$('#drag').addEventListener('dblclick', () => setSideWidth(SIDE_DEFAULT, true));
window.addEventListener('resize', () => setSideWidth(
  parseInt($('#left').style.width, 10) || store.get('width', SIDE_DEFAULT)));
MOBILE.addEventListener?.('change', e => {
  if (e.matches) {
    document.body.classList.toggle('mobile-detail',
      !!S.sel && store.get('mobilePage', 'list') === 'detail');
  } else {
    document.body.classList.remove('mobile-detail');
  }
  syncMobileViewport();
  setSideWidth(store.get('width', SIDE_DEFAULT));
  setSideCollapsed(store.get('sideCollapsed', false), false);
});

$('#q').oninput = e => {
  if (S.results) { S.results = null; }     // 改动输入即退出全文搜索态
  S.term = e.target.value.trim();
  renderSide();
};

$('#q').onkeydown = async e => {
  if (e.key !== 'Enter') return;
  runSearch();
};

let searchSeq = 0, searchRun = 0, searchAbort = null;

function searchProgress(done, total) {
  const box = $('#search-progress');
  const known = total > 0;
  box.classList.add('on');
  box.classList.toggle('idle', !known);
  box.querySelector('b').textContent = known ? `${done} / ${total}` : '扫描中…';
  box.querySelector('i').style.width = known ? `${Math.min(100, done / total * 100)}%` : '12%';
}

function searchProgressDone() {
  const box = $('#search-progress');
  box.classList.remove('on', 'idle');
  box.querySelector('i').style.width = '0%';
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
      if (event.type === 'progress') searchProgress(event.done, event.total);
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
  S.term = q;
  const run = ++searchRun;
  searchAbort?.abort();
  searchAbort = null;
  searchProgressDone();
  if (!q) { S.results = null; renderSide(); return; }
  if (S.opts.regex && !reTerm(false)) {   // 本地先验一次, 省掉一次全盘扫描
    S.results = [];
    $('#stat').textContent = ' 正则无效';
    $('#stat').classList.add('err');
    renderSide();
    $('#stat').dataset.seq = ++searchSeq;
    return;
  }
  const p = new URLSearchParams({ q });
  for (const k of ['case', 'word', 'regex']) if (S.opts[k]) p.set(k, '1');
  const ac = searchAbort = new AbortController();
  searchProgress(0, 0);
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
  if (!ok) {                       // 兜底: 前端漏判的非法模式或网络失败
    S.results = [];
    $('#stat').textContent = ' ' + (d.error || '搜索失败');
    $('#stat').classList.add('err');
  } else {
    $('#stat').classList.remove('err');
    S.results = d.results;
    $('#stat').textContent = d.truncated
      ? ` 命中超过 ${d.results.length} 个会话（已截断，请细化条件）`
      : ` 全文命中 ${d.results.length} 个会话`;
  }
  renderSide();
  // 全文搜索只筛左侧列表；右侧会话的内容、滚动位置和展开状态保持原样。
  $('#stat').dataset.seq = ++searchSeq;              // 供测试判定"这一轮搜索已结束"
}

$('#opts').onclick = e => {
  const b = e.target.closest('button');
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

$('#reload').onclick = () => { S.results = null; loadSessions(true); };

function openSettings() {
  $('#setting-font').value = store.get('font', 'cascadia');
  $('#setting-theme').value = store.get('theme', 'system');
  $('#setting-cache').value = String(cacheLimitMb);
  $('#settings-dialog').showModal();
}

$('#settings').onclick = openSettings;
$('#settings-dialog').addEventListener('click', e => {
  if (e.target === $('#settings-dialog')) $('#settings-dialog').close();
});
$('#setting-font').onchange = e => applyFont(e.target.value, true);
$('#setting-theme').onchange = e => applyTheme(e.target.value, true);
$('#setting-cache').onchange = e => {
  cacheLimitMb = Math.max(0, +e.target.value || 0);
  CACHE_MAX_BYTES = cacheLimitMb ? cacheLimitMb * 1024 * 1024 : Infinity;
  store.set('cacheMb', cacheLimitMb);
  trimCache();
};

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { $('#q').blur(); }
});

setSideWidth(store.get('width', SIDE_DEFAULT));
setSideCollapsed(store.get('sideCollapsed', false), false);
renderOpts();
renderView();
pollLive();   // 终端面板由 term.js 自己初始化 (它在本文件之后加载)
loadSessions(false).then(ok => {
  if (!ok) return;
  const last = store.get('sel', null);       // 恢复上次看的会话
  const savedAgent = store.get('agent', null);
  const restoreDetail = !MOBILE.matches || store.get('mobilePage', 'list') === 'detail';
  if (restoreDetail && last && S.sessions.some(s => s.uid === last)) {
    openSession(last, savedAgent?.uid === last ? savedAgent.id : null);
  }
});
