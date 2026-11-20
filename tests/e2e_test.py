#!/usr/bin/env python3
"""PaperDesk end-to-end test.

Starts the real server.py (with mock claude/codex CLIs), a local http.server that
serves a generated sample PDF, a temp data dir and a local bare git remote, then
drives the UI with Playwright (headless Chrome).

    python3 tests/e2e_test.py            # screenshots go to /tmp/pd-e2e/
    PD_E2E_HEADFUL=1 python3 tests/e2e_test.py

All front-end selectors live in SELECTORS below; adjust them to the real DOM.
Each UI step falls back to the HTTP API when its selector is missing, so later
steps still run; such fallbacks are reported as WARN.
"""
import functools
import glob
import http.server
import json
import re
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCKS = os.path.join(ROOT, "tests", "mocks")
SHOTS = os.environ.get("PD_E2E_SHOTS", "/tmp/pd-e2e")
CHROME = "/usr/bin/google-chrome"

# --------------------------------------------------------------------------- selectors
# Adjust these to match web/ once the front-end lands. Comma = CSS alternatives.
SELECTORS = {
    # library / sidebar
    "sidebar": "#sidebar, .sidebar, aside",
    "new_project_btn": "[data-action='new-project'], #new-project, button:has-text('新建项目')",
    "project_name_input": "[data-field='project-name'], #project-name, .modal input[type='text'], dialog input",
    "project_confirm": "[data-action='confirm'], .modal button.primary, dialog button.primary, button:has-text('创建')",
    "project_link": "[data-project-id='{pid}'], a[href='/p/{pid}']",
    # project view
    "add_input": "[data-field='add-input'], #add-input, input[placeholder*='arXiv']",
    "paper_card": "[data-item-type='paper'], .item-card.paper, .paper-card",
    "paper_open": "[data-action='open-reader'], a[href^='/r/'], button:has-text('阅读')",
    # reader
    "pdf_page": ".pdf-page, .page[data-page-number], [data-page]",
    "page_canvas": ".pdf-page canvas, .page canvas, [data-page] canvas",
    "text_layer_span": ".textLayer span",
    "sel_toolbar": ".sel-toolbar",
    "tb_translate": ".sel-toolbar [data-action='translate']",
    "tb_highlight": ".sel-toolbar [data-action='highlight'][data-color='yellow']",
    "tb_underline": ".sel-toolbar [data-action='underline']",
    "annotation": ".annoLayer .anno:not(.temp)",
    "tab_notes": "[data-tab='notes']",
    "tab_qa": "[data-tab='qa']",
    "tab_translate": "[data-tab='translate']",
    "tab_summary": "[data-tab='summary']",
    "translation_item": ".tr-item",
    "ask_input": "[data-field='ask-input']",
    "ask_send": "[data-action='ask']",
    "qa_answer": ".qa-a:not(.streaming)",
    "note_new": "[data-action='new-note']",
    "note_editor": ".editor-ta",
    "note_save": "[data-action='save-note']",
    "note_card": ".ncard",
    "archive_btn": "[data-action='archive']",
    "summary_panel": ".summary-panel, #summary, [data-panel='summary']",
    # vocabulary (§9)
    "tb_vocab": ".sel-toolbar [data-action='vocab']",
    "tab_vocab": "[data-tab='vocab']",
    "vocab_card": ".vcard",
    "sidebar_vocab_link": "a.sb-item[href$='/vocab']",
    "vocab_page_entry": ".vcard",
    # read aloud (§8)
    "tts_btn": "[data-action='tts']",
    # voice mode (§11)
    "tts_full_btn": "[data-action='tts-full']",
    "mic_btn": ".mic-btn",
    "voice_toggle": "[data-action='voice-mode'], [data-field='voice-mode'], label:has-text('语音模式')",
}


def sel(name, **fmt):
    return SELECTORS[name].format(**fmt)


