// GitHub 同步：状态点 + 同步面板（SPEC §4 Git 同步、§10 公开/私有）
import { get, post, cache, loadHealth, githubWebBase } from './api.js';
import { h, icon, openModal, toast, toastError, fmtTime } from './ui.js';

export async function refreshSync() {
  try {
    cache.sync = await get('/sync/status');
  } catch {
    cache.sync = null;
  }
  document.dispatchEvent(new CustomEvent('pd:sync'));
  return cache.sync;
}

/** 绿=已同步 / 黄=待同步 / 红=错误 / 灰=未连接 */
export function syncDotState(s) {
  if (!s) return { cls: '', label: '', title: '同步状态未知' };
  if (!s.enabled) return { cls: '', label: '未启用', title: '未检测到 git，同步不可用' };
  if (s.last_error) return { cls: 'error', label: '同步出错', title: `同步出错：${s.last_error}` };
  if (!s.remote) return { cls: '', label: '未连接', title: '尚未连接 GitHub 仓库，点击设置' };
  if (s.pending || s.dirty || (s.ahead || 0) > 0) return { cls: 'pending', label: '待同步', title: '有改动等待推送到 GitHub' };
  return { cls: 'ok', label: '已同步', title: `已同步到 ${s.remote}${s.last_sync ? '（' + fmtTime(s.last_sync) + '）' : ''}` };
}

export function githubFileUrl(path) {
  const base = githubWebBase(cache.sync && cache.sync.remote);
  if (!base) return null;
  const branch = (cache.sync && cache.sync.branch) || 'main';
  return `${base}/blob/${branch}/${path.split('/').map(encodeURIComponent).join('/')}`;
}

