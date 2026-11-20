// 通用 UI 工具：元素构造、toast、模态框、复制、时间格式化

// 让 append / prepend / replaceChildren 忽略 null / undefined / false（方便写条件子元素）
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const m of ['append', 'prepend', 'replaceChildren']) {
    const orig = proto[m];
    if (!orig || orig.__pdSafe) continue;
    const safe = function (...args) {
      return orig.apply(this, args.filter((a) => a !== null && a !== undefined && a !== false));
    };
    safe.__pdSafe = true;
    proto[m] = safe;
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.append(c);
    else el.append(document.createTextNode(String(c)));
  }
}

/** Tabler 风格的极简内联图标（自绘 SVG，避免外部依赖） */
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  folder: '<path d="M4 5h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/>',
  inbox: '<path d="M4 13h4l1.5 3h5L16 13h4"/><path d="M4 13l2.5-7h11L20 13v6H4z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/>',
  move: '<path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20"/>',
  external: '<path d="M12 6H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/><path d="M11 13l9-9M15 4h5v5"/>',
  book: '<path d="M3 19a9 9 0 0 1 9 0 9 9 0 0 1 9 0V6a9 9 0 0 0-9 0 9 9 0 0 0-9 0z"/><path d="M12 6v13"/>',
  note: '<path d="M5 4h10l4 4v12H5z"/><path d="M14 4v5h5M9 13h6M9 17h4"/>',
  message: '<path d="M4 18V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8z"/><path d="M9 9h6M9 12h4"/>',
  language: '<path d="M4 5h7M9 3v2c0 4.4-2.2 8-5 9"/><path d="M5 9c0 2.1 2.9 3.9 6.4 4"/><path d="M12 20l4-9 4 9M19.1 18h-6.2"/>',
  sparkles: '<path d="M16 18a2 2 0 0 1 2 2 2 2 0 0 1 2-2 2 2 0 0 1-2-2 2 2 0 0 1-2 2zM16 6a2 2 0 0 1 2 2 2 2 0 0 1 2-2 2 2 0 0 1-2-2 2 2 0 0 1-2 2zM9 18a6 6 0 0 1 6-6 6 6 0 0 1-6-6 6 6 0 0 1-6 6 6 6 0 0 1 6 6z"/>',
  bulb: '<path d="M9 16a5 5 0 1 1 6 0c-.6.5-1 1.2-1 2v1h-4v-1c0-.8-.4-1.5-1-2z"/><path d="M10 22h4"/>',
  pencil: '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-4-4L4 16v4z"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>',
  refresh: '<path d="M20 11A8 8 0 0 0 4.6 8M4 4v4h4"/><path d="M4 13a8 8 0 0 0 15.4 3M20 20v-4h-4"/>',
  zoomIn: '<circle cx="10" cy="10" r="7"/><path d="M21 21l-6-6M7 10h6M10 7v6"/>',
  zoomOut: '<circle cx="10" cy="10" r="7"/><path d="M21 21l-6-6M7 10h6"/>',
  fit: '<path d="M4 8V4h4M20 8V4h-4M4 16v4h4M20 16v4h-4"/>',
  github: '<path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>',
  markdown: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15V9l2 2 2-2v6M14 13l2 2 2-2M16 9v6"/>',
  send: '<path d="M10 14L21 3M21 3l-6.5 18a.6.6 0 0 1-1 0L10 14l-7-3.5a.6.6 0 0 1 0-1z"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8M10 12h4"/>',
  quote: '<path d="M10 11H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v6c0 2.7-1.3 4.3-4 5M19 11h-4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v6c0 2.7-1.3 4.3-4 5"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M5 6v.01M5 12v.01M5 18v.01"/>',
  code: '<path d="M7 8l-4 4 4 4M17 8l4 4-4 4M14 4l-4 16"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  cloud: '<path d="M7 18a4.6 4.4 0 0 1 0-9 5 4.5 0 0 1 11 2h1a3.5 3.5 0 0 1 0 7z"/>',
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  chat: '<path d="M3 20l1.3-3.9A9 8 0 1 1 7.7 19L3 20"/>',
  file: '<path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/>',
  home: '<path d="M5 12H3l9-9 9 9h-2M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/>',
};

export function icon(name, size = 16) {
  const span = document.createElement('span');
  span.className = 'ic';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
  return span;
}

// ---------------- toast ----------------
export function toast(msg, type = 'error', ms) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = h('div', { class: `toast toast-${type}` }, msg);
  box.append(el);
  requestAnimationFrame(() => el.classList.add('show'));
  const t = ms ?? (type === 'error' ? 5200 : 2400);
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, t);
}

export function toastError(e) {
  console.warn(e);
  toast(e && e.message ? e.message : String(e), 'error');
}

