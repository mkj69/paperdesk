"""HTTP 服务：REST API + SSE 流式 + 静态文件。只监听 127.0.0.1。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, agents, config, fetcher, store
from .gitsync import GitSync

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".pfb": "application/octet-stream",
    ".bcmap": "application/octet-stream",
    ".wasm": "application/wasm",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class ClientGone(Exception):
    pass


class App:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        store.ensure_library(data_dir)
        self.git = GitSync(data_dir, self.settings, pre_sync=self.export_site,
                           on_pages_url_change=lambda: store.rerender_all(data_dir))
        store.set_change_hook(self.git.schedule)

    def settings(self):
        return config.load_settings(self.data_dir)

    def export_site(self):
        return store.export_static_site(self.data_dir, WEB_DIR)

    def start(self):
        def boot():
            try:
                self.export_site()          # 启动时导出一次在线只读站
            except Exception:
                traceback.print_exc()
            self.git.init_repo()
        threading.Thread(target=boot, daemon=True).start()


# ---------------------------------------------------------------------------------------------
ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


ID = r"([a-z0-9][a-z0-9\-]*)"
IID = r"([a-zA-Z0-9_\-]+)"


def _require(body, key, msg=None):
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ApiError(400, msg or "缺少参数：%s" % key)
    return v


# ----------------------------------------------------------------- 系统
@route("GET", r"/api/health")
def h_health(app, h, m, q, body):
    s = app.settings()
    cli = agents.cli_status(s)
    return {"ok": True, "version": VERSION, "data_dir": app.data_dir,
            "claude": cli["claude"], "codex": cli["codex"],
            "git": {"found": bool(config.find_cli("git"))}, "gh": {"found": bool(config.find_cli("gh"))}}


@route("GET", r"/api/settings")
def h_settings_get(app, h, m, q, body):
    return app.settings()


@route("PUT", r"/api/settings")
def h_settings_put(app, h, m, q, body):
    old = app.settings()
    try:
        new = config.save_settings(app.data_dir, body or {})
    except ValueError as e:
        raise ApiError(400, str(e))
    if new.get("app_url") != old.get("app_url") or new.get("pages_url") != old.get("pages_url"):
        threading.Thread(target=store.rerender_all, args=(app.data_dir,), daemon=True).start()
        app.git.schedule("更新设置")
    return new


@route("POST", r"/api/current")
def h_current(app, h, m, q, body):
    return store.write_current(app.data_dir, _require(body, "pid"), _require(body, "iid"))


@route("GET", r"/api/current")
def h_current_get(app, h, m, q, body):
    return store.read_current(app.data_dir) or {}


@route("POST", r"/api/open_folder")
def h_open_folder(app, h, m, q, body):
    path = app.data_dir
    if body.get("pid"):
        store.get_project(app.data_dir, body["pid"])
        path = os.path.join(app.data_dir, "projects", body["pid"])
    opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
    try:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ApiError(500, "无法打开文件夹：%s" % e)
    return {"ok": True, "path": path}


# ----------------------------------------------------------------- 项目
@route("GET", r"/api/projects")
def h_projects(app, h, m, q, body):
    return store.list_projects(app.data_dir)


@route("POST", r"/api/projects")
def h_project_create(app, h, m, q, body):
    return store.create_project(app.data_dir, body.get("name") or "", body.get("description") or "")


@route("GET", r"/api/projects/" + ID)
def h_project_get(app, h, m, q, body):
    return store.get_project(app.data_dir, m.group(1))


@route("PATCH", r"/api/projects/" + ID)
def h_project_patch(app, h, m, q, body):
    return store.update_project(app.data_dir, m.group(1), body.get("name"), body.get("description"))


@route("DELETE", r"/api/projects/" + ID)
def h_project_delete(app, h, m, q, body):
    store.delete_project(app.data_dir, m.group(1))
    return {"ok": True}


@route("GET", r"/api/resolve")
def h_resolve(app, h, m, q, body):
    text = (q.get("input") or [""])[0]
    try:
        return fetcher.resolve(text)
    except ValueError as e:
        raise ApiError(400, str(e))


def _fill_resource_titles(app, pid, iid):
    """后台补全自动发现的论文资源标题（arXiv 元数据），失败忽略。"""
    def run():
        try:
            it = store.find_item(app.data_dir, pid, iid)
        except Exception:
            return
        for r in it.get("resources") or []:
            if r.get("kind") == "paper" and r.get("arxiv_id") and (r.get("title") or "").startswith("arXiv:"):
                meta = fetcher.fetch_arxiv_meta(r["arxiv_id"])
                if meta and meta.get("title"):
                    try:
                        store.update_resource(app.data_dir, pid, iid, r["id"], {"title": meta["title"]})
                    except Exception:
                        pass
    threading.Thread(target=run, daemon=True).start()


def _resolve_multi(text):
    """一次粘贴多个链接：主链接取第一个论文类（没有就第一个），其余作为资源。"""
    parts = fetcher.split_inputs(text)
    if len(parts) < 2:
        return fetcher.resolve(text)
    resolved = []
    for t in parts:
        try:
            resolved.append(fetcher.resolve(t))
        except ValueError:
            continue
    if not resolved:
        raise ValueError("无法识别输入的链接")
    main = next((r for r in resolved if r.get("type") == "paper"), resolved[0])
    res = list(main.get("resources") or [])
    for r in resolved:
        if r is main:
            continue
        res.append({"kind": r.get("kind") or fetcher.classify(r.get("url") or ""), "url": r.get("url"),
                    "title": r.get("title") if r.get("title") != r.get("url") else "",
                    "pdf_url": r.get("pdf_url"), "arxiv_id": r.get("arxiv_id"), "auto": False})
        for extra in r.get("resources") or []:
            res.append(extra)
    main = dict(main)
    main["resources"] = res
    return main


@route("POST", r"/api/projects/" + ID + r"/items")
def h_item_add(app, h, m, q, body):
    pid = m.group(1)
    store.get_project(app.data_dir, pid)
    data = {}
    if body.get("input"):
        if body.get("type") == "note":
            data = {"type": "note", "title": body.get("title") or "", "note": body["input"]}
        else:
            try:
                data = _resolve_multi(body["input"])
            except ValueError as e:
                raise ApiError(400, str(e))
    elif body.get("type"):
        data["type"] = body["type"]
    for k in ("title", "url", "note", "tags"):
        if body.get(k) not in (None, ""):
            data[k] = body[k]
    if data.get("type") == "paper" and body.get("url") and not body.get("input"):
        data.setdefault("pdf_url", body["url"])
    if data.get("type") in ("web", "link") and data.get("url") and not data.get("title"):
        data["title"] = data["url"]
    try:
        item = store.add_item(app.data_dir, pid, data)
    except ValueError as e:
        raise ApiError(400, str(e))
    if not item.get("existing") and any(r.get("auto") and r.get("kind") == "paper" for r in item.get("resources") or []):
        _fill_resource_titles(app, pid, item["id"])
    return item


# ----------------------------------------------------------------- 资源（SPEC §13.2）
@route("POST", r"/api/projects/" + ID + r"/items/" + IID + r"/resources")
def h_res_add(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    url = (_require(body, "url", "请输入资源链接") or "").strip()
    data = {"url": url, "kind": body.get("kind"), "title": body.get("title") or ""}
    try:
        r = fetcher.resolve(url)
        data["url"] = r.get("url") or url
        if r.get("type") == "paper" and r.get("pdf_url"):
            data["pdf_url"] = r["pdf_url"]
            data["arxiv_id"] = r.get("arxiv_id")
        if not data["title"] and r.get("title") and r.get("title") != r.get("url"):
            data["title"] = r["title"]
        if not data.get("kind"):
            data["kind"] = r.get("kind") or fetcher.classify(url)
    except ValueError as e:
        raise ApiError(400, str(e))
    except Exception:
        data["kind"] = data.get("kind") or fetcher.classify(url)
    return store.add_resource(app.data_dir, pid, iid, data)


@route("PATCH", r"/api/projects/" + ID + r"/items/" + IID + r"/resources/" + IID)
def h_res_patch(app, h, m, q, body):
    return store.update_resource(app.data_dir, m.group(1), m.group(2), m.group(3), body)


@route("DELETE", r"/api/projects/" + ID + r"/items/" + IID + r"/resources/" + IID)
def h_res_delete(app, h, m, q, body):
    store.delete_resource(app.data_dir, m.group(1), m.group(2), m.group(3))
    return {"ok": True}


# ----------------------------------------------------------------- 网页正文（不落盘）
@route("GET", r"/api/web")
def h_web(app, h, m, q, body):
    url = (q.get("url") or [""])[0]
    if not url:
        raise ApiError(400, "缺少 url 参数")
    try:
        return fetcher.fetch_web(url)
    except fetcher.FetchError as e:
        raise ApiError(502, "网页加载失败：%s" % e)


@route("PATCH", r"/api/projects/" + ID + r"/items/" + IID)
def h_item_patch(app, h, m, q, body):
    return store.update_item(app.data_dir, m.group(1), m.group(2), body)


@route("DELETE", r"/api/projects/" + ID + r"/items/" + IID)
def h_item_delete(app, h, m, q, body):
    store.delete_item(app.data_dir, m.group(1), m.group(2))
    return {"ok": True}


@route("POST", r"/api/projects/" + ID + r"/items/" + IID + r"/move")
def h_item_move(app, h, m, q, body):
    to = _require(body, "to", "请选择目标项目")
    item = store.move_item(app.data_dir, m.group(1), m.group(2), to)
    return {"item": item, "pid": to}


# ----------------------------------------------------------------- PDF 代理（不落盘）
@route("GET", r"/api/pdf")
def h_pdf(app, h, m, q, body):
    url = (q.get("url") or [""])[0]
    if not url:
        raise ApiError(400, "缺少 url 参数")
    try:
        resp, clen = fetcher.open_upstream(url)
    except fetcher.FetchError as e:
        raise ApiError(502, "PDF 加载失败：%s" % e)
    try:
        h.send_response(200)
        h.send_header("Content-Type", "application/pdf")
        if clen:
            h.send_header("Content-Length", str(clen))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-PaperDesk-Upstream", urllib.parse.quote(resp.geturl(), safe=":/?&=%"))
        if not clen:
            h.send_header("Connection", "close")
            h.close_connection = True
        h.end_headers()
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            try:
                h.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, socket.error):
                break
        try:
            h.wfile.flush()
        except Exception:
            pass
    finally:
        resp.close()
    return None


# ----------------------------------------------------------------- 论文
P = r"/api/papers/" + ID + "/" + IID


def _state_out(app, pid, st):
    out = dict(st)
    cached = store.cached_resources(app.data_dir, pid, st["paper_key"])
    out["text_cached"] = "main" in cached
    out["texts_cached"] = cached
    out["md_path"] = store.paper_md_relpath(pid, st["paper_key"])
    return out


@route("GET", P)
def h_paper_get(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    try:
        st = store.get_paper_state(app.data_dir, pid, iid)
    except ValueError as e:
        raise ApiError(400, str(e))
    return _state_out(app, pid, st)


@route("PATCH", P)
def h_paper_patch(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    st = store.update_paper(app.data_dir, pid, iid, body)
    return {"ok": True, "last_page": st["last_page"], "zoom": st["zoom"], "status": st["status"],
            "tts_pos": st.get("tts_pos")}


@route("PUT", P + r"/text")
def h_paper_text(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    it = store.find_item(app.data_dir, pid, iid)
    if it.get("type") not in store.STATEFUL_TYPES:
        raise ApiError(400, "这个条目不是论文或网页")
    if not it.get("paper_key"):
        store.get_paper_state(app.data_dir, pid, iid)        # 旧 link 条目：补 paper_key
        it = store.find_item(app.data_dir, pid, iid)
    rid = body.get("resource_id") or "main"
    res = next((r for r in store.item_resources(it) if r.get("id") == rid), None)
    if res is None:
        raise ApiError(404, "资源不存在：%s" % rid)
    pages = body.get("pages")
    if not pages and body.get("text"):
        pages = [{"page": 1, "text": body["text"]}]
    n = store.write_cached_text(app.data_dir, pid, it["paper_key"], pages or [], resource_id=rid,
                                title=body.get("title") or res.get("title"), kind=body.get("kind") or res.get("kind"))
    return {"ok": True, "chars": n, "resource_id": rid}


@route("POST", P + r"/annotations")
def h_ann_add(app, h, m, q, body):
    return store.add_annotation(app.data_dir, m.group(1), m.group(2), body)


@route("PATCH", P + r"/annotations/" + IID)
def h_ann_patch(app, h, m, q, body):
    return store.update_annotation(app.data_dir, m.group(1), m.group(2), m.group(3), body)


@route("DELETE", P + r"/annotations/" + IID)
def h_ann_delete(app, h, m, q, body):
    store.delete_annotation(app.data_dir, m.group(1), m.group(2), m.group(3))
    return {"ok": True}


@route("POST", P + r"/notes")
def h_note_add(app, h, m, q, body):
    return store.add_note(app.data_dir, m.group(1), m.group(2), body)


@route("PATCH", P + r"/notes/" + IID)
def h_note_patch(app, h, m, q, body):
    return store.update_note(app.data_dir, m.group(1), m.group(2), m.group(3), body)


@route("DELETE", P + r"/notes/" + IID)
def h_note_delete(app, h, m, q, body):
    store.delete_note(app.data_dir, m.group(1), m.group(2), m.group(3))
    return {"ok": True}


@route("DELETE", P + r"/qa/" + IID)
def h_qa_delete(app, h, m, q, body):
    store.delete_qa(app.data_dir, m.group(1), m.group(2), m.group(3))
    return {"ok": True}


@route("POST", P + r"/reset_session")
def h_reset_session(app, h, m, q, body):
    engine = body.get("engine") or app.settings().get("default_engine")
    if engine not in ("claude", "codex"):
        raise ApiError(400, "engine 只能是 claude 或 codex")
    store.reset_session(app.data_dir, m.group(1), m.group(2), engine)
    return {"ok": True}


@route("GET", P + r"/markdown")
def h_markdown(app, h, m, q, body):
    md = store.paper_markdown(app.data_dir, m.group(1), m.group(2))
    data = md.encode("utf-8")
    h.send_response(200)
    h.send_header("Content-Type", "text/markdown; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)
    return None


# ----------------------------------------------------------------- 生词
@route("POST", P + r"/vocab")
def h_vocab_add(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    word = _require(body, "word", "单词不能为空")
    entry = {"word": word, "context": body.get("context") or "", "page": body.get("page"),
             "annotation_id": body.get("annotation_id"), "resource_id": body.get("resource_id"),
             "anchor": body.get("anchor")}
    existing = store.find_vocab(app.data_dir, pid, iid, word)
    if existing is None:
        s = app.settings()
        res = agents.lookup_word(word, entry["context"], body.get("engine") or s.get("vocab_engine"), s)
        entry.update(res)
    elif entry["context"]:
        # 已有生词：只补一条新语境的翻译（best-effort，失败不影响）
        try:
            s = app.settings()
            eng = s.get("vocab_engine") if s.get("vocab_engine") == "google" else s.get("translate_engine")
            entry["context_translation"] = agents.translate(entry["context"], eng, None, s)
        except Exception:
            pass
    try:
        return store.add_vocab(app.data_dir, pid, iid, entry)
    except ValueError as e:
        raise ApiError(400, str(e))


@route("PATCH", P + r"/vocab/" + IID)
def h_vocab_patch(app, h, m, q, body):
    return store.update_vocab(app.data_dir, m.group(1), m.group(2), m.group(3), body)


@route("DELETE", P + r"/vocab/" + IID)
def h_vocab_delete(app, h, m, q, body):
    store.delete_vocab(app.data_dir, m.group(1), m.group(2), m.group(3))
    return {"ok": True}


@route("POST", P + r"/vocab/" + IID + r"/relookup")
def h_vocab_relookup(app, h, m, q, body):
    pid, iid, vid = m.group(1), m.group(2), m.group(3)
    st = store.get_paper_state(app.data_dir, pid, iid)
    v = next((x for x in st.get("vocab") or [] if x["id"] == vid), None)
    if v is None:
        raise ApiError(404, "生词不存在")
    s = app.settings()
    res = agents.lookup_word(v["word"], v.get("context") or "", body.get("engine") or s.get("vocab_engine"), s)
    patch = {k: res[k] for k in ("phonetic", "pos", "meaning", "context_meaning", "context_translation") if res.get(k)}
    patch["lookup_error"] = res.get("lookup_error")
    return store.update_vocab(app.data_dir, pid, iid, vid, patch)


@route("GET", r"/api/vocab")
def h_vocab_all(app, h, m, q, body):
    mv = (q.get("mastered") or [None])[0]
    mastered = None
    if mv in ("0", "false", "False"):
        mastered = False
    elif mv in ("1", "true", "True"):
        mastered = True
    pid = (q.get("project") or [None])[0]
    return store.list_all_vocab(app.data_dir, mastered=mastered, pid=pid)


# ----------------------------------------------------------------- 翻译
@route("POST", r"/api/translate")
def h_translate(app, h, m, q, body):
    text = _require(body, "text", "没有要翻译的文字")
    s = app.settings()
    engine = body.get("engine") or s.get("translate_engine") or "google"
    try:
        res = agents.translate(text, engine, body.get("target"), s)
    except agents.AgentError as e:
        raise ApiError(502, str(e))
    out = {"translation": res, "engine": engine}
    if body.get("pid") and body.get("iid"):
        try:
            t = store.add_translation(app.data_dir, body["pid"], body["iid"],
                                      {"source": text, "translation": res, "engine": engine, "page": body.get("page"),
                                       "resource_id": body.get("resource_id")})
            out["id"] = t["id"]
        except (store.NotFound, ValueError):
            pass
    return out


# ----------------------------------------------------------------- AI（SSE）
def _sse_begin(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "close")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    h.close_connection = True


def _sse(h, obj):
    data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
    try:
        h.wfile.write(data)
        h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, socket.error):
        raise ClientGone()


def _stream_agent(app, h, pid, iid, engine, prompt, on_done):
    s = app.settings()
    if engine not in ("claude", "codex"):
        raise ApiError(400, "engine 只能是 claude 或 codex")
    st = store.get_paper_state(app.data_dir, pid, iid)
    wd = agents.prepare_workdir(app.data_dir, pid, st, s)
    sid = (st.get("sessions") or {}).get(engine)
    _sse_begin(h)
    gen = None
    try:
        _sse(h, {"type": "start", "engine": engine})
        if not store.cached_resources(app.data_dir, pid, st["paper_key"]):
            _sse(h, {"type": "status", "text": "提示：全文还没抽取完，AI 暂时只能看到摘要"})
        gen = agents.run_agent(engine, prompt, wd, s, session_id=sid)
        final = None
        for ev in gen:
            if ev["type"] == "final":
                final = ev
            else:
                _sse(h, ev)
        if final is None:
            raise agents.AgentError("没有拿到回答")
        if final.get("session_id"):
            store.set_session(app.data_dir, pid, iid, engine, final["session_id"])
        _sse(h, on_done(final))
    except ClientGone:
        pass
    except agents.AgentError as e:
        try:
            _sse(h, {"type": "error", "message": str(e)})
        except ClientGone:
            pass
    except Exception as e:
        traceback.print_exc()
        try:
            _sse(h, {"type": "error", "message": "内部错误：%s" % e})
        except ClientGone:
            pass
    finally:
        if gen is not None:
            try:
                gen.close()   # 触发 finally → kill 子进程
            except Exception:
                pass
    return None


@route("POST", P + r"/ask")
def h_ask(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    mode = body.get("mode") if body.get("mode") in ("ask", "explain") else "ask"
    question = (body.get("question") or "").strip()
    if not question and not body.get("quote"):
        raise ApiError(400, "请输入问题")
    s = app.settings()
    engine = body.get("engine") or s.get("default_engine") or "claude"
    style = "voice" if body.get("style") == "voice" else "text"
    rid = body.get("resource_id") or "main"
    source_label = None
    if body.get("quote"):
        try:
            it = store.find_item(app.data_dir, pid, iid)
            res = next((r for r in store.item_resources(it) if r.get("id") == rid), None)
            if res is not None and (rid != "main" or it.get("type") in ("web", "link")):
                source_label = agents.resource_label(res)
        except store.NotFound:
            pass
    prompt = agents.build_ask_prompt(question, body.get("quote"), body.get("page"), mode, style,
                                     s.get("answer_lang") or "中文", source_label=source_label)

    def done(final):
        qa = store.add_qa(app.data_dir, pid, iid, {
            "question": question or ("解释：" + (body.get("quote") or "")[:60]),
            "answer": final["text"], "engine": engine, "mode": mode, "style": style,
            "quote": body.get("quote") or "", "page": body.get("page"), "resource_id": rid,
            "anchor": body.get("anchor")})
        return {"type": "done", "qa": qa}
    return _stream_agent(app, h, pid, iid, engine, prompt, done)


@route("POST", P + r"/archive")
def h_archive(app, h, m, q, body):
    pid, iid = m.group(1), m.group(2)
    engine = body.get("engine") or app.settings().get("default_engine") or "claude"
    st = store.get_paper_state(app.data_dir, pid, iid)
    prompt = agents.build_archive_prompt(st)

    def done(final):
        summ = store.set_summary(app.data_dir, pid, iid, final["text"], engine)
        return {"type": "done", "summary": summ, "md_path": store.paper_md_relpath(pid, st["paper_key"])}
    return _stream_agent(app, h, pid, iid, engine, prompt, done)


# ----------------------------------------------------------------- 同步
@route("GET", r"/api/sync/status")
def h_sync_status(app, h, m, q, body):
    return app.git.status()


@route("POST", r"/api/sync/now")
def h_sync_now(app, h, m, q, body):
    return app.git.sync_now()


@route("POST", r"/api/sync/setup")
def h_sync_setup(app, h, m, q, body):
    try:
        return app.git.setup(remote_url=body.get("remote_url"), create=body.get("create"),
                             private=bool(body.get("private", False)))
    except (RuntimeError, ValueError) as e:
        raise ApiError(400, str(e))


@route("POST", r"/api/sync/enable_pages")
def h_sync_enable_pages(app, h, m, q, body):
    try:
        res = app.git.enable_pages()
    except RuntimeError as e:
        raise ApiError(400, str(e))
    app.git.schedule("开启在线只读版")
    return res


@route("POST", r"/api/sync/export")
def h_sync_export(app, h, m, q, body):
    return app.export_site()


# ---------------------------------------------------------------------------------------------
SPA_RX = re.compile(r"^/(?:$|p/[^/]+/?$|r/[^/]+/[^/]+/?$|settings/?$|vocab/?$|index\.html$)")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperDesk/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 安静一点
            if os.environ.get("PAPERDESK_DEBUG"):
                sys.stderr.write("[http] " + (fmt % args) + "\n")

        # -------------------------------------------------- 工具
        def _send_json(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > 50 * 1024 * 1024:
                raise ApiError(413, "请求太大")
            raw = self.rfile.read(n)
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise ApiError(400, "请求体不是合法的 JSON")
            if not isinstance(obj, dict):
                raise ApiError(400, "请求体需要是 JSON 对象")
            return obj

        def _static(self, rel):
            rel = urllib.parse.unquote(rel)
            root = os.path.realpath(WEB_DIR)
            path = os.path.realpath(os.path.join(root, rel.lstrip("/")))
            if not (path == root or path.startswith(root + os.sep)) or not os.path.isfile(path):
                return self._send_json(404, {"error": "文件不存在"})
            ext = os.path.splitext(path)[1].lower()
            ctype = MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache" if ext in (".html", ".js", ".mjs", ".css") else "max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def _index(self):
            idx = os.path.join(WEB_DIR, "index.html")
            if os.path.isfile(idx):
                return self._static("index.html")
            data = "<!doctype html><meta charset=utf-8><title>PaperDesk</title><p>PaperDesk 后端已运行，但没有找到 web/index.html。".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -------------------------------------------------- 分发
        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query)
            try:
                if path.startswith("/api/"):
                    body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
                    allowed = []
                    for meth, rx, fn in ROUTES:
                        mm = rx.match(path)
                        if not mm:
                            continue
                        allowed.append(meth)
                        if meth == method:
                            res = fn(app, self, mm, q, body)
                            if res is not None:
                                self._send_json(200, res)
                            return
                    if allowed:
                        raise ApiError(405, "不支持的请求方法")
                    raise ApiError(404, "接口不存在：%s" % path)
                if method != "GET" and method != "HEAD":
                    raise ApiError(405, "不支持的请求方法")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/favicon.ico":
                    ico = os.path.join(WEB_DIR, "favicon.svg")
                    if os.path.isfile(ico):
                        return self._static("favicon.svg")
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if SPA_RX.match(path):
                    return self._index()
                raise ApiError(404, "页面不存在")
            except ApiError as e:
                self._send_json(e.status, {"error": e.message})
            except store.NotFound as e:
                self._send_json(404, {"error": str(e) or "不存在"})
            except store.Conflict as e:
                self._send_json(409, {"error": str(e)})
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            except agents.AgentError as e:
                self._send_json(502, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                traceback.print_exc()
                try:
                    self._send_json(500, {"error": "服务器内部错误：%s" % e})
                except Exception:
                    pass

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(data_dir, port=8765, host="127.0.0.1"):
    app = App(data_dir)
    srv = Server((host, port), make_handler(app))
    srv.app = app
    return srv