# --------------------------------------------------------------------------- helpers

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, base):
        self.base = base

    def req(self, method, path, body=None, raw=False, timeout=30):
        data = None if body is None else json.dumps(body).encode("utf-8")
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            content = resp.read()
            if raw:
                return content, resp.headers
            return json.loads(content.decode("utf-8")) if content else None

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body or {}, **kw)

    def put(self, path, body, **kw):
        return self.req("PUT", path, body, **kw)

    def sse(self, path, body, timeout=60):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                   headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        events = []
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return events


class Results:
    def __init__(self):
        self.rows = []

    def add(self, status, step, detail=""):
        self.rows.append((status, step, detail))
        print(f"[{status}] {step}" + (f" — {detail}" if detail else ""), flush=True)

    def ok(self, step, detail=""):
        self.add("PASS", step, detail)

    def warn(self, step, detail=""):
        self.add("WARN", step, detail)

    def fail(self, step, detail=""):
        self.add("FAIL", step, detail)

    @property
    def failed(self):
        return [r for r in self.rows if r[0] == "FAIL"]


R = Results()


def check(step, cond, detail=""):
    (R.ok if cond else R.fail)(step, detail)
    return cond


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def start_pdf_server(directory):
    port = free_port()
    handler = functools.partial(QuietHandler, directory=directory)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def start_app(data_dir, port, log_path):
    env = dict(os.environ)
    env["PAPERDESK_CLAUDE"] = os.path.join(MOCKS, "claude")
    env["PAPERDESK_CODEX"] = os.path.join(MOCKS, "codex")
    env["PAPERDESK_HOME"] = data_dir
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "PaperDesk Test"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    log = open(log_path, "w")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port),
                          "--data", data_dir, "--no-browser"], cwd=ROOT, env=env, stdout=log,
                         stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if p.poll() is not None:
            raise RuntimeError(f"server.py exited with {p.returncode}; see {log_path}")
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return p, base
        except Exception:
            time.sleep(0.15)
    p.terminate()
    raise RuntimeError("server did not become healthy; see " + log_path)


def shot(page, name):
    path = os.path.join(SHOTS, f"{len(R.rows):02d}-{name}.png")
    try:
        page.screenshot(path=path, full_page=False)
    except Exception as e:  # noqa: BLE001
        print("screenshot failed:", e)
    return path


def first_visible(page, selector, timeout=4000):
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    return loc


