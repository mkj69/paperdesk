// 听论文（Audiobook）：文本清洗 → 逐句（≤220）朗读 → textLayer 高亮当前句 + 自动滚动 → 自动翻页
// 进度条 / 章节跳转 / 双语朗读 / J K L 快捷键 / Media Session / tts_pos 位置记忆（SPEC §8 §11.1）
import { post, patch, STATIC } from './api.js';
import { h, icon, toast } from './ui.js';
import { tts, splitSentences, chunkLong, detectLang } from './tts.js';

const LS = { bilingual: 'pd.book.bilingual', skipRefs: 'pd.book.skipRefs', stripCites: 'pd.book.stripCites' };
export const REF_RE = /^\s*(?:\d+\.?\s*|[IVX]+\.?\s*)?(References|REFERENCES|Bibliography|BIBLIOGRAPHY|Works Cited|Literature Cited|参考文献)\s*$/;
const NUM_HEADING_RE = /^(?:(\d{1,2}(?:\.\d{1,2}){0,2})\.?|([IVX]{1,5})\.|([A-H])(?:\.\d{1,2}){0,2}\.?)\s+([A-Z][^.!?]{1,72})$/;
const NAMED_HEADING_RE = /^(Abstract|Introduction|Background|Related Work|Prior Work|Preliminaries|Problem (Setup|Formulation)|Methods?|Methodology|Approach|Our Approach|Model|Experiments?|Experimental (Setup|Results)|Results|Evaluation|Analysis|Discussion|Limitations|Conclusions?|Conclusion and Future Work|Future Work|Appendix|Appendices|Acknowledg(e)?ments?|摘要|引言|相关工作|方法|实验|结论)\s*$/i;
const MATH_CHARS = /[=+−×÷∑∏∫√∞≤≥≈≠∈∉⊂⊆∪∩∀∃∂∇→←↔⇒⇔±·∘⊗⊕‖|^_{}\\<>Ͱ-Ͽ∀-⋿⟀-⟯⦀-⧿\u{1d400}-\u{1d7ff}]/gu;

export function isHeadingLine(t) {
  const s = t.trim();
  if (s.length < 3 || s.length > 80) return false;
  if (NAMED_HEADING_RE.test(s)) return true;
  const m = s.match(NUM_HEADING_RE);
  if (!m) return false;
  const words = m[4].trim().split(/\s+/);
  if (words.length > 10) return false;
  if (/\d{3,}/.test(m[4])) return false;
  return true;
}

export function isFormulaLine(t) {
  const s = t.trim();
  if (!s) return false;
  if (/^\(\d{1,3}\)$/.test(s)) return true; // 公式编号
  const len = s.replace(/\s/g, '').length;
  const words = (s.match(/[A-Za-z]{3,}/g) || []).length;
  const maths = (s.match(MATH_CHARS) || []).length;
  const digits = (s.match(/\d/g) || []).length;
  if (len <= 3 && words === 0) return true;
  if (words <= 2 && (maths + digits) / len > 0.35) return true;
  if (words <= 4 && maths / len > 0.25 && maths >= 3) return true;
  return false;
}

/** 去引用标记：[12] [3, 7] [3–7] (Vaswani et al., 2017) (Smith and Lee, 2020; Wu, 2021) */
const CITE_RES = [
  /\s?\[(?:\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]/g,
  /\s?\((?:[A-Z][A-Za-z'’\-]+(?:\s+et\s+al\.)?(?:,?\s+(?:and|&)\s+[A-Z][A-Za-z'’\-]+)?,?\s+\d{4}[a-z]?(?:;\s*)?)+\)/g,
];

function headerKey(s) {
  return s.toLowerCase().replace(/\d+/g, '#').replace(/\s+/g, ' ').trim();
}

