// 朗读：浏览器 Web Speech API（speechSynthesis）
// - say(text, lang)：读一句，返回 Promise<boolean>（读完 true，被取消 false）
// - 句子切分 / 长句再切（≤220 字符，规避 Chrome 长句截断）
// - speechify：去 Markdown、公式口语化（语音问答播报用）
// - StreamSpeaker：边流式边按句朗读
// 设置（语速 / 中英语音）存 localStorage（SPEC §8 §11）

const LS = { rate: 'pd.tts.rate', en: 'pd.tts.en', zh: 'pd.tts.zh' };
export const MAX_UTTER = 220;

const ABBR = ['e.g.', 'i.e.', 'et al.', 'etc.', 'Fig.', 'Figs.', 'Eq.', 'Eqs.', 'Sec.', 'Tab.', 'vs.', 'cf.', 'resp.', 'approx.', 'Dr.', 'Mr.', 'Ms.', 'No.', 'Ref.', 'Refs.', 'Thm.', 'Def.', 'Prop.', 'Lem.', 'Alg.', 'App.', 'Ch.', 'pp.', 'al.', 'w.r.t.', 'a.k.a.'];

export function detectLang(text) {
  const s = String(text || '');
  const cjk = (s.match(/[㐀-鿿豈-﫿]/g) || []).length;
  const latin = (s.match(/[A-Za-z]/g) || []).length;
  return cjk > 1 && cjk > latin * 0.25 ? 'zh' : 'en';
}

