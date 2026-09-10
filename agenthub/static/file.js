'use strict';
(async () => {
  const base = new URL('.', location.href), context = new URLSearchParams(location.search);
  const host = document.getElementById('file-content');
  const api = (extra = {}) => {
    const url = new URL(context.has('path') ? 'api/session/files' : 'api/session/file', base);
    for (const key of ['uid', 'agent', 'ref', 'path']) if (context.has(key)) url.searchParams.set(key, context.get(key));
    for (const [key, value] of Object.entries(extra)) url.searchParams.set(key, value);
    return url.href;
  };
  try {
    if (context.get('open') === '1') {
      const response = await fetch(new URL('api/session/resolve-files', base), {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({uid:context.get('uid'), agent:context.get('agent') || '', refs:[context.get('ref')]}),
      });
      if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('请登录后刷新页面');
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '无法打开文件');
      const target = result.targets?.find(item => item.ref === context.get('ref'));
      if (!target && !result.resolved?.[context.get('ref')]) throw new Error('文件不存在、未在会话中提及，或有多个同名文件；请使用完整路径。');
      context.delete('open');
      if (!result.file_browser) { location.replace(api({raw:1})); return; }
      if (target?.kind === 'directory') {
        const url = new URL('files.html', base); url.search = context.toString();
        location.replace(url); return;
      }
      // The file is rendered in this document; no file manager page is loaded.
      const url = new URL(location.href); url.search = context.toString();
      history.replaceState(history.state, '', url);
    }
    const response = await fetch(api({mode:'info'}), {cache:'no-store'});
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('请登录后刷新页面');
    const info = await response.json();
    if (!response.ok) throw new Error(info.error || '无法打开文件');
    if (info.kind === 'directory') {
      const url = new URL('files.html', base); url.search = context.toString();
      location.replace(url); return;
    }
    document.title = info.name + ' · AgentHub';
    document.getElementById('file-title').textContent = info.name;
    document.querySelector('header').hidden = false;
    const download = document.getElementById('file-download');
    download.href = api({download:1}); download.hidden = false;
    host.replaceChildren();
    if (info.preview === 'text') {
      AgentHubFilePreview.textPreview(host, info, (ref, image) => AgentHubFilePreview.documentLink(ref, info,
        (path, media, hash) => {
          if (context.has('path')) {
            if (media) return api({path,mode:'preview'}) + hash;
            const url = new URL('file.html', base); url.search = context.toString();
            url.searchParams.set('path', path); return url.href + hash;
          }
          return api({ref:path, ...(media ? {raw:1} : {})}) + hash;
        }, image));
    } else if (/^(image\/|audio\/|video\/|application\/pdf$)/.test(info.preview || '')) {
      const tag = info.preview.startsWith('image/') ? 'img' : info.preview.startsWith('audio/') ? 'audio' : info.preview.startsWith('video/') ? 'video' : 'iframe';
      const media = document.createElement(tag); media.className = 'file-media';
      media.src = api({mode:'preview'}); media.title = info.name;
      if (tag === 'img') media.alt = info.name;
      if (tag === 'audio' || tag === 'video') { media.controls = true; media.preload = 'metadata'; }
      host.append(media);
    } else host.textContent = '此格式暂不支持预览，请下载后打开。';
  } catch (error) {
    document.title = '无法打开文件 · AgentHub';
    document.getElementById('file-title').textContent = '无法打开文件';
    document.querySelector('header').hidden = false;
    const retry = document.getElementById('file-retry'); retry.hidden = false;
    retry.onclick = () => location.reload();
    host.textContent = error.message || '无法打开文件'; host.classList.add('error');
  }
})();