export async function openSyncPanel() {
  const body = h('div', { class: 'form' });
  const m = openModal({ title: 'GitHub 同步', content: body, width: 520 });
  const render = async () => {
    const [s, health] = await Promise.all([refreshSync(), loadHealth(true).catch(() => cache.health)]);
    body.replaceChildren();
    if (!s) {
      body.append(h('div', { class: 'empty' }, '读取同步状态失败'));
      return;
    }
    const st = syncDotState(s);
    const web = githubWebBase(s.remote);
    body.append(h('div', { class: 'kv' },
      h('span', { class: 'k' }, '状态'), h('span', { class: st.cls === 'error' ? 'err-text' : st.cls === 'ok' ? 'ok-text' : '' }, st.label || '—'),
      h('span', { class: 'k' }, '数据目录'), h('code', {}, (health && health.data_dir) || '—'),
      h('span', { class: 'k' }, '远程仓库'), s.remote ? (web ? h('a', { href: web, target: '_blank' }, s.remote) : h('code', {}, s.remote)) : h('span', { class: 'faint' }, '未连接'),
      h('span', { class: 'k' }, '分支'), h('span', {}, s.branch || '—'),
      h('span', { class: 'k' }, '未推送提交'), h('span', {}, String(s.ahead ?? 0)),
      h('span', { class: 'k' }, '上次同步'), h('span', {}, s.last_sync ? fmtTime(s.last_sync) : '—'),
      s.pages_url ? h('span', { class: 'k' }, '在线只读版') : null,
      s.pages_url ? h('a', { href: s.pages_url, target: '_blank' }, s.pages_url) : null,
      s.last_error ? h('span', { class: 'k' }, '错误') : null,
      s.last_error ? h('span', { class: 'err-text', style: { whiteSpace: 'pre-wrap', fontSize: '12px' } }, s.last_error) : null,
    ));
    if (s.enabled && s.remote) {
      const btn = h('button', { class: 'btn btn-accent' }, icon('refresh', 14), '立即同步');
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          cache.sync = await post('/sync/now', {});
          document.dispatchEvent(new CustomEvent('pd:sync'));
          toast(cache.sync.last_error ? '同步出错' : '已同步', cache.sync.last_error ? 'error' : 'ok');
          render();
        } catch (e) {
          toastError(e);
        } finally {
          btn.disabled = false;
        }
      };
      body.append(h('div', { style: { display: 'flex', gap: '8px', marginTop: '6px' } }, btn,
        web ? h('a', { class: 'btn', href: web, target: '_blank' }, icon('github', 14), '在 GitHub 打开') : null));
    }
    if (s.enabled && web) {
      const pagesBtn = h('button', { class: 'btn btn-sm' }, icon('cloud', 14), '一键开启 GitHub Pages');
      pagesBtn.onclick = async () => {
        pagesBtn.disabled = true;
        try {
          cache.sync = await post('/sync/enable_pages', {});
          document.dispatchEvent(new CustomEvent('pd:sync'));
          toast('已请求开启 GitHub Pages，几分钟后在线只读版就能访问', 'ok', 4000);
          render();
        } catch (e) {
          toastError(e);
        } finally {
          pagesBtn.disabled = false;
        }
      };
      body.append(h('div', { class: 'sub-title' }, '在线只读版（GitHub Pages）'),
        h('div', { class: 'field-hint' }, '每次同步都会把只读阅读器（index.html + static/ + library.json）一起推到仓库。首次需要开启 Pages：仓库 Settings → Pages → Source 选「Deploy from a branch」，分支 main、目录 / (root)。公开仓库免费可用。'),
        h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
          health && health.gh && health.gh.found ? pagesBtn : null,
          h('a', { class: 'btn btn-sm btn-ghost', href: `${web}/settings/pages`, target: '_blank' }, icon('external', 13), '打开 Pages 设置')));
    }
    if (!s.enabled) {
      body.append(h('p', { class: 'muted' }, '没有检测到 git。macOS 上在终端运行 xcode-select --install 安装后重启 PaperDesk。'));
      return;
    }
    // 设置远程
    body.append(h('div', { class: 'sub-title' }, s.remote ? '更换远程仓库' : '连接 GitHub 仓库'));
    if (health && health.gh && health.gh.found) {
      const name = h('input', { type: 'text', value: 'paper-library', placeholder: '仓库名' });
      const pub = h('input', { type: 'radio', name: 'vis', value: 'public', checked: true });
      const pri = h('input', { type: 'radio', name: 'vis', value: 'private' });
      const createBtn = h('button', { class: 'btn btn-primary' }, icon('github', 14), '用 gh 创建仓库并推送');
      createBtn.onclick = async () => {
        if (!name.value.trim()) return name.focus();
        createBtn.disabled = true;
        createBtn.lastChild.textContent = '创建中…';
        try {
          cache.sync = await post('/sync/setup', { create: name.value.trim(), private: pri.checked });
          document.dispatchEvent(new CustomEvent('pd:sync'));
          toast('仓库已创建并推送', 'ok');
          render();
        } catch (e) {
          toastError(e);
        } finally {
          createBtn.disabled = false;
          createBtn.lastChild.textContent = '用 gh 创建仓库并推送';
        }
      };
      body.append(
        h('label', { class: 'field' }, h('span', { class: 'field-label' }, '新仓库名（在你的 GitHub 账号下）'), name),
        h('div', { class: 'radio-row' },
          h('label', { class: 'check' }, pub, '公开'),
          h('label', { class: 'check' }, pri, '私有')),
        h('div', {}, createBtn),
        h('div', { class: 'field-hint' }, '需要本机已执行过 gh auth login。仓库里只有你的笔记和条目列表，不含 PDF 与论文全文。'),
        h('div', { class: 'sub-title' }, '或填写已有仓库地址'));
    }
    const url = h('input', { type: 'text', placeholder: 'git@github.com:你的用户名/paper-library.git 或 https://github.com/…', value: s.remote || '' });
    const setBtn = h('button', { class: 'btn' }, '连接并推送');
    setBtn.onclick = async () => {
      if (!url.value.trim()) return url.focus();
      setBtn.disabled = true;
      try {
        cache.sync = await post('/sync/setup', { remote_url: url.value.trim() });
        document.dispatchEvent(new CustomEvent('pd:sync'));
        toast('已连接远程仓库', 'ok');
        render();
      } catch (e) {
        toastError(e);
      } finally {
        setBtn.disabled = false;
      }
    };
    body.append(h('div', { style: { display: 'flex', gap: '8px' } }, url, setBtn),
      h('div', { class: 'field-hint' }, '先在 GitHub 上新建一个空仓库，再把地址粘贴到这里。之后每次改动都会自动 commit 并 push。'));
  };
  try {
    await render();
  } catch (e) {
    toastError(e);
  }
  return m;
}
