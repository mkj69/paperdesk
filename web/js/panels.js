// 阅读页右侧面板：笔记 | 问答 | 翻译 | 生词 | 总结
import { post, patch, del, sse, cache, STATIC, appHref, markdownUrl } from './api.js';
import { h, icon, toast, toastError, confirmDialog, copyText, fmtTime, truncate, ENGINE_LABEL, escapeHtml } from './ui.js';
import { setMarkdown } from './markdown.js';
import { createEditor } from './editor.js';
import { speakButton, splitSentences } from './tts.js';
import { githubFileUrl, refreshSync } from './sync.js';
import { kindMeta, resLabel } from './resources.js';

const TABS = [
  ['notes', '笔记'],
  ['qa', '问答'],
  ['translate', '翻译'],
  ['vocab', '生词'],
  ['summary', '总结'],
];
const SRC_LABEL = { manual: '', qa: '来自问答', translation: '来自翻译', voice: '语音' };

export function createPanels(ctx, root) {
  const { paper, base } = ctx;
  let active = localStorage.getItem('pd.tab') || 'notes';
  if (!TABS.some(([k]) => k === active)) active = 'notes';
  const tabBtns = {};
  const tabsEl = h('div', { class: 'tabs' });
  for (const [k, label] of TABS) {
    tabBtns[k] = h('button', { 'data-tab': k, class: 'tab', onclick: () => switchTab(k) }, label, h('span', { class: 'n' }));
    tabsEl.append(tabBtns[k]);
  }
  const views = {};
  for (const [k] of TABS) views[k] = h('div', { class: 'panel-view', style: { display: 'none', flex: '1', minHeight: '0', flexDirection: 'column' } });
  root.append(tabsEl, ...Object.values(views));

  function switchTab(k) {
    active = k;
    localStorage.setItem('pd.tab', k);
    for (const [key] of TABS) {
      tabBtns[key].classList.toggle('active', key === k);
      views[key].style.display = key === k ? 'flex' : 'none';
    }
    if (!rendered[k]) refresh(k);
  }
  const rendered = {};

  function updateCounts() {
    const n = {
      notes: paper.notes.length + paper.annotations.filter((a) => a.kind !== 'vocab').length,
      qa: paper.qa.length,
      translate: paper.translations.length,
      vocab: paper.vocab.length,
      summary: paper.summary ? '✓' : '',
    };
    for (const [k] of TABS) tabBtns[k].querySelector('.n').textContent = n[k] ? String(n[k]) : '';
  }

  function refresh(k) {
    updateCounts();
    if (!k) {
      for (const [key] of TABS) if (rendered[key]) refresh(key);
      return;
    }
    rendered[k] = true;
    ({ notes: renderNotes, qa: renderQa, translate: renderTranslate, vocab: renderVocab, summary: renderSummary })[k]();
  }

  // =============== 笔记 ===============
  const notesTools = h('div', { class: 'panel-tools' });
  const notesEditorSlot = h('div', { style: { padding: '0 14px' } });
  const notesBody = h('div', { class: 'panel-body' });
  const notesList = h('div', { class: 'panel-pad', style: { paddingTop: '4px' } });
  notesBody.append(notesEditorSlot, notesList);
  const notesFoot = h('div', { class: 'panel-foot' });
  views.notes.append(notesTools, notesBody, notesFoot);
  notesTools.append(
    STATIC ? null : h('button', { 'data-action': 'new-note', class: 'btn btn-sm', onclick: () => newNote({ page: ctx.currentPage() }) }, icon('plus', 14), '新笔记'),
    h('span', { class: 'spacer' }),
    h('span', { class: 'faint', style: { fontSize: '12px' } }, STATIC ? '点击笔记或标注跳到原文位置' : '选中文字可高亮、划线、记笔记'));

  function mdPath() {
    return `projects/${ctx.pid}/papers/${paper.paper_key}.md`;
  }

  // ---------- 多资源：徽标与跳转 ----------
  const multi = () => (ctx.resources || []).length > 1;
  const ridOf = (x) => (x && x.resource_id) || 'main';
  function resBadge(rid, onClick, force = false) {
    const r = ctx.resourceById ? ctx.resourceById(rid) : null;
    if (!r) return null;
    if (!multi() && rid === 'main' && !(force && r.kind !== 'paper')) return null;
    const km = kindMeta(r.kind);
    return h('button', { class: `rbadge ${rid === ctx.resource ? 'cur' : ''}`, title: `${resLabel(r, { long: true })}\n点击切换到这个资源`, onclick: onClick || (() => ctx.gotoResource(rid)) },
      h('span', {}, km.icon), h('span', {}, r.main && r.kind === 'paper' ? '论文' : km.label));
  }
  function resIndex(rid) {
    const i = (ctx.resources || []).findIndex((r) => r.id === rid);
    return i < 0 ? 999 : i;
  }
  function anchorStart(x) {
    return (x && x.anchor && Number.isInteger(x.anchor.start)) ? x.anchor.start : 0;
  }

  function renderNotesFoot() {
    const gh = githubFileUrl(mdPath());
    notesFoot.replaceChildren(
      h('a', { class: 'btn btn-sm', href: markdownUrl(base, ctx.pid, paper.paper_key), target: '_blank' }, icon('markdown', 14), '查看 Markdown'),
      gh ? h('a', { class: 'btn btn-sm', href: gh, target: '_blank' }, icon('github', 14), '在 GitHub 上查看') : null,
      STATIC ? null : h('button', { class: 'btn btn-sm btn-ghost', onclick: () => post('/open_folder', { pid: ctx.pid }).catch(toastError) }, icon('folder', 14), 'Finder'));
  }
  document.addEventListener('pd:sync', renderNotesFoot);

  function newNote({ quote, page, rects, annotation_id, anchor } = {}) {
    switchTab('notes');
    notesEditorSlot.replaceChildren();
    const ed = createEditor({
      quote: quote ? truncate(quote, 300) : null,
      placeholder: quote ? '对这段的想法…（**加粗**、<ins>下划线</ins>、$公式$）' : (ctx.isArticleView && ctx.isArticleView() ? '写一条笔记…（会记在当前资源）' : '写一条笔记…（会记在当前页）'),
      saveLabel: '保存笔记',
      onCancel: () => notesEditorSlot.replaceChildren(),
      onSave: async (text) => {
        try {
          let aid = annotation_id;
          if (!aid && ((rects && rects.length) || anchor)) {
            const a = await ctx.createAnnotation({ kind: 'highlight', color: ctx.lastColor || 'yellow', page, quote, rects, anchor });
            aid = a.id;
            ctx.pdf.clearSelection();
          }
          await ctx.addNote({ text, page: page || ctx.currentPage(), quote: quote || null, annotation_id: aid || null, anchor: anchor || null });
          notesEditorSlot.replaceChildren();
          toast('笔记已保存', 'ok', 1200);
        } catch (e) {
          toastError(e);
        }
      },
    });
    notesEditorSlot.append(ed.el);
    notesBody.scrollTop = 0;
    ed.focus();
  }

  async function goToAnchor({ annotation_id, page, quote, resource_id, anchor }) {
    const a = annotation_id && ctx.annotationById(annotation_id);
    if (a) return ctx.pdf.goToAnnotation(a);
    const rid = resource_id || 'main';
    if (ctx.gotoResource && rid !== ctx.resource && ctx.resourceById(rid)) await ctx.gotoResource(rid);
    if (quote) {
      const same = paper.annotations.find((x) => ridOf(x) === rid && x.quote === quote && (!page || x.page === page));
      if (same) return ctx.pdf.goToAnnotation(same);
    }
    if (ctx.isArticleView && ctx.isArticleView()) {
      if (anchor || quote) ctx.pdf.flashAnchor(anchor || { exact: quote });
      return;
    }
    if (page) ctx.pdf.scrollToPage(page);
  }

  function noteCard(n) {
    const a = n.annotation_id && ctx.annotationById(n.annotation_id);
    const body = h('div', { class: 'md' });
    setMarkdown(body, n.text);
    const card = h('div', { class: 'ncard', dataset: { id: n.id } });
    const edit = () => {
      const ed = createEditor({
        value: n.text,
        quote: n.quote ? truncate(n.quote, 200) : null,
        onCancel: () => fill(),
        onSave: async (text) => {
          try {
            const nn = await patch(`${base}/notes/${encodeURIComponent(n.id)}`, { text });
            Object.assign(n, nn);
            fill();
          } catch (e) { toastError(e); }
        },
      });
      card.replaceChildren(ed.el);
      ed.focus();
    };
    const remove = async () => {
      if (!(await confirmDialog('删除这条笔记？', { danger: true, okText: '删除' }))) return;
      try {
        await del(`${base}/notes/${encodeURIComponent(n.id)}`);
        paper.notes = paper.notes.filter((x) => x.id !== n.id);
        ctx.paper.notes = paper.notes;
        ctx.onPaperChanged();
        refresh('notes');
      } catch (e) { toastError(e); }
    };
    const fill = () => {
      setMarkdown(body, n.text);
      card.replaceChildren(
        h('div', { class: 'ncard-head' },
          resBadge(ridOf(n), () => goToAnchor(n), !n.page),
          n.page ? h('button', { class: 'pbadge', title: '跳到原文位置', onclick: () => goToAnchor(n) }, `p.${n.page}`) : null,
          SRC_LABEL[n.source] ? h('span', { class: 'srcbadge' }, SRC_LABEL[n.source]) : null,
          h('span', {}, fmtTime(n.updated || n.created)),
          h('span', { class: 'acts' },
            speakButton(() => n.text),
            STATIC ? null : h('button', { class: 'icon-btn', title: '编辑', onclick: edit }, icon('edit', 13)),
            STATIC ? null : h('button', { class: 'icon-btn', title: '删除', onclick: remove }, icon('trash', 13)))),
        n.quote ? h('div', { class: `nquote ${a ? 'c-' + a.color : ''}`, title: '跳到原文位置', onclick: () => goToAnchor(n) }, truncate(n.quote, 280)) : null,
        body);
    };
    if (!STATIC) body.addEventListener('dblclick', edit);
    fill();
    return card;
  }

  function renderNotes() {
    renderNotesFoot();
    notesList.replaceChildren();
    const notes = paper.notes.slice().sort((x, y) => resIndex(ridOf(x)) - resIndex(ridOf(y)) || (x.page || 1e9) - (y.page || 1e9) || anchorStart(x) - anchorStart(y) || (x.created || '').localeCompare(y.created || ''));
    const annos = paper.annotations.filter((a) => a.kind !== 'vocab')
      .sort((x, y) => resIndex(ridOf(x)) - resIndex(ridOf(y)) || (x.page || 0) - (y.page || 0) || anchorStart(x) - anchorStart(y) || ((x.rects && x.rects[0] && x.rects[0][1]) || 0) - ((y.rects && y.rects[0] && y.rects[0][1]) || 0));
    if (!notes.length && !annos.length) {
      notesList.append(h('div', { class: 'empty', style: { marginTop: '8px' } }, STATIC ? '这篇论文还没有笔记。' : '还没有笔记。选中 PDF / 网页里的文字 → 「记一笔」或高亮；也可以点「新笔记」直接写。'));
      return;
    }
    if (notes.length) {
      notesList.append(h('div', { class: 'sub-title' }, '笔记', h('span', { class: 'faint' }, notes.length)));
      for (const n of notes) notesList.append(noteCard(n));
    }
    if (annos.length) {
      notesList.append(h('div', { class: 'sub-title' }, '标注', h('span', { class: 'faint' }, annos.length)));
      for (const a of annos) {
        const r = ctx.resourceById ? ctx.resourceById(ridOf(a)) : null;
        const loc = a.page ? `p.${a.page}` : (r && multi() ? `${kindMeta(r.kind).icon}` : '');
        notesList.append(h('div', { class: 'alist-item', dataset: { res: ridOf(a) }, title: r && multi() ? `点击跳到原文（${resLabel(r, { long: true })}）` : '点击跳到原文', onclick: () => ctx.pdf.goToAnnotation(a) },
          h('span', { class: `sw c-${a.color} k-${a.kind}` }),
          h('span', { class: 'q' }, a.quote, a.comment ? h('span', { class: 'faint' }, ` — ${truncate(plainText(a.comment), 80)}`) : null),
          h('span', { class: 'pg' }, multi() && a.page && r ? `${kindMeta(r.kind).icon} ${loc}` : loc)));
      }
    }
  }

  function focusNote(id) {
    const el = notesList.querySelector(`.ncard[data-id="${CSS.escape(id)}"]`);
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.style.borderColor = 'var(--accent)';
      setTimeout(() => { el.style.borderColor = ''; }, 1600);
    }
  }

  // =============== 问答 ===============
  const qaTools = h('div', { class: 'panel-tools' });
  const qaBody = h('div', { class: 'panel-body' });
  const qaList = h('div', { class: 'qa-list' });
  qaBody.append(qaList);
  const qaChip = h('div', { class: 'qa-chip', style: { display: 'none' } });
  const qaTa = h('textarea', { 'data-field': 'ask-input', rows: 1, placeholder: '问这篇论文任何问题…（⌘/Ctrl+Enter 发送）' });
  const sendBtn = h('button', { 'data-action': 'ask', class: 'btn btn-primary', title: '发送 (⌘/Ctrl+Enter)', style: { height: '38px' } }, icon('send', 15));
  const micSlot = h('span');
  const voiceSlot = h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px' } });
  const qaMeta = h('div', { class: 'qa-meta' });
  const qaInput = h('div', { class: 'qa-input' }, qaChip, h('div', { class: 'qa-box' }, qaTa, micSlot, sendBtn), qaMeta);
  views.qa.append(qaTools, qaBody, qaInput);
  if (STATIC) { qaTools.style.display = 'none'; qaInput.style.display = 'none'; }
  let qaQuote = null;
  let busy = false;

  qaTools.append(
    h('button', { class: 'btn btn-sm', title: '清空 AI 的上下文，重新开始一段对话（历史记录保留）', onclick: async () => {
      try {
        await post(`${base}/reset_session`, { engine: ctx.engine });
        toast(`已开始新的 ${ENGINE_LABEL[ctx.engine]} 会话`, 'ok');
      } catch (e) { toastError(e); }
    } }, icon('refresh', 14), '新会话'),
    h('span', { class: 'spacer' }),
    voiceSlot);

  const autosize = () => {
    qaTa.style.height = 'auto';
    qaTa.style.height = `${Math.min(qaTa.scrollHeight + 2, 180)}px`;
  };
  qaTa.addEventListener('input', autosize);
  qaTa.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submitQa();
    }
  });
  sendBtn.onclick = () => submitQa();

  function updateQaMeta() {
    qaMeta.replaceChildren(h('span', {}, `回答引擎：${ENGINE_LABEL[ctx.engine] || ctx.engine}`), h('span', {}, '·'), h('span', {}, 'AI 能读到全文和你的笔记'));
  }

  function setQuote(quote, page) {
    qaQuote = quote ? { quote, page } : null;
    if (qaQuote) {
      qaChip.style.display = '';
      qaChip.replaceChildren(icon('quote', 13), h('span', {}, `${page ? `p.${page} ` : ''}${quote}`),
        h('button', { class: 'icon-btn', title: '移除引文', onclick: () => setQuote(null) }, icon('x', 12)));
      switchTab('qa');
      qaTa.focus();
    } else {
      qaChip.style.display = 'none';
      qaChip.replaceChildren();
    }
  }

  async function submitQa() {
    const q = qaTa.value.trim();
    if (!q) return qaTa.focus();
    if (busy) return toast('上一个问题还在回答中', 'info');
    qaTa.value = '';
    autosize();
    const quote = qaQuote;
    setQuote(null);
    await ask({ question: q, quote: quote && quote.quote, page: (quote && quote.page) || (ctx.storePage ? ctx.storePage(ctx.currentPage()) : ctx.currentPage()) });
  }

  function qaEntry(qa, { streaming = false } = {}) {
    const ans = h('div', { class: `qa-a md ${streaming ? 'streaming' : ''}` });
    if (qa.answer) setMarkdown(ans, qa.answer);
    const status = h('div', { class: 'qa-status', style: { display: 'none' } });
    const foot = h('div', { class: 'qa-foot' });
    const el = h('div', { class: 'qa-item', dataset: { id: qa.id || '' } },
      h('div', { class: 'qa-q' },
        qa.quote ? h('span', { class: 'qa-quote' }, `${qa.page ? `p.${qa.page} ` : ''}${truncate(qa.quote, 240)}`) : null,
        qa.mode === 'explain' && !qa.question ? '请解释这段' : qa.question),
      ans, status, foot);
    const fillFoot = () => {
      foot.replaceChildren(
        h('span', {}, `${ENGINE_LABEL[qa.engine] || qa.engine || ''}${qa.style === 'voice' ? ' · 语音' : ''} · ${fmtTime(qa.at)}`),
        h('span', { class: 'spacer' }),
        speakButton(() => qa.answer || ''),
        STATIC ? null : h('button', { class: 'lnk', onclick: () => saveQaAsNote(qa) }, '收进笔记'),
        h('button', { class: 'icon-btn', title: '复制回答', onclick: () => copyText(qa.answer || '') }, icon('copy', 13)),
        qa.id && !STATIC ? h('button', { class: 'icon-btn', title: '删除这条问答', onclick: () => removeQa(qa) }, icon('trash', 13)) : null);
    };
    if (!streaming) fillFoot();
    return { el, ans, status, fillFoot };
  }

  async function saveQaAsNote(qa) {
    try {
      const text = `**Q：${qa.question}**\n\n${qa.answer || ''}`;
      await ctx.addNote({ text, page: qa.page || null, quote: qa.quote || null, source: 'qa' });
      toast('已收进笔记', 'ok', 1400);
    } catch (e) { toastError(e); }
  }

  async function removeQa(qa) {
    if (!(await confirmDialog('删除这条问答记录？', { danger: true, okText: '删除' }))) return;
    try {
      await del(`${base}/qa/${encodeURIComponent(qa.id)}`);
      paper.qa = paper.qa.filter((x) => x.id !== qa.id);
      ctx.paper.qa = paper.qa;
      refresh('qa');
    } catch (e) { toastError(e); }
  }

  function renderQa() {
    updateQaMeta();
    qaList.replaceChildren();
    if (!paper.qa.length) {
      qaList.append(h('div', { class: 'empty' }, STATIC ? '这篇论文还没有问答记录。' : '向 AI 提问：它会读这篇论文的全文（以及关联的博客 / README）和你的笔记来回答。也可以选中一段 → 「问 Agent」或「解释」。'));
    }
    for (const qa of paper.qa) qaList.append(qaEntry(qa).el);
    requestAnimationFrame(() => { qaBody.scrollTop = qaBody.scrollHeight; });
  }

  /**
   * 提问（流式）。返回保存后的 qa 记录（失败返回 null）。
   * @param {{question:string,quote?:string,page?:number,mode?:'ask'|'explain',style?:string,onDelta?:Function,onStatus?:Function}} o
   */
  async function ask({ question, quote, page, mode = 'ask', style, onDelta, onStatus }) {
    if (busy) {
      toast('上一个问题还在回答中', 'info');
      return null;
    }
    busy = true;
    sendBtn.disabled = true;
    switchTab('qa');
    const empty = qaList.querySelector('.empty');
    if (empty) empty.remove();
    const tmp = { question, quote, page, mode, style, engine: ctx.engine, answer: '', at: new Date().toISOString() };
    const ent = qaEntry(tmp, { streaming: true });
    qaList.append(ent.el);
    qaBody.scrollTop = qaBody.scrollHeight;
    let text = '';
    let saved = null;
    let errMsg = null;
    let raf = null;
    const paint = () => {
      raf = null;
      const nearBottom = qaBody.scrollHeight - qaBody.scrollTop - qaBody.clientHeight < 80;
      setMarkdown(ent.ans, text);
      if (nearBottom) qaBody.scrollTop = qaBody.scrollHeight;
    };
    try {
      const body = { question, quote: quote || null, page: page || null, engine: ctx.engine, mode };
      if (ctx.resource) body.resource_id = ctx.resource;
      if (style) body.style = style;
      await sse(`${base}/ask`, body, (ev) => {
        if (ev.type === 'delta') {
          text += ev.text || '';
          ent.status.style.display = 'none';
          if (!raf) raf = requestAnimationFrame(paint);
          onDelta && onDelta(ev.text || '');
        } else if (ev.type === 'status') {
          ent.status.style.display = '';
          ent.status.textContent = ev.text;
          onStatus && onStatus(ev.text);
        } else if (ev.type === 'start') {
          ent.status.style.display = '';
          ent.status.textContent = `${ENGINE_LABEL[ev.engine] || ev.engine} 正在思考…`;
        } else if (ev.type === 'done') {
          saved = ev.qa || null;
        } else if (ev.type === 'error') {
          errMsg = ev.message || '回答失败';
        }
      });
      if (errMsg) throw new Error(errMsg);
      if (raf) cancelAnimationFrame(raf);
      const qa = saved || { ...tmp, answer: text };
      paper.qa.push(qa);
      Object.assign(tmp, qa);
      ent.el.dataset.id = qa.id || '';
      ent.ans.classList.remove('streaming');
      setMarkdown(ent.ans, qa.answer || text);
      ent.status.style.display = 'none';
      ent.fillFoot();
      updateCounts();
      return qa;
    } catch (e) {
      if (raf) cancelAnimationFrame(raf);
      ent.ans.classList.remove('streaming');
      if (text) setMarkdown(ent.ans, text);
      ent.status.style.display = '';
      ent.status.innerHTML = `<span class="err-text">${escapeHtml(e.message || String(e))}</span>`;
      toastError(e);
      return null;
    } finally {
      busy = false;
      sendBtn.disabled = false;
    }
  }

  // =============== 翻译 ===============
  const trTools = h('div', { class: 'panel-tools' });
  const trBody = h('div', { class: 'panel-body' });
  const trPageSlot = h('div', { class: 'panel-pad', style: { paddingBottom: '0' } });
  const trList = h('div', { class: 'panel-pad', style: { paddingTop: '6px' } });
  trBody.append(trPageSlot, trList);
  views.translate.append(trTools, trBody);
  const pageTrCache = new Map();
  const pageBtn = h('button', { class: 'btn btn-sm', onclick: () => translatePage(ctx.currentPage()) }, icon('file', 14), '翻译当前页');
  trTools.append(pageBtn, h('span', { class: 'spacer' }), h('span', { class: 'faint', style: { fontSize: '12px' } }, '选中文字 → 翻译；顶栏可切换翻译引擎'));
  if (STATIC) trTools.style.display = 'none';

  function trItem(t) {
    return h('div', { class: 'tr-item' },
      h('div', { class: 'tr-src' }, t.source),
      h('div', { class: 'tr-dst' }, t.translation || h('span', { class: 'faint' }, '翻译中…')),
      t.translation ? h('div', { class: 'tr-foot' },
        resBadge(ridOf(t), () => goToAnchor({ page: t.page, quote: t.source, resource_id: ridOf(t) }), !t.page),
        t.page ? h('button', { class: 'pbadge', onclick: () => goToAnchor({ page: t.page, quote: t.source, resource_id: ridOf(t) }) }, `p.${t.page}`) : null,
        h('span', { style: { marginLeft: '4px' } }, `${ENGINE_LABEL[t.engine] || t.engine || ''} · ${fmtTime(t.at)}`),
        h('span', { class: 'spacer' }),
        speakButton(() => t.source, null, '朗读原文'),
        speakButton(() => t.translation, 'zh', '朗读译文'),
        STATIC ? null : h('button', { class: 'lnk', onclick: async () => {
          try {
            await ctx.addNote({ text: t.translation, quote: t.source, page: t.page || null, source: 'translation', resource_id: ridOf(t) });
            toast('已收进笔记', 'ok', 1400);
          } catch (e) { toastError(e); }
        } }, '收进笔记'),
        h('button', { class: 'icon-btn', title: '复制译文', onclick: () => copyText(t.translation) }, icon('copy', 13))) : null);
  }

  function renderTranslate() {
    trList.replaceChildren();
    if (!paper.translations.length && !trPageSlot.firstChild) {
      trList.append(h('div', { class: 'empty' }, STATIC ? '没有翻译记录。' : '选中文字 → 「翻译」，或点「翻译当前页」做段落对照（网页 / README 从当前位置起翻译若干段）。'));
      return;
    }
    if (paper.translations.length) trList.append(h('div', { class: 'sub-title' }, '划词翻译', h('span', { class: 'faint' }, paper.translations.length)));
    for (const t of paper.translations.slice().sort((a, b) => (b.at || '').localeCompare(a.at || ''))) trList.append(trItem(t));
  }

  async function translateText(text, page, { speak = false } = {}) {
    switchTab('translate');
    const empty = trList.querySelector('.empty');
    if (empty) empty.remove();
    const pending = trItem({ source: text });
    trList.prepend(pending);
    trBody.scrollTop = 0;
    try {
      const pg = ctx.storePage ? ctx.storePage(page) : (page || null);
      const r = await post('/translate', { text, engine: ctx.tengine, target: ctx.settings.target_lang || 'zh-CN', pid: ctx.pid, iid: ctx.iid, page: pg, resource_id: ctx.resource || 'main' });
      const t = { source: text, translation: r.translation, engine: r.engine || ctx.tengine, page: pg, resource_id: ctx.resource || 'main', at: new Date().toISOString(), ...(r.item || {}) };
      paper.translations.push(t);
      pending.replaceWith(trItem(t));
      updateCounts();
      if (speak && r.translation) {
        const { tts } = await import('./tts.js');
        tts.speak(r.translation, 'zh');
      }
      return r.translation;
    } catch (e) {
      pending.remove();
      toastError(e);
      return null;
    }
  }

  async function translatePage(n) {
    switchTab('translate');
    const art = ctx.isArticleView && ctx.isArticleView();
    const key = art ? `${ctx.resource}|${Date.now()}|${ctx.tengine}` : `${ctx.resource || 'main'}|${n}|${ctx.tengine}`;
    const box = h('div', { class: 'tr-page' });
    const r0 = ctx.resourceById ? ctx.resourceById(ctx.resource) : null;
    const where = art ? `${r0 ? resLabel(r0) : '网页'} · 当前位置起` : `第 ${n} 页`;
    const head = h('div', { class: 'tr-page-head' }, icon('file', 13), `${where} · ${ENGINE_LABEL[ctx.tengine] || ctx.tengine}`, h('span', { class: 'spacer' }),
      h('button', { class: 'icon-btn', title: '关闭', onclick: () => { box.remove(); if (!trPageSlot.firstChild) renderTranslate(); } }, icon('x', 13)));
    box.append(head);
    trPageSlot.prepend(box);
    trBody.scrollTop = 0;
    const empty = trList.querySelector('.empty');
    if (empty) empty.remove();
    let paras = pageTrCache.get(key);
    if (!paras) {
      const src = await ctx.pdf.getPageParagraphs(n);
      paras = src.map((s) => ({ source: s, translation: null }));
      pageTrCache.set(key, paras);
    }
    if (!paras.length) {
      box.append(h('div', { class: 'faint' }, art ? '当前位置没有可翻译的段落。' : '这一页没有可提取的文字（可能是扫描版或图片）。'));
      return;
    }
    pageBtn.disabled = true;
    try {
      for (const p of paras) {
        const dst = h('div', { class: 'tr-dst' }, p.translation || h('span', { class: 'faint' }, '翻译中…'));
        const row = h('div', { class: 'tr-para' }, h('div', { class: 'tr-src' }, p.source), dst);
        box.append(row);
        if (!p.translation) {
          try {
            const r = await post('/translate', { text: p.source, engine: ctx.tengine, target: ctx.settings.target_lang || 'zh-CN' });
            p.translation = r.translation;
          } catch (e) {
            dst.replaceChildren(h('span', { class: 'err-text' }, e.message));
            continue;
          }
        }
        dst.replaceChildren(p.translation);
        row.append(h('div', { class: 'tr-foot' }, h('span', { class: 'spacer' }),
          speakButton(() => p.translation, 'zh', '朗读译文'),
          h('button', { class: 'lnk', onclick: () => ctx.addNote({ text: p.translation, quote: p.source, page: art ? null : n, source: 'translation' }).then(() => toast('已收进笔记', 'ok', 1400), toastError) }, '收进笔记')));
      }
    } finally {
      pageBtn.disabled = false;
    }
  }

  // =============== 生词 ===============
  const vTools = h('div', { class: 'panel-tools' });
  const vBody = h('div', { class: 'panel-body' });
  const vList = h('div', { class: 'panel-pad', style: { paddingTop: '4px' } });
  vBody.append(vList);
  views.vocab.append(vTools, vBody);
  const vOnlyNew = h('input', { type: 'checkbox' });
  vOnlyNew.onchange = () => renderVocab();
  vTools.append(h('label', { class: 'check' }, vOnlyNew, '只看未掌握'), h('span', { class: 'spacer' }),
    h('a', { class: 'btn btn-sm btn-ghost', href: appHref('/vocab'), 'data-link': '' }, icon('book', 14), '打开生词本'));

  function renderVocab() {
    vList.replaceChildren();
    let list = paper.vocab.slice().sort((a, b) => (b.created || '').localeCompare(a.created || ''));
    if (vOnlyNew.checked) list = list.filter((v) => !v.mastered);
    if (!list.length) {
      vList.append(h('div', { class: 'empty', style: { marginTop: '8px' } }, paper.vocab.length ? '没有未掌握的生词了 🎉' : STATIC ? '这篇论文没有生词。' : '双击文中的单词（或选中 ≤6 个词的短语）→ 「生词」，自动查释义并记下原句。'));
      return;
    }
    for (const v of list) vList.append(vocabCard(v, {
      onJump: () => goToAnchor({ annotation_id: v.annotation_id, page: v.page, quote: v.word, resource_id: ridOf(v) }),
      source: resBadge(ridOf(v), () => goToAnchor({ annotation_id: v.annotation_id, page: v.page, quote: v.word, resource_id: ridOf(v) }), !v.page),
      readonly: STATIC,
      onToggle: async (m) => {
        try {
          Object.assign(v, await patch(`${base}/vocab/${encodeURIComponent(v.id)}`, { mastered: m }));
          renderVocab();
        } catch (e) { toastError(e); }
      },
      onDelete: () => deleteVocab(v),
    }));
  }

  async function deleteVocab(v) {
    try {
      await del(`${base}/vocab/${encodeURIComponent(v.id)}`);
      paper.vocab = paper.vocab.filter((x) => x.id !== v.id);
      ctx.paper.vocab = paper.vocab;
      if (v.annotation_id) {
        paper.annotations = paper.annotations.filter((a) => a.id !== v.annotation_id);
        ctx.paper.annotations = paper.annotations;
        ctx.pdf.removeAnnotation(v.annotation_id);
      }
      refresh('vocab');
      updateCounts();
    } catch (e) { toastError(e); }
  }

  function focusVocab(id) {
    if (!id) return;
    setTimeout(() => {
      const el = vList.querySelector(`.vcard[data-id="${CSS.escape(id)}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        el.style.borderColor = 'var(--vocab)';
        setTimeout(() => { el.style.borderColor = ''; }, 1600);
      }
    }, 30);
  }

  async function findContext(page, quote) {
    try {
      const raw = await ctx.pdf.getPageText(page);
      const text = raw.replace(/-\n(?=[a-z])/g, '').replace(/\s+/g, ' ');
      const q = quote.replace(/\s+/g, ' ').toLowerCase();
      for (const s of splitSentences(text, { merge: false })) {
        if (s.text.toLowerCase().includes(q)) return s.text.length > 260 ? windowAround(s.text, q) : s.text;
      }
      const i = text.toLowerCase().indexOf(q);
      if (i >= 0) return windowAround(text, q);
    } catch { /* ignore */ }
    return quote;
  }

  function windowAround(text, q) {
    const i = text.toLowerCase().indexOf(q);
    if (i < 0) return text.slice(0, 200);
    const a = Math.max(0, i - 110);
    const b = Math.min(text.length, i + q.length + 110);
    return `${a > 0 ? '…' : ''}${text.slice(a, b).trim()}${b < text.length ? '…' : ''}`;
  }

  async function addVocabFromSelection(info) {
    const word = info.quote.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '').trim();
    const nWords = word.split(/\s+/).filter(Boolean).length;
    if (!word) return;
    if (nWords > 6 || word.length > 60) {
      toast('生词最多 6 个词，选短一点', 'info');
      return;
    }
    let ann = null;
    try {
      let anchor = info.anchor || null;
      if (anchor && anchor.exact !== word) {
        // 生词只锚定单词本身（去掉选区两端的标点/空格）
        const k = anchor.exact.indexOf(word);
        if (k >= 0) {
          const full = (anchor.prefix || '') + anchor.exact + (anchor.suffix || '');
          const off = (anchor.prefix || '').length + k;
          anchor = { type: 'text', exact: word, prefix: full.slice(Math.max(0, off - 32), off), suffix: full.slice(off + word.length, off + word.length + 32),
            start: Number.isInteger(anchor.start) ? anchor.start + k : undefined, end: Number.isInteger(anchor.start) ? anchor.start + k + word.length : undefined };
        }
      }
      ann = await ctx.createAnnotation({ kind: 'vocab', color: 'purple', page: info.page, quote: word, rects: info.rects, anchor });
      ctx.pdf.clearSelection();
      const context = await findContext(info.page, word);
      toast(`正在查「${word}」…`, 'info', 1200);
      const r = await post(`${base}/vocab`, { word, context, page: ctx.storePage ? ctx.storePage(info.page) : info.page, annotation_id: ann.id, resource_id: ctx.resource || 'main' });
      const v = r.entry || r;
      upsertVocab(v);
      switchTab('vocab');
      focusVocab(v.id);
      if (v.meaning) toast(`${v.word}：${truncate(v.meaning, 60)}`, 'ok', 3200);
      else if (v.lookup_error) toast(`已记下「${v.word}」，但查词失败：${v.lookup_error}`, 'error');
      return v;
    } catch (e) {
      if (ann) ctx.deleteAnnotation(ann.id).catch(() => {});
      toastError(e);
      return null;
    }
  }

  async function addVocabWord(word, { context, page } = {}) {
    try {
      const pg = ctx.storePage ? ctx.storePage(page || ctx.currentPage()) : (page || ctx.currentPage());
      const r = await post(`${base}/vocab`, { word, context: context || word, page: pg, annotation_id: null, resource_id: ctx.resource || 'main' });
      const v = r.entry || r;
      upsertVocab(v);
      refresh('vocab');
      return v;
    } catch (e) {
      toastError(e);
      return null;
    }
  }

  function upsertVocab(v) {
    const i = paper.vocab.findIndex((x) => x.id === v.id);
    if (i >= 0) paper.vocab[i] = v;
    else paper.vocab.push(v);
    rendered.vocab = false;
    if (active === 'vocab') refresh('vocab');
    updateCounts();
  }

  // =============== 总结 ===============
  const sBody = h('div', { class: 'panel-body' });
  const sPad = h('div', { class: 'panel-pad' });
  sBody.append(sPad);
  views.summary.append(sBody);
  let archiving = false;

  function renderSummary() {
    if (archiving) return;
    sPad.replaceChildren();
    if (paper.summary && paper.summary.markdown) {
      const gh = githubFileUrl(mdPath());
      const md = h('div', { class: 'md' });
      setMarkdown(md, paper.summary.markdown);
      sPad.append(
        h('div', { class: 'summary-meta' },
          h('span', {}, `${ENGINE_LABEL[paper.summary.engine] || paper.summary.engine || ''} · ${fmtTime(paper.summary.at)}`),
          h('span', { class: 'spacer' }),
          speakButton(() => paper.summary.markdown, 'zh', '朗读总结'),
          gh ? h('a', { class: 'btn btn-sm btn-ghost', href: gh, target: '_blank' }, icon('github', 13), 'GitHub') : null,
          h('a', { class: 'btn btn-sm btn-ghost', href: markdownUrl(base, ctx.pid, paper.paper_key), target: '_blank' }, icon('markdown', 13), 'MD'),
          STATIC ? null : h('button', { class: 'btn btn-sm', onclick: () => archive() }, icon('refresh', 13), '重新归档')),
        md);
    } else if (STATIC) {
      sPad.append(h('div', { class: 'empty' }, '这篇论文还没有归档总结。'));
    } else {
      sPad.append(h('div', { class: 'archive-hint' },
        h('p', {}, '读完后点「读完，归档笔记」：AI 会结合论文全文、你的高亮/笔记/问答，生成一份结构化中文总结（一句话总结、问题与动机、方法、关键结果、你的笔记要点、局限与疑问……），'),
        h('p', {}, '总结会写进这篇论文的 Markdown 笔记文件，并自动同步到 GitHub。'),
        h('button', { class: 'btn btn-primary', onclick: () => archive() }, icon('archive', 14), '读完，归档笔记')));
    }
  }

  async function archive() {
    if (archiving) return;
    switchTab('summary');
    archiving = true;
    const md = h('div', { class: 'md qa-a streaming' });
    const status = h('div', { class: 'qa-status' }, `${ENGINE_LABEL[ctx.engine]} 正在整理你的笔记和问答…`);
    sPad.replaceChildren(status, md);
    let text = '';
    let done = null;
    let errMsg = null;
    try {
      await sse(`${base}/archive`, { engine: ctx.engine }, (ev) => {
        if (ev.type === 'delta') {
          text += ev.text || '';
          setMarkdown(md, text);
          md.classList.add('streaming');
        } else if (ev.type === 'status') status.textContent = ev.text;
        else if (ev.type === 'done') done = ev;
        else if (ev.type === 'error') errMsg = ev.message;
      });
      if (errMsg) throw new Error(errMsg);
      paper.summary = (done && done.summary) || { markdown: text, engine: ctx.engine, at: new Date().toISOString() };
      paper.status = 'done';
      toast(`已归档：${(done && done.md_path) || mdPath()}`, 'ok', 3500);
      document.dispatchEvent(new CustomEvent('pd:projects-changed'));
      setTimeout(() => refreshSync(), 1500);
    } catch (e) {
      toastError(e);
      archiving = false;
      sPad.prepend(h('div', { class: 'err-text', style: { marginBottom: '8px' } }, `归档失败：${e.message}`));
      md.classList.remove('streaming');
      return;
    }
    archiving = false;
    renderSummary();
    updateCounts();
  }

  // 初始
  switchTab(active);
  updateCounts();

  return {
    switchTab,
    refresh,
    newNote,
    focusNote,
    setQuote,
    ask,
    translateText,
    translatePage,
    addVocabFromSelection,
    addVocabWord,
    deleteVocab,
    focusVocab,
    archive,
    qa: { textarea: qaTa, micSlot, voiceSlot, submit: submitQa, setQuote, getQuote: () => qaQuote },
    get active() { return active; },
    destroy() { document.removeEventListener('pd:sync', renderNotesFoot); },
  };
}

function plainText(md) {
  return String(md || '').replace(/<[^>]+>/g, '').replace(/(\*\*|__|\*|_|~~|`)/g, '').replace(/\$([^$]+)\$/g, '$1').replace(/\s+/g, ' ').trim();
}

/** 生词卡片（阅读页与生词本共用） */
export function vocabCard(v, { onJump, onToggle, onDelete, source, readonly = false } = {}) {
  const ctxEl = h('div', { class: 'v-ctx' });
  if (v.context) {
    const word = (v.word || '').trim();
    // 只加粗完整的词（skim 不会把 Skimmer 的前半截加粗）
    const esc = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = word ? new RegExp(`(?<![\\p{L}\\p{N}])(${esc})(?![\\p{L}\\p{N}])`, 'iu') : null;
    const parts = re ? v.context.split(re) : [v.context];
    for (const p of parts) ctxEl.append(word && p.toLowerCase() === word.toLowerCase() ? h('b', {}, p) : p);
  }
  const chk = h('input', { type: 'checkbox' });
  chk.checked = !!v.mastered;
  chk.onchange = () => onToggle && onToggle(chk.checked);
  return h('div', { class: `vcard ${v.mastered ? 'mastered' : ''}`, dataset: { id: v.id } },
    h('div', { class: 'v-head' },
      h('span', { class: 'v-word' }, v.word),
      v.phonetic ? h('span', { class: 'v-phon' }, v.phonetic) : null,
      v.pos ? h('span', { class: 'v-pos' }, v.pos) : null,
      speakButton(() => v.word, 'en', '朗读单词'),
      h('span', { class: 'acts' },
        readonly ? (v.mastered ? h('span', { class: 'badge done' }, '已掌握') : null) : h('label', { class: 'check', title: '已掌握' }, chk, h('span', { style: { fontSize: '12px' } }, '已掌握')),
        onDelete && !readonly ? h('button', { class: 'icon-btn', title: '删除', onclick: () => onDelete() }, icon('trash', 13)) : null)),
    v.meaning ? h('div', { class: 'v-mean' }, v.meaning) : null,
    v.context_meaning ? h('div', { class: 'v-cmean' }, `语境义：${v.context_meaning}`) : null,
    v.context ? ctxEl : null,
    v.context_translation ? h('div', { class: 'v-ctxtr' }, v.context_translation) : null,
    v.lookup_error ? h('div', { class: 'v-err' }, `查词失败：${v.lookup_error}`) : null,
    h('div', { class: 'v-foot' },
      v.page ? h('button', { class: 'pbadge', title: '跳到原文', onclick: () => onJump && onJump() }, `p.${v.page}`) : null,
      source || null,
      h('span', { class: 'spacer' }),
      v.context ? speakButton(() => v.context, 'en', '朗读原句') : null,
      h('span', {}, fmtTime(v.created))));
}