export class Audiobook {
  constructor(ctx) {
    this.ctx = ctx;
    this.pageCache = new Map();
    this.lineKeyCache = new Map();
    this.trCache = new Map();
    this.tok = 0;
    this.playing = false;
    this.paused = false;
    let saved = ctx.paper.tts_pos;
    if (STATIC) {
      try { saved = JSON.parse(localStorage.getItem(`pd.ttspos.${ctx.pid}.${ctx.iid}`) || 'null') || saved; } catch { /* ignore */ }
    }
    this.pos = saved ? { page: saved.page || 1, s: saved.sentence || 0 } : null;
    this.current = null; // {page, s, item, total}
    this.listeners = new Set();
    this.sections = null;
    this.opts = {
      bilingual: localStorage.getItem(LS.bilingual) === '1',
      skipRefs: localStorage.getItem(LS.skipRefs) !== '0',
      stripCites: localStorage.getItem(LS.stripCites) !== '0',
    };
    this.highlighted = [];
    this.followPausedUntil = 0;
    this._lastSave = 0;
    this._audio = null;
    this._onUserScroll = () => { if (this.playing) this.followPausedUntil = Date.now() + 5000; };
    this._ttsOff = tts.on((ev) => {
      if (ev.type === 'state' && this.playing && tts.owner !== 'book' && ev.state === 'idle' && tts.owner === null) {
        // 被别处（如语音回答、喇叭按钮）打断
      }
    });
  }

  onDocReady() {
    const el = this.ctx.pdf.scrollEl;
    el.addEventListener('wheel', this._onUserScroll, { passive: true });
    el.addEventListener('touchmove', this._onUserScroll, { passive: true });
    this._emit();
  }

  on(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  _emit(extra = {}) {
    for (const fn of this.listeners) {
      try { fn({ ...extra }); } catch (e) { console.warn(e); }
    }
  }

  setOpt(k, v) {
    this.opts[k] = v;
    localStorage.setItem(LS[k], v ? '1' : '0');
    if (k === 'skipRefs' || k === 'stripCites') this.pageCache.clear();
    this._emit();
  }

  get numPages() {
    return this.ctx.pdf ? this.ctx.pdf.numPages : 0;
  }

  // ---------------- 文本准备 ----------------
  async _lineKeys(num) {
    if (this.lineKeyCache.has(num)) return this.lineKeyCache.get(num);
    let res = { first: [], last: [] };
    try {
      const lines = (await this.ctx.pdf.getPageText(num)).split('\n').map((s) => s.trim()).filter(Boolean);
      res = { first: lines.slice(0, 2).map(headerKey), last: lines.slice(-2).map(headerKey) };
    } catch { /* ignore */ }
    this.lineKeyCache.set(num, res);
    return res;
  }

  async _stripHeaderFooter(num, lines) {
    if (lines.length < 4) return lines;
    const n = this.numPages;
    const neigh = [];
    for (const d of [-2, -1, 1, 2]) if (num + d >= 1 && num + d <= n) neigh.push(await this._lineKeys(num + d));
    const isRunning = (text, where) => {
      const t = text.trim();
      if (t.length > 90) return false;
      if (/^(page\s*)?\d{1,4}(\s*(of|\/)\s*\d+)?$/i.test(t) || /^[ivxlc]{1,6}$/i.test(t)) return true;
      const k = headerKey(t);
      if (k.length < 3) return false;
      return neigh.some((nb) => nb[where].includes(k));
    };
    let a = 0;
    while (a < 2 && a < lines.length && isRunning(lines[a].text, 'first')) a++;
    let b = lines.length;
    while (b > a && lines.length - b < 2 && isRunning(lines[b - 1].text, 'last')) b--;
    return lines.slice(a, b);
  }

  /** 构建一页的朗读条目：[{text, lang, kind:'text'|'heading'|'formula', spans:[HTMLElement]}] */
  async buildPage(num) {
    if (this.pageCache.has(num)) return this.pageCache.get(num);
    const textEl = await this.ctx.pdf.ensureText(num);
    const raw = [];
    let cur = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (ch.tagName === 'BR') {
          raw.push(cur);
          cur = [];
        } else if (ch.tagName === 'SPAN') {
          if (ch.classList.contains('markedContent') || ch.querySelector('span')) walk(ch);
          else if (ch.getAttribute('role') !== 'img') cur.push(ch);
        }
      }
    };
    if (textEl) walk(textEl);
    if (cur.length) raw.push(cur);
    const fh = (sp) => parseFloat(sp.style.getPropertyValue('--font-height')) || 0;
    let lines = raw.map((spans) => ({ spans, text: spans.map((s) => s.textContent).join(''), fh: Math.max(0, ...spans.map(fh)) })).filter((l) => l.text.trim());
    const hs = lines.map((l) => l.fh).filter(Boolean).sort((a, b) => a - b);
    const medH = hs.length ? hs[Math.floor(hs.length / 2)] : 0;
    lines = await this._stripHeaderFooter(num, lines);

