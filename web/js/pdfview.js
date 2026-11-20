// PDF 视图：pdf.js 连续滚动 + 懒渲染 + 高 DPI + 文字层 + 标注层
// 标注坐标：相对页面盒子归一化到 0..1（左上原点），用百分比定位 → 任意缩放都对齐。
import * as pdfjsLib from '../vendor/pdfjs/pdf.min.mjs';
import { h } from './ui.js';

const V = new URL('../vendor/pdfjs/', import.meta.url).href;
pdfjsLib.GlobalWorkerOptions.workerSrc = V + 'pdf.worker.min.mjs';
const { TextLayer } = pdfjsLib;

const MAX_LIVE_CANVASES = 12;
export const MIN_SCALE = 0.5;
export const MAX_SCALE = 4;

export class PdfView {
  /**
   * @param {HTMLElement} root
   * @param {{onSelection?:Function,onAnnotationClick?:Function,onBlankClick?:Function,onPageChange?:Function,onScaleChange?:Function,onProgress?:Function}} opts
   */
  constructor(root, opts = {}) {
    this.root = root;
    this.opts = opts;
    this.scale = 1.25;
    this.doc = null;
    this.pages = []; // {num, el, canvasWrap, textEl, annoEl, markerEl, w1, h1, canvasScale, rendering, task, textDone}
    this.annotations = [];
    this.current = 1;
    this.queue = new Set();
    this.busy = 0;
    this.destroyed = false;
    this.flashTimer = null;
    this.useEmbeddedFonts = localStorage.getItem('pd.embeddedFonts') !== '0';

    this.scrollEl = h('div', { class: 'pdf-scroll' });
    this.pagesEl = h('div', { class: 'pdf-pages' });
    this.scrollEl.append(this.pagesEl);
    root.append(this.scrollEl);

    this._onScroll = () => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => {
        this._raf = null;
        this._updateCurrent();
      });
    };
    this.scrollEl.addEventListener('scroll', this._onScroll, { passive: true });

    // 选择 / 点击处理
    this._down = null;
    this._onDown = (e) => {
      if (e.button !== 0) return;
      const tl = e.target.closest && e.target.closest('.textLayer');
      if (tl) tl.classList.add('selecting');
      this._down = { x: e.clientX, y: e.clientY, t: Date.now(), inPages: !!e.target.closest('.pdf-page') };
    };
    this._onUp = (e) => {
      this.pagesEl.querySelectorAll('.textLayer.selecting').forEach((t) => t.classList.remove('selecting'));
      const down = this._down;
      this._down = null;
      if (!down || !down.inPages) return;
      setTimeout(() => this._afterMouseUp(e, down), 0);
    };
    this.pagesEl.addEventListener('mousedown', this._onDown);
    document.addEventListener('mouseup', this._onUp);
    // Ctrl/⌘ + 滚轮缩放
    this._onWheel = (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const f = Math.exp(-e.deltaY * 0.0025);
      this.setScale(this.scale * f);
    };
    this.scrollEl.addEventListener('wheel', this._onWheel, { passive: false });
  }

  destroy() {
    this.destroyed = true;
    this.observer && this.observer.disconnect();
    document.removeEventListener('mouseup', this._onUp);
    for (const p of this.pages) p.task && p.task.cancel();
    try { this.loadingTask && this.loadingTask.destroy(); } catch { /* ignore */ }
    try { this.doc && this.doc.destroy(); } catch { /* ignore */ }
    this.root.innerHTML = '';
  }

  get numPages() {
    return this.doc ? this.doc.numPages : 0;
  }

  async load(url, { scale = null, page = 1 } = {}) {
    this.loadingTask = pdfjsLib.getDocument({
      url,
      cMapUrl: V + 'cmaps/',
      cMapPacked: true,
      standardFontDataUrl: V + 'standard_fonts/',
      wasmUrl: V + 'wasm/',
      iccUrl: V + 'iccs/',
      disableRange: true,
      disableAutoFetch: true,
      isEvalSupported: false,
    });
    this.loadingTask.onProgress = ({ loaded, total }) => this.opts.onProgress && this.opts.onProgress(loaded, total);
    this.doc = await this.loadingTask.promise;
    if (this.destroyed) return;
    const first = await this.doc.getPage(1);
    const vp1 = first.getViewport({ scale: 1 });
    const n = this.doc.numPages;
    this.pages = [];
    for (let i = 1; i <= n; i++) {
      this.pages.push({ num: i, w1: vp1.width, h1: vp1.height, canvasScale: null, rendering: false, textDone: false });
    }
    this.scale = clampScale(scale || this.fitWidthScale());
    this._build();
    this._setupObserver();
    if (page > 1) this.scrollToPage(page, { instant: true });
    this._updateCurrent();
    // 后台补齐每页真实尺寸（大多数论文所有页同尺寸）
    this._fixSizes();
    return this.doc;
  }

  async _fixSizes() {
    for (let i = 2; i <= this.numPages; i++) {
      if (this.destroyed) return;
      try {
        const pg = await this.doc.getPage(i);
        const vp = pg.getViewport({ scale: 1 });
        const st = this.pages[i - 1];
        if (Math.abs(vp.width - st.w1) > 0.5 || Math.abs(vp.height - st.h1) > 0.5) {
          const anchor = this._anchor();
          st.w1 = vp.width;
          st.h1 = vp.height;
          this._sizePage(st);
          this._restoreAnchor(anchor);
        }
      } catch { /* ignore */ }
    }
  }

  fitWidthScale() {
    const p = this.pages[0];
    if (!p) return 1.25;
    const avail = (this.scrollEl.clientWidth || 800) - 48;
    return clampScale(avail / p.w1);
  }

  _build() {
    this.pagesEl.innerHTML = '';
    for (const st of this.pages) {
      st.canvasWrap = h('div', { class: 'canvasWrapper' });
      st.annoEl = h('div', { class: 'annoLayer' });
      st.textEl = h('div', { class: 'textLayer' });
      st.markerEl = h('div', { class: 'markerLayer' });
      st.el = h('div', { class: 'pdf-page', dataset: { page: String(st.num) } },
        st.canvasWrap, st.annoEl, st.textEl, st.markerEl,
        h('div', { class: 'page-label' }, String(st.num)));
      this._sizePage(st);
      this.pagesEl.append(st.el);
      this._drawAnnotations(st.num);
    }
  }

  _sizePage(st) {
    const w = Math.floor(st.w1 * this.scale);
    const hh = Math.floor(st.h1 * this.scale);
    st.el.style.width = `${w}px`;
    st.el.style.height = `${hh}px`;
    st.el.style.setProperty('--scale-factor', this.scale);
    st.el.style.setProperty('--total-scale-factor', this.scale);
    st.el.style.setProperty('--scale-round-x', '1px');
    st.el.style.setProperty('--scale-round-y', '1px');
  }

  _setupObserver() {
    this.observer && this.observer.disconnect();
    this.observer = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const num = +en.target.dataset.page;
        if (en.isIntersecting) this.queue.add(num);
        else this.queue.delete(num);
      }
      this._pump();
    }, { root: this.scrollEl, rootMargin: '900px 0px' });
    for (const st of this.pages) this.observer.observe(st.el);
  }

  _pump() {
    if (this.destroyed) return;
    while (this.busy < 2 && this.queue.size) {
      // 优先渲染离当前页最近的
      let best = null;
      for (const n of this.queue) {
        if (best === null || Math.abs(n - this.current) < Math.abs(best - this.current)) best = n;
      }
      this.queue.delete(best);
      const st = this.pages[best - 1];
      if (!st || st.canvasScale === this.scale || st.rendering) continue;
      this.busy++;
      this._renderPage(st).catch((e) => {
        if (!(e && e.name === 'RenderingCancelledException')) console.warn('render page failed', best, e);
      }).finally(() => {
        this.busy--;
        this._pump();
      });
    }
  }

  async _renderPage(st) {
    st.rendering = true;
    const scale = this.scale;
    try {
      const page = await this.doc.getPage(st.num);
      const viewport = page.getViewport({ scale });
      const dpr = window.devicePixelRatio || 1;
      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      const ctx = canvas.getContext('2d', { alpha: false });
      st.task && st.task.cancel();
      st.task = page.render({
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
      });
      await st.task.promise;
      st.task = null;
      if (this.destroyed) return;
      st.canvasWrap.replaceChildren(canvas);
      st.canvasScale = scale;
      // 文字层只渲染一次：pdf.js 的文字层由 --total-scale-factor 驱动，缩放时自动跟随
      await this._renderText(st, page);
      this._trimCanvases();
      if (scale !== this.scale) this.queue.add(st.num); // 渲染期间缩放变了
    } finally {
      st.rendering = false;
    }
  }

  async _renderText(st, page) {
    if (st.textPromise) return st.textPromise;
    st.textPromise = (async () => {
      const pg = page || (await this.doc.getPage(st.num));
      const viewport = pg.getViewport({ scale: this.scale });
      const tc = await pg.getTextContent({ includeMarkedContent: true, disableNormalization: true });
      // 让文字层用 PDF 内嵌字体（pdf.js 渲染 canvas 时已注册到 document.fonts）测量与排版，
      // 这样行内每个字的位置与画面一致；字体未加载时退回 pdf.js 默认的通用字体。
      if (this.useEmbeddedFonts) {
        const loaded = loadedFontFamilies();
        for (const [name, style] of Object.entries(tc.styles || {})) {
          if (loaded.has(name)) style.fontFamily = `"${name}", ${style.fontFamily || 'sans-serif'}`;
        }
      }
      const tl = new TextLayer({
        textContentSource: tc,
        container: st.textEl,
        viewport,
      });
      await tl.render();
      st.textEl.append(h('div', { class: 'endOfContent' }));
      st.textDone = true;
    })();
    return st.textPromise;
  }

  /** 确保某页文字层已渲染（朗读高亮需要），返回该页的文字层元素 */
  async ensureText(num) {
    const st = this.pages[num - 1];
    if (!st) return null;
    await this._renderText(st);
    return st.textEl;
  }

  pageEl(num) {
    const st = this.pages[num - 1];
    return st ? st.el : null;
  }

  _trimCanvases() {
    const live = this.pages.filter((p) => p.canvasScale !== null);
    if (live.length <= MAX_LIVE_CANVASES) return;
    live.sort((a, b) => Math.abs(b.num - this.current) - Math.abs(a.num - this.current));
    for (const p of live.slice(0, live.length - MAX_LIVE_CANVASES)) {
      const c = p.canvasWrap.querySelector('canvas');
      if (c) { c.width = 0; c.height = 0; }
      p.canvasWrap.replaceChildren();
      p.canvasScale = null;
    }
  }

  // ---------------- 缩放 / 导航 ----------------
  _anchor() {
    const st = this.pages[this.current - 1];
    if (!st) return null;
    const top = st.el.offsetTop;
    return { num: st.num, ratio: (this.scrollEl.scrollTop - top) / st.el.offsetHeight };
  }

  _restoreAnchor(a) {
    if (!a) return;
    const st = this.pages[a.num - 1];
    this.scrollEl.scrollTop = st.el.offsetTop + a.ratio * st.el.offsetHeight;
  }

  setScale(s) {
    s = clampScale(s);
    if (!this.doc || Math.abs(s - this.scale) < 0.001) return;
    const anchor = this._anchor();
    const sx = this.scrollEl.scrollLeft / Math.max(1, this.scrollEl.scrollWidth - this.scrollEl.clientWidth);
    this.scale = s;
    for (const st of this.pages) this._sizePage(st);
    this._restoreAnchor(anchor);
    this.scrollEl.scrollLeft = sx * Math.max(0, this.scrollEl.scrollWidth - this.scrollEl.clientWidth);
    clearTimeout(this._rerenderT);
    // 停止缩放 150ms 后重新渲染可见页（期间旧位图被 CSS 拉伸显示）
    this._rerenderT = setTimeout(() => {
      for (const st of this.pages) if (this._isNearView(st)) this.queue.add(st.num);
      this._pump();
    }, 150);
    this.opts.onScaleChange && this.opts.onScaleChange(s);
  }

  _isNearView(st) {
    const top = st.el.offsetTop;
    const bottom = top + st.el.offsetHeight;
    const vt = this.scrollEl.scrollTop - 900;
    const vb = this.scrollEl.scrollTop + this.scrollEl.clientHeight + 900;
    return bottom > vt && top < vb;
  }

  _updateCurrent() {
    if (!this.pages.length) return;
    const y = this.scrollEl.scrollTop + this.scrollEl.clientHeight * 0.3;
    let lo = 0;
    let hi = this.pages.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.pages[mid].el.offsetTop <= y) lo = mid;
      else hi = mid - 1;
    }
    const cur = lo + 1;
    if (cur !== this.current) {
      this.current = cur;
      this.opts.onPageChange && this.opts.onPageChange(cur);
    }
  }

  scrollToPage(num, { y = null, instant = false } = {}) {
    const st = this.pages[num - 1];
    if (!st) return;
    let top = st.el.offsetTop - 16;
    if (y !== null) top = st.el.offsetTop + y * st.el.offsetHeight - this.scrollEl.clientHeight * 0.35;
    this.scrollEl.scrollTo({ top: Math.max(0, top), behavior: instant ? 'auto' : 'smooth' });
    if (instant) this._updateCurrent();
  }

  // ---------------- 选择 ----------------
  _afterMouseUp(e, down) {
    const sel = window.getSelection();
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const info = this.getSelectionInfo();
      if (info) {
        this.opts.onSelection && this.opts.onSelection(info);
        return;
      }
    }
    if (moved < 5) {
      const hit = this.hitTest(e.clientX, e.clientY);
      if (hit) this.opts.onAnnotationClick && this.opts.onAnnotationClick(hit, e.clientX, e.clientY);
      else this.opts.onBlankClick && this.opts.onBlankClick();
    }
  }

  /** 当前选区 → {page, quote, rects(归一化), clientRect} */
  getSelectionInfo() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    if (!this.pagesEl.contains(range.commonAncestorContainer)) return null;
    const quote = sel.toString().replace(/-\n(?=\w)/g, '').replace(/\s+/g, ' ').trim();
    if (!quote) return null;
    const startNode = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    let pageEl = startNode && startNode.closest('.pdf-page');
    const raw = rangeTextRects(range);
    if (!pageEl) {
      // 选区从页面外开始：取第一个落在页面上的矩形
      for (const r of raw) {
        const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        pageEl = el && el.closest('.pdf-page');
        if (pageEl) break;
      }
    }
    if (!pageEl) return null;
    const page = +pageEl.dataset.page;
    const rects = normalizeRects(raw, pageEl.getBoundingClientRect());
    if (!rects.length) return null;
    const br = range.getBoundingClientRect();
    return { page, quote, rects, clientRect: br };
  }

  clearSelection() {
    const sel = window.getSelection();
    sel && sel.removeAllRanges();
  }

  /** 屏幕坐标命中的标注（取面积最小的） */
  hitTest(cx, cy) {
    const el = document.elementFromPoint(cx, cy);
    const pageEl = el && el.closest && el.closest('.pdf-page');
    if (!pageEl || !this.pagesEl.contains(pageEl)) return null;
    const page = +pageEl.dataset.page;
    const pr = pageEl.getBoundingClientRect();
    const x = (cx - pr.left) / pr.width;
    const y = (cy - pr.top) / pr.height;
    const padX = 3 / pr.width;
    const padY = 3 / pr.height;
    let best = null;
    let bestArea = Infinity;
    for (const a of this.annotations) {
      if (a.page !== page) continue;
      for (const [rx, ry, rw, rh] of a.rects || []) {
        if (x >= rx - padX && x <= rx + rw + padX && y >= ry - padY && y <= ry + rh + padY) {
          const area = rw * rh;
          if (area < bestArea) { best = a; bestArea = area; }
        }
      }
    }
    return best;
  }

  // ---------------- 标注 ----------------
  setAnnotations(list) {
    this.annotations = (list || []).slice();
    for (const st of this.pages) this._drawAnnotations(st.num);
  }

  upsertAnnotation(a) {
    const i = this.annotations.findIndex((x) => x.id === a.id);
    const oldPage = i >= 0 ? this.annotations[i].page : null;
    if (i >= 0) this.annotations[i] = a;
    else this.annotations.push(a);
    this._drawAnnotations(a.page);
    if (oldPage && oldPage !== a.page) this._drawAnnotations(oldPage);
  }

  removeAnnotation(id) {
    const i = this.annotations.findIndex((x) => x.id === id);
    if (i < 0) return;
    const [a] = this.annotations.splice(i, 1);
    this._drawAnnotations(a.page);
  }

  /** 由外部（笔记数量等）决定是否显示气泡 */
  setMarkerResolver(fn) {
    this.markerResolver = fn;
    for (const st of this.pages) this._drawAnnotations(st.num);
  }

  _drawAnnotations(num) {
    const st = this.pages[num - 1];
    if (!st || !st.annoEl) return;
    st.annoEl.replaceChildren();
    st.markerEl.replaceChildren();
    for (const a of this.annotations) {
      if (a.page !== num) continue;
      const kind = a.kind || 'highlight';
      const color = a.color || 'yellow';
      const rects = a.rects || [];
      for (const [x, y, w, hgt] of rects) {
        st.annoEl.append(h('div', {
          class: `anno anno-${kind} c-${color}`,
          dataset: { id: a.id },
          style: { left: pct(x), top: pct(y), width: pct(w), height: pct(hgt) },
        }));
      }
      const hasNote = (a.comment && a.comment.trim()) || (this.markerResolver && this.markerResolver(a));
      if (hasNote && rects.length) {
        // 气泡放在最后一个矩形的右上角
        const last = rects.reduce((m, r) => (r[0] + r[2] > m[0] + m[2] ? r : m), rects[0]);
        const first = rects[0];
        const mk = h('button', {
          class: `anno-marker c-${color}`,
          title: '查看批注',
          dataset: { id: a.id },
          style: { left: `calc(${pct(Math.min(last[0] + last[2], 0.985))} + 2px)`, top: `calc(${pct(first[1])} - 6px)` },
          onmousedown: (e) => e.stopPropagation(),
          onclick: (e) => {
            e.stopPropagation();
            const r = e.currentTarget.getBoundingClientRect();
            this.opts.onAnnotationClick && this.opts.onAnnotationClick(a, r.left, r.bottom);
          },
        });
        mk.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 18V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8z"/></svg>';
        st.markerEl.append(mk);
      }
    }
  }

  flashAnnotation(id) {
    const els = this.pagesEl.querySelectorAll(`.anno[data-id="${CSS.escape(id)}"]`);
    els.forEach((el) => {
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    });
    clearTimeout(this.flashTimer);
    this.flashTimer = setTimeout(() => els.forEach((el) => el.classList.remove('flash')), 2200);
  }

  goToAnnotation(a) {
    if (!a) return;
    const y = a.rects && a.rects.length ? a.rects[0][1] : null;
    this.scrollToPage(a.page, { y });
    setTimeout(() => this.flashAnnotation(a.id), 350);
  }

  /** 在页面上临时闪烁一个区域（找不到标注时用引文定位） */
  flashRects(page, rects) {
    const st = this.pages[page - 1];
    if (!st) return;
    for (const [x, y, w, hgt] of rects) {
      const d = h('div', { class: 'anno anno-highlight c-blue flash temp', style: { left: pct(x), top: pct(y), width: pct(w), height: pct(hgt) } });
      st.annoEl.append(d);
      setTimeout(() => d.remove(), 2300);
    }
  }

  // ---------------- 文字 ----------------
  async _textItems(num) {
    const page = await this.doc.getPage(num);
    const vp = page.getViewport({ scale: 1 });
    const tc = await page.getTextContent();
    return { items: tc.items.filter((it) => typeof it.str === 'string'), vp };
  }

  /** 每页纯文本（按行） */
  async getPageText(num) {
    const { items } = await this._textItems(num);
    let out = '';
    for (const it of items) {
      out += it.str;
      out += it.hasEOL ? '\n' : '';
    }
    return out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  /** 每页段落：按行距 / 字号变化 / 标题行 / 段末短行切段，合并连字符换行 */
  async getPageParagraphs(num) {
    const { items } = await this._textItems(num);
    const lines = [];
    let cur = { text: '', y: null, h: 0, x: null };
    for (const it of items) {
      if (cur.y === null && it.str.trim()) {
        cur.y = it.transform[5];
        cur.x = it.transform[4];
        cur.h = Math.abs(it.transform[3]) || it.height || 10;
      }
      cur.text += it.str;
      if (it.hasEOL) {
        if (cur.text.trim()) lines.push(cur);
        cur = { text: '', y: null, h: 0, x: null };
      }
    }
    if (cur.text.trim()) lines.push(cur);
    if (!lines.length) return [];
    const gaps = [];
    for (let i = 1; i < lines.length; i++) {
      const g = lines[i - 1].y - lines[i].y;
      if (g > 0) gaps.push(g);
    }
    gaps.sort((a, b) => a - b);
    const med = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 12;
    const lens = lines.map((l) => l.text.trim().length).sort((a, b) => a - b);
    const medLen = lens[Math.floor(lens.length / 2)] || 60;
    const isHead = (t) => t.length < 80 && /^((\d{1,2}(\.\d{1,2}){0,2}\.?|[IVX]{1,5}\.|[A-H](\.\d)*\.?)\s+[A-Z]|(Abstract|Introduction|Conclusions?|References|Acknowledg(e)?ments?|Appendix|Related Work|Discussion|Method(s)?|Experiments?|Results)\s*$)/.test(t) && !/[.,;:]$/.test(t);
    const paras = [];
    let buf = '';
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i].text.trim();
      if (i > 0 && buf) {
        const prev = lines[i - 1];
        const g = prev.y - lines[i].y;
        const sizeJump = Math.abs(lines[i].h - prev.h) / Math.max(prev.h, 1) > 0.18;
        const prevShortEnd = prev.text.trim().length < medLen * 0.7 && /[.!?:。！？]$/.test(prev.text.trim());
        if (g > med * 1.45 || g < -med * 2 || sizeJump || isHead(prev.text.trim()) || isHead(ln) || prevShortEnd) {
          paras.push(buf);
          buf = '';
        }
      }
      if (buf.endsWith('-') && /^[a-z]/.test(ln)) buf = buf.slice(0, -1) + ln;
      else buf = buf ? `${buf} ${ln}` : ln;
    }
    if (buf) paras.push(buf);
    return paras.map((p) => p.replace(/\s+/g, ' ').trim()).filter((p) => p.length > 1);
  }

  async getAllText(onProgress) {
    const out = [];
    for (let i = 1; i <= this.numPages; i++) {
      if (this.destroyed) break;
      out.push({ page: i, text: await this.getPageText(i) });
      onProgress && onProgress(i, this.numPages);
    }
    return out;
  }
}

