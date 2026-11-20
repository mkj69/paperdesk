"""文献库存储：项目 / 条目 / 论文状态 + Markdown 渲染 + README 索引。

所有函数都是「纯函数风格」，第一个参数是数据目录 data_dir（由 config.get_data_dir() 得到）。
写入全部是原子写，并由模块级 RLock 保护。找不到资源时抛 NotFound，参数错误抛 ValueError，
冲突抛 Conflict。修改后会调用通过 set_change_hook() 注册的回调（server 用来触发 git 同步）。

公共函数签名（MCP server 可直接 import 使用）：

  库与项目
    ensure_library(data_dir) -> None                         # 建目录、收件箱、.gitignore、README
    list_projects(data_dir) -> list[dict]                     # [{id,name,description,created,updated,counts}]
        counts = {"paper","web","link","note"}；其中 link = 旧 link + web（兼容旧界面），web 只算 web
    get_project(data_dir, pid) -> dict                        # 含 items
    create_project(data_dir, name, description="") -> dict
    update_project(data_dir, pid, name=None, description=None) -> dict
    delete_project(data_dir, pid) -> None                     # 移入 .trash；inbox 不可删

  条目
    add_item(data_dir, pid, item_dict) -> dict                # 返回 item（额外带 "existing": bool，不落盘）
        item_dict 可含：type("paper"|"web"|"link"|"note"), kind, title, url, pdf_url, arxiv_id, authors, year,
        abstract, paper_key, note, tags, status, site_name, description, resources
        （"link" 是旧数据的类型，行为与 "web" 相同；web/link/paper 条目都有论文状态）
    find_item(data_dir, pid, iid) -> dict
    update_item(data_dir, pid, iid, patch) -> dict            # title/tags/status/note/url
    delete_item(data_dir, pid, iid) -> None
    move_item(data_dir, pid, iid, to_pid) -> dict

  资源（一篇工作的论文 / 代码 / 主页 / 博客……，SPEC §13.2）
    add_resource(data_dir, pid, iid, res_dict) -> dict        # {url, kind?, title?, pdf_url?, arxiv_id?, auto?}，带 "existing"
    update_resource(data_dir, pid, iid, rid, patch) -> dict   # kind/title/url
    delete_resource(data_dir, pid, iid, rid) -> None          # 同时删掉该资源的标注；笔记保留、去掉锚点
    item_resources(item) -> list[dict]                        # 含主链接（id="main"）在内的全部资源

  论文状态
    get_paper_state(data_dir, pid, iid) -> dict               # 不存在则按 item 初始化并保存
    update_paper(data_dir, pid, iid, patch, notify=True) -> dict   # last_page/zoom/tts_pos/status
        （只改 last_page/zoom/tts_pos 时只写 json，不重渲染 md/README、不触发同步）
    add_annotation(data_dir, pid, iid, ann) -> dict
    update_annotation(data_dir, pid, iid, aid, patch) -> dict
    delete_annotation(data_dir, pid, iid, aid) -> None
    add_note(data_dir, pid, iid, note_dict) -> dict           # {text, page?, quote?, source?, annotation_id?}
    update_note(data_dir, pid, iid, nid, patch) -> dict
    delete_note(data_dir, pid, iid, nid) -> None
    add_qa(data_dir, pid, iid, qa_dict) -> dict
    delete_qa(data_dir, pid, iid, qid) -> None
    add_translation(data_dir, pid, iid, tr_dict) -> dict
    set_summary(data_dir, pid, iid, markdown, engine) -> dict
    set_session(data_dir, pid, iid, engine, session_id) -> None
    reset_session(data_dir, pid, iid, engine) -> None

  生词本
    lemma_of(word) -> str
    find_vocab(data_dir, pid, iid, word) -> dict | None       # 同论文同 lemma 的已有条目
    add_vocab(data_dir, pid, iid, entry) -> dict              # 已存在同词则只追加语境，返回条目（带 "merged": bool）
    update_vocab(data_dir, pid, iid, vid, patch) -> dict      # mastered/meaning/context_meaning/phonetic/pos
    delete_vocab(data_dir, pid, iid, vid) -> None             # 同时删除关联的 vocab 标注
    list_all_vocab(data_dir, mastered=None, pid=None) -> list[dict]   # 带 pid/iid/paper_title/paper_key，created 倒序
    refresh_vocabulary(data_dir) -> None                      # 重写根目录 vocabulary.md

  全文缓存 / 当前论文 / 渲染
    write_cached_text(data_dir, pid, paper_key, pages, resource_id="main", title=None, kind=None) -> int
    read_cached_text(data_dir, pid, paper_key, resource_id="main") -> list[dict] | None  # [{page,text}]
    cached_resources(data_dir, pid, paper_key) -> list[str]           # 有缓存的资源 id
    agent_text_string(data_dir, pid, item) -> str | None              # 拼好的 paper.txt 正文（PDF + 网页 + README）
    cached_text_string(data_dir, pid, paper_key, pages=None) -> str | None
    write_current(data_dir, pid, iid) -> dict
    read_current(data_dir) -> dict | None
    render_paper_markdown(state, project_name="", app_url="http://localhost:8765", pages_url="") -> str
    paper_markdown(data_dir, pid, iid) -> str
    paper_md_relpath(pid, paper_key) -> str                          # "projects/<pid>/papers/<key>.md"
    refresh_readmes(data_dir, pid=None) -> None
    rerender_all(data_dir) -> None

  多台电脑同步（git 合并时用）
    LOCK                                                        # 模块级 RLock，合并期间持有
    is_library_empty(data_dir) -> bool                          # 只有空收件箱、没有任何论文文件
    is_data_file(path) -> bool                                  # projects/*/project.json、papers/*.json、settings.json
    merge_data_file(path, base, ours, theirs) -> dict | None    # 三方语义合并（按 id），None 表示删除

  GitHub Pages 只读站
    pages_url_of(settings) -> str                               # "" 或以 / 结尾
    export_static_site(data_dir, web_dir) -> dict               # library.json / vocabulary.json / index.html /
                                                                #   404.html / .nojekyll / static/（增量拷贝）
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import shutil
import threading

from . import config

INBOX_ID = "inbox"
INBOX_NAME = "收件箱"
GITIGNORE = """# PaperDesk：以下内容只在本机，不入库
.cache/
.agent/
.trash/
settings.local.json
.current.json
.DS_Store
.tmp-*
"""

_LOCK = threading.RLock()
LOCK = _LOCK
_change_hook = None


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def set_change_hook(fn):
    global _change_hook
    _change_hook = fn


def _notify(msg):
    if _change_hook:
        try:
            _change_hook(msg)
        except Exception:
            pass


def _new_id(prefix="", n=4):
    return prefix + secrets.token_hex(n)


# ----------------------------------------------------------------- 路径
def _projects_dir(data_dir):
    return os.path.join(data_dir, "projects")


def _project_dir(data_dir, pid):
    if not re.match(r"^[a-z0-9][a-z0-9\-]*$", pid or ""):
        raise NotFound("项目不存在")
    return os.path.join(_projects_dir(data_dir), pid)


def _project_file(data_dir, pid):
    return os.path.join(_project_dir(data_dir, pid), "project.json")


def _paper_json(data_dir, pid, key):
    return os.path.join(_project_dir(data_dir, pid), "papers", key + ".json")


def _paper_md(data_dir, pid, key):
    return os.path.join(_project_dir(data_dir, pid), "papers", key + ".md")


def paper_md_relpath(pid, paper_key):
    return "projects/%s/papers/%s.md" % (pid, paper_key)


def _trash_dir(data_dir):
    d = os.path.join(data_dir, ".trash")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ----------------------------------------------------------------- 库
def ensure_library(data_dir):
    with _LOCK:
        os.makedirs(_projects_dir(data_dir), exist_ok=True)
        gi = os.path.join(data_dir, ".gitignore")
        if not os.path.exists(gi):
            config.atomic_write_text(gi, GITIGNORE)
        if not os.path.exists(os.path.join(data_dir, "settings.json")):
            config.save_settings(data_dir, {})
        if not os.path.exists(_project_file(data_dir, INBOX_ID)):
            now = config.now_iso()
            _save_project(data_dir, {"id": INBOX_ID, "name": INBOX_NAME,
                                     "description": "随手丢进来的论文、链接和想法",
                                     "created": now, "updated": now, "items": []}, refresh=False)
        if not os.path.exists(os.path.join(data_dir, "vocabulary.md")):
            refresh_vocabulary(data_dir)
        refresh_readmes(data_dir)


def _load_project(data_dir, pid):
    p = config.read_json(_project_file(data_dir, pid))
    if not isinstance(p, dict):
        raise NotFound("项目不存在：%s" % pid)
    p.setdefault("items", [])
    p.setdefault("description", "")
    return p


def _save_project(data_dir, project, refresh=True):
    os.makedirs(os.path.join(_project_dir(data_dir, project["id"]), "papers"), exist_ok=True)
    config.atomic_write_json(_project_file(data_dir, project["id"]), project)
    if refresh:
        refresh_readmes(data_dir, project["id"])


STATEFUL_TYPES = ("paper", "web", "link")     # 有论文状态（可阅读、可标注）的条目类型
RESOURCE_KINDS = ("paper", "code", "homepage", "blog", "video", "slides", "dataset", "model", "demo", "other")


def _counts(items):
    c = {"paper": 0, "web": 0, "link": 0, "note": 0}
    for it in items:
        t = it.get("type")
        if t == "web":
            c["web"] += 1
            c["link"] += 1
        elif t in c:
            c[t] += 1
    return c


def _summary(p):
    return {"id": p["id"], "name": p.get("name", p["id"]), "description": p.get("description", ""),
            "created": p.get("created"), "updated": p.get("updated"), "counts": _counts(p.get("items", []))}


def list_projects(data_dir):
    out = []
    d = _projects_dir(data_dir)
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        try:
            p = _load_project(data_dir, name)
        except NotFound:
            continue
        out.append(_summary(p))
    out.sort(key=lambda s: (0 if s["id"] == INBOX_ID else 1, s.get("created") or ""))
    return out


def get_project(data_dir, pid):
    with _LOCK:
        return _load_project(data_dir, pid)


def _slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s


def create_project(data_dir, name, description=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("项目名不能为空")
    with _LOCK:
        base = _slugify(name) or ("p-" + secrets.token_hex(3))
        if base == INBOX_ID:
            base = "inbox-2"
        pid = base
        n = 2
        while os.path.exists(_project_dir(data_dir, pid)):
            pid = "%s-%d" % (base, n)
            n += 1
        now = config.now_iso()
        p = {"id": pid, "name": name, "description": (description or "").strip(),
             "created": now, "updated": now, "items": []}
        _save_project(data_dir, p)
    _notify("新建项目「%s」" % name)
    return p


def update_project(data_dir, pid, name=None, description=None):
    with _LOCK:
        p = _load_project(data_dir, pid)
        if name is not None:
            if not name.strip():
                raise ValueError("项目名不能为空")
            p["name"] = name.strip()
        if description is not None:
            p["description"] = description.strip()
        p["updated"] = config.now_iso()
        _save_project(data_dir, p)
    _notify("更新项目「%s」" % p["name"])
    return p


def delete_project(data_dir, pid):
    if pid == INBOX_ID:
        raise ValueError("收件箱不能删除")
    with _LOCK:
        p = _load_project(data_dir, pid)
        src = _project_dir(data_dir, pid)
        dst = os.path.join(_trash_dir(data_dir), "%s-project-%s" % (_stamp(), pid))
        shutil.move(src, dst)
        refresh_readmes(data_dir)
        refresh_vocabulary(data_dir)
    _notify("删除项目「%s」（已移入 .trash）" % p.get("name", pid))


# ----------------------------------------------------------------- 条目
def _find(p, iid):
    for it in p["items"]:
        if it.get("id") == iid:
            return it
    raise NotFound("条目不存在：%s" % iid)


def find_item(data_dir, pid, iid):
    with _LOCK:
        return dict(_find(_load_project(data_dir, pid), iid))


def _clean_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,，\s]+", tags)
    out = []
    for t in tags:
        t = str(t).strip().lstrip("#")
        if t and t not in out:
            out.append(t[:40])
    return out


def _norm_resource(r, now=None):
    """清洗一条资源记录；url 为空返回 None。"""
    if not isinstance(r, dict):
        return None
    url = (r.get("url") or "").strip()
    if not url:
        return None
    from .fetcher import classify  # 延迟导入避免循环
    kind = r.get("kind") if r.get("kind") in RESOURCE_KINDS else classify(url)
    out = {"id": r.get("id") or _new_id("r_"), "kind": kind, "url": url,
           "title": (r.get("title") or "").strip()[:300], "auto": bool(r.get("auto")),
           "added": r.get("added") or now or config.now_iso()}
    if r.get("pdf_url"):
        out["pdf_url"] = r["pdf_url"]
    if r.get("arxiv_id"):
        out["arxiv_id"] = r["arxiv_id"]
    return out


def _same_url(a, b):
    def n(u):
        u = (u or "").strip().lower()
        u = re.sub(r"^https?://(www\.)?", "", u)
        return u.rstrip("/")
    return bool(a) and bool(b) and n(a) == n(b)


def item_resources(item):
    """条目的全部资源：主链接（id="main"）+ resources 数组。"""
    out = []
    if item.get("type") in STATEFUL_TYPES and (item.get("url") or item.get("pdf_url")):
        kind = item.get("kind") or ("paper" if item.get("type") == "paper" else "blog")
        out.append({"id": "main", "kind": kind, "url": item.get("url") or item.get("pdf_url"),
                    "pdf_url": item.get("pdf_url"), "title": item.get("title") or "", "auto": False})
    out.extend(item.get("resources") or [])
    return out


def add_item(data_dir, pid, item_dict):
    d = dict(item_dict or {})
    typ = d.get("type") or ("note" if d.get("note") else "web")
    if typ not in ("paper", "web", "link", "note"):
        raise ValueError("未知的条目类型：%s" % typ)
    with _LOCK:
        p = _load_project(data_dir, pid)
        if typ == "paper":
            key = d.get("paper_key")
            if not key:
                from .fetcher import paper_key_for  # 延迟导入避免循环
                key = paper_key_for(arxiv_id=d.get("arxiv_id"), url=d.get("pdf_url") or d.get("url"))
            for it in p["items"]:
                if it.get("type") == "paper" and it.get("paper_key") == key:
                    out = dict(it)
                    out["existing"] = True
                    return out
            if not (d.get("pdf_url") or d.get("url")):
                raise ValueError("论文需要 PDF 链接")
        elif typ in ("web", "link"):
            if not d.get("url"):
                raise ValueError("链接不能为空")
            key = d.get("paper_key")
            if not key:
                from .fetcher import web_key_for
                key = web_key_for(d["url"])
            for it in p["items"]:
                if it.get("type") in ("web", "link") and (it.get("url") == d.get("url") or
                                                          (it.get("paper_key") and it.get("paper_key") == key)):
                    out = dict(it)
                    out["existing"] = True
                    return out
        else:
            if not (d.get("note") or d.get("title")):
                raise ValueError("笔记内容不能为空")
        now = config.now_iso()
        item = {
            "id": _new_id(),
            "type": typ,
            "title": (d.get("title") or "").strip() or (d.get("url") or ("笔记" if typ == "note" else "")),
            "url": d.get("url") or (d.get("pdf_url") if typ == "paper" else None),
            "pdf_url": d.get("pdf_url") or (d.get("url") if typ == "paper" else None),
            "arxiv_id": d.get("arxiv_id"),
            "authors": list(d.get("authors") or []),
            "year": d.get("year"),
            "abstract": d.get("abstract"),
            "paper_key": key if typ in STATEFUL_TYPES else None,
            "kind": (d.get("kind") if d.get("kind") in RESOURCE_KINDS else None) or ("paper" if typ == "paper" else None),
            "note": d.get("note") if typ == "note" else None,
            "tags": _clean_tags(d.get("tags")),
            "status": d.get("status") if d.get("status") in ("unread", "reading", "done") else "unread",
            "added": now,
            "updated": now,
        }
        if typ != "paper":
            for k in ("pdf_url", "arxiv_id"):
                item[k] = None
        if typ in ("web", "link"):
            from .fetcher import classify
            item["kind"] = item["kind"] or classify(item["url"])
            item["site_name"] = d.get("site_name") or ""
            item["description"] = (d.get("description") or "")[:500]
        if typ == "note":
            item["paper_key"] = None
            item["kind"] = None
        if typ in STATEFUL_TYPES:
            res = []
            for r in d.get("resources") or []:
                nr = _norm_resource(r, now)
                if not nr or _same_url(nr["url"], item.get("url")) or _same_url(nr["url"], item.get("pdf_url")):
                    continue
                if any(_same_url(nr["url"], x["url"]) for x in res):
                    continue
                res.append(nr)
            item["resources"] = res
        p["items"].insert(0, item)
        p["updated"] = now
        _save_project(data_dir, p, refresh=False)
        if typ in STATEFUL_TYPES:
            _init_paper_state(data_dir, p, item)
        refresh_readmes(data_dir, pid)
    _notify("添加%s「%s」" % ({"paper": "论文", "web": "网页", "link": "链接", "note": "笔记"}[typ], item["title"][:60]))
    out = dict(item)
    out["existing"] = False
    return out


def update_item(data_dir, pid, iid, patch):
    patch = patch or {}
    with _LOCK:
        p = _load_project(data_dir, pid)
        it = _find(p, iid)
        if "title" in patch and patch["title"] is not None:
            t = str(patch["title"]).strip()
            if t:
                it["title"] = t
        if "tags" in patch:
            it["tags"] = _clean_tags(patch["tags"])
        if "status" in patch:
            if patch["status"] not in ("unread", "reading", "done"):
                raise ValueError("状态只能是 unread / reading / done")
            it["status"] = patch["status"]
        if "note" in patch and it["type"] == "note":
            it["note"] = patch["note"] or ""
        if "url" in patch and patch["url"]:
            it["url"] = str(patch["url"]).strip()
        it["updated"] = config.now_iso()
        p["updated"] = it["updated"]
        _save_project(data_dir, p, refresh=False)
        if it["type"] in STATEFUL_TYPES and it.get("paper_key"):
            st = _load_state(data_dir, pid, it["paper_key"])
            if st is not None:
                st["meta"]["title"] = it["title"]
                st["status"] = it["status"]
                _write_state(data_dir, pid, st, p)
        refresh_readmes(data_dir, pid)
    _notify("更新条目「%s」" % it["title"][:60])
    return dict(it)


def delete_item(data_dir, pid, iid):
    with _LOCK:
        p = _load_project(data_dir, pid)
        it = _find(p, iid)
        p["items"] = [x for x in p["items"] if x.get("id") != iid]
        p["updated"] = config.now_iso()
        _save_project(data_dir, p, refresh=False)
        if it.get("type") in STATEFUL_TYPES and it.get("paper_key"):
            for src in (_paper_json(data_dir, pid, it["paper_key"]), _paper_md(data_dir, pid, it["paper_key"])):
                if os.path.exists(src):
                    shutil.move(src, os.path.join(_trash_dir(data_dir),
                                                  "%s-%s-%s" % (_stamp(), pid, os.path.basename(src))))
        refresh_readmes(data_dir, pid)
        if it.get("type") in STATEFUL_TYPES:
            refresh_vocabulary(data_dir)
    _notify("删除条目「%s」" % it.get("title", "")[:60])


def move_item(data_dir, pid, iid, to_pid):
    if pid == to_pid:
        return find_item(data_dir, pid, iid)
    with _LOCK:
        src = _load_project(data_dir, pid)
        dst = _load_project(data_dir, to_pid)
        it = _find(src, iid)
        if it.get("type") in STATEFUL_TYPES and it.get("paper_key"):
            for x in dst["items"]:
                if x.get("type") in STATEFUL_TYPES and x.get("paper_key") == it.get("paper_key"):
                    raise Conflict("目标项目里已经有这篇论文 / 网页了")
        src["items"] = [x for x in src["items"] if x.get("id") != iid]
        it["updated"] = config.now_iso()
        dst["items"].insert(0, it)
        src["updated"] = dst["updated"] = it["updated"]
        _save_project(data_dir, src, refresh=False)
        _save_project(data_dir, dst, refresh=False)
        if it.get("type") in STATEFUL_TYPES and it.get("paper_key"):
            key = it["paper_key"]
            os.makedirs(os.path.join(_project_dir(data_dir, to_pid), "papers"), exist_ok=True)
            st = _load_state(data_dir, pid, key)
            for f in (_paper_json(data_dir, pid, key), _paper_md(data_dir, pid, key)):
                if os.path.exists(f):
                    os.remove(f)
            if st is not None:
                st["pid"] = to_pid
                _write_state(data_dir, to_pid, st, dst)
            # 迁移全文缓存（主资源 + 各资源）
            for rid in ["main"] + [r.get("id") for r in it.get("resources") or []]:
                old_c = _cache_file(data_dir, pid, key, rid)
                if os.path.exists(old_c):
                    new_c = _cache_file(data_dir, to_pid, key, rid)
                    os.makedirs(os.path.dirname(new_c), exist_ok=True)
                    shutil.move(old_c, new_c)
        refresh_readmes(data_dir, pid)
        refresh_readmes(data_dir, to_pid)
        refresh_vocabulary(data_dir)
    _notify("移动「%s」到「%s」" % (it.get("title", "")[:50], dst.get("name", to_pid)))
    return dict(it)


# ----------------------------------------------------------------- 资源（SPEC §13.2）
def _sync_state_resources(data_dir, p, it):
    """资源变化后：更新论文状态里的 meta.resources，重写 md 与项目 README。调用方持锁。"""
    if it.get("type") in STATEFUL_TYPES and it.get("paper_key"):
        st = _load_state(data_dir, p["id"], it["paper_key"])
        if st is not None:
            st["meta"]["resources"] = item_resources(it)
            _write_state(data_dir, p["id"], st, p)
    refresh_readmes(data_dir, p["id"])


def add_resource(data_dir, pid, iid, res_dict):
    r = dict(res_dict or {})
    with _LOCK:
        p = _load_project(data_dir, pid)
        it = _find(p, iid)
        if it.get("type") not in STATEFUL_TYPES:
            raise ValueError("只有论文和网页条目可以关联资源")
        nr = _norm_resource(r)
        if nr is None:
            raise ValueError("资源链接不能为空")
        if _same_url(nr["url"], it.get("url")) or _same_url(nr["url"], it.get("pdf_url")):
            out = {"id": "main", "kind": it.get("kind") or "paper", "url": it.get("url"), "title": it.get("title"),
                   "auto": False, "existing": True}
            return out
        res = it.setdefault("resources", [])
        for x in res:
            if _same_url(x.get("url"), nr["url"]):
                out = dict(x)
                out["existing"] = True
                return out
        res.append(nr)
        it["updated"] = config.now_iso()
        p["updated"] = it["updated"]
        _save_project(data_dir, p, refresh=False)
        _sync_state_resources(data_dir, p, it)
    _notify("关联资源「%s」→「%s」" % ((nr.get("title") or nr["url"])[:40], it.get("title", "")[:40]))
    out = dict(nr)
    out["existing"] = False
    return out


def update_resource(data_dir, pid, iid, rid, patch):
    patch = patch or {}
    with _LOCK:
        p = _load_project(data_dir, pid)
        it = _find(p, iid)
        for x in it.get("resources") or []:
            if x.get("id") == rid:
                if patch.get("kind") is not None:
                    if patch["kind"] not in RESOURCE_KINDS:
                        raise ValueError("未知的资源类型：%s" % patch["kind"])
                    x["kind"] = patch["kind"]
                if patch.get("title") is not None:
                    x["title"] = str(patch["title"]).strip()[:300]
                if patch.get("url"):
                    x["url"] = str(patch["url"]).strip()
                if "auto" in patch:
                    x["auto"] = bool(patch["auto"])
                it["updated"] = config.now_iso()
                p["updated"] = it["updated"]
                _save_project(data_dir, p, refresh=False)
                _sync_state_resources(data_dir, p, it)
                out = dict(x)
                break
        else:
            raise NotFound("资源不存在：%s" % rid)
    _notify("修改资源「%s」" % (out.get("title") or out.get("url") or "")[:50])
    return out


def delete_resource(data_dir, pid, iid, rid):
    with _LOCK:
        p = _load_project(data_dir, pid)
        it = _find(p, iid)
        res = it.get("resources") or []
        target = next((x for x in res if x.get("id") == rid), None)
        if target is None:
            raise NotFound("资源不存在：%s" % rid)
        it["resources"] = [x for x in res if x.get("id") != rid]
        it["updated"] = config.now_iso()
        p["updated"] = it["updated"]
        _save_project(data_dir, p, refresh=False)
        if it.get("paper_key"):
            st = _load_state(data_dir, pid, it["paper_key"])
            if st is not None:
                gone = set(a["id"] for a in st["annotations"] if a.get("resource_id") == rid)
                st["annotations"] = [a for a in st["annotations"] if a["id"] not in gone]
                for n in st["notes"]:
                    if n.get("resource_id") == rid:
                        n["resource_id"] = "main"
                        n.pop("anchor", None)
                        n["detached_from"] = target.get("title") or target.get("url")
                    if n.get("annotation_id") in gone:
                        n["annotation_id"] = None
                for v in st.get("vocab") or []:
                    if v.get("annotation_id") in gone:
                        v["annotation_id"] = None
                st["meta"]["resources"] = item_resources(it)
                _write_state(data_dir, pid, st, p)
            cf = _cache_file(data_dir, pid, it["paper_key"], rid)
            if os.path.exists(cf):
                os.remove(cf)
        refresh_readmes(data_dir, pid)
        refresh_vocabulary(data_dir)
    _notify("移除资源「%s」" % (target.get("title") or target.get("url") or "")[:50])


# ----------------------------------------------------------------- 论文状态
def _blank_state(pid, item):
    now = config.now_iso()
    return {
        "pid": pid,
        "iid": item["id"],
        "paper_key": item["paper_key"],
        "meta": {
            "title": item.get("title"),
            "authors": list(item.get("authors") or []),
            "url": item.get("url"),
            "pdf_url": item.get("pdf_url"),
            "arxiv_id": item.get("arxiv_id"),
            "year": item.get("year"),
            "abstract": item.get("abstract"),
            "type": item.get("type"),
            "kind": item.get("kind"),
            "site_name": item.get("site_name") or "",
            "description": item.get("description") or "",
        },
        "annotations": [],
        "notes": [],
        "qa": [],
        "translations": [],
        "vocab": [],
        "summary": None,
        "sessions": {"claude": None, "codex": None},
        "last_page": 1,
        "zoom": 1.25,
        "tts_pos": None,
        "status": item.get("status", "unread"),
        "created": now,
        "updated": now,
    }


def _load_state(data_dir, pid, key):
    st = config.read_json(_paper_json(data_dir, pid, key))
    if not isinstance(st, dict):
        return None
    for k, v in (("annotations", []), ("notes", []), ("qa", []), ("translations", []), ("vocab", []),
                 ("sessions", {"claude": None, "codex": None}), ("summary", None),
                 ("last_page", 1), ("zoom", 1.25), ("tts_pos", None), ("status", "unread")):
        st.setdefault(k, v)
    st.setdefault("meta", {})
    return st


def _write_state(data_dir, pid, st, project=None):
    st["updated"] = config.now_iso()
    config.atomic_write_json(_paper_json(data_dir, pid, st["paper_key"]), st)
    if project is None:
        try:
            project = _load_project(data_dir, pid)
        except NotFound:
            project = {"name": pid}
    settings = config.load_settings(data_dir)
    md = render_paper_markdown(st, project.get("name", pid), settings.get("app_url") or "http://localhost:8765",
                               pages_url_of(settings))
    config.atomic_write_text(_paper_md(data_dir, pid, st["paper_key"]), md)


def _init_paper_state(data_dir, project, item):
    st = _load_state(data_dir, project["id"], item["paper_key"])
    if st is None:
        st = _blank_state(project["id"], item)
    else:
        st["iid"] = item["id"]
        st["pid"] = project["id"]
    _write_state(data_dir, project["id"], st, project)
    return st


def _paper_ctx(data_dir, pid, iid):
    """返回 (project, item, state)，必要时初始化 state。调用方需持锁。"""
    p = _load_project(data_dir, pid)
    it = _find(p, iid)
    if it.get("type") not in STATEFUL_TYPES:
        raise ValueError("这个条目不是论文或网页")
    if not it.get("paper_key"):
        # 旧版本的 link 条目没有 paper_key：补上并保存
        from .fetcher import web_key_for, classify
        it["paper_key"] = web_key_for(it.get("url") or it["id"])
        it.setdefault("kind", classify(it.get("url") or ""))
        _save_project(data_dir, p, refresh=False)
    st = _load_state(data_dir, pid, it["paper_key"])
    if st is None:
        st = _init_paper_state(data_dir, p, it)
    # 让 meta 跟随条目最新信息
    st["meta"]["title"] = it.get("title") or st["meta"].get("title")
    st["meta"]["type"] = it.get("type")
    st["meta"]["kind"] = it.get("kind") or st["meta"].get("kind")
    st["meta"]["resources"] = item_resources(it)
    st["iid"] = it["id"]
    st["pid"] = pid
    return p, it, st


def _commit_paper(data_dir, p, it, st, msg, notify=True, readme=True):
    _write_state(data_dir, p["id"], st, p)
    if it.get("status") != st.get("status"):
        it["status"] = st["status"]
        it["updated"] = config.now_iso()
        _save_project(data_dir, p, refresh=False)
        readme = True
    if readme:
        refresh_readmes(data_dir, p["id"])
    if notify:
        _notify(msg)


def get_paper_state(data_dir, pid, iid):
    with _LOCK:
        _, _, st = _paper_ctx(data_dir, pid, iid)
        return st


def update_paper(data_dir, pid, iid, patch, notify=True):
    patch = patch or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        status_changed = False
        if "last_page" in patch and patch["last_page"] is not None:
            try:
                st["last_page"] = max(1, int(patch["last_page"]))
            except (TypeError, ValueError):
                raise ValueError("last_page 需要整数")
        if "zoom" in patch and patch["zoom"] is not None:
            try:
                st["zoom"] = max(0.3, min(float(patch["zoom"]), 5.0))
            except (TypeError, ValueError):
                raise ValueError("zoom 需要数字")
        if "tts_pos" in patch:
            tp = patch["tts_pos"]
            if tp is None:
                st["tts_pos"] = None
            else:
                try:
                    st["tts_pos"] = {"page": max(1, int(tp.get("page"))),
                                     "sentence": max(0, int(tp.get("sentence") or 0))}
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("tts_pos 需要 {page, sentence}")
        if "status" in patch and patch["status"]:
            if patch["status"] not in ("unread", "reading", "done"):
                raise ValueError("状态只能是 unread / reading / done")
            status_changed = patch["status"] != st.get("status")
            st["status"] = patch["status"]
        if status_changed:
            _commit_paper(data_dir, p, it, st, "阅读状态：%s" % st["meta"].get("title", "")[:50], notify=notify)
        else:
            # 阅读进度这类高频字段：只写 json，不重渲染、不触发同步
            config.atomic_write_json(_paper_json(data_dir, pid, st["paper_key"]), st)
        return st


def _rid(d):
    r = (d or {}).get("resource_id")
    r = str(r).strip() if r not in (None, "") else "main"
    return r[:40] if re.match(r"^[A-Za-z0-9_\-]+$", r) else "main"


def _anchor(a):
    """网页标注的文本锚点：{type:"text", exact, prefix, suffix, start, end[, space]}；不合法返回 None。

    space="page" 表示锚点建在原网页模式（整页文字）上，start/end 只在同一模式里可用。"""
    if not isinstance(a, dict):
        return None
    exact = str(a.get("exact") or "")
    if not exact.strip():
        return None
    out = {"type": "text", "exact": exact[:4000], "prefix": str(a.get("prefix") or "")[-64:],
           "suffix": str(a.get("suffix") or "")[:64]}
    for k in ("start", "end"):
        try:
            out[k] = max(0, int(a.get(k)))
        except (TypeError, ValueError):
            out[k] = None
    if a.get("space") == "page":
        out["space"] = "page"
    return out


def _page_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _norm_rects(rects):
    out = []
    for r in rects or []:
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            continue
        try:
            x, y, w, h = [float(v) for v in r]
        except (TypeError, ValueError):
            continue
        out.append([round(x, 5), round(y, 5), round(w, 5), round(h, 5)])
    return out


ANN_KINDS = ("highlight", "underline", "strike", "squiggly", "vocab")
ANN_COLORS = ("yellow", "green", "blue", "pink", "purple")


def add_annotation(data_dir, pid, iid, ann):
    ann = ann or {}
    kind = ann.get("kind") or "highlight"
    if kind not in ANN_KINDS:
        raise ValueError("未知的标注类型：%s" % kind)
    color = ann.get("color") or ("purple" if kind == "vocab" else "yellow")
    if color not in ANN_COLORS:
        color = "yellow"
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        anchor = _anchor(ann.get("anchor"))
        page = _page_or_none(ann.get("page"))
        if page is None and anchor is None:
            page = 1
        a = {"id": _new_id("h_"), "kind": kind, "color": color,
             "page": page, "quote": (ann.get("quote") or (anchor or {}).get("exact") or "").strip(),
             "rects": _norm_rects(ann.get("rects")), "comment": ann.get("comment") or "",
             "resource_id": _rid(ann), "created": config.now_iso()}
        if anchor:
            a["anchor"] = anchor
        st["annotations"].append(a)
        if st.get("status") == "unread":
            st["status"] = "reading"
        _commit_paper(data_dir, p, it, st, "标注：%s" % st["meta"].get("title", "")[:50])
        return a


def update_annotation(data_dir, pid, iid, aid, patch):
    patch = patch or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        for a in st["annotations"]:
            if a["id"] == aid:
                if patch.get("kind") in ANN_KINDS:
                    a["kind"] = patch["kind"]
                if patch.get("color") in ANN_COLORS:
                    a["color"] = patch["color"]
                if "comment" in patch:
                    a["comment"] = patch["comment"] or ""
                a["updated"] = config.now_iso()
                _commit_paper(data_dir, p, it, st, "修改标注：%s" % st["meta"].get("title", "")[:50])
                return a
        raise NotFound("标注不存在")


def delete_annotation(data_dir, pid, iid, aid):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        n = len(st["annotations"])
        st["annotations"] = [a for a in st["annotations"] if a["id"] != aid]
        if len(st["annotations"]) == n:
            raise NotFound("标注不存在")
        for nt in st["notes"]:
            if nt.get("annotation_id") == aid:
                nt["annotation_id"] = None
        _commit_paper(data_dir, p, it, st, "删除标注：%s" % st["meta"].get("title", "")[:50])


def add_note(data_dir, pid, iid, note_dict):
    nd = note_dict or {}
    text = (nd.get("text") or "").strip()
    if not text and not (nd.get("quote") or "").strip():
        raise ValueError("笔记内容不能为空")
    page = nd.get("page")
    try:
        page = int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        page = None
    src = nd.get("source") if nd.get("source") in ("manual", "qa", "translation") else "manual"
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        now = config.now_iso()
        n = {"id": _new_id("n_"), "page": page, "quote": (nd.get("quote") or "").strip(),
             "text": text, "source": src, "annotation_id": nd.get("annotation_id") or None,
             "resource_id": _rid(nd), "created": now, "updated": now}
        anchor = _anchor(nd.get("anchor"))
        if anchor:
            n["anchor"] = anchor
        st["notes"].append(n)
        if st.get("status") == "unread":
            st["status"] = "reading"
        _commit_paper(data_dir, p, it, st, "笔记：%s" % st["meta"].get("title", "")[:50])
        return n


def update_note(data_dir, pid, iid, nid, patch):
    patch = patch or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        for n in st["notes"]:
            if n["id"] == nid:
                if "text" in patch:
                    n["text"] = (patch["text"] or "").strip()
                if "quote" in patch:
                    n["quote"] = (patch["quote"] or "").strip()
                if "page" in patch:
                    try:
                        n["page"] = int(patch["page"]) if patch["page"] not in (None, "") else None
                    except (TypeError, ValueError):
                        pass
                n["updated"] = config.now_iso()
                _commit_paper(data_dir, p, it, st, "修改笔记：%s" % st["meta"].get("title", "")[:50])
                return n
        raise NotFound("笔记不存在")


def delete_note(data_dir, pid, iid, nid):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        n0 = len(st["notes"])
        st["notes"] = [n for n in st["notes"] if n["id"] != nid]
        if len(st["notes"]) == n0:
            raise NotFound("笔记不存在")
        _commit_paper(data_dir, p, it, st, "删除笔记：%s" % st["meta"].get("title", "")[:50])


def add_qa(data_dir, pid, iid, qa_dict):
    q = qa_dict or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        page = q.get("page")
        try:
            page = int(page) if page not in (None, "") else None
        except (TypeError, ValueError):
            page = None
        item = {"id": _new_id("q_"), "question": q.get("question") or "", "answer": q.get("answer") or "",
                "engine": q.get("engine") or "claude", "mode": q.get("mode") or "ask",
                "style": q.get("style") if q.get("style") in ("voice",) else "text",
                "quote": q.get("quote") or "", "page": page, "resource_id": _rid(q), "at": config.now_iso()}
        anchor = _anchor(q.get("anchor"))
        if anchor:
            item["anchor"] = anchor
        st["qa"].append(item)
        if st.get("status") == "unread":
            st["status"] = "reading"
        _commit_paper(data_dir, p, it, st, "问答：%s" % st["meta"].get("title", "")[:50])
        return item


def delete_qa(data_dir, pid, iid, qid):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        n0 = len(st["qa"])
        st["qa"] = [q for q in st["qa"] if q["id"] != qid]
        if len(st["qa"]) == n0:
            raise NotFound("问答不存在")
        _commit_paper(data_dir, p, it, st, "删除问答：%s" % st["meta"].get("title", "")[:50])


def add_translation(data_dir, pid, iid, tr):
    tr = tr or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        page = tr.get("page")
        try:
            page = int(page) if page not in (None, "") else None
        except (TypeError, ValueError):
            page = None
        t = {"id": _new_id("t_"), "source": tr.get("source") or "", "translation": tr.get("translation") or "",
             "engine": tr.get("engine") or "", "page": page, "resource_id": _rid(tr), "at": config.now_iso()}
        st["translations"].append(t)
        st["translations"] = st["translations"][-200:]
        # 翻译记录不进 Markdown 笔记，也不单独触发同步（随下次提交一起）
        _commit_paper(data_dir, p, it, st, "翻译", notify=False, readme=False)
        return t


def set_summary(data_dir, pid, iid, markdown, engine):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        st["summary"] = {"markdown": markdown or "", "engine": engine, "at": config.now_iso()}
        st["status"] = "done"
        _commit_paper(data_dir, p, it, st, "归档：%s" % st["meta"].get("title", "")[:50])
        return st["summary"]


def set_session(data_dir, pid, iid, engine, session_id):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        st.setdefault("sessions", {})[engine] = session_id or None
        _commit_paper(data_dir, p, it, st, "会话", notify=False, readme=False)


def reset_session(data_dir, pid, iid, engine):
    set_session(data_dir, pid, iid, engine, None)


# ----------------------------------------------------------------- 生词本
def lemma_of(word):
    w = (word or "").strip().lower()
    w = re.sub(r"^[^\w]+|[^\w]+$", "", w, flags=re.UNICODE)
    return re.sub(r"\s+", " ", w)


def find_vocab(data_dir, pid, iid, word):
    lem = lemma_of(word)
    with _LOCK:
        _, _, st = _paper_ctx(data_dir, pid, iid)
        for v in st.get("vocab") or []:
            if v.get("lemma") == lem:
                return dict(v)
    return None


def add_vocab(data_dir, pid, iid, entry):
    e = entry or {}
    word = (e.get("word") or "").strip()
    word = re.sub(r"\s+", " ", word)
    if not word:
        raise ValueError("单词不能为空")
    if len(word) > 80 or len(word.split()) > 6:
        raise ValueError("生词最多 6 个单词")
    lem = lemma_of(word)
    page = e.get("page")
    try:
        page = int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        page = None
    context = " ".join((e.get("context") or "").split())
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        for v in st["vocab"]:
            if v.get("lemma") == lem:
                extra = v.setdefault("extra_contexts", [])
                known = [v.get("context")] + [x.get("context") for x in extra]
                if context and context not in known:
                    extra.append({"context": context, "page": page, "resource_id": _rid(e),
                                  "annotation_id": e.get("annotation_id") or None,
                                  "context_translation": e.get("context_translation") or "",
                                  "created": config.now_iso()})
                elif e.get("annotation_id") and not v.get("annotation_id"):
                    v["annotation_id"] = e["annotation_id"]
                v["updated"] = config.now_iso()
                _commit_paper(data_dir, p, it, st, "生词：%s" % word, readme=False)
                refresh_vocabulary(data_dir)
                out = dict(v)
                out["merged"] = True
                return out
        now = config.now_iso()
        v = {"id": _new_id("v_"), "word": word, "lemma": lem,
             "phonetic": e.get("phonetic") or "", "pos": e.get("pos") or "",
             "meaning": e.get("meaning") or "", "context_meaning": e.get("context_meaning") or "",
             "context": context, "context_translation": e.get("context_translation") or "",
             "page": page, "annotation_id": e.get("annotation_id") or None, "resource_id": _rid(e),
             "created": now, "updated": now, "mastered": False,
             "lookup_error": e.get("lookup_error") or None, "engine": e.get("engine") or None,
             "extra_contexts": []}
        anchor = _anchor(e.get("anchor"))
        if anchor:
            v["anchor"] = anchor
        st["vocab"].append(v)
        if st.get("status") == "unread":
            st["status"] = "reading"
        _commit_paper(data_dir, p, it, st, "生词：%s" % word, readme=False)
        refresh_vocabulary(data_dir)
        out = dict(v)
        out["merged"] = False
        return out


def update_vocab(data_dir, pid, iid, vid, patch):
    patch = patch or {}
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        for v in st["vocab"]:
            if v["id"] == vid:
                if "mastered" in patch:
                    v["mastered"] = bool(patch["mastered"])
                for k in ("meaning", "context_meaning", "phonetic", "pos", "context_translation"):
                    if k in patch and patch[k] is not None:
                        v[k] = str(patch[k]).strip()
                if "lookup_error" in patch:
                    v["lookup_error"] = patch["lookup_error"] or None
                v["updated"] = config.now_iso()
                _commit_paper(data_dir, p, it, st, "生词：%s" % v["word"], readme=False)
                refresh_vocabulary(data_dir)
                return v
        raise NotFound("生词不存在")


def delete_vocab(data_dir, pid, iid, vid):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        target = None
        for v in st["vocab"]:
            if v["id"] == vid:
                target = v
                break
        if target is None:
            raise NotFound("生词不存在")
        st["vocab"] = [v for v in st["vocab"] if v["id"] != vid]
        ann_ids = set([target.get("annotation_id")] + [x.get("annotation_id") for x in target.get("extra_contexts") or []])
        ann_ids.discard(None)
        if ann_ids:
            st["annotations"] = [a for a in st["annotations"] if a["id"] not in ann_ids]
        _commit_paper(data_dir, p, it, st, "删除生词：%s" % target["word"], readme=False)
        refresh_vocabulary(data_dir)


def list_all_vocab(data_dir, mastered=None, pid=None):
    out = []
    with _LOCK:
        for s in list_projects(data_dir):
            if pid and s["id"] != pid:
                continue
            try:
                proj = _load_project(data_dir, s["id"])
            except NotFound:
                continue
            for it in proj["items"]:
                if it.get("type") not in STATEFUL_TYPES or not it.get("paper_key"):
                    continue
                st = _load_state(data_dir, proj["id"], it["paper_key"])
                if not st:
                    continue
                for v in st.get("vocab") or []:
                    if mastered is not None and bool(v.get("mastered")) != bool(mastered):
                        continue
                    e = dict(v)
                    e.update({"pid": proj["id"], "iid": it["id"], "paper_title": it.get("title"),
                              "paper_key": it["paper_key"], "project_name": proj.get("name")})
                    out.append(e)
    out.sort(key=lambda e: e.get("created") or "", reverse=True)
    return out


def _bold_word(context, word):
    if not context:
        return ""
    ctx = _md_inline(context)
    if not word:
        return ctx
    # 优先匹配完整的词（skim 不应加粗 Skimmer 的前半截），找不到再退回普通匹配
    m = re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", ctx, re.I) or re.search(re.escape(word), ctx, re.I)
    if not m:
        return ctx
    return ctx[:m.start()] + "**" + m.group(0) + "**" + ctx[m.end():]


def _render_vocab_entry(v, app_url, pages_url=""):
    L = []
    head = "- **%s**" % _md_inline(v.get("word"))
    if v.get("phonetic"):
        head += " `%s`" % _md_inline(v["phonetic"]).replace("`", "")
    if v.get("pos"):
        head += " *%s*" % _md_inline(v["pos"])
    meaning = " ".join((v.get("meaning") or "").split("\n")).strip()
    if meaning:
        head += " — " + _md_inline(meaning)
    elif v.get("lookup_error"):
        head += " — _（未查到释义）_"
    L.append(head)
    if v.get("context_meaning"):
        L.append("  - 语境义：" + _md_inline(v["context_meaning"]))
    ctxs = [{"context": v.get("context"), "page": v.get("page"), "context_translation": v.get("context_translation")}]
    ctxs += v.get("extra_contexts") or []
    for c in ctxs:
        if not c.get("context"):
            continue
        pg = " (p.%s)" % c["page"] if c.get("page") else ""
        L.append("  - 原句：" + _bold_word(c["context"], v.get("word")) + pg)
        if c.get("context_translation"):
            L.append("    - 译：" + _md_inline(c["context_translation"]))
    online = (" · [🌐 在线](%s#/r/%s/%s)" % (pages_url, v["pid"], v["iid"])) if pages_url else ""
    src = "  - <sub>来自 [%s](%s) · [📖 本地打开](%s/r/%s/%s)%s</sub>" % (
        _md_inline(v.get("paper_title") or v.get("paper_key")), paper_md_relpath(v["pid"], v["paper_key"]),
        app_url, v["pid"], v["iid"], online)
    L.append(src)
    return "\n".join(L)


def _render_vocabulary(data_dir, app_url, pages_url=""):
    allv = list_all_vocab(data_dir)
    learning = sorted([v for v in allv if not v.get("mastered")], key=lambda v: v.get("lemma") or "")
    mastered = sorted([v for v in allv if v.get("mastered")], key=lambda v: v.get("lemma") or "")
    L = ["# 🔤 生词本", "",
         "共 **%d** 个生词 · 学习中 **%d** · 已掌握 **%d** · [⬅️ 返回论文库](README.md) · [在 PaperDesk 中复习](%s/vocab)"
         % (len(allv), len(learning), len(mastered), app_url), ""]
    if not allv:
        L.append("_还没有生词。在阅读页选中单词，点「生词」即可加入。_")
        L.append("")
    groups = {}
    for v in learning:
        ch = (v.get("lemma") or "#")[:1].upper()
        if not ("A" <= ch <= "Z"):
            ch = "#"
        groups.setdefault(ch, []).append(v)
    for ch in sorted(groups.keys(), key=lambda c: (c == "#", c)):
        L.append("## %s" % ch)
        L.append("")
        for v in groups[ch]:
            L.append(_render_vocab_entry(v, app_url, pages_url))
        L.append("")
    if mastered:
        L.append("## ✅ 已掌握")
        L.append("")
        for v in mastered:
            L.append(_render_vocab_entry(v, app_url, pages_url))
        L.append("")
    L.append("---")
    L.append("<sub>由 PaperDesk 自动生成，请不要手动编辑此文件。</sub>")
    L.append("")
    return "\n".join(L)


def refresh_vocabulary(data_dir):
    with _LOCK:
        settings = config.load_settings(data_dir)
        app_url = (settings.get("app_url") or "http://localhost:8765").rstrip("/")
        pages_url = pages_url_of(settings)
        config.atomic_write_text(os.path.join(data_dir, "vocabulary.md"),
                                 _render_vocabulary(data_dir, app_url, pages_url))
        config.atomic_write_text(os.path.join(data_dir, "README.md"),
                                 _render_root_readme(data_dir, app_url, pages_url))


# ----------------------------------------------------------------- 全文缓存
def _cache_file(data_dir, pid, key, rid="main"):
    rid = rid or "main"
    name = key + (".json" if rid == "main" else "@%s.json" % rid)
    return os.path.join(data_dir, ".cache", "text", pid, name)


def write_cached_text(data_dir, pid, paper_key, pages, resource_id="main", title=None, kind=None):
    """pages：[{page, text}]（PDF 按页；网页 / README 通常只有 page=1 一段）。"""
    clean = []
    for i, pg in enumerate(pages or []):
        try:
            n = int(pg.get("page") if pg.get("page") is not None else i + 1)
        except (TypeError, ValueError, AttributeError):
            continue
        clean.append({"page": n, "text": str(pg.get("text") or "")})
    clean.sort(key=lambda x: x["page"])
    rid = _rid({"resource_id": resource_id})
    config.atomic_write_json(_cache_file(data_dir, pid, paper_key, rid),
                             {"pages": clean, "resource_id": rid, "title": title or "", "kind": kind or "",
                              "at": config.now_iso()})
    return sum(len(x["text"]) for x in clean)


def _read_cache_obj(data_dir, pid, paper_key, rid="main"):
    d = config.read_json(_cache_file(data_dir, pid, paper_key, rid))
    if not isinstance(d, dict) or not d.get("pages"):
        return None
    return d


def read_cached_text(data_dir, pid, paper_key, resource_id="main"):
    d = _read_cache_obj(data_dir, pid, paper_key, resource_id or "main")
    return d["pages"] if d else None


def cached_resources(data_dir, pid, paper_key):
    folder = os.path.join(data_dir, ".cache", "text", pid)
    out = []
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if name == paper_key + ".json":
            out.insert(0, "main")
        elif name.startswith(paper_key + "@") and name.endswith(".json"):
            out.append(name[len(paper_key) + 1:-5])
    return out


def _parse_pages(spec):
    if not spec:
        return None
    want = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            want.update(range(min(a, b), max(a, b) + 1))
        else:
            try:
                want.add(int(part))
            except ValueError:
                pass
    return want or None


def cached_text_string(data_dir, pid, paper_key, pages=None):
    pgs = read_cached_text(data_dir, pid, paper_key)
    if not pgs:
        return None
    want = _parse_pages(pages)
    parts = []
    for pg in pgs:
        if want and pg["page"] not in want:
            continue
        parts.append("\n\n===== [p.%d] =====\n%s" % (pg["page"], pg["text"]))
    return "".join(parts).strip() + "\n"


_RES_LABEL = {"paper": "论文 PDF", "code": "代码 README", "homepage": "主页", "blog": "网页", "video": "视频",
              "slides": "幻灯片", "dataset": "数据集", "model": "模型", "demo": "Demo", "other": "网页"}


def agent_text_string(data_dir, pid, item):
    """给 agent 的 paper.txt 正文：主资源（PDF 按页 / 网页 / README）+ 每个有缓存的关联资源。"""
    key = item.get("paper_key")
    if not key:
        return None
    parts = []
    for r in item_resources(item):
        d = _read_cache_obj(data_dir, pid, key, r["id"])
        if not d:
            continue
        kind = d.get("kind") or r.get("kind") or ("paper" if item.get("type") == "paper" else "blog")
        title = d.get("title") or r.get("title") or r.get("url") or ""
        is_pdf = kind == "paper" and (r["id"] != "main" or item.get("type") == "paper")
        if r["id"] == "main" and is_pdf:
            for pg in d["pages"]:
                parts.append("\n\n===== [p.%d] =====\n%s" % (pg["page"], pg["text"]))
        elif is_pdf:
            for pg in d["pages"]:
                parts.append("\n\n===== [论文 PDF：%s · p.%d] =====\n%s" % (title, pg["page"], pg["text"]))
        else:
            label = "代码 README" if kind == "code" else ("网页" if kind in ("blog", "homepage", "other") else
                                                         _RES_LABEL.get(kind, "网页"))
            body = "\n\n".join(pg["text"] for pg in d["pages"])
            parts.append("\n\n===== [%s：%s] =====\n%s" % (label, title, body))
    if not parts:
        return None
    return "".join(parts).strip() + "\n"


# ----------------------------------------------------------------- 当前论文
def write_current(data_dir, pid, iid):
    it = find_item(data_dir, pid, iid)
    cur = {"pid": pid, "iid": iid, "title": it.get("title"), "paper_key": it.get("paper_key"),
           "at": config.now_iso()}
    config.atomic_write_json(os.path.join(data_dir, ".current.json"), cur)
    return cur


def read_current(data_dir):
    c = config.read_json(os.path.join(data_dir, ".current.json"))
    return c if isinstance(c, dict) else None


# ----------------------------------------------------------------- Markdown 渲染
_COLOR_EMOJI = {"yellow": "🟨", "green": "🟩", "blue": "🟦", "pink": "🟥", "purple": "🟪"}
_KIND_LABEL = {"highlight": "高亮", "underline": "下划线", "strike": "删除线", "squiggly": "波浪线"}
_SOURCE_LABEL = {"manual": "手写", "qa": "来自问答", "translation": "来自翻译"}
_ENGINE_LABEL = {"claude": "Claude Code", "codex": "Codex", "google": "Google 翻译"}
_STATUS_LABEL = {"unread": "🆕 未读", "reading": "📖 在读", "done": "✅ 已读"}


def _yaml_str(v):
    return json.dumps("" if v is None else str(v), ensure_ascii=False)


def _short_time(iso):
    if not iso:
        return ""
    return iso[:16].replace("T", " ")


def _quote_block(text):
    lines = (text or "").strip().splitlines() or [""]
    return "\n".join("> " + ln if ln.strip() else ">" for ln in lines)


def _page_key(x):
    pg = x.get("page")
    return (pg if isinstance(pg, int) else 10 ** 6, x.get("created") or x.get("at") or "")


def _md_inline(text):
    """表格/标题里用的单行文本：去换行、转义竖线。"""
    return " ".join(str(text or "").split()).replace("|", "\\|")


_RES_EMOJI = {"paper": "📄", "code": "💻", "homepage": "🏠", "blog": "📝", "video": "🎬", "model": "🤗",
              "dataset": "🤗", "demo": "🤗", "slides": "🖼️", "other": "🔗"}
_RES_NAME = {"paper": "论文", "code": "代码", "homepage": "主页", "blog": "博客", "video": "视频", "model": "模型",
             "dataset": "数据集", "demo": "Demo", "slides": "幻灯片", "other": "链接"}


def _md_link_text(t):
    return _md_inline(t).replace("[", "\\[").replace("]", "\\]")


def _short(t, n=40):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _res_label(r, with_title=True):
    kind = r.get("kind") or "other"
    name = _RES_NAME.get(kind, "链接")
    title = (r.get("title") or "").strip()
    if with_title and title and title != r.get("url") and not title.startswith("arXiv:"):
        if r.get("id") != "main" or kind not in ("paper",):
            name += "：" + _short(title)
    return name


def _resources_line(resources):
    if not resources or len(resources) < 2:
        return None
    counts = {}
    for r in resources:
        counts[r.get("kind")] = counts.get(r.get("kind"), 0) + 1
    bits = []
    for r in resources:
        kind = r.get("kind") or "other"
        with_title = counts.get(kind, 0) > 1 or kind in ("blog", "homepage", "other")
        bits.append("%s [%s](%s)" % (_RES_EMOJI.get(kind, "🔗"), _md_link_text(_res_label(r, with_title)), r.get("url")))
    return "**相关资源**：" + " · ".join(bits)


def _res_heading(r):
    kind = r.get("kind") or "other"
    return "%s %s" % (_RES_EMOJI.get(kind, "🔗"), _res_label(r, True))


def pages_url_of(settings):
    u = ((settings or {}).get("pages_url") or "").strip()
    return (u.rstrip("/") + "/") if u else ""


def render_paper_markdown(state, project_name="", app_url="http://localhost:8765", pages_url=""):
    st = state
    meta = st.get("meta") or {}
    app_url = (app_url or "http://localhost:8765").rstrip("/")
    title = meta.get("title") or st.get("paper_key")
    authors = meta.get("authors") or []
    lines = ["---"]
    lines.append("title: " + _yaml_str(title))
    lines.append("authors: [" + ", ".join(_yaml_str(a) for a in authors) + "]")
    if meta.get("arxiv_id"):
        lines.append("arxiv: " + _yaml_str(meta["arxiv_id"]))
    if meta.get("year"):
        lines.append("year: " + _yaml_str(meta["year"]))
    lines.append("url: " + _yaml_str(meta.get("url") or meta.get("pdf_url") or ""))
    lines.append("project: " + _yaml_str(project_name))
    lines.append("status: " + (st.get("status") or "unread"))
    lines.append("added: " + (st.get("created") or "")[:10])
    lines.append("updated: " + (st.get("updated") or "")[:10])
    lines.append("paperdesk: " + _yaml_str("%s/r/%s/%s" % (app_url, st.get("pid"), st.get("iid"))))
    lines.append("---")
    lines.append("")
    lines.append("# " + _md_inline(title))
    lines.append("")
    links = ["[📖 在 PaperDesk 中打开（本地，可编辑）](%s/r/%s/%s)" % (app_url, st.get("pid"), st.get("iid"))]
    if pages_url:
        links.append("[🌐 在线只读版](%s#/r/%s/%s)" % (pages_url, st.get("pid"), st.get("iid")))
    if meta.get("url"):
        links.append("[🔗 原文](%s)" % meta["url"])
    if meta.get("pdf_url") and meta.get("pdf_url") != meta.get("url"):
        links.append("[📄 PDF](%s)" % meta["pdf_url"])
    lines.append(" · ".join(links))
    lines.append("")
    resources = meta.get("resources") or []
    rl = _resources_line(resources)
    if rl:
        lines.append(rl)
        lines.append("")
    if meta.get("type") in ("web", "link") and meta.get("description"):
        lines.append("> " + _md_inline(meta["description"]))
        lines.append("")
    info = []
    if authors:
        a = ", ".join(authors[:6]) + (" 等" if len(authors) > 6 else "")
        info.append("**作者**：" + _md_inline(a))
    if meta.get("year"):
        info.append("**年份**：%s" % meta["year"])
    info.append("**状态**：" + _STATUS_LABEL.get(st.get("status") or "unread", ""))
    if project_name:
        info.append("**项目**：" + _md_inline(project_name))
    lines.append(" ｜ ".join(info))
    lines.append("")
    stats = "笔记 %d 条 · 标注 %d 处 · 问答 %d 次 · 生词 %d 个" % (
        len(st.get("notes") or []), len([a for a in st.get("annotations") or [] if a.get("kind") != "vocab"]),
        len(st.get("qa") or []), len(st.get("vocab") or []))
    lines.append("<sub>%s · 最后更新 %s</sub>" % (stats, _short_time(st.get("updated"))))
    lines.append("")

    if meta.get("abstract"):
        lines.append("<details><summary>摘要（Abstract）</summary>")
        lines.append("")
        lines.append(meta["abstract"].strip())
        lines.append("")
        lines.append("</details>")
        lines.append("")

    summ = st.get("summary")
    if summ and summ.get("markdown"):
        lines.append("## 🧾 总结")
        lines.append("")
        lines.append(summ["markdown"].strip())
        lines.append("")
        lines.append("<sub>由 %s 于 %s 整理</sub>" % (_ENGINE_LABEL.get(summ.get("engine"), summ.get("engine") or ""),
                                                 _short_time(summ.get("at"))))
        lines.append("")

    known_rids = [r.get("id") for r in resources] or ["main"]
    res_by_id = dict((r.get("id"), r) for r in resources)

    def rid_of(x):
        r = x.get("resource_id") or "main"
        return r if r in res_by_id or not resources else "main"

    def groups(records):
        """按资源分组；只有主资源时返回 [(None, records)]（保持旧格式）。"""
        if not any(rid_of(x) != "main" for x in records):
            return [(None, records)]
        out = []
        for rid in known_rids:
            sub = [x for x in records if rid_of(x) == rid]
            if sub:
                out.append((res_by_id.get(rid), sub))
        return out

    is_web = meta.get("type") in ("web", "link")
    notes = sorted(st.get("notes") or [], key=_page_key)
    lines.append("## 📝 笔记")
    lines.append("")
    if not notes:
        lines.append("_还没有笔记。_")
        lines.append("")
    for res, sub in groups(notes):
        if res is not None:
            lines.append("### " + _res_heading(res))
            lines.append("")
        for n in sub:
            if isinstance(n.get("page"), int):
                head = "p.%d" % n["page"]
            elif n.get("anchor") or is_web or (res is not None and res.get("id") != "main"):
                head = "原文" if n.get("quote") else "全文"
            else:
                head = "全文"
            lines.append("#### 📌 %s · %s · %s" % (head, _SOURCE_LABEL.get(n.get("source"), "手写"),
                                                   _short_time(n.get("created"))))
            lines.append("")
            if n.get("quote"):
                lines.append(_quote_block(n["quote"]))
                lines.append("")
            if n.get("text"):
                lines.append(n["text"].strip())
                lines.append("")
            if n.get("detached_from"):
                lines.append("<sub>（原关联资源「%s」已移除）</sub>" % _md_inline(n["detached_from"]))
                lines.append("")

    anns = sorted([a for a in st.get("annotations") or [] if a.get("kind") != "vocab"], key=_page_key)
    lines.append("## 🖍️ 标注")
    lines.append("")
    if not anns:
        lines.append("_还没有标注。_")
        lines.append("")
    ann_rows = []
    for res, sub in groups(anns):
        if res is not None:
            ann_rows.append(("head", res))
        ann_rows.extend(("ann", a) for a in sub)
    for kind_row, a in ann_rows:
        if kind_row == "head":
            lines.append("")
            lines.append("**%s**" % _md_inline(_res_heading(a)))
            lines.append("")
            continue
        q = _md_inline(a.get("quote") or "（图形区域）")
        kind = a.get("kind")
        if kind == "underline":
            qtext = "<ins>“%s”</ins>" % q
        elif kind == "strike":
            qtext = "~~“%s”~~" % q
        elif kind == "squiggly":
            qtext = "<ins>*“%s”*</ins>" % q
        else:
            qtext = "“%s”" % q
        loc = ("**p.%s** " % a.get("page")) if isinstance(a.get("page"), int) else ""
        line = "- %s%s %s：%s" % (loc, _COLOR_EMOJI.get(a.get("color"), "🟨"), _KIND_LABEL.get(kind, "高亮"), qtext)
        lines.append(line)
        if a.get("comment"):
            for cl in a["comment"].strip().splitlines():
                lines.append("  " + ("> " + cl if cl.strip() else ">"))
    lines.append("")

    vocab = sorted(st.get("vocab") or [], key=lambda v: (bool(v.get("mastered")), v.get("lemma") or ""))
    lines.append("## 🔤 生词")
    lines.append("")
    if not vocab:
        lines.append("_还没有生词。_")
        lines.append("")
    else:
        lines.append("| 单词 | 音标 | 词性 | 释义 | 原句 |")
        lines.append("|---|---|---|---|---|")
        for v in vocab:
            word = _md_inline(v.get("word"))
            if v.get("mastered"):
                word += " ✅"
            meaning = "<br>".join(_md_inline(x) for x in (v.get("meaning") or "").splitlines() if x.strip())
            if v.get("context_meaning"):
                meaning += ("<br>" if meaning else "") + "<sub>语境：%s</sub>" % _md_inline(v["context_meaning"])
            ctxs = [{"context": v.get("context"), "page": v.get("page")}] + list(v.get("extra_contexts") or [])
            ctx_cells = []
            for c in ctxs:
                if c.get("context"):
                    ctx_cells.append(_bold_word(c["context"], v.get("word")) + (" (p.%s)" % c["page"] if c.get("page") else ""))
            lines.append("| %s | %s | %s | %s | %s |" % (
                word, _md_inline(v.get("phonetic")), _md_inline(v.get("pos")), meaning or "—",
                "<br>".join(ctx_cells) or "—"))
        lines.append("")

    qas = st.get("qa") or []
    lines.append("## 💬 问答")
    lines.append("")
    if not qas:
        lines.append("_还没有问答。_")
        lines.append("")
    for q in qas:
        question = (q.get("question") or "").strip()
        first = _md_inline(question.splitlines()[0] if question else "（解释原文）")
        if len(first) > 90:
            first = first[:88] + "…"
        lines.append("### ❓ " + first)
        lines.append("")
        meta_bits = [_ENGINE_LABEL.get(q.get("engine"), q.get("engine") or "")]
        if q.get("mode") == "explain":
            meta_bits.append("解释")
        if q.get("style") == "voice":
            meta_bits.append("🎤 语音")
        if isinstance(q.get("page"), int):
            meta_bits.append("p.%d" % q["page"])
        meta_bits.append(_short_time(q.get("at")))
        lines.append("<sub>%s</sub>" % " · ".join(b for b in meta_bits if b))
        lines.append("")
        if question and (len(question) > 90 or "\n" in question):
            lines.append("**问题：** " + question)
            lines.append("")
        if q.get("quote"):
            lines.append(_quote_block(q["quote"]))
            lines.append("")
        lines.append((q.get("answer") or "").strip() or "_（无回答）_")
        lines.append("")
    lines.append("---")
    lines.append("<sub>由 PaperDesk 自动生成。%s不保存在本仓库，打开时从原链接实时加载。</sub>"
                 % ("网页正文" if is_web else "PDF"))
    lines.append("")
    return "\n".join(lines)


def paper_markdown(data_dir, pid, iid):
    with _LOCK:
        p, it, st = _paper_ctx(data_dir, pid, iid)
        settings = config.load_settings(data_dir)
        return render_paper_markdown(st, p.get("name", pid), settings.get("app_url"), pages_url_of(settings))


def _render_project_readme(data_dir, p, app_url, pages_url=""):
    items = p.get("items") or []
    papers = [i for i in items if i.get("type") == "paper"]
    links = [i for i in items if i.get("type") in ("web", "link")]
    notes = [i for i in items if i.get("type") == "note"]

    def res_icons(it):
        rs = it.get("resources") or []
        if not rs:
            return ""
        return " ".join("[%s](%s \"%s\")" % (_RES_EMOJI.get(r.get("kind"), "🔗"), r.get("url"),
                                                _md_inline(_res_label(r)).replace('"', "'")) for r in rs)
    L = ["# 📁 " + _md_inline(p.get("name") or p["id"]), ""]
    if p.get("description"):
        L.append(_quote_block(p["description"]))
        L.append("")
    online = (" · [🌐 在线只读版](%s#/p/%s)" % (pages_url, p["id"])) if pages_url else ""
    L.append("[🗂️ 在 PaperDesk 中打开这个项目（本地）](%s/p/%s)%s · 论文 %d 篇 · 网页 / 链接 %d 个 · 笔记 %d 条 · [⬅️ 返回全部项目](../../README.md)"
             % (app_url, p["id"], online, len(papers), len(links), len(notes)))
    L.append("")
    L.append("## 📄 论文")
    L.append("")
    if papers:
        L.append("| 状态 | 论文 | 阅读 | 笔记 | 添加于 |")
        L.append("|:---:|---|:---:|:---:|:---:|")
        for it in papers:
            title = _md_inline(it.get("title") or it.get("paper_key"))
            href = it.get("url") or it.get("pdf_url") or ""
            sub = []
            if it.get("authors"):
                a = it["authors"]
                sub.append(_md_inline(a[0] + (" et al." if len(a) > 1 else "")))
            if it.get("year"):
                sub.append(str(it["year"]))
            if it.get("tags"):
                sub.append(" ".join("`#%s`" % _md_inline(t) for t in it["tags"]))
            cell = "[%s](%s)" % (title, href) if href else title
            icons = res_icons(it)
            if icons:
                sub.append(icons)
            if sub:
                cell += "<br><sub>%s</sub>" % " · ".join(sub)
            key = it.get("paper_key")
            st = _load_state(data_dir, p["id"], key) if key else None
            ncount = ""
            if st is not None:
                n = len(st.get("notes") or []) + len(st.get("annotations") or [])
                ncount = " (%d)" % n if n else ""
            note_cell = "[📝 笔记%s](papers/%s.md)" % (ncount, key) if key else ""
            read_cell = "[📖 本地打开](%s/r/%s/%s)" % (app_url, p["id"], it["id"])
            if pages_url:
                read_cell += "<br>[🌐 在线只读](%s#/r/%s/%s)" % (pages_url, p["id"], it["id"])
            L.append("| %s | %s | %s | %s | %s |" % (
                _STATUS_LABEL.get(it.get("status"), ""), cell, read_cell, note_cell, (it.get("added") or "")[:10]))
        L.append("")
    else:
        L.append("_还没有论文。_")
        L.append("")
    L.append("## 🌐 网页与链接")
    L.append("")
    if links:
        L.append("| 类型 | 网页 | 阅读 | 笔记 | 添加于 |")
        L.append("|:---:|---|:---:|:---:|:---:|")
        for it in links:
            kind = it.get("kind") or "blog"
            title = _md_inline(it.get("title") or it.get("url"))
            cell = "[%s](%s)" % (title, it.get("url"))
            sub = []
            if it.get("site_name"):
                sub.append(_md_inline(it["site_name"]))
            if it.get("tags"):
                sub.append(" ".join("`#%s`" % _md_inline(t) for t in it["tags"]))
            icons = res_icons(it)
            if icons:
                sub.append(icons)
            if sub:
                cell += "<br><sub>%s</sub>" % " · ".join(sub)
            key = it.get("paper_key")
            st = _load_state(data_dir, p["id"], key) if key else None
            note_cell = ""
            if st is not None:
                n = len(st.get("notes") or []) + len(st.get("annotations") or [])
                note_cell = "[📝 笔记%s](papers/%s.md)" % (" (%d)" % n if n else "", key)
            read_cell = "[📖 本地打开](%s/r/%s/%s)" % (app_url, p["id"], it["id"])
            if pages_url:
                read_cell += "<br>[🌐 在线只读](%s#/r/%s/%s)" % (pages_url, p["id"], it["id"])
            L.append("| %s %s | %s | %s | %s | %s |" % (
                _RES_EMOJI.get(kind, "🔗"), _RES_NAME.get(kind, "链接"), cell, read_cell, note_cell,
                (it.get("added") or "")[:10]))
        L.append("")
    else:
        L.append("_还没有网页或链接。_")
        L.append("")
    L.append("## 🗒️ 笔记")
    L.append("")
    if notes:
        for it in notes:
            L.append("### " + _md_inline(it.get("title") or "笔记"))
            L.append("")
            L.append("<sub>%s</sub>" % _short_time(it.get("added")))
            L.append("")
            if it.get("note"):
                L.append(it["note"].strip())
                L.append("")
    else:
        L.append("_还没有笔记。_")
        L.append("")
    L.append("---")
    L.append("<sub>由 PaperDesk 自动生成，请不要手动编辑此文件。</sub>")
    L.append("")
    return "\n".join(L)


def _render_root_readme(data_dir, app_url, pages_url=""):
    projs = list_projects(data_dir)
    L = ["# 📚 我的论文库", "",
         "> 由 PaperDesk 管理。本地打开（可编辑）：[%s](%s)。PDF 不保存在仓库里，只保存项目、链接、笔记、标注与问答。" % (app_url, app_url), ""]
    if pages_url:
        L.append("**[🌐 在线只读版](%s)** · [🔤 在线生词本](%s#/vocab)" % (pages_url, pages_url))
        L.append("")
    tp = sum(p["counts"]["paper"] for p in projs)
    nv = len(list_all_vocab(data_dir)) if os.path.exists(os.path.join(data_dir, "vocabulary.md")) else 0
    L.append("共 **%d** 个项目 · **%d** 篇论文 · [🔤 生词本（%d 个）](vocabulary.md)" % (len(projs), tp, nv))
    L.append("")
    L.append("| 项目 | 论文 | 链接 | 笔记 | 最近更新 |")
    L.append("|---|:---:|:---:|:---:|:---:|")
    for p in projs:
        desc = ("<br><sub>%s</sub>" % _md_inline(p["description"])) if p.get("description") else ""
        L.append("| [📁 %s](projects/%s/README.md)%s | %d | %d | %d | %s |" % (
            _md_inline(p["name"]), p["id"], desc, p["counts"]["paper"], p["counts"]["link"], p["counts"]["note"],
            (p.get("updated") or "")[:10]))
    L.append("")
    L.append("---")
    L.append("<sub>由 PaperDesk 自动生成，请不要手动编辑此文件。</sub>")
    L.append("")
    return "\n".join(L)


def refresh_readmes(data_dir, pid=None):
    with _LOCK:
        settings = config.load_settings(data_dir)
        app_url = (settings.get("app_url") or "http://localhost:8765").rstrip("/")
        pages_url = pages_url_of(settings)
        if pid:
            try:
                p = _load_project(data_dir, pid)
                config.atomic_write_text(os.path.join(_project_dir(data_dir, pid), "README.md"),
                                         _render_project_readme(data_dir, p, app_url, pages_url))
            except NotFound:
                pass
        else:
            for s in list_projects(data_dir):
                p = _load_project(data_dir, s["id"])
                config.atomic_write_text(os.path.join(_project_dir(data_dir, s["id"]), "README.md"),
                                         _render_project_readme(data_dir, p, app_url, pages_url))
        config.atomic_write_text(os.path.join(data_dir, "README.md"),
                                 _render_root_readme(data_dir, app_url, pages_url))


def rerender_all(data_dir):
    """app_url 等设置变化后，重写所有论文 md 和 README。"""
    with _LOCK:
        for s in list_projects(data_dir):
            p = _load_project(data_dir, s["id"])
            for it in p["items"]:
                if it.get("type") in STATEFUL_TYPES and it.get("paper_key"):
                    st = _load_state(data_dir, p["id"], it["paper_key"])
                    if st is not None:
                        st["meta"]["resources"] = item_resources(it)
                        _write_state(data_dir, p["id"], st, p)
        refresh_readmes(data_dir)
        refresh_vocabulary(data_dir)



# ----------------------------------------------------------------- GitHub Pages 只读站导出
_STATIC_TEXT_EXT = (".js", ".mjs", ".css", ".html", ".htm")
_RE_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(\s*|\bimport\s+|\bexport\s*\*\s*from\s*|\bnew\s+URL\(\s*)(['"])/static/""")
_RE_STR = re.compile(r"""(['"`])/static/""")
_RE_CSS_URL = re.compile(r"""(url\(\s*['"]?|@import\s+['"])/static/""")


