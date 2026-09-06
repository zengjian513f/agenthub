import hljs from './vendor/highlight/core.min.js';
import python from './vendor/highlight/languages/python.min.js';
import javascript from './vendor/highlight/languages/javascript.min.js';
import typescript from './vendor/highlight/languages/typescript.min.js';
import bash from './vendor/highlight/languages/bash.min.js';
import json from './vendor/highlight/languages/json.min.js';
import css from './vendor/highlight/languages/css.min.js';
import xml from './vendor/highlight/languages/xml.min.js';
import markdown from './vendor/highlight/languages/markdown.min.js';
import sql from './vendor/highlight/languages/sql.min.js';
import yaml from './vendor/highlight/languages/yaml.min.js';
import diff from './vendor/highlight/languages/diff.min.js';

const grammars = {python, javascript, typescript, bash, json, css, xml, markdown, sql, yaml, diff};
for (const [name, grammar] of Object.entries(grammars)) hljs.registerLanguage(name, grammar);

const aliases = {
  py: 'python', python3: 'python', js: 'javascript', jsx: 'javascript', node: 'javascript',
  ts: 'typescript', tsx: 'typescript', sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
  jsonc: 'json', html: 'xml', htm: 'xml', svg: 'xml', md: 'markdown', yml: 'yaml', patch: 'diff',
};
const plain = new Set(['text', 'txt', 'plain', 'plaintext', 'none']);
const autoLanguages = ['python', 'javascript', 'typescript', 'bash', 'json', 'css', 'xml', 'sql', 'yaml'];
const extensionLanguages = new Map(Object.entries({
  py: 'python', pyw: 'python', js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'typescript', sh: 'bash', bash: 'bash', zsh: 'bash', json: 'json', jsonc: 'json',
  css: 'css', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', md: 'markdown', markdown: 'markdown',
  sql: 'sql', yaml: 'yaml', yml: 'yaml', diff: 'diff', patch: 'diff', txt: 'text', log: 'text',
}));

function languageForPath(rawPath = '') {
  const path = String(rawPath || '').split(/[?#]/, 1)[0].replace(/\\/g, '/');
  const name = path.slice(path.lastIndexOf('/') + 1).toLowerCase();
  if (/^(?:dockerfile|containerfile)(?:\.|$)/.test(name)) return 'bash';
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? (extensionLanguages.get(name.slice(dot + 1)) || '') : '';
}

function detectionSource(source) {
  let text = String(source || '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
  // Codex 工具回执常在真正内容前包一层 Script completed / Output
  // 信封；Read 输出又可能带行号和箭头。检测时去掉它们，显示仍保留原文。
  const envelope = text.match(/(?:^|\n)(?:Output|stdout|content):\s*\n([\s\S]*)$/i);
  if (envelope) text = envelope[1];
  return text.replace(/^\s*(?:\d+\s*[\u2192│|]\s?|\d+:\s+)/gm, '');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>]/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[char]));
}

function shellCommandHighlight(source) {
  const tokens = String(source || '').match(/\s+|&&|\|\||>>?|<<?|[|;()]|"(?:\\.|[^"\\])*"|'[^']*'|[^\s|;&()<>]+/g) || [];
  let expectCommand = true;
  let first = true;
  const mark = (name, value) => `<span class="hljs-${name}">${escapeHtml(value)}</span>`;
  const html = tokens.map(token => {
    if (/^\s+$/.test(token)) return escapeHtml(token);
    if (first && /^(?:\$|#|❯)$/.test(token)) {
      first = false;
      return mark('meta', token);
    }
    first = false;
    if (/^(?:&&|\|\||>>?|<<?|[|;()])$/.test(token)) {
      expectCommand = !/^[()]$/.test(token);
      return mark('keyword', token);
    }
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(token) && expectCommand) return mark('variable', token);
    if (expectCommand && !token.startsWith('-')) {
      expectCommand = false;
      return mark('title', token);
    }
    if (/^-{1,2}[A-Za-z0-9]/.test(token)) return mark('attr', token);
    if (/^(?:"[\s\S]*"|'[\s\S]*')$/.test(token)) return mark('string', token);
    if (/\$[{A-Za-z_]/.test(token)) return mark('variable', token);
    if (/(?:^|\/)\.?[\w@+-]+\.[A-Za-z0-9]+$/.test(token) || token.includes('/')) {
      return mark('string', token);
    }
    return escapeHtml(token);
  }).join('');
  return {html, language: 'bash', detected: false};
}

