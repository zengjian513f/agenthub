'use strict';

const SOURCES = {
  claude: { name: 'Claude', icon: 'i-claude', color: 'var(--claude)' },
  codex:  { name: 'Codex',  icon: 'i-codex',  color: 'var(--codex)'  },
  grok:   { name: 'Grok',   icon: 'i-grok',   color: 'var(--grok)'   },
};

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

const S = {
  sessions: [],
  view: store.get('view', 'tree'),
  off: new Set(store.get('off', [])),
  closed: new Set(store.get('closed', [])),
  sel: null,
  filter: '',
  term: '',           // 当前要高亮的词 (= 搜索框内容)
  opts: Object.assign({ case: false, word: false, regex: false }, store.get('opts', {})),
  cur: -1,            // 匹配跳转游标
  autoOpen: 0,        // 本次渲染已自动展开的命中消息数
  markCapped: false,  // 高亮是否因数量上限被截断
  results: null,      // 全文搜索结果, null 表示未处于搜索态
  agents: false,      // 详情页是否合并子代理消息
  syncing: false,     // 增量同步进行中
  syncGap: 350,       // 当前会话的同步间隔, 随有无新内容自适应
  live: new Set(),    // 仍在运行的会话 uid
  liveTmux: new Set(),// 其中运行在 tmux 里的会话 uid
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

function showMobileDetail() {
  if (MOBILE.matches) {
    document.body.classList.add('mobile-detail');
    store.set('mobilePage', 'detail');
  }
}

function showMobileList() {
  if (typeof T !== 'undefined' && !$('#termpane').classList.contains('hidden')) closeTermPane();
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
const CACHE_MAX_BYTES = 64 * 1024 * 1024;
const RENDER_BATCH = 250;
const SYNC_MS = 10000;      // 没在运行的会话, 偶尔看一眼就行
const LIVE_MS = 3000;       // 活跃探测(扫 /proc)的间隔
// 正在看的活跃会话用自适应间隔: 有新内容就贴到最快, 静下来逐步退避
const FAST_MIN = 350;
const FAST_MAX = 3000;
const TICK_MS = 200;
const BACKUP_MS = 20000;    // SSE 正常时的兜底对账间隔
const LIST_MS = 8000;       // 会话列表跟进磁盘变化的间隔
const cache = new Map();          // uid → {meta, msgs, version, end, bytes}

function cacheGet(uid) {
  const e = cache.get(uid);
  if (e) { cache.delete(uid); cache.set(uid, e); }   // 命中即移到队尾
  return e;
}

function cachePut(uid, e) {
  cache.delete(uid);
  cache.set(uid, e);
  let total = 0;
  for (const v of cache.values()) total += v.bytes;
  while (total > CACHE_MAX_BYTES && cache.size > 1) {
    const oldest = cache.keys().next().value;
    total -= cache.get(oldest).bytes;
    cache.delete(oldest);
  }
}

/** 带下载进度的取消息。start/head 给定时服务端只回新增部分。 */
async function fetchMessages(uid, opts = {}) {
  const p = new URLSearchParams();
  if (opts.start) {
    p.set('start', opts.start);
    p.set('head', opts.head);
    p.set('anchor', opts.anchor || '');     // 没有锚点服务端会拒绝续读, 直接给整份
  }
  if (opts.agents) p.set('agents', '1');
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
async function applyDiff(uid, data, bytes = 0) {
  const e = cache.get(uid);
  if (!e) return 0;
  if (data.reset) {                         // 回滚 / 重写过, 缓存作废
    cachePut(uid, { meta: data.meta, msgs: data.messages, version: data.version,
                    end: data.end, anchor: data.anchor, bytes });
    if (S.sel === uid) await renderSession(data.meta, data.messages);
    return data.messages.length;
  }
  if (data.start !== e.end) return 0;       // 不是接着当前位置的(重连/乱序), 丢掉
  e.version = data.version;
  e.end = data.end;
  e.anchor = data.anchor;
  e.bytes += bytes;
  if (!data.messages.length) return 0;
  e.msgs = e.msgs.concat(data.messages);
  if (S.sel !== uid) return data.messages.length;
  const box = $('#msgs');
  if (!box) return data.messages.length;
  const mark = el('span');
  box.appendChild(mark);
  appendMessages(box, data.messages, null);
  for (let n = mark.nextSibling; n; n = n.nextSibling) markMatches(n);
  mark.remove();
  const c = $('#mcount-total');
  if (c) c.textContent = `${e.msgs.length} 条消息`;
  const mc = $('.mobile-msg-count');
  if (mc) {
    mc.textContent = e.msgs.length;
    mc.setAttribute('aria-label', `${e.msgs.length} 条消息`);
  }
  newBadge(data.messages.length);
  return data.messages.length;
}

/** 兜底用的主动拉取。正常情况下更新由服务端 SSE 推过来, 这里只在
 *  连接还没建起来或断了的时候补一手。 */
async function syncSession(uid) {
  const e = cache.get(uid);
  if (!e || S.syncing) return 0;
  S.syncing = true;
  try {
    const { data, bytes } = await fetchMessages(uid, {
      start: e.end, head: e.version.head, anchor: e.anchor });
    return await applyDiff(uid, data, bytes);
  } catch {
    return 0;
  } finally {
    S.syncing = false;
  }
}

// ---- 服务端推送 ----
// 服务端盯着会话文件, 一变就把 diff 推过来, 不用客户端反复问。
let _es = null, _esUid = null, _esRetry = null;

function watchSession(uid) {
  closeWatch();
  const e = cache.get(uid);
  if (!e || !window.EventSource) return;
  const p = new URLSearchParams({ uid, start: e.end, head: e.version.head, anchor: e.anchor || '' });
  const es = new EventSource(appUrl('api/watch?' + p));
  _es = es;
  _esUid = uid;
  es.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    applyDiff(uid, data);
  };
  es.onerror = () => {
    // EventSource 自带的重连会沿用旧 URL(旧偏移), 所以自己关掉重开, 带上新偏移
    es.close();
    if (_es !== es) return;
    _es = null;
    clearTimeout(_esRetry);
    _esRetry = setTimeout(() => { if (S.sel === uid) watchSession(uid); }, 1500);
  };
}

function closeWatch() {
  clearTimeout(_esRetry);
  if (_es) { _es.close(); _es = null; _esUid = null; }
}

function newBadge(n) {
  const b = $('#newmsg');
  if (!b) return;
  b.textContent = `+${n} 条新消息`;
  b.classList.add('on');
  clearTimeout(b._t);
  b._t = setTimeout(() => b.classList.remove('on'), 4000);
}

/** 兜底轮询: SSE 连着的时候只是很慢地对一下账, 断了才回到自适应的快节奏。 */
function tickSync() {
  if (!S.sel || document.hidden || S.agents) return;
  const pushing = _es && _esUid === S.sel && _es.readyState === 1;
  const gap = pushing ? BACKUP_MS : (S.live.has(S.sel) ? S.syncGap : SYNC_MS);
  if (Date.now() - (S.lastSync || 0) < gap) return;
  S.lastSync = Date.now();
  const uid = S.sel;
  syncSession(uid).then(n => {
    if (uid !== S.sel || pushing) return;
    S.syncGap = n ? FAST_MIN : Math.min(FAST_MAX, Math.round(S.syncGap * 1.5));
  });
}

setInterval(tickSync, TICK_MS);

// ---- 活跃会话 ----
async function refreshLive(force = false) {
  const d = await (await fetch(appUrl('api/live' + (force ? '?force=1' : '')))).json();
  const next = new Set(d.uids);
  const nextTmux = new Set((d.tmux_uids || []).filter(u => next.has(u)));
  const changed = (a, b) => a.size !== b.size || [...a].some(u => !b.has(u));
  if (changed(next, S.live) || changed(nextTmux, S.liveTmux)) {
    S.live = next;
    S.liveTmux = nextTmux;
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
    n.classList.toggle('live', S.live.has(n.dataset.uid));
    n.classList.toggle('live-tmux', S.liveTmux.has(n.dataset.uid));
  }
  const h = $('#dlive');
  if (h) {
    const tmux = S.liveTmux.has(S.sel);
    h.classList.toggle('on', S.live.has(S.sel));
    h.classList.toggle('tmux', tmux);
    h.textContent = tmux ? '● tmux 中' : '● 进行中';
  }
  const termButton = $('#a-term');
  if (termButton) {
    termButton.classList.toggle('session-live', S.live.has(S.sel));
    termButton.classList.toggle('session-tmux', S.liveTmux.has(S.sel));
  }
  const c = $('#livecount');
  if (c) {
    const tmux = S.liveTmux.size, direct = S.live.size - tmux;
    c.innerHTML = S.live.size
      ? `${direct ? `<span class="live-direct">● ${direct}</span>` : ''}`
        + `${tmux ? `<span class="live-tmux-count">● ${tmux}<span class="live-kind"> tmux</span></span>` : ''}` : '';
    c.ariaLabel = `${S.live.size} 个进行中：${direct} 个非 tmux，${tmux} 个 tmux`;
    c.title = '绿色：非 tmux · 蓝色：tmux';
    c.classList.toggle('on', S.live.size > 0);
  }
}

setInterval(pollLive, LIVE_MS);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { closeWatch(); return; }
  pollLive();
  if (S.sel && cache.get(S.sel)) { syncSession(S.sel).then(() => watchSession(S.sel)); }
});