/** 只取选区内文字节点的矩形（排除 br / 元素盒子等干扰） */
function rangeTextRects(range) {
  const out = [];
  const root = range.commonAncestorContainer;
  if (root.nodeType === 3) {
    for (const r of range.getClientRects()) out.push(r);
    return out;
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (!range.intersectsNode(node)) continue;
    if (!node.parentElement || !node.parentElement.closest('.textLayer')) continue;
    const start = node === range.startContainer ? range.startOffset : 0;
    const end = node === range.endContainer ? range.endOffset : node.length;
    if (end <= start) continue;
    const sub = document.createRange();
    sub.setStart(node, start);
    sub.setEnd(node, end);
    for (const r of sub.getClientRects()) out.push(r);
  }
  return out.length ? out : Array.from(range.getClientRects());
}

function loadedFontFamilies() {
  const set = new Set();
  try {
    for (const f of document.fonts) if (f.status === 'loaded') set.add(f.family.replace(/^["']|["']$/g, ''));
  } catch { /* ignore */ }
  return set;
}

function pct(v) {
  return `${(v * 100).toFixed(4)}%`;
}

export function clampScale(s) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s || 1));
}

/**
 * 把 Range.getClientRects() 的结果换算到页面归一化坐标并合并：
 * 1) 过滤零面积、页外、异常大的矩形（如 endOfContent）；
 * 2) 按行分组（垂直重叠 > 50%）；3) 同行相邻矩形（间隙 < 行高）合并为一个。
 */
