#!/usr/bin/env python3
"""生成 README / 主页用的界面截图（演示数据，真实界面）。

用法：python3 scripts/make_screenshots.py [输出目录，默认 docs/screenshots]
需要：playwright（python）+ Chrome；只在开发机上用，PaperDesk 本身不依赖它。

演示数据全部是虚构的：一篇原创的演示论文（scripts/demo/demo_paper.tex 编译而来）、
一篇原创的演示博客（scripts/demo/blog.html），以及手写的笔记 / 问答 / 生词 / 总结。
"""
import functools
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "scripts", "demo")
sys.path.insert(0, ROOT)
from paperdesk import store  # noqa: E402

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "screenshots"))
CHROME = "/usr/bin/google-chrome"
W, H = 1440, 900


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *a):
        pass


def serve(directory):
    port = free_port()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), functools.partial(Quiet, directory=directory))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class Api:
    def __init__(self, base):
        self.base = base

    def __call__(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


SELECT_JS = """
([page, phrase]) => {
  const layer = document.querySelector(`[data-page="${page}"] .textLayer, .pdf-page[data-page="${page}"] .textLayer`)
             || document.querySelectorAll('.textLayer')[page - 1];
  if (!layer) return false;
  const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
  const nodes = []; let text = '';
  while (walker.nextNode()) { nodes.push([walker.currentNode, text.length]); text += walker.currentNode.data; }
  const norm = (s) => s.replace(/\\s+/g, ' ');
  const flat = norm(text); const i = flat.indexOf(norm(phrase));
  if (i < 0) return false;
  // map normalized index back (text layer has single spaces already in practice)
  const start = i, end = i + phrase.length;
  const loc = (pos) => { for (let k = nodes.length - 1; k >= 0; k--) if (nodes[k][1] <= pos) return [nodes[k][0], Math.min(pos - nodes[k][1], nodes[k][0].data.length)]; };
  const [sn, so] = loc(start), [en, eo] = loc(end);
  const r = document.createRange(); r.setStart(sn, so); r.setEnd(en, eo);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
  const rect = r.getBoundingClientRect();
  const target = sn.parentElement;
  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: rect.left, clientY: rect.top }));
  sel.removeAllRanges(); sel.addRange(r);
  target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button: 0, clientX: rect.right, clientY: rect.bottom }));
  return true;
}
"""


SELECT_ARTICLE_JS = """
(phrase) => {
  const root = document.querySelector('.article-view');
  if (!root) return false;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode; const i = n.data.indexOf(phrase);
    if (i >= 0) {
      const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + phrase.length);
      const rect = r.getBoundingClientRect(); const t = n.parentElement;
      t.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: rect.left, clientY: rect.top }));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      t.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button: 0, clientX: rect.right, clientY: rect.bottom }));
      return true;
    }
  }
  return false;
}
"""


VOICES_JS = """
(() => {
  if (!window.speechSynthesis) return;
  const mk = (name, lang) => ({ name, lang, voiceURI: name, localService: true, default: lang === 'en-US' });
  const voices = [mk('Samantha', 'en-US'), mk('Ava (Premium)', 'en-US'), mk('Tingting', 'zh-CN')];
  try { Object.defineProperty(speechSynthesis, 'getVoices', { value: () => voices, configurable: true }); } catch (e) {}
})();
"""


def main():
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT, exist_ok=True)
    work = tempfile.mkdtemp(prefix="pd-shots-")
    data = os.path.join(work, "library")
    assets = os.path.join(work, "assets")
    shutil.copytree(DEMO, assets)
    os.makedirs(os.path.join(assets, "posts"), exist_ok=True)
    shutil.copy(os.path.join(DEMO, "blog.html"), os.path.join(assets, "posts", "skimming-explained.html"))
    shutil.copy(os.path.join(DEMO, "figure.svg"), os.path.join(assets, "posts", "figure.svg"))
    asrv, aport = serve(assets)
    A = "http://127.0.0.1:%d" % aport

    port = free_port()
    env = dict(os.environ, PAPERDESK_CLAUDE=os.path.join(ROOT, "tests", "mocks", "claude"),
               PAPERDESK_CODEX=os.path.join(ROOT, "tests", "mocks", "codex"))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port),
                             "--data", data, "--no-browser"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    api = Api(base)
    for _ in range(100):
        try:
            api("GET", "/api/health")
            break
        except Exception:
            time.sleep(0.1)

    try:
        api("PUT", "/api/settings", {"auto_sync": False, "app_url": "http://localhost:8765",
                                     "pages_url": "https://mkj69.github.io/paper-library/"})
        proj = api("POST", "/api/projects", {"name": "Long-context reading",
                                            "description": "高效长文档建模：稀疏注意力、检索、自适应计算"})
        pid = proj["id"]
        main_item = api("POST", "/api/projects/%s/items" % pid, {
            "input": "%s/demo_paper.pdf\n%s/posts/skimming-explained.html\nhttps://github.com/example/skimmer" % (A, A)})
        iid = main_item["id"]
        api("PATCH", "/api/projects/%s/items/%s" % (pid, iid), {
            "title": "Learning to Skim: Adaptive Token Budgets for Long-Document Transformers",
            "tags": ["efficient attention", "long context"]})
        # 其它条目
        for title, url in [("Attention Is All You Need", "https://arxiv.org/abs/1706.03762"),
                           ("Longformer: The Long-Document Transformer", "https://arxiv.org/abs/2004.05150")]:
            it = api("POST", "/api/projects/%s/items" % pid, {"input": url})
            api("PATCH", "/api/projects/%s/items/%s" % (pid, it["id"]), {"title": title})
        api("POST", "/api/projects/%s/items" % pid, {"type": "note", "title": "读书计划",
            "note": "这周读完 **3 篇** 长上下文论文，重点看 <ins>计算预算怎么分配</ins>；对比 $O(n^2)$ 与 $O(n\\log n)$ 的方法。"})

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME)
            ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
            # 无头 Chrome 没有系统语音；截图时模拟 macOS 常见语音，让朗读栏显示正常
            ctx.add_init_script(VOICES_JS)
            page = ctx.new_page()

            # ---------- 阅读页：PDF + 标注 + 笔记
            page.goto("%s/r/%s/%s" % (base, pid, iid))
            page.wait_for_selector(".textLayer span", timeout=30000)
            time.sleep(2.5)
            # 选同一行内的短语（跨行的选区在 pdf.js 文字层里不稳定）
            marks = [(1, "assigns each token a learned budget", "highlight", "yellow"),
                     (1, "matches full-attention accuracy while using 38% of", "highlight", "green"),
                     (1, "allocate a uniform amount of", "underline", "blue"),
                     (1, "Skimmer stays within 0.3 points of full attention while using", "highlight", "pink")]
            for pg, phrase, kind, color in marks:
                if page.evaluate(SELECT_JS, [pg, phrase]):
                    time.sleep(0.3)
                    if kind == "highlight":
                        btn = page.locator(".sel-toolbar [data-action='highlight'][data-color='%s']" % color)
                    else:
                        btn = page.locator(".sel-toolbar [data-action='%s']" % kind)
                    if btn.count():
                        btn.first.click()
                        time.sleep(0.4)
            page.mouse.click(5, H - 5)
            state = api("GET", "/api/papers/%s/%s" % (pid, iid))
            anns = state.get("annotations", [])
            notes = [
                {"page": 1, "quote": anns[0]["quote"] if anns else "learned budget",
                 "annotation_id": anns[0]["id"] if anns else None,
                 "text": "核心想法：给**每个 token** 一个可学习的预算 $b_i\\in[0,1]$，预算低的 token 走便宜的汇总路径。\n\n和固定稀疏模式的区别是 <ins>预算依赖内容</ins>。"},
                {"page": 1, "quote": anns[1]["quote"] if len(anns) > 1 else "38%",
                 "annotation_id": anns[1]["id"] if len(anns) > 1 else None,
                 "text": "关键结果：准确率只差 0.3，FLOPs 降到 **38%**。想看一下预算从 0.6 降到 0.2 时的曲线。"},
                {"page": 1, "quote": "", "text": "疑问：pooled summary 的常数开销在短输入上占多少？Limitations 里提到了。"},
            ]
            for n in notes:
                api("POST", "/api/papers/%s/%s/notes" % (pid, iid), {k: v for k, v in n.items() if v is not None})
            store.add_qa(data, pid, iid, {
                "question": "为什么要把 query 和 key 的预算相乘，而不是只用 query 的？",
                "answer": ("**结论**：相乘让「低价值的 key」也被压低，两边任何一方预算低，这一对的注意力就会变弱 (p.1)。\n\n"
                           "具体来说，注意力 logit 被缩放成 $b_i b_j \\, q_i^\\top k_j / \\sqrt{d_k}$：\n\n"
                           "- 只用 $b_i$：只能决定**谁去读**，但被读的内容不区分轻重；\n"
                           "- 用 $b_i b_j$：同时决定**谁去读、读谁**，冗余的引用和页眉作为 key 也会被跳过。\n\n"
                           "配合目标函数里的 $\\beta\\sum_i b_i(1-b_i)$，预算会被推向 0 或 1，推理时的硬阈值 $\\tau$ 才和训练一致 (p.1)。"),
                "engine": "claude", "mode": "ask", "quote": None, "page": 1})
            store.add_translation(data, pid, iid, {
                "source": "Tokens whose budget falls below a threshold are routed to a cheap pooled summary instead of full attention.",
                "translation": "预算低于阈值的 token 不参与完整注意力，而是被送入一个代价很低的池化摘要。",
                "engine": "google", "page": 1})
            for v in [("skim", "/skɪm/", "v.", "略读；浏览", "在本文中指模型对低价值片段只做粗略处理",
                       "Skimmer lets the model skim low-value spans while reading salient spans in full."),
                      ("salient", "/ˈseɪliənt/", "adj.", "显著的；突出的", "指对当前问题最重要的文本片段",
                       "reading salient spans in full"),
                      ("relaxation", "/ˌriːlækˈseɪʃn/", "n.", "（数学）松弛", "把硬约束换成可微的近似",
                       "trained end-to-end with a differentiable relaxation of the budget constraint")]:
                store.add_vocab(data, pid, iid, {"word": v[0], "phonetic": v[1], "pos": v[2], "meaning": v[3],
                                                 "context_meaning": v[4], "context": v[5], "page": 1})
            page.reload()
            page.wait_for_selector(".textLayer span", timeout=30000)
            time.sleep(2.5)
            page.locator("[data-tab='notes']").first.click()
            time.sleep(0.6)
            page.screenshot(path=os.path.join(OUT, "reader-notes.png"))
            page.screenshot(path=os.path.join(work, "home-card.png"))

            page.locator("[data-tab='qa']").first.click()
            time.sleep(0.8)
            page.screenshot(path=os.path.join(OUT, "reader-qa.png"))

            page.locator("[data-tab='vocab']").first.click()
            time.sleep(0.6)
            tts = page.locator("[data-action='tts']")
            if tts.count():
                tts.first.click()
                time.sleep(0.5)
            page.screenshot(path=os.path.join(OUT, "reader-vocab-tts.png"))
            if tts.count():
                tts.first.click()

            # ---------- 博客资源
            blog_btn = page.locator("[data-res]").filter(has_text="博客")
            if blog_btn.count():
                blog_btn.first.click()
                page.wait_for_selector(".article-view", timeout=20000)
                time.sleep(2)
                for phrase, kind, color in [("teaches a transformer to do something similar", "highlight", "yellow"),
                                            ("this particular document", "underline", "blue"),
                                            ("where", "highlight", "green")][:2]:
                    if page.evaluate(SELECT_ARTICLE_JS, phrase):
                        time.sleep(0.3)
                        sel_ = (".sel-toolbar [data-action='highlight'][data-color='%s']" % color) if kind == "highlight" \
                            else ".sel-toolbar [data-action='%s']" % kind
                        if page.locator(sel_).count():
                            page.locator(sel_).first.click()
                            time.sleep(0.5)
                page.mouse.click(5, H - 5)
                rid = None
                for r in (api("GET", "/api/papers/%s/%s" % (pid, iid)).get("meta", {}).get("resources") or []):
                    if r.get("kind") == "blog":
                        rid = r["id"]
                if rid:
                    api("POST", "/api/papers/%s/%s/notes" % (pid, iid), {
                        "resource_id": rid, "quote": "teaches a transformer to do something similar",
                        "text": "博客的类比很好记：像审稿人一样，**样板内容快速略过**，关键处放慢。适合拿来给组会开场。"})
                    page.reload()
                    page.wait_for_selector(".article-view", timeout=20000)
                    time.sleep(2)
                page.locator("[data-tab='notes']").first.click()
                time.sleep(0.6)
                page.screenshot(path=os.path.join(OUT, "reader-blog.png"))

            # ---------- 项目页（把演示博客的本地地址换成一个示例域名，截图更自然）
            for r in (api("GET", "/api/papers/%s/%s" % (pid, iid)).get("meta", {}).get("resources") or []):
                if r.get("kind") == "blog":
                    api("PATCH", "/api/projects/%s/items/%s/resources/%s" % (pid, iid, r["id"]),
                        {"url": "https://longcontext.example/posts/skimming-explained"})
            page.goto("%s/p/%s" % (base, pid))
            time.sleep(1.5)
            page.screenshot(path=os.path.join(OUT, "project.png"))

            # ---------- 生词本
            page.goto("%s/vocab" % base)
            time.sleep(1.2)
            page.screenshot(path=os.path.join(OUT, "vocab-book.png"))

            # ---------- 在线只读版（GitHub Pages）
            api("POST", "/api/sync/export", {})
            ssrv, sport = serve(work)
            page.goto("http://127.0.0.1:%d/library/#/r/%s/%s" % (sport, pid, iid))
            time.sleep(5)
            page.screenshot(path=os.path.join(OUT, "static-readonly.png"))
            ssrv.shutdown()

            # 主页卡片用的 1x 版本
            ctx1 = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
            p1 = ctx1.new_page()
            p1.goto("%s/r/%s/%s" % (base, pid, iid))
            p1.wait_for_selector(".textLayer span", timeout=30000)
            time.sleep(2.5)
            p1.locator("[data-tab='notes']").first.click()
            time.sleep(0.6)
            p1.screenshot(path=os.path.join(OUT, "reader-notes-1x.png"))
            browser.close()
    finally:
        proc.terminate()
        asrv.shutdown()
    # README 里显示宽度有限：2x 截图缩到 2000px 宽；1x 那张留给个人主页的卡片
    try:
        from PIL import Image
        for f in os.listdir(OUT):
            fp = os.path.join(OUT, f)
            if f.endswith(".png") and not f.endswith("-1x.png"):
                im = Image.open(fp)
                if im.width > 2000:
                    im.resize((2000, round(im.height * 2000 / im.width)), Image.LANCZOS).save(fp, optimize=True)
    except ImportError:
        pass
    print("screenshots ->", OUT)
    for f in sorted(os.listdir(OUT)):
        print("  ", f, os.path.getsize(os.path.join(OUT, f)) // 1024, "KB")


if __name__ == "__main__":
    main()