def select_text_on_page(page, page_no=1, span_from=3, span_to=6):
    """Drag-select across several text-layer spans of a given page."""
    spans = page.locator(f"{sel('pdf_page')} >> nth={page_no - 1} >> css=.textLayer span")
    n = spans.count()
    if n == 0:
        spans = page.locator(sel("text_layer_span"))
        n = spans.count()
    # Pick reasonably long spans on (hopefully) the same paragraph.
    candidates = []
    for i in range(min(n, 80)):
        t = spans.nth(i).inner_text()
        if len(t.strip()) > 25:
            candidates.append(i)
    if len(candidates) < 2:
        candidates = list(range(min(n, 8)))
    a = spans.nth(candidates[0]).bounding_box()
    b = spans.nth(candidates[min(1, len(candidates) - 1)]).bounding_box()
    page.mouse.move(a["x"] + 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move((a["x"] + b["x"] + b["width"]) / 2, b["y"] + b["height"] / 2, steps=8)
    page.mouse.move(b["x"] + b["width"] * 0.8, b["y"] + b["height"] / 2, steps=8)
    page.mouse.up()
    return page.evaluate("() => String(window.getSelection())")


def double_click_word(page, page_no=1):
    """Double-click inside a text-layer span on a word of >= 6 letters; returns the selected word."""
    spans = page.locator(sel("text_layer_span"))
    n = spans.count()
    import re as _re
    for i in range(min(n, 120)):
        t = spans.nth(i).inner_text()
        m = _re.search(r"[A-Za-z]{6,}", t)
        if not m or len(t) < 10:
            continue
        box = spans.nth(i).bounding_box()
        if not box or box["width"] < 20:
            continue
        # approximate x of the word by character ratio
        x = box["x"] + box["width"] * (m.start() + 2) / max(len(t), 1)
        y = box["y"] + box["height"] / 2
        page.mouse.dblclick(x, y)
        page.wait_for_timeout(300)
        w = page.evaluate("() => String(window.getSelection()).trim()")
        if _re.fullmatch(r"[A-Za-z][A-Za-z\-]+", w or ""):
            return w
    raise RuntimeError("could not double-click a word")


# --------------------------------------------------------------------------- main flow

def run():
    os.makedirs(SHOTS, exist_ok=True)
    for f in glob.glob(os.path.join(SHOTS, "*.png")):
        os.remove(f)
    work = tempfile.mkdtemp(prefix="pd-e2e-")
    data_dir = os.path.join(work, "library")
    pdf_dir = os.path.join(work, "pdfs")
    remote = os.path.join(work, "remote.git")
    os.makedirs(pdf_dir)
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_sample_pdf.py"),
                    os.path.join(pdf_dir, "sample-paper.pdf")], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    pdf_srv, pdf_base = start_pdf_server(pdf_dir)
    pdf_url = f"{pdf_base}/sample-paper.pdf"
    port = free_port()
    app, base = start_app(data_dir, port, os.path.join(work, "server.log"))
    api = Api(base)
    print("work dir:", work, "| app:", base, "| pdf:", pdf_url, flush=True)

    try:
        h = api.get("/api/health")
        check("health", h.get("ok") and h["claude"]["found"] and h["codex"]["found"], json.dumps(h, ensure_ascii=False))
        api.put("/api/settings", {"translate_engine": "claude", "default_engine": "claude", "vocab_engine": "claude",
                                  "sync_debounce_sec": 1, "app_url": base})
        try:
            st = api.post("/api/sync/setup", {"remote_url": remote})
            check("sync setup (bare remote)", st.get("remote"), json.dumps(st, ensure_ascii=False))
        except urllib.error.HTTPError as e:
            R.fail("sync setup", e.read().decode("utf-8", "replace"))

        # PDF proxy must stream bytes without writing them to disk.
        body, headers = api.get("/api/pdf?url=" + urllib.parse.quote(pdf_url, safe=""), raw=True)
        check("pdf proxy streams PDF", body[:5] == b"%PDF-" and "pdf" in headers.get("Content-Type", ""),
              f"{len(body)} bytes")

        ui_flow(api, base, pdf_url, data_dir)

        # Codex engine via API (SSE).
        pid, iid = STATE.get("pid"), STATE.get("iid")
        if pid and iid:
            ev = api.sse(f"/api/papers/{pid}/{iid}/ask", {"question": "用 Codex 解释第一页", "engine": "codex"})
            types = [e.get("type") for e in ev]
            done = [e for e in ev if e.get("type") == "done"]
            check("codex ask via SSE", done and "Codex" in done[0]["qa"]["answer"] and "paper.txt" in done[0]["qa"]["answer"],
                  f"types={types}")

        # Voice-style ask (§11.2) and tts_pos persistence (§11.1)
        if pid and iid:
            try:
                ev = api.sse(f"/api/papers/{pid}/{iid}/ask",
                             {"question": "用一句话说这篇论文的贡献", "style": "voice", "page": 1})
                done = [e for e in ev if e.get("type") == "done"]
                check("ask style=voice saved on qa", bool(done) and done[0]["qa"].get("style") == "voice",
                      f"types={[e.get('type') for e in ev]}")
                st = api.get(f"/api/papers/{pid}/{iid}")
                check("voice qa persisted", any(q.get("style") == "voice" for q in st.get("qa", [])))
            except Exception as e:  # noqa: BLE001
                R.fail("ask style=voice", repr(e))
            try:
                api.req("PATCH", f"/api/papers/{pid}/{iid}", {"tts_pos": {"page": 2, "sentence": 7}})
                st = api.get(f"/api/papers/{pid}/{iid}")
                check("PATCH tts_pos → GET reads back", st.get("tts_pos") == {"page": 2, "sentence": 7},
                      str(st.get("tts_pos")))
            except Exception as e:  # noqa: BLE001
                R.fail("tts_pos", repr(e))

        # Files on disk
        check_files(api, data_dir)

        # Git sync to bare remote
        try:
            st = api.post("/api/sync/now")
            time.sleep(1.5)
            log = subprocess.run(["git", "--git-dir", remote, "log", "--oneline", "--all"],
                                 capture_output=True, text=True).stdout.strip()
            check("git push reached bare remote", bool(log), (log.splitlines() or ["(empty)"])[0] +
                  f" | status={json.dumps(st, ensure_ascii=False)[:200]}")
            tree = subprocess.run(["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "HEAD"],
                                  capture_output=True, text=True).stdout
            check("remote has no PDF / cache / agent files",
                  not any(x.endswith(".pdf") or x.startswith(".cache/") or x.startswith(".agent/")
                          for x in tree.splitlines()), f"{len(tree.splitlines())} files")
        except Exception as e:  # noqa: BLE001
            R.fail("git sync", repr(e))
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except Exception:
            app.kill()
        pdf_srv.shutdown()
        print("\nserver log tail:\n" + "".join(open(os.path.join(work, "server.log")).readlines()[-15:]))

    print("\n===== SUMMARY =====")
    for status, step, detail in R.rows:
        print(f"{status:4}  {step}")
    print(f"screenshots: {SHOTS}   work dir: {work}")
    return 1 if R.failed else 0


STATE = {}


def ui_flow(api, base, pdf_url, data_dir):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        kw = {"headless": not os.environ.get("PD_E2E_HEADFUL")}
        if os.path.exists(CHROME):
            kw["executable_path"] = CHROME
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        page = ctx.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
        page.on("dialog", lambda d: d.accept("E2E 项目") if d.type == "prompt" else d.accept())

        # 1. Home
        page.goto(base + "/", wait_until="networkidle")
        shot(page, "home")
        check("home renders sidebar", page.locator(sel("sidebar")).count() > 0)

        # 2. New project
        proj_name = "E2E 项目"
        pid = None
        try:
            first_visible(page, sel("new_project_btn")).click()
            try:
                inp = first_visible(page, sel("project_name_input"), timeout=2500)
                inp.fill(proj_name)
                try:
                    page.locator(sel("project_confirm")).first.click(timeout=1500)
                except PWTimeout:
                    inp.press("Enter")
            except PWTimeout:
                pass  # maybe window.prompt (auto-accepted by dialog handler)
            page.wait_for_timeout(800)
            pid = next((x["id"] for x in api.get("/api/projects") if x["name"] == proj_name), None)
            check("create project via UI", pid is not None)
        except PWTimeout as e:
            R.warn("create project via UI", f"selector missing → API fallback ({e.message.splitlines()[0]})")
        if not pid:
            pid = api.post("/api/projects", {"name": proj_name, "description": "e2e"})["id"]
            page.goto(f"{base}/p/{pid}", wait_until="networkidle")
        STATE["pid"] = pid
        shot(page, "project-created")

        # 3. Add paper by PDF link
        iid = None
        try:
            if f"/p/{pid}" not in page.url:
                page.goto(f"{base}/p/{pid}", wait_until="networkidle")
            add = first_visible(page, sel("add_input"))
            add.fill(pdf_url)
            add.press("Enter")
            first_visible(page, sel("paper_card"), timeout=8000)
            items = api.get(f"/api/projects/{pid}")["items"]
            iid = next((i["id"] for i in items if i["type"] == "paper"), None)
            check("add PDF link via UI", iid is not None)
        except PWTimeout as e:
            R.warn("add PDF link via UI", f"→ API fallback ({e.message.splitlines()[0]})")
        if not iid:
            iid = api.post(f"/api/projects/{pid}/items", {"input": pdf_url})["id"]
        STATE["iid"] = iid
        shot(page, "paper-added")

        # 4. Open reader (permalink /r/<pid>/<iid>)
        try:
            page.locator(sel("paper_open")).first.click(timeout=3000)
            page.wait_for_url(f"**/r/{pid}/{iid}", timeout=5000)
            R.ok("open reader from card")
        except PWTimeout:
            R.warn("open reader from card", "→ navigating to permalink directly")
            page.goto(f"{base}/r/{pid}/{iid}")

        # 5. Rendering
        try:
            page.locator(sel("page_canvas")).first.wait_for(state="visible", timeout=15000)
            page.wait_for_function(
                "sel => document.querySelectorAll(sel).length > 10", arg=sel("text_layer_span"), timeout=15000)
            n_pages = page.locator(sel("pdf_page")).count()
            check("pdf rendered with text layer", n_pages >= 3, f"pages={n_pages}")
        except PWTimeout as e:
            R.fail("pdf rendered with text layer", e.message.splitlines()[0])
        shot(page, "reader")
        # text cache should be uploaded in the background
        page.wait_for_timeout(1500)
        st = api.get(f"/api/papers/{pid}/{iid}")
        check("text cached after open", st.get("text_cached") is True)

        # 6-7. Select text → toolbar → highlight
        try:
            txt = select_text_on_page(page, 1)
            first_visible(page, sel("sel_toolbar"), timeout=3000)
            check("selection toolbar appears", True, f"selected {len(txt)} chars")
            shot(page, "selection-toolbar")
            page.locator(sel("tb_highlight")).first.click()
            page.wait_for_timeout(800)
            n_ann = page.locator(sel("annotation")).count()
            st = api.get(f"/api/papers/{pid}/{iid}")
            check("highlight saved & drawn", n_ann >= 1 and len(st["annotations"]) >= 1,
                  f"dom={n_ann} saved={len(st['annotations'])}")
        except PWTimeout as e:
            R.fail("select text / highlight", e.message.splitlines()[0])
            api.post(f"/api/papers/{pid}/{iid}/annotations", {"kind": "highlight", "color": "yellow", "page": 1,
                                                              "quote": "fallback", "rects": [[0.1, 0.1, 0.5, 0.02]]})
        shot(page, "highlighted")

        # underline as a second annotation kind
        try:
            select_text_on_page(page, 1)
            first_visible(page, sel("sel_toolbar"), timeout=3000)
            page.locator(sel("tb_underline")).first.click()
            page.wait_for_timeout(600)
            kinds = {a["kind"] for a in api.get(f"/api/papers/{pid}/{iid}")["annotations"]}
            check("underline saved", "underline" in kinds, str(kinds))
        except PWTimeout as e:
            R.warn("underline", e.message.splitlines()[0])

        # 8. Translate selection
        try:
            select_text_on_page(page, 1)
            first_visible(page, sel("sel_toolbar"), timeout=3000)
            page.locator(sel("tb_translate")).first.click()
            page.wait_for_function(
                "sel => [...document.querySelectorAll(sel)].some(e => e.innerText.includes('【译文】'))",
                arg=sel("translation_item"), timeout=15000)
            R.ok("translate selection")
        except PWTimeout as e:
            R.fail("translate selection", e.message.splitlines()[0])
        shot(page, "translated")

        # 8b. Double-click a word → 生词 → vocab tab shows it
        try:
            word = double_click_word(page, 1)
            STATE["word"] = word
            first_visible(page, sel("sel_toolbar"), timeout=3000)
            page.locator(sel("tb_vocab")).first.click()
            try:
                page.locator(sel("tab_vocab")).first.click(timeout=2000)
            except PWTimeout:
                pass
            page.wait_for_function(
                "([s, w]) => [...document.querySelectorAll(s)].some(e => e.innerText.toLowerCase().includes(w.toLowerCase()))",
                arg=[sel("vocab_card"), word], timeout=20000)
            st = api.get(f"/api/papers/{pid}/{iid}")
            entry = next((v for v in st.get("vocab", []) if v.get("word", "").lower() == word.lower()), None)
            check("vocab: double-click word → 生词 card", entry is not None and bool(entry.get("meaning")),
                  f"word={word!r} meaning={entry and entry.get('meaning')!r}")
            kinds = {a["kind"] for a in st["annotations"]}
            check("vocab annotation created", "vocab" in kinds, str(kinds))
        except (PWTimeout, RuntimeError) as e:
            R.fail("vocab flow", str(e).splitlines()[0])
        shot(page, "vocab")

        # 9. Ask the agent (streaming)
        try:
            page.locator(sel("tab_qa")).first.click()
            ask = first_visible(page, sel("ask_input"))
            ask.fill("为什么要除以 sqrt(d_k)？")
            try:
                page.locator(sel("ask_send")).first.click(timeout=2000)
            except PWTimeout:
                ask.press("Control+Enter")
            page.wait_for_function(
                "sel => [...document.querySelectorAll(sel)].some(e => e.innerText.includes('梯度更稳定'))",
                arg=sel("qa_answer"), timeout=30000)
            page.wait_for_timeout(800)
            ans_html = page.locator(sel("qa_answer")).last.inner_html()
            check("agent answer streamed & rendered", "<strong>" in ans_html and "paper.txt" in ans_html,
                  "katex" if "katex" in ans_html else "no katex")
        except PWTimeout as e:
            R.fail("ask agent", e.message.splitlines()[0])
        shot(page, "qa")

        # 10. New note with bold / underline / math
        note_md = "这是 **加粗** 和 <ins>下划线</ins>，公式 $\\sqrt{d_k}$"
        try:
            page.locator(sel("tab_notes")).first.click()
            page.locator(sel("note_new")).first.click()
            ed = first_visible(page, sel("note_editor"))
            ed.fill(note_md)
            try:
                page.locator(sel("note_save")).first.click(timeout=2000)
            except PWTimeout:
                ed.press("Control+Enter")
            page.wait_for_timeout(800)
            html = page.locator(sel("note_card")).last.inner_html()
            check("note rendered with <strong>/<ins>", "<strong>" in html and "<ins>" in html)
        except PWTimeout as e:
            R.fail("new note", e.message.splitlines()[0])
            api.post(f"/api/papers/{pid}/{iid}/notes", {"text": note_md, "page": 1})
        shot(page, "note")

        # 11. Archive
        try:
            page.locator(sel("archive_btn")).first.click()
            page.wait_for_function(
                "() => document.body.innerText.includes('一句话总结')", timeout=30000)
            page.wait_for_timeout(800)
            st = api.get(f"/api/papers/{pid}/{iid}")
            check("archive → summary saved", bool(st.get("summary")) and st.get("status") == "done")
        except PWTimeout as e:
            R.fail("archive", e.message.splitlines()[0])
        shot(page, "archived")

        # 12. Reload → annotations + notes restored
        page.reload()
        try:
            page.locator(sel("page_canvas")).first.wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(2000)
            n_ann = page.locator(sel("annotation")).count()
            check("annotations restored after reload", n_ann >= 1, f"{n_ann}")
            try:
                page.locator(sel("tab_notes")).first.click(timeout=2000)
            except PWTimeout:
                pass
            page.wait_for_timeout(500)
            cards = page.locator(sel("note_card"))
            html = "".join(cards.nth(i).inner_html() for i in range(cards.count()))
            check("notes restored with formatting", "<strong>" in html and "<ins>" in html)
            check("math rendered by KaTeX", "katex" in html or "katex" in page.content())
        except PWTimeout as e:
            R.fail("reload restore", e.message.splitlines()[0])
        shot(page, "reloaded")

        # 12b. Voice mode controls present (§11): 听整篇 in the read-aloud bar, 🎤 next to the ask box
        try:
            full = page.locator(sel("tts_full_btn"))
            if not full.first.is_visible():
                page.locator(sel("tts_btn")).first.click(timeout=2000)
            full.first.wait_for(state="visible", timeout=3000)
            R.ok("read-aloud bar has 听整篇")
        except PWTimeout as e:
            R.fail("read-aloud bar has 听整篇", e.message.splitlines()[0])
        try:
            page.locator(sel("tab_qa")).first.click(timeout=2000)
            page.locator(sel("mic_btn")).first.wait_for(state="visible", timeout=3000)
            R.ok("🎤 button next to ask box")
            (R.ok if page.locator(sel("voice_toggle")).count() else R.warn)("语音模式 toggle present")
        except PWTimeout as e:
            R.fail("🎤 button", e.message.splitlines()[0])
        shot(page, "voice-controls")

        # 13. /vocab page lists the word
        word = STATE.get("word")
        if word:
            try:
                try:
                    page.locator(sel("sidebar_vocab_link")).first.click(timeout=2000)
                    page.wait_for_url("**/vocab", timeout=4000)
                except PWTimeout:
                    page.goto(base + "/vocab")
                page.wait_for_function(
                    "([s, w]) => [...document.querySelectorAll(s)].some(e => e.innerText.toLowerCase().includes(w.toLowerCase()))",
                    arg=[sel("vocab_page_entry"), word], timeout=8000)
                R.ok("/vocab page lists the word", word)
            except PWTimeout as e:
                R.fail("/vocab page", e.message.splitlines()[0])
            shot(page, "vocab-page")
            try:
                allv = api.get("/api/vocab")
                check("GET /api/vocab includes word", any(v.get("word", "").lower() == word.lower() for v in allv))
            except Exception as e:  # noqa: BLE001
                R.fail("GET /api/vocab", repr(e))

        errs = [e for e in console_errors if "favicon" not in e]
        (R.ok if not errs else R.warn)("no console errors", "; ".join(errs[:5]))
        browser.close()


def check_files(api, data_dir):
    pid, iid = STATE.get("pid"), STATE.get("iid")
    pdfs = [p for p in glob.glob(os.path.join(data_dir, "**", "*.pdf"), recursive=True)]
    check("no PDF written to data dir", not pdfs, ", ".join(pdfs))
    mds = glob.glob(os.path.join(data_dir, "projects", pid or "*", "papers", "*.md"))
    if not check("paper markdown exists", bool(mds)):
        return
    md = open(mds[0], encoding="utf-8").read()
    check("md keeps **bold** and <ins>", "**加粗**" in md and "<ins>下划线</ins>" in md)
    check("md has 总结/笔记/问答 sections", all(re.search(r"^## .*%s" % h, md, re.M) for h in ("总结", "笔记", "问答")))
    check("md has PaperDesk permalink", f"/r/{pid}/{iid}" in md)
    check("md has YAML front matter", md.startswith("---"))
    readme = os.path.join(data_dir, "projects", pid, "README.md")
    check("project README index", os.path.exists(readme) and f"/r/{pid}/{iid}" in open(readme, encoding="utf-8").read())
    check("root README", os.path.exists(os.path.join(data_dir, "README.md")))
    gi = os.path.join(data_dir, ".gitignore")
    check(".gitignore excludes cache/agent", os.path.exists(gi) and ".cache" in open(gi).read() and ".agent" in open(gi).read())
    word = STATE.get("word")
    if word:
        vpath = os.path.join(data_dir, "vocabulary.md")
        check("vocabulary.md contains word", os.path.exists(vpath) and word.lower() in open(vpath, encoding="utf-8").read().lower())
        check("paper md has 生词 table", bool(re.search(r"^## .*生词", md, re.M)) and word.lower() in md.lower())
    raw, headers = api.get(f"/api/papers/{pid}/{iid}/markdown", raw=True)
    check("GET markdown endpoint", "markdown" in headers.get("Content-Type", "") and "**加粗**" in raw.decode("utf-8"))


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