export function normalizeRects(clientRects, pr) {
  const rs = [];
  for (const r of clientRects) {
    if (r.width < 0.5 || r.height < 0.5) continue;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < pr.left || cx > pr.right || cy < pr.top || cy > pr.bottom) continue;
    if (r.height > pr.height * 0.12) continue;
    if (r.width > pr.width * 0.97 && r.height > pr.height * 0.03) continue;
    const left = Math.max(r.left, pr.left);
    const right = Math.min(r.right, pr.right);
    const top = Math.max(r.top, pr.top);
    const bottom = Math.min(r.bottom, pr.bottom);
    rs.push({ l: left, r: right, t: top, b: bottom });
  }
  if (!rs.length) return [];
  rs.sort((a, b) => (a.t + a.b) / 2 - (b.t + b.b) / 2 || a.l - b.l);
  const lines = [];
  for (const r of rs) {
    const hgt = r.b - r.t;
    let line = null;
    for (const ln of lines) {
      const ov = Math.min(ln.b, r.b) - Math.max(ln.t, r.t);
      if (ov > 0.5 * Math.min(hgt, ln.b - ln.t)) { line = ln; break; }
    }
    if (line) line.items.push(r), line.t = Math.min(line.t, r.t), line.b = Math.max(line.b, r.b);
    else lines.push({ t: r.t, b: r.b, items: [r] });
  }
  const out = [];
  for (const ln of lines) {
    ln.items.sort((a, b) => a.l - b.l);
    const lh = ln.b - ln.t;
    let cur = null;
    for (const r of ln.items) {
      if (cur && r.l - cur.r < lh * 1.0) {
        cur.r = Math.max(cur.r, r.r);
        cur.t = Math.min(cur.t, r.t);
        cur.b = Math.max(cur.b, r.b);
      } else {
        if (cur) out.push(cur);
        cur = { ...r };
      }
    }
    if (cur) out.push(cur);
  }
  return out.map((r) => [
    round6((r.l - pr.left) / pr.width),
    round6((r.t - pr.top) / pr.height),
    round6((r.r - r.l) / pr.width),
    round6((r.b - r.t) / pr.height),
  ]);
}

function round6(v) {
  return Math.round(v * 1e6) / 1e6;
}
