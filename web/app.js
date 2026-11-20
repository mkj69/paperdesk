// PaperDesk 前端入口：路由（History API）+ 左侧栏
import { get, loadSettings, loadHealth, cache, STATIC, appHref, currentRoute, localUrl } from './js/api.js';
import { h, icon, toastError, promptDialog, toast } from './js/ui.js';
import { openSyncPanel, syncDotState, refreshSync } from './js/sync.js';

const main = document.getElementById('main');
const sidebar = document.getElementById('sidebar');

let currentView = null; // { destroy?() }
let projects = [];

export function navigate(path, { replace = false } = {}) {
  if (path === currentRoute()) return;
  if (currentView && currentView.beforeLeave && currentView.beforeLeave() === false) return;
  if (STATIC) {
    if (replace) location.replace(`#${path}`);
    else location.hash = path; // 触发 hashchange → route()
    return;
  }
  if (replace) history.replaceState({}, '', path);
  else history.pushState({}, '', path);
  route();
}

if (STATIC) window.addEventListener('hashchange', () => route());
else window.addEventListener('popstate', () => route());
document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[data-link]');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  const href = a.getAttribute('href');
  navigate(href.startsWith('#') ? href.slice(1) : href);
});

// 本地模式下也接受 hash 形式的链接（例如从在线只读版复制过来的 #/r/..）
if (!STATIC && location.hash.startsWith('#/')) history.replaceState({}, '', location.hash.slice(1));

async function route() {
  const full = currentRoute();
  const qIdx = full.indexOf('?');
  const path = qIdx >= 0 ? full.slice(0, qIdx) : full;
  const search = qIdx >= 0 ? full.slice(qIdx) : '';
  if (currentView && currentView.destroy) {
    try { currentView.destroy(); } catch (e) { console.warn(e); }
  }
  currentView = null;
  document.body.classList.remove('reader-mode', 'sidebar-peek');
  main.innerHTML = '';
  main.scrollTop = 0;
  let m;
  try {
    if ((m = path.match(/^\/r\/([^/]+)\/([^/]+)\/?$/))) {
      const { mountReader } = await import('./js/reader.js');
      document.body.classList.add('reader-mode');
      const params = new URLSearchParams(search);
      currentView = await mountReader(main, {
        pid: decodeURIComponent(m[1]),
        iid: decodeURIComponent(m[2]),
        page: params.get('page') ? +params.get('page') : null,
        anno: params.get('anno'),
      });
    } else if ((m = path.match(/^\/p\/([^/]+)\/?$/))) {
      const { mountProject } = await import('./js/project.js');
      currentView = await mountProject(main, decodeURIComponent(m[1]));
    } else if (path === '/settings' && STATIC) {
      navigate('/', { replace: true });
      return;
    } else if (path === '/settings') {
      const { mountSettings } = await import('./js/settings.js');
      currentView = await mountSettings(main);
    } else if (path === '/vocab') {
      const { mountVocab } = await import('./js/vocab.js');
      currentView = await mountVocab(main);
    } else {
      const { mountHome } = await import('./js/home.js');
      currentView = await mountHome(main);
    }
  } catch (e) {
    toastError(e);
    main.append(h('div', { class: 'page' }, h('div', { class: 'empty' }, `页面加载失败：${e.message}`)));
  }
  if (STATIC) showStaticBanner(path + search);
  renderSidebar();
}

function showStaticBanner(path) {
  document.querySelectorAll('.static-banner').forEach((x) => x.remove());
  const bar = h('div', { class: 'static-banner' },
    h('span', {}, '只读在线版 · 编辑请在本地 PaperDesk 中打开'),
    h('a', { href: localUrl(path), target: '_blank', rel: 'noopener' }, '在本地 PaperDesk 中打开 →'));
  document.body.classList.add('static-mode');
  document.body.prepend(bar);
}

// ---------------- 左侧栏 ----------------
export async function refreshProjects() {
  try {
    projects = await get('/projects');
  } catch (e) {
    toastError(e);
  }
  renderSidebar();
  return projects;
}

export function getProjects() {
  return projects;
}

const syncBtn = h('button', { class: 'sync-dot', title: 'GitHub 同步', onclick: () => openSyncPanel() }, h('i'), h('span', { class: 'sync-label' }, ''));

