// 首页：全部项目 + 快速添加到收件箱
import { post, STATIC, appHref } from './api.js';
import { h, icon, toast, toastError } from './ui.js';
import { getProjects, refreshProjects, newProject, navigate } from '../app.js';

export async function mountHome(root) {
  const page = h('div', { class: 'page' });
  root.append(page);

  const input = h('input', { type: 'text', placeholder: '粘贴 arXiv ID / PDF / 博客 / GitHub 链接（可一次粘贴多个：论文 + 代码 + 主页），回车加入收件箱' });
  const addBtn = h('button', { class: 'btn btn-primary btn-sm' }, '加入收件箱');
  const add = async () => {
    const v = input.value.trim();
    if (!v) return input.focus();
    addBtn.disabled = true;
    try {
      const it = await post('/projects/inbox/items', { input: v });
      input.value = '';
      await refreshProjects();
      toast(it.existing ? '收件箱里已经有这篇了' : `已加入收件箱：${it.title || v}`, 'ok');
      if (it.type === 'paper' || it.type === 'web') navigate(`/r/inbox/${encodeURIComponent(it.id)}`);
    } catch (e) {
      toastError(e);
    } finally {
      addBtn.disabled = false;
    }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  addBtn.onclick = add;

  const grid = h('div', { class: 'proj-grid' });
  const render = () => {
    grid.replaceChildren();
    const ps = getProjects().slice().sort((a, b) => (a.id === 'inbox' ? -1 : b.id === 'inbox' ? 1 : (b.updated || '').localeCompare(a.updated || '')));
    for (const p of ps) {
      const c = p.counts || {};
      grid.append(h('a', { class: 'proj-tile', href: appHref(`/p/${encodeURIComponent(p.id)}`), 'data-link': '' },
        h('div', { class: 'pt-name' }, p.name),
        h('div', { class: 'pt-desc' }, p.description || ''),
        h('div', { class: 'pt-counts' }, `${c.paper || 0} 篇论文 · ${(c.web || 0) + (c.link || 0)} 个网页 · ${c.note || 0} 条笔记`)));
    }
    if (!STATIC) grid.append(h('button', { class: 'proj-tile', style: { textAlign: 'left', border: '1px dashed var(--border-2)', background: 'transparent' }, onclick: () => newProject() },
      h('div', { class: 'pt-name', style: { display: 'flex', alignItems: 'center', gap: '6px' } }, icon('plus', 16), '新建项目'),
      h('div', { class: 'pt-desc' }, '为一个研究方向建一个文件夹')));
  };
  render();
  const onChange = () => render();
  document.addEventListener('pd:projects-rendered', onChange);

  page.append(
    h('div', { class: 'page-head' }, h('div', { style: { flex: 1 } },
      h('h1', { class: 'page-title' }, '全部项目'),
      h('div', { class: 'page-desc' }, STATIC ? '这是 PaperDesk 文献库的只读在线版：可以浏览项目、论文、笔记、标注、问答和生词；编辑请在本地 PaperDesk 中进行。' : '在浏览器里读论文：不下载 PDF，边读边翻译、边问 Claude Code / Codex、随手记笔记，读完一键归档到 GitHub。'))),
    STATIC ? null : h('div', { class: 'addbar' }, icon('link', 16), input, addBtn),
    grid,
  );
  refreshProjects().then(render);
  return { destroy() { document.removeEventListener('pd:projects-rendered', onChange); } };
}