def _rewrite_static_text(rel, text):
    """把前端里的 /static/ 绝对路径改成可在 GitHub Pages 子路径下工作的相对路径。
    rel：文件相对 static/ 的路径（用 / 分隔）。
      - JS 模块说明符（import/export from、import()、new URL(…, import.meta.url)）→ 相对模块自身（../vendor/x.js）
      - JS 里其它字符串（如 pdf.js worker 地址）→ 相对页面（static/...，页面位于站点根）
      - CSS url()/@import → 相对 CSS 文件自身
      - HTML → 相对文件自身
    """
    up = "../" * rel.count("/") or "./"
    ext = os.path.splitext(rel)[1].lower()
    if ext in (".js", ".mjs"):
        text = _RE_IMPORT.sub(lambda m: m.group(1) + m.group(2) + up, text)
        text = _RE_STR.sub(lambda m: m.group(1) + "static/", text)
    elif ext == ".css":
        text = _RE_CSS_URL.sub(lambda m: m.group(1) + up, text)
    else:
        text = text.replace('"/static/', '"' + up).replace("'/static/", "'" + up)
    return text


def _write_if_changed(path, data):
    """data: bytes。内容相同则不写（避免无意义的 git 提交）。返回是否写入。"""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-pd"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def _write_json_if_changed(path, obj, ignore=("generated",)):
    old = config.read_json(path)
    if isinstance(old, dict) and isinstance(obj, dict):
        a = {k: v for k, v in old.items() if k not in ignore}
        b = {k: v for k, v in obj.items() if k not in ignore}
        if a == b:
            return False
    elif old == obj:
        return False
    return _write_if_changed(path, (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def _static_index_html(web_index, base_path=None):
    with open(web_index, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/", "static/")
    inject = "<script>window.PAPERDESK_STATIC=true;</script>"
    if base_path:
        inject += '<base href="%s">' % base_path
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        html = html[:m.end()] + inject + html[m.end():]
    else:
        html = inject + html
    return html


def _sync_static_dir(data_dir, web_dir):
    """增量把 web/ 拷贝到 <data>/static/；返回 (copied, removed)。"""
    dst_root = os.path.join(data_dir, "static")
    manifest_path = os.path.join(data_dir, ".cache", "static-manifest.json")
    manifest = config.read_json(manifest_path, {}) or {}
    new_manifest = {}
    seen = set()
    copied = 0
    for root, dirs, files in os.walk(web_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for name in sorted(files):
            if name.startswith(".") or name.endswith((".pyc", ".tmp-pd")):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, web_dir).replace(os.sep, "/")
            dst = os.path.join(dst_root, *rel.split("/"))
            seen.add(rel)
            st = os.stat(src)
            sig = [st.st_size, int(st.st_mtime)]
            new_manifest[rel] = sig
            if manifest.get(rel) == sig and os.path.exists(dst):
                continue
            ext = os.path.splitext(name)[1].lower()
            with open(src, "rb") as f:
                data = f.read()
            if ext in _STATIC_TEXT_EXT and b"/static/" in data:
                try:
                    data = _rewrite_static_text(rel, data.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError:
                    pass
            if _write_if_changed(dst, data):
                copied += 1
    removed = 0
    if os.path.isdir(dst_root):
        for root, dirs, files in os.walk(dst_root, topdown=False):
            for name in files:
                p = os.path.join(root, name)
                rel = os.path.relpath(p, dst_root).replace(os.sep, "/")
                if rel not in seen:
                    os.remove(p)
                    removed += 1
            if root != dst_root and not os.listdir(root):
                os.rmdir(root)
    config.atomic_write_json(manifest_path, new_manifest)
    return copied, removed


def export_static_site(data_dir, web_dir):
    """导出 GitHub Pages 只读站需要的文件。可重复调用；内容不变则不改动任何文件。"""
    import urllib.parse as _up
    with _LOCK:
        settings = config.load_settings(data_dir)
        app_url = (settings.get("app_url") or "http://localhost:8765").rstrip("/")
        pages_url = pages_url_of(settings)
        result = {"library": False, "vocabulary": False, "index": False, "copied": 0, "removed": 0,
                  "pages_url": pages_url, "web": os.path.isfile(os.path.join(web_dir, "index.html"))}
        lib = {"generated": config.now_iso(), "app_url": app_url, "pages_url": pages_url,
               "projects": [{k: p[k] for k in ("id", "name", "description", "updated", "counts")}
                            for p in list_projects(data_dir)]}
        result["library"] = _write_json_if_changed(os.path.join(data_dir, "library.json"), lib)
        result["vocabulary"] = _write_json_if_changed(os.path.join(data_dir, "vocabulary.json"),
                                                      list_all_vocab(data_dir))
        nj = os.path.join(data_dir, ".nojekyll")
        if not os.path.exists(nj):
            _write_if_changed(nj, b"")
        web_index = os.path.join(web_dir, "index.html")
        if result["web"]:
            idx = _static_index_html(web_index)
            result["index"] = _write_if_changed(os.path.join(data_dir, "index.html"), idx.encode("utf-8"))
            base_path = _up.urlparse(pages_url).path if pages_url else None
            nf = _static_index_html(web_index, base_path or None)
            _write_if_changed(os.path.join(data_dir, "404.html"), nf.encode("utf-8"))
            result["copied"], result["removed"] = _sync_static_dir(data_dir, web_dir)
        return result



# ----------------------------------------------------------------- 多台电脑：三方语义合并
_DATA_RE = re.compile(r"^(?:projects/[^/]+/project\.json|projects/[^/]+/papers/[^/]+\.json|settings\.json)$")
_STATE_LISTS = ("annotations", "notes", "qa", "translations", "vocab")


def is_data_file(path):
    return bool(_DATA_RE.match(path or ""))


def is_library_empty(data_dir):
    d = _projects_dir(data_dir)
    if not os.path.isdir(d):
        return True
    for pid in os.listdir(d):
        pdir = os.path.join(d, pid)
        if not os.path.isdir(pdir):
            continue
        if pid != INBOX_ID:
            return False
        p = config.read_json(os.path.join(pdir, "project.json"), {}) or {}
        if p.get("items"):
            return False
        papers = os.path.join(pdir, "papers")
        if os.path.isdir(papers) and any(f.endswith(".json") for f in os.listdir(papers)):
            return False
    return True


def _ts(x):
    if not isinstance(x, dict):
        return ""
    v = x.get("updated") or x.get("created") or x.get("at") or x.get("added") or ""
    try:
        return _dt.datetime.fromisoformat(v).astimezone(_dt.timezone.utc).isoformat()
    except (ValueError, TypeError):
        return str(v)


def _newer(o, t):
    return t if _ts(t) > _ts(o) else o


def _merge_list(base, ours, theirs, key="id"):
    def m(lst):
        return dict((x[key], x) for x in (lst or []) if isinstance(x, dict) and x.get(key))
    bm, om, tm = m(base), m(ours), m(theirs)
    order = [x[key] for x in (ours or []) if isinstance(x, dict) and x.get(key)]
    order += [k for k in [x[key] for x in (theirs or []) if isinstance(x, dict) and x.get(key)] if k not in om]
    out = []
    for k in order:
        o, t, b = om.get(k), tm.get(k), bm.get(k)
        if o is not None and t is not None:
            out.append(o if o == t else _newer(o, t))
        elif o is not None:
            if b is not None and o == b:
                continue            # 对方删除、我方没改 → 删除
            out.append(o)
        elif t is not None:
            if b is not None and t == b:
                continue
            out.append(t)
    return out


def _merge_dict_keys(base, ours, theirs, skip=()):
    out = {}
    base = base or {}
    for k in list(ours.keys()) + [k for k in theirs.keys() if k not in ours]:
        if k in skip:
            continue
        o, t, b = ours.get(k), theirs.get(k), base.get(k)
        if k not in theirs:
            out[k] = o
        elif k not in ours:
            out[k] = t
        elif o == t or t == b:
            out[k] = o
        elif o == b:
            out[k] = t
        else:
            out[k] = o
    return out


def _dedupe_vocab(vocab):
    by = {}
    out = []
    for v in sorted(vocab, key=lambda x: x.get("created") or ""):
        lem = v.get("lemma") or lemma_of(v.get("word"))
        if lem in by:
            keep = by[lem]
            known = [keep.get("context")] + [c.get("context") for c in keep.get("extra_contexts") or []]
            extra = keep.setdefault("extra_contexts", [])
            for c in [{"context": v.get("context"), "page": v.get("page"), "annotation_id": v.get("annotation_id"),
                       "context_translation": v.get("context_translation"), "created": v.get("created")}] + \
                    list(v.get("extra_contexts") or []):
                if c.get("context") and c["context"] not in known:
                    extra.append(c)
                    known.append(c["context"])
            for k in ("meaning", "phonetic", "pos", "context_meaning"):
                if not keep.get(k) and v.get(k):
                    keep[k] = v[k]
            keep["mastered"] = bool(keep.get("mastered") or v.get("mastered"))
            continue
        v = dict(v)
        by[lem] = v
        out.append(v)
    return out


def _merge_paper_state(base, ours, theirs):
    top = dict(_newer(ours, theirs))
    for name in _STATE_LISTS:
        top[name] = _merge_list((base or {}).get(name), ours.get(name), theirs.get(name))
    top["vocab"] = _dedupe_vocab(top.get("vocab") or [])
    for name in ("annotations", "notes", "vocab"):
        top[name].sort(key=lambda x: x.get("created") or "")
    top["qa"].sort(key=lambda x: x.get("at") or "")
    top["translations"] = sorted(top["translations"], key=lambda x: x.get("at") or "")[-200:]
    if not top.get("summary"):
        top["summary"] = ours.get("summary") or theirs.get("summary")
    top["updated"] = max(ours.get("updated") or "", theirs.get("updated") or "")
    return top


def _merge_project(base, ours, theirs):
    top = dict(_newer(ours, theirs))
    items = _merge_list((base or {}).get("items"), ours.get("items"), theirs.get("items"))
    seen_keys = {}
    deduped = []
    for it in sorted(items, key=lambda x: (_ts(dict(added=x.get("added"))), x.get("id") or "")):
        if it.get("type") == "paper" and it.get("paper_key"):
            if it["paper_key"] in seen_keys:
                continue            # 两台电脑各自加了同一篇论文 → 保留先加的那条
            seen_keys[it["paper_key"]] = it["id"]
        deduped.append(it)
    deduped.sort(key=lambda x: x.get("added") or "", reverse=True)
    top["items"] = deduped
    top["updated"] = max(ours.get("updated") or "", theirs.get("updated") or "")
    return top


def merge_data_file(path, base, ours, theirs):
    """三方合并一个数据文件（已解析的 dict，缺失为 None）。返回合并结果，None 表示应删除。"""
    if ours is None and theirs is None:
        return None
    if ours is None or theirs is None:
        cur = ours if ours is not None else theirs
        if base is not None and cur == base:
            return None             # 一方删除、另一方没改 → 删除
        return cur
    if ours == theirs:
        return ours
    if path == "settings.json":
        return _merge_dict_keys(base, ours, theirs, skip=config.LOCAL_KEYS)
    if path.endswith("/project.json"):
        return _merge_project(base, ours, theirs)
    return _merge_paper_state(base, ours, theirs)