// ---------------- modal ----------------
export function openModal({ title, content, actions = [], width = 460, onClose } = {}) {
  const overlay = h('div', { class: 'modal-overlay' });
  const box = h('div', { class: 'modal', style: { width: `min(${width}px, calc(100vw - 32px))` }, role: 'dialog' });
  const close = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    onClose && onClose();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
  };
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) close();
  });
  const head = h('div', { class: 'modal-head' },
    h('div', { class: 'modal-title' }, title || ''),
    h('button', { class: 'icon-btn', title: '关闭', onclick: close }, icon('x')));
  const body = h('div', { class: 'modal-body' }, content);
  const foot = actions.length ? h('div', { class: 'modal-foot' }) : null;
  for (const a of actions) {
    const b = h('button', { class: `btn ${a.primary ? 'btn-primary' : ''} ${a.danger ? 'btn-danger' : ''}` }, a.label);
    b.addEventListener('click', async () => {
      if (a.onClick) {
        b.disabled = true;
        try {
          const r = await a.onClick();
          if (r !== false) close();
        } catch (e) {
          toastError(e);
        } finally {
          b.disabled = false;
        }
      } else close();
    });
    foot.append(b);
  }
  box.append(head, body);
  if (foot) box.append(foot);
  overlay.append(box);
  document.body.append(overlay);
  const first = box.querySelector('input, textarea, select');
  if (first) setTimeout(() => first.focus(), 30);
  return { close, el: box, body };
}

export function confirmDialog(message, { okText = '确定', danger = false, title = '请确认' } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const m = openModal({
      title,
      content: h('p', { class: 'modal-text' }, message),
      actions: [
        { label: '取消', onClick: () => { done = true; resolve(false); } },
        { label: okText, primary: !danger, danger, onClick: () => { done = true; resolve(true); } },
      ],
      onClose: () => { if (!done) resolve(false); },
    });
    return m;
  });
}

/** fields: [{name,label,value,placeholder,type:'text'|'textarea'|'select',options:[{value,label}]}] */
export function promptDialog({ title, fields, okText = '确定', width } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const inputs = {};
    const form = h('form', { class: 'form', onsubmit: (e) => e.preventDefault() });
    for (const f of fields) {
      let input;
      if (f.type === 'textarea') {
        input = h('textarea', { rows: f.rows || 3, placeholder: f.placeholder || '' });
        input.value = f.value || '';
      } else if (f.type === 'select') {
        input = h('select', {}, (f.options || []).map((o) => h('option', { value: o.value }, o.label)));
        if (f.value !== undefined) input.value = f.value;
      } else {
        input = h('input', { type: 'text', placeholder: f.placeholder || '' });
        input.value = f.value || '';
      }
      inputs[f.name] = input;
      form.append(h('label', { class: 'field' }, h('span', { class: 'field-label' }, f.label), input,
        f.hint ? h('span', { class: 'field-hint' }, f.hint) : null));
    }
    const collect = () => {
      const out = {};
      for (const [k, el] of Object.entries(inputs)) out[k] = el.value;
      return out;
    };
    const m = openModal({
      title,
      width,
      content: form,
      actions: [
        { label: '取消', onClick: () => { done = true; resolve(null); } },
        { label: okText, primary: true, onClick: () => { done = true; resolve(collect()); } },
      ],
      onClose: () => { if (!done) resolve(null); },
    });
    form.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT') {
        e.preventDefault();
        done = true;
        resolve(collect());
        m.close();
      }
    });
  });
}

// ---------------- misc ----------------
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast('已复制', 'ok', 1400);
}

export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400 && now.getDate() === d.getDate()) return `今天 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const y = d.getFullYear() === now.getFullYear() ? '' : `${d.getFullYear()}-`;
  return `${y}${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  d.flush = (...args) => {
    clearTimeout(t);
    fn(...args);
  };
  return d;
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function truncate(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

export const STATUS_LABEL = { unread: '未读', reading: '在读', done: '已读' };
export const ENGINE_LABEL = { claude: 'Claude Code', codex: 'Codex', google: 'Google 翻译' };

/** 一个小菜单（popover），挂在 anchor 旁边 */
export function popMenu(anchor, items) {
  closePopMenus();
  const menu = h('div', { class: 'popmenu' });
  for (const it of items) {
    if (it === '-') {
      menu.append(h('div', { class: 'popmenu-sep' }));
      continue;
    }
    menu.append(h('button', {
      class: `popmenu-item ${it.danger ? 'danger' : ''}`,
      onclick: (e) => {
        e.stopPropagation();
        closePopMenus();
        it.onClick && it.onClick();
      },
    }, it.icon ? icon(it.icon, 14) : null, it.label));
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - mw - 8);
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 4;
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  setTimeout(() => document.addEventListener('mousedown', outside), 0);
  function outside(e) {
    if (!menu.contains(e.target)) closePopMenus();
  }
  menu._cleanup = () => document.removeEventListener('mousedown', outside);
  return menu;
}

export function closePopMenus() {
  document.querySelectorAll('.popmenu').forEach((m) => {
    m._cleanup && m._cleanup();
    m.remove();
  });
}