/** 句子切分，返回 [{start, end, text}]（start/end 为原字符串偏移） */
export function splitSentences(text, { merge = true } = {}) {
  const s = String(text || '');
  const out = [];
  let start = 0;
  const isAbbrEnd = (i) => {
    for (const a of ABBR) {
      const st = i - a.length + 1;
      if (st >= 0 && s.slice(st, i + 1) === a && (st === 0 || /[\s(]/.test(s[st - 1]))) return true;
    }
    if (i >= 1 && /[A-Z]/.test(s[i - 1]) && (i === 1 || /\s/.test(s[i - 2]))) return true; // 单个大写字母 "J. Smith"
    return false;
  };
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    let cut = false;
    if ('。！？；'.includes(c)) cut = true;
    else if (c === '!' || c === '?') cut = /\s/.test(s[i + 1] || ' ');
    else if (c === '.') {
      const next = s[i + 1];
      if ((next === undefined || /\s/.test(next)) && !isAbbrEnd(i)) {
        let j = i + 1;
        while (j < s.length && /\s/.test(s[j])) j++;
        cut = j >= s.length || /[A-Z0-9“"(\[㐀-鿿]/.test(s[j]);
      }
    }
    if (cut) {
      let end = i + 1;
      while (end < s.length && /["'”’)\]]/.test(s[end])) end++;
      push(start, end);
      start = end;
      i = end - 1;
    }
  }
  push(start, s.length);
  function push(a, b) {
    let x = a;
    let y = b;
    while (x < y && /\s/.test(s[x])) x++;
    while (y > x && /\s/.test(s[y - 1])) y--;
    if (y > x) out.push({ start: x, end: y, text: s.slice(x, y).replace(/\s+/g, ' ') });
  }
  if (!merge) return out;
  const merged = [];
  for (const it of out) {
    const prev = merged[merged.length - 1];
    if (prev && (prev.text.length < 14 || it.text.length < 4) && prev.text.length + it.text.length < MAX_UTTER) {
      prev.end = it.end;
      prev.text = `${prev.text} ${it.text}`;
    } else merged.push({ ...it });
  }
  return merged;
}

/** 长句再切：优先在 ; , : — 处切，其次在空格处切，每段 ≤ max；返回 [{start,end,text}]（相对 text） */
export function chunkLong(text, max = MAX_UTTER) {
  const s = String(text || '');
  if (s.length <= max) return [{ start: 0, end: s.length, text: s }];
  const out = [];
  let pos = 0;
  while (s.length - pos > max) {
    const win = s.slice(pos, pos + max);
    let cut = -1;
    for (const re of [/[;；]\s/g, /[,，:：—]\s?/g, /\s(?:and|but|which|where|while|that|或|和)\s/g, /\s/g]) {
      let m;
      let last = -1;
      re.lastIndex = 0;
      while ((m = re.exec(win))) if (m.index > max * 0.35) last = m.index + m[0].length;
      if (last > 0) { cut = last; break; }
    }
    if (cut <= 0) cut = max;
    const piece = s.slice(pos, pos + cut);
    out.push({ start: pos, end: pos + cut, text: piece.trim() });
    pos += cut;
  }
  if (pos < s.length) out.push({ start: pos, end: s.length, text: s.slice(pos).trim() });
  return out.filter((x) => x.text);
}

/** 句子 → 可朗读的片段（每段 ≤220） */
export function toUtterances(text) {
  const res = [];
  for (const sen of splitSentences(text)) for (const ch of chunkLong(sen.text)) res.push(ch.text);
  return res;
}

// ---------------- 公式口语化 / 去 Markdown ----------------
const TEX_WORDS = {
  alpha: 'alpha', beta: 'beta', gamma: 'gamma', delta: 'delta', epsilon: 'epsilon', theta: 'theta', lambda: 'lambda',
  mu: 'mu', sigma: 'sigma', pi: 'pi', phi: 'phi', psi: 'psi', omega: 'omega', tau: 'tau', eta: 'eta', rho: 'rho',
};

export function verbalizeTex(tex, lang = 'zh') {
  let t = String(tex);
  const zh = lang === 'zh';
  const rep = (re, f) => { t = t.replace(re, f); };
  for (let k = 0; k < 3; k++) {
    rep(/\\sqrt\s*\{([^{}]*)\}/g, (m, x) => (zh ? `根号 ${x}` : `square root of ${x}`));
    rep(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, (m, a, b) => (zh ? `${b} 分之 ${a}` : `${a} over ${b}`));
    rep(/\\(?:mathrm|mathbf|mathcal|text|operatorname|boldsymbol|mathbb)\s*\{([^{}]*)\}/g, '$1');
  }
  rep(/\^\s*\{?\s*2\s*\}?/g, zh ? ' 平方' : ' squared');
  rep(/\^\s*\{?\s*3\s*\}?/g, zh ? ' 立方' : ' cubed');
  rep(/\^\s*\{?\s*T\s*\}?/g, zh ? ' 转置' : ' transpose');
  rep(/\^\s*\{([^{}]*)\}/g, (m, x) => (zh ? ` 的 ${x} 次方` : ` to the power ${x}`));
  rep(/\^\s*(\w)/g, (m, x) => (zh ? ` 的 ${x} 次方` : ` to the power ${x}`));
  rep(/_\s*\{([^{}]*)\}/g, (m, x) => (zh ? ` 下标 ${x}` : ` sub ${x}`));
  rep(/_\s*(\w)/g, (m, x) => (zh ? ` 下标 ${x}` : ` sub ${x}`));
  rep(/\\(sum)\b/g, zh ? '求和' : 'sum');
  rep(/\\(prod)\b/g, zh ? '连乘' : 'product');
  rep(/\\int\b/g, zh ? '积分' : 'integral');
  rep(/\\(leq|le)\b/g, zh ? '小于等于' : 'less or equal');
  rep(/\\(geq|ge)\b/g, zh ? '大于等于' : 'greater or equal');
  rep(/\\(neq|ne)\b/g, zh ? '不等于' : 'not equal');
  rep(/\\approx\b/g, zh ? '约等于' : 'approximately');
  rep(/\\(cdot|times)\b/g, zh ? '乘' : 'times');
  rep(/\\in\b/g, zh ? '属于' : 'in');
  rep(/\\(to|rightarrow)\b/g, zh ? '到' : 'to');
  rep(/\\infty\b/g, zh ? '无穷' : 'infinity');
  rep(/\\([a-zA-Z]+)/g, (m, w) => TEX_WORDS[w] || w);
  rep(/[{}\\$]/g, ' ');
  rep(/=/g, zh ? ' 等于 ' : ' equals ');
  return t.replace(/\s+/g, ' ').trim();
}

/** Markdown → 适合朗读的纯文本（公式口语化） */
export function speechify(md, lang) {
  let s = String(md || '');
  const L = lang || detectLang(s);
  s = s.replace(/```[\s\S]*?(```|$)/g, L === 'zh' ? '（代码略）' : ' (code omitted) ');
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => ` ${verbalizeTex(t, L)} `);
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => ` ${verbalizeTex(t, L)} `);
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => ` ${verbalizeTex(t, L)} `);
  s = s.replace(/\$([^$\n]+?)\$/g, (m, t) => ` ${verbalizeTex(t, L)} `);
  s = s.replace(/`([^`]+)`/g, '$1');
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  s = s.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  s = s.replace(/<\/?(ins|u|mark|b|i|em|strong|sup|sub|br)\s*\/?>/gi, '');
  s = s.replace(/^\s{0,3}#{1,6}\s*/gm, '');
  s = s.replace(/^\s*>\s?/gm, '');
  s = s.replace(/^\s*[-*+]\s+/gm, '');
  s = s.replace(/^\s*\d+[.)]\s+/gm, '');
  s = s.replace(/^\s*\|?[-:\s|]+\|?\s*$/gm, '');
  s = s.replace(/\|/g, ' ');
  s = s.replace(/(\*\*|__|\*|_|~~)(?=\S)([\s\S]*?\S)\1/g, '$2');
  s = s.replace(/\(p\.\s*(\d+)\)/g, L === 'zh' ? '（第 $1 页）' : ' (page $1) ');
  return s.replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
}

// ---------------- 播放核心 ----------------
class TTS {
  constructor() {
    this.supported = typeof window !== 'undefined' && 'speechSynthesis' in window;
    this.voices = [];
    this.rate = clamp(parseFloat(localStorage.getItem(LS.rate)) || 1.0, 0.6, 1.6);
    this.enName = localStorage.getItem(LS.en) || '';
    this.zhName = localStorage.getItem(LS.zh) || '';
    this.state = 'idle'; // idle | playing | paused
    this.owner = null; // 当前占用者：'book' | 'voice' | 'snippet'
    this.listeners = new Set();
    this.gen = 0;
    this._pending = new Set();
    if (this.supported) {
      this._loadVoices();
      speechSynthesis.addEventListener && speechSynthesis.addEventListener('voiceschanged', () => this._loadVoices());
    }
  }

  _loadVoices() {
    this.voices = speechSynthesis.getVoices() || [];
    this._emit({ type: 'voices' });
  }

  on(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  _emit(ev) {
    for (const fn of this.listeners) {
      try { fn(ev); } catch (e) { console.warn(e); }
    }
  }

  _setState(st) {
    if (this.state === st) return;
    this.state = st;
    this._emit({ type: 'state', state: st, owner: this.owner });
  }

  enVoices() { return this.voices.filter((v) => /^en/i.test(v.lang)); }
  zhVoices() { return this.voices.filter((v) => /^(zh|cmn)/i.test(v.lang)); }

  pickEn() {
    const list = this.enVoices();
    if (!list.length) return null;
    if (this.enName) {
      const v = list.find((x) => x.name === this.enName);
      if (v) return v;
    }
    const us = list.filter((v) => /en[-_]US/i.test(v.lang));
    const pool = us.length ? us : list;
    const score = (v) => (/premium/i.test(v.name) ? 6 : 0) + (/enhanced/i.test(v.name) ? 5 : 0)
      + (/samantha|ava|alex|allison|zoe|evan|nathan|serena/i.test(v.name) ? 3 : 0) + (v.localService ? 1 : 0);
    return pool.slice().sort((a, b) => score(b) - score(a))[0];
  }

  pickZh() {
    const list = this.zhVoices();
    if (!list.length) return null;
    if (this.zhName) {
      const v = list.find((x) => x.name === this.zhName);
      if (v) return v;
    }
    const cn = list.filter((v) => /zh[-_]CN|cmn/i.test(v.lang));
    const pool = cn.length ? cn : list;
    const score = (v) => (/premium|enhanced/i.test(v.name) ? 3 : 0) + (/tingting|lili|yu-shu|meijia/i.test(v.name) ? 1 : 0);
    return pool.slice().sort((a, b) => score(b) - score(a))[0];
  }

  setRate(r) {
    this.rate = clamp(r, 0.6, 1.6);
    localStorage.setItem(LS.rate, String(this.rate));
  }

  setVoice(lang, name) {
    if (lang === 'zh') { this.zhName = name; localStorage.setItem(LS.zh, name); } else { this.enName = name; localStorage.setItem(LS.en, name); }
  }

  /** 读一句（≤220 字符由调用方保证；超长会自动切） */
  say(text, lang, { onstart } = {}) {
    if (!this.supported || !text || !String(text).trim()) return Promise.resolve(true);
    const L = lang || detectLang(text);
    const parts = String(text).length > MAX_UTTER ? chunkLong(text).map((c) => c.text) : [String(text)];
    const gen = this.gen;
    const one = (t, first) => new Promise((resolve) => {
      if (gen !== this.gen) return resolve(false);
      const u = new SpeechSynthesisUtterance(t);
      const v = L === 'zh' ? this.pickZh() : this.pickEn();
      if (v) u.voice = v;
      u.lang = v ? v.lang : (L === 'zh' ? 'zh-CN' : 'en-US');
      u.rate = this.rate;
      const done = (ok) => {
        this._pending.delete(entry);
        resolve(ok && gen === this.gen);
      };
      const entry = { resolve: () => done(false) };
      this._pending.add(entry);
      u.onstart = () => {
        if (gen !== this.gen) return;
        this._setState('playing');
        if (first && onstart) onstart();
      };
      u.onend = () => done(true);
      u.onerror = (e) => {
        if (e.error !== 'interrupted' && e.error !== 'canceled') console.warn('tts error', e.error);
        done(e.error !== 'interrupted' && e.error !== 'canceled');
      };
      speechSynthesis.speak(u);
    });
    return (async () => {
      for (let i = 0; i < parts.length; i++) {
        const ok = await one(parts[i], i === 0);
        if (!ok) return false;
      }
      return true;
    })();
  }

  /** 读一段文字（自动切句），owner='snippet' */
  async speak(text, lang) {
    this.stop();
    this.owner = 'snippet';
    const gen = this.gen;
    for (const t of toUtterances(text)) {
      const ok = await this.say(t, lang || detectLang(text));
      if (!ok || gen !== this.gen) return false;
    }
    if (gen === this.gen) { this.owner = null; this._setState('idle'); }
    return true;
  }

  pause() {
    if (!this.supported || this.state !== 'playing') return;
    speechSynthesis.pause();
    this._setState('paused');
  }

  resume() {
    if (!this.supported || this.state !== 'paused') return;
    speechSynthesis.resume();
    this._setState('playing');
  }

  /** 停止一切朗读，所有挂起的 say() 立即以 false 结束 */
  stop() {
    if (!this.supported) return;
    this.gen++;
    for (const p of Array.from(this._pending)) p.resolve();
    this._pending.clear();
    speechSynthesis.cancel();
    this.owner = null;
    this._setState('idle');
  }

  /** 标记某次播放结束（由调用方在队列读完时调用） */
  done(owner) {
    if (this.owner === owner || owner === undefined) {
      this.owner = null;
      this._setState('idle');
    }
  }
}

function clamp(v, a, b) {
  return Math.min(b, Math.max(a, v));
}

export const tts = new TTS();

/**
 * 边流式边朗读：feed(增量文本) → 每凑满一个完整句子就排队朗读；finish() 读完剩余；
 * 返回的 done Promise 在全部读完（或被 cancel）时 resolve。
 */
export class StreamSpeaker {
  constructor({ lang = null, onSentence } = {}) {
    this.buf = '';
    this.queue = [];
    this.finished = false;
    this.cancelled = false;
    this.lang = lang;
    this.onSentence = onSentence;
    this.running = false;
    this._resolve = null;
    this.done = new Promise((r) => { this._resolve = r; });
    tts.stop();
    tts.owner = 'voice';
    this.gen = tts.gen;
  }

  feed(text) {
    if (this.cancelled) return;
    this.buf += text;
    // 不在未闭合的公式 / 代码块中途切句
    const dollars = (this.buf.match(/\$/g) || []).length;
    const fences = (this.buf.match(/```/g) || []).length;
    if (dollars % 2 === 1 || fences % 2 === 1) return;
    const sens = splitSentences(this.buf, { merge: false });
    if (sens.length < 2) return;
    const keepFrom = sens[sens.length - 1].start;
    const ready = this.buf.slice(0, keepFrom);
    this.buf = this.buf.slice(keepFrom);
    this._enqueue(ready);
  }

  _enqueue(md) {
    const L = this.lang || detectLang(md);
    const plain = speechify(md, L);
    for (const t of toUtterances(plain)) this.queue.push({ text: t, lang: this.lang || detectLang(t) || L });
    this._run();
  }

  finish() {
    if (this.cancelled) return this.done;
    if (this.buf.trim()) this._enqueue(this.buf);
    this.buf = '';
    this.finished = true;
    this._run();
    return this.done;
  }

  cancel() {
    this.cancelled = true;
    this.queue = [];
    if (tts.gen === this.gen) tts.stop();
    this._resolve(false);
  }

  async _run() {
    if (this.running || this.cancelled) return;
    this.running = true;
    while (this.queue.length && !this.cancelled) {
      const it = this.queue.shift();
      this.onSentence && this.onSentence(it.text);
      const ok = await tts.say(it.text, it.lang);
      if (!ok || tts.gen !== this.gen) {
        this.cancelled = true;
        this._resolve(false);
        this.running = false;
        return;
      }
    }
    this.running = false;
    if (this.finished && !this.queue.length && !this.cancelled) {
      tts.done('voice');
      this._resolve(true);
    }
  }
}

/** 生成一个小喇叭按钮 */
export function speakButton(getText, lang, title = '朗读') {
  const b = document.createElement('button');
  b.className = 'icon-btn speak-btn';
  b.title = title;
  b.type = 'button';
  b.textContent = '🔊';
  b.addEventListener('mousedown', (e) => e.stopPropagation());
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!tts.supported) return;
    const t = typeof getText === 'function' ? getText() : getText;
    const L = lang || detectLang(t);
    tts.speak(speechify(t, L), L);
  });
  return b;
}
