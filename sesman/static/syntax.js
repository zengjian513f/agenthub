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

window.sesmanHighlight = (source, rawLanguage = '') => {
  const label = String(rawLanguage || '').trim().toLowerCase();
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
    if (source.length < 12 || source.length > 20000) return null;
    const inferred = inferLanguage(source);
    if (inferred) {
      return {html: hljs.highlight(source, {language: inferred, ignoreIllegals: true}).value,
              language: inferred, detected: true};
    }
    if (!/[{}()[\];=<>]|\b(?:def|class|function|const|let|var|SELECT|FROM|import)\b/.test(source)) return null;
    const result = hljs.highlightAuto(source, autoLanguages);
    const runnerUp = result.secondBest?.relevance || 0;
    const related = new Set([result.language, result.secondBest?.language]);
    const sameFamily = related.has('javascript') && related.has('typescript');
    if (result.relevance < 2 || (!sameFamily && result.relevance - runnerUp < .75)) return null;
    return {html: result.value, language: result.language || '', detected: true};
  } catch {
    return null;
  }
};

dispatchEvent(new Event('sesman-highlight-ready'));
