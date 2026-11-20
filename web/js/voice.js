// 语音对话模式（SPEC §11.2）：SpeechRecognition 语音输入 → 本地指令 / 问 claude|codex（style:"voice"）→ 边流式边朗读
// 触发：🎤 按钮；或按住 ⌥ Option 说话、松开发送（push-to-talk）
import { h, icon, toast } from './ui.js';
import { tts, StreamSpeaker, speechify, verbalizeTex } from './tts.js';

const SR = typeof window !== 'undefined' ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null;
export const recognitionSupported = !!SR;
const UNSUPPORTED = '当前浏览器不支持语音识别，请用 Chrome / Edge / Safari';
const LS = { on: 'pd.voice.on', lang: 'pd.voice.lang', hands: 'pd.voice.handsFree', resume: 'pd.voice.resumeBook' };

/**
 * 本地语音指令解析（中英文）。返回 {cmd, arg?}；cmd ∈
 * read | pause | resume | stop | nextPage | prevPage | translate | note | vocab | exit | ask
 */
export function parseVoiceCommand(raw) {
  const text = String(raw || '').trim();
  const t = text.replace(/[。．.!！?？，,、\s]+$/u, '').replace(/^(请|麻烦|帮我|嘿|hey|ok|okay|please)[，,\s]*/iu, '').trim();
  const lower = t.toLowerCase();
  const zhLen = t.replace(/\s/g, '').length;
  const enWords = lower.split(/\s+/).filter(Boolean).length;
  const short = (maxZh = 7, maxEn = 4) => zhLen <= maxZh || (/^[a-z\s']+$/i.test(t) && enWords <= maxEn);

  // 带参数的指令（参数可以很长）
  let m;
  if ((m = t.match(/^(?:记下来|记一下|记个笔记|记笔记|做个笔记|笔记)[：:，,\s]*(.+)$/u))) return { cmd: 'note', arg: m[1].trim() };
  if ((m = t.match(/^(?:take a note|make a note|note|write down|jot down)[:,\s]+(.+)$/i))) return { cmd: 'note', arg: m[1].trim() };
  if ((m = t.match(/^(?:加入生词本?|加生词|添加生词|生词本?)[：:，,\s]*(.+)$/u))) return { cmd: 'vocab', arg: cleanWord(m[1]) };
  if ((m = t.match(/^(?:add (?:the )?word|add to (?:my )?vocab(?:ulary)?|vocab(?:ulary)?|new word)[:,\s]+(.+)$/i))) return { cmd: 'vocab', arg: cleanWord(m[1]) };

  // 退出
  if (/^(结束|退出|关闭|关掉|停止)(语音模式|语音对话|语音)$/u.test(t) || /^(exit|quit|end|stop|close|leave|turn off) voice( mode| chat)?$/i.test(lower)) return { cmd: 'exit' };

  if (!short(8, 5)) return { cmd: 'ask' };

  if (/^(下一页|下页|往后翻|翻到下一页|翻页)$/u.test(t) || /^(next page|go to next page|page down)$/i.test(lower)) return { cmd: 'nextPage' };
  if (/^(上一页|上页|往前翻|翻到上一页)$/u.test(t) || /^(previous page|last page|prev page|go back a page|page up)$/i.test(lower)) return { cmd: 'prevPage' };
  if (/^翻译(一下)?(这|那)?(一)?(句|句话|段|段话|个|里)?$/u.test(t) || /^translate( this| that| it)?( sentence| paragraph| part)?$/i.test(lower)) return { cmd: 'translate' };
  if (/^(暂停|停|停一下|停下|先停|先停一下|别读了|暂停一下|安静)$/u.test(t) || /^(pause|stop|hold on|wait|be quiet|stop reading)$/i.test(lower)) return { cmd: 'pause' };
  if (/^(继续|接着读|继续读|接着念|继续念|往下读|接着来)$/u.test(t) || /^(continue|resume|go on|keep going|keep reading)$/i.test(lower)) return { cmd: 'resume' };
  if (/^(开始)?(朗读|读一下|读一读|读给我听|开始读|念一下|念给我听|读吧|读这段|读这一页)$/u.test(t) || /^(read|read it|read aloud|read this|start reading|read to me|read this page)$/i.test(lower)) return { cmd: 'read' };
  return { cmd: 'ask' };
}

function cleanWord(s) {
  return String(s).replace(/^[“"'‘]+|[”"'’。.!！?？，,]+$/gu, '').trim();
}

if (typeof window !== 'undefined') {
  window.__pdVoiceParse = parseVoiceCommand;
  window.__pdSpeechify = speechify;
  window.__pdVerbalizeTex = verbalizeTex;
}

const STATE_LABEL = { idle: '语音模式 · 点我或按住 ⌥ 说话', listening: '聆听中…', thinking: '思考中…', speaking: '回答中 · 点我打断' };

export class VoiceController {
  constructor(ctx, host) {
    this.ctx = ctx;
    this.host = host;
    this.supported = recognitionSupported;
    this.on = localStorage.getItem(LS.on) === '1';
    this.lang = localStorage.getItem(LS.lang) || 'zh-CN';
    this.handsFree = localStorage.getItem(LS.hands) === '1';
    this.resumeBook = localStorage.getItem(LS.resume) !== '0';
    this.state = 'idle';
    this.rec = null;
    this.ptt = false;
    this.speaker = null;
    this.pausedBook = null; // {pos} 因聆听而暂停的朗读
    this._buildUi();
    this._onKeyDown = (e) => this._keyDown(e);
    this._onKeyUp = (e) => this._keyUp(e);
    this._onBlur = () => { if (this.ptt) this.stopListening(); };
    document.addEventListener('keydown', this._onKeyDown, true);
    document.addEventListener('keyup', this._onKeyUp, true);
    window.addEventListener('blur', this._onBlur);
    this._render();
  }

  // ---------------- UI ----------------
  _buildUi() {
    const qa = this.ctx.panels.qa;
    this.orbLabel = h('span', { class: 'vo-label' });
    this.orbText = h('span', { class: 'vo-text' });
    this.orb = h('button', { class: 'voice-orb', title: '语音模式', onclick: () => this._orbClick() },
      h('span', { class: 'vo-dot' }), h('span', { class: 'vo-main' }, this.orbLabel, this.orbText));
    this.host.append(this.orb);

    this.mic = h('button', {
      class: 'btn mic-btn',
      title: this.supported ? '语音输入（语音模式下：说完自动提问并朗读回答；按住 ⌥ Option 也可以说话）' : UNSUPPORTED,
      style: { height: '38px', padding: '0 10px' },
      onclick: () => this._micClick(),
    }, '🎤');
    qa.micSlot.append(this.mic);

    this.toggle = h('input', { type: 'checkbox' });
    this.toggle.checked = this.on;
    this.toggle.onchange = () => this.setMode(this.toggle.checked);
    const gear = h('button', { class: 'icon-btn', title: '语音设置' }, icon('settings', 14));
    gear.onclick = () => this._openSettings(gear);
    qa.voiceSlot.append(h('label', { class: 'check', title: '开启后：语音提问，回答会被朗读出来；支持「朗读 / 暂停 / 继续 / 下一页 / 翻译这句 / 记下来… / 生词…」等语音指令' }, this.toggle, '语音模式'), gear);
  }

  _openSettings(anchor) {
    document.querySelectorAll('.voice-settings').forEach((x) => x.remove());
    const langSel = h('select', {}, h('option', { value: 'zh-CN' }, '中文（普通话）'), h('option', { value: 'en-US' }, 'English (US)'));
    langSel.value = this.lang;
    langSel.onchange = () => { this.lang = langSel.value; localStorage.setItem(LS.lang, this.lang); };
    const hands = h('input', { type: 'checkbox' });
    hands.checked = this.handsFree;
    hands.onchange = () => { this.handsFree = hands.checked; localStorage.setItem(LS.hands, hands.checked ? '1' : '0'); };
    const res = h('input', { type: 'checkbox' });
    res.checked = this.resumeBook;
    res.onchange = () => { this.resumeBook = res.checked; localStorage.setItem(LS.resume, res.checked ? '1' : '0'); };
    const box = h('div', { class: 'anno-menu voice-settings', style: { width: '310px' }, onmousedown: (e) => e.stopPropagation() },
      h('div', { style: { fontWeight: '600', fontSize: '13px' } }, '语音设置'),
      h('label', { class: 'field' }, h('span', { class: 'field-label' }, '识别语言'), langSel),
      h('label', { class: 'check' }, hands, '连续对话（回答读完后自动继续聆听）'),
      h('label', { class: 'check' }, res, '答完继续朗读论文（从被打断的位置）'),
      h('div', { class: 'field-hint' }, '按住 ⌥ Option 说话、松开发送；回答播报时点语音球可打断。'),
      h('div', { class: 'field-hint' }, this.supported
        ? '隐私提示：Chrome / Edge 的语音识别会把音频发到 Google / 微软服务器转写；Safari 走 Apple。朗读（TTS）是本机离线的。'
        : UNSUPPORTED));
    document.body.append(box);
    const r = anchor.getBoundingClientRect();
    box.style.left = `${Math.max(8, Math.min(window.innerWidth - 320, r.right - 310))}px`;
    box.style.top = `${r.bottom + 6}px`;
    const close = (e) => {
      if (!box.contains(e.target)) {
        box.remove();
        document.removeEventListener('mousedown', close);
      }
    };
    setTimeout(() => document.addEventListener('mousedown', close), 0);
  }

  _render() {
    this.orb.style.display = this.on ? '' : 'none';
    this.orb.className = `voice-orb st-${this.state}${this.supported ? '' : ' unsupported'}`;
    this.orbLabel.textContent = this.supported || this.state !== 'idle' ? STATE_LABEL[this.state] : '不支持语音识别（请用 Chrome / Edge / Safari）';
    this.mic.classList.toggle('active', this.state === 'listening');
    this.mic.textContent = this.state === 'listening' ? '⏺' : '🎤';
  }

  _interim(text) {
    this.orbText.textContent = text ? `“${text}”` : '';
    if (this.state === 'listening' && this._dictateBase !== undefined) {
      this.ctx.panels.qa.textarea.value = this._dictateBase + text;
    }
  }

  setState(s) {
    this.state = s;
    this._render();
  }

  setMode(on) {
    this.on = on;
    this.toggle.checked = on;
    localStorage.setItem(LS.on, on ? '1' : '0');
    if (!on) {
      this.stopListening(true);
      if (this.speaker) { this.speaker.cancel(); this.speaker = null; }
      this.setState('idle');
    } else {
      if (!this.supported) toast(UNSUPPORTED, 'error');
      else toast('语音模式已开启：点语音球或按住 ⌥ Option 说话', 'ok', 2600);
    }
    this._render();
  }

  // ---------------- 交互 ----------------
  _orbClick() {
    if (this.state === 'speaking' || this.state === 'thinking') {
      this.bargeIn();
      this.listen();
    } else if (this.state === 'listening') this.stopListening();
    else this.listen();
  }

  _micClick() {
    if (!this.supported) return toast(UNSUPPORTED, 'error');
    if (this.state === 'listening') return this.stopListening();
    if (this.state === 'speaking' || this.state === 'thinking') this.bargeIn();
    this.listen({ dictate: !this.on });
  }

  _keyDown(e) {
    if (e.key !== 'Alt' || e.repeat || !this.on) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    if (!this.supported) return;
    e.preventDefault();
    if (this.state === 'speaking' || this.state === 'thinking') this.bargeIn();
    if (!this.rec) this.listen({ ptt: true });
  }

  _keyUp(e) {
    if (e.key !== 'Alt' || !this.ptt) return;
    e.preventDefault();
    this.stopListening();
  }

  /** 打断播报（barge-in） */
  bargeIn() {
    if (this.speaker) {
      this.speaker.cancel();
      this.speaker = null;
    }
    if (tts.owner === 'voice' || tts.owner === 'snippet') tts.stop();
    this.setState('idle');
  }

  listen({ ptt = false, dictate = false } = {}) {
    if (!this.supported) {
      toast(UNSUPPORTED, 'error');
      return;
    }
    if (this.rec) return;
    if (tts.owner === 'voice' || tts.owner === 'snippet') tts.stop(); // 播报时不聆听，避免回声
    const book = this.ctx.book;
    if (!dictate && book.playing && !book.paused) {
      this.pausedBook = { pos: { ...book.pos } };
      book.pause();
    }
    let r;
    try {
      r = new SR();
    } catch (e) {
      toast(`无法启动语音识别：${e.message}`, 'error');
      return;
    }
    r.lang = this.lang;
    r.interimResults = true;
    r.continuous = ptt;
    r.maxAlternatives = 1;
    let finalText = '';
    this._dictateBase = dictate ? (this.ctx.panels.qa.textarea.value ? `${this.ctx.panels.qa.textarea.value} ` : '') : undefined;
    r.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      this._interim(finalText + interim);
    };
    r.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') toast('麦克风权限被拒绝：在浏览器地址栏左侧允许麦克风后重试', 'error');
      else if (e.error === 'network') toast('语音识别需要联网（Chrome 会把音频发到 Google 转写）', 'error');
      else if (e.error === 'audio-capture') toast('没有检测到麦克风', 'error');
      else if (e.error !== 'no-speech' && e.error !== 'aborted') toast(`语音识别出错：${e.error}`, 'error');
    };
    r.onend = () => {
      const wasDictate = this._dictateBase !== undefined;
      const base = this._dictateBase || '';
      this.rec = null;
      this.ptt = false;
      this._dictateBase = undefined;
      const t = finalText.trim();
      this._interim('');
      if (wasDictate) {
        const ta = this.ctx.panels.qa.textarea;
        ta.value = (base + t).trimStart();
        ta.dispatchEvent(new Event('input'));
        ta.focus();
        this.setState('idle');
        return;
      }
      if (t) this.handleFinal(t);
      else {
        this.setState('idle');
        this._resumePausedBook();
      }
    };
    try {
      r.start();
    } catch (e) {
      toast(`无法启动语音识别：${e.message}`, 'error');
      return;
    }
    this.rec = r;
    this.ptt = ptt;
    this.setState('listening');
  }

  stopListening(abort = false) {
    if (!this.rec) return;
    try { abort ? this.rec.abort() : this.rec.stop(); } catch { /* ignore */ }
  }

  _resumePausedBook() {
    if (this.pausedBook) {
      this.pausedBook = null;
      this.ctx.book.resume();
    }
  }

  _currentSentence() {
    const sel = this.ctx.pdf && this.ctx.pdf.getSelectionInfo();
    if (sel && sel.quote) return sel.quote;
    const b = this.ctx.book;
    if (b.current && (b.playing || this.pausedBook)) return b.current.item.text;
    return '';
  }

  async _say(text) {
    this.setState('speaking');
    await tts.speak(text, 'zh');
    if (this.state === 'speaking') this.setState('idle');
  }

  async handleFinal(text) {
    const c = parseVoiceCommand(text);
    if (c.cmd !== 'ask') toast(`🎤 ${text}`, 'info', 1800);
    try {
      await this.runCommand(c, text);
    } catch (e) {
      toast(e.message || String(e), 'error');
      this.setState('idle');
    }
  }

  async runCommand(c, text) {
    const { ctx } = this;
    const book = ctx.book;
    const paused = this.pausedBook;
    this.pausedBook = null;
    switch (c.cmd) {
      case 'read': {
        ctx.showTtsBar();
        if (book.playing && book.paused) book.resume();
        else if (!book.playing) {
          const p = book.pos && book.pos.page === ctx.currentPage() ? book.pos : { page: ctx.currentPage(), s: 0 };
          book.playFrom(p.page, p.s);
        }
        this.setState('idle');
        return;
      }
      case 'pause':
        if (book.playing && !book.paused) book.pause();
        else if (!book.playing) tts.stop();
        this.setState('idle');
        return;
      case 'resume':
        ctx.showTtsBar();
        book.resume();
        this.setState('idle');
        return;
      case 'nextPage':
      case 'prevPage': {
        const d = c.cmd === 'nextPage' ? 1 : -1;
        if (book.playing || paused) (d > 0 ? book.nextPage() : book.prevPage());
        else ctx.gotoPage(ctx.currentPage() + d);
        this.setState('idle');
        return;
      }
      case 'translate': {
        const s = this._currentSentence();
        if (!s) return this._afterTurn(paused, () => this._say('没有选中的句子，也没有正在朗读的句子'));
        if (book.playing && !book.paused) book.pause();
        this.setState('thinking');
        const tr = await ctx.panels.translateText(s, ctx.currentPage());
        if (tr) await this._say(tr);
        return this._afterTurn(paused);
      }
      case 'note': {
        const quote = this._currentSentence() || null;
        await ctx.addNote({ text: c.arg, page: ctx.currentPage(), quote, source: 'manual' });
        toast('已记下', 'ok', 1400);
        if (!paused) await this._say('已记下');
        return this._afterTurn(paused);
      }
      case 'vocab': {
        const v = await ctx.panels.addVocabWord(c.arg, { context: this._currentSentence() || c.arg, page: ctx.currentPage() });
        if (v) {
          ctx.panels.switchTab('vocab');
          if (!paused) await this._say(v.meaning ? `${v.word}：${v.meaning}` : `已把 ${v.word} 加入生词本`);
        }
        return this._afterTurn(paused);
      }
      case 'exit':
        this.setMode(false);
        await tts.speak('已退出语音模式', 'zh');
        return;
      default:
        return this.askVoice(text, paused);
    }
  }

  async _afterTurn(paused, extra) {
    if (extra) await extra();
    if (paused && this.resumeBook) {
      this.ctx.book.playFrom(paused.pos.page, paused.pos.s);
      this.setState('idle');
      return;
    }
    this.setState('idle');
    if (this.handsFree && this.on) setTimeout(() => { if (this.state === 'idle' && !this.rec) this.listen(); }, 350);
  }

  async askVoice(question, paused) {
    const { ctx } = this;
    const book = ctx.book;
    const quote = this._currentSentence() || null;
    if (book.playing) book.stop();
    this.setState('thinking');
    const speaker = new StreamSpeaker({ onSentence: () => { if (this.speaker === speaker) this.setState('speaking'); } });
    this.speaker = speaker;
    const qa = await ctx.panels.ask({
      question,
      quote,
      page: ctx.currentPage(),
      style: 'voice',
      onDelta: (t) => { if (this.speaker === speaker) speaker.feed(t); },
    });
    if (this.speaker !== speaker) return; // 被打断
    if (!qa) {
      speaker.cancel();
      this.speaker = null;
      this.setState('idle');
      return;
    }
    const ok = await speaker.finish();
    if (this.speaker !== speaker) return;
    this.speaker = null;
    if (!ok) { this.setState('idle'); return; }
    return this._afterTurn(paused);
  }

  destroy() {
    this.stopListening(true);
    if (this.speaker) this.speaker.cancel();
    document.removeEventListener('keydown', this._onKeyDown, true);
    document.removeEventListener('keyup', this._onKeyUp, true);
    window.removeEventListener('blur', this._onBlur);
    document.querySelectorAll('.voice-settings').forEach((x) => x.remove());
    this.orb.remove();
  }
}
