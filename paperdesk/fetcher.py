"""arXiv / URL 解析、元数据获取、PDF 流式代理（绝不写磁盘）。

公共函数：
    parse_arxiv_id(text) -> str | None          # 保留版本号，如 "2406.12345v2"
    strip_version(arxiv_id) -> str
    paper_key_for(arxiv_id=None, url=None) -> str
    fetch_arxiv_meta(arxiv_id, timeout=10) -> dict | None
    resolve(text) -> dict                       # 见 SPEC §4 /api/resolve 与 §13（web 类型、kind、resources）
    open_upstream(url, timeout=60) -> (response, content_length|None)

  网页与多资源（SPEC §13）
    classify(url) -> str                        # paper|code|video|dataset|model|demo|slides|blog|homepage
    web_key_for(url) -> str                     # "web-<sha1[:10]>"
    github_repo_of(url) -> (owner, repo) | None
    split_inputs(text) -> list[str]             # 输入框里的多个 URL / arXiv ID
    discover_links(text) -> list[dict]          # 从 arXiv 摘要/评论里找代码、主页等 → [{kind,url,title,auto}]
    arxiv_ids_in(text) -> list[str]
    fetch_web(url, timeout=30) -> dict          # /api/web：正文抽取或 GitHub README
    fetch_github_readme(owner, repo, timeout=20) -> dict
    extract_article(html, base_url) -> dict     # 见 webextract.py
"""
from __future__ import annotations

import hashlib
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import json
import os
import xml.etree.ElementTree as ET

from . import VERSION
from .webextract import decode_html, extract_article, page_meta, word_count  # noqa: F401

USER_AGENT = "PaperDesk/%s (+local paper reader)" % VERSION
# 抓网页正文时用接近浏览器的 UA（有些博客平台会拒绝陌生 UA）
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 PaperDesk/%s" % VERSION)
WEB_LIMIT = 8 * 1024 * 1024

_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ID_RE = re.compile(r"^(?:arxiv:\s*)?(%s|%s)$" % (_NEW_ID, _OLD_ID), re.I)
_URL_ID_RE = re.compile(
    r"^https?://(?:www\.|export\.)?(?:arxiv\.org|alphaxiv\.org)/(?:abs|pdf|html|overview)/(%s|%s)(?:\.pdf)?/?(?:[?#].*)?$"
    % (_NEW_ID, _OLD_ID),
    re.I,
)


class FetchError(Exception):
    pass


def parse_arxiv_id(text):
    if not text:
        return None
    t = text.strip()
    m = _ID_RE.match(t)
    if m:
        return m.group(1)
    m = _URL_ID_RE.match(t)
    if m:
        return m.group(1)
    return None


def strip_version(arxiv_id):
    return re.sub(r"v\d+$", "", arxiv_id or "")


def paper_key_for(arxiv_id=None, url=None):
    if arxiv_id:
        return "arxiv-" + strip_version(arxiv_id).replace("/", "_")
    return "url-" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url, method="GET", timeout=10, headers=None):
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=hdrs)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        # 本机地址永远不走代理
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_get(url, timeout=10, limit=2 * 1024 * 1024):
    """返回 (bytes, content_type)。"""
    with _request(url, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(limit)
    return data, ctype


def _http_head_ctype(url, timeout=6):
    try:
        with _request(url, method="HEAD", timeout=timeout) as r:
            return r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 501):
            # 部分站点不支持 HEAD，用 GET 只看响应头
            with _request(url, timeout=timeout) as r:
                return r.headers.get("Content-Type", "")
        raise


_ATOM = "{http://www.w3.org/2005/Atom}"


def fetch_arxiv_meta(arxiv_id, timeout=10):
    url = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(arxiv_id)
    try:
        data, _ = _http_get(url, timeout=timeout)
        root = ET.fromstring(data)
    except Exception:
        return None
    entry = root.find(_ATOM + "entry")
    if entry is None:
        return None
    title_el = entry.find(_ATOM + "title")
    title = " ".join((title_el.text or "").split()) if title_el is not None else ""
    if not title or title.lower() == "error":
        return None
    authors = []
    for a in entry.findall(_ATOM + "author"):
        n = a.find(_ATOM + "name")
        if n is not None and n.text:
            authors.append(n.text.strip())
    summ = entry.find(_ATOM + "summary")
    abstract = " ".join((summ.text or "").split()) if summ is not None else ""
    pub = entry.find(_ATOM + "published")
    year = (pub.text or "")[:4] if pub is not None and pub.text else None
    com = entry.find("{http://arxiv.org/schemas/atom}comment")
    comment = " ".join((com.text or "").split()) if com is not None and com.text else ""
    return {"title": title, "authors": authors, "abstract": abstract, "year": year, "comment": comment}


