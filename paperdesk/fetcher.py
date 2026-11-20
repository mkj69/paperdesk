"""arXiv / URL 解析、元数据获取、PDF 流式代理（绝不写磁盘）。

公共函数：
    parse_arxiv_id(text) -> str | None          # 保留版本号，如 "2406.12345v2"
    strip_version(arxiv_id) -> str
    paper_key_for(arxiv_id=None, url=None) -> str
    fetch_arxiv_meta(arxiv_id, timeout=10) -> dict | None
    resolve(text) -> dict                       # 见 SPEC §4 /api/resolve
    open_upstream(url, timeout=60) -> (response, content_length|None)
"""
from __future__ import annotations

import hashlib
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import VERSION

USER_AGENT = "PaperDesk/%s (+local paper reader)" % VERSION

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


def _request(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
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
    return {"title": title, "authors": authors, "abstract": abstract, "year": year}


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
            return {"type": "paper", "title": title, "url": forum, "pdf_url": pdf_url,
                    "arxiv_id": None, "authors": [], "year": None, "abstract": None,
                    "paper_key": paper_key_for(url=pdf_url)}

    def paper_from_pdf(url):
        return {"type": "paper", "title": _filename_title(url), "url": url, "pdf_url": url,
                "arxiv_id": None, "authors": [], "year": None, "abstract": None,
                "paper_key": paper_key_for(url=url)}

    if parsed.path.lower().endswith(".pdf"):
        return paper_from_pdf(t)

    ctype = ""
    try:
        ctype = _http_head_ctype(t)
    except Exception:
        ctype = ""
    if "application/pdf" in ctype.lower():
        return paper_from_pdf(t)

    title = None
    if not ctype or "html" in ctype.lower():
        try:
            data, ct2 = _http_get(t, timeout=8, limit=512 * 1024)
            if "application/pdf" in (ct2 or "").lower() or data[:5] == b"%PDF-":
                return paper_from_pdf(t)
            title = _title_from_html(data)
        except Exception:
            title = None
    return {"type": "link", "title": title or t, "url": t, "pdf_url": None, "arxiv_id": None,
            "authors": [], "year": None, "abstract": None, "paper_key": None}


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