function lineStartLanguage(line) {
  const text = line.trimStart();
  if (/^(?:\$|❯)\s+\S/.test(text)
      || /^(?:#!.*\b(?:ba|z|k)?sh\b|(?:sudo\s+)?(?:git|cd|ls|rg|grep|find|curl|wget|npm|pnpm|yarn|python\d*|node|docker|systemctl)\b)/.test(text)) {
    return 'bash';
  }
  if (/^(?:interface|type|enum|namespace)\s+\w+/.test(text)) return 'typescript';
  if (/^(?:const|let|var|function|export\s+|import\s+.+\s+from\b)/.test(text)) return 'javascript';
  if (/^(?:@\w|(?:async\s+)?(?:def|class)\s+\w|from\s+\S+\s+import\b|import\s+[\w.]+(?:\s+as\s+\w+)?\s*(?:#.*)?$)/.test(text)) {
    return 'python';
  }
  if (/^(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b/i.test(text)) return 'sql';
  if (/^(?:<\?xml\b|<!DOCTYPE\b|<[A-Za-z][\w:-]*(?:\s|>|\/))/.test(text)) return 'xml';
  return '';
}

function inferLanguage(source) {
  const text = source.trim();
  if (!text) return '';
  if (/^(?:diff --git\b|@@\s+-\d|---\s+\S+\n\+\+\+\s+\S+)/m.test(text)) return 'diff';
  if (/^[{[]/.test(text)) {
    try { JSON.parse(text); return 'json'; } catch { /* 继续识别其他语言 */ }
  }
  if (/^\s*(?:async\s+)?(?:def|class|for|while|if|elif|with|try|except)\b.*:\s*$/m.test(text)
      || /^\s*(?:from\s+\S+\s+import|import\s+[\w.]+)\b/m.test(text)) return 'python';
  if (/^\s*(?:interface|type|enum|namespace)\s+\w+/m.test(text)
      || /:\s*(?:string|number|boolean|unknown|never)(?:\W|$)/.test(text)) return 'typescript';
  if (/^\s*(?:const|let|var|function|export|import\s+.+\s+from)\b/m.test(text)
      || /=>|\b(?:document|window)\./.test(text)) return 'javascript';
  if (/^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b/im.test(text)
      && /\b(?:FROM|INTO|TABLE|SET|JOIN|AS)\b/i.test(text)) return 'sql';
  if (/^\s*(?:#!.*\b(?:ba|z|k)?sh\b|(?:[$#]\s*)?(?:sudo\s+)?(?:git|cd|ls|rg|grep|find|curl|wget|npm|pnpm|yarn|python\d*|node|docker|systemctl)\b)/m.test(text)
      || /^\s*(?:if|then|fi|for|do|done)\b/m.test(text)) return 'bash';
  if (/^\s*(?:<\?xml\b|<!DOCTYPE\b|<[A-Za-z][\w:-]*(?:\s|>|\/))/i.test(text)) return 'xml';
  if (/^[^{}\n]+\{\s*$/.test(text.split('\n')[0])
      && /^\s*[\w-]+\s*:\s*[^/].*;?\s*$/m.test(text)) return 'css';
  if ((text.match(/^\s*[\w.-]+\s*:\s*\S.*$/gm) || []).length >= 2) return 'yaml';
  return '';
}

function highlight(source, rawLanguage = '', rawPath = '') {
  const label = String(rawLanguage || languageForPath(rawPath) || '').trim().toLowerCase();
  if (plain.has(label)) return null;
  const language = aliases[label] || label;
  try {
    if (language) {
      if (!hljs.getLanguage(language)) return null;
      return {html: hljs.highlight(source, {language, ignoreIllegals: true}).value,
              language, detected: false};
    }
    // 先用可解释的强特征判断常见语言。highlightAuto 的 JS/TS、JSON/JS
    // 经常同分，旧的“领先 1.5”条件使它们实际永远不会高亮。
    if (source.length < 12 || source.length > 20000 || /\x1b\[/.test(source)) return null;
    const sample = detectionSource(source);
    const inferred = inferLanguage(sample);
    if (inferred) {
      return {html: hljs.highlight(source, {language: inferred, ignoreIllegals: true}).value,
              language: inferred, detected: true};
    }
    if (!/[{}()[\];=<>]|\b(?:def|class|function|const|let|var|SELECT|FROM|import)\b/.test(sample)) return null;
    const result = hljs.highlightAuto(sample, autoLanguages);
    const runnerUp = result.secondBest?.relevance || 0;
    const related = new Set([result.language, result.secondBest?.language]);
    const sameFamily = related.has('javascript') && related.has('typescript');
    if (result.relevance < 2 || (!sameFamily && result.relevance - runnerUp < .75)) return null;
    const detected = result.language || '';
    return {html: hljs.highlight(source, {language: detected, ignoreIllegals: true}).value,
            language: detected, detected: true};
  } catch {
    return null;
  }
}

/**
 * 工具输出不是一个源文件：一块终端文本里可能先有 shell 命令，随后又
 * 打印 Python/JS。按空行和强语言起始行分岛，无法确认的普通日志原样保留。
 */
function highlightSegments(source, rawPath = '') {
  const text = String(source || '');
  if (!text || /\x1b\[/.test(text)) return null;
  const pathLanguage = languageForPath(rawPath);
  if (pathLanguage) return plain.has(pathLanguage) ? null : highlight(text, pathLanguage);

  const lines = text.match(/[^\n]*(?:\n|$)/g)?.filter(Boolean) || [];
  const chunks = [];
  let current = null;
  const flush = () => {
    if (current?.text) chunks.push(current);
    current = null;
  };
  const append = (kind, language, line) => {
    if (!current || current.kind !== kind || current.language !== language) {
      flush();
      current = {kind, language, text: ''};
    }
    current.text += line;
  };

  for (const line of lines) {
    const bare = line.replace(/\n$/, '');
    if (!bare.trim()) {
      append('plain', '', line);
      flush();
      continue;
    }
    const language = lineStartLanguage(bare);
    if (language) {
      append('code', language, line);
    } else if (current?.kind === 'code') {
      // 缩进正文、续行以及代码块里的普通表达式跟随已经确认的语言。
      current.text += line;
    } else {
      append('plain', '', line);
    }
  }
  flush();

  let painted = 0;
  const languages = new Set();
  const html = chunks.map(chunk => {
    const result = chunk.kind === 'code'
      ? highlight(chunk.text, chunk.language)
      : highlight(chunk.text);
    if (!result?.html) return escapeHtml(chunk.text);
    painted += 1;
    languages.add(result.language);
    return `<span class="syntax-segment hljs language-${result.language}" data-syntax-language="${result.language}">${result.html}</span>`;
  }).join('');
  if (!painted) return null;
  const language = languages.size === 1 ? [...languages][0] : '';
  return {html, language, languages: [...languages], detected: true, segmented: true};
}

window.agenthubLanguageForPath = languageForPath;
window.agenthubHighlight = highlight;
window.agenthubHighlightSegments = highlightSegments;
window.agenthubHighlightShellCommand = shellCommandHighlight;

dispatchEvent(new Event('agenthub-highlight-ready'));
