// 生词本 /vocab：全库生词，搜索、只看未掌握、🔊、卡片复习、跳回论文对应页（SPEC §9）
import { get, patch, del, STATIC, appHref } from './api.js';
import { h, icon, toast, toastError, openModal, confirmDialog } from './ui.js';
import { vocabCard } from './panels.js';
import { speakButton, tts } from './tts.js';
import { navigate } from '../app.js';

export async function mountVocab(root) {
  const page = h('div', { class: 'page' });
  root.append(page);
  let all = [];
  const q = h('input', { type: 'search', placeholder: '搜索单词、释义或原句…' });
  const onlyNew = h('input', { type: 'checkbox' });
  onlyNew.checked = localStorage.getItem('pd.vocab.onlyNew') === '1';
  const countEl = h('span', { class: 'faint' });
  const list = h('div', { class: 'cards' });
  const reviewBtn = h('button', { class: 'btn btn-primary btn-sm' }, icon('sparkles', 14), '卡片复习');

  page.append(
    h('div', { class: 'page-head' }, h('div', { style: { flex: 1 } },
      h('h1', { class: 'page-title' }, '生词本'),
      h('div', { class: 'page-desc' }, '读论文时收集的所有生词，带原句和出处；点页码回到论文里那一句。'))),
    h('div', { class: 'vocab-tools' }, q, h('label', { class: 'check' }, onlyNew, '只看未掌握'), countEl, reviewBtn),
    list);

  const load = async () => {
    try {
      all = await get('/vocab');
    } catch (e) {
      toastError(e);
      all = [];
    }
    render();
  };

  const jump = (v) => {
    const qs = new URLSearchParams();
    if (v.page) qs.set('page', String(v.page));
    if (v.annotation_id) qs.set('anno', v.annotation_id);
    navigate(`/r/${encodeURIComponent(v.pid)}/${encodeURIComponent(v.iid)}${qs.toString() ? '?' + qs : ''}`);
  };

  const filtered = () => {
    const kw = q.value.trim().toLowerCase();
    return all.filter((v) => (!onlyNew.checked || !v.mastered)
      && (!kw || [v.word, v.meaning, v.context, v.context_meaning, v.paper_title].some((x) => (x || '').toLowerCase().includes(kw))));
  };

  const base = (v) => `/papers/${encodeURIComponent(v.pid)}/${encodeURIComponent(v.iid)}/vocab/${encodeURIComponent(v.id)}`;

  function render() {
    const items = filtered();
    countEl.textContent = `${items.length} / ${all.length} 个`;
    list.replaceChildren();
    if (!items.length) {
      list.append(h('div', { class: 'empty' }, all.length ? '没有匹配的生词' : '还没有生词。在阅读页双击单词 → 「生词」就会收进来。'));
      return;
    }
    for (const v of items) {
      list.append(vocabCard(v, {
        readonly: STATIC,
        source: h('a', { class: 'vocab-src', href: appHref(`/r/${encodeURIComponent(v.pid)}/${encodeURIComponent(v.iid)}${v.page ? `?page=${v.page}` : ''}`), 'data-link': '', title: '回到论文' }, v.paper_title || v.paper_key || '论文'),
        onJump: () => jump(v),
        onToggle: async (m) => {
          try {
            Object.assign(v, await patch(base(v), { mastered: m }));
            render();
          } catch (e) { toastError(e); }
        },
        onDelete: async () => {
          if (!(await confirmDialog(`从生词本删除「${v.word}」？`, { danger: true, okText: '删除' }))) return;
          try {
            await del(base(v));
            all = all.filter((x) => x !== v);
            render();
          } catch (e) { toastError(e); }
        },
      }));
    }
  }

  q.addEventListener('input', render);
  onlyNew.addEventListener('change', () => {
    localStorage.setItem('pd.vocab.onlyNew', onlyNew.checked ? '1' : '0');
    render();
  });
  reviewBtn.onclick = () => review(filtered().filter((v) => !v.mastered));

  function review(cards) {
    if (!cards.length) {
      toast('没有需要复习的生词', 'info');
      return;
    }
    const queue = cards.slice().sort(() => Math.random() - 0.5);
    const retried = new Set();
    let done = 0;
    const total = queue.length;
    const box = h('div', { class: 'review' });
    const m = openModal({ title: '卡片复习', content: box, width: 520, onClose: () => { tts.stop(); render(); } });
    const show = () => {
      box.replaceChildren();
      const v = queue[0];
      if (!v) {
        box.append(h('div', { class: 'review-card' }, h('div', { class: 'v-word' }, '🎉'), h('div', {}, `复习完了：${total} 个词`)),
          h('button', { class: 'btn', onclick: () => m.close() }, '关闭'));
        return;
      }
      let flipped = false;
      const back = h('div', { style: { display: 'none' } },
        v.pos || v.meaning ? h('div', { class: 'v-mean' }, `${v.pos ? v.pos + ' ' : ''}${v.meaning || ''}`) : null,
        v.context_meaning ? h('div', { class: 'v-cmean' }, `语境义：${v.context_meaning}`) : null,
        v.context ? h('div', { class: 'v-ctx' }, v.context) : null,
        v.context_translation ? h('div', { class: 'v-ctxtr' }, v.context_translation) : null);
      const flip = () => {
        if (flipped) return;
        flipped = true;
        back.style.display = '';
        hint.style.display = 'none';
        btns.style.display = 'flex';
      };
      const hint = h('div', { class: 'faint' }, '想一想它的意思，点卡片翻面');
      const card = h('div', { class: 'review-card', onclick: flip },
        h('div', { class: 'v-word' }, v.word),
        h('div', { style: { display: 'flex', justifyContent: 'center', gap: '8px', alignItems: 'center' } },
          v.phonetic ? h('span', { class: 'v-phon' }, v.phonetic) : null, speakButton(() => v.word, 'en')),
        hint, back);
      const know = h('button', { class: 'btn btn-primary' }, '认识');
      const dont = h('button', { class: 'btn' }, '不认识');
      know.onclick = async () => {
        queue.shift();
        done++;
        if (!STATIC) {
          try { Object.assign(v, await patch(base(v), { mastered: true })); } catch (e) { toastError(e); }
        }
        show();
      };
      dont.onclick = () => {
        queue.shift();
        if (!retried.has(v.id)) {
          retried.add(v.id);
          queue.splice(Math.min(queue.length, 3), 0, v); // 隔几张再出现一次
        } else done++;
        show();
      };
      const btns = h('div', { style: { display: 'none', gap: '10px' } }, dont, know);
      box.append(h('div', { class: 'review-prog' }, `已完成 ${done} / ${total}`), card, btns,
        h('a', { class: 'faint', style: { fontSize: '12px', cursor: 'pointer' }, onclick: () => { m.close(); jump(v); } }, `出处：${v.paper_title || ''}${v.page ? ` p.${v.page}` : ''}`));
      if (tts.supported) tts.speak(v.word, 'en');
    };
    show();
  }

  await load();
  return {};
}
