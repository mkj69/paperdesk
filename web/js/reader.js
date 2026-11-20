// 阅读页：顶栏 + 资源条 + 阅读器（PDF / 文章 / README / 视频）+ 划词工具条 + 标注菜单 + 右侧面板
// 一篇工作可以有多个资源（SPEC §13）：资源条切换阅读器；ctx.pdf 是指向「当前阅读器」的代理，面板/朗读/语音无需关心类型。
import { get, post, patch, del, put, loadSettings, loadHealth, readerUrl, STATIC } from './api.js';
import { h, icon, toast, toastError, copyText, debounce, closePopMenus, truncate, ENGINE_LABEL, popMenu, promptDialog, confirmDialog } from './ui.js';
import { PdfView } from './pdfview.js';
import { ArticleView, VideoView, IframeView, NoticeView, loadingBox, fetchReadmeDirect } from './articleview.js';
import { OriginalView, sanitizePage } from './originalview.js';
import { itemResources, viewerFor, pdfUrlOf, videoEmbed, resLabel, kindMeta, KIND_OPTIONS, hostOf, isGithubRepo } from './resources.js';
import { setMarkdown } from './markdown.js';
import { createEditor } from './editor.js';
import { tts, speakButton } from './tts.js';
import { createPanels } from './panels.js';
import { Audiobook, createTtsBar } from './audiobook.js';
import { VoiceController } from './voice.js';
import { navigate } from '../app.js';

const COLORS = ['yellow', 'green', 'blue', 'pink', 'purple'];
const KIND_LABEL = { highlight: '高亮', underline: '下划线', strike: '删除线', squiggly: '波浪线', vocab: '生词' };

