// 原网页模式：保留网页原来的排版和样式，在上面直接划词标注、记笔记。
//
// 安全模型：
//   - 后端 /api/webpage 只取回 HTML 文本（不落盘）；这里去掉全部 <script>、on* 事件属性、javascript: 链接、
//     meta refresh / 网页自带的 CSP，再加一条 script-src 'none'、connect-src 'none' 的 CSP。
//   - iframe 用 sandbox="allow-same-origin allow-popups ..."，**不给 allow-scripts**：网页自己的脚本一行都不会运行，
//     所以它碰不到 PaperDesk 的 /api。同源只是为了让阅读页能读写 iframe 里的 DOM（划词、包 <mark>）。
//   - 相对地址用 <base href=原网址> 解析，图片 / 样式表 / 字体直接从原网站加载。
//
// 标注锚点与正文模式相同（规范化纯文本里的 exact / prefix / suffix），所以同一条标注两种模式下通常都能找到。
import { ArticleView, TextIndex, renderMathIn } from './articleview.js';
import { h, icon } from './ui.js';

const ZOOM_KEY = 'pd.originalZoom';
const MIN_ZOOM = 0.6;
const MAX_ZOOM = 2;

const PAGE_CSP = [
  "default-src * data: blob:",
  "script-src 'none'",
  "object-src 'none'",
  "connect-src 'none'",
  "form-action 'none'",
  "style-src * data: 'unsafe-inline'",
  "img-src * data: blob:",
  "font-src * data:",
  "media-src * data: blob:",
  "frame-src *",
].join('; ');

// 注入 iframe 的样式：标注外观 + 常见「等脚本来显示」的元素强制显示
const INJECT_CSS = `
mark.anno { color: inherit; background: transparent; padding: 0; border-radius: 2px; cursor: pointer;
  -webkit-box-decoration-break: clone; box-decoration-break: clone; --lc: #d4a300; }
mark.anno.c-yellow { --lc: #d9a400; } mark.anno.c-green { --lc: #2e9a4a; } mark.anno.c-blue { --lc: #2b74e0; }
mark.anno.c-pink { --lc: #e0457b; } mark.anno.c-purple { --lc: #8a5cf0; }
mark.anno-highlight.c-yellow { background: rgba(255, 210, 0, 0.42); }
mark.anno-highlight.c-green { background: rgba(92, 200, 110, 0.36); }
mark.anno-highlight.c-blue { background: rgba(80, 155, 255, 0.32); }
mark.anno-highlight.c-pink { background: rgba(255, 100, 160, 0.3); }
mark.anno-highlight.c-purple { background: rgba(160, 110, 255, 0.3); }
mark.anno-underline { text-decoration: underline; text-decoration-color: var(--lc); text-decoration-thickness: 2px; text-underline-offset: 3px; }
mark.anno-strike { text-decoration: line-through; text-decoration-color: var(--lc); text-decoration-thickness: 1.7px; }
mark.anno-squiggly { text-decoration: underline wavy; text-decoration-color: var(--lc); text-decoration-thickness: 1.3px; text-underline-offset: 3px; }
mark.anno-vocab { background: rgba(160, 110, 255, 0.1); text-decoration: underline dotted #8a5cf0; text-decoration-thickness: 2.2px; text-underline-offset: 4px; }
mark.anno.has-note::after { content: "💬"; font-size: 0.62em; vertical-align: super; margin-left: 1px; text-decoration: none; }
mark.anno.flash { animation: pdmarkflash 0.55s ease-in-out 0s 4; }
@keyframes pdmarkflash { 0%, 100% { box-shadow: 0 0 0 0 rgba(47, 95, 179, 0); } 50% { box-shadow: 0 0 0 3px rgba(47, 95, 179, 0.5); background-color: rgba(80, 155, 255, 0.28); } }
::highlight(pd-tts) { background-color: rgba(255, 196, 0, 0.4); }
::highlight(pd-flash) { background-color: rgba(80, 155, 255, 0.35); }
.tts-cur-block { background: rgba(255, 196, 0, 0.15); border-radius: 4px; }
[data-aos] { opacity: 1 !important; transform: none !important; }
`;

