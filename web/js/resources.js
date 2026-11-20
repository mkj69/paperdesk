// 一篇工作的多资源（SPEC §13.2）：类型识别、标签、阅读器选择、视频嵌入地址
import { h, truncate } from './ui.js';

export const KINDS = {
  paper: { icon: '📄', label: '论文' },
  code: { icon: '💻', label: '代码' },
  homepage: { icon: '🏠', label: '主页' },
  blog: { icon: '📝', label: '博客' },
  video: { icon: '🎬', label: '视频' },
  slides: { icon: '🖼️', label: '幻灯片' },
  dataset: { icon: '🤗', label: '数据集' },
  model: { icon: '🤗', label: '模型' },
  demo: { icon: '🤗', label: 'Demo' },
  other: { icon: '🔗', label: '链接' },
};
export const KIND_ORDER = ['paper', 'code', 'homepage', 'blog', 'video', 'slides', 'model', 'dataset', 'demo', 'other'];

export function kindMeta(kind) {
  return KINDS[kind] || KINDS.other;
}

export function hostOf(url) {
  try {
    return new URL(url).host.replace(/^www\./, '');
  } catch {
    return '';
  }
}

/** 与后端 fetcher.classify 相同的规则（前端只用于预览/兜底，后端结果优先） */
export function classifyUrl(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return 'other';
  }
  const host = u.host.toLowerCase().replace(/^www\./, '');
  const path = u.pathname.toLowerCase();
  if (/(^|\.)arxiv\.org$/.test(host) || /(^|\.)alphaxiv\.org$/.test(host) || host === 'openreview.net' || /\.pdf$/.test(path)) return 'paper';
  if (/^(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)$/.test(host)) return 'code';
  if (/(^|\.)youtube\.com$/.test(host) || host === 'youtu.be' || /(^|\.)bilibili\.com$/.test(host) || /(^|\.)vimeo\.com$/.test(host)) return 'video';
  if (host === 'huggingface.co') {
    if (path.startsWith('/datasets/')) return 'dataset';
    if (path.startsWith('/spaces/')) return 'demo';
    if (path.split('/').filter(Boolean).length >= 2) return 'model';
    return 'homepage';
  }
  if (/speakerdeck\.com$|slideshare\.net$/.test(host) || (host === 'docs.google.com' && path.startsWith('/presentation')) || /\.(pptx?|key)$/.test(path)) return 'slides';
  if (/(^|\.)medium\.com$|substack\.com$|distill\.pub$/.test(host) || /blog/.test(host) || /\/(blog|posts?|p)\//.test(path)) return 'blog';
  const segs = path.split('/').filter(Boolean);
  return segs.length <= 1 ? 'homepage' : 'blog';
}

export function isGithubRepo(url) {
  try {
    const u = new URL(url);
    return /^(www\.)?github\.com$/i.test(u.host) && u.pathname.split('/').filter(Boolean).length >= 2;
  } catch {
    return false;
  }
}