// ---------------------------------------------------------------- 数据加载
function showSessionCount(n) {
  $('#stat').innerHTML = `${n}<span class="stat-unit"> 个会话</span>`;
}

/** 列表元数据变更后同步缓存和当前详情标题，不重绘消息正文。 */
function refreshSessionMeta() {
  for (const s of S.sessions) {
    const e = cache.get(s.uid);
    if (e) e.meta = { ...e.meta, ...s };
  }
  const current = S.sessions.find(s => s.uid === S.sel);
  const title = $('#detail .dhead h2');
  if (current && title && title.textContent.trim() !== current.title) {
    title.innerHTML = `${icon(current.source)} ${esc(current.title)}`;
  }
}

async function loadSessions(force) {
  $('#stat').textContent = force ? ' 重新扫描…' : ' 加载中…';
  const r = await fetch(appUrl('api/sessions' + (force ? '?force=1' : '')));
  const d = await r.json();
  S.sig = d.sig;
  S.sessions = d.sessions;
  refreshSessionMeta();
  renderChips();
  renderSide();
  showSessionCount(d.sessions.length);
}

/** 列表自动跟进磁盘变化。签名没变时服务端只回一个 unchanged, 成本约 3ms。 */
async function pollSessions() {
  if (document.hidden || !S.sig) return;
  try {
    const d = await (await fetch(appUrl('api/sessions?sig=' + encodeURIComponent(S.sig)))).json();
    if (d.unchanged || !d.sessions) return;
    S.sig = d.sig;
    S.sessions = d.sessions;
    refreshSessionMeta();
    renderChips();
    if (S.results) {
      // 搜索结果集合保持不变，只合入 rename 等最新元数据。
      const fresh = new Map(S.sessions.map(s => [s.uid, s]));
      S.results = S.results.map(r => fresh.has(r.uid) ? { ...r, ...fresh.get(r.uid) } : r);
      if (!patchSide(visible())) renderSide();
      return;
    }
    showSessionCount(d.sessions.length);
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
  const pool = (S.results || S.sessions).filter(s => !S.off.has(s.source));
  if (!S.term || S.results) return pool;          // 搜索态下服务端已经筛过
  return pool.filter(s => hasTerm(s.title) || hasTerm(s.cwd));
}

// ---------------------------------------------------------------- 左栏
function renderChips() {
  const box = $('#chips');
  box.innerHTML = '';
  for (const [k, v] of Object.entries(SOURCES)) {
    const n = S.sessions.filter(s => s.source === k).length;
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
  for (const k of keys) m.get(k).sort((a, b) => new Date(b.updated) - new Date(a.updated));
  return keys.map(k => [k, m.get(k)]);
}

const itemMeta = s => [fmtTime(s.updated), fmtSize(s.size), s.model || '',
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
    side.appendChild(el('div', 'empty', S.results ? '没有匹配的会话' : '没有会话'));
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
                              + (S.live.has(s.uid) ? ' live' : '')
                              + (S.liveTmux.has(s.uid) ? ' live-tmux' : ''),
        `<span class="ico">${icon(s.source)}</span>
         <div class="body">
           <div class="t" title="${esc(s.title)}">${hl(s.title)}</div>
           <div class="m">${esc(meta)}</div>
           ${S.view === 'date'
             ? `<div class="cwd" title="${esc(s.cwd)}">${esc(shortCwd(s.cwd))}</div>` : ''}
           ${s.snippet ? `<div class="snip">${hl(s.snippet)}</div>` : ''}
         </div>`);
      it.dataset.uid = s.uid;
      it.onclick = () => openSession(s.uid);
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

async function openSession(uid, agents) {
  showMobileDetail();
  inflight?.abort();            // 连点列表时, 放弃上一个还没回来的请求
  const ac = inflight = new AbortController();
  closeWatch();
  if (typeof T !== 'undefined') {
    if (T.uid && T.uid !== uid) closeTermPane();
    $('#composer').classList.add('hidden');    // 先收起, 渲染完再按新会话的状态决定
  }
  S.sel = uid;
  S.agents = agents ?? false;
  store.set('sel', uid);
  renderSide();

  const hit = S.agents ? null : cacheGet(uid);   // 合并子代理的结果不入缓存
  if (hit) {
    await renderSession(hit.meta, hit.msgs);
    syncSession(uid);                            // 缓存先上屏, 再后台补新消息
    return;
  }

  $('#detail').innerHTML = '<div class="spin">正在读取会话…</div>';
  progress(0, 0, '读取');
  let res;
  try {
    res = await fetchMessages(uid, {
      agents: S.agents, signal: ac.signal,
      onProgress: (a, b) => progress(a, b, '读取'),
    });
  } catch (e) {
    if (e.name === 'AbortError') return;      // 已经切到别的会话了
    progressDone();
    $('#detail').innerHTML = `<div class="empty">读取失败: ${esc(e.message)}</div>`;
    return;
  }
  if (S.sel !== uid) return progressDone();      // 期间点了别的会话
  const { data, bytes } = res;
  if (!S.agents) {
    cachePut(uid, { meta: data.meta, msgs: data.messages, version: data.version,
                    end: data.end, anchor: data.anchor, bytes });
  }
  await renderSession(data.meta, data.messages);
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
async function renderSession(meta, msgs) {
  const uid = meta.uid;
  const d = $('#detail');
  d.innerHTML = '';
  d.appendChild(head(meta, msgs.length));
  const box = el('div', 'msgs');
  box.id = 'msgs';
  d.appendChild(box);
  S.cur = -1; S.autoOpen = 0; S.markCapped = false;

  // 关键: 建在游离的 fragment 里, 最后一次性挂上。
  // 若逐批插入已在文档中的容器, 每批都会触发一次全量 layout, 上万条时是 O(n²) —— 实测 0.2s 变 14s。
  // 批间让出主线程用 setTimeout 而不是 rAF: rAF 会等一次绘制, 又把 layout 成本引回来。
  const plan = planMessages(msgs);
  const frag = document.createDocumentFragment();
  for (let i = 0; i < plan.length; i += RENDER_BATCH) {
    buildPlan(frag, plan.slice(i, i + RENDER_BATCH), null);
    if (i + RENDER_BATCH < plan.length) {
      progress(i + RENDER_BATCH, plan.length, '渲染');
      await new Promise(r => setTimeout(r, 0));
      if (S.sel !== uid) return progressDone();
    }
  }
  box.appendChild(frag);
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
  if (typeof renderComposer === 'function') renderComposer();
  watchSession(meta.uid);          // 之后的更新由服务端推过来
}

function head(m, total) {
  const h = el('div', 'dhead');
  const tmuxLive = S.liveTmux.has(m.uid);
  h.innerHTML = `
    <div class="dtitle">
      <button class="mobile-back" title="返回会话列表" aria-label="返回会话列表">←</button>
      <h2>${icon(m.source)} ${esc(m.title)}</h2>
      <div class="dhead-actions" aria-label="会话操作">
        <span class="mobile-msg-count" aria-label="${total} 条消息">${total}</span>
        ${/* const 声明的全局不会挂到 window 上, 只能这样探 */
          (typeof T !== 'undefined' && T.enabled)
            ? `<button class="iconbtn" id="a-term" title="接管会话" aria-label="接管会话">${uiIcon('terminal')}</button>` : ''}
        <button class="iconbtn mobile-more" id="a-more" title="更多操作" aria-label="更多操作"
          aria-expanded="false">${uiIcon('more')}</button>
        <div class="mobile-action-menu">
          <a class="iconbtn" id="a-export" href="${appUrl(`api/export/${encodeURIComponent(m.uid)}`)}" download
             title="导出 Markdown" aria-label="导出 Markdown">${uiIcon('download')}</a>
          <button class="iconbtn" id="a-fold" title="折叠工具输出" aria-label="折叠工具输出">${uiIcon('fold')}</button>
          ${S.term ? `<span class="mnav"><b id="mcount">…</b>
            <button class="iconbtn" id="m-prev" title="上一处" aria-label="上一处">↑</button>
            <button class="iconbtn" id="m-next" title="下一处" aria-label="下一处">↓</button></span>` : ''}
          ${m.agents ? `<button class="iconbtn${S.agents ? ' on' : ''}" id="a-agents"
            title="${S.agents ? '不合并' : '合并'} ${m.agents} 个子代理" aria-label="${S.agents ? '不合并' : '合并'} ${m.agents} 个子代理"
            aria-pressed="${S.agents}">${uiIcon('agents')}<span class="action-badge">${m.agents}</span></button>` : ''}
          <button class="iconbtn danger" id="a-del" title="删除会话" aria-label="删除会话">${uiIcon('trash')}</button>
        </div>
      </div>
    </div>
    <div class="dmeta">
      <span class="meta-source">${SOURCES[m.source].name}</span>
      <span id="mcount-total">${total} 条消息</span>
      <span id="dlive" class="dlive${S.live.has(m.uid) ? ' on' : ''}${tmuxLive ? ' tmux' : ''}">${tmuxLive ? '● tmux 中' : '● 进行中'}</span>
      <span id="newmsg" class="newmsg"></span>
      <span class="meta-secondary">${esc(fmtTime(m.created))} → ${esc(fmtTime(m.updated))}</span>
      <span class="meta-secondary">${fmtSize(m.size)}</span>
      ${m.model ? `<span class="meta-secondary">${esc(m.model)}</span>` : ''}
      ${m.branch ? `<span class="meta-secondary">⑂ ${esc(m.branch)}</span>` : ''}
      <span class="meta-secondary"><code>${esc(m.cwd)}</code></span>
    </div>`;
  h.querySelector('.mobile-back').onclick = showMobileList;
  const actions = h.querySelector('.dhead-actions');
  const more = h.querySelector('#a-more');
  const closeActions = () => {
    actions.classList.remove('menu-open');
    more.setAttribute('aria-expanded', 'false');
  };
  more.onclick = e => {
    e.stopPropagation();
    const open = actions.classList.toggle('menu-open');
    more.setAttribute('aria-expanded', String(open));
    if (open) setTimeout(() => document.addEventListener('click', e => {
      if (!actions.contains(e.target)) closeActions();
    }, { once: true }), 0);
  };
  h.querySelector('.mobile-action-menu').onclick = e => {
    if (e.target.closest('a, button')) setTimeout(closeActions, 0);
  };
  const ag = h.querySelector('#a-agents');
  if (ag) ag.onclick = () => openSession(m.uid, !S.agents);
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
  const fb = h.querySelector('#a-fold');
  fb.onclick = () => {
    const fold = fb.dataset.state !== 'folded';   // 按状态判断, 不能靠按钮文案
    document.querySelectorAll('.msg.foldable').forEach(n => fold ? n._fold() : n._open());
    fb.dataset.state = fold ? 'folded' : 'open';
    const label = fold ? '展开工具输出' : '折叠工具输出';
    fb.innerHTML = uiIcon(fold ? 'expand' : 'fold');
    fb.title = fb.ariaLabel = label;
  };
  h.querySelector('#a-del').onclick = () => del(m);
  return h;
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
};
// 连续 3 条以上的工具调用/输出合并成一个可折叠的组, 避免刷屏
const TOOL_ROLES = new Set(['tool', 'tool_result']);
const SEARCH_ROLES = new Set(['user', 'assistant', 'user·subagent', 'assistant·subagent', 'thinking']);
const GROUP_MIN = 3;

/** 先算分组(纯计算, 很快), 再分批建 DOM —— 分批不会把一个组切成两半。 */
function planMessages(msgs) {
  const plan = [];
  let run = [];
  const flush = () => {
    if (run.length >= GROUP_MIN) plan.push({ g: run });
    else for (const m of run) plan.push({ m });
    run = [];
  };
  for (const m of msgs) {
    if (TOOL_ROLES.has(m.role)) { run.push(m); continue; }
    flush();
    plan.push({ m });
  }
  flush();
  return plan;
}

function buildPlan(box, plan, before) {
  for (const p of plan) {
    const n = p.g ? groupNode(p.g) : msgNode(p.m);
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

function toolEntry(m) {
  const entry = el('div', 'tool-entry');
  entry.dataset.role = m.role;
  const pre = el('pre');
  const text = m.name ? `${m.name}\n${m.text}` : m.text;
  const paint = full => { pre.textContent = full ? text : clipText(text); };
  paint(false);
  entry.appendChild(pre);
  if (m.media?.length) entry.insertAdjacentHTML('beforeend', mediaGallery(m.media));
  if (text.length > CLIP) {
    const more = el('button', 'more', `展开全文 (${text.length.toLocaleString()} 字符)`);
    more.onclick = () => { pre.classList.remove('clip'); paint(true); more.remove(); };
    pre.classList.add('clip');
    entry.appendChild(more);
  }
  return entry;
}

function groupNode(items) {
  // 工具协议不属于对话正文搜索范围，工具组始终按默认规则折叠。
  const n = el('div', 'msg grp folded');
  n.dataset.role = 'toolgroup';
  const calls = items.filter(m => m.role === 'tool');
  const tally = {};
  calls.forEach(m => { const k = m.name || 'tool'; tally[k] = (tally[k] || 0) + 1; });
  const summary = Object.entries(tally).map(([k, v]) => v > 1 ? `${k} ×${v}` : k).join(' · ');
  const preview = addFoldPreview(n, `🔧 ${calls.length} 次工具调用 · ${summary}`, '工具调用组');
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

function msgNode(m) {
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
  const label = (ROLE_LABEL[m.role] || m.role) + (m.name ? ` · ${m.name}` : '');
  const peek = m.text.replace(/\s+/g, ' ').slice(0, 200);
  const preview = foldable ? addFoldPreview(n, peek, label, found && !hit) : null;
  const body = el('div', 'mb');
  const render = full => (raw
    ? `<pre>${esc(full ? m.text : clipText(m.text))}</pre>`
    : md(m.text, full, m.media)) + mediaGallery(m.media);
  const paint = full => { body.innerHTML = render(full); renderFormulae(body); };
  paint(hit);
  n.appendChild(body);
  const setAction = addAction(n);
  const long = m.text.length > CLIP;
  const fold = () => { n.classList.add('folded'); body.classList.remove('clip'); setAction(); };
  const full = () => {
    n.classList.remove('folded'); body.classList.remove('clip'); paint(true);
    setAction(foldable ? '收起' : '', foldable ? fold : null);
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

const CLIP = 4000;
const AUTO_OPEN_MAX = 40;    // 最多自动展开这么多条命中消息, 其余只标记
const MARK_MAX = 3000;       // 单页高亮节点上限
const clipText = t => t.length > CLIP ? t.slice(0, CLIP) + '\n… (点下方按钮展开全文)' : t;

// 轻量 markdown: 代码块 / 表格 / 列表 / 引用 / 标题 / 行内标记
function md(text, full, media = []) {
  return (full ? text : clipText(text)).split(/```/)
    .map((b, i) => i % 2 ? `<pre>${esc(b.replace(/^[\w+-]*\n/, ''))}</pre>` : blocks(b, media))
    .join('') || '<p></p>';
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

function inline(s, media = []) {
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
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<i>$2</i>')
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
  document.documentElement.style.setProperty('--side-width', w + 'px');
  if (save) store.set('width', w);
}

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
  setSideWidth(store.get('width', SIDE_DEFAULT));
});

$('#q').oninput = e => {
  if (S.results) { S.results = null; }     // 改动输入即退出全文搜索态
  S.filter = S.term = e.target.value.trim();
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

document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== $('#q')) { e.preventDefault(); $('#q').focus(); }
  if (e.key === 'Escape') { $('#q').blur(); }
});

setSideWidth(store.get('width', SIDE_DEFAULT));
renderOpts();
renderView();
pollLive();   // 终端面板由 term.js 自己初始化 (它在本文件之后加载)
loadSessions(false).then(() => {
  const last = store.get('sel', null);       // 恢复上次看的会话
  const restoreDetail = !MOBILE.matches || store.get('mobilePage', 'list') === 'detail';
  if (restoreDetail && last && S.sessions.some(s => s.uid === last)) openSession(last);
});