    let stop = false;
    if (this.opts.skipRefs) {
      const i = lines.findIndex((l) => REF_RE.test(l.text));
      if (i >= 0) {
        lines = lines.slice(0, i);
        stop = true;
      }
    }

    const items = [];
    const spanList = [];
    let buf = '';
    let owner = [];
    const flush = () => {
      if (!buf.trim()) { buf = ''; owner = []; return; }
      let text = buf;
      let own = owner;
      if (this.opts.stripCites) {
        for (const re of CITE_RES) {
          const keep = new Array(text.length).fill(true);
          re.lastIndex = 0;
          let m;
          while ((m = re.exec(text))) for (let k = m.index; k < m.index + m[0].length; k++) keep[k] = false;
          let t2 = '';
          const o2 = [];
          for (let k = 0; k < text.length; k++) if (keep[k]) { t2 += text[k]; o2.push(own[k]); }
          text = t2;
          own = o2;
        }
      }
      for (const sen of splitSentences(text)) {
        for (const ch of chunkLong(text.slice(sen.start, sen.end))) {
          const a = sen.start + ch.start;
          const b = sen.start + ch.end;
          const set = new Set();
          for (let k = a; k < b; k++) if (own[k] >= 0) set.add(own[k]);
          const t = ch.text.replace(/\s+/g, ' ').trim();
          if (t.length < 2 || !/[\p{L}\p{N}]/u.test(t)) continue;
          items.push({ text: t, lang: detectLang(t), kind: 'text', spans: Array.from(set).sort((x, y) => x - y).map((i) => spanList[i]) });
        }
      }
      buf = '';
      owner = [];
    };
    for (const ln of lines) {
      const t = ln.text;
      if (isFormulaLine(t)) {
        flush();
        const last = items[items.length - 1];
        if (last && last.kind === 'formula') last.spans.push(...ln.spans);
        else items.push({ text: '公式略', lang: 'zh', kind: 'formula', spans: ln.spans.slice() });
        continue;
      }
      if (isHeadingLine(t) || (medH && ln.fh > medH * 1.3 && t.trim().length < 140)) {
        flush();
        items.push({ text: t.trim(), lang: detectLang(t), kind: 'heading', spans: ln.spans.slice() });
        continue;
      }
      // 去连字符断词：上一行以 "-" 结尾、这一行小写开头
      if (/[A-Za-z]- $/.test(buf) && /^[a-z]/.test(t.trimStart())) {
        buf = buf.slice(0, -2);
        owner = owner.slice(0, -2);
      }
      for (const sp of ln.spans) {
        const idx = spanList.push(sp) - 1;
        const s = sp.textContent;
        buf += s;
        for (let k = 0; k < s.length; k++) owner.push(idx);
      }
      if (!buf.endsWith(' ')) { buf += ' '; owner.push(-1); }
    }
    flush();
    const res = { items, stop };
    this.pageCache.set(num, res);
    return res;
  }

  // ---------------- 播放控制 ----------------
  /** 从 (page, s) 开始；s 可为 {frac} 表示页内比例 */
  async playFrom(page, s = 0) {
    if (!tts.supported) {
      toast('这个浏览器不支持朗读（speechSynthesis）', 'error');
      return;
    }
    if (!this.numPages) return;
    const tok = ++this.tok;
    tts.stop();
    tts.owner = 'book';
    this.playing = true;
    this.paused = false;
    this._startMedia();
    this._emit();
    let p = Math.max(1, Math.min(this.numPages, page));
    let i = s;
    while (tok === this.tok) {
      if (p > this.numPages) break;
      let pg;
      try {
        pg = await this.buildPage(p);
      } catch (e) {
        console.warn('build page failed', p, e);
        p++;
        i = 0;
        continue;
      }
      if (tok !== this.tok) return;
      if (typeof i === 'object' && i) i = Math.floor((i.frac || 0) * pg.items.length);
      if (i < 0) i = Math.max(0, pg.items.length + i);
      if (i >= pg.items.length) {
        if (pg.stop) break;
        p++;
        i = 0;
        continue;
      }
      const it = pg.items[i];
      this.pos = { page: p, s: i };
      this.current = { page: p, s: i, item: it, total: pg.items.length };
      this._highlight(it, p);
      this._emit({ sentence: it.text });
      if (this.opts.bilingual) this._prefetch(pg.items, i);
      const ok = await tts.say(it.text, it.lang);
      if (!ok || tok !== this.tok) return;
      if (this.opts.bilingual && it.kind === 'text' && it.lang === 'en') {
        const tr = await this._translate(it.text);
        if (tok !== this.tok) return;
        if (tr) {
          this._emit({ sentence: it.text, translation: tr });
          const ok2 = await tts.say(tr, 'zh');
          if (!ok2 || tok !== this.tok) return;
        }
      }
      this._savePos();
      i++;
    }
    if (tok === this.tok) {
      this.playing = false;
      this.paused = false;
      this._clearHighlight();
      this._stopMedia();
      tts.done('book');
      this._savePos(true);
      this._emit({ ended: true });
      toast('读完了', 'ok', 1600);
    }
  }

  async playWhole() {
    const p = this.pos;
    if (p && (p.page > 1 || p.s > 0)) return this.playFrom(p.page, p.s);
    return this.playFrom(1, 0);
  }

  playFromCurrentPage() {
    return this.playFrom(this.ctx.currentPage(), 0);
  }

  async playSelection() {
    const sel = window.getSelection();
    const text = sel && !sel.isCollapsed ? sel.toString().trim() : '';
    if (!text) {
      toast('先在 PDF 里选中一段文字', 'info');
      return;
    }
    this.stop();
    tts.speak(text.replace(/-\n(?=[a-z])/g, '').replace(/\s+/g, ' '));
  }

  pause() {
    if (!this.playing || this.paused) return;
    tts.pause();
    this.paused = true;
    this._pausedAt = Date.now();
    this._savePos(true);
    this._mediaState('paused');
    this._emit();
  }

  resume() {
    if (!this.playing) {
      if (this.pos) return this.playFrom(this.pos.page, this.pos.s);
      return this.playFromCurrentPage();
    }
    if (!this.paused) return;
    // Chrome 的 resume 偶尔失效：暂停超过 10 秒就从当前句重读
    if (this._pausedAt && Date.now() - this._pausedAt > 10000) {
      return this.playFrom(this.pos.page, this.pos.s);
    }
    tts.resume();
    this.paused = false;
    this._mediaState('playing');
    this._emit();
  }

  toggle() {
    if (!this.playing) return this.pos ? this.playFrom(this.pos.page, this.pos.s) : this.playFromCurrentPage();
    if (this.paused) return this.resume();
    return this.pause();
  }

  /** 停止（保留位置） */
  stop() {
    if (!this.playing) return;
    this.tok++;
    if (tts.owner === 'book') tts.stop();
    this.playing = false;
    this.paused = false;
    this._clearHighlight();
    this._stopMedia();
    this._savePos(true);
    this._emit();
  }

  next() {
    const p = this.pos || { page: this.ctx.currentPage(), s: -1 };
    return this.playFrom(p.page, p.s + 1);
  }

  async prev() {
    const p = this.pos || { page: this.ctx.currentPage(), s: 0 };
    if (p.s > 0) return this.playFrom(p.page, p.s - 1);
    if (p.page > 1) return this.playFrom(p.page - 1, -1);
    return this.playFrom(1, 0);
  }

  nextPage() {
    const p = this.pos ? this.pos.page : this.ctx.currentPage();
    return this.playFrom(Math.min(this.numPages, p + 1), 0);
  }

  prevPage() {
    const p = this.pos ? this.pos.page : this.ctx.currentPage();
    return this.playFrom(Math.max(1, p - 1), 0);
  }

  seek(frac) {
    const n = this.numPages;
    if (!n) return;
    const x = Math.max(0, Math.min(0.9999, frac)) * n;
    const page = Math.floor(x) + 1;
    return this.playFrom(page, { frac: x - Math.floor(x) });
  }

  progress() {
    if (!this.pos || !this.numPages) return 0;
    const pg = this.pageCache.get(this.pos.page);
    const within = pg && pg.items.length ? this.pos.s / pg.items.length : 0;
    return (this.pos.page - 1 + within) / this.numPages;
  }

  currentSentenceText() {
    return this.current && this.playing ? this.current.item.text : '';
  }

  // ---------------- 高亮 / 跟随 ----------------
  _highlight(it, page) {
    this._clearHighlight();
    this.highlighted = it.spans || [];
    for (const sp of this.highlighted) sp.classList.add('tts-cur');
    if (Date.now() < this.followPausedUntil) return;
    const pdf = this.ctx.pdf;
    const first = this.highlighted[0];
    if (!first) {
      if (pdf.current !== page) pdf.scrollToPage(page);
      return;
    }
    const box = pdf.scrollEl.getBoundingClientRect();
    const r = first.getBoundingClientRect();
    if (r.top < box.top + box.height * 0.12 || r.bottom > box.top + box.height * 0.85) {
      pdf.scrollEl.scrollTo({ top: pdf.scrollEl.scrollTop + (r.top - box.top) - box.height * 0.35, behavior: 'smooth' });
    }
  }

  _clearHighlight() {
    for (const sp of this.highlighted) sp.classList.remove('tts-cur');
    this.highlighted = [];
  }

  // ---------------- 双语 ----------------
  _translate(text) {
    if (STATIC) return Promise.resolve(null);
    const key = `${this.ctx.tengine}|${text}`;
    if (!this.trCache.has(key)) {
      this.trCache.set(key, post('/translate', { text, engine: this.ctx.tengine, target: this.ctx.settings.target_lang || 'zh-CN' })
        .then((r) => r.translation)
        .catch((e) => {
          this.trCache.delete(key);
          console.warn('翻译失败', e);
          return null;
        }));
    }
    return this.trCache.get(key);
  }

  _prefetch(items, i) {
    for (let k = i; k < Math.min(items.length, i + 3); k++) {
      if (items[k].kind === 'text' && items[k].lang === 'en') this._translate(items[k].text);
    }
  }

  // ---------------- 位置记忆 ----------------
  _savePos(force = false) {
    if (!this.pos) return;
    const now = Date.now();
    if (!force && now - this._lastSave < 5000) return;
    this._lastSave = now;
    const tts_pos = { page: this.pos.page, sentence: this.pos.s };
    this.ctx.paper.tts_pos = tts_pos;
    if (STATIC) localStorage.setItem(`pd.ttspos.${this.ctx.pid}.${this.ctx.iid}`, JSON.stringify(tts_pos));
    else patch(this.ctx.base, { tts_pos }).catch(() => {});
  }

  // ---------------- 章节 ----------------
  async getSections() {
    if (this.sections) return this.sections;
    const out = [];
    const doc = this.ctx.pdf.doc;
    try {
      const ol = await doc.getOutline();
      const walk = async (nodes, depth) => {
        for (const nd of nodes || []) {
          try {
            let dest = nd.dest;
            if (typeof dest === 'string') dest = await doc.getDestination(dest);
            if (Array.isArray(dest) && dest[0] !== undefined) {
              const ref = dest[0];
              const idx = typeof ref === 'object' ? await doc.getPageIndex(ref) : ref;
              out.push({ title: nd.title, page: idx + 1, depth });
            }
          } catch { /* ignore */ }
          if (depth < 1 && nd.items && nd.items.length) await walk(nd.items, depth + 1);
        }
      };
      if (ol && ol.length) await walk(ol, 0);
    } catch { /* ignore */ }
    if (!out.length) {
      for (let p = 1; p <= this.numPages; p++) {
        let text = '';
        try { text = await this.ctx.pdf.getPageText(p); } catch { continue; }
        for (const line of text.split('\n')) {
          const t = line.trim();
          if (REF_RE.test(t)) out.push({ title: t, page: p, heading: t, depth: 0 });
          else if (isHeadingLine(t)) out.push({ title: t, page: p, heading: t, depth: /^\d+\.\d/.test(t) ? 1 : 0 });
        }
      }
    }
    this.sections = out;
    return out;
  }

  async playSection(sec) {
    if (!sec) return;
    let s = 0;
    if (sec.heading) {
      try {
        const pg = await this.buildPage(sec.page);
        const norm = (x) => x.replace(/\s+/g, ' ').trim().toLowerCase();
        const i = pg.items.findIndex((it) => it.kind === 'heading' && norm(it.text) === norm(sec.heading));
        if (i >= 0) s = i;
      } catch { /* ignore */ }
    }
    this.ctx.pdf.scrollToPage(sec.page);
    return this.playFrom(sec.page, s);
  }

  // ---------------- Media Session ----------------
  _startMedia() {
    try {
      if (!this._audio) {
        this._audio = new Audio(silentWavUrl());
        this._audio.loop = true;
      }
      this._audio.play().catch(() => {});
    } catch { /* ignore */ }
    const ms = navigator.mediaSession;
    if (!ms) return;
    try {
      const meta = this.ctx.paper.meta || {};
      ms.metadata = new MediaMetadata({
        title: meta.title || this.ctx.item.title || 'PaperDesk',
        artist: (meta.authors || this.ctx.item.authors || []).slice(0, 3).join(', '),
        album: 'PaperDesk',
      });
      ms.setActionHandler('play', () => this.resume());
      ms.setActionHandler('pause', () => this.pause());
      ms.setActionHandler('previoustrack', () => this.prev());
      ms.setActionHandler('nexttrack', () => this.next());
      ms.setActionHandler('stop', () => this.stop());
      ms.playbackState = 'playing';
    } catch { /* ignore */ }
  }

  _mediaState(st) {
    try {
      if (navigator.mediaSession) navigator.mediaSession.playbackState = st;
      if (this._audio) st === 'paused' ? this._audio.pause() : this._audio.play().catch(() => {});
    } catch { /* ignore */ }
  }

  _stopMedia() {
    try {
      if (this._audio) this._audio.pause();
      if (navigator.mediaSession) navigator.mediaSession.playbackState = 'none';
    } catch { /* ignore */ }
  }

  destroy() {
    this.stop();
    this._savePos(true);
    this._ttsOff && this._ttsOff();
    try {
      const el = this.ctx.pdf && this.ctx.pdf.scrollEl;
      el && el.removeEventListener('wheel', this._onUserScroll);
      el && el.removeEventListener('touchmove', this._onUserScroll);
    } catch { /* ignore */ }
    if (this._audio) {
      this._audio.pause();
      this._audio = null;
    }
    this.listeners.clear();
  }
}