export async function mountReader(root, { pid, iid, page: pageParam, anno: annoParam, res: resParam }) {
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
  document.title = `${meta.title || item.title || '论文'} · PaperDesk`;

  // ---------------- 资源 ----------------
  let resources = itemResources(item, meta);
  let initialRid = resParam && resources.some((r) => r.id === resParam) ? resParam : 'main';
  if (annoParam) {
    const a0 = paper.annotations.find((x) => x.id === annoParam);
    if (a0 && a0.resource_id && resources.some((r) => r.id === a0.resource_id)) initialRid = a0.resource_id;
  }

  // ---------------- 上下文 ----------------
  const ctx = {
    pid, iid, P, I, base, settings, health, project, item, paper,
    engine: localStorage.getItem('pd.engine') || settings.default_engine || 'claude',
    tengine: localStorage.getItem('pd.tengine') || settings.translate_engine || 'google',
    lastColor: localStorage.getItem('pd.color') || 'yellow',
    resource: initialRid,
    get resources() { return resources; },
    pdf: null,
    book: null,
    voice: null,
    panels: null,
  };
  ctx.resourceById = (rid) => resources.find((r) => r.id === (rid || 'main')) || null;

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

  const zoomLabel = h('span', { class: 'rd-zoom', title: '点击适合宽度 / 恢复默认字号' }, '100%');
  const pageInput = h('input', { type: 'text', value: '1' });
  const pageTotal = h('span', {}, '/ –');
  const ttsToggle = h('button', { 'data-action': 'tts', class: 'icon-btn', title: '朗读 / 听论文' }, h('span', { style: { fontSize: '14px' } }, '🔊'));
  const archiveBtn = h('button', { 'data-action': 'archive', class: 'btn btn-primary btn-sm', title: '让 AI 把笔记 + 问答 + 全文整理成总结，写入笔记文件并同步' }, icon('archive', 14), '读完，归档笔记');
  const extLink = h('a', { class: 'icon-btn', title: '打开原文页面', target: '_blank', rel: 'noopener' }, icon('external', 16));
  const zoomGroup = h('div', { class: 'rd-group' },
    h('button', { class: 'icon-btn', title: '缩小', onclick: () => ctx.pdf.setScale(ctx.pdf.scale / 1.15) }, icon('zoomOut', 16)),
    zoomLabel,
    h('button', { class: 'icon-btn', title: '放大', onclick: () => ctx.pdf.setScale(ctx.pdf.scale * 1.15) }, icon('zoomIn', 16)));
  const pageBox = h('div', { class: 'rd-group rd-pagebox' }, pageInput, pageTotal);

  const top = h('div', { class: 'rd-top' },
    h('button', { class: 'icon-btn', title: `返回「${project.name}」`, onclick: () => navigate(`/p/${P}`) }, icon('back', 18)),
    h('div', { class: 'rd-title', title: meta.title || item.title || '' }, meta.title || item.title || item.url || ''),
    STATIC ? null : h('div', { class: 'rd-group' }, engineSel, tengineSel),
    zoomGroup,
    pageBox,
    h('div', { class: 'rd-group' },
      ttsToggle,
      h('button', { class: 'icon-btn', title: '复制本篇阅读链接', onclick: () => copyText(readerUrl(pid, iid) + (ctx.resource !== 'main' ? `?res=${encodeURIComponent(ctx.resource)}` : '')) }, icon('link', 16)),
      extLink),
    STATIC ? null : archiveBtn,
  );

  const ttsSlot = h('div');
  const resBar = h('div', { class: 'res-bar' });
  const viewHost = h('div', { class: 'rd-view' });
  const pdfWrap = h('div', { class: 'rd-pdf' }, viewHost);
  const leftPane = h('div', { class: 'rd-left' }, resBar, pdfWrap);
  const resizer = h('div', { class: 'rd-resizer', title: '拖动调整宽度' });
  const panelEl = h('div', { class: 'rd-panel' });
  const savedW = +localStorage.getItem('pd.panelW');
  if (savedW) panelEl.style.width = `${savedW}px`;
  const reader = h('div', { class: 'reader' }, top, ttsSlot, h('div', { class: 'rd-body' }, leftPane, resizer, panelEl));
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

  // ---------------- 当前阅读器（代理） ----------------
  let view = null;
  const nullView = makeNullView();
  const goToAnnotationAny = async (a) => {
    if (!a) return;
    const rid = a.resource_id || 'main';
    if (rid !== ctx.resource && ctx.resourceById(rid)) await switchResource(rid);
    if (view) view.goToAnnotation(a);
  };
  ctx.pdf = new Proxy({}, {
    get(_, prop) {
      if (prop === 'goToAnnotation') return goToAnnotationAny;
      if (prop === 'view') return view;
      const v = view || nullView;
      const val = v[prop];
      return typeof val === 'function' ? val.bind(v) : val;
    },
    set(_, prop, val) {
      (view || nullView)[prop] = val;
      return true;
    },
  });
  ctx.isArticleView = () => !!(view && view.isArticle);
  ctx.storePage = (p) => (view && (view.isArticle || view.isMedia) ? null : (p || null));
  ctx.annotationsFor = (rid) => paper.annotations.filter((a) => (a.resource_id || 'main') === (rid || 'main'));
  ctx.gotoResource = (rid) => ((rid || 'main') === ctx.resource ? Promise.resolve(view) : switchResource(rid || 'main'));

  // ---------------- 数据操作（给面板 / 语音用） ----------------
  ctx.savePaper = debounce((fields) => { if (!STATIC) patch(base, fields).catch(() => {}); }, 1200);

  const inView = (a) => (a.resource_id || 'main') === ctx.resource;
  ctx.createAnnotation = async ({ kind, color, page, quote, rects, comment, anchor }) => {
    const body = { kind, color, page: ctx.storePage(page), quote, rects: rects || [], comment, resource_id: ctx.resource };
    if (anchor) body.anchor = anchor;
    const a = await post(`${base}/annotations`, body);
    if (!a.resource_id) a.resource_id = ctx.resource;
    if (anchor && !a.anchor) a.anchor = anchor;
    paper.annotations.push(a);
    if (view && inView(a)) view.upsertAnnotation(a);
    ctx.panels && ctx.panels.refresh('notes');
    return a;
  };
  ctx.updateAnnotation = async (id, fields) => {
    const a = await patch(`${base}/annotations/${encodeURIComponent(id)}`, fields);
    const i = paper.annotations.findIndex((x) => x.id === id);
    if (i >= 0) {
      if (!a.resource_id && paper.annotations[i].resource_id) a.resource_id = paper.annotations[i].resource_id;
      if (!a.anchor && paper.annotations[i].anchor) a.anchor = paper.annotations[i].anchor;
      paper.annotations[i] = a;
    }
    if (view && inView(a)) view.upsertAnnotation(a);
    ctx.panels && ctx.panels.refresh('notes');
    return a;
  };
  ctx.deleteAnnotation = async (id) => {
    await del(`${base}/annotations/${encodeURIComponent(id)}`);
    paper.annotations = paper.annotations.filter((x) => x.id !== id);
    ctx.paper.annotations = paper.annotations;
    if (view) view.removeAnnotation(id);
    ctx.panels && ctx.panels.refresh('notes');
  };
  ctx.addNote = async ({ text, page, quote, source = 'manual', annotation_id, anchor, resource_id }) => {
    const linked = annotation_id && ctx.annotationById(annotation_id);
    const rid = resource_id || (linked && linked.resource_id) || ctx.resource;
    const body = { text, page: linked ? (linked.page || null) : ctx.storePage(page), quote, source, annotation_id, resource_id: rid };
    const anc = anchor || (linked && linked.anchor);
    if (anc) body.anchor = anc;
    const n = await post(`${base}/notes`, body);
    if (!n.resource_id) n.resource_id = rid;
    if (anc && !n.anchor) n.anchor = anc;
    paper.notes.push(n);
    if (view) view.setMarkerResolver(markerResolver);
    ctx.panels && ctx.panels.refresh('notes');
    return n;
  };
  ctx.annotationById = (id) => paper.annotations.find((a) => a.id === id);
  ctx.currentPage = () => (view ? view.current || 1 : 1);
  ctx.gotoPage = (n) => {
    if (!view) return;
    if (view.isArticle) return view.scrollPage(n - (view.current || 1));
    if (view.numPages) view.scrollToPage(Math.max(1, Math.min(view.numPages, n)));
  };
  ctx.onPaperChanged = () => view && view.setMarkerResolver(markerResolver);

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

  // ---------------- 阅读位置记忆（只针对主 PDF） ----------------
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

  pageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const n = parseInt(pageInput.value, 10);
      if (n) ctx.gotoPage(n);
      pageInput.blur();
    }
  });
  zoomLabel.onclick = () => ctx.pdf.setScale(ctx.pdf.fitWidthScale());

  const commonOpts = () => ({
    onSelection: (info) => showSelToolbar(info),
    onAnnotationClick: (a, x, y) => showAnnoMenu(a, x, y),
    onBlankClick: () => hideFloating(),
    onScaleChange: (s) => { zoomLabel.textContent = `${Math.round(s * 100)}%`; },
  });

  // ---------------- 资源条 ----------------
  function renderResBar() {
    resBar.replaceChildren();
    const show = !STATIC || resources.length > 1;
    resBar.style.display = show ? '' : 'none';
    const tabs = h('div', { class: 'res-tabs' });
    resBar.append(tabs);
    for (const r of resources) {
      const km = kindMeta(r.kind);
      const active = r.id === ctx.resource;
      const btn = h('button', {
        class: `res-tab ${active ? 'active' : ''} ${r.auto ? 'auto' : ''}`,
        'data-res': r.id,
        title: `${resLabel(r, { long: true })}\n${r.url}${r.auto ? '\n（自动发现）' : ''}`,
        onclick: () => { if (r.id !== ctx.resource) switchResource(r.id); },
      }, h('span', { class: 'rt-ic' }, km.icon), h('span', { class: 'rt-label' }, resLabel(r)));
      if (active) {
        const more = h('span', { class: 'rt-more', role: 'button', title: '资源操作' }, icon('dots', 13));
        more.onclick = (e) => { e.stopPropagation(); resMenu(more, r); };
        btn.append(more);
      }
      tabs.append(btn);
    }
    if (!STATIC) {
      resBar.append(h('button', {
        class: 'res-add', 'data-action': 'add-resource',
        title: '把这篇工作的论文 / 代码 / 主页 / 博客 / 视频等链接关联进来，统一阅读和记笔记',
        onclick: () => addResource(),
      }, icon('plus', 13), '关联资源'));
    }
    const act = tabs.querySelector('.res-tab.active');
    if (act) requestAnimationFrame(() => act.scrollIntoView({ block: 'nearest', inline: 'nearest' }));
  }

  function resMenu(anchor, r) {
    const kind = viewerFor(r);
    popMenu(anchor, [
      { label: '在新标签页打开', icon: 'external', onClick: () => window.open(r.url, '_blank', 'noopener') },
      ...(kind === 'article' && canOriginal(r) ? [webMode() === 'original'
        ? { label: '切回正文模式', icon: 'file', onClick: () => setWebMode('reader', r.id) }
        : { label: '在原网页上阅读和标注', icon: 'edit', onClick: () => setWebMode('original', r.id) }] : []),
      ...(kind === 'article' ? [{ label: '用 iframe 嵌入原网页（无法标注）', icon: 'eye', onClick: () => switchResource(r.id, { forceIframe: true }) }] : []),
      { label: '复制链接', icon: 'copy', onClick: () => copyText(r.url) },
      ...(!STATIC && !r.main ? ['-',
        { label: '修改类型 / 标题', icon: 'edit', onClick: () => editResource(r) },
        { label: '删除这个资源', icon: 'trash', danger: true, onClick: () => removeResource(r) }] : []),
    ]);
  }

  async function addResource() {
    const r = await promptDialog({
      title: '关联资源',
      fields: [
        { name: 'url', label: '链接', placeholder: 'https://github.com/…  或项目主页 / 博客 / 视频 / arXiv', hint: '同一篇工作的论文、代码、主页、博客、视频……都可以关联到一起' },
        { name: 'kind', label: '类型', type: 'select', options: KIND_OPTIONS, value: '' },
        { name: 'title', label: '标题（可选）', placeholder: '留空则自动获取' },
      ],
      okText: '关联',
    });
    if (!r || !r.url.trim()) return;
    const body = { url: r.url.trim() };
    if (r.kind) body.kind = r.kind;
    if (r.title.trim()) body.title = r.title.trim();
    try {
      const res = await post(`/projects/${P}/items/${I}/resources`, body);
      const entry = res.resource || res;
      item.resources = (item.resources || []).filter((x) => x.id !== entry.id).concat(entry);
      resources = itemResources(item, meta);
      toast(`已关联：${resLabel(entry, { long: true })}`, 'ok');
      await switchResource(entry.id);
    } catch (e) { toastError(e); }
  }

  async function editResource(r) {
    const v = await promptDialog({
      title: '修改资源',
      fields: [
        { name: 'kind', label: '类型', type: 'select', options: KIND_OPTIONS.filter((o) => o.value), value: r.kind || 'other' },
        { name: 'title', label: '标题', value: r.title || '' },
      ],
    });
    if (!v) return;
    try {
      const updated = await patch(`/projects/${P}/items/${I}/resources/${encodeURIComponent(r.id)}`, { kind: v.kind, title: v.title.trim() });
      const entry = updated.resource || updated;
      item.resources = (item.resources || []).map((x) => (x.id === r.id ? { ...x, ...entry } : x));
      resources = itemResources(item, meta);
      if (ctx.resource === r.id && viewerFor(r) !== viewerFor(ctx.resourceById(r.id))) await switchResource(r.id);
      else renderResBar();
      ctx.panels.refresh();
    } catch (e) { toastError(e); }
  }

  async function removeResource(r) {
    const n = paper.annotations.filter((a) => a.resource_id === r.id).length;
    if (!(await confirmDialog(`删除资源「${resLabel(r, { long: true })}」？${n ? `它上面的 ${n} 个标注会一起删除，` : ''}笔记会保留（去掉定位）。`, { danger: true, okText: '删除' }))) return;
    try {
      await del(`/projects/${P}/items/${I}/resources/${encodeURIComponent(r.id)}`);
      item.resources = (item.resources || []).filter((x) => x.id !== r.id);
      resources = itemResources(item, meta);
      paper.annotations = paper.annotations.filter((a) => a.resource_id !== r.id);
      ctx.paper.annotations = paper.annotations;
      for (const nt of paper.notes) if (nt.resource_id === r.id) { nt.anchor = null; nt.annotation_id = null; nt.resource_id = null; }
      if (ctx.resource === r.id) await switchResource('main');
      else renderResBar();
      ctx.panels.refresh();
    } catch (e) { toastError(e); }
  }

  function updateUrl(rid) {
    const path = `/r/${P}/${I}${rid && rid !== 'main' ? `?res=${encodeURIComponent(rid)}` : ''}`;
    try {
      if (STATIC) history.replaceState(history.state, '', `${location.pathname}${location.search}#${path}`);
      else history.replaceState(history.state, '', path);
    } catch { /* ignore */ }
  }

  // ---------------- 切换资源 ----------------
  let switchTok = 0;
  const uploaded = new Set();
  let onScrollEl = null;

  function attachView(v) {
    if (onScrollEl) onScrollEl.removeEventListener('scroll', onScroll);
    onScrollEl = v && v.scrollEl;
    if (onScrollEl) onScrollEl.addEventListener('scroll', onScroll, { passive: true });
  }

  function updateTopForView(res, v) {
    const isPdf = v instanceof PdfView;
    const isArt = !!(v && v.isArticle);
    pageBox.style.display = isPdf ? '' : 'none';
    zoomGroup.style.display = isPdf || isArt ? '' : 'none';
    zoomLabel.textContent = `${Math.round(((v && v.scale) || 1) * 100)}%`;
    if (isPdf && v.numPages) pageTotal.textContent = `/ ${v.numPages}`;
    const href = (res && res.url) || meta.url || item.url;
    if (href) { extLink.href = href; extLink.style.display = ''; } else extLink.style.display = 'none';
    extLink.title = res && !res.main ? `打开原网页：${hostOf(res.url)}` : '打开原文页面';
  }

  async function switchResource(rid, { forceIframe = false, page = null } = {}) {
    const res = ctx.resourceById(rid) || resources[0];
    const tok = ++switchTok;
    hideFloating();
    closePopMenus();
    try { ctx.book && ctx.book.stop(); } catch { /* ignore */ }
    if (view) {
      if (view instanceof PdfView && ctx.resource === 'main') { try { savePage.flush(view.current); } catch { /* ignore */ } }
      try { view.destroy(); } catch { /* ignore */ }
      view = null;
    }
    viewHost.replaceChildren();
    ctx.resource = res.id;
    renderResBar();
    updateUrl(res.id);
    updateTopForView(res, null);
    const host = h('div', { class: 'rd-view-inner' });
    viewHost.append(host);
    let v = null;
    try {
      const kind = forceIframe ? 'iframe' : viewerFor(res);
      if (kind === 'pdf') v = await openPdf(res, host, tok, page);
      else if (kind === 'article') v = canOriginal(res) && webMode() === 'original' ? await openOriginal(res, host, tok) : await openArticle(res, host, tok);
      else if (kind === 'video') v = new VideoView(host, commonOpts()).load({ embed: videoEmbed(res.url), url: res.url, title: res.title !== res.url ? res.title : '', kind: res.kind });
      else if (kind === 'iframe') v = new IframeView(host, commonOpts()).load({ url: res.url, hint: forceIframe ? '嵌入原网页：这里无法划词标注；如果一片空白，说明网站拒绝被嵌入。' : null });
      else v = noticeView(host, res, { message: '这个资源无法在页面里直接显示。' });
    } catch (e) {
      console.error(e);
      if (tok === switchTok) v = noticeView(host, res, { message: `打开失败：${e.message || e}` });
    }
    if (tok !== switchTok) {
      try { v && v.destroy(); } catch { /* ignore */ }
      return null;
    }
    view = v;
    attachView(v);
    if (v) {
      v.setAnnotations(ctx.annotationsFor(res.id));
      v.setMarkerResolver(markerResolver);
    }
    updateTopForView(res, v);
    try { ctx.book.resetView(res.id === 'main'); } catch (e) { console.warn(e); }
    try { ttsBar.reset && ttsBar.reset(); } catch { /* ignore */ }
    ctx.panels.refresh();
    return v;
  }

  async function openPdf(res, host, tok, page) {
    const isMain = res.id === 'main';
    const url = pdfUrlOf(res);
    const loading = loadingBox('正在加载 PDF（只在内存中，不落盘）…');
    loading.querySelector('.bar').classList.remove('indeterminate');
    const pdf = new PdfView(host, {
      ...commonOpts(),
      onProgress: (loaded, total) => {
        const bar = loading.querySelector('.bar i');
        if (total) bar.style.width = `${Math.round((loaded / total) * 100)}%`;
        loading.querySelector('.ld-text').textContent = `正在加载 PDF（只在内存中，不落盘）… ${(loaded / 1048576).toFixed(1)} MB`;
      },
      onPageChange: (n) => {
        pageInput.value = String(n);
        if (isMain) savePage(n);
      },
      onScaleChange: (s) => {
        zoomLabel.textContent = `${Math.round(s * 100)}%`;
        if (isMain) saveZoom(s);
      },
    });
    host.append(loading);
    try {
      if (!url) throw new Error('这条资源没有 PDF 地址');
      // 本地：经后端代理（内存流式，不落盘）；静态只读版：pdf.js 直连源站（需源站允许跨域）
      await pdf.load(STATIC ? url : `/api/pdf?url=${encodeURIComponent(url)}`, {
        scale: isMain ? paper.zoom || null : null,
        page: page || (isMain ? pageParam || paper.last_page || 1 : 1),
      });
    } catch (e) {
      console.error(e);
      try { pdf.destroy(); } catch { /* ignore */ }
      if (tok !== switchTok) return null;
      host.replaceChildren();
      if (STATIC && url) return pdfIframeView(host, res, url);
      return noticeView(host, res, {
        message: h('div', {}, h('div', { class: 'err-text', style: { marginBottom: '6px' } }, 'PDF 加载失败'), h('div', { class: 'muted', style: { fontSize: '12.5px' } }, e && e.message ? e.message : String(e))),
        actions: [h('a', { class: 'btn btn-sm', href: url || res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开原 PDF')],
      });
    }
    loading.remove();
    if (tok !== switchTok) return pdf;
    pageTotal.textContent = `/ ${pdf.numPages}`;
    zoomLabel.textContent = `${Math.round(pdf.scale * 100)}%`;
    setTimeout(() => { if (!pdf.destroyed) ctx.book.onDocReady(); }, 0);
    if (!STATIC && !uploaded.has(res.id) && (!isMain || !paper.text_cached)) uploadText(pdf, res.id);
    return pdf;
  }

  async function openArticle(res, host, tok) {
    const isCode = isGithubRepo(res.url);
    const loading = loadingBox(isCode ? '正在获取 README…' : '正在抓取网页正文（只在内存中，不落盘）…');
    host.append(loading);
    let data;
    try {
      if (STATIC) {
        if (!isCode) {
          loading.remove();
          return staticWebNotice(host, res);
        }
        data = await fetchReadmeDirect(res.url);
      } else {
        data = await get(`/web?url=${encodeURIComponent(res.url)}`);
      }
      if (data && data.error) throw new Error(data.error);
    } catch (e) {
      if (tok !== switchTok) return null;
      loading.remove();
      if (STATIC) return staticWebNotice(host, res, e);
      return noticeView(host, res, {
        message: `无法抓取这个网页：${e.message || e}`,
        actions: [
          h('a', { class: 'btn btn-primary btn-sm', href: res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开原网页'),
          h('button', { class: 'btn btn-sm', onclick: () => switchResource(res.id, { forceIframe: true }) }, icon('eye', 13), '仍然用 iframe 嵌入（可能被网站拒绝，无法标注）'),
          h('button', { class: 'btn btn-sm btn-ghost', onclick: () => switchResource(res.id) }, icon('refresh', 13), '重试'),
        ],
      });
    }
    if (tok !== switchTok) return null;
    loading.remove();
    const banner = data.weak ? h('div', { class: 'weak-banner' },
      h('span', {}, '正文抽取可能不完整（这个网站可能要靠脚本渲染、需要登录，或者正文很短）。'),
      h('span', { class: 'spacer' }),
      h('a', { class: 'btn btn-sm', href: res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开原网页'),
      h('button', { class: 'btn btn-sm', onclick: () => switchResource(res.id, { forceIframe: true }) }, icon('eye', 13), '仍然用 iframe 嵌入（无法标注）')) : null;
    const v = new ArticleView(host, { ...commonOpts(), onOriginalMode: canOriginal(res) ? () => setWebMode('original', res.id) : null }).load(data, { resource: res, banner });
    if (res.main && !item.title && data.title) item.title = data.title;
    if (!res.title || res.title === res.url) res.title = data.title || res.title;
    renderResBar();
    setTimeout(() => { if (!v.destroyed) ctx.book.onDocReady(); }, 0);
    if (!STATIC && !uploaded.has(res.id)) uploadText(v, res.id);
    return v;
  }

  // ---------------- 原网页模式（在原网页上标注） ----------------
  // 选择会记住（localStorage），之后打开博客 / 网页都用同一种模式；GitHub 仓库始终用 README 视图。
  function webMode() {
    try { return localStorage.getItem('pd.webMode') === 'original' ? 'original' : 'reader'; } catch { return 'reader'; }
  }
  function setWebMode(mode, rid) {
    try { localStorage.setItem('pd.webMode', mode); } catch { /* ignore */ }
    switchResource(rid);
  }
  function canOriginal(res) {
    return !STATIC && !!res && !!res.url && !isGithubRepo(res.url);
  }

  async function openOriginal(res, host, tok) {
    const loading = loadingBox('正在加载原网页（只在内存中，不落盘）…');
    host.append(loading);
    let data;
    try {
      data = await get(`/webpage?url=${encodeURIComponent(res.url)}`);
      if (data && data.error) throw new Error(data.error);
    } catch (e) {
      if (tok !== switchTok) return null;
      loading.remove();
      return noticeView(host, res, {
        message: `无法加载原网页：${e.message || e}`,
        actions: [
          h('button', { class: 'btn btn-primary btn-sm', onclick: () => setWebMode('reader', res.id) }, icon('file', 13), '切回正文模式'),
          h('a', { class: 'btn btn-sm', href: res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开原网页'),
          h('button', { class: 'btn btn-sm btn-ghost', onclick: () => switchResource(res.id) }, icon('refresh', 13), '重试'),
        ],
      });
    }
    if (tok !== switchTok) return null;
    loading.remove();
    const page = sanitizePage(data.html, data.url || res.url);
    const v = new OriginalView(host, { ...commonOpts(), onReaderMode: () => setWebMode('reader', res.id) })
      .load({ ...page, url: data.url || res.url }, { resource: res });
    v.ready.then(() => {
      if (v.destroyed || tok !== switchTok) return;
      const t = page.title || v.pageTitle;
      if (t && (!res.title || res.title === res.url)) { res.title = t; renderResBar(); }
      if (res.main && !item.title && t) item.title = t;
      ctx.book.onDocReady();
      ctx.panels.refresh();
      if (!uploaded.has(res.id)) uploadText(v, res.id);
    });
    return v;
  }

  function noticeView(host, res, { message, actions = [] } = {}) {
    const km = kindMeta(res.kind);
    return new NoticeView(host, commonOpts()).load({
      site: h('span', {}, km.icon, ' ', hostOf(res.url) || km.label),
      title: res.title && res.title !== res.url ? res.title : hostOf(res.url),
      message,
      actions: actions.length ? actions : [h('a', { class: 'btn btn-primary btn-sm', href: res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开')],
    });
  }

  /** 静态只读版：网页资源无法经代理抓取 → 打开按钮 + 这个资源上的标注与笔记 */
  function staticWebNotice(host, res, err) {
    const annos = ctx.annotationsFor(res.id).filter((a) => a.kind !== 'vocab');
    const notes = paper.notes.filter((n) => (n.resource_id || 'main') === res.id);
    const list = h('div', { class: 'notice-list' });
    if (annos.length || notes.length) {
      list.append(h('div', { class: 'sub-title' }, '这个资源上的标注与笔记', h('span', { class: 'faint' }, annos.length + notes.length)));
      for (const a of annos) {
        list.append(h('div', { class: 'alist-item static' },
          h('span', { class: `sw c-${a.color} k-${a.kind}` }),
          h('span', { class: 'q' }, a.quote, a.comment ? h('span', { class: 'faint' }, ` — ${truncate(a.comment.replace(/\s+/g, ' '), 120)}`) : null)));
      }
      for (const n of notes) {
        const md = h('div', { class: 'md' });
        setMarkdown(md, n.text);
        list.append(h('div', { class: 'ncard' }, n.quote ? h('div', { class: 'nquote' }, truncate(n.quote, 280)) : null, md));
      }
    } else list.append(h('div', { class: 'faint' }, '这个资源上还没有标注或笔记。'));
    const km = kindMeta(res.kind);
    return new NoticeView(host, commonOpts()).load({
      site: h('span', {}, km.icon, ' ', hostOf(res.url) || km.label),
      title: res.title && res.title !== res.url ? res.title : hostOf(res.url),
      message: `在线只读版无法抓取网页正文（需要本地 PaperDesk 的代理）。${err ? `（${err.message || err}）` : ''}`,
      actions: [h('a', { class: 'btn btn-primary btn-sm', href: res.url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开')],
      extra: list,
    });
  }

  // 静态只读版：pdf.js 跨域失败 → 浏览器自带 PDF 查看器（iframe #page=N）→ 再失败只显示右侧内容
  function pdfIframeView(host, res, url) {
    pageTotal.textContent = '';
    const openBtn = h('a', { class: 'btn btn-sm', href: url, target: '_blank', rel: 'noopener' }, icon('external', 13), '在新标签页打开 PDF');
    const wrap = h('div', { class: 'iframe-wrap' },
      h('div', { class: 'iframe-hint' }, h('span', {}, '此 PDF 源站不允许跨域，标注无法叠加显示，已按页码定位'), h('span', { class: 'spacer' }), openBtn));
    const isMain = res.id === 'main';
    let cur = isMain ? pageParam || paper.last_page || 1 : 1;
    let frame = null;
    let everLoaded = false;
    const stub = new NoticeView(host, commonOpts());
    stub.isMedia = false;
    stub.numPages = 0;
    Object.defineProperty(stub, 'current', { get: () => cur, set: () => {}, configurable: true });
    const setPage = (n) => {
      cur = Math.max(1, n || 1);
      pageInput.value = String(cur);
      if (frame) frame.remove();
      frame = h('iframe', { src: `${url}#page=${cur}`, title: 'PDF', referrerpolicy: 'no-referrer' });
      frame.addEventListener('load', () => { everLoaded = true; });
      wrap.append(frame);
      if (isMain) savePage(cur);
    };
    stub.scrollToPage = (n) => setPage(n);
    stub.goToAnnotation = (a) => a && a.page && setPage(a.page);
    stub.scrollEl.classList.add('iframe-mode');
    stub.scrollEl.replaceChildren(wrap);
    setPage(cur);
    setTimeout(() => { if (!everLoaded && !stub.destroyed) noViewer(); }, 20000);
    function noViewer() {
      stub.scrollEl.replaceChildren(h('div', { class: 'pdf-fallback' }, h('div', {},
        h('div', { style: { marginBottom: '10px' } }, '无法在页面中显示这个 PDF，右侧的笔记、标注和问答仍可查看。'),
        h('a', { class: 'btn btn-primary', href: res.url || url, target: '_blank', rel: 'noopener' }, icon('external', 14), (isMain && (meta.arxiv_id || item.arxiv_id)) ? '在 arXiv 打开' : '打开原文'))));
    }
    setTimeout(() => { pageBox.style.display = ''; zoomGroup.style.display = 'none'; }, 0);
    return stub;
  }

  async function uploadText(v, rid) {
    try {
      const pages = await v.getAllText();
      if (v.destroyed || !pages || !pages.length) return;
      await put(`${base}/text`, { resource_id: rid, pages });
      uploaded.add(rid);
      if (rid === 'main') paper.text_cached = true;
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
      ctx.pdf.clearSelection();
    };
    const annotate = (kind, color) => act(async () => {
      try {
        ctx.lastColor = color;
        localStorage.setItem('pd.color', color);
        await ctx.createAnnotation({ kind, color, page: info.page, quote: info.quote, rects: info.rects, anchor: info.anchor });
        ctx.pdf.clearSelection();
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
      h('button', { 'data-action': 'note', title: '针对这段写笔记（会同时高亮）', onclick: act(() => ctx.panels.newNote({ quote: info.quote, page: info.page, rects: info.rects, anchor: info.anchor })) }, icon('pencil', 14), '记一笔'),
      h('span', { class: 'tb-sep' }),
      COLORS.map((c) => h('button', { 'data-action': 'highlight', 'data-color': c, class: `color-dot c-${c} ${c === ctx.lastColor ? 'sel' : ''}`, title: `${c} 高亮`, onclick: annotate('highlight', c) })),
      h('span', { class: 'tb-sep' }),
      h('button', { 'data-action': 'underline', class: 'tb-kind', title: '下划线', onclick: annotate('underline', ctx.lastColor) }, h('u', {}, 'U')),
      h('button', { 'data-action': 'strike', class: 'tb-kind', title: '删除线', onclick: annotate('strike', ctx.lastColor) }, h('s', {}, 'S')),
      h('button', { 'data-action': 'squiggly', class: 'tb-kind', title: '波浪线', onclick: annotate('squiggly', ctx.lastColor) }, h('span', { style: { textDecoration: 'underline wavy', textUnderlineOffset: '3px' } }, 'W')),
      h('span', { class: 'tb-sep' }),
      h('button', { 'data-action': 'tts-sel', title: '朗读选中内容', onclick: act(() => tts.speak(info.quote)) }, '🔊'),
      h('button', { 'data-action': 'vocab', title: '加入生词本（单词或 ≤6 个词的短语）', onclick: act(() => ctx.panels.addVocabFromSelection(info)) }, icon('book', 14), '生词'),
    );
    floating = tb;
    placeFloating(tb, info.clientRect, 'above');
  }

  // ---------------- 标注菜单 ----------------
  function locLabel(a) {
    if (a.page) return `p.${a.page}`;
    const r = ctx.resourceById(a.resource_id);
    return r ? resLabel(r) : '';
  }

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
        menu.append(h('div', { class: 'am-row' }, h('span', { class: 'faint', style: { fontSize: '12px' } }, `${locLabel(a)} · ${KIND_LABEL[a.kind] || ''}`), h('span', { class: 'spacer' }),
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
      if (!e.target.closest('.textLayer, .article-body')) hideFloating();
    }
  };
  function onScroll() { if (floating && floating.classList.contains('sel-toolbar')) hideFloating(); }
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
  document.addEventListener('selectionchange', onSelChange);
  document.addEventListener('keydown', onKey);

  const flush = () => {
    try { if (view instanceof PdfView && ctx.resource === 'main') savePage.flush(view.current); } catch { /* ignore */ }
  };
  window.addEventListener('beforeunload', flush);

  // ---------------- 打开初始资源 ----------------
  renderResBar();
  switchResource(initialRid).then((v) => {
    if (annoParam && v) {
      const a = ctx.annotationById(annoParam);
      if (a) setTimeout(() => v.goToAnnotation(a), 400);
    }
  });

  return {
    destroy() {
      flush();
      switchTok++;
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('mousedown', onDocDown);
      document.removeEventListener('selectionchange', onSelChange);
      document.removeEventListener('keydown', onKey);
      if (onScrollEl) onScrollEl.removeEventListener('scroll', onScroll);
      hideFloating();
      closePopMenus();
      try { ctx.book.destroy(); } catch { /* ignore */ }
      try { ctx.voice.destroy(); } catch { /* ignore */ }
      try { ttsBar.destroy && ttsBar.destroy(); } catch { /* ignore */ }
      try { ctx.panels.destroy && ctx.panels.destroy(); } catch { /* ignore */ }
      try { view && view.destroy(); } catch { /* ignore */ }
      document.title = 'PaperDesk';
    },
  };
}

/** 阅读器还没加载出来时的占位：所有方法都是安全的空操作 */
function makeNullView() {
  const noop = () => {};
  return {
    isNull: true, isArticle: false, isMedia: true, current: 1, numPages: 0, scale: 1, doc: null, destroyed: false, annotations: [],
    scrollEl: document.createElement('div'),
    setAnnotations: noop, upsertAnnotation: noop, removeAnnotation: noop, setMarkerResolver: noop,
    clearSelection: () => { const s = window.getSelection(); s && s.removeAllRanges(); },
    getSelectionInfo: () => null, hitTest: () => null, flashAnnotation: noop, goToAnnotation: noop, flashAnchor: noop, flashRects: noop,
    scrollToPage: noop, scrollPage: noop, getPageText: async () => '', getPageParagraphs: async () => [], getAllText: async () => [],
    ensureText: async () => null, fitWidthScale: () => 1, setScale: noop, destroy: noop,
  };
}