/** arXiv abs/html 链接 → PDF 地址 */
export function guessPdfUrl(url) {
  if (!url) return null;
  const m = url.match(/arxiv\.org\/(?:abs|pdf|html)\/([^?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$/i);
  if (m) return `https://arxiv.org/pdf/${m[1]}`;
  const o = url.match(/openreview\.net\/(?:forum|pdf)\?id=([^&#]+)/i);
  if (o) return `https://openreview.net/pdf?id=${o[1]}`;
  if (/\.pdf([?#]|$)/i.test(url)) return url;
  return null;
}

/** 条目的全部资源：主链接（id "main"）+ item.resources */
export function itemResources(item, meta = {}) {
  const type = item.type === 'link' ? 'web' : item.type;
  const url = item.url || meta.url || '';
  const mainKind = type === 'paper' ? 'paper' : (item.kind || meta.kind || classifyUrl(url));
  const main = {
    id: 'main',
    main: true,
    kind: mainKind,
    url,
    pdf_url: item.pdf_url || meta.pdf_url || (type === 'paper' ? guessPdfUrl(url) : null),
    title: item.title || meta.title || url,
  };
  const extra = (item.resources || []).filter((r) => r && r.id && r.id !== 'main' && r.url);
  return [main, ...extra];
}

/** 资源用哪种阅读器打开 */
export function viewerFor(res) {
  if (!res) return 'notice';
  const kind = res.kind || classifyUrl(res.url);
  if (kind === 'paper') return (res.pdf_url || guessPdfUrl(res.url)) ? 'pdf' : 'article';
  if (kind === 'video') return videoEmbed(res.url) ? 'video' : 'notice';
  if (kind === 'slides') return 'iframe';
  return 'article';
}

export function pdfUrlOf(res) {
  return res.pdf_url || guessPdfUrl(res.url) || res.url;
}

/** 资源条/徽标上的文字 */
export function resLabel(res, { long = false } = {}) {
  const km = kindMeta(res.kind);
  if (res.main && res.kind === 'paper') return '论文 PDF';
  const t = res.title && res.title !== res.url ? res.title : hostOf(res.url);
  if (!t) return km.label;
  return long ? `${km.label}：${t}` : `${km.label}：${truncate(t, 22)}`;
}

export function resShort(res) {
  const km = kindMeta(res.kind);
  if (res.main && res.kind === 'paper') return '论文';
  return km.label;
}

/** 视频嵌入地址（YouTube / Bilibili / Vimeo），不支持的返回 null */
export function videoEmbed(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  const host = u.host.replace(/^www\.|^m\./, '');
  if (host === 'youtu.be') {
    const id = u.pathname.slice(1).split('/')[0];
    return id ? `https://www.youtube-nocookie.com/embed/${id}` : null;
  }
  if (host.endsWith('youtube.com')) {
    let id = u.searchParams.get('v');
    const m = u.pathname.match(/^\/(?:embed|shorts|live)\/([^/?#]+)/);
    if (!id && m) id = m[1];
    if (!id) return null;
    const t = u.searchParams.get('t');
    const start = t ? parseInt(t, 10) : 0;
    return `https://www.youtube-nocookie.com/embed/${id}${start ? `?start=${start}` : ''}`;
  }
  if (host.endsWith('bilibili.com')) {
    const m = u.pathname.match(/\/video\/(BV[0-9A-Za-z]+|av\d+)/i);
    if (!m) return null;
    const id = m[1];
    const p = u.searchParams.get('p');
    const q = id.toLowerCase().startsWith('av') ? `aid=${id.slice(2)}` : `bvid=${id}`;
    return `https://player.bilibili.com/player.html?${q}${p ? `&p=${p}` : ''}&high_quality=1&autoplay=0`;
  }
  if (host.endsWith('vimeo.com')) {
    const m = u.pathname.match(/\/(\d+)/);
    return m ? `https://player.vimeo.com/video/${m[1]}` : null;
  }
  return null;
}

/** 项目页 / 阅读页共用的资源小标签 */
export function resChip(res, { onClick, title } = {}) {
  const km = kindMeta(res.kind);
  const host = hostOf(res.url);
  const attrs = {
    class: `res-chip ${res.auto ? 'auto' : ''}`,
    title: title || `${km.label}${res.title && res.title !== res.url ? `：${res.title}` : ''}\n${res.url}${res.auto ? '\n（自动发现）' : ''}`,
  };
  if (onClick) {
    attrs.type = 'button';
    attrs.onclick = onClick;
    return h('button', attrs, h('span', { class: 'rc-ic' }, km.icon), h('span', { class: 'rc-t' }, km.label), host ? h('span', { class: 'rc-h' }, host) : null);
  }
  return h('a', { ...attrs, href: res.url, target: '_blank', rel: 'noopener' }, h('span', { class: 'rc-ic' }, km.icon), h('span', { class: 'rc-t' }, km.label), host ? h('span', { class: 'rc-h' }, host) : null);
}

export const KIND_OPTIONS = [{ value: '', label: '自动识别' }, ...KIND_ORDER.map((k) => ({ value: k, label: `${KINDS[k].icon} ${KINDS[k].label}` }))];
