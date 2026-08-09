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
    // 无标签时只处理适中长度且明显像代码的块；相关性不够或候选太接近就
    // 保持纯文本，避免把日志、自然语言和命令输出染成随机颜色。
    if (source.length < 20 || source.length > 20000
        || !/[{}()[\];=<>]|\b(?:def|class|function|const|let|var|SELECT|FROM|import)\b/.test(source)) return null;
    const result = hljs.highlightAuto(source, autoLanguages);
    const runnerUp = result.secondBest?.relevance || 0;
    if (result.relevance < 4 || result.relevance - runnerUp < 1.5) return null;
    return {html: result.value, language: result.language || '', detected: true};
  } catch {
    return null;
  }
};

dispatchEvent(new Event('sesman-highlight-ready'));