def _arxiv_result(arxiv_id):
    base = strip_version(arxiv_id)
    res = {
        "type": "paper",
        "title": "arXiv:" + arxiv_id,
        "url": "https://arxiv.org/abs/" + base,
        "pdf_url": "https://arxiv.org/pdf/" + arxiv_id,
        "arxiv_id": base,
        "authors": [],
        "year": None,
        "abstract": None,
        "paper_key": paper_key_for(arxiv_id=base),
        "kind": "paper",
        "resources": [],
    }
    if re.match(r"^\d{4}\.", base):
        yy = int(base[:2])
        res["year"] = str(2000 + yy) if yy < 90 else str(1900 + yy)
    meta = fetch_arxiv_meta(base)
    if meta:
        res["title"] = meta["title"]
        res["authors"] = meta["authors"]
        res["abstract"] = meta["abstract"] or None
        res["year"] = meta["year"] or res["year"]
        res["resources"] = discover_links((meta.get("abstract") or "") + "\n" + (meta.get("comment") or ""))
    return res


def _title_from_html(data):
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)", text, re.I)
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        t = html.unescape(" ".join(m.group(1).split()))
        return t[:300] or None
    return None


def _filename_title(url):
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1]) or url
    return name


# ----------------------------------------------------------------- 资源分类 / 发现（SPEC §13.2）
RESOURCE_KINDS = ("paper", "code", "homepage", "blog", "video", "slides", "dataset", "model", "demo", "other")

_GH_RESERVED = {"features", "topics", "orgs", "settings", "marketplace", "sponsors", "login", "explore", "about",
                "pricing", "collections", "trending", "enterprise", "apps", "site", "customer-stories", "security",
                "readme", "notifications", "search", "new", "join", "events", "contact", "team", "codespaces"}
_GH_RX = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/?#].*)?$",
                    re.I)


def web_key_for(url):
    return "web-" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]


def github_repo_of(url):
    m = _GH_RX.match((url or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in _GH_RESERVED or not repo or repo in (".", ".."):
        return None
    return owner, repo


def _host_path(url):
    p = urllib.parse.urlparse(url or "")
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host[2:] in ("youtube.com", "bilibili.com", "medium.com"):
        host = host[2:]
    return host, p.path or "/"


def classify(url):
    """按 SPEC §13.2 判断链接属于哪类资源。"""
    u = (url or "").strip()
    if parse_arxiv_id(u):
        return "paper"
    host, path = _host_path(u)
    lpath = path.lower()
    segs = [x for x in path.split("/") if x]
    if host.endswith("openreview.net") or lpath.endswith(".pdf") or host in ("dl.acm.org", "aclanthology.org") \
            and (lpath.endswith(".pdf") or "/doi/pdf" in lpath):
        return "paper"
    if host in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitee.com"):
        if host == "github.com" and segs and segs[0].lower() in _GH_RESERVED:
            return "homepage"
        return "code" if len(segs) >= 2 else "homepage"
    if host in ("youtube.com", "youtu.be", "bilibili.com", "b23.tv", "vimeo.com") or host.endswith(".bilibili.com") \
            or host.endswith(".youtube.com"):
        return "video"
    if host in ("huggingface.co", "hf.co"):
        if segs and segs[0] == "datasets":
            return "dataset"
        if segs and segs[0] == "spaces":
            return "demo"
        if segs and segs[0] == "papers":
            return "paper"
        return "model" if len(segs) >= 2 else "homepage"
    if host in ("speakerdeck.com", "slideshare.net") or (host == "docs.google.com" and lpath.startswith("/presentation")) \
            or lpath.endswith((".pptx", ".ppt", ".key")):
        return "slides"
    if host.endswith(("medium.com", "substack.com")) or host == "distill.pub" or "blog" in host \
            or re.search(r"/(blog|blogs|posts|post|p|articles|article|notes)/", lpath + "/"):
        return "blog"
    if len(segs) <= 1:
        return "homepage"
    return "blog"


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'{}\[\]|\\^`]+", re.I)
_BARE_GH = re.compile(r"(?<![\w/.])(?:www\.)?(github\.com|gitlab\.com)/([\w.\-]+)/([\w.\-]+)", re.I)
_HOMEPAGE_HINT = re.compile(r"(project\s*page|project\s*website|website|homepage|home\s*page|demo|page|site|"
                            r"项目主页|主页)\W{0,12}$", re.I)
_ARXIV_IN_TEXT = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?|[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})",
                            re.I)


