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
  } catch (error) { host.textContent = error.message || '无法打开文件'; host.classList.add('error'); }
})();
