#!/usr/bin/env python3
"""PaperDesk MCP server (stdio, JSON-RPC 2.0, newline-delimited).

Lets a terminal Claude Code / Codex session read the paper you are currently
reading in PaperDesk, your notes, the extracted paper text, and append notes.

    claude mcp add paperdesk -- python3 /path/to/paperdesk/mcp_server.py

Data directory: $PAPERDESK_HOME, or --data DIR, or ~/PaperDesk-Library.
Only the Python standard library is used.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SERVER_INFO = {"name": "paperdesk", "version": "0.1.0"}
DEFAULT_PROTOCOL = "2025-06-18"


# --------------------------------------------------------------------------- data dir

def resolve_data_dir(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--data" and i + 1 < len(argv):
            return os.path.abspath(os.path.expanduser(argv[i + 1]))
        if a.startswith("--data="):
            return os.path.abspath(os.path.expanduser(a.split("=", 1)[1]))
    env = os.environ.get("PAPERDESK_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), "PaperDesk-Library")


DATA_DIR = resolve_data_dir()

_store = None
if not os.environ.get("PAPERDESK_MCP_NO_STORE"):
    try:  # Prefer the real store for writes (keeps md / README in sync).
        from paperdesk import store as _store  # type: ignore
    except Exception:  # pragma: no cover - depends on parallel branch
        _store = None


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def projects_dir():
    return os.path.join(DATA_DIR, "projects")


def load_project(pid):
    return read_json(os.path.join(projects_dir(), pid, "project.json"))


def all_projects():
    out = []
    root = projects_dir()
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = read_json(os.path.join(root, name, "project.json"))
        if isinstance(p, dict):
            p.setdefault("id", name)
            out.append(p)
    return out


class ToolError(Exception):
    pass


def find_project(ref):
    if not ref:
        raise ToolError("缺少参数 project（项目 id 或名称）")
    ref_s = str(ref).strip()
    projects = all_projects()
    for p in projects:
        if p.get("id") == ref_s:
            return p
    low = ref_s.lower()
    for p in projects:
        if str(p.get("name", "")).lower() == low:
            return p
    matches = [p for p in projects if low in str(p.get("name", "")).lower()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in projects) or "（暂无项目）"
    raise ToolError(f"找不到项目「{ref_s}」。现有项目：{names}")


def find_item(project, ref):
    items = project.get("items") or []
    if not ref:
        raise ToolError("缺少参数 item（条目 id、paper_key、arXiv ID 或标题片段）")
    ref_s = str(ref).strip()
    for it in items:
        if ref_s in (it.get("id"), it.get("paper_key"), it.get("arxiv_id")):
            return it
    low = ref_s.lower()
    matches = [it for it in items if low in str(it.get("title", "")).lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError("有多个条目匹配：" + "; ".join(f"{m.get('title')} ({m.get('id')})" for m in matches))
    raise ToolError(f"在项目「{project.get('name')}」中找不到条目「{ref_s}」")


def paper_paths(pid, item):
    key = item.get("paper_key") or item.get("id")
    base = os.path.join(projects_dir(), pid, "papers", key)
    return key, base + ".json", base + ".md"


# --------------------------------------------------------------------------- rendering fallbacks

def render_md_fallback(item, state):
    lines = [f"# {item.get('title') or state.get('meta', {}).get('title') or '(untitled)'}", ""]
    if item.get("url"):
        lines.append(f"原文：{item['url']}")
        lines.append("")
    summ = state.get("summary") or {}
    if summ.get("markdown"):
        lines += ["## 总结", "", summ["markdown"], ""]
    notes = sorted(state.get("notes") or [], key=lambda n: (n.get("page") or 0))
    if notes:
        lines += ["## 笔记", ""]
        for n in notes:
            pg = f"（p.{n['page']}）" if n.get("page") else ""
            if n.get("quote"):
                lines.append(f"> {n['quote']}{pg}")
                lines.append("")
            lines.append(n.get("text", ""))
            lines.append("")
    anns = sorted(state.get("annotations") or [], key=lambda a: (a.get("page") or 0))
    if anns:
        lines += ["## 标注", ""]
        for a in anns:
            c = f" — {a['comment']}" if a.get("comment") else ""
            lines.append(f"- p.{a.get('page')} {a.get('kind')}：“{a.get('quote', '')}”{c}")
        lines.append("")
    qa = state.get("qa") or []
    if qa:
        lines += ["## 问答", ""]
        for q in qa:
            lines += [f"### Q: {q.get('question', '')}", "", q.get("answer", ""), ""]
    return "\n".join(lines)


def paper_key_for(url=None, arxiv_id=None):
    if arxiv_id:
        return "arxiv-" + re.sub(r"v\d+$", "", arxiv_id).replace("/", "_")
    return "url-" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]


ARXIV_NEW = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])")
ARXIV_OLD = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")


def simple_resolve(text):
    text = (text or "").strip()
    if not text:
        raise ToolError("缺少参数 input")
    m = None
    if "arxiv.org" in text or not text.startswith("http"):
        m = ARXIV_NEW.search(text) or ARXIV_OLD.search(text)
    if m:
        aid = m.group(1)
        return {"type": "paper", "title": f"arXiv:{aid}", "url": f"https://arxiv.org/abs/{aid}",
                "pdf_url": f"https://arxiv.org/pdf/{aid}", "arxiv_id": aid, "authors": [],
                "paper_key": paper_key_for(arxiv_id=aid)}
    if text.startswith("http"):
        om = re.search(r"openreview\.net/(?:forum|pdf)\?id=([\w\-]+)", text)
        if om:
            pdf = f"https://openreview.net/pdf?id={om.group(1)}"
            return {"type": "paper", "title": text, "url": text, "pdf_url": pdf, "arxiv_id": None,
                    "authors": [], "paper_key": paper_key_for(url=pdf)}
        if text.lower().split("?")[0].endswith(".pdf"):
            return {"type": "paper", "title": text.rsplit("/", 1)[-1], "url": text, "pdf_url": text,
                    "arxiv_id": None, "authors": [], "paper_key": paper_key_for(url=text)}
        return {"type": "link", "title": text, "url": text}
    return {"type": "note", "title": text[:40], "note": text}


# --------------------------------------------------------------------------- tools

def tool_list_projects(_args):
    out = []
    for p in all_projects():
        counts = {"paper": 0, "link": 0, "note": 0}
        for it in p.get("items") or []:
            t = it.get("type")
            if t in counts:
                counts[t] += 1
        out.append({"id": p.get("id"), "name": p.get("name"), "description": p.get("description", ""),
                    "counts": counts, "updated": p.get("updated")})
    return {"data_dir": DATA_DIR, "projects": out}


def tool_list_items(args):
    p = find_project(args.get("project"))
    items = []
    for it in p.get("items") or []:
        d = {k: it.get(k) for k in ("id", "type", "title", "url", "arxiv_id", "status", "tags", "added", "paper_key")}
        if it.get("type") == "note":
            d["note"] = it.get("note", "")
        items.append(d)
    return {"project": {"id": p.get("id"), "name": p.get("name")}, "items": items}


def tool_get_current_paper(_args):
    cur = read_json(os.path.join(DATA_DIR, ".current.json"))
    if not cur or not cur.get("pid"):
        raise ToolError("当前没有正在阅读的论文（请先在 PaperDesk 中打开一篇论文）")
    pid, iid = cur.get("pid"), cur.get("iid")
    p = load_project(pid)
    if not p:
        raise ToolError(f"当前论文所在项目 {pid} 不存在")
    item = find_item(p, iid)
    key, jpath, mpath = paper_paths(pid, item)
    state = read_json(jpath, {}) or {}
    text_cached = os.path.exists(os.path.join(DATA_DIR, ".cache", "text", pid, key + ".json"))
    return {"project": {"id": pid, "name": p.get("name")}, "item": item, "opened_at": cur.get("at"),
            "last_page": state.get("last_page"), "notes_count": len(state.get("notes") or []),
            "annotations_count": len(state.get("annotations") or []), "qa_count": len(state.get("qa") or []),
            "has_summary": bool(state.get("summary")), "text_cached": text_cached,
            "notes_markdown_path": os.path.relpath(mpath, DATA_DIR),
            "hint": "用 get_paper_notes / get_paper_text（project 与 item 用上面的 id）读取内容。"}


def tool_get_paper_notes(args):
    p = find_project(args.get("project"))
    item = find_item(p, args.get("item"))
    if item.get("type") == "note":
        return item.get("note", "")
    key, jpath, mpath = paper_paths(p["id"], item)
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            return f.read()
    state = read_json(jpath)
    if state:
        return render_md_fallback(item, state)
    return f"# {item.get('title')}\n\n（这篇论文还没有笔记）"


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    pages = set()
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else total
            pages.update(range(max(1, a), min(total, b) + 1))
        else:
            pages.add(int(part))
    return sorted(x for x in pages if 1 <= x <= total)


def tool_get_paper_text(args):
    p = find_project(args.get("project"))
    item = find_item(p, args.get("item"))
    key = item.get("paper_key") or item.get("id")
    cache = read_json(os.path.join(DATA_DIR, ".cache", "text", p["id"], key + ".json"))
    if not cache:
        raise ToolError("这篇论文的全文还没缓存：请先在 PaperDesk 中打开它一次（阅读页会自动抽取文字）")
    pages = cache.get("pages") if isinstance(cache, dict) else cache
    pages = pages or []
    by_no = {}
    for i, pg in enumerate(pages):
        if isinstance(pg, dict):
            by_no[int(pg.get("page", i + 1))] = pg.get("text", "")
        else:
            by_no[i + 1] = str(pg)
    total = max(by_no) if by_no else 0
    try:
        wanted = parse_pages(args.get("pages"), total)
    except ValueError:
        raise ToolError("pages 格式应类似 \"3-5\" 或 \"1,4,7-9\"")
    parts = [f"===== [p.{n}] =====\n{by_no.get(n, '')}" for n in wanted]
    return f"《{item.get('title')}》共 {total} 页，以下为第 {args.get('pages') or '全部'} 页：\n\n" + "\n\n".join(parts)


def _fallback_add_note(pid, item, note):
    key, jpath, _ = paper_paths(pid, item)
    state = read_json(jpath) or {
        "pid": pid, "iid": item.get("id"), "paper_key": key,
        "meta": {k: item.get(k) for k in ("title", "authors", "url", "pdf_url", "arxiv_id", "year", "abstract")},
        "annotations": [], "notes": [], "qa": [], "translations": [], "summary": None,
        "sessions": {"claude": None, "codex": None}, "last_page": 1, "zoom": 1.25,
        "status": item.get("status", "unread"), "created": now_iso(), "updated": now_iso()}
    state.setdefault("notes", []).append(note)
    state["updated"] = now_iso()
    write_json_atomic(jpath, state)
    return note


def tool_add_note(args):
    p = find_project(args.get("project"))
    item = find_item(p, args.get("item"))
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("缺少参数 text（笔记正文，支持 Markdown）")
    if item.get("type") != "paper":
        raise ToolError("只能给论文条目添加笔记")
    note = {"text": text, "source": "manual"}
    if args.get("page") not in (None, ""):
        note["page"] = int(args["page"])
    if args.get("quote"):
        note["quote"] = args["quote"]
    if _store is not None and hasattr(_store, "add_note"):
        try:
            saved = _store.add_note(DATA_DIR, p["id"], item["id"], dict(note))
            return {"ok": True, "note": saved, "via": "store"}
        except TypeError:
            pass
    ts = now_iso()
    note.update({"id": "n_" + secrets.token_hex(4), "created": ts, "updated": ts,
                 "page": note.get("page"), "quote": note.get("quote")})
    _fallback_add_note(p["id"], item, note)
    return {"ok": True, "note": note, "via": "file",
            "warning": "已写入 JSON；对应的 Markdown 会在 PaperDesk 服务下次保存这篇论文时重新渲染"}


def tool_add_item(args):
    p = find_project(args.get("project"))
    raw = (args.get("input") or "").strip()
    if not raw:
        raise ToolError("缺少参数 input（arXiv ID / 链接 / 文字笔记）")
    if _store is not None and hasattr(_store, "add_item"):
        # store.add_item expects already-resolved fields; resolve with fetcher (network,
        # arXiv metadata) when available, else with the offline resolver.
        resolved = None
        try:
            from paperdesk import fetcher as _fetcher  # type: ignore
            resolved = _fetcher.resolve(raw)
        except Exception:
            resolved = None
        if not resolved:
            resolved = simple_resolve(raw)
        try:
            res = _store.add_item(DATA_DIR, p["id"], dict(resolved))
            return {"ok": True, "item": res, "existing": bool(res.get("existing")), "via": "store"}
        except TypeError:
            pass
    r = simple_resolve(raw)
    ts = now_iso()
    project = load_project(p["id"])
    for it in project.get("items") or []:
        if r.get("paper_key") and it.get("paper_key") == r["paper_key"]:
            return {"ok": True, "item": it, "existing": True}
    item = {"id": secrets.token_hex(4), "type": r["type"], "title": r.get("title"), "url": r.get("url"),
            "pdf_url": r.get("pdf_url"), "arxiv_id": r.get("arxiv_id"), "authors": r.get("authors", []),
            "year": None, "abstract": None, "paper_key": r.get("paper_key"), "note": r.get("note", ""),
            "tags": [], "status": "unread", "added": ts, "updated": ts}
    project.setdefault("items", []).append(item)
    project["updated"] = ts
    write_json_atomic(os.path.join(projects_dir(), p["id"], "project.json"), project)
    return {"ok": True, "item": item, "via": "file",
            "warning": "已写入 project.json；README 索引会在 PaperDesk 服务下次保存时更新"}


def tool_list_vocab(args):
    """All vocabulary entries (optionally one project / mastered filter), newest first."""
    projects = [find_project(args["project"])] if args.get("project") else all_projects()
    mastered = args.get("mastered")
    if isinstance(mastered, str):
        mastered = mastered.strip().lower() in ("1", "true", "yes")
    out = []
    for p in projects:
        pid = p.get("id")
        for it in p.get("items") or []:
            if it.get("type") != "paper":
                continue
            _, jpath, _ = paper_paths(pid, it)
            state = read_json(jpath) or {}
            for v in state.get("vocab") or []:
                if mastered is not None and bool(v.get("mastered")) != bool(mastered):
                    continue
                e = {k: v.get(k) for k in ("id", "word", "lemma", "phonetic", "pos", "meaning", "context_meaning",
                                           "context", "context_translation", "page", "mastered", "created")}
                e.update({"pid": pid, "iid": it.get("id"), "paper_title": it.get("title"),
                          "paper_key": it.get("paper_key")})
                out.append(e)
    out.sort(key=lambda e: e.get("created") or "", reverse=True)
    return {"count": len(out), "vocab": out}


TOOLS = [
    {"name": "list_projects", "description": "列出 PaperDesk 中的所有项目（文件夹）及其论文/链接/笔记数量。",
     "inputSchema": {"type": "object", "properties": {}}, "handler": tool_list_projects},
    {"name": "list_items", "description": "列出某个项目里的条目（论文、链接、文字笔记）。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目 id 或名称"}}, "required": ["project"]},
     "handler": tool_list_items},
    {"name": "get_current_paper", "description": "返回用户此刻在 PaperDesk 阅读页中打开的论文（项目、条目、笔记数量等）。",
     "inputSchema": {"type": "object", "properties": {}}, "handler": tool_get_current_paper},
    {"name": "get_paper_notes", "description": "读取某篇论文的完整笔记 Markdown（总结、笔记、标注、问答）。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目 id 或名称"},
         "item": {"type": "string", "description": "条目 id、paper_key、arXiv ID 或标题片段"}},
         "required": ["project", "item"]}, "handler": tool_get_paper_notes},
    {"name": "get_paper_text", "description": "读取论文正文（PaperDesk 打开时抽取的文字），可按页范围读取。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "item": {"type": "string"},
         "pages": {"type": "string", "description": "页范围，如 \"3-5\" 或 \"1,4,7-9\"；省略为全部"}},
         "required": ["project", "item"]}, "handler": tool_get_paper_text},
    {"name": "add_note", "description": "给某篇论文追加一条笔记（Markdown，可选页码与引文）。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "item": {"type": "string"},
         "text": {"type": "string", "description": "笔记正文，支持 **加粗**、<ins>下划线</ins>、$公式$"},
         "page": {"type": "integer"}, "quote": {"type": "string", "description": "可选：引用的原文"}},
         "required": ["project", "item", "text"]}, "handler": tool_add_note},
    {"name": "add_item", "description": "往项目里添加条目：arXiv ID/链接、PDF 链接、普通网页链接或一段文字笔记。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "input": {"type": "string"}}, "required": ["project", "input"]},
     "handler": tool_add_item},
    {"name": "list_vocab", "description": "列出生词本：全库（或某个项目）在 PaperDesk 里收藏的生词，含音标、释义、原句与来源论文。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "可选：只看某个项目"},
         "mastered": {"type": "boolean", "description": "可选：true 只看已掌握，false 只看未掌握"}}},
     "handler": tool_list_vocab},
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------- JSON-RPC loop

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    is_request = "id" in msg
    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "PaperDesk 论文库：先 get_current_paper 看用户在读哪篇，再用 get_paper_text / get_paper_notes 读取。"}}
    if method in ("notifications/initialized", "initialized") or method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if not tool:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"未知工具: {name}"}}
        try:
            value = tool["handler"](args)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": result_text(value)}], "isError": False}}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"内部错误：{type(e).__name__}: {e}"}], "isError": True}}
    if method in ("resources/list",):
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method in ("prompts/list",):
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if is_request:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        if isinstance(msg, list):
            if replies:
                send(replies)
        else:
            for r in replies:
                send(r)


if __name__ == "__main__":
    main()
