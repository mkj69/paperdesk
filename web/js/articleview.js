// 网页文章 / README / 视频 / iframe 阅读器（SPEC §13）
// 与 PdfView 保持同样的接口，阅读页可以无差别地切换；文章当作「只有 1 页」。
// 标注：Range 包裹 <mark class="anno anno-<kind> c-<color>">；锚点 = 规范化纯文本中的 exact/prefix/suffix/start/end。
import DOMPurify from '../vendor/purify.es.mjs';
import katex from '../vendor/katex/katex.mjs';
import { h, icon, truncate } from './ui.js';
import { renderMarkdown } from './markdown.js';
import { splitSentences, chunkLong, detectLang } from './tts.js';
import { hostOf, kindMeta } from './resources.js';

const BLOCK_TAGS = new Set(['P', 'DIV', 'LI', 'UL', 'OL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'FIGURE', 'FIGCAPTION',
  'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'MAIN', 'ASIDE', 'NAV', 'HR', 'BR', 'DL', 'DT', 'DD',
  'IMG', 'DETAILS', 'SUMMARY', 'CAPTION']);
const NO_WRAP_PARENTS = new Set(['TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'UL', 'OL', 'DL', 'COLGROUP']);
const READ_BLOCKS = 'p, li, h1, h2, h3, h4, h5, h6, blockquote, figcaption, pre, td, th, dt, dd, caption, summary';
const REF_HEAD = /^\s*(?:\d+\.?\s*)?(References|Bibliography|Citations?|Footnotes|参考文献|Comments?|评论)\s*$/i;
const CTX = 32;
export const MATH_TOKEN = '〔公式〕';
const MIN_SCALE = 0.75;
const MAX_SCALE = 1.8;

// ---------------------------------------------------------------- 文本索引
/**
 * 把一个元素里的文字规范化成纯文本（空白折叠为一个空格，块级元素之间插入 "\n\n"），
 * 并记住每个字符来自哪个文本节点的哪个偏移，用于 选区 ↔ 偏移 ↔ Range 的互相换算。
 */
export class TextIndex {
  /** @param {Element} root  @param {string} [space]  'page' = 原网页模式（整页文字），默认是正文模式 */
  constructor(root, space = '') {
    this.root = root;
    this.space = space;
    this.doc = root.ownerDocument || document; // 原网页模式下 root 在 iframe 的文档里
    this.build();
  }

  static skip(el) {
    if (el.nodeType !== 1) return false;
    const t = el.tagName;
    if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT' || t === 'TEMPLATE' || t === 'SVG' || t === 'svg') return true;
    return el.classList.contains('ix-skip');
  }

  /** 公式整体当作一个占位词「〔公式〕」（不对应任何文本节点，不会被标注包裹） */
  static isMath(el) {
    return el.nodeType === 1 && (el.classList.contains('katex') || el.classList.contains('katex-display'));
  }

  build() {
    const chars = [];
    const fmap = []; // 扁平存储 [node, off, node, off, ...]；块间虚拟换行存 null, null
    const nodeMap = new Map();
    let lastWs = true;
    const pushBreak = () => {
      while (chars.length && chars[chars.length - 1] === ' ') { chars.pop(); fmap.length -= 2; }
      if (!chars.length) { lastWs = true; return; }
      let n = 0;
      for (let i = chars.length - 1; i >= 0 && chars[i] === '\n' && n < 2; i--) n++;
      while (n < 2) { chars.push('\n'); fmap.push(null, null); n++; }
      lastWs = true;
    };
    const walk = (node) => {
      for (let ch = node.firstChild; ch; ch = ch.nextSibling) {
        if (ch.nodeType === 3) {
          const s = ch.data;
          const r2n = new Int32Array(s.length + 1);
          for (let i = 0; i < s.length; i++) {
            r2n[i] = chars.length;
            const c = s.charCodeAt(i);
            const ws = c === 32 || c === 10 || c === 9 || c === 13 || c === 12 || c === 160;
            if (ws) {
              if (!lastWs) { chars.push(' '); fmap.push(ch, i); lastWs = true; }
            } else {
              chars.push(s[i]);
              fmap.push(ch, i);
              lastWs = false;
            }
          }
          r2n[s.length] = chars.length;
          nodeMap.set(ch, r2n);
        } else if (ch.nodeType === 1) {
          if (TextIndex.skip(ch)) continue;
          if (TextIndex.isMath(ch)) {
            if (!lastWs && chars.length) { chars.push(' '); fmap.push(null, null); }
            for (const c of MATH_TOKEN) { chars.push(c); fmap.push(null, null); }
            lastWs = false;
            continue;
          }
          const block = BLOCK_TAGS.has(ch.tagName);
          if (block) pushBreak();
          walk(ch);
          if (block) pushBreak();
        }
      }
    };
    walk(this.root);
    while (chars.length && /\s/.test(chars[chars.length - 1])) { chars.pop(); fmap.length -= 2; }
    this.text = chars.join('');
    this.fmap = fmap;
    this.nodeMap = nodeMap;
  }

  nodeAt(i) {
    if (i < 0 || i >= this.text.length) return null;
    const n = this.fmap[i * 2];
    return n ? [n, this.fmap[i * 2 + 1]] : null;
  }

  /** DOM 边界点 → 规范化文本偏移 */
  offsetOf(container, offset) {
    if (container.nodeType === 3) {
      const r2n = this.nodeMap.get(container);
      if (r2n) return Math.min(this.text.length, r2n[Math.min(offset, r2n.length - 1)]);
      return this._nextIndexed(container, false);
    }
    const child = container.childNodes[offset];
    if (child) return this._firstIndexedFrom(child);
    return this._nextIndexed(container, true);
  }

  _firstIndexedFrom(node) {
    if (node.nodeType === 3) {
      if (this.nodeMap.has(node)) return Math.min(this.text.length, this.nodeMap.get(node)[0]);
      return this._nextIndexed(node, false);
    }
    const w = this.doc.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) if (this.nodeMap.has(n)) return Math.min(this.text.length, this.nodeMap.get(n)[0]);
    return this._nextIndexed(node, true);
  }

  _nextIndexed(node, skipChildren) {
    const w = this.doc.createTreeWalker(this.root, NodeFilter.SHOW_TEXT);
    w.currentNode = node;
    let n = w.nextNode();
    while (n) {
      if (this.nodeMap.has(n) && !(skipChildren && node.contains(n))) return Math.min(this.text.length, this.nodeMap.get(n)[0]);
      n = w.nextNode();
    }
    return this.text.length;
  }

  /** 偏移区间 → DOM Range（找不到返回 null） */
  rangeFor(start, end) {
    let a = start;
    while (a < end && !this.nodeAt(a)) a++;
    let b = end - 1;
    while (b >= a && !this.nodeAt(b)) b--;
    if (a > b) return null;
    const [n1, o1] = this.nodeAt(a);
    const [n2, o2] = this.nodeAt(b);
    const r = this.doc.createRange();
    try {
      r.setStart(n1, o1);
      r.setEnd(n2, o2 + 1);
    } catch {
      return null;
    }
    return r;
  }

  /** 元素 → 偏移区间 */
  elementSpan(el) {
    const s = this.offsetOf(el, 0);
    const e = this.offsetOf(el, el.childNodes.length);
    return [s, Math.max(s, e)];
  }

  /** 锚点 → 偏移区间：先校验 start/end，再按 exact + prefix/suffix 打分，最后偏移兜底 */
  locate(anchor) {
    if (!anchor) return null;
    const text = this.text;
    const exact = anchor.exact || '';
    if (!exact) return null;
    const { start, end } = anchor;
    // 偏移只在建锚点时的同一种文字空间里有意义（正文模式 / 原网页模式的偏移不同）
    const sameSpace = (anchor.space || '') === this.space;
    if (sameSpace && Number.isInteger(start) && Number.isInteger(end) && text.slice(start, end) === exact) return [start, end];
    const prefix = anchor.prefix || '';
    const suffix = anchor.suffix || '';
    let best = null;
    let bestScore = -Infinity;
    let i = text.indexOf(exact);
    let guard = 0;
    while (i >= 0 && guard++ < 2000) {
      const pre = text.slice(Math.max(0, i - prefix.length), i);
      const suf = text.slice(i + exact.length, i + exact.length + suffix.length);
      let score = commonSuffix(pre, prefix) + commonPrefix(suf, suffix);
      if (sameSpace && Number.isInteger(start)) score -= Math.abs(i - start) / 100000;
      if (score > bestScore) { bestScore = score; best = i; }
      i = text.indexOf(exact, i + 1);
    }
    if (best !== null) return [best, best + exact.length];
    // 空白不敏感再找一次
    const re = flexRegex(exact);
    if (re) {
      const m = re.exec(text);
      if (m) return [m.index, m.index + m[0].length];
    }
    if (sameSpace && Number.isInteger(start) && Number.isInteger(end) && end <= text.length && end > start) return [start, end];
    return null;
  }

  anchorFor(start, end) {
    return {
      type: 'text',
      ...(this.space ? { space: this.space } : {}),
      exact: this.text.slice(start, end),
      prefix: this.text.slice(Math.max(0, start - CTX), start),
      suffix: this.text.slice(end, end + CTX),
      start,
      end,
    };
  }
}

function commonSuffix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
  return n;
}
function commonPrefix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}
function flexRegex(s) {
  const t = s.replace(/\s+/g, ' ').trim();
  if (!t) return null;
  try {
    return new RegExp(t.split(' ').map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+'));
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- 公式
function renderTex(tex, display) {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: 'htmlAndMathml', strict: 'ignore' });
  } catch {
    return null;
  }
}

const MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(^|[^\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])/g;

/** 把元素内文本节点里的 $…$ / $$…$$ / \(...\) / \[...\] 渲染成 KaTeX（跳过 pre/code/a） */
export function renderMathIn(root) {
  const doc = root.ownerDocument || document;
  const w = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && n.parentElement.closest('pre, code, .katex, a, textarea') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  let n;
  while ((n = w.nextNode())) if (/\$|\\\(|\\\[/.test(n.data)) nodes.push(n);
  for (const node of nodes) {
    const s = node.data;
    MATH_RE.lastIndex = 0;
    let m;
    let last = 0;
    const frag = doc.createDocumentFragment();
    let changed = false;
    while ((m = MATH_RE.exec(s))) {
      let tex;
      let display = false;
      let lead = '';
      if (m[1] !== undefined) { tex = m[1]; display = true; } else if (m[2] !== undefined) { tex = m[2]; display = true; } else if (m[3] !== undefined) { tex = m[3]; } else { lead = m[4] || ''; tex = m[5]; }
      const html = renderTex(tex, display);
      if (!html) continue;
      const startIdx = m.index + lead.length;
      frag.append(doc.createTextNode(s.slice(last, startIdx)));
      const span = doc.createElement(display ? 'div' : 'span');
      span.className = display ? 'math-display' : 'math-inline';
      span.innerHTML = html;
      frag.append(span);
      last = m.index + m[0].length;
      changed = true;
    }
    if (!changed) continue;
    frag.append(doc.createTextNode(s.slice(last)));
    node.replaceWith(frag);
  }
}

/** 文章纯文本（给 Agent）：公式还原为 $tex$，块之间空行 */
function agentTextOf(body) {
  const clone = body.cloneNode(true);
  const texOf = (k) => {
    const ann = k.querySelector('annotation[encoding="application/x-tex"]');
    return ann ? ann.textContent : '';
  };
  clone.querySelectorAll('mark.anno').forEach((m) => m.replaceWith(...m.childNodes));
  clone.querySelectorAll('.katex-display').forEach((k) => {
    const tex = texOf(k);
    k.replaceWith(document.createTextNode(tex ? `\n$$${tex}$$\n` : ''));
  });
  clone.querySelectorAll('.katex').forEach((k) => {
    const tex = texOf(k);
    k.replaceWith(document.createTextNode(tex ? `$${tex}$` : ''));
  });
  clone.querySelectorAll('img').forEach((img) => {
    const alt = (img.getAttribute('alt') || '').trim();
    img.replaceWith(document.createTextNode(alt ? ` [图：${alt}] ` : ''));
  });
  clone.querySelectorAll('pre').forEach((p) => {
    p.replaceWith(document.createTextNode(`\n\`\`\`\n${p.textContent.replace(/\n+$/, '')}\n\`\`\`\n`));
  });
  return new TextIndex(clone).text;
}

// ---------------------------------------------------------------- 基础视图（无操作的同接口实现）
class BaseView {
  constructor(root, opts = {}) {
    this.root = root;
    this.opts = opts;
    this.isArticle = false;
    this.isMedia = false;
    this.current = 1;
    this.numPages = 0;
    this.scale = 1;
    this.doc = null;
    this.destroyed = false;
    this.annotations = [];
    this.scrollEl = h('div', { class: 'article-scroll' });
    root.append(this.scrollEl);
  }

  setAnnotations(list) { this.annotations = (list || []).slice(); }
  upsertAnnotation() {}
  removeAnnotation() {}
  setMarkerResolver(fn) { this.markerResolver = fn; }
  clearSelection() { const s = window.getSelection(); s && s.removeAllRanges(); }
  getSelectionInfo() { return null; }
  hitTest() { return null; }
  flashAnnotation() {}
  goToAnnotation() {}
  flashAnchor() {}
  flashRects() {}
  scrollToPage() {}
  scrollPage() {}
  async getPageText() { return ''; }
  async getPageParagraphs() { return []; }
  async getAllText() { return []; }
  async ensureText() { return null; }
  fitWidthScale() { return 1; }
  setScale() {}
  destroy() {
    this.destroyed = true;
    this.root.innerHTML = '';
  }
}

// ---------------------------------------------------------------- 文章 / README
export class ArticleView extends BaseView {
  /**
   * @param {HTMLElement} root
   * @param {{onSelection?,onAnnotationClick?,onBlankClick?,onScaleChange?,onPageChange?}} opts
   */
  constructor(root, opts = {}) {
    super(root, opts);
    this.isArticle = true;
    this.numPages = 1;
    this.scale = clamp(+localStorage.getItem('pd.articleZoom') || 1);
    this.index = null;
    this.body = null;
    this.wrap = null;
    this._down = null;
    this._onDown = (e) => {
      if (e.button !== 0) return;
      this._down = { x: e.clientX, y: e.clientY, inBody: !!(this.body && this.body.contains(e.target)) };
    };
    this._onUp = (e) => {
      const down = this._down;
      this._down = null;
      if (!down) return;
      setTimeout(() => this._afterMouseUp(e, down), 0);
    };
    this.scrollEl.addEventListener('mousedown', this._onDown);
    document.addEventListener('mouseup', this._onUp);
    this._onWheel = (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      this.setScale(this.scale * Math.exp(-e.deltaY * 0.0025));
    };
    this.scrollEl.addEventListener('wheel', this._onWheel, { passive: false });
  }

  /**
   * @param {object} data  /api/web 的返回
   * @param {{resource?:object, banner?:HTMLElement}} o
   */
  load(data, { resource = {}, banner = null } = {}) {
    const mode = data.mode === 'readme' ? 'readme' : 'article';
    this.mode = mode;
    const wrap = h('article', { class: mode === 'readme' ? 'readme-view' : 'article-view' });
    wrap.style.setProperty('--article-scale', String(this.scale));
    const head = h('header', { class: 'av-head' });
    const url = data.url || resource.url || '';
    if (mode === 'readme') {
      head.append(
        h('div', { class: 'av-site' }, icon('github', 14), h('span', {}, 'GitHub'), data.language ? h('span', { class: 'av-dot' }, '·') : null, data.language ? h('span', {}, data.language) : null,
          Number.isFinite(+data.stars) && data.stars !== null && data.stars !== undefined ? h('span', { class: 'av-dot' }, '·') : null,
          Number.isFinite(+data.stars) && data.stars !== null && data.stars !== undefined ? h('span', {}, `★ ${fmtNum(+data.stars)}`) : null),
        h('h1', { class: 'av-title' }, data.title || resource.title || hostOf(url)),
        data.description ? h('p', { class: 'av-desc' }, data.description) : null,
        h('div', { class: 'av-meta' },
          h('a', { href: url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在 GitHub 打开'),
          data.homepage ? h('a', { href: data.homepage, target: '_blank', rel: 'noopener' }, icon('home', 13), hostOf(data.homepage) || '主页') : null));
    } else {
      const wc = data.word_count || 0;
      const mins = wc ? Math.max(1, Math.round(wc / 230)) : 0;
      head.append(
        h('div', { class: 'av-site' }, h('span', {}, kindMeta(resource.kind).icon), h('span', {}, data.site_name || hostOf(url))),
        h('h1', { class: 'av-title' }, data.title || resource.title || hostOf(url)),
        h('div', { class: 'av-meta' },
          data.byline ? h('span', {}, data.byline) : null,
          data.published ? h('span', {}, String(data.published).slice(0, 10)) : null,
          mins ? h('span', {}, `约 ${mins} 分钟读完`) : null,
          h('a', { href: url, target: '_blank', rel: 'noopener' }, icon('external', 13), '原网页'),
          this.opts.onOriginalMode ? h('button', { class: 'av-mode-btn', type: 'button', title: '保留原网页的排版和样式，直接在上面划词高亮、记笔记', onclick: () => this.opts.onOriginalMode() }, icon('eye', 13), '在原网页上阅读和标注') : null));
    }
    const body = h('div', { class: 'article-body md' });
    if (mode === 'readme') {
      body.innerHTML = renderMarkdown(data.markdown || '');
      fixRelativeUrls(body, data.raw_base, data.html_base);
    } else {
      body.innerHTML = DOMPurify.sanitize(data.html || '', {
        FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe', 'script', 'object', 'embed', 'textarea', 'select'],
        FORBID_ATTR: ['style', 'class', 'srcset'],
        ADD_ATTR: ['target'],
      });
      fixRelativeUrls(body, url, url);
      renderMathIn(body);
      // 正文开头重复的标题（头部已显示）去掉
      const first = body.firstElementChild;
      const norm = (x) => String(x || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (first && /^H[12]$/.test(first.tagName) && norm(first.textContent) && norm(first.textContent) === norm(data.title || resource.title)) first.remove();
    }
    for (const img of body.querySelectorAll('img')) {
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => img.classList.add('img-broken'), { once: true });
    }
    for (const a of body.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href');
      if (href && !href.startsWith('#')) {
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
      }
    }
    wrap.append(head, banner, body);
    this.scrollEl.replaceChildren(wrap);
    this.wrap = wrap;
    this.body = body;
    this.index = new TextIndex(body);
    this._agentText = null;
    return this;
  }

  get text() {
    return this.index ? this.index.text : '';
  }

  // ---------- 选择 ----------
  _afterMouseUp(e, down) {
    if (this.destroyed || !this.body) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const info = this.getSelectionInfo();
      if (info) {
        this.opts.onSelection && this.opts.onSelection(info);
        return;
      }
    }
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    if (moved < 5 && down.inBody) {
      const hit = this.hitTest(e.clientX, e.clientY);
      if (hit) this.opts.onAnnotationClick && this.opts.onAnnotationClick(hit, e.clientX, e.clientY);
      else this.opts.onBlankClick && this.opts.onBlankClick();
    }
  }

  getSelectionInfo() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount || !this.index) return null;
    const range = sel.getRangeAt(0);
    if (!this.body.contains(range.commonAncestorContainer)) return null;
    let s = this.index.offsetOf(range.startContainer, range.startOffset);
    let e = this.index.offsetOf(range.endContainer, range.endOffset);
    if (e < s) [s, e] = [e, s];
    const t = this.index.text;
    while (s < e && /\s/.test(t[s])) s++;
    while (e > s && /\s/.test(t[e - 1])) e--;
    if (e <= s) return null;
    const anchor = this.index.anchorFor(s, e);
    const quote = anchor.exact.replace(/\s+/g, ' ').trim();
    if (!quote) return null;
    return { page: null, quote, rects: [], anchor, clientRect: range.getBoundingClientRect() };
  }

  hitTest(cx, cy) {
    const el = document.elementFromPoint(cx, cy);
    const m = el && el.closest && el.closest('mark.anno');
    if (!m || !this.body.contains(m)) return null;
    return this.annotations.find((a) => a.id === m.dataset.id) || null;
  }

  // ---------- 标注 ----------
  setAnnotations(list) {
    this.annotations = (list || []).slice();
    this._redraw();
  }

  upsertAnnotation(a) {
    const i = this.annotations.findIndex((x) => x.id === a.id);
    if (i >= 0) this.annotations[i] = a;
    else this.annotations.push(a);
    if (i >= 0) {
      // 只改颜色/类型/批注：直接改 class，不必重排
      const marks = this.body ? this.body.querySelectorAll(`mark.anno[data-id="${CSS.escape(a.id)}"]`) : [];
      if (marks.length) {
        marks.forEach((m) => { m.className = `anno anno-${a.kind || 'highlight'} c-${a.color || 'yellow'}`; });
        this._markers();
        return;
      }
    }
    if (this._wrap(a)) this._markers();
  }

  removeAnnotation(id) {
    this.annotations = this.annotations.filter((x) => x.id !== id);
    this._unwrap(id);
    this._markers();
  }

  setMarkerResolver(fn) {
    this.markerResolver = fn;
    this._markers();
  }

  _redraw() {
    if (!this.body) return;
    this._unwrap(null);
    const placed = this.annotations
      .map((a) => ({ a, pos: this._locate(a) }))
      .filter((x) => x.pos)
      .sort((x, y) => x.pos[0] - y.pos[0]);
    for (const { a } of placed) this._wrap(a);
    this._markers();
  }

  _locate(a) {
    if (!this.index) return null;
    if (a.anchor && a.anchor.exact) return this.index.locate(a.anchor);
    if (a.quote) return this.index.locate({ exact: a.quote });
    return null;
  }

  _wrap(a) {
    const pos = this._locate(a);
    if (!pos) return false;
    const [s, e] = pos;
    const segs = [];
    let cur = null;
    for (let i = s; i < e; i++) {
      const at = this.index.nodeAt(i);
      if (!at) continue;
      const [node, off] = at;
      if (cur && cur.node === node) cur.end = off;
      else {
        cur = { node, start: off, end: off };
        segs.push(cur);
      }
    }
    if (!segs.length) return false;
    const cls = `anno anno-${a.kind || 'highlight'} c-${a.color || 'yellow'}`;
    for (const sg of segs) {
      let node = sg.node;
      if (!node.parentNode) continue;
      const piece = node.data.slice(sg.start, sg.end + 1);
      if (NO_WRAP_PARENTS.has(node.parentNode.nodeName) && !piece.trim()) continue;
      if (sg.end + 1 < node.length) node.splitText(sg.end + 1);
      if (sg.start > 0) node = node.splitText(sg.start);
      const mark = node.ownerDocument.createElement('mark');
      mark.className = cls;
      mark.dataset.id = a.id;
      node.parentNode.insertBefore(mark, node);
      mark.appendChild(node);
    }
    this.index.build();
    return true;
  }

  _unwrap(id) {
    if (!this.body) return;
    const sel = id ? `mark.anno[data-id="${CSS.escape(id)}"]` : 'mark.anno';
    const marks = Array.from(this.body.querySelectorAll(sel));
    if (!marks.length) return;
    const parents = new Set();
    for (const m of marks) {
      const p = m.parentNode;
      if (!p) continue;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      m.remove();
      parents.add(p);
    }
    for (const p of parents) p.normalize();
    this.index.build();
  }

  _markers() {
    if (!this.body) return;
    this.body.querySelectorAll('mark.anno.has-note').forEach((m) => m.classList.remove('has-note'));
    for (const a of this.annotations) {
      const hasNote = (a.comment && a.comment.trim()) || (this.markerResolver && this.markerResolver(a));
      if (!hasNote) continue;
      const marks = this.body.querySelectorAll(`mark.anno[data-id="${CSS.escape(a.id)}"]`);
      if (marks.length) marks[marks.length - 1].classList.add('has-note');
    }
  }

  flashAnnotation(id) {
    const els = this.body ? this.body.querySelectorAll(`mark.anno[data-id="${CSS.escape(id)}"]`) : [];
    els.forEach((el) => {
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    });
    clearTimeout(this._flashT);
    this._flashT = setTimeout(() => els.forEach((el) => el.classList.remove('flash')), 2200);
    return els.length > 0;
  }

  goToAnnotation(a) {
    if (!a || !this.body) return;
    const m = this.body.querySelector(`mark.anno[data-id="${CSS.escape(a.id)}"]`);
    if (m) {
      this._scrollTo(m.getBoundingClientRect());
      setTimeout(() => this.flashAnnotation(a.id), 350);
      return;
    }
    this.flashAnchor(a.anchor || { exact: a.quote });
  }

  /** 按锚点（或纯引文）定位并临时闪烁 */
  flashAnchor(anchor) {
    if (!this.index || !anchor) return false;
    const pos = this.index.locate(anchor);
    if (!pos) return false;
    const r = this.index.rangeFor(pos[0], pos[1]);
    if (!r) return false;
    this._scrollTo(r.getBoundingClientRect());
    const hl = this._hlApi();
    if (hl) {
      hl.reg.set('pd-flash', new hl.Highlight(r));
      clearTimeout(this._flashAT);
      this._flashAT = setTimeout(() => { try { hl.reg.delete('pd-flash'); } catch { /* ignore */ } }, 2400);
    }
    return true;
  }

  /** 可视区域（子类原网页模式里是 iframe 的视口） */
  _viewBox() {
    return this.scrollEl.getBoundingClientRect();
  }

  /** CSS Custom Highlight API（子类里换成 iframe 的 window） */
  _hlApi() {
    return window.CSS && CSS.highlights && window.Highlight ? { reg: CSS.highlights, Highlight: window.Highlight } : null;
  }

  _scrollTo(rect, frac = 0.3) {
    const box = this._viewBox();
    this.scrollEl.scrollTo({ top: Math.max(0, this.scrollEl.scrollTop + rect.top - box.top - box.height * frac), behavior: 'smooth' });
  }

  scrollToPage(n) {
    if (n === 1 || !n) this.scrollEl.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /** 按屏翻页（d 可为负） */
  scrollPage(d) {
    this.scrollEl.scrollBy({ top: d * this.scrollEl.clientHeight * 0.85, behavior: 'smooth' });
  }

  // ---------- 缩放（字号） ----------
  fitWidthScale() { return 1; }

  setScale(s) {
    this.scale = clamp(s);
    if (this.wrap) this.wrap.style.setProperty('--article-scale', String(this.scale));
    localStorage.setItem('pd.articleZoom', String(Math.round(this.scale * 100) / 100));
    this.opts.onScaleChange && this.opts.onScaleChange(this.scale);
  }

  // ---------- 文字 ----------
  async getPageText() {
    return this.text;
  }

  _blocks() {
    if (!this.body) return [];
    const all = Array.from(this.body.querySelectorAll(READ_BLOCKS));
    return all.filter((el) => !el.querySelector(READ_BLOCKS) || el.tagName === 'PRE');
  }

  /** 「翻译当前页」：从当前可见位置开始的若干段 */
  async getPageParagraphs() {
    const box = this._viewBox();
    const out = [];
    let chars = 0;
    for (const el of this._blocks()) {
      if (el.tagName === 'PRE') continue;
      const r = el.getBoundingClientRect();
      if (r.bottom < box.top + 4) continue;
      const [s, e] = this.index.elementSpan(el);
      const t = this.index.text.slice(s, e).replace(/\s+/g, ' ').trim();
      if (t.length < 2) continue;
      out.push(t);
      chars += t.length;
      if (out.length >= 12 || chars > 4500) break;
    }
    return out;
  }

  async getAllText() {
    if (!this._agentText) this._agentText = this.body ? agentTextOf(this.body) : '';
    return [{ page: 1, text: this._agentText }];
  }

  // ---------- 朗读（Audiobook 调用） ----------
  async buildReadItems(num, opts = {}) {
    const items = [];
    let stop = false;
    if (!this.index) return { items, stop: true };
    const text = this.index.text;
    for (const el of this._blocks()) {
      const [s, e] = this.index.elementSpan(el);
      if (e <= s) continue;
      const t = text.slice(s, e);
      if (!t.trim()) continue;
      if (el.tagName === 'PRE') {
        items.push({ text: '代码略', lang: 'zh', kind: 'formula', start: s, end: e, el });
        continue;
      }
      if (/^H[1-6]$/.test(el.tagName)) {
        const ht = t.replace(/\s+/g, ' ').trim();
        if (opts.skipRefs && REF_HEAD.test(ht) && items.length > 3) {
          stop = true;
          break;
        }
        items.push({ text: ht, lang: detectLang(ht), kind: 'heading', start: s, end: e, el });
        continue;
      }
      for (const sen of splitSentences(t)) {
        for (const ch of chunkLong(t.slice(sen.start, sen.end))) {
          let said = ch.text.replace(/\s+/g, ' ').trim();
          const lang = detectLang(said.replace(/〔公式〕/g, ''));
          said = said.replace(/〔公式〕/g, lang === 'zh' ? '公式' : 'formula');
          if (opts.stripCites) said = said.replace(/\s?\[(?:\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]/g, '');
          if (said.length < 2 || !/[\p{L}\p{N}]/u.test(said)) continue;
          items.push({ text: said, lang, kind: 'text', start: s + sen.start + ch.start, end: s + sen.start + ch.end, el });
        }
      }
    }
    if (!items.length) stop = true;
    return { items, stop };
  }

  highlightReadItem(it, { follow = true } = {}) {
    this.clearReadHighlight();
    if (!this.index) return;
    const r = this.index.rangeFor(it.start, it.end);
    const hl = this._hlApi();
    if (r && hl) hl.reg.set('pd-tts', new hl.Highlight(r));
    else if (it.el) { it.el.classList.add('tts-cur-block'); this._ttsEl = it.el; }
    if (!follow) return;
    const rect = r ? r.getBoundingClientRect() : it.el && it.el.getBoundingClientRect();
    if (!rect) return;
    const box = this._viewBox();
    if (rect.top < box.top + box.height * 0.12 || rect.bottom > box.top + box.height * 0.85) this._scrollTo(rect, 0.35);
  }

  clearReadHighlight() {
    try { const hl = this._hlApi(); if (hl) hl.reg.delete('pd-tts'); } catch { /* ignore */ }
    if (this._ttsEl) { this._ttsEl.classList.remove('tts-cur-block'); this._ttsEl = null; }
  }

  /** 第一个出现在视口里的朗读条目下标 */
  firstVisibleIndex(items) {
    const box = this._viewBox();
    for (let i = 0; i < items.length; i++) {
      const r = this.index.rangeFor(items[i].start, items[i].end);
      const rect = r ? r.getBoundingClientRect() : items[i].el && items[i].el.getBoundingClientRect();
      if (rect && rect.bottom > box.top + 8) return i;
    }
    return 0;
  }

  async getSections() {
    if (!this.body) return [];
    return Array.from(this.body.querySelectorAll('h1, h2, h3, h4')).map((el) => {
      const t = el.textContent.replace(/\s+/g, ' ').trim();
      return { title: t, page: 1, heading: t, depth: /H[34]/.test(el.tagName) ? 1 : 0 };
    }).filter((s) => s.title);
  }

  destroy() {
    this.destroyed = true;
    document.removeEventListener('mouseup', this._onUp);
    this.clearReadHighlight();
    try { const hl = this._hlApi(); if (hl) hl.reg.delete('pd-flash'); } catch { /* ignore */ }
    this.root.innerHTML = '';
  }
}

// ---------------------------------------------------------------- 视频
export class VideoView extends BaseView {
  constructor(root, opts = {}) {
    super(root, opts);
    this.isMedia = true;
  }

  load({ embed, url, title, kind }) {
    const frame = h('iframe', {
      src: embed,
      title: title || '视频',
      allow: 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share',
      allowfullscreen: true,
      referrerpolicy: 'strict-origin-when-cross-origin',
    });
    this.scrollEl.replaceChildren(h('div', { class: 'video-view' },
      h('div', { class: 'av-site' }, h('span', {}, kindMeta(kind || 'video').icon), h('span', {}, hostOf(url))),
      h('h1', { class: 'av-title' }, title || hostOf(url)),
      h('div', { class: 'video-box' }, frame),
      h('div', { class: 'video-foot' },
        h('span', { class: 'faint' }, '视频不能划词标注；可以在右侧记笔记、提问（AI 读不到视频内容，只能看到你的笔记和其他资源）。'),
        h('span', { class: 'spacer' }),
        h('a', { class: 'btn btn-sm', href: url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开'))));
    return this;
  }
}

// ---------------------------------------------------------------- iframe 嵌入（无法标注）
export class IframeView extends BaseView {
  constructor(root, opts = {}) {
    super(root, opts);
    this.isMedia = true;
  }

  load({ url, hint }) {
    const frame = h('iframe', { src: url, title: '网页', referrerpolicy: 'no-referrer', sandbox: 'allow-scripts allow-same-origin allow-popups allow-forms' });
    this.scrollEl.classList.add('iframe-mode');
    this.scrollEl.replaceChildren(h('div', { class: 'iframe-wrap' },
      h('div', { class: 'iframe-hint' }, h('span', {}, hint || '嵌入模式：这里无法划词标注；如果一片空白，说明网站拒绝被嵌入。'), h('span', { class: 'spacer' }),
        h('a', { class: 'btn btn-sm', href: url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开')),
      frame));
    return this;
  }
}

// ---------------------------------------------------------------- 提示（加载失败 / 静态版降级）
export class NoticeView extends BaseView {
  constructor(root, opts = {}) {
    super(root, opts);
    this.isMedia = true;
  }

  load({ title, message, actions = [], extra = null, site = null }) {
    this.scrollEl.replaceChildren(h('div', { class: 'notice-view' },
      site ? h('div', { class: 'av-site' }, site) : null,
      title ? h('h1', { class: 'av-title' }, title) : null,
      message ? h('div', { class: 'notice-msg' }, message) : null,
      actions.length ? h('div', { class: 'notice-actions' }, actions) : null,
      extra));
    return this;
  }
}

/** 加载中占位（与 PDF 的加载条同款） */
export function loadingBox(text) {
  return h('div', { class: 'rd-loading' }, h('div', { style: { textAlign: 'center' } }, h('div', { class: 'ld-text' }, text), h('div', { class: 'bar indeterminate' }, h('i'))));
}

/** 静态只读版：浏览器直接调 GitHub API 取 README（api.github.com 允许跨域） */
export async function fetchReadmeDirect(url) {
  const m = url.match(/github\.com\/([^/]+)\/([^/#?]+)/i);
  if (!m) throw new Error('不是 GitHub 仓库链接');
  const o = m[1];
  const r = m[2].replace(/\.git$/, '');
  const [meta, md] = await Promise.all([
    fetch(`https://api.github.com/repos/${o}/${r}`).then((x) => (x.ok ? x.json() : {})).catch(() => ({})),
    fetch(`https://api.github.com/repos/${o}/${r}/readme`, { headers: { Accept: 'application/vnd.github.raw' } }).then((x) => {
      if (!x.ok) throw new Error(`GitHub 返回 ${x.status}`);
      return x.text();
    }),
  ]);
  return {
    mode: 'readme',
    url: `https://github.com/${o}/${r}`,
    markdown: md,
    title: `${o}/${r}`,
    description: meta.description || '',
    stars: meta.stargazers_count ?? null,
    language: meta.language || '',
    homepage: meta.homepage || '',
    raw_base: `https://raw.githubusercontent.com/${o}/${r}/HEAD/`,
    html_base: `https://github.com/${o}/${r}/blob/HEAD/`,
  };
}

// ---------------------------------------------------------------- 工具
function clamp(s) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s || 1));
}

function fmtNum(n) {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

/** 相对地址补全：图片按 rawBase，链接按 htmlBase */
function fixRelativeUrls(body, rawBase, htmlBase) {
  const abs = (u, base) => {
    if (!u || !base) return u;
    if (/^(https?:|data:|mailto:|#)/i.test(u)) return u;
    try {
      return new URL(u.replace(/^\.\//, ''), base).href;
    } catch {
      return u;
    }
  };
  for (const img of body.querySelectorAll('img')) {
    const src = img.getAttribute('src') || img.getAttribute('data-src');
    if (src) img.setAttribute('src', abs(src, rawBase));
  }
  for (const a of body.querySelectorAll('a[href]')) {
    a.setAttribute('href', abs(a.getAttribute('href'), htmlBase));
  }
}

export { truncate };
