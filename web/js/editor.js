// Markdown 笔记编辑器：工具栏（B / I / U / code / ∑ / 列表 / 引用）+ 预览 + 快捷键
import { h, icon } from './ui.js';
import { setMarkdown } from './markdown.js';

function insertText(ta, text, selStart, selEnd) {
  ta.focus();
  // execCommand 能保留撤销栈；失败则退回 setRangeText
  const ok = document.execCommand && document.execCommand('insertText', false, text);
  if (!ok) ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
  if (selStart !== undefined) {
    ta.selectionStart = selStart;
    ta.selectionEnd = selEnd ?? selStart;
  }
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

export function wrapSelection(ta, before, after = before, placeholder = '文字') {
  const s = ta.selectionStart;
  const e = ta.selectionEnd;
  const v = ta.value;
  const sel = v.slice(s, e);
  // 已经被包裹 → 取消包裹
  if (sel && v.slice(s - before.length, s) === before && v.slice(e, e + after.length) === after) {
    ta.selectionStart = s - before.length;
    ta.selectionEnd = e + after.length;
    insertText(ta, sel, s - before.length, s - before.length + sel.length);
    return;
  }
  const inner = sel || placeholder;
  ta.selectionStart = s;
  ta.selectionEnd = e;
  insertText(ta, before + inner + after, s + before.length, s + before.length + inner.length);
}

export function prefixLines(ta, prefix) {
  const v = ta.value;
  let s = ta.selectionStart;
  let e = ta.selectionEnd;
  s = v.lastIndexOf('\n', s - 1) + 1;
  const nl = v.indexOf('\n', e);
  e = nl === -1 ? v.length : nl;
  const lines = v.slice(s, e).split('\n');
  const all = lines.every((l) => l.startsWith(prefix));
  const out = lines.map((l) => (all ? l.slice(prefix.length) : prefix + l)).join('\n');
  ta.selectionStart = s;
  ta.selectionEnd = e;
  insertText(ta, out, s, s + out.length);
}

/**
 * @param {object} o
 * @param {string} [o.value]
 * @param {string} [o.placeholder]
 * @param {string} [o.saveLabel]
 * @param {(text:string)=>Promise<any>|any} o.onSave
 * @param {()=>void} [o.onCancel]
 * @param {number} [o.rows]
 */
export function createEditor({ value = '', placeholder = '写点什么…支持 Markdown 与 $公式$', saveLabel = '保存', onSave, onCancel, rows = 5, quote } = {}) {
  const ta = h('textarea', { class: 'editor-ta', rows, placeholder, spellcheck: 'false' });
  ta.value = value;
  const preview = h('div', { class: 'editor-preview md', style: { display: 'none' } });
  let previewing = false;

  const btn = (label, title, fn, cls = '') => h('button', {
    type: 'button',
    class: `ed-btn ${cls}`,
    title,
    onmousedown: (e) => e.preventDefault(),
    onclick: () => { if (!previewing) fn(); },
  }, label);

  const previewBtn = h('button', {
    type: 'button',
    class: 'ed-btn ed-preview-btn',
    title: '预览',
    onmousedown: (e) => e.preventDefault(),
    onclick: () => togglePreview(),
  }, icon('eye', 14), h('span', {}, '预览'));

  const toolbar = h('div', { class: 'editor-toolbar' },
    btn(h('b', {}, 'B'), '加粗 (⌘B)', () => wrapSelection(ta, '**')),
    btn(h('i', {}, 'I'), '斜体 (⌘I)', () => wrapSelection(ta, '*')),
    btn(h('u', {}, 'U'), '下划线 (⌘U)', () => wrapSelection(ta, '<ins>', '</ins>')),
    btn(h('s', {}, 'S'), '删除线', () => wrapSelection(ta, '~~')),
    btn(h('mark', {}, 'H'), '标记', () => wrapSelection(ta, '<mark>', '</mark>')),
    h('span', { class: 'ed-sep' }),
    btn(icon('code', 14), '行内代码', () => wrapSelection(ta, '`', '`', 'code')),
    btn(h('span', { class: 'ed-sigma' }, '∑'), '公式 $…$', () => wrapSelection(ta, '$', '$', 'x^2')),
    btn(icon('list', 14), '列表', () => prefixLines(ta, '- ')),
    btn(icon('quote', 14), '引用', () => prefixLines(ta, '> ')),
    h('span', { class: 'ed-spacer' }),
    previewBtn,
  );

  function togglePreview() {
    previewing = !previewing;
    if (previewing) {
      setMarkdown(preview, ta.value || '*（空）*');
      preview.style.minHeight = `${Math.max(ta.offsetHeight, 80)}px`;
      preview.style.display = '';
      ta.style.display = 'none';
      previewBtn.classList.add('active');
    } else {
      preview.style.display = 'none';
      ta.style.display = '';
      previewBtn.classList.remove('active');
      ta.focus();
    }
  }

  const saveBtn = h('button', { type: 'button', 'data-action': 'save-note', class: 'btn btn-primary btn-sm', onclick: () => save() }, saveLabel);
  const cancelBtn = onCancel ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => onCancel() }, '取消') : null;

  async function save() {
    const text = ta.value.trim();
    if (!text) {
      ta.focus();
      ta.classList.add('shake');
      setTimeout(() => ta.classList.remove('shake'), 400);
      return;
    }
    saveBtn.disabled = true;
    try {
      await onSave(text);
    } finally {
      saveBtn.disabled = false;
    }
  }

  ta.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key === 'Enter') {
      e.preventDefault();
      save();
    } else if (mod && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      wrapSelection(ta, '**');
    } else if (mod && (e.key === 'i' || e.key === 'I')) {
      e.preventDefault();
      wrapSelection(ta, '*');
    } else if (mod && (e.key === 'u' || e.key === 'U')) {
      e.preventDefault();
      wrapSelection(ta, '<ins>', '</ins>');
    } else if (e.key === 'Escape' && onCancel) {
      e.preventDefault();
      onCancel();
    }
  });
  // 自动增高
  const autosize = () => {
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight + 2, rows * 20), 420)}px`;
  };
  ta.addEventListener('input', autosize);

  const el = h('div', { class: 'editor' },
    quote ? h('blockquote', { class: 'editor-quote' }, quote) : null,
    toolbar, ta, preview,
    h('div', { class: 'editor-foot' },
      h('span', { class: 'editor-hint' }, '⌘/Ctrl + Enter 保存'),
      h('span', { class: 'ed-spacer' }),
      cancelBtn, saveBtn));
  setTimeout(autosize, 0);
  return {
    el,
    ta,
    focus() {
      ta.focus();
      ta.selectionStart = ta.selectionEnd = ta.value.length;
    },
    getValue: () => ta.value,
    setValue: (v) => { ta.value = v; autosize(); },
  };
}