function renderSidebar() {
  const full = currentRoute();
  const path = full.split('?')[0];
  const activePid = (path.match(/^\/(?:p|r)\/([^/]+)/) || [])[1];
  const list = h('div', { class: 'sb-list' });
  const sorted = projects.slice().sort((a, b) => (a.id === 'inbox' ? -1 : b.id === 'inbox' ? 1 : 0));
  for (const p of sorted) {
    const c = p.counts || {};
    const n = (c.paper || 0) + (c.link || 0) + (c.note || 0);
    list.append(h('a', {
      class: `sb-item ${decodeURIComponent(activePid || '') === p.id ? 'active' : ''}`,
      href: appHref(`/p/${encodeURIComponent(p.id)}`),
      'data-link': '',
      title: p.description || p.name,
    }, icon(p.id === 'inbox' ? 'inbox' : 'folder', 16), h('span', { class: 'name' }, p.name), h('span', { class: 'count' }, n || '')));
  }
  if (!projects.length) list.append(h('div', { class: 'faint', style: { padding: '8px', fontSize: '12.5px' } }, '还没有项目'));
  sidebar.replaceChildren(
    h('div', { class: 'sb-head' },
      h('a', { class: 'sb-logo', href: appHref('/'), 'data-link': '' }, h('span', { class: 'sb-mark' }, 'P'), 'PaperDesk'),
      STATIC ? h('span', { class: 'badge', style: { marginLeft: 'auto' } }, '只读') : syncBtn),
    h('div', { class: 'sb-section' },
      h('a', { class: `sb-item ${path === '/' ? 'active' : ''}`, href: appHref('/'), 'data-link': '' }, icon('home', 16), h('span', { class: 'name' }, '全部项目')),
      h('a', { class: `sb-item ${path === '/vocab' ? 'active' : ''}`, href: appHref('/vocab'), 'data-link': '' }, icon('book', 16), h('span', { class: 'name' }, '生词本'))),
    h('div', { class: 'sb-label' }, '项目',
      STATIC ? null : h('button', { class: 'icon-btn', title: '新建项目', onclick: () => newProject() }, icon('plus', 14))),
    list,
    STATIC ? null : h('div', { class: 'sb-foot' },
      h('a', { class: `sb-item ${path === '/settings' ? 'active' : ''}`, href: appHref('/settings'), 'data-link': '' }, icon('settings', 16), h('span', { class: 'name' }, '设置'))),
  );
  updateSyncDot();
}

export async function newProject() {
  const r = await promptDialog({
    title: '新建项目',
    fields: [
      { name: 'name', label: '项目名称', placeholder: '例如：Cognitive Core 调研' },
      { name: 'description', label: '描述（可选）', type: 'textarea', rows: 2, placeholder: '这个项目要回答什么问题？' },
    ],
    okText: '创建',
  });
  if (!r || !r.name.trim()) return;
  try {
    const { post } = await import('./js/api.js');
    const p = await post('/projects', { name: r.name.trim(), description: r.description.trim() });
    await refreshProjects();
    navigate(`/p/${encodeURIComponent(p.id)}`);
    toast('项目已创建', 'ok');
  } catch (e) {
    toastError(e);
  }
}

function updateSyncDot() {
  const st = syncDotState(cache.sync);
  syncBtn.className = `sync-dot ${st.cls}`;
  syncBtn.title = st.title;
  syncBtn.querySelector('.sync-label').textContent = st.label;
}

document.addEventListener('pd:sync', updateSyncDot);
document.addEventListener('pd:projects-changed', () => refreshProjects());

// 阅读模式下，鼠标贴左边缘时临时显示侧栏
document.addEventListener('mousemove', (e) => {
  if (!document.body.classList.contains('reader-mode')) return;
  if (e.clientX < 6) document.body.classList.add('sidebar-peek');
  else if (e.clientX > 260 && document.body.classList.contains('sidebar-peek')) document.body.classList.remove('sidebar-peek');
});

// ---------------- 启动 ----------------
(async function boot() {
  try {
    await Promise.all([loadSettings(), loadHealth().catch(() => null), refreshProjects()]);
  } catch (e) {
    toastError(e);
  }
  route();
  if (STATIC) return;
  refreshSync().then(updateSyncDot).catch(() => {});
  setInterval(() => refreshSync().then(updateSyncDot).catch(() => {}), 15000);
})();