def _trim_url(u):
    u = u.rstrip(".,;:!?)）】」'\"")
    if u.count("(") < u.count(")"):
        u = u.rstrip(")")
    return u


def _norm_repo_url(u):
    gh = github_repo_of(u)
    if gh:
        return "https://github.com/%s/%s" % gh
    m = re.match(r"^https?://(?:www\.)?(gitlab\.com)/([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:[/?#].*)?$", u, re.I)
    if m:
        return "https://gitlab.com/%s/%s" % (m.group(2), m.group(3))
    return u


def discover_links(text):
    """从 arXiv 摘要 / comment 里找代码仓库、项目主页、模型、视频等（auto 资源）。"""
    out, seen = [], set()
    t = text or ""

    def push(kind, url):
        key = url.rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "url": url, "title": "", "auto": True})

    for m in _URL_IN_TEXT.finditer(t):
        url = _trim_url(m.group(0))
        if parse_arxiv_id(url) or re.search(r"(doi\.org|arxiv\.org|creativecommons\.org)", url, re.I):
            continue
        kind = classify(url)
        if kind == "code":
            push("code", _norm_repo_url(url))
            continue
        if kind in ("video", "model", "dataset", "demo", "slides"):
            push(kind, url)
            continue
        host, _ = _host_path(url)
        before = t[max(0, m.start() - 40):m.start()]
        if host.endswith(".github.io") or _HOMEPAGE_HINT.search(before):
            push("homepage", url)
    for m in _BARE_GH.finditer(t):
        url = _trim_url("https://%s/%s/%s" % (m.group(1).lower(), m.group(2), m.group(3)))
        if m.group(1).lower() == "github.com" and not github_repo_of(url):
            continue
        push("code", _norm_repo_url(url))
    return out


def arxiv_ids_in(text):
    ids, seen = [], set()
    for m in _ARXIV_IN_TEXT.finditer(text or ""):
        aid = strip_version(m.group(1))
        if aid not in seen:
            seen.add(aid)
            ids.append(aid)
    return ids


def split_inputs(text):
    """输入框里可能一次粘贴了多个链接（换行 / 空格 / 逗号分隔）。只拆 URL 和 arXiv ID。"""
    t = (text or "").strip()
    if not t:
        return []
    toks = [x.strip().strip(",，;；") for x in re.split(r"[\s,，;；]+", t) if x.strip()]
    good = [x for x in toks if re.match(r"^(https?://|www\.)", x, re.I) or parse_arxiv_id(x)]
    if len(good) >= 2 and len(good) == len(toks):
        return good
    return [t]


# ----------------------------------------------------------------- resolve
def _web_result(url, title=None, site_name=None, description=None, kind=None):
    host, _ = _host_path(url)
    return {"type": "web", "kind": kind or classify(url), "title": title or url, "url": url, "pdf_url": None,
            "arxiv_id": None, "authors": [], "year": None, "abstract": None, "site_name": site_name or host,
            "description": description or "", "paper_key": web_key_for(url), "resources": []}


