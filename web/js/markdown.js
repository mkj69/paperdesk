// Markdown 渲染：marked(GFM) + KaTeX 公式 + DOMPurify 清洗
// 流程：先把代码块/行内代码抽出占位 → 抽出 $$..$$ / \[..\] / \(..\) / $..$ 公式占位
//      → 还原代码 → marked 解析 → 公式占位替换为 KaTeX HTML → DOMPurify（放行 ins/u/mark）
import { marked } from '../vendor/marked.esm.js';
import DOMPurify from '../vendor/purify.es.mjs';
import katex from '../vendor/katex/katex.mjs';

marked.setOptions({ gfm: true, breaks: true });

// 私有区字符做占位，marked 不会改动它们
const M0 = '';
const M1 = '';
const C0 = '';
const C1 = '';

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    const href = node.getAttribute('href');
    if (!href.startsWith('#') && !href.startsWith('/')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  }
});

function renderTex(tex, display) {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: 'html', strict: 'ignore' });
  } catch {
    const s = tex.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    return display ? `<pre class="math-error">${s}</pre>` : `<code>${s}</code>`;
  }
}

export function renderMarkdown(md) {
  if (!md) return '';
  let src = String(md);
  const codes = [];
  const maths = [];
  // 1) 保护代码
  src = src.replace(/```[\s\S]*?(```|$)|`[^`\n]+`/g, (m) => {
    codes.push(m);
    return `${C0}${codes.length - 1}${C1}`;
  });
  const pushMath = (tex, display) => {
    maths.push({ tex, display });
    return `${M0}${maths.length - 1}${M1}`;
  };
  // 2) 公式
  src = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => pushMath(t, true));
  src = src.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => pushMath(t, true));
  src = src.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => pushMath(t, false));
  src = src.replace(/(^|[^\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])/g, (m, pre, t) => pre + pushMath(t, false));
  // 3) 还原代码
  src = src.replace(new RegExp(`${C0}(\\d+)${C1}`, 'g'), (m, i) => codes[+i]);
  // 4) markdown
  let html = marked.parse(src);
  // 5) 公式
  html = html.replace(new RegExp(`${M0}(\\d+)${M1}`, 'g'), (m, i) => {
    const it = maths[+i];
    return it ? renderTex(it.tex, it.display) : m;
  });
  // 6) 清洗
  return DOMPurify.sanitize(html, {
    ADD_TAGS: ['ins', 'u', 'mark'],
    ADD_ATTR: ['target'],
  });
}

/** 渲染到元素上，并给代码块加复制按钮 */
export function setMarkdown(el, md) {
  el.innerHTML = renderMarkdown(md);
  el.classList.add('md');
  return el;
}
