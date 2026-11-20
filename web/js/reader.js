// 阅读页：顶栏 + PDF + 划词工具条 + 标注菜单 + 右侧面板；把各模块串起来
import { get, post, patch, del, put, loadSettings, loadHealth, readerUrl, cache, STATIC } from './api.js';
import { h, icon, toast, toastError, copyText, debounce, closePopMenus, truncate, ENGINE_LABEL } from './ui.js';
import { PdfView } from './pdfview.js';
import { setMarkdown } from './markdown.js';
import { createEditor } from './editor.js';
import { tts, speakButton } from './tts.js';
import { createPanels } from './panels.js';
import { Audiobook, createTtsBar } from './audiobook.js';
import { VoiceController } from './voice.js';
import { navigate } from '../app.js';

const COLORS = ['yellow', 'green', 'blue', 'pink', 'purple'];
const KIND_LABEL = { highlight: '高亮', underline: '下划线', strike: '删除线', squiggly: '波浪线', vocab: '生词' };

export async function mountReader(root, { pid, iid, page: pageParam, anno: annoParam }) {
  const P = encodeURIComponent(pid);
  const I = encodeURIComponent(iid);
  const base = `/papers/${P}/${I}`;
  const settings = await loadSettings();
  const health = await loadHealth().catch(() => null);

  let project;
  let paper;
  try {
    [project, paper] = await Promise.all([get(`/projects/${P}`), get(base)]);
  } catch (e) {
    root.append(h('div', { class: 'page' }, h('div', { class: 'empty' }, e.status === 404 ? '找不到这篇论文（可能被移动或删除了）' : `加载失败：${e.message}`),
      h('p', {}, h('a', { href: `/p/${P}`, 'data-link': '' }, '← 返回项目'))));
    return {};
  }
  const item = (project.items || []).find((x) => x.id === iid) || {};
  paper.annotations = paper.annotations || [];
  paper.notes = paper.notes || [];
  paper.qa = paper.qa || [];
  paper.translations = paper.translations || [];
  paper.vocab = paper.vocab || [];
  const meta = paper.meta || {};
  const pdfUrl = meta.pdf_url || item.pdf_url || meta.url || item.url;
  document.title = `${meta.title || item.title || '论文'} · PaperDesk`;

  // ---------------- 上下文 ----------------
  const ctx = {
    pid, iid, P, I, base, settings, health, project, item, paper,
    engine: localStorage.getItem('pd.engine') || settings.default_engine || 'claude',
    tengine: localStorage.getItem('pd.tengine') || settings.translate_engine || 'google',
    lastColor: localStorage.getItem('pd.color') || 'yellow',
    pdf: null,
    book: null,
    voice: null,
    panels: null,
  };

  // ---------------- 布局 ----------------
  const engineSel = h('select', { title: '提问用的 AI' },
    ['claude', 'codex'].map((e) => {
      const found = !health || (health[e] && health[e].found);
      return h('option', { value: e }, `${ENGINE_LABEL[e]}${found ? '' : '（未检测到）'}`);
    }));
  engineSel.value = ctx.engine;
  engineSel.onchange = () => { ctx.engine = engineSel.value; localStorage.setItem('pd.engine', ctx.engine); };
  const tengineSel = h('select', { title: '翻译引擎' },
    h('option', { value: 'google' }, '译：Google'), h('option', { value: 'claude' }, '译：Claude'), h('option', { value: 'codex' }, '译：Codex'));
  tengineSel.value = ctx.tengine;
  tengineSel.onchange = () => { ctx.tengine = tengineSel.value; localStorage.setItem('pd.tengine', ctx.tengine); };

  const zoomLabel = h('span', { class: 'rd-zoom', title: '点击适合宽度' }, '100%');
  const pageInput = h('input', { type: 'text', value: '1' });
  const pageTotal = h('span', {}, '/ –');
  const ttsToggle = h('button', { 'data-action': 'tts', class: 'icon-btn', title: '朗读 / 听论文' }, h('span', { style: { fontSize: '14px' } }, '🔊'));
  const archiveBtn = h('button', { 'data-action': 'archive', class: 'btn btn-primary btn-sm', title: '让 AI 把笔记 + 问答 + 全文整理成总结，写入笔记文件并同步' }, icon('archive', 14), '读完，归档笔记');

  const top = h('div', { class: 'rd-top' },
    h('button', { class: 'icon-btn', title: `返回「${project.name}」`, onclick: () => navigate(`/p/${P}`) }, icon('back', 18)),
    h('div', { class: 'rd-title', title: meta.title || item.title || '' }, meta.title || item.title || pdfUrl),
    STATIC ? null : h('div', { class: 'rd-group' }, engineSel, tengineSel),
    h('div', { class: 'rd-group' },
      h('button', { class: 'icon-btn', title: '缩小', onclick: () => ctx.pdf && ctx.pdf.setScale(ctx.pdf.scale / 1.15) }, icon('zoomOut', 16)),
      zoomLabel,
      h('button', { class: 'icon-btn', title: '放大', onclick: () => ctx.pdf && ctx.pdf.setScale(ctx.pdf.scale * 1.15) }, icon('zoomIn', 16))),
    h('div', { class: 'rd-group rd-pagebox' }, pageInput, pageTotal),
    h('div', { class: 'rd-group' },
      ttsToggle,
      h('button', { class: 'icon-btn', title: '复制本篇阅读链接', onclick: () => copyText(readerUrl(pid, iid)) }, icon('link', 16)),
      meta.url || item.url ? h('a', { class: 'icon-btn', title: '打开原文页面', href: meta.url || item.url, target: '_blank', rel: 'noopener' }, icon('external', 16)) : null),
    STATIC ? null : archiveBtn,
  );

  const ttsSlot = h('div');
  const pdfWrap = h('div', { class: 'rd-pdf' });
  const loading = h('div', { class: 'rd-loading' }, h('div', { style: { textAlign: 'center' } }, h('div', { class: 'ld-text' }, '正在加载 PDF（只在内存中，不落盘）…'), h('div', { class: 'bar' }, h('i'))));
  pdfWrap.append(loading);
  const resizer = h('div', { class: 'rd-resizer', title: '拖动调整宽度' });
  const panelEl = h('div', { class: 'rd-panel' });
  const savedW = +localStorage.getItem('pd.panelW');
  if (savedW) panelEl.style.width = `${savedW}px`;
  const reader = h('div', { class: 'reader' }, top, ttsSlot, h('div', { class: 'rd-body' }, pdfWrap, resizer, panelEl));
  root.append(reader);

  // 拖动面板宽度
  resizer.addEventListener('mousedown', (e) => {
    e.preventDefault();
    resizer.classList.add('drag');
    const move = (ev) => {
      const w = Math.min(window.innerWidth * 0.7, Math.max(300, window.innerWidth - ev.clientX));
      panelEl.style.width = `${w}px`;
    };
    const up = () => {
      resizer.classList.remove('drag');
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      localStorage.setItem('pd.panelW', String(parseInt(panelEl.style.width, 10)));
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });

  // ---------------- 数据操作（给面板 / 语音用） ----------------
  ctx.savePaper = debounce((fields) => { if (!STATIC) patch(base, fields).catch(() => {}); }, 1200);

  ctx.createAnnotation = async ({ kind, color, page, quote, rects, comment }) => {
    const a = await post(`${base}/annotations`, { kind, color, page, quote, rects, comment });
    paper.annotations.push(a);
    ctx.pdf && ctx.pdf.upsertAnnotation(a);
    ctx.panels && ctx.panels.refresh('notes');
    return a;
  };
  ctx.updateAnnotation = async (id, fields) => {
    const a = await patch(`${base}/annotations/${encodeURIComponent(id)}`, fields);
    const i = paper.annotations.findIndex((x) => x.id === id);
    if (i >= 0) paper.annotations[i] = a;
    ctx.pdf && ctx.pdf.upsertAnnotation(a);
    ctx.panels && ctx.panels.refresh('notes');
    return a;
  };
  ctx.deleteAnnotation = async (id) => {
    await del(`${base}/annotations/${encodeURIComponent(id)}`);
    paper.annotations = paper.annotations.filter((x) => x.id !== id);
    ctx.paper.annotations = paper.annotations;
    ctx.pdf && ctx.pdf.removeAnnotation(id);
    ctx.panels && ctx.panels.refresh('notes');
  };
  ctx.addNote = async ({ text, page, quote, source = 'manual', annotation_id }) => {
    const n = await post(`${base}/notes`, { text, page, quote, source, annotation_id });
    paper.notes.push(n);
    ctx.pdf && ctx.pdf.setMarkerResolver(markerResolver);
    ctx.panels && ctx.panels.refresh('notes');
    return n;
  };
  ctx.annotationById = (id) => paper.annotations.find((a) => a.id === id);
  ctx.currentPage = () => (ctx.pdf ? ctx.pdf.current : 1);
  ctx.gotoPage = (n) => ctx.pdf && ctx.pdf.scrollToPage(Math.max(1, Math.min(ctx.pdf.numPages, n)));
  ctx.onPaperChanged = () => ctx.pdf && ctx.pdf.setMarkerResolver(markerResolver);

  const markerResolver = (a) => paper.notes.some((n) => n.annotation_id === a.id);

  // ---------------- 面板 ----------------
  ctx.panels = createPanels(ctx, panelEl);

  // ---------------- 朗读 / 语音 ----------------
  ctx.book = new Audiobook(ctx);
  const ttsBar = createTtsBar(ctx);
  ttsSlot.append(ttsBar.el);
  ttsBar.el.classList.add('hidden');
  ttsToggle.onclick = () => {
    ttsBar.el.classList.toggle('hidden');
    ttsToggle.classList.toggle('active', !ttsBar.el.classList.contains('hidden'));
  };
  ctx.showTtsBar = () => {
    ttsBar.el.classList.remove('hidden');
    ttsToggle.classList.add('active');
  };
  ctx.voice = STATIC ? null : new VoiceController(ctx, pdfWrap);

  archiveBtn.onclick = () => ctx.panels.archive();

  // ---------------- PDF ----------------
  const lsKey = `pd.view.${pid}.${iid}`;
  const savePage = debounce((n) => {
    if (STATIC) return localStorage.setItem(`${lsKey}.page`, String(n));
    patch(base, { last_page: n }).catch(() => {});
  }, 1500);
  const saveZoom = debounce((z) => {
    if (STATIC) return localStorage.setItem(`${lsKey}.zoom`, String(z));
    patch(base, { zoom: Math.round(z * 1000) / 1000 }).catch(() => {});
  }, 1500);
  if (STATIC) {
    paper.last_page = +localStorage.getItem(`${lsKey}.page`) || paper.last_page;
    paper.zoom = +localStorage.getItem(`${lsKey}.zoom`) || paper.zoom;
  }
  const pdf = new PdfView(pdfWrap, {
    onProgress: (loaded, total) => {
      const bar = loading.querySelector('.bar i');
      if (total) bar.style.width = `${Math.round((loaded / total) * 100)}%`;
      loading.querySelector('.ld-text').textContent = `正在加载 PDF（只在内存中，不落盘）… ${(loaded / 1048576).toFixed(1)} MB`;
    },
    onPageChange: (n) => {
      pageInput.value = String(n);
      savePage(n);
    },
    onScaleChange: (s) => {
      zoomLabel.textContent = `${Math.round(s * 100)}%`;
      saveZoom(s);
    },
    onSelection: (info) => showSelToolbar(info),
    onAnnotationClick: (a, x, y) => showAnnoMenu(a, x, y),
    onBlankClick: () => hideFloating(),
  });
  ctx.pdf = pdf;
  zoomLabel.onclick = () => pdf.setScale(pdf.fitWidthScale());
  pageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const n = parseInt(pageInput.value, 10);
      if (n) ctx.gotoPage(n);
      pageInput.blur();
    }
  });

  (async () => {
    try {
      if (!pdfUrl) throw new Error('这条论文没有 PDF 地址');
      // 本地：经后端代理（内存流式，不落盘）；静态只读版：pdf.js 直连源站（需源站允许跨域）
      await pdf.load(STATIC ? pdfUrl : `/api/pdf?url=${encodeURIComponent(pdfUrl)}`, {
        scale: paper.zoom || null,
        page: pageParam || paper.last_page || 1,
      });
      loading.remove();
      pageTotal.textContent = `/ ${pdf.numPages}`;
      zoomLabel.textContent = `${Math.round(pdf.scale * 100)}%`;
      pdf.setAnnotations(paper.annotations);
      pdf.setMarkerResolver(markerResolver);
      if (annoParam) {
        const a = ctx.annotationById(annoParam);
        if (a) setTimeout(() => pdf.goToAnnotation(a), 400);
      }
      ctx.book.onDocReady();
      if (!paper.text_cached && !STATIC) uploadText();
    } catch (e) {
      console.error(e);
      if (STATIC && pdfUrl) return iframeFallback();
      loading.replaceChildren(h('div', { style: { textAlign: 'center', maxWidth: '420px' } },
        h('div', { class: 'err-text', style: { marginBottom: '8px' } }, 'PDF 加载失败'),
        h('div', { class: 'muted', style: { fontSize: '12.5px' } }, e && e.message ? e.message : String(e)),
        h('div', { style: { marginTop: '12px' } }, h('a', { class: 'btn btn-sm', href: pdfUrl, target: '_blank' }, '在新标签页打开原 PDF'))));
    }
  })();

  // 静态只读版：pdf.js 跨域失败 → 浏览器自带 PDF 查看器（iframe #page=N）→ 再失败只显示右侧内容
  function iframeFallback() {
    try { pdf.destroy(); } catch { /* ignore */ }
    ttsToggle.style.display = 'none';
    pageInput.disabled = true;
    pageTotal.textContent = '';
    zoomLabel.parentElement.style.display = 'none';
    const openBtn = h('a', { class: 'btn btn-sm', href: pdfUrl, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开 PDF');
    const wrap = h('div', { class: 'iframe-wrap' },
      h('div', { class: 'iframe-hint' }, h('span', {}, '此 PDF 源站不允许跨域，标注无法叠加显示，已按页码定位'), h('span', { class: 'spacer' }), openBtn));
    let cur = pageParam || paper.last_page || 1;
    let frame = null;
    let everLoaded = false;
    const setPage = (n) => {
      cur = Math.max(1, n || 1);
      pageInput.value = String(cur);
      if (frame) frame.remove();
      frame = h('iframe', { src: `${pdfUrl}#page=${cur}`, title: 'PDF', referrerpolicy: 'no-referrer' });
      frame.addEventListener('load', () => { everLoaded = true; });
      wrap.append(frame);
      savePage(cur);
    };
    pdfWrap.replaceChildren(wrap);
    setPage(cur);
    setTimeout(() => { if (!everLoaded && !stub.destroyed) noViewer(); }, 20000);
    const stub = {
      destroyed: false, numPages: 0, scale: 1, doc: null, scrollEl: h('div'), annotations: [],
      get current() { return cur; },
      scrollToPage: (n) => setPage(n),
      goToAnnotation: (a) => a && setPage(a.page),
      flashAnnotation() {}, flashRects() {}, setAnnotations() {}, upsertAnnotation() {}, removeAnnotation() {}, setMarkerResolver() {},
      clearSelection() {}, getSelectionInfo: () => null, getPageText: async () => '', getPageParagraphs: async () => [],
      getAllText: async () => [], ensureText: async () => null, fitWidthScale: () => 1, setScale() {}, hitTest: () => null,
      destroy() { this.destroyed = true; wrap.remove(); },
    };
    ctx.pdf = stub;
    function noViewer() {
      pdfWrap.replaceChildren(h('div', { class: 'pdf-fallback' }, h('div', {},
        h('div', { style: { marginBottom: '10px' } }, '无法在页面中显示这个 PDF，右侧的笔记、标注和问答仍可查看。'),
        h('a', { class: 'btn btn-primary', href: meta.url || item.url || pdfUrl, target: '_blank', rel: 'noopener' }, icon('external', 14), meta.arxiv_id || item.arxiv_id ? '在 arXiv 打开' : '打开原文'))));
    }
  }

  async function uploadText() {
    try {
      const pages = await pdf.getAllText();
      if (!pdf.destroyed) {
        await put(`${base}/text`, { pages });
        paper.text_cached = true;
      }
    } catch (e) {
      console.warn('上传全文失败', e);
    }
  }

  if (!STATIC) post('/current', { pid, iid }).catch(() => {});
  if (!STATIC && ((item.status || paper.status) === 'unread' || !paper.status)) {
    patch(base, { status: 'reading' }).then(() => document.dispatchEvent(new CustomEvent('pd:projects-changed'))).catch(() => {});
  }

  // ---------------- 划词工具条 ----------------
  let floating = null;
  function hideFloating() {
    if (floating) {
      floating.remove();
      floating = null;
    }
  }

  function placeFloating(el, rect, prefer = 'above') {
    document.body.append(el);
    const w = el.offsetWidth;
    const hh = el.offsetHeight;
    let left = rect.left + rect.width / 2 - w / 2;
    left = Math.max(8, Math.min(window.innerWidth - w - 8, left));
    let topY = prefer === 'above' ? rect.top - hh - 8 : rect.bottom + 8;
    if (topY < 56) topY = rect.bottom + 8;
    if (topY + hh > window.innerHeight - 8) topY = Math.max(56, rect.top - hh - 8);
    el.style.left = `${left}px`;
    el.style.top = `${topY}px`;
  }

  function showSelToolbar(info) {
    hideFloating();
    closePopMenus();
    const act = (fn) => (e) => {
      e.preventDefault();
      e.stopPropagation();
      hideFloating();
      fn();
      pdf.clearSelection();
    };
    const annotate = (kind, color) => act(async () => {
      try {
        ctx.lastColor = color;
        localStorage.setItem('pd.color', color);
        await ctx.createAnnotation({ kind, color, page: info.page, quote: info.quote, rects: info.rects });
        pdf.clearSelection();
      } catch (err) { toastError(err); }
    });
    if (STATIC) {
      floating = h('div', { class: 'sel-toolbar', onmousedown: (e) => e.preventDefault() },
        h('button', { title: '朗读选中内容', onclick: act(() => tts.speak(info.quote)) }, '🔊 朗读'),
        h('button', { title: '复制', onclick: act(() => copyText(info.quote)) }, icon('copy', 14), '复制'));
      placeFloating(floating, info.clientRect, 'above');
      return;
    }
    const tb = h('div', { class: 'sel-toolbar', onmousedown: (e) => e.preventDefault() },
      h('button', { 'data-action': 'translate', title: '翻译选中内容', onclick: act(() => ctx.panels.translateText(info.quote, info.page)) }, icon('language', 14), '翻译'),
      h('button', { 'data-action': 'explain', title: '让 AI 解释这段', onclick: act(() => ctx.panels.ask({ question: '请解释这段内容：它在说什么、为什么这样做、和上下文的关系。', quote: info.quote, page: info.page, mode: 'explain' })) }, icon('bulb', 14), '解释'),
      h('button', { 'data-action': 'ask-quote', title: '带着这段去问 AI', onclick: act(() => ctx.panels.setQuote(info.quote, info.page)) }, icon('message', 14), '问 Agent'),
      h('button', { 'data-action': 'note', title: '针对这段写笔记（会同时高亮）', onclick: act(() => ctx.panels.newNote({ quote: info.quote, page: info.page, rects: info.rects })) }, icon('pencil', 14), '记一笔'),
      h('span', { class: 'tb-sep' }),
      COLORS.map((c) => h('button', { 'data-action': 'highlight', 'data-color': c, class: `color-dot c-${c} ${c === ctx.lastColor ? 'sel' : ''}`, title: `${c} 高亮`, onclick: annotate('highlight', c) })),
      h('span', { class: 'tb-sep' }),
      h('button', { 'data-action': 'underline', class: 'tb-kind', title: '下划线', onclick: annotate('underline', ctx.lastColor) }, h('u', {}, 'U')),
      h('button', { 'data-action': 'strike', class: 'tb-kind', title: '删除线', onclick: annotate('strike', ctx.lastColor) }, h('s', {}, 'S')),
      h('button', { 'data-action': 'squiggly', class: 'tb-kind', title: '波浪线', onclick: annotate('squiggly', ctx.lastColor) }, h('span', { style: { textDecoration: 'underline wavy', textUnderlineOffset: '3px' } }, 'W')),
      h('span', { class: 'tb-sep' }),
      h('button', { title: '朗读选中内容', onclick: act(() => tts.speak(info.quote)) }, '🔊'),
      h('button', { 'data-action': 'vocab', title: '加入生词本（单词或 ≤6 个词的短语）', onclick: act(() => ctx.panels.addVocabFromSelection(info)) }, icon('book', 14), '生词'),
    );
    floating = tb;
    placeFloating(tb, info.clientRect, 'above');
  }

  // ---------------- 标注菜单 ----------------
  function showAnnoMenu(a, x, y) {
    hideFloating();
    closePopMenus();
    const menu = h('div', { class: 'anno-menu', onmousedown: (e) => e.stopPropagation() });
    const render = () => {
      menu.replaceChildren();
      if (a.kind === 'vocab') {
        const v = paper.vocab.find((x2) => x2.annotation_id === a.id);
        menu.append(h('div', { class: 'v-head' },
          h('span', { class: 'v-word' }, v ? v.word : a.quote),
          v && v.phonetic ? h('span', { class: 'v-phon' }, v.phonetic) : null,
          speakButton(() => (v ? v.word : a.quote), 'en')));
        if (v && v.meaning) menu.append(h('div', { class: 'v-mean' }, (v.pos ? `${v.pos} ` : '') + v.meaning));
        if (v && v.context_meaning) menu.append(h('div', { class: 'v-cmean' }, `语境：${v.context_meaning}`));
        menu.append(h('div', { class: 'am-row' },
          h('button', { class: 'tb', onclick: () => { hideFloating(); ctx.panels.switchTab('vocab'); ctx.panels.focusVocab(v && v.id); } }, icon('book', 14), '在生词里查看'),
          h('span', { class: 'spacer' }),
          STATIC ? null : h('button', { class: 'tb', style: { color: 'var(--danger)' }, onclick: async () => {
            hideFloating();
            try {
              if (v) await ctx.panels.deleteVocab(v);
              else await ctx.deleteAnnotation(a.id);
            } catch (err) { toastError(err); }
          } }, icon('trash', 14), '删除')));
        return;
      }
      menu.append(h('div', { class: 'am-quote' }, truncate(a.quote, 220)));
      if (!STATIC) menu.append(h('div', { class: 'am-row' },
        COLORS.map((c) => h('button', { class: `color-dot tb c-${c} ${c === a.color ? 'sel' : ''}`, title: c, onclick: async () => {
          try { a = await ctx.updateAnnotation(a.id, { color: c }); render(); } catch (err) { toastError(err); }
        } })),
        h('span', { class: 'tb-sep' }),
        ['highlight', 'underline', 'strike', 'squiggly'].map((k) => h('button', {
          class: `tb tb-kind ${a.kind === k ? 'on' : ''}`,
          title: KIND_LABEL[k],
          onclick: async () => { try { a = await ctx.updateAnnotation(a.id, { kind: k }); render(); } catch (err) { toastError(err); } },
        }, k === 'highlight' ? h('span', { style: { background: '#ffe066', padding: '0 3px', color: '#222' } }, 'H') : k === 'underline' ? h('u', {}, 'U') : k === 'strike' ? h('s', {}, 'S') : h('span', { style: { textDecoration: 'underline wavy' } }, 'W')))));
      if (a.comment && a.comment.trim()) {
        const c = h('div', { class: 'am-comment' });
        setMarkdown(c, a.comment);
        menu.append(c);
      }
      const linked = paper.notes.filter((n) => n.annotation_id === a.id);
      if (linked.length) {
        menu.append(h('button', { class: 'tb', onclick: () => { hideFloating(); ctx.panels.switchTab('notes'); ctx.panels.focusNote(linked[0].id); } }, icon('note', 14), `查看关联笔记（${linked.length}）`));
      }
      if (STATIC) {
        menu.append(h('div', { class: 'am-row' }, h('span', { class: 'faint', style: { fontSize: '12px' } }, `p.${a.page} · ${KIND_LABEL[a.kind] || ''}`), h('span', { class: 'spacer' }),
          speakButton(() => a.quote), h('button', { class: 'tb', title: '复制原文', onclick: () => copyText(a.quote) }, icon('copy', 14))));
        return;
      }
      menu.append(h('div', { class: 'am-row' },
        h('button', { class: 'tb', onclick: () => editComment() }, icon('chat', 14), a.comment ? '改批注' : '批注'),
        h('button', { class: 'tb', onclick: () => { hideFloating(); ctx.panels.newNote({ quote: a.quote, page: a.page, annotation_id: a.id }); } }, icon('pencil', 14), '记一笔'),
        h('button', { class: 'tb', onclick: () => { hideFloating(); ctx.panels.setQuote(a.quote, a.page); } }, icon('message', 14), '问'),
        h('button', { class: 'tb', onclick: () => { hideFloating(); ctx.panels.translateText(a.quote, a.page); } }, icon('language', 14), '译'),
        speakButton(() => a.quote),
        h('button', { class: 'tb', title: '复制原文', onclick: () => copyText(a.quote) }, icon('copy', 14)),
        h('span', { class: 'spacer' }),
        h('button', { class: 'tb', title: '删除标注', style: { color: 'var(--danger)' }, onclick: async () => {
          hideFloating();
          try { await ctx.deleteAnnotation(a.id); } catch (err) { toastError(err); }
        } }, icon('trash', 14))));
    };
    const editComment = () => {
      const ed = createEditor({
        value: a.comment || '',
        rows: 3,
        placeholder: '写批注（Markdown）',
        onCancel: () => render(),
        onSave: async (text) => {
          a = await ctx.updateAnnotation(a.id, { comment: text });
          render();
        },
      });
      menu.replaceChildren(h('div', { class: 'am-quote' }, truncate(a.quote, 140)), ed.el);
      ed.focus();
    };
    render();
    floating = menu;
    placeFloating(menu, { left: x - 10, top: y, width: 20, height: 4, bottom: y + 4 }, 'below');
  }

  // 点击空白 / 滚动 / Esc 关闭浮层
  const onDocDown = (e) => {
    if (floating && !floating.contains(e.target)) {
      if (!e.target.closest('.textLayer')) hideFloating();
    }
  };
  const onScroll = () => { if (floating && floating.classList.contains('sel-toolbar')) hideFloating(); };
  const onSelChange = () => {
    if (floating && floating.classList.contains('sel-toolbar')) {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hideFloating();
    }
  };
  const onKey = (e) => {
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
    if (e.key === 'Escape') { hideFloating(); closePopMenus(); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === 'k') { e.preventDefault(); ctx.showTtsBar(); ctx.book.toggle(); }
    else if (k === 'j') { e.preventDefault(); ctx.book.prev(); }
    else if (k === 'l') { e.preventDefault(); ctx.book.next(); }
  };
  document.addEventListener('mousedown', onDocDown);
  pdf.scrollEl.addEventListener('scroll', onScroll, { passive: true });
  document.addEventListener('selectionchange', onSelChange);
  document.addEventListener('keydown', onKey);

  const flush = () => {
    try { savePage.flush(pdf.current); } catch { /* ignore */ }
  };
  window.addEventListener('beforeunload', flush);

  return {
    destroy() {
      flush();
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('mousedown', onDocDown);
      document.removeEventListener('selectionchange', onSelChange);
      document.removeEventListener('keydown', onKey);
      hideFloating();
      closePopMenus();
      try { ctx.book.destroy(); } catch { /* ignore */ }
      try { ctx.voice.destroy(); } catch { /* ignore */ }
      try { ctx.panels.destroy && ctx.panels.destroy(); } catch { /* ignore */ }
      pdf.destroy();
      document.title = 'PaperDesk';
    },
  };
}
