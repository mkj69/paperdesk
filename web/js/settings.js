// 设置页：AI 引擎 / 模型 / 翻译 / 查词 / 应用地址 / CLI 路径 / 同步
import { get, put, loadHealth, cache } from './api.js';
import { h, icon, toast, toastError } from './ui.js';
import { openSyncPanel, refreshSync, syncDotState } from './sync.js';

export async function mountSettings(root) {
  const page = h('div', { class: 'page' });
  root.append(page);
  let settings;
  let health;
  try {
    [settings, health] = await Promise.all([get('/settings'), loadHealth(true)]);
  } catch (e) {
    page.append(h('div', { class: 'empty' }, `读取设置失败：${e.message}`));
    return {};
  }

  const fields = {};
  const sel = (name, options) => {
    const el = h('select', {}, options.map(([v, l]) => h('option', { value: v }, l)));
    el.value = settings[name] ?? options[0][0];
    fields[name] = el;
    return el;
  };
  const txt = (name, placeholder = '') => {
    const el = h('input', { type: 'text', placeholder });
    el.value = settings[name] ?? '';
    fields[name] = el;
    return el;
  };
  const num = (name, min, max) => {
    const el = h('input', { type: 'number', min: String(min), max: String(max) });
    el.value = settings[name] ?? '';
    fields[name] = el;
    return el;
  };
  const chk = (name) => {
    const el = h('input', { type: 'checkbox' });
    el.checked = !!settings[name];
    fields[name] = el;
    return el;
  };
  const field = (label, el, hint) => h('label', { class: 'field' }, h('span', { class: 'field-label' }, label), el, hint ? h('span', { class: 'field-hint' }, hint) : null);
  const cliRow = (name, info) => h('div', { class: 'kv' },
    h('span', { class: 'k' }, name),
    info && info.found ? h('span', { class: 'ok-text' }, '已找到 ', h('code', {}, info.path || '')) : h('span', { class: 'err-text' }, '未找到'));

  const syncInfo = h('div', { class: 'kv' });
  const renderSync = () => {
    const s = cache.sync;
    const st = syncDotState(s);
    syncInfo.replaceChildren(
      h('span', { class: 'k' }, '状态'), h('span', {}, st.label || '—'),
      h('span', { class: 'k' }, '远程仓库'), h('span', {}, (s && s.remote) || '未连接'),
      s && s.pages_url ? h('span', { class: 'k' }, '在线只读版') : null,
      s && s.pages_url ? h('a', { href: s.pages_url, target: '_blank' }, s.pages_url) : null);
  };
  refreshSync().then(renderSync);

  const saveBtn = h('button', { class: 'btn btn-primary' }, '保存设置');
  saveBtn.onclick = async () => {
    const out = {};
    for (const [k, el] of Object.entries(fields)) {
      if (el.type === 'checkbox') out[k] = el.checked;
      else if (el.type === 'number') out[k] = el.value === '' ? null : Number(el.value);
      else out[k] = el.value.trim();
    }
    saveBtn.disabled = true;
    try {
      cache.settings = await put('/settings', out);
      toast('设置已保存', 'ok');
    } catch (e) {
      toastError(e);
    } finally {
      saveBtn.disabled = false;
    }
  };

  page.append(
    h('div', { class: 'page-head' }, h('div', { style: { flex: 1 } }, h('h1', { class: 'page-title' }, '设置'),
      h('div', { class: 'page-desc' }, `数据目录：${health.data_dir || '—'} · 版本 ${health.version || ''}`))),
    h('div', { class: 'settings-card' }, h('h3', {}, '本机命令行工具'),
      cliRow('claude', health.claude), cliRow('codex', health.codex), cliRow('git', health.git), cliRow('gh', health.gh),
      h('div', { class: 'field-hint', style: { marginTop: '8px' } }, '找不到时在下面填完整路径（终端里运行 which claude / which codex 查看）。')),
    h('div', { class: 'settings-card' }, h('h3', {}, 'AI 问答'),
      h('div', { class: 'settings-grid' },
        field('默认提问引擎', sel('default_engine', [['claude', 'Claude Code'], ['codex', 'Codex']])),
        field('回答语言', txt('answer_lang', '中文')),
        field('Claude 模型（留空用默认）', txt('claude_model', 'sonnet / opus')),
        field('Codex 模型（留空用默认）', txt('codex_model', '')),
        field('claude 路径（可选）', txt('claude_path', '/opt/homebrew/bin/claude')),
        field('codex 路径（可选）', txt('codex_path', '/opt/homebrew/bin/codex')))),
    h('div', { class: 'settings-card' }, h('h3', {}, '翻译与生词'),
      h('div', { class: 'settings-grid' },
        field('默认翻译引擎', sel('translate_engine', [['google', 'Google 翻译（免费、快）'], ['claude', 'Claude'], ['codex', 'Codex']])),
        field('目标语言', sel('target_lang', [['zh-CN', '简体中文'], ['zh-TW', '繁體中文'], ['en', 'English'], ['ja', '日本語']])),
        field('Claude 翻译模型', txt('claude_translate_model', 'haiku'), '翻译用便宜快的模型就够了'),
        field('查词引擎', sel('vocab_engine', [['google', 'Google（词性 + 释义 + 音标）'], ['claude', 'Claude'], ['codex', 'Codex']])))),
    h('div', { class: 'settings-card' }, h('h3', {}, '链接与同步'),
      h('div', { class: 'settings-grid' },
        field('本地应用地址 app_url', txt('app_url', 'http://localhost:8765'), '笔记里「在 PaperDesk 中打开」链接用这个地址'),
        field('在线只读版地址 pages_url', txt('pages_url', 'https://你的用户名.github.io/paper-library/'), '留空则由 GitHub 远程地址自动推导'),
        field('自动同步延迟（秒）', num('sync_debounce_sec', 5, 3600)),
        h('label', { class: 'check', style: { alignSelf: 'end', paddingBottom: '8px' } }, chk('auto_sync'), '改动后自动 commit + push')),
      h('div', { style: { marginTop: '12px' } }, syncInfo),
      h('div', { style: { marginTop: '10px' } }, h('button', { class: 'btn', onclick: () => openSyncPanel().then(() => {}) }, icon('github', 14), '打开同步面板'))),
    h('div', { style: { display: 'flex', justifyContent: 'flex-end' } }, saveBtn),
  );
  const off = () => renderSync();
  document.addEventListener('pd:sync', off);
  return { destroy() { document.removeEventListener('pd:sync', off); } };
}
