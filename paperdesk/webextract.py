"""网页正文抽取：纯标准库实现的简化版 Readability。

公共函数：
    decode_html(data: bytes, content_type="") -> str
        按 BOM → HTTP 头 → <meta charset> 的顺序判断编码（gb2312/gbk 统一按 gb18030 解码）。
    extract_article(html_text: str, base_url: str) -> dict
        返回 {"title","byline","site_name","published","lang","description",
              "html": 清洗后的正文 HTML（白名单标签）, "text": 纯文本（段落间空行）,
              "word_count", "weak": 正文是否太短（<200 字符）}

处理流程：解析成轻量 DOM → 公式转写（MathJax script / KaTeX annotation / MathML alttext → $…$）
→ 去掉脚本、导航、侧栏、页脚、评论、分享等噪声 → 选正文容器（<article>/<main>/常见正文类，
否则按「段落文本 − 链接密度」打分）→ 按白名单输出 HTML，相对链接与懒加载图片转成绝对地址。
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
from html.parser import HTMLParser

# ------------------------------------------------------------------ 轻量 DOM
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}
_CLOSES_P = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figcaption",
             "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "menu",
             "nav", "ol", "p", "pre", "section", "table", "ul"}


class Node(object):
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag=None, attrs=None, parent=None, text=None):
        self.tag = tag            # None 表示文本节点
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.text = text

    def get(self, k, default=""):
        v = self.attrs.get(k)
        return default if v is None else v

    def classid(self):
        return (self.get("class") + " " + self.get("id")).strip()

    def iter(self):
        """先序遍历所有元素节点（含自身）。"""
        stack = [self]
        while stack:
            n = stack.pop()
            if n.tag is not None:
                yield n
                stack.extend(reversed(n.children))

    def find_all(self, tag):
        return [n for n in self.iter() if n.tag == tag]

    def replace_with(self, new_nodes):
        p = self.parent
        if p is None:
            return
        i = p.children.index(self)
        for nn in new_nodes:
            nn.parent = p
        p.children[i:i + 1] = list(new_nodes)
        self.parent = None

    def remove(self):
        self.replace_with([])

    def has_ancestor(self, tags):
        p = self.parent
        while p is not None:
            if p.tag in tags:
                return True
            p = p.parent
        return False


def _text_node(t, parent=None):
    return Node(None, None, parent, t)


class _Builder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    @property
    def cur(self):
        return self.stack[-1]

    def _pop_to(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return True
        return False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _CLOSES_P and self.cur.tag == "p":
            self.stack.pop()
        if tag == "li":
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in ("ul", "ol"):
                    break
                if t == "li":
                    del self.stack[i:]
                    break
        if tag in ("td", "th"):
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in ("tr", "table"):
                    break
                if t in ("td", "th"):
                    del self.stack[i:]
                    break
        if tag == "tr":
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in ("table", "tbody", "thead", "tfoot"):
                    break
                if t == "tr":
                    del self.stack[i:]
                    break
        d = {}
        for k, v in attrs:
            if k:
                d[k.lower()] = v if v is not None else ""
        n = Node(tag, d, self.cur)
        self.cur.children.append(n)
        if tag not in _VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        d = {}
        for k, v in attrs:
            if k:
                d[k.lower()] = v if v is not None else ""
        self.cur.children.append(Node(tag, d, self.cur))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        self._pop_to(tag)

    def handle_data(self, data):
        if not data:
            return
        c = self.cur
        if c.children and c.children[-1].tag is None:
            c.children[-1].text += data
        else:
            c.children.append(_text_node(data, c))


def parse_html(text):
    b = _Builder()
    try:
        b.feed(text or "")
        b.close()
    except Exception:
        pass
    return b.root


def text_of(node, keep_ws=False):
    parts = []

    def walk(n):
        if n.tag is None:
            parts.append(n.text)
            return
        if n.tag in ("script", "style", "noscript", "template"):
            return
        if n.tag == "br":
            parts.append("\n")
        for c in n.children:
            walk(c)
    walk(node)
    s = "".join(parts)
    return s if keep_ws else " ".join(s.split())


# ------------------------------------------------------------------ 编码
_CS_ALIAS = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "gb_2312-80": "gb18030",
             "big5": "big5hkscs", "shift_jis": "cp932", "x-sjis": "cp932", "iso-8859-1": "cp1252",
             "latin1": "cp1252", "us-ascii": "utf-8", "ascii": "utf-8"}


def _norm_cs(cs):
    cs = (cs or "").strip().strip("\"'").lower()
    return _CS_ALIAS.get(cs, cs)


def decode_html(data, content_type=""):
    if not isinstance(data, (bytes, bytearray)):
        return data or ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    cands = []
    m = re.search(r"charset=([\w\-:.]+)", content_type or "", re.I)
    if m:
        cands.append(_norm_cs(m.group(1)))
    head = bytes(data[:8192])
    m = re.search(br"<meta[^>]+charset\s*=\s*[\"']?\s*([\w\-:.]+)", head, re.I)
    if m:
        cands.append(_norm_cs(m.group(1).decode("ascii", "ignore")))
    cands += ["utf-8"]
    for cs in cands:
        if not cs:
            continue
        try:
            return bytes(data).decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    for cs in ("gb18030",):
        try:
            return bytes(data).decode(cs)
        except UnicodeDecodeError:
            pass
    return bytes(data).decode("utf-8", errors="replace")


# ------------------------------------------------------------------ 元数据
def _meta_map(root):
    out = {}
    for m in root.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop")).strip().lower()
        val = m.get("content").strip()
        if key and val and key not in out:
            out[key] = _html.unescape(val)
    return out


def _clean_title(title, site_name):
    t = " ".join((title or "").split())
    if site_name and t:
        for sep in (" | ", " - ", " — ", " – ", " · ", " :: ", "｜", "_"):
            if t.endswith(sep + site_name):
                t = t[: -len(sep + site_name)].strip()
                break
            if t.startswith(site_name + sep):
                t = t[len(site_name + sep):].strip()
                break
    return t


# ------------------------------------------------------------------ 公式
def _tex_from_annotation(node):
    for a in node.find_all("annotation"):
        if "tex" in a.get("encoding").lower():
            return text_of(a, keep_ws=True).strip()
    return None


def _convert_math(root):
    # 1) MathJax v2：<script type="math/tex[; mode=display]">
    for s in list(root.find_all("script")):
        typ = s.get("type").lower()
        if typ.startswith("math/tex"):
            tex = "".join(c.text for c in s.children if c.tag is None).strip()
            if not tex:
                s.remove()
                continue
            disp = "mode=display" in typ.replace(" ", "")
            s.replace_with([_text_node("$$%s$$" % tex if disp else "$%s$" % tex)])
    # 2) KaTeX：.katex-display / .katex 里取 annotation
    for n in list(root.iter()):
        if n.parent is None and n is not root:
            continue
        cls = " " + n.get("class") + " "
        if " katex-display " in cls:
            tex = _tex_from_annotation(n)
            if tex is not None:
                n.replace_with([_text_node("$$%s$$" % tex)])
    for n in list(root.iter()):
        if n.parent is None and n is not root:
            continue
        cls = " " + n.get("class") + " "
        if " katex " in cls or " katex-mathml " in cls:
            tex = _tex_from_annotation(n)
            if tex is not None:
                n.replace_with([_text_node("$%s$" % tex)])
    # 3) 独立 MathML：alttext 或 annotation
    for n in list(root.find_all("math")):
        if n.parent is None:
            continue
        tex = n.get("alttext").strip() or _tex_from_annotation(n)
        disp = n.get("display") == "block"
        if tex:
            n.replace_with([_text_node("$$%s$$" % tex if disp else "$%s$" % tex)])
    # 4) MathJax 渲染产物：已经有 script 源时直接丢掉；MathJax 3 的 mjx-container 没有源 → 占位
    for n in list(root.iter()):
        if n.parent is None:
            continue
        cls = n.get("class")
        if n.tag == "mjx-container":
            n.replace_with([_text_node("[公式]")])
        elif re.search(r"(^|\s)(MathJax(_Preview|_Display|_SVG(_Display)?|_CHTML)?|MJX_Assistive_MathML)(\s|$)", cls):
            n.remove()


# ------------------------------------------------------------------ 清洗
_JUNK_TAGS = {"script", "style", "noscript", "template", "iframe", "svg", "canvas", "form", "button", "input",
              "select", "textarea", "nav", "footer", "aside", "object", "embed", "link", "meta", "head", "title",
              "dialog", "label", "audio", "video", "map"}
_STRONG_JUNK = re.compile(r"comment|sidebar|footer|share|sharing|social|related|advert|promo|cookie|subscribe|"
                          r"newsletter|breadcrumb|disqus|sponsor|popup|modal|signup|login|recommend", re.I)
_WEAK_JUNK = re.compile(r"(^|[\s_\-])(nav|navbar|menu|banner|widget|toolbar|masthead|pagination|pager|tags?|"
                        r"meta|author-box|ad|ads|header|topbar|site-header|skip)([\s_\-]|$)", re.I)
_OK_RX = re.compile(r"article|body|content|entry|main|post|text|story|blog|markdown|prose", re.I)
_PROTECT = {"html", "body", "article", "main", "#root"}


def _is_hidden(n):
    st = n.get("style").replace(" ", "").lower()
    if "display:none" in st or "visibility:hidden" in st:
        return True
    if "hidden" in n.attrs and n.tag not in ("input",):
        return True
    return False


def _clean(root, total_len):
    for n in list(root.iter()):
        if n is root or n.parent is None:
            continue
        tag = n.tag
        if tag in _JUNK_TAGS:
            n.remove()
            continue
        if tag in _PROTECT:
            continue
        if _is_hidden(n):
            n.remove()
            continue
        if tag == "header" and not n.has_ancestor({"article", "main"}):
            n.remove()
            continue
        ci = n.classid()
        if not ci:
            continue
        junk = bool(_STRONG_JUNK.search(ci)) or (bool(_WEAK_JUNK.search(ci)) and not _OK_RX.search(ci))
        if not junk:
            continue
        # 安全网：包含大半正文的容器不删（多半是外层包装）
        if total_len > 1000 and len(text_of(n)) > 0.6 * total_len:
            continue
        n.remove()


# ------------------------------------------------------------------ 选正文
_CONTENT_RX = re.compile(r"(^|[\s_\-])(post-content|post-body|post-text|entry-content|entry-body|article-body|"
                         r"article-content|article__body|markdown-body|rich-text|prose|blog-post|post|content|"
                         r"entry|article|story|main-content|page-content)([\s_\-]|$)", re.I)
_PARA_TAGS = {"p", "pre", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "td", "figcaption", "dd"}


def _link_len(n):
    return sum(len(text_of(a)) for a in n.find_all("a"))


_COUNTED = {"p", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "li", "figcaption", "dd", "td"}


def _para_len(n):
    """段落文本总长（嵌套的段落只算一次），再乘以 (1 - 链接密度)。"""
    total = 0
    stack = [(c, False) for c in n.children]
    while stack:
        x, inside = stack.pop()
        if x.tag is None:
            continue
        counted = x.tag in _COUNTED and not inside
        if x.tag == "li" and any(c.tag in ("p", "pre", "blockquote") for c in x.children):
            counted = False
        if counted:
            total += len(text_of(x))
        stack.extend((c, inside or counted) for c in x.children)
    alln = max(1, len(text_of(n)))
    if total == 0:
        total = alln
    return total * (1 - min(0.9, float(_link_len(n)) / alln))


def _depth(n):
    d = 0
    p = n.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _pick_container(body):
    cands = []
    for n in body.iter():
        if n is body:
            continue
        if n.tag in ("article", "main") or n.get("role") == "main" or n.get("itemprop") == "articleBody":
            cands.append(n)
        elif n.tag in ("div", "section") and _CONTENT_RX.search(n.classid()):
            cands.append(n)
    scored = [(c, _para_len(c)) for c in cands]
    scored = [x for x in scored if x[1] >= 250]
    if scored:
        best = max(s for _, s in scored)
        good = [c for c, s in scored if s >= 0.85 * best]
        good.sort(key=_depth, reverse=True)
        return good[0]
    # Readability 式打分：段落分数加到父节点，一半加到祖父节点
    scores = {}
    nodes = {}

    def add(node, sc):
        nodes[id(node)] = node
        scores[id(node)] = scores.get(id(node), 0.0) + sc

    for p in body.iter():
        if p.tag not in ("p", "pre", "td", "blockquote"):
            continue
        t = text_of(p)
        if len(t) < 25:
            continue
        sc = 1 + t.count(",") + t.count("，") + min(len(t) / 100.0, 3)
        if p.parent is not None:
            add(p.parent, sc)
            if p.parent.parent is not None:
                add(p.parent.parent, sc / 2.0)
    best, best_sc = None, 0.0
    for k, sc in scores.items():
        node = nodes[k]
        alln = max(1, len(text_of(node)))
        sc = sc * (1 - min(0.9, float(_link_len(node)) / alln))
        if sc > best_sc:
            best, best_sc = node, sc
    return best or body


# ------------------------------------------------------------------ 输出
_KEEP = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote", "pre", "code", "img", "figure",
         "figcaption", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "a", "em", "strong", "b", "i", "u",
         "sup", "sub", "br", "hr", "dl", "dt", "dd", "caption", "del", "s", "mark", "kbd"}
_BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote", "pre", "figure", "figcaption",
          "table", "thead", "tbody", "tfoot", "tr", "th", "td", "hr", "dl", "dt", "dd", "caption", "div", "section",
          "article", "main", "header", "center", "details", "summary", "address"}
_CONTAINER_AS_P = {"div", "section", "article", "main", "header", "center", "details", "summary", "address"}


def _has_block(n):
    for c in n.iter():
        if c is not n and (c.tag in _BLOCK or c.tag == "img"):
            return True
    return False


def _abs(base, u):
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("#"):
        return u
    low = u.lower()
    if low.startswith(("javascript:", "vbscript:", "data:text")):
        return ""
    try:
        return urllib.parse.urljoin(base, u)
    except ValueError:
        return ""


def _img_src(n, base):
    cand = None
    for k in ("data-src", "data-original", "data-lazy-src", "data-lazyload", "data-actualsrc", "data-url"):
        if n.get(k).strip():
            cand = n.get(k)
            break
    src = n.get("src").strip()
    if not cand and src and not src.lower().startswith("data:"):
        cand = src
    if not cand:
        ss = (n.get("data-srcset") or n.get("srcset")).strip()
        if ss:
            cand = ss.split(",")[0].strip().split(" ")[0]
    if not cand:
        return ""
    if n.get("width").strip() in ("0", "1") or n.get("height").strip() in ("0", "1"):
        return ""
    return _abs(base, cand)


class _Out(object):
    def __init__(self, base):
        self.base = base
        self.h = []          # HTML 片段
        self.t = []          # 文本片段（"\n\n" 表示段落边界）

    def text(self, s):
        self.t.append(s)

    def block(self):
        self.t.append("\n\n")


def _esc(s):
    return _html.escape(s, quote=False)


def _attr(k, v):
    return ' %s="%s"' % (k, _html.escape(v, quote=True))


def _ser(n, out, in_pre=False):
    if n.tag is None:
        txt = n.text
        if not in_pre:
            txt = re.sub(r"\s+", " ", txt)
        if txt:
            out.h.append(_esc(txt))
            out.text(txt)
        return
    tag = n.tag
    if tag in ("script", "style", "noscript", "template", "source"):
        return
    if tag == "br":
        out.h.append("<br>")
        out.text("\n")
        return
    if tag == "hr":
        out.h.append("<hr>")
        out.block()
        return
    if tag == "img":
        src = _img_src(n, out.base)
        if not src:
            return
        s = "<img" + _attr("src", src)
        if n.get("alt"):
            s += _attr("alt", n.get("alt"))
        if n.get("title"):
            s += _attr("title", n.get("title"))
        out.h.append(s + ">")
        return
    if tag == "picture":
        imgs = n.find_all("img")
        if imgs:
            _ser(imgs[0], out, in_pre)
        return
    if tag in _KEEP:
        attrs = ""
        if tag == "a":
            href = _abs(out.base, n.get("href"))
            if href:
                attrs += _attr("href", href)
            if n.get("title"):
                attrs += _attr("title", n.get("title"))
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if n.get(k).strip().isdigit():
                    attrs += _attr(k, n.get(k).strip())
        elif tag == "ol" and n.get("start").strip().isdigit():
            attrs += _attr("start", n.get("start").strip())
        elif tag in ("code", "pre"):
            m = re.search(r"(?:language|lang)-([\w+#\-]+)", n.get("class"))
            if m:
                attrs += _attr("class", "language-" + m.group(1))
        is_block = tag in _BLOCK
        if is_block:
            out.block()
        if tag == "li":
            out.text("• ")
        start = len(out.h)
        out.h.append("<%s%s>" % (tag, attrs))
        tstart = len(out.t)
        for c in n.children:
            _ser(c, out, in_pre or tag == "pre")
        # 删除空段落 / 空链接
        inner_text = "".join(x for x in out.t[tstart:] if x not in ("\n\n",)).strip()
        inner_html = "".join(out.h[start + 1:])
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption", "a", "em", "strong",
                   "b", "i", "u", "span", "mark") and not inner_text and "<img" not in inner_html:
            del out.h[start:]
            del out.t[tstart:]
            return
        out.h.append("</%s>" % tag)
        if is_block:
            out.block()
        elif tag in ("td", "th"):
            out.text("\t")
        return
    # 容器 / 未知标签：拆包装
    if tag in _CONTAINER_AS_P and not in_pre and not _has_block(n) and text_of(n):
        out.block()
        start = len(out.h)
        out.h.append("<p>")
        tstart = len(out.t)
        for c in n.children:
            _ser(c, out, in_pre)
        if not "".join(out.t[tstart:]).strip():
            del out.h[start:]
            del out.t[tstart:]
            return
        out.h.append("</p>")
        out.block()
        return
    if tag in _BLOCK:
        out.block()
    for c in n.children:
        _ser(c, out, in_pre)
    if tag in _BLOCK:
        out.block()


def _norm_text(parts):
    s = "".join(parts)
    s = s.replace("\r", "")
    s = re.sub(r"[ \t ]+\n", "\n", s)
    s = re.sub(r"\n[ \t ]+(?=\S)", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _norm_html(s):
    s = re.sub(r"(<br>\s*){3,}", "<br><br>", s)
    s = re.sub(r"<p>\s*(<br>\s*)*</p>", "", s)
    return s.strip()


def word_count(text):
    return len(re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*", text or "")) + \
        len(re.findall(r"[㐀-鿿豈-﫿]", text or ""))


def extract_article(html_text, base_url):
    root = parse_html(html_text or "")
    # <base href>
    base = base_url or ""
    for b in root.find_all("base"):
        if b.get("href"):
            base = _abs(base_url, b.get("href")) or base
            break
    meta = _meta_map(root)
    htmls = root.find_all("html")
    lang = (htmls[0].get("lang") if htmls else "") or meta.get("og:locale", "")
    title_nodes = [t for t in root.find_all("title") if not t.has_ancestor({"svg"})]
    raw_title = text_of(title_nodes[0]) if title_nodes else ""
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    site_name = meta.get("og:site_name") or meta.get("application-name") or host
    title = meta.get("og:title") or meta.get("twitter:title") or _clean_title(raw_title, site_name)
    byline = meta.get("author") or meta.get("article:author") or meta.get("twitter:creator") or ""
    published = (meta.get("article:published_time") or meta.get("date") or meta.get("pubdate")
                 or meta.get("publish-date") or meta.get("dc.date") or meta.get("datepublished") or "")
    description = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""
    if not byline:
        for n in root.iter():
            if n.get("rel") == "author" or re.search(r"(^|[\s_\-])(byline|author-name|post-author)([\s_\-]|$)",
                                                      n.classid(), re.I):
                t = text_of(n)
                if 0 < len(t) <= 80:
                    byline = t
                    break
    if not published:
        for n in root.find_all("time"):
            if n.get("datetime"):
                published = n.get("datetime")
                break

    bodies = root.find_all("body")
    body = bodies[0] if bodies else root
    _convert_math(body)
    total_len = len(text_of(body))
    _clean(body, total_len)
    cont = _pick_container(body)
    if not title:
        h1 = cont.find_all("h1") or body.find_all("h1")
        title = text_of(h1[0]) if h1 else (raw_title or base_url or "")
    out = _Out(base)
    for c in cont.children:
        _ser(c, out)
    html_out = _norm_html("".join(out.h))
    text_out = _norm_text(out.t)
    return {
        "title": " ".join((title or "").split())[:300],
        "byline": " ".join((byline or "").split())[:120],
        "site_name": site_name,
        "published": published[:40],
        "lang": lang,
        "description": " ".join(description.split())[:500],
        "html": html_out,
        "text": text_out,
        "word_count": word_count(text_out),
        "weak": len(text_out) < 200,
    }


def page_meta(html_text, base_url=""):
    """只看 <head>：标题 / 站点名 / 描述（resolve 用，速度优先）。"""
    root = parse_html(html_text or "")
    meta = _meta_map(root)
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    site = meta.get("og:site_name") or meta.get("application-name") or host
    tn = root.find_all("title")
    raw = text_of(tn[0]) if tn else ""
    title = meta.get("og:title") or meta.get("twitter:title") or _clean_title(raw, site)
    desc = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""
    return {"title": " ".join((title or "").split())[:300] or None, "site_name": site,
            "description": " ".join(desc.split())[:500]}