/**
 * 把原网页 HTML 变成「不运行任何脚本」的版本。
 * @returns {{html: string, math: boolean, title: string}}
 */
export function sanitizePage(rawHtml, url) {
  const doc = new DOMParser().parseFromString(rawHtml || '', 'text/html');
  let math = false;
  // MathJax v2 的 <script type="math/tex"> 先还原成 \( \) / \[ \]，稍后用 KaTeX 渲染
  for (const sc of Array.from(doc.querySelectorAll('script[type^="math/tex"]'))) {
    const display = /mode\s*=\s*display/i.test(sc.getAttribute('type') || '');
    const span = doc.createElement(display ? 'div' : 'span');
    span.textContent = display ? `\\[${sc.textContent}\\]` : `\\(${sc.textContent}\\)`;
    sc.replaceWith(span);
    math = true;
  }
  for (const sc of Array.from(doc.querySelectorAll('script'))) {
    if (/mathjax|katex/i.test((sc.getAttribute('src') || '') + ' ' + (sc.textContent || '').slice(0, 4000))) math = true;
    sc.remove();
  }
  doc.querySelectorAll('base, meta[http-equiv], link[rel~="preload"][as="script"], link[rel~="modulepreload"], link[rel~="serviceworker"], object, embed, applet')
    .forEach((el) => el.remove());
  // 事件属性和 javascript: 链接（CSP 和 sandbox 已经挡住，这里再清一遍）
  for (const el of Array.from(doc.querySelectorAll('*'))) {
    for (const attr of Array.from(el.attributes)) {
      const n = attr.name.toLowerCase();
      if (n.startsWith('on')) el.removeAttribute(attr.name);
      else if ((n === 'href' || n === 'src' || n === 'action' || n === 'formaction' || n === 'xlink:href') && /^\s*javascript:/i.test(attr.value)) el.removeAttribute(attr.name);
    }
  }
  // 懒加载图片：常见的 data-src 写法
  for (const img of Array.from(doc.querySelectorAll('img'))) {
    const lazy = img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy-src');
    const src = img.getAttribute('src') || '';
    if (lazy && (!src || src.startsWith('data:'))) img.setAttribute('src', lazy);
    const ls = img.getAttribute('data-srcset');
    if (ls && !img.getAttribute('srcset')) img.setAttribute('srcset', ls);
    img.removeAttribute('loading');
  }
  const head = doc.head || doc.documentElement.insertBefore(doc.createElement('head'), doc.body);
  const meta = (attrs) => {
    const m = doc.createElement('meta');
    for (const [k, v] of Object.entries(attrs)) m.setAttribute(k, v);
    return m;
  };
  const base = doc.createElement('base');
  base.setAttribute('href', url);
  head.prepend(
    meta({ charset: 'utf-8' }),
    meta({ 'http-equiv': 'Content-Security-Policy', content: PAGE_CSP }),
    meta({ name: 'referrer', content: 'no-referrer' }),
    base);
  const title = (doc.title || '').replace(/\s+/g, ' ').trim();
  return { html: '<!doctype html>\n' + doc.documentElement.outerHTML, math, title };
}

export class OriginalView extends ArticleView {
  /**
   * @param {HTMLElement} root
   * @param {object} opts  同 ArticleView，另加 onReaderMode()（切回正文）
   */
  constructor(root, opts = {}) {
    super(root, opts);
    this.isOriginal = true;
    this.scale = clampZoom(+localStorage.getItem(ZOOM_KEY) || 1);
    this.frame = null;
    this.win = null;
    this.fdoc = null;
    this.ready = new Promise((res) => { this._resolveReady = res; });
    this._fdown = null;
  }