let _silentUrl = null;
function silentWavUrl() {
  if (_silentUrl) return _silentUrl;
  const n = 8000;
  const buf = new ArrayBuffer(44 + n);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); v.setUint32(4, 36 + n, true); w(8, 'WAVE'); w(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true);
  w(36, 'data'); v.setUint32(40, n, true);
  for (let i = 0; i < n; i++) v.setUint8(44 + i, 128);
  _silentUrl = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  return _silentUrl;
}

// ---------------- 播放条 UI ----------------
export function createTtsBar(ctx) {
  const book = ctx.book;
  const btn = (label, title, fn, cls = 'btn btn-sm') => h('button', { class: cls, title, onclick: fn }, label);
  const btnA = (action, label, title, fn, cls = 'btn btn-sm') => h('button', { 'data-action': action, class: cls, title, onclick: fn }, label);
  const resumeBtn = btn('', '从上次停下的位置继续', () => book.playWhole());
  const playPause = btn('⏯', '播放 / 暂停 (K)', () => book.toggle(), 'icon-btn');
  const progress = h('input', { type: 'range', min: '0', max: '1000', step: '1', value: '0', title: '拖动跳转', style: { width: '140px' } });
  const progLabel = h('span', { class: 'faint', style: { minWidth: '120px' } }, '未开始');
  const secSel = h('select', { title: '章节跳转' }, h('option', { value: '' }, '目录…'));
  const rate = h('input', { type: 'range', min: '0.6', max: '1.6', step: '0.1', value: String(tts.rate), title: '语速' });
  const rateLabel = h('span', { class: 'faint' }, `${tts.rate.toFixed(1)}×`);
  const enSel = h('select', { title: '英文语音' });
  const zhSel = h('select', { title: '中文语音' });
  const bil = h('input', { type: 'checkbox' });
  bil.checked = book.opts.bilingual;
  const skip = h('input', { type: 'checkbox' });
  skip.checked = book.opts.skipRefs;
  const now = h('div', { class: 'tts-now' }, tts.supported ? '选择「听整篇」或「从当前页」开始；快捷键 K 播放/暂停，J/L 上一句/下一句' : '这个浏览器不支持朗读（speechSynthesis）');

  rate.oninput = () => {
    tts.setRate(parseFloat(rate.value));
    rateLabel.textContent = `${tts.rate.toFixed(1)}×`;
  };
  bil.onchange = () => book.setOpt('bilingual', bil.checked);
  skip.onchange = () => book.setOpt('skipRefs', skip.checked);
  let dragging = false;
  progress.addEventListener('input', () => {
    dragging = true;
    const n = book.numPages || 1;
    progLabel.textContent = `跳到第 ${Math.min(n, Math.floor((progress.value / 1000) * n) + 1)}/${n} 页`;
  });
  progress.addEventListener('change', () => {
    dragging = false;
    book.seek(progress.value / 1000);
  });
  secSel.addEventListener('focus', async () => {
    if (secSel.options.length > 1) return;
    secSel.options[0].textContent = '正在读取目录…';
    const secs = await book.getSections();
    secSel.options[0].textContent = secs.length ? '目录…' : '（没有检测到章节）';
    secs.forEach((s, i) => secSel.append(h('option', { value: String(i) }, `${s.depth ? '　' : ''}${s.title.slice(0, 50)} · p.${s.page}`)));
  });
  secSel.onchange = async () => {
    if (secSel.value === '') return;
    const secs = await book.getSections();
    book.playSection(secs[+secSel.value]);
    secSel.value = '';
    secSel.blur();
  };

  const fillVoices = () => {
    const fill = (sel, list, picked) => {
      sel.replaceChildren();
      if (!list.length) sel.append(h('option', { value: '' }, '（无可用语音）'));
      for (const v of list) sel.append(h('option', { value: v.name }, `${v.name}${/premium|enhanced/i.test(v.name) ? ' ★' : ''} (${v.lang})`));
      if (picked) sel.value = picked.name;
    };
    fill(enSel, tts.enVoices(), tts.pickEn());
    fill(zhSel, tts.zhVoices(), tts.pickZh());
  };
  enSel.onchange = () => tts.setVoice('en', enSel.value);
  zhSel.onchange = () => tts.setVoice('zh', zhSel.value);
  fillVoices();
  const offVoices = tts.on((ev) => { if (ev.type === 'voices') fillVoices(); });

  const update = (ev = {}) => {
    const n = book.numPages;
    const p = book.pos;
    if (p && p.page) {
      resumeBtn.style.display = '';
      resumeBtn.textContent = `▶ 从上次位置（p.${p.page}）`;
    } else resumeBtn.style.display = 'none';
    if (book.playing) {
      playPause.textContent = book.paused ? '▶' : '⏸';
      const tot = book.current ? book.current.total : 0;
      if (p) progLabel.textContent = `第 ${p.page}/${n} 页 · 第 ${p.s + 1}${tot ? '/' + tot : ''} 句`;
      else progLabel.textContent = '准备中…';
    } else {
      playPause.textContent = '▶';
      if (p && n) progLabel.textContent = `停在第 ${p.page}/${n} 页 · 第 ${p.s + 1} 句`;
    }
    if (!dragging) progress.value = String(Math.round(book.progress() * 1000));
    if (ev.sentence) now.textContent = ev.translation ? `${ev.sentence}　→　${ev.translation}` : ev.sentence;
    if (ev.ended) now.textContent = '读完了';
  };
  const offBook = book.on(update);

  const el = h('div', { class: 'tts-bar' },
    btnA('tts-full', '▶ 听整篇', '从第 1 页一直读到最后（默认跳过参考文献）', () => book.playFrom(1, 0)),
    resumeBtn,
    btn('从当前页', '从当前页开头开始读', () => book.playFromCurrentPage()),
    btn('读选区', '只读选中的文字', () => book.playSelection()),
    h('span', { class: 'tb-sep' }),
    btn('⏮', '上一句 (J)', () => book.prev(), 'icon-btn'),
    playPause,
    btn('⏭', '下一句 (L)', () => book.next(), 'icon-btn'),
    btn('⏹', '停止', () => book.stop(), 'icon-btn'),
    progress, progLabel,
    secSel,
    h('span', { class: 'tb-sep' }),
    h('span', { class: 'faint' }, '语速'), rate, rateLabel,
    enSel, zhSel,
    STATIC ? null : h('label', { class: 'check', title: '每句英文读完后，用中文语音读译文' }, bil, '双语'),
    h('label', { class: 'check', title: '读到 References / 参考文献 时停止' }, skip, '跳过参考文献'),
    now);
  update();
  return {
    el,
    destroy() { offBook(); offVoices(); },
  };
}