def resolve(text):
    t = (text or "").strip()
    if not t:
        raise ValueError("请输入 arXiv ID 或链接")
    aid = parse_arxiv_id(t)
    if aid:
        return _arxiv_result(aid)
    if re.match(r"^www\.", t, re.I):
        t = "https://" + t
    if not re.match(r"^https?://", t, re.I):
        raise ValueError("无法识别：请粘贴 arXiv ID（如 1706.03762）、PDF 链接或网页链接")
    parsed = urllib.parse.urlparse(t)
    host = (parsed.netloc or "").lower()

    # OpenReview
    if host.endswith("openreview.net"):
        qs = urllib.parse.parse_qs(parsed.query)
        oid = (qs.get("id") or [None])[0]
        if oid and parsed.path.startswith(("/forum", "/pdf")):
            pdf_url = "https://openreview.net/pdf?id=" + oid
            forum = "https://openreview.net/forum?id=" + oid
            title = "OpenReview " + oid
            try:
                data, _ = _http_get(forum, timeout=8, limit=256 * 1024)
                title = _title_from_html(data) or title
            except Exception:
                pass
            return {"type": "paper", "kind": "paper", "title": title, "url": forum, "pdf_url": pdf_url,
                    "arxiv_id": None, "authors": [], "year": None, "abstract": None,
                    "paper_key": paper_key_for(url=pdf_url), "resources": []}

    def paper_from_pdf(url):
        return {"type": "paper", "kind": "paper", "title": _filename_title(url), "url": url, "pdf_url": url,
                "arxiv_id": None, "authors": [], "year": None, "abstract": None,
                "paper_key": paper_key_for(url=url), "resources": []}

    if parsed.path.lower().endswith(".pdf"):
        return paper_from_pdf(t)

    # GitHub 仓库：标题 / 描述走 API，README 里找论文链接（auto 资源）
    gh = github_repo_of(t)
    if gh:
        res = _web_result(t, title="%s/%s" % gh, site_name="GitHub", kind="code")
        try:
            info = fetch_github_readme(gh[0], gh[1], timeout=6)
            res["title"] = info.get("title") or res["title"]
            res["description"] = info.get("description") or ""
            for aid2 in arxiv_ids_in(info.get("markdown") or "")[:1]:
                res["resources"].append({"kind": "paper", "url": "https://arxiv.org/abs/" + aid2,
                                         "pdf_url": "https://arxiv.org/pdf/" + aid2, "arxiv_id": aid2,
                                         "title": "arXiv:" + aid2, "auto": True})
            if info.get("homepage"):
                res["resources"].append({"kind": classify(info["homepage"]) if classify(info["homepage"]) != "blog"
                                         else "homepage", "url": info["homepage"], "title": "", "auto": True})
        except Exception:
            pass
        return res

    ctype = ""
    try:
        ctype = _http_head_ctype(t)
    except Exception:
        ctype = ""
    if "application/pdf" in ctype.lower():
        return paper_from_pdf(t)

    meta = {}
    if not ctype or "html" in ctype.lower():
        try:
            data, ct2 = _http_get(t, timeout=8, limit=512 * 1024)
            if "application/pdf" in (ct2 or "").lower() or data[:5] == b"%PDF-":
                return paper_from_pdf(t)
            meta = page_meta(decode_html(data, ct2), t)
        except Exception:
            meta = {}
    return _web_result(t, meta.get("title"), meta.get("site_name"), meta.get("description"))


# ----------------------------------------------------------------- /api/web
def _github_headers(raw=False):
    h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def fetch_github_readme(owner, repo, timeout=20):
    api = "https://api.github.com/repos/%s/%s" % (owner, repo)
    meta = {}
    meta_err = None
    try:
        with _request(api, timeout=timeout, headers=_github_headers()) as r:
            meta = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        meta_err = e.code
        if e.code == 404:
            raise FetchError("GitHub 仓库不存在，或者是私有仓库：%s/%s" % (owner, repo))
    except Exception as e:  # 网络不通
        meta_err = str(e)
    branch = (meta or {}).get("default_branch") or "HEAD"
    md = None
    try:
        with _request(api + "/readme", timeout=timeout, headers=_github_headers(raw=True)) as r:
            md = decode_html(r.read(WEB_LIMIT), r.headers.get("Content-Type", ""))
    except Exception:
        md = None
    if md is None:
        # API 限流时退回 raw 文件
        for name in ("README.md", "readme.md", "README.rst", "README"):
            try:
                with _request("https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, branch, name),
                              timeout=timeout) as r:
                    md = decode_html(r.read(WEB_LIMIT), r.headers.get("Content-Type", ""))
                    break
            except Exception:
                continue
    if md is None:
        raise FetchError("无法获取 %s/%s 的 README（%s）" % (owner, repo, meta_err or "网络不通"))
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    plain = re.sub(r"<[^>]+>", "", plain)
    return {
        "mode": "readme",
        "url": "https://github.com/%s/%s" % (owner, repo),
        "title": (meta or {}).get("full_name") or "%s/%s" % (owner, repo),
        "description": (meta or {}).get("description") or "",
        "stars": (meta or {}).get("stargazers_count"),
        "language": (meta or {}).get("language"),
        "homepage": (meta or {}).get("homepage") or "",
        "markdown": md,
        "text": plain.strip(),
        "word_count": word_count(plain),
        "site_name": "GitHub",
        "raw_base": "https://raw.githubusercontent.com/%s/%s/%s/" % (owner, repo, branch),
        "html_base": "https://github.com/%s/%s/blob/%s/" % (owner, repo, branch),
    }