  /**
   * @param {{url:string, html:string, math?:boolean}} page   sanitizePage 的结果 + 原网址
   */
  load(page, { resource = {} } = {}) {
    this.url = page.url || resource.url || '';
    this.math = !!page.math;
    this.scrollEl.classList.add('iframe-mode', 'original-mode');
    this.banner = h('div', { class: 'iframe-hint original-hint' },
      icon('edit', 13),
      h('span', {}, '原网页模式：可以直接划词高亮、记笔记。网页自己的脚本已关闭，个别靠脚本显示的内容可能缺失。'),
      h('span', { class: 'spacer' }),
      this.opts.onReaderMode ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => this.opts.onReaderMode() }, icon('file', 13), '切回正文模式') : null,
      h('a', { class: 'btn btn-sm', href: this.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '新标签页打开'));
    const frame = h('iframe', {
      title: '原网页',
      class: 'original-frame',
      referrerpolicy: 'no-referrer',
      sandbox: 'allow-same-origin allow-popups allow-popups-to-escape-sandbox',
    });
    frame.srcdoc = page.html;
    frame.addEventListener('load', () => this._onFrameLoad(), { once: true });
    this.frame = frame;
    this.scrollEl.replaceChildren(h('div', { class: 'iframe-wrap' }, this.banner, frame));
    return this;
  }

  _onFrameLoad() {
    if (this.destroyed) return;
    const win = this.frame.contentWindow;
    const fdoc = this.frame.contentDocument;
    if (!win || !fdoc || !fdoc.body) return this._resolveReady(this);
    this.win = win;
    this.fdoc = fdoc;
    const style = fdoc.createElement('style');
    style.textContent = INJECT_CSS;
    (fdoc.head || fdoc.documentElement).append(style);
    if (this.math) {
      const link = fdoc.createElement('link');
      link.rel = 'stylesheet';
      link.href = new URL('/static/vendor/katex/katex.min.css', location.href).href;
      (fdoc.head || fdoc.documentElement).append(link);
      try { renderMathIn(fdoc.body); } catch (e) { console.warn('公式渲染失败', e); }
    }
    for (const a of fdoc.querySelectorAll('a[href]')) {
      const raw = a.getAttribute('href') || '';
      if (raw.startsWith('#')) continue;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    }
    // 事件：监听器属于阅读页（父页面）的脚本，sandbox 只禁止网页自己的脚本
    this._on(fdoc, 'click', (e) => this._onClick(e));
    this._on(fdoc, 'mousedown', (e) => {
      if (e.button !== 0) return;
      this._fdown = { x: e.clientX, y: e.clientY, inBody: !!(this.body && this.body.contains(e.target)) };
      this.opts.onBlankClick && this.opts.onBlankClick();
    });
    this._on(fdoc, 'mouseup', (e) => {
      const down = this._fdown;
      this._fdown = null;
      if (!down) return;
      setTimeout(() => this._afterFrameMouseUp(e, down), 0);
    });
    this._on(fdoc, 'keydown', (e) => {
      // 让阅读页的快捷键（Esc / K / J / L）在 iframe 里也能用
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: e.key, code: e.code, metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, shiftKey: e.shiftKey, bubbles: true,
      }));
    });
    this._on(win, 'scroll', () => this.scrollEl.dispatchEvent(new Event('scroll')), { passive: true });
    this._on(fdoc, 'wheel', (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      this.setScale(this.scale * Math.exp(-e.deltaY * 0.0025));
    }, { passive: false });
    this.body = fdoc.body;
    this._applyZoom();
    this.index = new TextIndex(this.body, 'page');
    this._agentText = null;
    this.pageTitle = (fdoc.title || '').replace(/\s+/g, ' ').trim();
    if (this.index.text.length < 200) {
      this.banner.after(h('div', { class: 'weak-banner' },
        h('span', {}, '这个网页的文字很少，可能要靠脚本才能显示。正文模式通常能抓到内容。'),
        h('span', { class: 'spacer' }),
        this.opts.onReaderMode ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => this.opts.onReaderMode() }, '切回正文模式') : null));
    }
    this._redraw();
    this._resolveReady(this);
  }

  _on(target, type, fn, opt) {
    target.addEventListener(type, fn, opt);
    (this._listeners || (this._listeners = [])).push([target, type, fn, opt]);
  }

  /** 页内锚点（#section）：<base> 会把它解析成原网站地址，这里改成在 iframe 内滚动 */
  _onClick(e) {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    const raw = a.getAttribute('href') || '';
    if (!raw.startsWith('#')) return;
    e.preventDefault();
    const id = decodeURIComponent(raw.slice(1));
    const t = id ? (this.fdoc.getElementById(id) || this.fdoc.getElementsByName(id)[0]) : this.fdoc.body;
    if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /** iframe 视口坐标 → 阅读页坐标 */
  _toParentRect(r) {
    const f = this.frame.getBoundingClientRect();
    return { left: r.left + f.left, top: r.top + f.top, right: r.right + f.left, bottom: r.bottom + f.top, width: r.width, height: r.height, x: r.left + f.left, y: r.top + f.top };
  }

  _afterFrameMouseUp(e, down) {
    if (this.destroyed || !this.body) return;
    const sel = this.win.getSelection();
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const info = this.getSelectionInfo();
      if (info) {
        this.opts.onSelection && this.opts.onSelection(info);
        return;
      }
    }
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    if (moved < 5 && down.inBody) {
      const hit = this._hitFrame(e.clientX, e.clientY);
      const p = this._toParentRect({ left: e.clientX, top: e.clientY, right: e.clientX, bottom: e.clientY, width: 0, height: 0 });
      if (hit) this.opts.onAnnotationClick && this.opts.onAnnotationClick(hit, p.left, p.top);
      else this.opts.onBlankClick && this.opts.onBlankClick();
    }
  }

  getSelectionInfo() {
    if (!this.win || !this.index) return null;
    const sel = this.win.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
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
    return { page: null, quote, rects: [], anchor, clientRect: this._toParentRect(range.getBoundingClientRect()) };
  }

  clearSelection() {
    try { this.win && this.win.getSelection().removeAllRanges(); } catch { /* ignore */ }
  }

  _hitFrame(cx, cy) {
    const el = this.fdoc.elementFromPoint(cx, cy);
    const m = el && el.closest && el.closest('mark.anno');
    if (!m || !this.body.contains(m)) return null;
    return this.annotations.find((a) => a.id === m.dataset.id) || null;
  }

  /** 阅读页坐标的命中测试 */
  hitTest(cx, cy) {
    if (!this.fdoc) return null;
    const f = this.frame.getBoundingClientRect();
    return this._hitFrame(cx - f.left, cy - f.top);
  }

  _viewBox() {
    const hgt = this.win ? this.win.innerHeight : 0;
    return { top: 0, bottom: hgt, height: hgt, left: 0, right: this.win ? this.win.innerWidth : 0 };
  }

  _hlApi() {
    const w = this.win;
    return w && w.CSS && w.CSS.highlights && w.Highlight ? { reg: w.CSS.highlights, Highlight: w.Highlight } : null;
  }

  _scrollTo(rect, frac = 0.3) {
    if (!this.win) return;
    this.win.scrollTo({ top: Math.max(0, this.win.scrollY + rect.top - this.win.innerHeight * frac), behavior: 'smooth' });
  }

  scrollToPage(n) {
    if ((n === 1 || !n) && this.win) this.win.scrollTo({ top: 0, behavior: 'smooth' });
  }

  scrollPage(d) {
    if (this.win) this.win.scrollBy({ top: d * this.win.innerHeight * 0.85, behavior: 'smooth' });
  }

  // ---------- 缩放：整页 zoom ----------
  _applyZoom() {
    if (this.fdoc && this.fdoc.documentElement) this.fdoc.documentElement.style.zoom = String(this.scale);
  }

  setScale(s) {
    this.scale = clampZoom(s);
    this._applyZoom();
    try { localStorage.setItem(ZOOM_KEY, String(Math.round(this.scale * 100) / 100)); } catch { /* ignore */ }
    this.opts.onScaleChange && this.opts.onScaleChange(this.scale);
  }

  destroy() {
    for (const [t, type, fn, opt] of this._listeners || []) {
      try { t.removeEventListener(type, fn, opt); } catch { /* ignore */ }
    }
    this._listeners = [];
    super.destroy();
  }
}

function clampZoom(s) {
  if (!Number.isFinite(s)) return 1;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, s));
}
