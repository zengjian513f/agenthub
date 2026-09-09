'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const base = new URL('.', location.href);
  const params = new URLSearchParams(location.search);
  const context = new URLSearchParams();
  for (const key of ['uid', 'agent', 'ref']) {
    if (params.has(key)) context.set(key, params.get(key));
  }
  let controller;

  function urlFor(path, offset = 0, download = false) {
    const url = new URL(download ? 'api/session/files' : 'files.html', base);
    url.search = context.toString();
    if (path) url.searchParams.set('path', path);
    if (offset) url.searchParams.set('offset', offset);
    if (download) url.searchParams.set('download', '1');
    return url.href;
  }

  function navigation(link, path, offset = 0) {
    const enabled = path !== null;
    link.setAttribute('aria-disabled', String(!enabled));
    if (enabled) {
      link.href = urlFor(path, offset);
      link.dataset.navigate = '1';
    } else {
      link.removeAttribute('href');
      delete link.dataset.navigate;
    }
  }

  function sizeText(size) {
    if (size === null) return '—';
    if (size < 1024) return size + ' B';
    const power = Math.min(Math.floor(Math.log(size) / Math.log(1024)), 4);
    return (size / 1024 ** power).toFixed(1) + ' ' + ['B', 'KiB', 'MiB', 'GiB', 'TiB'][power];
  }

  function render(data) {
    $('machine').textContent = data.hostname || '';
    $('entries').replaceChildren();
    $('breadcrumbs').replaceChildren();
    const segments = data.path.split('/').filter(Boolean);
    document.title = (segments.at(-1) || '/') + ' · 文件浏览 · AgentHub';
    document.querySelector('h1').textContent = segments.at(-1) || '根目录';
    let path = '';
    for (const name of ['/', ...segments]) {
      if (name !== '/') {
        path += '/' + name;
        const divider = document.createElement('span');
        divider.textContent = '/'; divider.setAttribute('aria-hidden', 'true');
        $('breadcrumbs').append(divider);
      }
      const link = document.createElement('a');
      link.textContent = name;
      navigation(link, path || '/');
      if ((path || '/') === data.path) link.setAttribute('aria-current', 'page');
      $('breadcrumbs').append(link);
    }
    navigation($('parent'), data.parent);
    navigation($('previous'), data.offset > 0 ? data.path : null, Math.max(0, data.offset - 500));
    navigation($('next'), data.next_offset !== null ? data.path : null, data.next_offset);
    $('page-info').textContent = data.total ? `${data.offset + 1}–${data.offset + data.entries.length} / ${data.total}` : '0 项';
    const fragment = document.createDocumentFragment();
    for (const entry of data.entries) {
      const row = document.createElement('tr');
      const name = document.createElement('td');
      const link = document.createElement(entry.kind === 'unavailable' ? 'span' : 'a');
      const icon = document.createElement('span');
      icon.className = 'icon'; icon.setAttribute('aria-hidden', 'true');
      icon.textContent = entry.kind === 'directory' ? '📁' : '📄';
      const label = document.createElement('span');
      label.textContent = entry.name;
      link.append(icon, label);
      if (entry.kind === 'directory') navigation(link, entry.path);
      else if (entry.kind === 'file') {
        link.href = urlFor(entry.path, 0, true);
        link.download = entry.name;
        link.title = '下载 ' + entry.name;
      } else link.title = '无法访问或不支持的文件类型';
      name.append(link);
      const size = document.createElement('td'); size.textContent = sizeText(entry.size);
      const modified = document.createElement('td');
      modified.textContent = entry.modified === null ? '—' : new Date(entry.modified * 1000).toLocaleString();
      row.append(name, size, modified); fragment.append(row);
    }
    $('entries').append(fragment);
    $('status').textContent = data.total ? `共 ${data.total} 项，包含隐藏文件` : '此目录为空';
  }

  async function load() {
    controller?.abort();
    const request = controller = new AbortController();
    $('status').className = '';
    $('status').textContent = '正在加载…';
    $('entries').replaceChildren();
    $('page-info').textContent = '';
    for (const id of ['parent', 'previous', 'next']) navigation($(id), null);
    document.querySelector('table').setAttribute('aria-busy', 'true');
    try {
      if (!context.get('uid') || !context.get('ref')) throw new Error('请从会话中的目录链接打开文件浏览器');
      const current = new URL(location.href);
      const url = new URL('api/session/files', base);
      url.search = context.toString();
      for (const key of ['path', 'offset']) {
        if (current.searchParams.has(key)) url.searchParams.set(key, current.searchParams.get(key));
      }
      const response = await fetch(url, {signal: request.signal, cache: 'no-store'});
      if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
        throw new Error('无法读取目录，请确认登录状态后刷新');
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `读取失败（${response.status}）`);
      if (request !== controller) return;
      render(data);
    } catch (error) {
      if (request !== controller) return;
      $('status').className = 'error';
      $('status').textContent = error.message || '无法连接机器，请刷新重试';
      document.querySelector('h1').textContent = '目录无法打开';
    } finally {
      if (request === controller) document.querySelector('table').setAttribute('aria-busy', 'false');
    }
  }

  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-navigate]');
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.pushState(null, '', link.href);
    load();
  });
  $('refresh').addEventListener('click', load);
  addEventListener('popstate', load);
  load();
})();
