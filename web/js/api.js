// API 封装：JSON 请求 + POST SSE 流解析（SPEC §4）
// 静态只读模式（window.PAPERDESK_STATIC === true，SPEC §12.2）：GET 映射到相对路径 JSON 文件，写操作一律拒绝。

export const STATIC = typeof window !== 'undefined' && window.PAPERDESK_STATIC === true;

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** 应用内链接：本地模式用路径，静态模式用 hash */
export function appHref(path) {
  return STATIC ? `#${path}` : path;
}

/** 当前路由路径（含 query），两种模式都能解析 */
export function currentRoute() {
  const hash = location.hash || '';
  if (hash.startsWith('#/')) return hash.slice(1);
  if (STATIC) return '/';
  return location.pathname + location.search;
}

export async function api(method, path, body) {
  if (STATIC) {
    if (method !== 'GET') throw new ApiError('只读在线版不能修改，请在本地 PaperDesk 中编辑', 405);
    return staticGet(path);
  }
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch('/api' + path, opt);
  } catch (e) {
    throw new ApiError('连不上 PaperDesk 后端，确认 server.py 还在运行', 0);
  }
  const ct = r.headers.get('content-type') || '';
  let data = null;
  try {
    data = ct.includes('json') ? await r.json() : await r.text();
  } catch {
    data = null;
  }
  if (!r.ok) {
    const msg = (data && typeof data === 'object' && data.error) || `请求失败（${r.status}）`;
    throw new ApiError(msg, r.status);
  }
  return data;
}

export const get = (p) => api('GET', p);
export const post = (p, b = {}) => api('POST', p, b);
export const put = (p, b = {}) => api('PUT', p, b);
export const patch = (p, b = {}) => api('PATCH', p, b);
export const del = (p) => api('DELETE', p);

/**
 * POST 一个返回 text/event-stream 的接口，逐条回调 JSON 事件。
 * 格式：每条 `data: <json>\n\n`（容忍 \r\n、多行 data、注释行）。
 */
export async function sse(path, body, onEvent, { signal } = {}) {
  if (STATIC) throw new ApiError('只读在线版不能使用 AI', 405);
  let r;
  try {
    r = await fetch('/api' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body || {}),
      signal,
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('连不上 PaperDesk 后端，确认 server.py 还在运行', 0);
  }
  if (!r.ok) {
    let msg = '';
    try {
      const j = await r.json();
      msg = j.error;
    } catch { /* ignore */ }
    throw new ApiError(msg || `请求失败（${r.status}）`, r.status);
  }
  if (!r.body) throw new ApiError('浏览器不支持流式响应', 0);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const flush = (chunk) => {
    const data = [];
    for (const line of chunk.split('\n')) {
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (!data.length) return;
    let ev;
    try {
      ev = JSON.parse(data.join('\n'));
    } catch {
      return;
    }
    onEvent(ev);
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n');
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      flush(chunk);
    }
  }
  buf += dec.decode();
  if (buf.trim()) flush(buf);
}

// ---------------- 静态只读数据层 ----------------
const staticCache = new Map();

async function fetchJson(rel) {
  if (staticCache.has(rel)) return staticCache.get(rel);
  const p = (async () => {
    let r;
    try {
      r = await fetch(rel, { cache: 'no-cache' });
    } catch {
      throw new ApiError(`读取 ${rel} 失败（网络错误）`, 0);
    }
    if (!r.ok) throw new ApiError(r.status === 404 ? '找不到这份数据（可能还没同步到 GitHub）' : `读取 ${rel} 失败（${r.status}）`, r.status);
    return r.json();
  })();
  staticCache.set(rel, p);
  p.catch(() => staticCache.delete(rel));
  return p;
}

const enc = (s) => s.split('/').map(encodeURIComponent).join('/');

export function staticLibrary() {
  return fetchJson('library.json');
}

