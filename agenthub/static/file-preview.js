'use strict';
(() => {
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function highlight(root) {
    for (const code of root.querySelectorAll('pre > code')) {
      const language = code.dataset.language || code.className.match(/language-([^ ]+)/)?.[1] || '';
      const result = window.agenthubHighlight?.(code.textContent, language, code.dataset.path || '');
      if (result?.html) code.innerHTML = result.html;
    }
  }
  addEventListener('agenthub-highlight-ready', () => highlight(document));

  function textPreview(host, info, resolveLink) {
    const markdown = /\.(md|markdown|mdown)$/i.test(info.name);
    const reader = element('section', undefined, 'file-reader');
    const toolbar = element('div', undefined, 'reader-toolbar');
    const content = element('div', undefined, 'reader-content');
    const note = element('span', '', 'reader-note'); note.setAttribute('role', 'status');
    const button = (label, action) => {
      const node = element('button', label); node.type = 'button'; node.onclick = action;
      toolbar.append(node); return node;
    };
    let source = !markdown, wrap = false;
    const render = () => {
      content.replaceChildren(); content.className = 'reader-content';
      if (source) {
        const pre = element('pre', undefined, 'reader-source');
        const code = element('code', info.text); code.dataset.path = info.path || info.name;
        pre.append(code); pre.classList.toggle('wrap', wrap); content.append(pre);
      } else {
        // Parse Markdown only: file HTML, scripts and event attributes remain text.
        const parser = window.markdownit({html:false, linkify:false, maxNesting:30});
        const renderImage = parser.renderer.rules.image;
        parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
          const token = tokens[index], url = resolveLink?.(token.attrGet('src'), true);
          // Resolve before insertion so the browser never requests a document-relative
          // filesystem path from the application's static asset directory.
          if (!url) return parser.utils.escapeHtml(token.content);
          token.attrSet('src', url); token.attrSet('loading', 'lazy');
          token.attrSet('referrerpolicy', 'no-referrer');
          return renderImage(tokens, index, options, env, renderer);
        };
        content.classList.add('reader-markdown');
        content.innerHTML = parser.render(info.text);
        const ids = new Map();
        for (const heading of content.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
          const slug = heading.textContent.trim().toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu, '').replace(/\s/g, '-');
          const count = ids.get(slug) || 0; ids.set(slug, count + 1);
          heading.id = slug + (count ? '-' + count : '');
        }
        for (const link of content.querySelectorAll('a')) {
          const ref = link.getAttribute('href');
          if (ref.startsWith('#')) {
            link.onclick = event => {
              event.preventDefault();
              let id; try { id = decodeURIComponent(ref.slice(1)); } catch { return; }
              [...content.querySelectorAll('[id]')].find(node => node.id === id)?.scrollIntoView({block:'start'});
            };
          } else {
            const url = resolveLink?.(ref, false);
            if (url) link.href = url; else link.removeAttribute('href');
            link.target = '_blank'; link.rel = 'noopener noreferrer';
          }
        }
        for (const table of content.querySelectorAll('table')) {
          const scroll = element('div', undefined, 'reader-table');
          table.before(scroll); scroll.append(table);
        }
      }
      highlight(content);
      previewButton.hidden = sourceButton.hidden = !markdown;
      previewButton.setAttribute('aria-pressed', String(!source));
      sourceButton.setAttribute('aria-pressed', String(source));
      wrapButton.hidden = !source;
      wrapButton.setAttribute('aria-pressed', String(wrap));
    };
    const previewButton = button('预览', () => { source = false; render(); });
    const sourceButton = button('源码', () => { source = true; render(); });
    const wrapButton = button('自动换行', () => { wrap = !wrap; render(); });
    button('复制源码', async () => {
      try { await navigator.clipboard.writeText(info.text); note.textContent = '已复制'; }
      catch { note.textContent = '复制失败，请在源码视图中选择文本复制'; }
    });
    toolbar.append(note); reader.append(toolbar);
    if (info.truncated) reader.append(element('p', '仅预览前 1 MiB，完整内容请下载。', 'reader-note'));
    reader.append(content); host.append(reader); render();
  }

  // Relative document references remain subject to the caller's existing access checks.
  function documentLink(ref, info, localURL, image = false) {
    if (/^https?:\/\//i.test(ref)) return ref;
    if (!image && /^mailto:/i.test(ref)) return ref;
    if (/^[a-z][a-z\d+.-]*:/i.test(ref) || ref.startsWith('//')) return '';
    try {
      const url = new URL(ref, 'https://file.invalid' + (info.path || '/'));
      return localURL(decodeURIComponent(url.pathname), image, url.hash);
    } catch { return ''; }
  }
  window.AgentHubFilePreview = {textPreview, documentLink};
})();