def _normalize_web_url(url):
    u = (url or "").strip()
    if re.match(r"^www\.", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise FetchError("只支持 http/https 链接")
    return u


def _download_html(u, timeout):
    """下载网页，返回 (最终地址, Content-Type, bytes)。只在内存里，不落盘。"""
    try:
        r = _request(u, timeout=timeout, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    except urllib.error.HTTPError as e:
        raise FetchError("网页返回 HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise FetchError("无法打开网页：%s" % getattr(e, "reason", e))
    except Exception as e:
        raise FetchError("无法打开网页：%s" % e)
    try:
        final = r.geturl() or u
        ctype = r.headers.get("Content-Type", "") or ""
        data = r.read(WEB_LIMIT + 1)
    finally:
        r.close()
    if len(data) > WEB_LIMIT:
        raise FetchError("网页太大（超过 8MB）")
    return final, ctype, data


def fetch_page(url, timeout=30):
    """原网页模式：返回完整 HTML 文本（不抽取正文、不落盘）。脚本由前端去掉，并在禁止脚本的 iframe 里显示。"""
    u = _normalize_web_url(url)
    final, ctype, data = _download_html(u, timeout)
    low = ctype.lower()
    if "application/pdf" in low or data[:5] == b"%PDF-":
        raise FetchError("这是 PDF，不是网页")
    if low and not any(t in low for t in ("html", "xml", "text/plain")):
        raise FetchError("不是网页（Content-Type: %s）" % ctype)
    text = decode_html(data, ctype)
    if low.startswith("text/plain"):
        text = "<!doctype html><meta charset=utf-8><pre style='white-space:pre-wrap'>%s</pre>" % html.escape(text, quote=False)
    return {"url": final, "html": text}


def fetch_web(url, timeout=30):
    """抓网页并抽取正文（不落盘、不缓存）。GitHub 仓库走 README 模式。"""
    u = _normalize_web_url(url)
    gh = github_repo_of(u)
    if gh:
        return fetch_github_readme(gh[0], gh[1], timeout=min(timeout, 20))
    final, ctype, data = _download_html(u, timeout)
    if "application/pdf" in ctype.lower() or data[:5] == b"%PDF-":
        return {"mode": "pdf", "url": final, "pdf_url": final, "title": _filename_title(final),
                "html": "", "text": "", "word_count": 0, "weak": True}
    text = decode_html(data, ctype)
    low = ctype.lower()
    if low.startswith("text/plain") or low.startswith("text/markdown"):
        host, _ = _host_path(final)
        return {"mode": "article", "url": final, "title": _filename_title(final), "byline": "", "site_name": host,
                "published": "", "lang": "", "description": "",
                "html": "<pre>%s</pre>" % html.escape(text, quote=False), "text": text,
                "word_count": word_count(text), "weak": len(text) < 200}
    res = extract_article(text, final)
    res["url"] = final
    res["mode"] = "article"
    return res


def open_upstream(url, timeout=60):
    """打开上游 PDF 连接，返回 (response, content_length)。调用方负责逐块转发并关闭。"""
    p = urllib.parse.urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.netloc:
        raise FetchError("只支持 http/https 链接")
    try:
        r = _request(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise FetchError("上游返回 HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise FetchError("无法连接上游：%s" % getattr(e, "reason", e))
    except Exception as e:  # socket.timeout 等
        raise FetchError("无法连接上游：%s" % e)
    clen = r.headers.get("Content-Length")
    try:
        clen = int(clen) if clen else None
    except ValueError:
        clen = None
    return r, clen