async function staticProject(pid) {
  return fetchJson(`projects/${enc(pid)}/project.json`);
}

async function staticGet(path) {
  const [p, qs] = path.split('?');
  const q = new URLSearchParams(qs || '');
  let m;
  if (p === '/projects') {
    const lib = await staticLibrary();
    return lib.projects || [];
  }
  if ((m = p.match(/^\/projects\/([^/]+)$/))) return staticProject(decodeURIComponent(m[1]));
  if ((m = p.match(/^\/papers\/([^/]+)\/([^/]+)$/))) {
    const pid = decodeURIComponent(m[1]);
    const iid = decodeURIComponent(m[2]);
    const proj = await staticProject(pid);
    const item = (proj.items || []).find((x) => x.id === iid);
    if (!item) throw new ApiError('找不到这篇论文', 404);
    const key = item.paper_key;
    try {
      const st = await fetchJson(`projects/${enc(pid)}/papers/${enc(key)}.json`);
      return { ...st, text_cached: true };
    } catch (e) {
      if (e.status !== 404) throw e;
      return {
        pid, iid, paper_key: key,
        meta: { title: item.title, authors: item.authors, url: item.url, pdf_url: item.pdf_url, arxiv_id: item.arxiv_id, year: item.year, abstract: item.abstract },
        annotations: [], notes: [], qa: [], translations: [], vocab: [], summary: null, sessions: {}, status: item.status, text_cached: true,
      };
    }
  }
  if (p === '/vocab') {
    const list = await fetchJson('vocabulary.json').catch(() => []);
    const mastered = q.get('mastered');
    if (mastered === '0') return list.filter((v) => !v.mastered);
    if (mastered === '1') return list.filter((v) => v.mastered);
    return list;
  }
  if (p === '/settings') {
    const lib = await staticLibrary().catch(() => ({}));
    return { app_url: lib.app_url || 'http://localhost:8765', target_lang: 'zh-CN', default_engine: 'claude', translate_engine: 'google' };
  }
  if (p === '/health') return { ok: true, static: true };
  if (p === '/sync/status') return null;
  throw new ApiError('只读在线版不支持这个操作', 404);
}

/** 静态模式下论文 Markdown 的相对地址；本地模式走 API */
export function markdownUrl(base, pid, paperKey) {
  if (STATIC) return `projects/${enc(pid)}/papers/${enc(paperKey)}.md`;
  return `/api${base}/markdown`;
}

// 简单的全局缓存（设置、健康状态、同步状态）
export const cache = {
  settings: null,
  health: null,
  sync: null,
  library: null,
};

export async function loadSettings(force = false) {
  if (!cache.settings || force) cache.settings = await get('/settings');
  return cache.settings;
}

export async function loadHealth(force = false) {
  if (!cache.health || force) cache.health = await get('/health');
  return cache.health;
}

/** 可分享的阅读链接：本地 → app_url/r/..；静态 → 当前站点 #/r/.. */
export function readerUrl(pid, iid) {
  const path = `/r/${encodeURIComponent(pid)}/${encodeURIComponent(iid)}`;
  if (STATIC) return `${location.href.split('#')[0]}#${path}`;
  const base = (cache.settings && cache.settings.app_url) || location.origin;
  return `${base.replace(/\/$/, '')}${path}`;
}

/** 本地可编辑版的链接（静态横幅用） */
export function localUrl(path) {
  const base = ((cache.settings && cache.settings.app_url) || 'http://localhost:8765').replace(/\/$/, '');
  return `${base}${path || '/'}`;
}

/** 由 remote url 推断 GitHub 网页地址，如 git@github.com:u/r.git → https://github.com/u/r */
export function githubWebBase(remote) {
  if (!remote) return null;
  const m = remote.match(/github\.com[:/]+([^/]+)\/([^/]+?)(\.git)?\/?$/);
  if (!m) return null;
  return `https://github.com/${m[1]}/${m[2]}`;
}
