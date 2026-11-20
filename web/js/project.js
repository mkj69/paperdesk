// 项目页：添加栏 + 论文 / 链接 / 笔记分组卡片
import { get, post, patch, del, readerUrl, STATIC, appHref } from './api.js';
import { h, icon, toast, toastError, confirmDialog, promptDialog, popMenu, copyText, fmtTime, STATUS_LABEL, truncate } from './ui.js';
import { setMarkdown } from './markdown.js';
import { createEditor } from './editor.js';
import { refreshProjects, getProjects, navigate } from '../app.js';
import { resChip, KIND_OPTIONS, kindMeta, hostOf, classifyUrl } from './resources.js';

export async function mountProject(root, pid) {
  const page = h('div', { class: 'page' });
  root.append(page);
  let project;
  try {
    project = await get(`/projects/${encodeURIComponent(pid)}`);
  } catch (e) {
    page.append(h('div', { class: 'empty' }, e.status === 404 ? '这个项目不存在（可能已删除）' : `加载失败：${e.message}`));
    return {};
  }
  const P = encodeURIComponent(pid);

  // ---------- 头部 ----------
  const title = h('h1', { class: 'page-title', contenteditable: STATIC ? null : 'true', spellcheck: 'false', title: STATIC ? null : '点击修改名称' }, project.name);
  const desc = h('div', { class: 'page-desc', contenteditable: STATIC ? null : 'true', 'data-placeholder': STATIC ? '' : '添加描述…', spellcheck: 'false' }, project.description || '');
  const saveField = async (field, el) => {
    const v = el.textContent.trim();
    if (field === 'name' && !v) { el.textContent = project.name; return; }
    if (v === (project[field] || '')) return;
    try {
      project = { ...project, ...(await patch(`/projects/${P}`, { [field]: v })) };
      refreshProjects();
    } catch (e) {
      toastError(e);
    }
  };
  for (const [f, el] of STATIC ? [] : [['name', title], ['description', desc]]) {
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); el.blur(); }
      if (e.key === 'Escape') { el.textContent = project[f] || ''; el.blur(); }
    });
    el.addEventListener('blur', () => saveField(f, el));
  }
  const moreBtn = h('button', { class: 'icon-btn', title: '更多' }, icon('dots', 16));
  moreBtn.onclick = () => popMenu(moreBtn, [
    { label: '在 Finder 中打开', icon: 'folder', onClick: () => post('/open_folder', { pid }).catch(toastError) },
    { label: '重命名', icon: 'edit', onClick: () => { title.focus(); document.getSelection().selectAllChildren(title); } },
    ...(pid !== 'inbox' ? ['-', { label: '删除项目', icon: 'trash', danger: true, onClick: deleteProject }] : []),
  ]);
  async function deleteProject() {
    if (!(await confirmDialog(`删除项目「${project.name}」？项目文件夹会移到数据目录的 .trash 中，不会立即永久删除。`, { danger: true, okText: '删除' }))) return;
    try {
      await del(`/projects/${P}`);
      await refreshProjects();
      navigate('/');
    } catch (e) {
      toastError(e);
    }
  }

  // ---------- 添加栏 ----------
  const input = h('input', { type: 'text', placeholder: '粘贴 arXiv / PDF / 博客 / GitHub 链接，回车添加（多个链接一起粘贴 = 同一篇工作）' });
  const addBtn = h('button', { class: 'btn btn-primary btn-sm' }, '添加');
  const noteBtn = h('button', { class: 'btn btn-sm' }, icon('note', 14), '写一条笔记');
  const add = async () => {
    const v = input.value.trim();
    if (!v) return input.focus();
    addBtn.disabled = true;
    addBtn.textContent = '解析中…';
    try {
      const it = await post(`/projects/${P}/items`, { input: v });
      input.value = '';
      if (it.existing) toast('这个项目里已经有它了', 'info');
      else toast(`已添加：${truncate(it.title || v, 40)}`, 'ok');
      await reload();
      refreshProjects();
    } catch (e) {
      toastError(e);
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = '添加';
    }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  addBtn.onclick = add;

  const editorSlot = h('div');
  noteBtn.onclick = () => {
    if (editorSlot.firstChild) { editorSlot.replaceChildren(); return; }
    const ed = createEditor({
      placeholder: '项目层面的想法、待读清单、问题……支持 Markdown',
      saveLabel: '添加笔记',
      onCancel: () => editorSlot.replaceChildren(),
      onSave: async (text) => {
        const firstLine = text.split('\n')[0].replace(/^[#>*\-\s]+/, '').slice(0, 60);
        await post(`/projects/${P}/items`, { type: 'note', note: text, title: firstLine || '笔记' });
        editorSlot.replaceChildren();
        await reload();
        refreshProjects();
      },
    });
    editorSlot.append(ed.el);
    ed.focus();
  };

  const listEl = h('div');
  page.append(
    h('div', { class: 'page-head' }, h('div', { style: { flex: 1, minWidth: 0 } }, title, desc), STATIC ? null : h('div', { class: 'page-actions' },
      h('button', { class: 'btn btn-sm', onclick: () => post('/open_folder', { pid }).catch(toastError) }, icon('folder', 14), '在 Finder 中打开'),
      moreBtn)),
    STATIC ? null : h('div', { class: 'addbar' }, icon('plus', 16), input, noteBtn, addBtn),
    editorSlot,
    listEl,
  );

  async function reload() {
    try {
      project = await get(`/projects/${P}`);
    } catch (e) {
      toastError(e);
    }
    renderList();
  }

  function renderList() {
    const items = (project.items || []).slice().sort((a, b) => (b.added || '').localeCompare(a.added || ''));
    const papers = items.filter((i) => i.type === 'paper');
    const links = items.filter((i) => i.type === 'web' || i.type === 'link');
    const notes = items.filter((i) => i.type === 'note');
    listEl.replaceChildren();
    if (!items.length) {
      listEl.append(h('div', { class: 'empty' }, '这个项目还是空的。在上面粘贴一个 arXiv 链接试试，比如 1706.03762'));
      return;
    }
    if (papers.length) {
      listEl.append(h('div', { class: 'section-title' }, icon('file', 14), '论文', h('span', { class: 'count' }, papers.length)));
      listEl.append(h('div', { class: 'cards' }, papers.map(paperCard)));
    }
    if (links.length) {
      listEl.append(h('div', { class: 'section-title' }, icon('link', 14), '网页 / 博客 / 代码', h('span', { class: 'count' }, links.length)));
      listEl.append(h('div', { class: 'cards' }, links.map(webCard)));
    }
    if (notes.length) {
      listEl.append(h('div', { class: 'section-title' }, icon('note', 14), '笔记', h('span', { class: 'count' }, notes.length)));
      listEl.append(h('div', { class: 'cards' }, notes.map(noteCard)));
    }
  }

  const readPath = (it) => `/r/${P}/${encodeURIComponent(it.id)}`;
  const readHref = (it) => appHref(readPath(it));

  function statusBadge(it) {
    const st = it.status || 'unread';
    const b = h('span', { class: `badge ${st} ${STATIC ? '' : 'clickable'}`, title: STATIC ? null : '点击修改状态' }, STATUS_LABEL[st] || st);
    if (!STATIC) b.onclick = (e) => {
      e.stopPropagation();
      popMenu(b, ['unread', 'reading', 'done'].map((s) => ({
        label: STATUS_LABEL[s],
        onClick: async () => {
          try {
            await patch(`/projects/${P}/items/${encodeURIComponent(it.id)}`, { status: s });
            await reload();
          } catch (err) { toastError(err); }
        },
      })));
    };
    return b;
  }

  function itemMenu(btn, it) {
    const others = getProjects().filter((p) => p.id !== pid);
    const readable = it.type === 'paper' || it.type === 'web' || it.type === 'link';
    popMenu(btn, [
      ...(readable ? [
        { label: '阅读', icon: 'book', onClick: () => navigate(readPath(it)) },
        { label: '复制阅读链接', icon: 'copy', onClick: () => copyText(readerUrl(pid, it.id)) },
        ...(STATIC ? [] : [{ label: '关联资源（代码 / 主页 / 博客…）', icon: 'plus', onClick: () => addResource(it) }]),
      ] : []),
      ...(it.url ? [{ label: '打开原文链接', icon: 'external', onClick: () => window.open(it.url, '_blank') }] : []),
      ...(STATIC ? [] : [
      { label: '修改标题', icon: 'edit', onClick: () => editTitle(it) },
      { label: '编辑标签', icon: 'pencil', onClick: () => editTags(it) },
      { label: '移动到…', icon: 'move', onClick: () => moveItem(it, others) },
      '-',
      { label: '删除', icon: 'trash', danger: true, onClick: () => removeItem(it) }]),
    ]);
  }

  async function editTitle(it) {
    const r = await promptDialog({ title: '修改标题', fields: [{ name: 't', label: '标题', value: it.title || '' }] });
    if (!r || !r.t.trim()) return;
    try { await patch(`/projects/${P}/items/${encodeURIComponent(it.id)}`, { title: r.t.trim() }); await reload(); } catch (e) { toastError(e); }
  }

  async function editTags(it) {
    const r = await promptDialog({ title: '编辑标签', fields: [{ name: 't', label: '标签（用逗号分隔）', value: (it.tags || []).join(', '), placeholder: 'transformer, 必读' }] });
    if (!r) return;
    const tags = r.t.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    try { await patch(`/projects/${P}/items/${encodeURIComponent(it.id)}`, { tags }); await reload(); } catch (e) { toastError(e); }
  }

  async function moveItem(it, others) {
    if (!others.length) {
      toast('还没有其他项目，先新建一个', 'info');
      return;
    }
    const r = await promptDialog({
      title: '移动到项目',
      fields: [{ name: 'to', label: '目标项目', type: 'select', options: others.map((p) => ({ value: p.id, label: p.name })), value: others[0].id }],
      okText: '移动',
    });
    if (!r) return;
    try {
      await post(`/projects/${P}/items/${encodeURIComponent(it.id)}/move`, { to: r.to });
      toast('已移动', 'ok');
      await reload();
      refreshProjects();
    } catch (e) { toastError(e); }
  }

  async function removeItem(it) {
    const extra = it.type === 'paper' || it.type === 'web' ? '它的笔记文件会移到 .trash。' : '';
    if (!(await confirmDialog(`删除「${truncate(it.title || it.url || '笔记', 50)}」？${extra}`, { danger: true, okText: '删除' }))) return;
    try {
      await del(`/projects/${P}/items/${encodeURIComponent(it.id)}`);
      await reload();
      refreshProjects();
    } catch (e) { toastError(e); }
  }

  async function addResource(it) {
    const r = await promptDialog({
      title: `关联资源 · ${truncate(it.title || it.url || '', 40)}`,
      fields: [
        { name: 'url', label: '链接', placeholder: 'https://github.com/…  或项目主页 / 博客 / 视频 / arXiv', hint: '同一篇工作的论文、代码、主页、博客、视频都可以存在一起，阅读页顶部可以切换' },
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
      const res = await post(`/projects/${P}/items/${encodeURIComponent(it.id)}/resources`, body);
      const entry = res.resource || res;
      toast(`已关联：${kindMeta(entry.kind).label} · ${hostOf(entry.url)}`, 'ok');
      await reload();
    } catch (e) { toastError(e); }
  }

  function resRow(it) {
    const rs = (it.resources || []).filter((r) => r && r.url);
    if (!rs.length && STATIC) return null;
    return h('div', { class: 'res-chips' },
      rs.map((r) => resChip(r)),
      STATIC ? null : h('button', { class: 'res-chip add', 'data-action': 'add-resource', title: '关联代码 / 主页 / 博客 / 视频…', onclick: (e) => { e.stopPropagation(); addResource(it); } }, icon('plus', 12), rs.length ? '' : '关联资源'));
  }

  function paperCard(it) {
    const authors = it.authors && it.authors.length ? (it.authors.length > 3 ? `${it.authors.slice(0, 3).join(', ')} 等` : it.authors.join(', ')) : '';
    const more = h('button', { class: 'icon-btn', title: '更多' }, icon('dots', 16));
    more.onclick = (e) => { e.stopPropagation(); itemMenu(more, it); };
    return h('div', { class: 'card paper-card', dataset: { itemType: 'paper' } },
      h('div', { class: 'pc-main' },
        h('a', { class: 'pc-title', href: readHref(it), 'data-link': '' }, it.title || it.url),
        h('div', { class: 'pc-meta' },
          statusBadge(it),
          authors ? h('span', {}, authors) : null,
          it.year ? h('span', {}, it.year) : null,
          it.arxiv_id ? h('span', { class: 'faint' }, `arXiv:${it.arxiv_id}`) : null,
          (it.tags || []).map((t) => h('span', { class: 'tag' }, t)),
          h('span', { class: 'faint' }, `添加于 ${fmtTime(it.added)}`)),
        resRow(it)),
      h('div', { class: 'pc-actions' },
        h('a', { class: 'btn btn-sm', href: readHref(it), 'data-link': '' }, icon('book', 14), '阅读'),
        h('button', { class: 'icon-btn', title: '复制阅读链接', onclick: () => copyText(readerUrl(pid, it.id)) }, icon('link', 15)),
        more));
  }

  function webCard(it) {
    const host = hostOf(it.url);
    const kind = it.kind || classifyUrl(it.url || '');
    const km = kindMeta(kind);
    const more = h('button', { class: 'icon-btn', title: '更多' }, icon('dots', 16));
    more.onclick = (e) => { e.stopPropagation(); itemMenu(more, it); };
    const desc = it.description || it.abstract || '';
    return h('div', { class: 'card web-card', dataset: { itemType: 'web' } },
      h('div', { class: 'wc-ic', title: km.label }, km.icon),
      h('div', { class: 'pc-main' },
        h('a', { class: 'pc-title', href: readHref(it), 'data-link': '' }, it.title || it.url),
        h('div', { class: 'pc-meta' },
          it.type === 'link' ? null : statusBadge(it),
          h('span', { class: 'kind-tag' }, km.label),
          h('span', {}, it.site_name || host),
          (it.tags || []).map((t) => h('span', { class: 'tag' }, t)),
          h('span', { class: 'faint' }, `添加于 ${fmtTime(it.added)}`)),
        desc ? h('div', { class: 'wc-desc' }, truncate(desc, 180)) : null,
        resRow(it)),
      h('div', { class: 'pc-actions' },
        h('a', { class: 'btn btn-sm', href: readHref(it), 'data-link': '' }, icon('book', 14), '阅读'),
        h('a', { class: 'icon-btn', title: '在新标签页打开原网页', href: it.url, target: '_blank', rel: 'noopener' }, icon('external', 15)),
        h('button', { class: 'icon-btn', title: '复制阅读链接', onclick: () => copyText(readerUrl(pid, it.id)) }, icon('link', 15)),
        STATIC ? null : more));
  }

  function noteCard(it) {
    const body = h('div');
    setMarkdown(body, it.note || it.title || '');
    const card = h('div', { class: 'card note-card' });
    const more = h('button', { class: 'icon-btn', title: '更多' }, icon('dots', 16));
    more.onclick = () => popMenu(more, [
      { label: '编辑', icon: 'edit', onClick: edit },
      { label: '移动到…', icon: 'move', onClick: () => moveItem(it, getProjects().filter((p) => p.id !== pid)) },
      '-',
      { label: '删除', icon: 'trash', danger: true, onClick: () => removeItem(it) },
    ]);
    function edit() {
      const ed = createEditor({
        value: it.note || '',
        onCancel: () => card.replaceChildren(body, foot),
        onSave: async (text) => {
          await patch(`/projects/${P}/items/${encodeURIComponent(it.id)}`, { note: text, title: text.split('\n')[0].replace(/^[#>*\-\s]+/, '').slice(0, 60) || '笔记' });
          await reload();
        },
      });
      card.replaceChildren(ed.el);
      ed.focus();
    }
    const foot = h('div', { class: 'nc-foot' }, fmtTime(it.updated || it.added), h('span', { class: 'spacer' }),
      STATIC ? null : h('button', { class: 'icon-btn', title: '编辑', onclick: edit }, icon('edit', 14)), STATIC ? null : more);
    if (!STATIC) body.addEventListener('dblclick', edit);
    card.append(body, foot);
    return card;
  }

  renderList();
  return {};
}
