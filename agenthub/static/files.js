'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const base = new URL('.', location.href);
  const initial = new URLSearchParams(location.search);
  const context = new URLSearchParams();
  for (const key of ['uid', 'agent', 'ref']) if (initial.has(key)) context.set(key, initial.get(key));
  const store = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} };
  const restore = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
  const preferences = {...{sort:'name', order:'asc', view:'list', hidden:true}, ...restore('agenthub-files-view', {})};
  const states = {queued:'等待中', running:'处理中', uploading:'上传中', completed:'已完成', failed:'失败', cancelled:'已取消', interrupted:'已中断'};
  const labels = {mkdir:'新建文件夹', 'new-file':'新建文件', rename:'重命名', copy:'复制', move:'移动', trash:'移入回收站', restore:'还原', purge:'永久删除', compress:'压缩', extract:'解压', bundle:'打包下载', upload:'上传'};
  let data = null, controller, selected = new Set(), anchor = -1, loading = false;
  let clipboard = restore('agenthub-files-clipboard', null);
  let knownJobs = new Map(), lastJobs = [], pollTimer, polling = false, taskSignature = '';
  let uploadDestination = '', resumeJob = null, previewController, trashController;
  const uploads = new Map(), uploading = new Set(), xhrs = new Map(), bundleDownloads = new Set();
  const historyKey = 'agenthub-files-history:' + context.toString();
  if (!history.state?.files) history.replaceState({files:0}, '', location.href);
  let maxHistory = Math.max(history.state.files, restore(historyKey, 0));

  function apiURL(extra = {}, suffix = '') {
    const url = new URL('api/session/files' + suffix, base);
    url.search = context.toString();
    for (const [key, value] of Object.entries(extra)) if (value !== null && value !== undefined) url.searchParams.set(key, value);
    return url.href;
  }
  function pageURL(path, offset = 0) {
    const url = new URL('files.html', base);
    url.search = context.toString();
    if (path) url.searchParams.set('path', path);
    if (offset) url.searchParams.set('offset', offset);
    return url.href;
  }
  async function responseJSON(response) {
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('无法读取响应，请确认登录状态后刷新');
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `请求失败（${response.status}）`);
    return result;
  }
  async function get(extra, signal) { return responseJSON(await fetch(apiURL(extra), {cache:'no-store', signal})); }
  async function post(spec) {
    return responseJSON(await fetch(apiURL({}, '/action'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...Object.fromEntries(context), ...spec})}));
  }
  function status(message, error = false) { $('status').textContent = message; $('status').className = error ? 'error' : ''; }
  function sizeText(size) {
    if (size === null || size === undefined) return '—';
    if (size < 1024) return size + ' B';
    const power = Math.min(Math.floor(Math.log(size) / Math.log(1024)), 4);
    return (size / 1024 ** power).toFixed(1) + ' ' + ['B','KiB','MiB','GiB','TiB'][power];
  }
  function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  function chosen() { return (data?.entries || []).filter(entry => selected.has(entry.path)); }
  function navigation(link, path, offset = 0) {
    link.setAttribute('aria-disabled', String(path === null));
    if (path !== null) { link.href = pageURL(path, offset); link.dataset.navigate = '1'; }
    else { link.removeAttribute('href'); delete link.dataset.navigate; }
  }
  function historyButtons() { $('back').disabled = !history.state?.files; $('forward').disabled = (history.state?.files || 0) >= maxHistory; }
  function navigate(path, offset = 0) {
    const position = (history.state?.files || 0) + 1;
    history.pushState({files:position}, '', pageURL(path, offset));
    maxHistory = position; store(historyKey, maxHistory); load();
  }
  function actionEnabled(action) {
    const count = selected.size, writable = data?.writable && !loading;
    if (action === 'copy') return !!count && !loading;
    if (action === 'download') return !!count && !loading && (writable || (count === 1 && chosen()[0]?.kind === 'file'));
    if (action === 'info' || action === 'open') return count === 1 && !loading;
    if (action === 'paste') return writable && clipboard?.node === data.node_id && !!clipboard.paths?.length;
    if (action === 'rename') return writable && count === 1;
    if (action === 'extract') return writable && count === 1 && chosen()[0]?.name.toLowerCase().endsWith('.zip');
    if (['cut','trash','compress'].includes(action)) return writable && !!count;
    return writable;
  }
  function selectionChanged() {
    for (const row of $('entries').children) {
      const on = selected.has(row.dataset.path);
      row.classList.toggle('selected', on); row.setAttribute('aria-selected', String(on));
      row.querySelector('input').checked = on;
      row.classList.toggle('cut', clipboard?.node === data?.node_id && clipboard.action === 'move' && clipboard.paths.includes(row.dataset.path));
    }
    const count = data?.entries.length || 0;
    $('select-all').checked = count > 0 && selected.size === count;
    $('select-all').indeterminate = selected.size > 0 && selected.size < count;
    const items = chosen(), total = items.reduce((sum, item) => sum + (item.size || 0), 0);
    $('selection-status').textContent = selected.size ? `已选 ${selected.size} 项${items.every(i => i.kind === 'file') ? ' · ' + sizeText(total) : ''}` : `${data?.total || 0} 个项目`;
    for (const button of document.querySelectorAll('[data-action]')) button.disabled = !actionEnabled(button.dataset.action);
  }
  function selectEntry(index, event = {}) {
    const entry = data.entries[index];
    if (!entry) return;
    if (event.shiftKey && anchor >= 0) {
      if (!event.ctrlKey && !event.metaKey) selected.clear();
      for (let i = Math.min(anchor,index); i <= Math.max(anchor,index); i++) selected.add(data.entries[i].path);
    } else if (event.ctrlKey || event.metaKey || event.target?.matches('input')) {
      selected.has(entry.path) ? selected.delete(entry.path) : selected.add(entry.path); anchor = index;
    } else { selected = new Set([entry.path]); anchor = index; }
    selectionChanged();
  }
  function renderEntries() {
    $('file-table').className = preferences.view === 'list' ? '' : preferences.view;
    const fragment = document.createDocumentFragment();
    for (const [index, entry] of data.entries.entries()) {
      const row = element('tr', undefined, 'entry'); row.dataset.path = entry.path; row.dataset.index = index; row.tabIndex = -1; row.draggable = true;
      const check = element('input'); check.type = 'checkbox'; check.setAttribute('aria-label', '选择 ' + entry.name);
      const checkCell = element('td'); checkCell.append(check);
      const cell = element('td'), link = element('a', undefined, 'entry-link');
      link.href = entry.kind === 'directory' ? pageURL(entry.path) : apiURL({path:entry.path, download:1});
      link.title = entry.name; link.tabIndex = -1;
      const icon = element('span', undefined, 'entry-icon ' + (entry.kind === 'directory' ? 'folder' : 'file'));
      icon.innerHTML = entry.kind === 'directory'
        ? '<svg viewBox="0 0 24 24"><path fill="#dca735" d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10H2z"/><path fill="#f6cb63" d="M2 9h20v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z"/></svg>'
        : '<svg viewBox="0 0 24 24"><path fill="var(--bg)" stroke="#7195c1" d="M5 2h9l5 5v15H5z"/><path fill="#d8e7fa" stroke="#7195c1" d="M14 2v5h5"/><path stroke="#9bb2ce" d="M8 11h8M8 15h8M8 19h6"/></svg>';
      icon.setAttribute('aria-hidden', 'true');
      if (preferences.view === 'grid' && /\.(png|jpe?g|gif|webp|avif|bmp|mp4|webm|mov)$/i.test(entry.name)) {
        const thumb = element('img', undefined, 'thumbnail'); thumb.alt = ''; thumb.loading = 'lazy';
        thumb.src = apiURL({mode:'thumbnail', path:entry.path});
        thumb.onerror = () => { thumb.remove(); };
        icon.append(thumb);
      }
      if (entry.symlink) icon.append(element('span', '↗', 'link-badge'));
      link.append(icon, element('span', entry.name, 'entry-name')); cell.append(link);
      row.append(checkCell, cell, element('td', entry.kind === 'directory' ? '文件夹' : entry.type || '文件'), element('td', sizeText(entry.size)), element('td', entry.modified === null ? '—' : new Date(entry.modified*1000).toLocaleString()));
      fragment.append(row);
    }
    $('entries').replaceChildren(fragment); selectionChanged();
  }
  function render(result) {
    data = result; selected.clear(); anchor = -1;
    $('machine').textContent = result.hostname + (result.writable ? '' : ' · 只读');
    const segments = result.path.split('/').filter(Boolean), title = segments.at(-1) || '根目录';
    document.title = title + ' · 文件管理 · AgentHub'; document.querySelector('h1').textContent = title;
    $('breadcrumbs').replaceChildren(); let path = '';
    for (const [index,name] of ['根目录', ...segments].entries()) {
      if (index) { path += '/' + name; $('breadcrumbs').append(element('span','›')); }
      const link = element('a', name); navigation(link, path || '/');
      if ((path || '/') === result.path) link.setAttribute('aria-current','page');
      $('breadcrumbs').append(link);
    }
    $('breadcrumbs').scrollLeft = $('breadcrumbs').scrollWidth;
    $('address').value = result.path;
    navigation($('parent'), result.parent);
    navigation($('previous'), result.offset > 0 ? result.path : null, Math.max(0,result.offset-500));
    navigation($('next'), result.next_offset !== null ? result.path : null, result.next_offset);
    $('page-info').textContent = result.total ? `${result.offset + 1}–${result.offset + result.entries.length} / ${result.total}` : '0 项';
    $('empty').hidden = result.total !== 0;
    renderEntries();
    status(result.total ? `共 ${result.total} 项${preferences.hidden ? '，包含隐藏文件' : ''}` : '此目录为空');
  }
  async function load() {
    controller?.abort(); const request = controller = new AbortController(); loading = true;
    $('address-form').hidden = true; $('context-menu').hidden = true;
    status('正在加载…'); $('entries').replaceChildren(); $('empty').hidden = true;
    for (const id of ['parent','previous','next']) navigation($(id), null);
    $('file-table').setAttribute('aria-busy','true'); selectionChanged(); historyButtons();
    try {
      if (!context.get('uid') || !context.get('ref')) throw new Error('请从会话中的目录链接打开文件管理器');
      const params = new URLSearchParams(location.search);
      const result = await get({path:params.get('path'), offset:params.get('offset') || 0, sort:preferences.sort, order:preferences.order, hidden:preferences.hidden ? 1 : 0}, request.signal);
      if (controller !== request) return;
      loading = false; render(result); $('workspace').scrollTop = 0;
      if (!pollTimer && !polling) poll();
    } catch (error) {
      if (controller !== request) return;
      status(error.message, true); selected.clear(); data = null;
      const requested = new URLSearchParams(location.search).get('path');
      if (requested?.startsWith('/')) navigation($('parent'), requested.replace(/\/+$/, '').split('/').slice(0,-1).join('/') || '/');
    } finally {
      if (controller === request) { loading = false; $('file-table').setAttribute('aria-busy','false'); selectionChanged(); }
    }
  }

  function ask(title, description, fields = [], confirm = '确定') {
    return new Promise(resolve => {
      const dialog = $('action-dialog');
      $('action-title').textContent = title; $('action-description').textContent = description; $('action-fields').replaceChildren(); $('action-error').textContent = ''; $('action-submit').textContent = confirm;
      for (const field of fields) {
        const label = element('label',field.label); label.htmlFor = 'field-' + field.name;
        const input = element(field.options ? 'select' : 'input'); input.id = label.htmlFor; input.name = field.name;
        if (field.options) for (const [value,text] of field.options) { const option = element('option',text); option.value = value; input.append(option); }
        else input.required = field.required !== false;
        input.value = field.value || ''; $('action-fields').append(label,input);
      }
      let answer = null;
      $('action-form').onsubmit = event => { event.preventDefault(); answer = Object.fromEntries(new FormData($('action-form'))); dialog.close(); };
      dialog.addEventListener('close', () => resolve(answer), {once:true});
      dialog.showModal();
      const input = dialog.querySelector('input'); if (input) { input.focus(); input.select(); }
    });
  }
  const conflictField = () => ({name:'conflict', label:'遇到同名项目', value:'error', options:[['error','停止该项并提示'],['keep','保留两份（自动编号）'],['skip','跳过'],['replace','覆盖（原项目移入回收站）']]});
  async function submit(spec, showTasks = true) {
    const {job} = await post(spec); knownJobs.set(job.id,'queued');
    if (spec.action === 'bundle') bundleDownloads.add(job.id);
    if (showTasks && !$('tasks-dialog').open) $('tasks-dialog').showModal();
    clearTimeout(pollTimer); pollTimer = null; poll(); return job;
  }
  function download(url, name = '') { const link = element('a'); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove(); }
  async function transfer(paths, destination, action) {
    const answer = await ask(action === 'move' ? '移动项目' : '复制项目', `${paths.length} 个项目 → ${destination}`, [conflictField()]);
    if (answer) await submit({action, paths, destination, ...answer});
  }
  async function perform(action) {
    if (!actionEnabled(action)) return;
    const items = chosen(), paths = items.map(item => item.path), directory = data.path;
    try {
      if (action === 'open') return openEntry(items[0]);
      if (action === 'info') return preview(items[0], true);
      if (action === 'copy' || action === 'cut') {
        clipboard = {node:data.node_id, action:action === 'cut' ? 'move' : 'copy', paths};
        store('agenthub-files-clipboard',clipboard); selectionChanged(); status(`已${action === 'cut' ? '剪切' : '复制'} ${paths.length} 项`); return;
      }
      if (action === 'paste') return await transfer(clipboard.paths, directory, clipboard.action);
      if (action === 'upload') { uploadDestination = directory; $('upload-input').value = ''; $('upload-input').click(); return; }
      if (action === 'download') {
        if (items.length === 1 && items[0].kind === 'file') download(apiURL({path:paths[0], download:1}), items[0].name);
        else await submit({action:'bundle', paths});
        return;
      }
      if (action === 'new') {
        const answer = await ask('新建', directory, [{name:'action',label:'类型',value:'mkdir',options:[['mkdir','文件夹'],['new-file','空文件']]},{name:'name',label:'名称',value:'新建文件夹'}]);
        if (answer) await submit({...answer,destination:directory}); return;
      }
      if (action === 'rename') {
        const answer = await ask('重命名',items[0].name,[{name:'name',label:'新名称',value:items[0].name},conflictField()]);
        if (answer) await submit({action,paths,...answer}); return;
      }
      if (action === 'trash') {
        if (await ask('移入回收站',`删除选中的 ${paths.length} 个项目？可以从文件回收站还原。`,[],'移入回收站')) await submit({action,paths}); return;
      }
      if (action === 'compress') {
        const answer = await ask('压缩为 ZIP',`打包 ${paths.length} 个项目`,[{name:'name',label:'压缩文件名',value:(items.length === 1 ? items[0].name : 'files') + '.zip'},conflictField()]);
        if (answer) await submit({action,paths,destination:directory,...answer}); return;
      }
      if (action === 'extract') {
        const answer = await ask('解压 ZIP',`在当前目录创建 ${items[0].name.replace(/\.zip$/i,'')} 文件夹。`,[conflictField()]);
        if (answer) await submit({action,paths,destination:directory,...answer});
      }
    } catch (error) { status(error.message,true); }
  }
  function openEntry(entry) {
    if (entry.kind === 'directory') navigate(entry.path);
    else if (entry.kind === 'file') preview(entry);
    else status('无法打开此项目，请检查权限或链接目标',true);
  }
  async function preview(entry, properties = false) {
    const dialog = $('preview-dialog'); previewController?.abort();
    const request = previewController = new AbortController();
    $('preview-title').textContent = (properties ? '属性 · ' : '') + entry.name; $('preview-content').textContent = '正在加载…';
    $('preview-download').hidden = entry.kind !== 'file'; $('preview-download').href = apiURL({path:entry.path,download:1}); $('preview-download').download = entry.name;
    if (!dialog.open) dialog.showModal();
    try {
      const info = await get({mode:'info',path:entry.path},request.signal);
      if (previewController !== request || !dialog.open) return;
      const box = $('preview-content'); box.replaceChildren();
      if (properties) {
        const list = element('dl',undefined,'properties');
        for (const [label,value] of [['名称',info.name],['路径',info.path],['类型',info.kind === 'directory' ? '文件夹' : info.mime],['大小',info.kind === 'directory' ? '—' : sizeText(info.size)],['修改时间',new Date(info.modified*1000).toLocaleString()],['权限',info.mode],['所有者 UID',info.owner],['所属组 GID',info.group],['链接目标',info.link_target]]) {
          if (value !== undefined) list.append(element('dt',label),element('dd',String(value)));
        }
        box.append(list);
      } else if (info.preview === 'text') {
        if (info.truncated) box.append(element('p','仅预览前 1 MiB，完整内容请下载。','muted'));
        const pre = element('pre'), code = element('code'); code.textContent = info.text;
        const highlighted = window.agenthubHighlight?.(info.text,'',entry.path); if (highlighted?.html) code.innerHTML = highlighted.html;
        pre.append(code); box.append(pre);
      } else if (info.preview === 'unsupported' || !info.preview) box.append(element('p','此格式暂不支持预览，请下载后打开。'));
      else {
        const tag = info.preview.startsWith('image/') ? 'img' : info.preview.startsWith('video/') ? 'video' : info.preview.startsWith('audio/') ? 'audio' : 'iframe';
        const media = element(tag); media.src = apiURL({mode:'preview',path:entry.path});
        if (tag === 'img') media.alt = entry.name;
        if (tag === 'video' || tag === 'audio') { media.controls = true; media.preload = 'metadata'; }
        if (tag === 'iframe') { media.title = entry.name; media.setAttribute('sandbox',''); }
        media.onerror = () => { box.append(element('p','浏览器无法预览此格式，可以下载后打开。','error')); };
        box.append(media);
      }
    } catch (error) { if (request === previewController && dialog.open) $('preview-content').textContent = error.message; }
  }

  async function poll() {
    if (polling || !context.get('uid') || !data) return;
    polling = true; clearTimeout(pollTimer); pollTimer = null;
    try {
      const result = await get({mode:'jobs'}); lastJobs = result.jobs;
      let refresh = false;
      for (const job of result.jobs) {
        const previous = knownJobs.get(job.id);
        if (previous && previous !== job.state && ['completed','failed','cancelled'].includes(job.state)) {
          refresh = true;
          if (job.state === 'failed') status('文件操作未全部完成，请查看传输任务中的错误',true);
        }
        knownJobs.set(job.id,job.state);
        if (previous && previous !== job.state && job.action === 'move'
            && clipboard?.node === data?.node_id && clipboard?.action === 'move' && job.completed?.length) {
          clipboard.paths = clipboard.paths.filter(path => !job.completed.includes(path));
          if (!clipboard.paths.length) clipboard = null;
          store('agenthub-files-clipboard',clipboard);
        }
        if (job.state === 'completed' && job.artifact && bundleDownloads.has(job.id)) {
          bundleDownloads.delete(job.id); download(apiURL({mode:'artifact',job:job.id}),job.download_name);
        }
      }
      const count = result.jobs.filter(j => ['queued','running','uploading'].includes(j.state)).length;
      $('task-count').textContent = count ? `(${count})` : '';
      renderTasks(result.jobs);
      if (refresh) { await load(); if ($('trash-dialog').open) await loadTrash(); }
    } catch (error) { if ($('tasks-dialog').open) $('tasks-list').textContent = error.message; }
    finally { polling = false; pollTimer = setTimeout(poll, document.hidden ? 6000 : 1500); }
  }
  function renderTasks(jobs) {
    const signature = JSON.stringify(jobs.map(j => [j.id,j.state,j.bytes,j.updated,uploading.has(j.id)]));
    if (signature === taskSignature) return; taskSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const job of jobs) {
      const row = element('section',undefined,'task'); row.dataset.job = job.id;
      const title = element('div',undefined,'task-title'); title.append(element('strong',labels[job.action] || job.action),element('span',states[job.state] || job.state));
      row.append(title,element('small',job.name));
      const active = ['running','queued','uploading'].includes(job.state), progress = element('progress');
      if (job.total_bytes) { progress.max = job.total_bytes; progress.value = Math.min(job.bytes,job.total_bytes); }
      else if (!active) { progress.max = job.items_total; progress.value = job.items_done; }
      row.append(progress,element('small',`${job.items_done}/${job.items_total} 项 · ${sizeText(job.bytes)}${job.total_bytes ? ' / ' + sizeText(job.total_bytes) : ''}`));
      const errors = [job.error,...(job.errors || []).map(e => `${e.path === '@' ? job.name : e.path}: ${e.error}`)].filter(Boolean);
      if (errors.length) row.append(element('p',errors.join('\n'),'task-errors'));
      const actions = element('div',undefined,'task-actions');
      const button = (label,fn) => { const node = element('button',label); node.onclick = async () => { try { await fn(); } catch (e) { status(e.message,true); } }; actions.append(node); };
      if (active) button('取消',async () => { xhrs.get(job.id)?.abort(); await post({action:'cancel',job:job.id}); await poll(); });
      if (job.action === 'upload' && job.state === 'uploading' && !uploading.has(job.id)) button('继续上传',() => resumeUpload(job));
      if (['failed','cancelled','interrupted'].includes(job.state)) button('重试',async () => {
        const answer = await ask('重试任务','已完成的项目不会重复处理。',[conflictField()]); if (!answer) return;
        const result = await post({action:'retry',job:job.id,...answer});
        if (job.action === 'upload') resumeUpload(result.job);
        await poll();
      });
      if (job.artifact && job.state === 'completed') button('下载 ZIP',() => download(apiURL({mode:'artifact',job:job.id}),job.download_name));
      row.append(actions); fragment.append(row);
    }
    $('tasks-list').replaceChildren(fragment);
    if (!jobs.length) $('tasks-list').textContent = '暂无任务';
  }
  async function resumeUpload(job) {
    try {
      const result = await get({mode:'jobs'});
      job = result.jobs.find(item => item.id === job.id) || job;
      if (job.state !== 'uploading') { poll(); return; }
      const file = uploads.get(job.id);
      if (file) sendUpload(job,file);
      else { resumeJob = job; $('resume-input').value = ''; $('resume-input').click(); }
    } catch (error) { status(error.message,true); }
  }
  async function sendUpload(job,file) {
    if (uploading.has(job.id)) return;
    uploading.add(job.id); uploads.set(job.id,file); taskSignature = ''; renderTasks(lastJobs);
    try {
      let offset = job.bytes;
      do {
        const chunk = file.slice(offset,offset + 8*1024*1024);
        const result = await new Promise((resolve,reject) => {
          const xhr = new XMLHttpRequest(); xhrs.set(job.id,xhr);
          xhr.open('POST',apiURL({job:job.id,offset},'/upload')); xhr.setRequestHeader('Content-Type','application/octet-stream'); xhr.responseType = 'json';
          xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) : reject(new Error(xhr.response?.error || '上传失败'));
          xhr.onerror = () => reject(new Error('上传连接中断，可从任务面板继续上传'));
          xhr.onabort = () => reject(new Error('上传已暂停，可从任务面板继续'));
          xhr.send(chunk);
        });
        job = result.job; offset = job.bytes;
        if (job.state !== 'uploading') break;
      } while (offset < file.size);
      if (job.state === 'completed') uploads.delete(job.id);
    } catch (error) { status(error.message,true); }
    finally { uploading.delete(job.id); xhrs.delete(job.id); taskSignature = ''; clearTimeout(pollTimer); pollTimer = null; poll(); }
  }
  async function startUploads(files,destination) {
    if (!files.length) return;
    const answer = await ask('上传文件',`${files.length} 个文件 → ${destination}`,[conflictField()]); if (!answer) return;
    try {
      for (const file of files) {
        const job = await submit({action:'upload',destination,name:file.name,size:file.size,modified:file.lastModified,...answer});
        await sendUpload(job,file);
      }
    } catch (error) { status(error.message,true); }
  }
  async function loadTrash(fresh = false) {
    trashController?.abort(); const request = trashController = new AbortController();
    if (fresh === true) $('trash-list').textContent = '正在加载…';
    try {
      const {items} = await get({mode:'trash'},request.signal);
      if (request !== trashController) return;
      const checked = new Set([...$('trash-list').querySelectorAll('input:checked')].map(input => input.value));
      const fragment = document.createDocumentFragment();
      for (const item of items) {
        const label = element('label'), check = element('input'); check.type = 'checkbox'; check.value = item.id; check.setAttribute('aria-label','选择 ' + item.name);
        check.checked = checked.has(item.id);
        const text = element('span'); text.append(element('strong',item.name),element('small',item.path),element('small',new Date(item.deleted*1000).toLocaleString()));
        label.append(check,text); fragment.append(label);
      }
      $('trash-list').replaceChildren(fragment); if (!items.length) $('trash-list').textContent = '回收站为空';
    } catch (error) { if (request === trashController) $('trash-list').textContent = error.message; }
  }
  async function trashAction(action) {
    const paths = [...$('trash-list').querySelectorAll('input:checked')].map(i => i.value); if (!paths.length) return;
    const answer = await ask(action === 'restore' ? '还原项目' : '永久删除',action === 'restore' ? `将 ${paths.length} 个项目恢复到原路径。` : `永久删除 ${paths.length} 个项目，此操作不能撤销。`,action === 'restore' ? [conflictField()] : [],action === 'restore' ? '还原' : '永久删除');
    if (answer) { try { await submit({action,paths,...answer}); } catch (error) { status(error.message,true); } }
  }

  $('entries').addEventListener('click',event => {
    const row = event.target.closest('.entry'); if (!row) return;
    event.preventDefault(); selectEntry(Number(row.dataset.index),event); row.focus({preventScroll:true});
  });
  $('entries').addEventListener('dblclick',event => { const row = event.target.closest('.entry'); if (row && !event.target.matches('input')) { event.preventDefault(); openEntry(data.entries[Number(row.dataset.index)]); } });
  $('select-all').onchange = () => { selected = new Set($('select-all').checked ? data.entries.map(e=>e.path) : []); selectionChanged(); };
  document.addEventListener('click',event => {
    const close = event.target.closest('[data-close]'); if (close) $(close.dataset.close).close();
    const action = event.target.closest('[data-action]'); if (action) { $('context-menu').hidden = true; perform(action.dataset.action); }
    const link = event.target.closest('a[data-navigate]');
    if (link && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      event.preventDefault(); const url = new URL(link.href); navigate(url.searchParams.get('path'),Number(url.searchParams.get('offset') || 0));
    }
  });
  function showMenu(event) {
    event.preventDefault(); const row = event.target.closest('.entry');
    if (row && !selected.has(row.dataset.path)) selectEntry(Number(row.dataset.index));
    if (!row) { selected.clear(); selectionChanged(); }
    const menu = $('context-menu'); menu.replaceChildren();
    const actions = row ? [['open','打开'],['download','下载'],['cut','剪切'],['copy','复制'],['paste','粘贴'],['rename','重命名'],['trash','移入回收站'],['compress','压缩为 ZIP'],['extract','解压 ZIP'],['info','属性']] : [['new','新建'],['upload','上传'],['paste','粘贴']];
    for (const [action,label] of actions) { const button = element('button',label); button.dataset.action = action; button.disabled = !actionEnabled(action); button.setAttribute('role','menuitem'); menu.append(button); }
    menu.hidden = false; menu.style.left = Math.min(event.clientX,innerWidth-menu.offsetWidth-8)+'px'; menu.style.top = Math.max(8,Math.min(event.clientY,innerHeight-menu.offsetHeight-8))+'px'; menu.querySelector('button:not(:disabled)')?.focus();
  }
  $('workspace').addEventListener('contextmenu',showMenu);
  document.addEventListener('pointerdown',event => { if (!$('context-menu').contains(event.target)) $('context-menu').hidden = true; });
  $('context-menu').onkeydown = event => {
    if (event.key === 'Escape') { $('context-menu').hidden = true; $('workspace').focus(); }
    const buttons = [...$('context-menu').querySelectorAll('button:not(:disabled)')], index = buttons.indexOf(document.activeElement);
    if (['ArrowDown','ArrowUp'].includes(event.key)) { event.preventDefault(); buttons[(index+(event.key === 'ArrowDown' ? 1 : buttons.length-1))%buttons.length]?.focus(); }
  };
  $('workspace').addEventListener('pointerdown',event => {
    if (event.button || event.pointerType === 'touch' || event.target.closest('.entry,thead')) return;
    const start = {x:event.clientX,y:event.clientY}, original = new Set(event.ctrlKey ? selected : []);
    const move = e => {
      const rect = {left:Math.min(start.x,e.clientX),top:Math.min(start.y,e.clientY),right:Math.max(start.x,e.clientX),bottom:Math.max(start.y,e.clientY)};
      const band = $('rubberband'); band.hidden = false; Object.assign(band.style,{left:rect.left+'px',top:rect.top+'px',width:(rect.right-rect.left)+'px',height:(rect.bottom-rect.top)+'px'});
      selected = new Set(original);
      for (const row of $('entries').children) { const box = row.getBoundingClientRect(); if (box.left < rect.right && box.right > rect.left && box.top < rect.bottom && box.bottom > rect.top) selected.add(row.dataset.path); }
      selectionChanged();
    };
    const up = () => { $('rubberband').hidden = true; document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up); };
    selected = original; selectionChanged(); document.addEventListener('pointermove',move); document.addEventListener('pointerup',up,{once:true});
  });
  $('entries').addEventListener('dragstart',event => {
    const row = event.target.closest('.entry'); if (!row) return;
    if (!selected.has(row.dataset.path)) selectEntry(Number(row.dataset.index));
    event.dataTransfer.effectAllowed = 'copyMove'; event.dataTransfer.setData('application/x-agenthub-files',JSON.stringify({node:data.node_id,paths:[...selected]}));
  });
  $('workspace').addEventListener('dragover',event => {
    if (!data?.writable) return; event.preventDefault();
    for (const node of document.querySelectorAll('.drop-target')) node.classList.remove('drop-target');
    const row = event.target.closest('.entry'); if (row && data.entries[Number(row.dataset.index)].kind === 'directory') row.classList.add('drop-target');
    event.dataTransfer.dropEffect = event.ctrlKey || event.dataTransfer.types.includes('Files') ? 'copy' : 'move';
  });
  $('workspace').addEventListener('drop',async event => {
    event.preventDefault(); for (const node of document.querySelectorAll('.drop-target')) node.classList.remove('drop-target'); if (!data?.writable) return;
    const row = event.target.closest('.entry'), entry = row && data.entries[Number(row.dataset.index)];
    const destination = entry?.kind === 'directory' ? entry.path : data.path;
    if (event.dataTransfer.files.length) { startUploads([...event.dataTransfer.files],destination); return; }
    try { const value = JSON.parse(event.dataTransfer.getData('application/x-agenthub-files')); if (value.node !== data.node_id) throw new Error('暂不支持跨机器拖放'); await transfer(value.paths,destination,event.ctrlKey ? 'copy' : 'move'); }
    catch (error) { status(error.message,true); }
  });
  function editAddress() { $('address-form').hidden = false; $('address').value = data?.path || ''; $('address').focus(); $('address').select(); }
  $('edit-address').onclick = editAddress;
  $('address-form').onsubmit = event => { event.preventDefault(); navigate($('address').value.trim()); };
  $('address').onkeydown = event => { if (event.key === 'Escape') { $('address-form').hidden = true; $('workspace').focus(); } };
  $('back').onclick = () => history.back(); $('forward').onclick = () => history.forward(); $('refresh').onclick = load;
  addEventListener('popstate',load);
  for (const id of ['sort','view','hidden']) {
    if (id === 'hidden') $(id).checked = preferences.hidden; else $(id).value = preferences[id];
    $(id).onchange = () => { preferences[id] = id === 'hidden' ? $(id).checked : $(id).value; store('agenthub-files-view',preferences); if (id === 'view' && data) renderEntries(); else navigate(data?.path || '',0); };
  }
  function orderLabel() { $('order').textContent = preferences.order === 'asc' ? '升序 ↑' : '降序 ↓'; }
  orderLabel(); $('order').onclick = () => { preferences.order = preferences.order === 'asc' ? 'desc' : 'asc'; orderLabel(); store('agenthub-files-view',preferences); navigate(data?.path || '',0); };
  for (const th of document.querySelectorAll('th[data-sort]')) th.onclick = () => { if (preferences.sort === th.dataset.sort) $('order').click(); else { $('sort').value = th.dataset.sort; $('sort').dispatchEvent(new Event('change')); } };
  document.addEventListener('keydown',event => {
    if (document.querySelector('dialog[open]') || event.target.closest('input,textarea,select,[contenteditable]')) return;
    const ctrl = event.ctrlKey || event.metaKey, key = event.key.toLowerCase();
    if ((ctrl && key === 'l') || key === 'f4') { event.preventDefault(); editAddress(); return; }
    if (ctrl && key === 'a') { event.preventDefault(); selected = new Set(data?.entries.map(e=>e.path) || []); selectionChanged(); return; }
    const action = ctrl ? ({c:'copy',x:'cut',v:'paste'}[key]) : ({f2:'rename',delete:'trash',enter:'open'}[key]);
    if (action) { event.preventDefault(); perform(action); return; }
    if (key === 'f5') { event.preventDefault(); load(); }
    if (event.altKey && key === 'arrowup' && data?.parent) { event.preventDefault(); navigate(data.parent); }
    if (event.altKey && ['arrowleft','arrowright'].includes(key)) { event.preventDefault(); $(key === 'arrowleft' ? 'back' : 'forward').click(); }
    if (!event.altKey && ['arrowup','arrowdown'].includes(key) && data?.entries.length) {
      event.preventDefault(); const current = Number(document.activeElement.closest('.entry')?.dataset.index ?? anchor);
      const index = Math.max(0,Math.min(data.entries.length-1,current+(key === 'arrowdown' ? 1 : -1)));
      selectEntry(index,event); $('entries').children[index].focus();
    }
    if (key === 'escape') { $('context-menu').hidden = true; selected.clear(); selectionChanged(); }
  });
  $('action-cancel').onclick = () => $('action-dialog').close();
  $('preview-dialog').addEventListener('close',() => { previewController?.abort(); $('preview-content').replaceChildren(); });
  $('tasks-open').onclick = () => { $('tasks-dialog').showModal(); poll(); };
  $('trash-open').onclick = () => { $('trash-dialog').showModal(); loadTrash(true); };
  $('trash-refresh').onclick = () => loadTrash(); $('trash-restore').onclick = () => trashAction('restore'); $('trash-purge').onclick = () => trashAction('purge');
  $('upload-input').onchange = () => startUploads([...$('upload-input').files],uploadDestination);
  $('resume-input').onchange = () => {
    const file = $('resume-input').files[0]; if (!file || !resumeJob) return;
    if (file.name !== resumeJob.upload_name || file.size !== resumeJob.total_bytes || file.lastModified !== resumeJob.upload_modified) { status('请重新选择原上传文件（名称、大小和修改时间须一致）',true); return; }
    sendUpload(resumeJob,file);
  };
  addEventListener('storage',event => { if (event.key === 'agenthub-files-clipboard') { clipboard = restore(event.key,null); selectionChanged(); } });
  addEventListener('beforeunload',event => { if (uploading.size) { event.preventDefault(); event.returnValue = ''; } });
  load();
})();
