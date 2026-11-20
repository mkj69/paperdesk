"""PaperDesk 后端单元 / 集成测试（只用标准库）。

运行：python3 -m unittest tests/test_backend.py -v
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from paperdesk import agents, config, fetcher, store  # noqa: E402
from paperdesk.api import make_server  # noqa: E402
from paperdesk.gitsync import GitSync, clone_library, github_web_url, pages_url_from_remote  # noqa: E402

MOCK_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("MOCK_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"bin": "claude", "args": args, "cwd": os.getcwd()}) + "\n")
def val(flag):
    if flag in args:
        i = args.index(flag)
        return args[i + 1] if i + 1 < len(args) else None
    return None
fmt = val("--output-format")
prompt = sys.stdin.read()
if fmt == "json":
    if "JSON 对象" in prompt:
        res = json.dumps({"phonetic": "/əˈtenʃn/", "pos": "n.", "meaning": "注意；关注",
                          "context_meaning": "注意力机制", "context_translation": "整句译文"}, ensure_ascii=False)
    else:
        body = prompt.split('"""')[1].strip() if '"""' in prompt else ""
        res = "【译】" + body
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": res,
                      "session_id": "tr-1"}, ensure_ascii=False))
    sys.exit(0)
resume = val("--resume")
if resume == "bad-session":
    sys.stderr.write("No conversation found with session ID\n")
    sys.exit(1)
sid = resume or "sess-123"
def emit(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)
emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd()})
has = os.path.exists("paper.txt") and os.path.exists("CLAUDE.md") and os.path.exists("notes.md")
emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
      "content_block": {"type": "tool_use", "name": "Read", "id": "t1"}}})
pieces = ["你好，", "这是**回答**。", " files=%s" % has]
for p in pieces:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
          "delta": {"type": "text_delta", "text": p}}})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "".join(pieces)}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "".join(pieces), "session_id": sid})
'''

MOCK_CODEX = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("MOCK_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"bin": "codex", "args": args, "cwd": os.getcwd()}) + "\n")
out = args[args.index("-o") + 1] if "-o" in args else None
prompt = args[-1]
if "resume" in args:
    tid = args[args.index("resume") + 1]
    if tid == "bad-thread":
        sys.stderr.write("thread not found\n")
        sys.exit(1)
else:
    tid = "thread-abc"
if "JSON 对象" in prompt:
    text = json.dumps({"phonetic": "", "pos": "v.", "meaning": "利用", "context_meaning": "使用",
                       "context_translation": "句子译文"}, ensure_ascii=False)
elif "学术论文翻译" in prompt:
    text = "【codex译】" + prompt.split('"""')[1].strip()
else:
    text = "Codex 回答：" + ("有全文" if os.path.exists("paper.txt") else "无全文")
if "--json" in args:
    print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)
    if os.environ.get("MOCK_CODEX_NO_MSG") != "1":
        print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
                         ensure_ascii=False), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
if out:
    with open(out, "w") as f:
        f.write(text)
'''


def make_pdf(text="Hello PaperDesk"):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offs:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class MockEnv:
    """把模拟 claude / codex 写进临时目录，并设置 PAPERDESK_CLAUDE / PAPERDESK_CODEX。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pd-mocks-")
        self.log = os.path.join(self.dir, "calls.log")
        for name, src in (("claude", MOCK_CLAUDE), ("codex", MOCK_CODEX)):
            p = os.path.join(self.dir, name)
            with open(p, "w") as f:
                f.write("#!%s\n%s" % (sys.executable, src))
            os.chmod(p, 0o755)
        self.old = {k: os.environ.get(k) for k in ("PAPERDESK_CLAUDE", "PAPERDESK_CODEX", "MOCK_LOG")}
        os.environ["PAPERDESK_CLAUDE"] = os.path.join(self.dir, "claude")
        os.environ["PAPERDESK_CODEX"] = os.path.join(self.dir, "codex")
        os.environ["MOCK_LOG"] = self.log

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


# =============================================================================================
class TestFetcher(unittest.TestCase):
    def test_parse_arxiv(self):
        self.assertEqual(fetcher.parse_arxiv_id("1706.03762"), "1706.03762")
        self.assertEqual(fetcher.parse_arxiv_id("2406.12345v2"), "2406.12345v2")
        self.assertEqual(fetcher.parse_arxiv_id("arXiv:2406.12345"), "2406.12345")
        self.assertEqual(fetcher.parse_arxiv_id("https://arxiv.org/abs/1706.03762v7"), "1706.03762v7")
        self.assertEqual(fetcher.parse_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf"), "1706.03762")
        self.assertEqual(fetcher.parse_arxiv_id("https://arxiv.org/html/2406.12345v1"), "2406.12345v1")
        self.assertEqual(fetcher.parse_arxiv_id("https://www.alphaxiv.org/abs/2406.12345"), "2406.12345")
        self.assertEqual(fetcher.parse_arxiv_id("hep-th/9901001"), "hep-th/9901001")
        self.assertIsNone(fetcher.parse_arxiv_id("https://example.com/1706.03762"))
        self.assertIsNone(fetcher.parse_arxiv_id("hello world"))

    def test_paper_key(self):
        self.assertEqual(fetcher.paper_key_for(arxiv_id="1706.03762v7"), "arxiv-1706.03762")
        self.assertEqual(fetcher.paper_key_for(arxiv_id="hep-th/9901001"), "arxiv-hep-th_9901001")
        k = fetcher.paper_key_for(url="https://x.org/a.pdf")
        self.assertTrue(k.startswith("url-") and len(k) == 14)

    @mock.patch.object(fetcher, "fetch_arxiv_meta", return_value=None)
    @mock.patch.object(fetcher, "_http_head_ctype", side_effect=OSError("offline"))
    @mock.patch.object(fetcher, "_http_get", side_effect=OSError("offline"))
    def test_resolve_offline(self, *_):
        r = fetcher.resolve("1706.03762v5")
        self.assertEqual(r["type"], "paper")
        self.assertEqual(r["pdf_url"], "https://arxiv.org/pdf/1706.03762v5")
        self.assertEqual(r["url"], "https://arxiv.org/abs/1706.03762")
        self.assertEqual(r["paper_key"], "arxiv-1706.03762")
        self.assertEqual(r["year"], "2017")
        r = fetcher.resolve("https://example.com/files/paper.pdf")
        self.assertEqual((r["type"], r["title"]), ("paper", "paper.pdf"))
        r = fetcher.resolve("https://example.com/blog")
        # SPEC §13：非 PDF 网页 → type "web"，带 kind / paper_key
        self.assertEqual((r["type"], r["title"], r["kind"]), ("web", "https://example.com/blog", "blog"))
        self.assertTrue(r["paper_key"].startswith("web-"))
        r = fetcher.resolve("https://openreview.net/forum?id=abc123")
        self.assertEqual(r["pdf_url"], "https://openreview.net/pdf?id=abc123")
        with self.assertRaises(ValueError):
            fetcher.resolve("not a link")

    def test_resolve_meta(self):
        meta = {"title": "Attention Is All You Need", "authors": ["A", "B"], "abstract": "abs", "year": "2017"}
        with mock.patch.object(fetcher, "fetch_arxiv_meta", return_value=meta):
            r = fetcher.resolve("https://arxiv.org/abs/1706.03762")
        self.assertEqual(r["title"], "Attention Is All You Need")
        self.assertEqual(r["authors"], ["A", "B"])

    def test_upstream_rejects_scheme(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher.open_upstream("file:///etc/passwd")


# =============================================================================================
class TestStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="pd-store-")
        store.set_change_hook(None)
        store.ensure_library(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _paper(self, pid="inbox"):
        return store.add_item(self.d, pid, {"type": "paper", "title": "Attention Is All You Need",
                                            "url": "https://arxiv.org/abs/1706.03762",
                                            "pdf_url": "https://arxiv.org/pdf/1706.03762",
                                            "arxiv_id": "1706.03762", "authors": ["Vaswani", "Shazeer"],
                                            "year": "2017", "paper_key": "arxiv-1706.03762"})

    def test_library_layout(self):
        for f in (".gitignore", "README.md", "settings.json", "vocabulary.md", "projects/inbox/project.json",
                  "projects/inbox/README.md"):
            self.assertTrue(os.path.exists(os.path.join(self.d, f)), f)
        projs = store.list_projects(self.d)
        self.assertEqual(projs[0]["id"], "inbox")
        with open(os.path.join(self.d, ".gitignore")) as f:
            self.assertIn(".cache/", f.read())

    def test_projects(self):
        p = store.create_project(self.d, "认知核心", "研究方向")
        self.assertTrue(p["id"].startswith("p-"))
        p2 = store.create_project(self.d, "Cognitive Core")
        self.assertEqual(p2["id"], "cognitive-core")
        p3 = store.create_project(self.d, "Cognitive Core")
        self.assertEqual(p3["id"], "cognitive-core-2")
        store.update_project(self.d, p2["id"], name="Cognitive Core v2")
        self.assertEqual(store.get_project(self.d, p2["id"])["name"], "Cognitive Core v2")
        store.delete_project(self.d, p3["id"])
        self.assertNotIn(p3["id"], [x["id"] for x in store.list_projects(self.d)])
        with self.assertRaises(ValueError):
            store.delete_project(self.d, "inbox")
        with self.assertRaises(store.NotFound):
            store.get_project(self.d, "nope")

    def test_items_and_markdown(self):
        it = self._paper()
        self.assertFalse(it["existing"])
        again = self._paper()
        self.assertTrue(again["existing"])
        self.assertEqual(again["id"], it["id"])
        link = store.add_item(self.d, "inbox", {"type": "link", "url": "https://example.com", "title": "Ex"})
        note = store.add_item(self.d, "inbox", {"type": "note", "title": "想法", "note": "**重要**"})
        pr = store.get_project(self.d, "inbox")
        self.assertEqual([x["type"] for x in pr["items"]], ["note", "link", "paper"])
        self.assertTrue(link["id"] and note["id"])

        a = store.add_annotation(self.d, "inbox", it["id"], {"kind": "underline", "color": "blue", "page": 3,
                                                              "quote": "scaled dot-product",
                                                              "rects": [[0.1, 0.2, 0.3, 0.02]]})
        n = store.add_note(self.d, "inbox", it["id"], {"text": "这里 <ins>很关键</ins>，公式 $\\sqrt{d_k}$",
                                                       "page": 3, "quote": "scaled dot-product",
                                                       "annotation_id": a["id"]})
        store.add_qa(self.d, "inbox", it["id"], {"question": "为什么除以根号 dk？", "answer": "方差**变大**",
                                                 "engine": "claude", "page": 4})
        st = store.get_paper_state(self.d, "inbox", it["id"])
        self.assertEqual(st["status"], "reading")
        self.assertEqual(st["annotations"][0]["rects"], [[0.1, 0.2, 0.3, 0.02]])
        md = store.paper_markdown(self.d, "inbox", it["id"])
        self.assertIn("# Attention Is All You Need", md)
        self.assertIn("http://localhost:8765/r/inbox/%s" % it["id"], md)
        self.assertIn("<ins>很关键</ins>", md)
        self.assertIn("<ins>“scaled dot-product”</ins>", md)
        self.assertIn("### ❓ 为什么除以根号 dk？", md)
        self.assertIn("## 🔤 生词", md)
        with open(os.path.join(self.d, "projects/inbox/papers/arxiv-1706.03762.md")) as f:
            self.assertEqual(f.read(), md.replace(st["updated"][:16].replace("T", " "),
                                                  st["updated"][:16].replace("T", " ")))
        with open(os.path.join(self.d, "projects/inbox/README.md")) as f:
            readme = f.read()
        self.assertIn("/r/inbox/%s" % it["id"], readme)
        self.assertIn("papers/arxiv-1706.03762.md", readme)
        self.assertIn("**重要**", readme)

        store.update_note(self.d, "inbox", it["id"], n["id"], {"text": "改过了"})
        store.delete_annotation(self.d, "inbox", it["id"], a["id"])
        st = store.get_paper_state(self.d, "inbox", it["id"])
        self.assertIsNone(st["notes"][0]["annotation_id"])
        summ = store.set_summary(self.d, "inbox", it["id"], "### 一句话总结\n**好**", "claude")
        self.assertEqual(summ["engine"], "claude")
        self.assertEqual(store.find_item(self.d, "inbox", it["id"])["status"], "done")
        self.assertIn("## 🧾 总结", store.paper_markdown(self.d, "inbox", it["id"]))

    def test_move_delete(self):
        it = self._paper()
        store.write_cached_text(self.d, "inbox", it["paper_key"], [{"page": 1, "text": "hello"}])
        p = store.create_project(self.d, "Proj")
        store.move_item(self.d, "inbox", it["id"], p["id"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "projects", p["id"], "papers", "arxiv-1706.03762.json")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "projects/inbox/papers/arxiv-1706.03762.json")))
        self.assertEqual(store.read_cached_text(self.d, p["id"], it["paper_key"])[0]["text"], "hello")
        st = store.get_paper_state(self.d, p["id"], it["id"])
        self.assertEqual(st["pid"], p["id"])
        it2 = self._paper()
        with self.assertRaises(store.Conflict):
            store.move_item(self.d, "inbox", it2["id"], p["id"])
        store.delete_item(self.d, p["id"], it["id"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "projects", p["id"], "papers", "arxiv-1706.03762.json")))
        self.assertTrue(any("arxiv-1706.03762.json" in f for f in os.listdir(os.path.join(self.d, ".trash"))))

    def test_cached_text_and_current(self):
        it = self._paper()
        n = store.write_cached_text(self.d, "inbox", it["paper_key"],
                                    [{"page": 2, "text": "bb"}, {"page": 1, "text": "aa"}])
        self.assertEqual(n, 4)
        s = store.cached_text_string(self.d, "inbox", it["paper_key"])
        self.assertLess(s.index("[p.1]"), s.index("[p.2]"))
        self.assertNotIn("[p.1]", store.cached_text_string(self.d, "inbox", it["paper_key"], pages="2"))
        store.write_current(self.d, "inbox", it["id"])
        self.assertEqual(store.read_current(self.d)["iid"], it["id"])

    def test_vocab(self):
        it = self._paper()
        a = store.add_annotation(self.d, "inbox", it["id"], {"kind": "vocab", "page": 2, "quote": "salient",
                                                              "rects": [[0, 0, 0.1, 0.01]]})
        self.assertEqual(a["color"], "purple")
        v = store.add_vocab(self.d, "inbox", it["id"], {"word": "Salient", "context": "the most salient feature",
                                                         "page": 2, "annotation_id": a["id"],
                                                         "meaning": "显著的", "pos": "adj.", "phonetic": "/ˈseɪliənt/"})
        self.assertFalse(v["merged"])
        self.assertEqual(v["lemma"], "salient")
        v2 = store.add_vocab(self.d, "inbox", it["id"], {"word": "salient", "context": "another salient point",
                                                          "page": 5})
        self.assertTrue(v2["merged"])
        self.assertEqual(v2["id"], v["id"])
        self.assertEqual(len(v2["extra_contexts"]), 1)
        st = store.get_paper_state(self.d, "inbox", it["id"])
        self.assertEqual(len(st["vocab"]), 1)
        md = store.paper_markdown(self.d, "inbox", it["id"])
        self.assertIn("| 单词 | 音标 | 词性 | 释义 | 原句 |", md)
        self.assertIn("the most **salient** feature (p.2)", md)
        with open(os.path.join(self.d, "vocabulary.md")) as f:
            voc = f.read()
        self.assertIn("## S", voc)
        self.assertIn("projects/inbox/papers/arxiv-1706.03762.md", voc)
        with open(os.path.join(self.d, "README.md")) as f:
            self.assertIn("(vocabulary.md)", f.read())
        allv = store.list_all_vocab(self.d)
        self.assertEqual(allv[0]["paper_title"], "Attention Is All You Need")
        self.assertEqual(store.list_all_vocab(self.d, mastered=True), [])
        store.update_vocab(self.d, "inbox", it["id"], v["id"], {"mastered": True})
        self.assertEqual(len(store.list_all_vocab(self.d, mastered=True)), 1)
        with open(os.path.join(self.d, "vocabulary.md")) as f:
            self.assertIn("## ✅ 已掌握", f.read())
        store.delete_vocab(self.d, "inbox", it["id"], v["id"])
        st = store.get_paper_state(self.d, "inbox", it["id"])
        self.assertEqual(st["vocab"], [])
        self.assertEqual(st["annotations"], [])
        with self.assertRaises(ValueError):
            store.add_vocab(self.d, "inbox", it["id"], {"word": "a b c d e f g"})

    def test_change_hook(self):
        calls = []
        it = self._paper()
        store.set_change_hook(calls.append)
        try:
            store.create_project(self.d, "Hooked")
            n = len(calls)
            store.update_paper(self.d, "inbox", it["id"], {"last_page": 5, "zoom": 2, "tts_pos": {"page": 5, "sentence": 1}})
            self.assertEqual(len(calls), n)       # 高频字段不触发同步
            store.update_paper(self.d, "inbox", it["id"], {"status": "done"})
            self.assertEqual(len(calls), n + 1)
        finally:
            store.set_change_hook(None)
        self.assertTrue(calls and "Hooked" in calls[0])
        st = store.get_paper_state(self.d, "inbox", it["id"])
        self.assertEqual((st["last_page"], st["tts_pos"]), (5, {"page": 5, "sentence": 1}))
        self.assertEqual(store.find_item(self.d, "inbox", it["id"])["status"], "done")


# =============================================================================================
class TestAgents(unittest.TestCase):
    def setUp(self):
        self.mocks = MockEnv()
        self.d = tempfile.mkdtemp(prefix="pd-agents-")
        store.set_change_hook(None)
        store.ensure_library(self.d)
        self.it = store.add_item(self.d, "inbox", {"type": "paper", "title": "T", "pdf_url": "https://x/a.pdf",
                                                   "url": "https://x/a.pdf"})
        store.write_cached_text(self.d, "inbox", self.it["paper_key"], [{"page": 1, "text": "full text"}])
        self.s = config.load_settings(self.d)

    def tearDown(self):
        self.mocks.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def _wd(self):
        st = store.get_paper_state(self.d, "inbox", self.it["id"])
        return agents.prepare_workdir(self.d, "inbox", st, self.s)

    def test_voice_prompt(self):
        p = agents.build_ask_prompt("为什么？", "quote text", 3, "ask", "voice", "中文")
        self.assertIn("第 3 页", p)
        self.assertIn("口语化的中文", p)
        self.assertNotIn("口语化", agents.build_ask_prompt("为什么？"))

    def test_workdir(self):
        wd = self._wd()
        for f in ("paper.txt", "notes.md", "CLAUDE.md", "AGENTS.md"):
            self.assertTrue(os.path.exists(os.path.join(wd, f)))
        with open(os.path.join(wd, "paper.txt")) as f:
            self.assertIn("===== [p.1] =====", f.read())

    def test_claude_stream(self):
        evs = list(agents.run_agent("claude", "问题", self._wd(), self.s))
        deltas = "".join(e["text"] for e in evs if e["type"] == "delta")
        self.assertEqual(deltas, "你好，这是**回答**。 files=True")
        self.assertTrue(any(e["type"] == "status" for e in evs))
        final = evs[-1]
        self.assertEqual((final["type"], final["session_id"]), ("final", "sess-123"))
        args = self.mocks.calls()[-1]["args"]
        self.assertIn("--include-partial-messages", args)
        self.assertIn("Read,Grep,Glob", args)

    def test_claude_resume_and_fallback(self):
        evs = list(agents.run_agent("claude", "q", self._wd(), self.s, session_id="sess-old"))
        self.assertEqual(evs[-1]["session_id"], "sess-old")
        self.assertIn("--resume", self.mocks.calls()[-1]["args"])
        evs = list(agents.run_agent("claude", "q", self._wd(), self.s, session_id="bad-session"))
        self.assertEqual(evs[-1]["session_id"], "sess-123")
        self.assertTrue(any(e["type"] == "status" and "新会话" in e["text"] for e in evs))

    def test_codex(self):
        evs = list(agents.run_agent("codex", "q", self._wd(), self.s))
        self.assertEqual(evs[-1]["text"], "Codex 回答：有全文")
        self.assertEqual(evs[-1]["session_id"], "thread-abc")
        evs = list(agents.run_agent("codex", "q", self._wd(), self.s, session_id="thread-xyz"))
        self.assertEqual(evs[-1]["session_id"], "thread-xyz")
        a = self.mocks.calls()[-1]["args"]
        self.assertEqual(a[0], "exec")
        self.assertEqual(a[a.index("resume") + 1], "thread-xyz")
        self.assertLess(a.index("--json"), a.index("resume"))
        evs = list(agents.run_agent("codex", "q", self._wd(), self.s, session_id="bad-thread"))
        self.assertEqual(evs[-1]["session_id"], "thread-abc")

    def test_codex_output_file_fallback(self):
        os.environ["MOCK_CODEX_NO_MSG"] = "1"
        try:
            evs = list(agents.run_agent("codex", "q", self._wd(), self.s))
        finally:
            os.environ.pop("MOCK_CODEX_NO_MSG", None)
        self.assertEqual(evs[-1]["text"], "Codex 回答：有全文")

    def test_missing_cli(self):
        os.environ["PAPERDESK_CLAUDE"] = "/nonexistent/claude"
        with mock.patch.object(config, "find_cli", return_value=None):
            with self.assertRaises(agents.AgentError):
                list(agents.run_agent("claude", "q", self._wd(), self.s))

    def test_translate(self):
        self.assertEqual(agents.translate("Hello world", "claude", "zh-CN", self.s), "【译】Hello world")
        self.assertEqual(agents.translate("Hi there", "codex", "zh-CN", self.s), "【codex译】Hi there")
        n = len(self.mocks.calls())
        agents.translate("Hello world", "claude", "zh-CN", self.s)  # 命中缓存
        self.assertEqual(len(self.mocks.calls()), n)
        with mock.patch.object(agents, "_google", side_effect=agents.AgentError("offline")):
            with self.assertRaises(agents.AgentError):
                agents.translate("x", "google", "zh-CN", self.s)
        self.assertGreater(len(agents._split_chunks("A sentence. " * 400)), 1)

    def test_lookup_word(self):
        with mock.patch.object(agents, "_google_dict", side_effect=OSError("network down")):
            r = agents.lookup_word("salient", "most salient", "google", self.s)
        self.assertTrue(r["lookup_error"])
        self.assertEqual(r["meaning"], "")
        r = agents.lookup_word("attention", "attention is all", "claude", self.s)
        self.assertEqual((r["pos"], r["meaning"], r["lookup_error"]), ("n.", "注意；关注", None))
        r = agents.lookup_word("leverage", "we leverage", "codex", self.s)
        self.assertEqual(r["meaning"], "利用")
        fake = {"pos": "adj.", "meaning": "adj. 显著的；突出的"}
        with mock.patch.object(agents, "_google_dict", return_value=fake), \
                mock.patch.object(agents, "_phonetic", return_value="/ˈseɪliənt/"), \
                mock.patch.object(agents, "_google", return_value="最显著"):
            r = agents.lookup_word("salient", "most salient", "google", self.s)
        self.assertEqual((r["phonetic"], r["context_translation"], r["lookup_error"]), ("/ˈseɪliənt/", "最显著", None))

    def test_google_bd_parsing(self):
        payload = json.dumps([[["注意", "attention", None, None, 10]],
                              [["noun", ["注意", "关注", "注意力"], [], "attention", 1]],
                              "en"]).encode("utf-8")

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload
        with mock.patch.object(agents.urllib.request, "urlopen", return_value=R()):
            d = agents._google_dict("attention", "zh-CN")
        self.assertEqual(d, {"pos": "n.", "meaning": "n. 注意；关注；注意力"})


# =============================================================================================
class TestGitSync(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pd-git-")
        self.d = os.path.join(self.root, "lib")
        self.bare = os.path.join(self.root, "remote.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", self.bare], check=True, capture_output=True)
        store.set_change_hook(None)
        store.ensure_library(self.d)

    def tearDown(self):
        store.set_change_hook(None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _remote_log(self):
        r = subprocess.run(["git", "--git-dir", self.bare, "log", "--oneline", "main"], capture_output=True, text=True)
        return r.stdout.strip().splitlines()

    def test_sync(self):
        settings = {"auto_sync": True, "sync_debounce_sec": 1}
        gs = GitSync(self.d, lambda: settings)
        gs.init_repo()
        self.assertTrue(gs.is_repo())
        st = gs.status()
        self.assertFalse(st["dirty"])
        st = gs.setup(remote_url=self.bare)
        self.assertIsNone(st["last_error"], st)
        self.assertEqual(st["remote"], self.bare)
        base = len(self._remote_log())
        self.assertGreaterEqual(base, 1)
        self.assertFalse(gs.status()["dirty"])
        # 改动 → 防抖自动同步
        store.set_change_hook(gs.schedule)
        store.create_project(self.d, "Synced Project")
        self.assertTrue(gs.status()["pending"])
        for _ in range(50):
            time.sleep(0.2)
            if len(self._remote_log()) >= base + 1 and not gs.status()["pending"]:
                break
        log = self._remote_log()
        self.assertEqual(len(log), base + 1, log)
        self.assertIn("Synced Project", log[0])
        st = gs.status()
        self.assertEqual(st["ahead"], 0)
        # .cache 不应进入仓库
        store.write_cached_text(self.d, "inbox", "k", [{"page": 1, "text": "x"}])
        gs.sync_now()
        files = subprocess.run(["git", "--git-dir", self.bare, "ls-tree", "-r", "--name-only", "main"],
                               capture_output=True, text=True).stdout
        self.assertNotIn(".cache", files)
        self.assertIn("projects/inbox/README.md", files)
        self.assertIn("vocabulary.md", files)

    def test_web_url(self):
        self.assertEqual(github_web_url("git@github.com:mkj69/lib.git"), "https://github.com/mkj69/lib")
        self.assertEqual(github_web_url("https://github.com/mkj69/lib"), "https://github.com/mkj69/lib")
        self.assertIsNone(github_web_url("/tmp/remote.git"))

    def test_setup_create_requires_gh(self):
        gs = GitSync(self.d, lambda: {"auto_sync": False})
        gs.init_repo()
        real = config.find_cli
        with mock.patch.object(config, "find_cli", side_effect=lambda n, s=None: None if n == "gh" else real(n, s)):
            with self.assertRaises(RuntimeError):
                gs.setup(create="paper-library", private=False)


# =============================================================================================
class TestPagesExport(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pd-pages-")
        self.d = os.path.join(self.root, "lib")
        self.web = os.path.join(self.root, "web")
        store.set_change_hook(None)
        store.ensure_library(self.d)
        files = {
            "index.html": '<!doctype html><html><head><meta charset="utf-8">'
                          '<link rel="stylesheet" href="/static/styles.css"></head>'
                          '<body><script type="module" src="/static/app.js"></script></body></html>',
            "app.js": "import { a } from '/static/js/a.js';\nconsole.log(a);\n",
            "js/a.js": "import { m } from '/static/vendor/m.js';\nexport * from \"/static/vendor/m.js\";\n"
                       "const V = '/static/vendor/pdfjs/';\nconst lazy = () => import('/static/js/b.js');\n"
                       "const ext = 'https://cdn.example.com/static/x.js';\n"
                       "const W = new URL('/static/vendor/w.mjs', import.meta.url);\nexport const a = [m, V, lazy, ext, W];\n",
            "js/b.js": "export const b = 1;\n",
            "styles.css": "@font-face{src:url('/static/vendor/f.woff2')}\nbody{background:url(/static/bg.png)}\n",
            "vendor/m.js": "export const m = '/static/should-be-rewritten-only-if-quoted';\n",
            ".DS_Store": "junk",
        }
        for rel, text in files.items():
            path = os.path.join(self.web, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        with open(os.path.join(self.web, "bg.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _read(self, rel):
        with open(os.path.join(self.d, rel), "r", encoding="utf-8") as f:
            return f.read()

    def test_export(self):
        it = store.add_item(self.d, "inbox", {"type": "paper", "title": "P", "pdf_url": "https://x/p.pdf"})
        store.add_vocab(self.d, "inbox", it["id"], {"word": "salient", "context": "salient bit", "meaning": "显著的"})
        r = store.export_static_site(self.d, self.web)
        self.assertTrue(r["web"] and r["library"] and r["vocabulary"] and r["index"])
        self.assertEqual(r["copied"], 7)
        lib = json.loads(self._read("library.json"))
        self.assertEqual(lib["projects"][0]["id"], "inbox")
        self.assertEqual(lib["app_url"], "http://localhost:8765")
        self.assertEqual(json.loads(self._read("vocabulary.json"))[0]["word"], "salient")
        self.assertTrue(os.path.exists(os.path.join(self.d, ".nojekyll")))
        idx = self._read("index.html")
        self.assertIn('<head><script>window.PAPERDESK_STATIC=true;</script><meta', idx)
        self.assertIn('href="static/styles.css"', idx)
        self.assertNotIn("/static/", idx)
        self.assertEqual(self._read("404.html"), idx)            # 没有 pages_url 时与 index 相同
        a = self._read("static/js/a.js")
        self.assertIn("from '../vendor/m.js'", a)
        self.assertIn('export * from "../vendor/m.js"', a)
        self.assertIn("const V = 'static/vendor/pdfjs/'", a)    # 运行时路径：相对页面
        self.assertIn("import('../js/b.js')", a)
        self.assertIn("https://cdn.example.com/static/x.js", a)  # 外部 URL 不动
        self.assertIn("new URL('../vendor/w.mjs', import.meta.url)", a)
        self.assertIn("from './js/a.js'", self._read("static/app.js"))
        css = self._read("static/styles.css")
        self.assertIn("url('./vendor/f.woff2')", css)
        self.assertIn("url(./bg.png)", css)
        with open(os.path.join(self.d, "static/bg.png"), "rb") as f:
            self.assertEqual(f.read()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertFalse(os.path.exists(os.path.join(self.d, "static/.DS_Store")))
        with open(os.path.join(self.d, ".gitignore")) as f:
            gi = f.read()
        for name in ("index.html", "static", "library.json", "vocabulary.json"):
            self.assertNotIn(name, gi)

        # 第二次：没有变化 → 不改任何文件
        mtime = os.path.getmtime(os.path.join(self.d, "library.json"))
        r = store.export_static_site(self.d, self.web)
        self.assertEqual((r["copied"], r["removed"], r["library"], r["vocabulary"], r["index"]),
                         (0, 0, False, False, False))
        self.assertEqual(os.path.getmtime(os.path.join(self.d, "library.json")), mtime)

        # 改一个文件、删一个文件 → 增量
        time.sleep(1.1)
        with open(os.path.join(self.web, "js/b.js"), "w") as f:
            f.write("export const b = 2;\n")
        os.remove(os.path.join(self.web, "bg.png"))
        r = store.export_static_site(self.d, self.web)
        self.assertEqual((r["copied"], r["removed"]), (1, 1))
        self.assertFalse(os.path.exists(os.path.join(self.d, "static/bg.png")))
        self.assertIn("b = 2", self._read("static/js/b.js"))

        # pages_url → 404.html 带 <base>，md / README 带在线只读链接
        config.save_settings(self.d, {"pages_url": "https://mkj69.github.io/paper-library"})
        store.rerender_all(self.d)
        r = store.export_static_site(self.d, self.web)
        self.assertEqual(r["pages_url"], "https://mkj69.github.io/paper-library/")
        self.assertIn('<base href="/paper-library/">', self._read("404.html"))
        self.assertEqual(json.loads(self._read("library.json"))["pages_url"], "https://mkj69.github.io/paper-library/")
        md = self._read("projects/inbox/papers/%s.md" % it["paper_key"])
        self.assertIn("[📖 在 PaperDesk 中打开（本地，可编辑）](http://localhost:8765/r/inbox/%s)" % it["id"], md)
        self.assertIn("[🌐 在线只读版](https://mkj69.github.io/paper-library/#/r/inbox/%s)" % it["id"], md)
        self.assertIn("https://mkj69.github.io/paper-library/#/p/inbox", self._read("projects/inbox/README.md"))
        self.assertIn("[🌐 在线只读版](https://mkj69.github.io/paper-library/)", self._read("README.md"))
        self.assertIn("🌐 在线", self._read("vocabulary.md"))

    def test_no_web_dir(self):
        r = store.export_static_site(self.d, os.path.join(self.root, "missing"))
        self.assertFalse(r["web"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "library.json")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "index.html")))

    def test_pages_url_from_remote(self):
        self.assertEqual(pages_url_from_remote("git@github.com:mkj69/paper-library.git"),
                         "https://mkj69.github.io/paper-library/")
        self.assertEqual(pages_url_from_remote("https://github.com/MKJ69/paper-library"),
                         "https://mkj69.github.io/paper-library/")
        self.assertEqual(pages_url_from_remote("https://github.com/mkj69/mkj69.github.io.git"),
                         "https://mkj69.github.io/")
        self.assertIsNone(pages_url_from_remote("/tmp/x.git"))
        self.assertIsNone(pages_url_from_remote(None))

    def test_gitsync_pages(self):
        calls = {"pre": 0, "rerender": 0}

        def pre():
            calls["pre"] += 1
            return store.export_static_site(self.d, self.web)

        def rer():
            calls["rerender"] += 1
            store.rerender_all(self.d)
        gs = GitSync(self.d, lambda: config.load_settings(self.d), pre_sync=pre, on_pages_url_change=rer)
        gs.init_repo()
        self.assertGreaterEqual(calls["pre"], 1)
        tracked = subprocess.run(["git", "ls-files"], cwd=self.d, capture_output=True, text=True).stdout
        for f in ("index.html", "404.html", ".nojekyll", "library.json", "vocabulary.json", "static/js/a.js"):
            self.assertIn(f, tracked)
        self.assertNotIn(".cache", tracked)
        # 连上 GitHub 远端（不推送）→ 自动推导 pages_url
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:mkj69/paper-library.git"], cwd=self.d,
                       check=True)
        self.assertEqual(gs.ensure_pages_url(), "https://mkj69.github.io/paper-library/")
        self.assertEqual(calls["rerender"], 1)
        self.assertEqual(gs.status()["pages_url"], "https://mkj69.github.io/paper-library/")
        gs.ensure_pages_url()
        self.assertEqual(calls["rerender"], 1)       # 已推导过不重复
        # 用户手动设置的 pages_url 不被覆盖
        config.save_settings(self.d, {"pages_url": "https://papers.example.com"})
        gs.ensure_pages_url()
        self.assertEqual(config.load_settings(self.d)["pages_url"], "https://papers.example.com/")
        n = calls["pre"]
        gs.sync_now()       # push 会失败（假远端），但同步前仍导出
        self.assertEqual(calls["pre"], n + 1)

    def test_enable_pages(self):
        gs = GitSync(self.d, lambda: config.load_settings(self.d))
        gs.init_repo()
        with self.assertRaises(RuntimeError):
            gs.enable_pages()            # 没有远端
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/mkj69/paper-library.git"], cwd=self.d,
                       check=True)
        fake = os.path.join(self.root, "gh")
        log = os.path.join(self.root, "gh.log")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\nif [ -n \"$GH_FAIL\" ]; then echo \"$GH_FAIL\" >&2; exit 1; fi\n"
                    "echo '{\"html_url\":\"https://mkj69.github.io/paper-library/\"}'\n" % log)
        os.chmod(fake, 0o755)
        real = config.find_cli
        with mock.patch.object(config, "find_cli", side_effect=lambda n, s=None: fake if n == "gh" else real(n, s)):
            r = gs.enable_pages()
            self.assertTrue(r["ok"] and not r["already"])
            self.assertEqual(r["pages_url"], "https://mkj69.github.io/paper-library/")
            with open(log) as f:
                self.assertIn("api -X POST repos/mkj69/paper-library/pages -f source[branch]=main -f source[path]=/",
                              f.read())
            os.environ["GH_FAIL"] = "HTTP 409: GitHub Pages is already enabled."
            try:
                self.assertTrue(gs.enable_pages()["already"])
                os.environ["GH_FAIL"] = "HTTP 403: Resource not accessible"
                with self.assertRaises(RuntimeError):
                    gs.enable_pages()
            finally:
                os.environ.pop("GH_FAIL", None)


# =============================================================================================
def _git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


class TestMultiMachine(unittest.TestCase):
    """模拟「旧电脑推送 → 新电脑接入 → 两边各加笔记 → 互相同步」。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pd-multi-")
        self.bare1 = os.path.join(self.root, "remote1.git")
        self.bare2 = os.path.join(self.root, "remote2.git")
        for b in (self.bare1, self.bare2):
            subprocess.run(["git", "init", "--bare", "-b", "main", b], check=True, capture_output=True)
        store.set_change_hook(None)

    def tearDown(self):
        store.set_change_hook(None)
        shutil.rmtree(self.root, ignore_errors=True)

    def machine(self, name, local=None):
        d = os.path.join(self.root, name)
        store.ensure_library(d)
        if local:
            config.save_settings(d, local)
        web = os.path.join(self.root, "no-web")
        gs = GitSync(d, lambda: config.load_settings(d), pre_sync=lambda: store.export_static_site(d, web),
                     on_pages_url_change=lambda: store.rerender_all(d))
        gs.init_repo()
        return d, gs

    def assertSynced(self, st):
        self.assertIsNone(st["last_error"], st)
        self.assertFalse(st["dirty"], st)
        self.assertEqual(st["ahead"], 0, st)

    def _paper_state(self, d, pid, iid):
        return store.get_paper_state(d, pid, iid)

    def test_old_to_new_machine_and_back(self):
        # ---------------- 旧电脑 A
        A, gA = self.machine("A", {"claude_path": "/A/bin/claude"})
        proj = store.create_project(A, "Cognitive Core", "认知核心")
        pid = proj["id"]
        paper = store.add_item(A, pid, {"type": "paper", "title": "Attention", "arxiv_id": "1706.03762",
                                        "url": "https://arxiv.org/abs/1706.03762",
                                        "pdf_url": "https://arxiv.org/pdf/1706.03762",
                                        "paper_key": "arxiv-1706.03762"})
        iid = paper["id"]
        store.add_note(A, pid, iid, {"text": "A 的笔记 1", "page": 1})
        doomed = store.add_note(A, pid, iid, {"text": "将被 A 删除", "page": 2})
        store.add_vocab(A, pid, iid, {"word": "salient", "context": "ctx from A", "meaning": "显著的"})
        st = gA.setup(remote_url=self.bare1)
        self.assertSynced(st)
        self.assertFalse(st["adopted"])
        shared = json.loads(_git(self.root, "--git-dir", self.bare1, "show", "main:settings.json").stdout)
        self.assertNotIn("claude_path", shared)
        self.assertNotIn("settings.local.json",
                         _git(self.root, "--git-dir", self.bare1, "ls-tree", "-r", "--name-only", "main").stdout)

        # ---------------- 新电脑 B：空库接入 → 直接采用远端
        B, gB = self.machine("B", {"claude_path": "/B/bin/claude"})
        self.assertTrue(store.is_library_empty(B))
        st = gB.setup(remote_url=self.bare1)
        self.assertSynced(st)
        self.assertTrue(st["adopted"])
        self.assertEqual(st["last_merge"]["kind"], "adopt")
        self.assertEqual(config.load_settings(B)["claude_path"], "/B/bin/claude")   # 本机字段保留
        self.assertNotIn("claude_path", config.read_json(os.path.join(B, "settings.json")))
        self.assertEqual(store.find_item(B, pid, iid)["title"], "Attention")
        self.assertIn("A 的笔记 1", [n["text"] for n in self._paper_state(B, pid, iid)["notes"]])
        self.assertTrue(os.path.exists(os.path.join(B, "projects", pid, "papers", "arxiv-1706.03762.md")))
        self.assertTrue(os.path.exists(os.path.join(B, "library.json")))

        # ---------------- 两边各自修改
        store.add_note(A, pid, iid, {"text": "A 新笔记", "page": 3})
        store.delete_note(A, pid, iid, doomed["id"])
        store.add_item(A, "inbox", {"type": "link", "url": "https://a.example/blog", "title": "A 的链接"})
        store.add_vocab(A, pid, iid, {"word": "leverage", "context": "we leverage A", "meaning": "利用"})

        store.add_note(B, pid, iid, {"text": "B 新笔记", "page": 4})
        store.add_annotation(B, pid, iid, {"kind": "underline", "page": 4, "quote": "B 划的线",
                                          "rects": [[0.1, 0.1, 0.2, 0.02]]})
        store.add_vocab(B, pid, iid, {"word": "Salient", "context": "ctx from B"})
        store.add_vocab(B, pid, iid, {"word": "leverage", "context": "we leverage B", "meaning": "利用；杠杆"})
        onlyb = store.create_project(B, "Only B")

        # ---------------- 互相同步：A 推 → B 合并并推 → A 快进
        self.assertSynced(gA.sync_now())
        st = gB.sync_now()
        self.assertSynced(st)
        self.assertEqual(st["last_merge"]["kind"], "merge")
        st = gA.sync_now()
        self.assertSynced(st)

        for d in (A, B):
            ps = self._paper_state(d, pid, iid)
            self.assertEqual(sorted(n["text"] for n in ps["notes"]), ["A 新笔记", "A 的笔记 1", "B 新笔记"], d)
            self.assertEqual([a["quote"] for a in ps["annotations"]], ["B 划的线"])
            lemmas = sorted(v["lemma"] for v in ps["vocab"])
            self.assertEqual(lemmas, ["leverage", "salient"])            # 同词去重
            sal = [v for v in ps["vocab"] if v["lemma"] == "salient"][0]
            ctxs = [sal["context"]] + [c["context"] for c in sal.get("extra_contexts") or []]
            self.assertIn("ctx from A", ctxs)
            self.assertIn("ctx from B", ctxs)
            self.assertIn(onlyb["id"], [p["id"] for p in store.list_projects(d)])
            inbox_urls = [i.get("url") for i in store.get_project(d, "inbox")["items"]]
            self.assertIn("https://a.example/blog", inbox_urls)
            with open(os.path.join(d, "projects", pid, "papers", "arxiv-1706.03762.md")) as f:
                md = f.read()
            self.assertIn("B 新笔记", md)
            self.assertIn("A 新笔记", md)
            self.assertNotIn("将被 A 删除", md)
            self.assertNotIn("<<<<<<<", md)
        self.assertEqual(config.load_settings(A)["claude_path"], "/A/bin/claude")
        self.assertEqual(config.load_settings(B)["claude_path"], "/B/bin/claude")
        log = _git(self.root, "--git-dir", self.bare1, "log", "--format=%s", "main").stdout
        self.assertIn("合并另一台电脑的修改", log)
        self.assertEqual(_git(A, "rev-parse", "HEAD").stdout, _git(B, "rev-parse", "HEAD").stdout)

        # 再来一轮：B 删除 A 加的链接 → A 同步后也消失
        link = [i for i in store.get_project(B, "inbox")["items"] if i.get("url") == "https://a.example/blog"][0]
        store.delete_item(B, "inbox", link["id"])
        self.assertSynced(gB.sync_now())
        self.assertSynced(gA.sync_now())
        self.assertNotIn("https://a.example/blog", [i.get("url") for i in store.get_project(A, "inbox")["items"]])

    def test_two_existing_libraries_merge(self):
        C1, g1 = self.machine("C1")
        p1 = store.create_project(C1, "From C1")
        store.add_item(C1, "inbox", {"type": "note", "title": "C1 的想法", "note": "**C1**"})
        self.assertSynced(g1.setup(remote_url=self.bare2))

        time.sleep(1.1)     # 保证两个库的首个提交不同（同一秒内创建的空库首个提交可能完全相同）
        C2, g2 = self.machine("C2")
        p2 = store.create_project(C2, "From C2")
        store.add_item(C2, "inbox", {"type": "note", "title": "C2 的想法", "note": "**C2**"})
        same = store.add_item(C2, "inbox", {"type": "paper", "title": "Same", "pdf_url": "https://x/same.pdf"})
        self.assertFalse(store.is_library_empty(C2))
        st = g2.setup(remote_url=self.bare2)
        self.assertSynced(st)
        self.assertFalse(st["adopted"])
        self.assertEqual(st["last_merge"]["kind"], "merge")
        self.assertTrue(st["last_merge"]["unrelated"])
        # C1 也加了同一篇论文（不同 id）→ 合并后只保留一条
        time.sleep(1.1)
        store.add_item(C1, "inbox", {"type": "paper", "title": "Same", "pdf_url": "https://x/same.pdf"})
        self.assertSynced(g1.sync_now())
        self.assertSynced(g2.sync_now())
        self.assertSynced(g1.sync_now())
        for d in (C1, C2):
            ids = [p["id"] for p in store.list_projects(d)]
            self.assertIn(p1["id"], ids)
            self.assertIn(p2["id"], ids)
            items = store.get_project(d, "inbox")["items"]
            self.assertEqual(sorted(i["title"] for i in items if i["type"] == "note"), ["C1 的想法", "C2 的想法"])
            papers = [i for i in items if i["type"] == "paper"]
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["id"], same["id"])       # 先加的那条
            with open(os.path.join(d, "README.md")) as f:
                readme = f.read()
            self.assertIn("From C1", readme)
            self.assertIn("From C2", readme)

    def test_clone_library(self):
        A, gA = self.machine("A")
        store.create_project(A, "Cloned Project")
        self.assertSynced(gA.setup(remote_url=self.bare1))
        D = os.path.join(self.root, "D")
        self.assertTrue(clone_library(self.bare1, D))
        self.assertIn("cloned-project", [p["id"] for p in store.list_projects(D)])
        gD = GitSync(D, lambda: config.load_settings(D))
        gD.init_repo()
        st = gD.sync_now()
        self.assertEqual(st["remote"], self.bare1)
        self.assertIsNone(st["last_error"])
        self.assertFalse(clone_library(self.bare1, D))      # 已有内容 → 跳过
        os.makedirs(os.path.join(self.root, "E"))           # 空目录 → 可以 clone
        self.assertTrue(clone_library(self.bare1, os.path.join(self.root, "E")))

    def test_merge_semantics(self):
        base = {"id": "p", "items": [{"id": "a", "added": "2026-01-01T00:00:00+00:00"},
                                     {"id": "b", "added": "2026-01-02T00:00:00+00:00"}]}
        ours = {"id": "p", "items": [{"id": "a", "added": "2026-01-01T00:00:00+00:00"}],
                "updated": "2026-01-03T00:00:00+00:00"}                 # 删了 b
        theirs = {"id": "p", "items": base["items"] + [{"id": "c", "added": "2026-01-04T00:00:00+00:00"}],
                  "updated": "2026-01-04T00:00:00+00:00"}               # 加了 c
        m = store.merge_data_file("projects/p/project.json", base, ours, theirs)
        self.assertEqual([i["id"] for i in m["items"]], ["c", "a"])
        # 一方删除整篇论文文件、另一方没改 → 删除；另一方改过 → 保留
        st = {"notes": [{"id": "n1"}], "updated": "1"}
        self.assertIsNone(store.merge_data_file("projects/p/papers/k.json", st, None, st))
        st2 = dict(st, notes=[{"id": "n1"}, {"id": "n2"}])
        self.assertEqual(store.merge_data_file("projects/p/papers/k.json", st, None, st2), st2)
        # 同 id 取 updated 较新者
        o = {"notes": [{"id": "n", "text": "旧", "updated": "2026-01-01T00:00:00-06:00"}], "updated": "a"}
        t = {"notes": [{"id": "n", "text": "新", "updated": "2026-01-01T07:00:00+00:00"}], "updated": "b"}
        self.assertEqual(store.merge_data_file("projects/p/papers/k.json", None, o, t)["notes"][0]["text"], "新")
        # settings：三方按键合并，机器相关字段不进共享设置
        s = store.merge_data_file("settings.json", {"a": 1, "b": 1}, {"a": 2, "b": 1, "claude_path": "/x"},
                                  {"a": 1, "b": 3})
        self.assertEqual(s, {"a": 2, "b": 3})

    def test_settings_split(self):
        d = os.path.join(self.root, "S")
        os.makedirs(d)
        # 旧格式：settings.json 里带 claude_path → 读取时仍可用，保存时迁移到 settings.local.json
        config.atomic_write_json(os.path.join(d, "settings.json"), {"claude_path": "/old/claude", "app_url": "http://x"})
        self.assertEqual(config.load_settings(d)["claude_path"], "/old/claude")
        config.save_settings(d, {"codex_path": "/old/codex"})
        self.assertNotIn("claude_path", config.read_json(os.path.join(d, "settings.json")))
        self.assertEqual(config.read_json(os.path.join(d, "settings.local.json")),
                         {"claude_path": "/old/claude", "codex_path": "/old/codex"})
        # 本机文件存在时，仓库里残留的机器字段被忽略
        config.atomic_write_json(os.path.join(d, "settings.json"), {"claude_path": "/other-machine/claude"})
        self.assertEqual(config.load_settings(d)["claude_path"], "/old/claude")


# =============================================================================================
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mocks = MockEnv()
        cls.root = tempfile.mkdtemp(prefix="pd-api-")
        cls.d = os.path.join(cls.root, "lib")
        cls.files = os.path.join(cls.root, "files")
        os.makedirs(cls.files)
        cls.pdf_bytes = make_pdf("Attention test")
        with open(os.path.join(cls.files, "sample.pdf"), "wb") as f:
            f.write(cls.pdf_bytes)
        cls.patches = [mock.patch.object(fetcher, "fetch_arxiv_meta", return_value=None),
                       mock.patch.object(agents, "_google_dict", side_effect=OSError("offline")),
                       mock.patch.object(agents, "_google", side_effect=agents.AgentError("offline"))]
        for p in cls.patches:
            p.start()
        cls.srv = make_server(cls.d, port=0)
        cls.srv.app.git.enabled = False   # API 测试里不跑 git
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        def handler(*a, **kw):
            return _QuietHandler(*a, directory=cls.files, **kw)
        cls.fsrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.fbase = "http://127.0.0.1:%d" % cls.fsrv.server_address[1]
        threading.Thread(target=cls.fsrv.serve_forever, daemon=True).start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.fsrv.shutdown()
        cls.fsrv.server_close()
        for p in cls.patches:
            p.stop()
        store.set_change_hook(None)
        cls.mocks.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def req(self, method, path, body=None, raw=False, expect=200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(r, timeout=30) as resp:
                status, payload, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
        self.assertEqual(status, expect, payload[:500])
        if raw:
            return payload, ctype
        return json.loads(payload.decode("utf-8"))

    def sse(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                   headers={"Content-Type": "application/json"})
        events = []
        with self.opener.open(r, timeout=60) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
            for line in resp:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    def test_full_flow(self):
        h = self.req("GET", "/api/health")
        self.assertTrue(h["ok"] and h["claude"]["found"] and h["codex"]["found"])
        self.assertEqual(self.req("GET", "/api/projects")[0]["id"], "inbox")
        p = self.req("POST", "/api/projects", {"name": "Cognitive Core", "description": "认知核心"})
        pid = p["id"]
        note = self.req("POST", "/api/projects/%s/items" % pid, {"input": "**一个想法**", "type": "note"})
        self.assertEqual(note["type"], "note")
        link = self.req("POST", "/api/projects/%s/items" % pid, {"input": "https://example.invalid/blog"})
        self.assertEqual(link["type"], "web")
        paper = self.req("POST", "/api/projects/%s/items" % pid, {"input": "1706.03762"})
        self.assertEqual((paper["type"], paper["paper_key"]), ("paper", "arxiv-1706.03762"))
        dup = self.req("POST", "/api/projects/%s/items" % pid, {"input": "https://arxiv.org/abs/1706.03762v7"})
        self.assertTrue(dup["existing"])
        local = self.req("POST", "/api/projects/%s/items" % pid, {"input": self.fbase + "/sample.pdf",
                                                                  "title": "本地样例"})
        self.assertEqual((local["type"], local["title"]), ("paper", "本地样例"))
        self.req("POST", "/api/projects/%s/items" % pid, {"input": "???"}, expect=400)
        proj = self.req("GET", "/api/projects/" + pid)
        self.assertEqual(len(proj["items"]), 4)

        # PDF 代理
        data, ctype = self.req("GET", "/api/pdf?url=" + urllib.request.quote(self.fbase + "/sample.pdf", safe=""),
                               raw=True)
        self.assertEqual(data, self.pdf_bytes)
        self.assertEqual(ctype, "application/pdf")
        self.req("GET", "/api/pdf?url=" + urllib.request.quote(self.fbase + "/missing.pdf", safe=""), expect=502)
        self.req("GET", "/api/pdf?url=file:///etc/passwd", expect=502)

        iid = local["id"]
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertFalse(st["text_cached"])
        self.req("PUT", "/api/papers/%s/%s/text" % (pid, iid), {"pages": [{"page": 1, "text": "Attention test"}]})
        self.assertTrue(self.req("GET", "/api/papers/%s/%s" % (pid, iid))["text_cached"])
        md_path = os.path.join(self.d, "projects", pid, "papers", local["paper_key"] + ".md")
        md_mtime = os.path.getmtime(md_path)
        time.sleep(0.05)
        r = self.req("PATCH", "/api/papers/%s/%s" % (pid, iid), {"last_page": 3, "zoom": 1.5,
                                                                 "tts_pos": {"page": 2, "sentence": 7}})
        self.assertEqual(r["tts_pos"], {"page": 2, "sentence": 7})
        self.assertEqual(os.path.getmtime(md_path), md_mtime)   # 高频字段不重渲染 md
        self.req("PATCH", "/api/papers/%s/%s" % (pid, iid), {"tts_pos": {"page": "x"}}, expect=400)
        self.req("POST", "/api/current", {"pid": pid, "iid": iid})

        ann = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid),
                       {"kind": "highlight", "color": "green", "page": 1, "quote": "Attention",
                        "rects": [[0.1, 0.1, 0.2, 0.03]]})
        self.req("PATCH", "/api/papers/%s/%s/annotations/%s" % (pid, iid, ann["id"]), {"comment": "**批注**"})
        n = self.req("POST", "/api/papers/%s/%s/notes" % (pid, iid),
                     {"text": "<ins>下划线</ins> 和 **加粗**", "page": 1, "quote": "Attention"})
        self.req("PATCH", "/api/papers/%s/%s/notes/%s" % (pid, iid, n["id"]), {"text": "<ins>下划线</ins> **加粗**"})

        # 问答（SSE）
        evs = self.sse("/api/papers/%s/%s/ask" % (pid, iid), {"question": "这篇讲什么？", "quote": "Attention",
                                                              "page": 1, "engine": "claude"})
        types = [e["type"] for e in evs]
        self.assertEqual(types[0], "start")
        self.assertIn("delta", types)
        self.assertEqual(types[-1], "done", evs)
        self.assertIn("files=True", evs[-1]["qa"]["answer"])
        self.assertEqual(evs[-1]["qa"]["style"], "text")
        evs = self.sse("/api/papers/%s/%s/ask" % (pid, iid), {"question": "继续", "engine": "codex", "style": "voice"})
        self.assertEqual(evs[-1]["type"], "done")
        self.assertEqual(evs[-1]["qa"]["answer"], "Codex 回答：有全文")
        self.assertEqual(evs[-1]["qa"]["style"], "voice")
        self.assertIn("这是语音对话", self.mocks.calls()[-1]["args"][-1])
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertEqual(st["sessions"], {"claude": "sess-123", "codex": "thread-abc"})
        self.assertEqual((st["last_page"], st["zoom"], st["tts_pos"]), (3, 1.5, {"page": 2, "sentence": 7}))
        self.assertEqual(len(st["qa"]), 2)
        self.req("POST", "/api/papers/%s/%s/reset_session" % (pid, iid), {"engine": "claude"})
        self.assertIsNone(self.req("GET", "/api/papers/%s/%s" % (pid, iid))["sessions"]["claude"])
        self.req("DELETE", "/api/papers/%s/%s/qa/%s" % (pid, iid, st["qa"][1]["id"]))

        # 翻译
        tr = self.req("POST", "/api/translate", {"text": "Hello", "engine": "claude", "pid": pid, "iid": iid,
                                                 "page": 1})
        self.assertEqual(tr["translation"], "【译】Hello")
        self.req("POST", "/api/translate", {"text": "Hello", "engine": "google"}, expect=502)
        self.assertEqual(len(self.req("GET", "/api/papers/%s/%s" % (pid, iid))["translations"]), 1)

        # 生词：google 离线 → 仍保存，lookup_error 有值
        va = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid),
                      {"kind": "vocab", "page": 1, "quote": "salient", "rects": [[0, 0, 0.1, 0.01]]})
        v = self.req("POST", "/api/papers/%s/%s/vocab" % (pid, iid),
                     {"word": "salient", "context": "the salient part", "page": 1, "annotation_id": va["id"]})
        self.assertFalse(v["merged"])
        self.assertTrue(v["lookup_error"])
        v2 = self.req("POST", "/api/papers/%s/%s/vocab" % (pid, iid),
                      {"word": "Salient", "context": "a second salient example", "page": 2})
        self.assertTrue(v2["merged"])
        vc = self.req("POST", "/api/papers/%s/%s/vocab" % (pid, iid),
                      {"word": "attention", "context": "attention is all you need", "page": 1, "engine": "claude"})
        self.assertEqual((vc["meaning"], vc["lookup_error"]), ("注意；关注", None))
        allv = self.req("GET", "/api/vocab?mastered=0")
        self.assertEqual({x["word"] for x in allv}, {"salient", "attention"})
        self.assertEqual(allv[0]["pid"], pid)
        self.req("PATCH", "/api/papers/%s/%s/vocab/%s" % (pid, iid, vc["id"]), {"mastered": True})
        self.assertEqual(len(self.req("GET", "/api/vocab?mastered=1")), 1)
        self.req("DELETE", "/api/papers/%s/%s/vocab/%s" % (pid, iid, v["id"]))
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertNotIn(va["id"], [a["id"] for a in st["annotations"]])

        # 归档（SSE）
        evs = self.sse("/api/papers/%s/%s/archive" % (pid, iid), {"engine": "claude"})
        self.assertEqual(evs[-1]["type"], "done", evs)
        self.assertEqual(evs[-1]["md_path"], "projects/%s/papers/%s.md" % (pid, local["paper_key"]))
        md, ctype = self.req("GET", "/api/papers/%s/%s/markdown" % (pid, iid), raw=True)
        md = md.decode("utf-8")
        self.assertIn("text/markdown", ctype)
        for s in ("## 🧾 总结", "<ins>下划线</ins> **加粗**", "## 🔤 生词", "🟩 高亮", "**批注**", "/r/%s/%s" % (pid, iid)):
            self.assertIn(s, md)
        items = {x["id"]: x for x in self.req("GET", "/api/projects/" + pid)["items"]}
        self.assertEqual(items[iid]["status"], "done")

        # 移动 / 删除
        mv = self.req("POST", "/api/projects/%s/items/%s/move" % (pid, iid), {"to": "inbox"})
        self.assertEqual(mv["pid"], "inbox")
        self.assertEqual(self.req("GET", "/api/papers/inbox/%s" % iid)["annotations"][0]["id"], ann["id"])
        self.req("PATCH", "/api/projects/%s/items/%s" % (pid, link["id"]), {"tags": "blog, idea", "title": "博客"})
        self.req("DELETE", "/api/projects/%s/items/%s" % (pid, note["id"]))
        self.req("PATCH", "/api/projects/" + pid, {"description": "新描述"})

        # 设置
        s = self.req("PUT", "/api/settings", {"translate_engine": "claude", "vocab_engine": "codex", "bogus": 1})
        self.assertEqual((s["translate_engine"], s["vocab_engine"]), ("claude", "codex"))
        self.assertNotIn("bogus", s)
        self.req("PUT", "/api/settings", {"translate_engine": "google", "vocab_engine": "google"})

        # 同步状态（git 已禁用）
        sst = self.req("GET", "/api/sync/status")
        self.assertFalse(sst["enabled"])
        self.assertIn("pages_url", sst)
        self.req("POST", "/api/sync/enable_pages", {}, expect=400)
        ex = self.req("POST", "/api/sync/export", {})
        self.assertIn("copied", ex)
        self.assertTrue(os.path.exists(os.path.join(self.d, "library.json")))

        # 静态 / SPA / 错误
        html, ctype = self.req("GET", "/", raw=True)
        self.assertIn("text/html", ctype)
        self.req("GET", "/r/%s/%s" % (pid, iid), raw=True)
        self.req("GET", "/vocab", raw=True)
        self.req("GET", "/api/nope", expect=404)
        self.req("GET", "/api/projects/nope", expect=404)
        self.req("GET", "/static/../server.py", expect=404)
        self.req("DELETE", "/api/projects/inbox", expect=400)
        self.req("POST", "/api/projects", {"name": ""}, expect=400)

    def test_ask_errors(self):
        p = self.req("POST", "/api/projects", {"name": "Errs"})
        it = self.req("POST", "/api/projects/%s/items" % p["id"], {"input": self.fbase + "/sample.pdf"})
        self.req("POST", "/api/papers/%s/%s/ask" % (p["id"], it["id"]), {"question": ""}, expect=400)
        old = os.environ["PAPERDESK_CLAUDE"]
        os.environ["PAPERDESK_CLAUDE"] = "/nonexistent"
        try:
            with mock.patch.object(config, "find_cli", return_value=None):
                evs = self.sse("/api/papers/%s/%s/ask" % (p["id"], it["id"]), {"question": "x", "engine": "claude"})
        finally:
            os.environ["PAPERDESK_CLAUDE"] = old
        self.assertEqual(evs[-1]["type"], "error")
        self.assertIn("claude", evs[-1]["message"])
        note = self.req("POST", "/api/projects/%s/items" % p["id"], {"type": "note", "note": "x"})
        self.req("GET", "/api/papers/%s/%s" % (p["id"], note["id"]), expect=400)


    def test_web_and_resources(self):
        """SPEC §13：网页条目、/api/web、资源增删改、按资源缓存全文、多 URL 输入。"""
        with open(os.path.join(self.files, "post.html"), "w", encoding="utf-8") as f:
            f.write(BLOG_HTML)
        p = self.req("POST", "/api/projects", {"name": "Web"})
        pid = p["id"]
        blog_url = self.fbase + "/post.html"
        web = self.req("GET", "/api/web?url=" + urllib.parse.quote(blog_url, safe=""))
        self.assertEqual(web["mode"], "article")
        self.assertIn("Attention, intuitively", web["title"])
        self.assertIn('src="%s/images/heads.png"' % self.fbase, web["html"])
        self.assertNotIn("Subscribe to my newsletter", web["html"])
        self.assertFalse(web["weak"])
        self.req("GET", "/api/web?url=" + urllib.parse.quote("ftp://x/y", safe=""), expect=502)
        self.req("GET", "/api/web", expect=400)

        # 原网页模式：完整 HTML（含导航等正文以外的部分），最终地址用于 <base>
        page = self.req("GET", "/api/webpage?url=" + urllib.parse.quote(blog_url, safe=""))
        self.assertEqual(page["url"], blog_url)
        self.assertIn("Subscribe to my newsletter", page["html"])
        self.assertIn("Attention, intuitively", page["html"])
        self.req("GET", "/api/webpage?url=" + urllib.parse.quote("ftp://x/y", safe=""), expect=502)
        self.req("GET", "/api/webpage", expect=400)

        # 网页条目：type web，有论文状态，可以缓存全文、标注（锚点）
        it = self.req("POST", "/api/projects/%s/items" % pid, {"input": blog_url})
        self.assertEqual(it["type"], "web")
        self.assertTrue(it["paper_key"].startswith("web-"))
        self.assertEqual(it["title"], "Attention, intuitively")
        iid = it["id"]
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertEqual(st["meta"]["type"], "web")
        self.assertFalse(st["text_cached"])
        r = self.req("PUT", "/api/papers/%s/%s/text" % (pid, iid), {"text": web["text"]})
        self.assertGreater(r["chars"], 200)
        ann = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid), {
            "kind": "highlight", "color": "green", "quote": "softmax over scores",
            "anchor": {"exact": "softmax over scores", "prefix": "a ", "suffix": " gives", "start": 120, "end": 139}})
        self.assertIsNone(ann["page"])
        self.assertEqual((ann["resource_id"], ann["anchor"]["start"]), ("main", 120))
        self.assertNotIn("space", ann["anchor"])
        # 原网页模式的锚点带 space="page"；其他值丢弃
        ann2 = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid), {
            "kind": "highlight", "quote": "softmax over scores",
            "anchor": {"exact": "softmax over scores", "start": 900, "end": 919, "space": "page"}})
        self.assertEqual(ann2["anchor"]["space"], "page")
        ann3 = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid), {
            "kind": "highlight", "quote": "softmax over scores",
            "anchor": {"exact": "softmax over scores", "space": "evil"}})
        self.assertNotIn("space", ann3["anchor"])
        for a in (ann2, ann3):
            self.req("DELETE", "/api/papers/%s/%s/annotations/%s" % (pid, iid, a["id"]))

        # 关联资源：代码（离线 → 仍然保存）、重复链接返回 existing
        with mock.patch.object(fetcher, "fetch_github_readme", side_effect=fetcher.FetchError("offline")):
            code = self.req("POST", "/api/projects/%s/items/%s/resources" % (pid, iid),
                            {"url": "https://github.com/foo/attn"})
        self.assertEqual((code["kind"], code["existing"]), ("code", False))
        again = self.req("POST", "/api/projects/%s/items/%s/resources" % (pid, iid), {"url": "https://github.com/foo/attn/"})
        self.assertTrue(again["existing"])
        same_main = self.req("POST", "/api/projects/%s/items/%s/resources" % (pid, iid), {"url": blog_url})
        self.assertEqual(same_main["id"], "main")
        rid = code["id"]
        upd = self.req("PATCH", "/api/projects/%s/items/%s/resources/%s" % (pid, iid, rid), {"title": "foo/attn"})
        self.assertEqual(upd["title"], "foo/attn")
        self.req("PATCH", "/api/projects/%s/items/%s/resources/%s" % (pid, iid, rid), {"kind": "bogus"}, expect=400)
        self.req("PUT", "/api/papers/%s/%s/text" % (pid, iid), {"resource_id": rid, "pages": [{"page": 1, "text": "# attn\nREADME 正文"}]})
        self.req("PUT", "/api/papers/%s/%s/text" % (pid, iid), {"resource_id": "r_nope", "text": "x"}, expect=404)
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertEqual(st["texts_cached"], ["main", rid])
        self.assertEqual([r["id"] for r in st["meta"]["resources"]], ["main", rid])
        a2 = self.req("POST", "/api/papers/%s/%s/annotations" % (pid, iid), {
            "kind": "underline", "quote": "README 正文", "resource_id": rid, "anchor": {"exact": "README 正文"}})
        n2 = self.req("POST", "/api/papers/%s/%s/notes" % (pid, iid), {
            "text": "代码里的 **实现**", "quote": "README 正文", "resource_id": rid, "anchor": {"exact": "README 正文"}})
        self.assertEqual(n2["resource_id"], rid)

        # 问答带资源：提示词里说明出处，agent 的 paper.txt 拼上了 README
        evs = self.sse("/api/papers/%s/%s/ask" % (pid, iid), {"question": "这段代码做什么？", "quote": "README 正文",
                                                               "resource_id": rid, "engine": "claude"})
        self.assertEqual(evs[-1]["type"], "done")
        self.assertEqual(evs[-1]["qa"]["resource_id"], rid)
        paper_txt = _read_text(os.path.join(self.d, ".agent", pid, it["paper_key"], "paper.txt"))
        self.assertIn("===== [网页：Attention, intuitively] =====", paper_txt)
        self.assertIn("===== [代码 README：foo/attn] =====", paper_txt)

        md = self.req("GET", "/api/papers/%s/%s/markdown" % (pid, iid), raw=True)[0].decode("utf-8")
        self.assertIn("**相关资源**：", md)
        self.assertIn("💻 [代码](https://github.com/foo/attn)", md)
        self.assertIn("### 💻 代码：foo/attn", md)
        readme = _read_text(os.path.join(self.d, "projects", pid, "README.md"))
        self.assertIn("## 🌐 网页与链接", readme)
        self.assertIn("(https://github.com/foo/attn", readme)

        # 删除资源：它的标注被删掉，笔记保留但去掉锚点
        self.req("DELETE", "/api/projects/%s/items/%s/resources/%s" % (pid, iid, rid))
        st = self.req("GET", "/api/papers/%s/%s" % (pid, iid))
        self.assertNotIn(a2["id"], [a["id"] for a in st["annotations"]])
        self.assertIn(ann["id"], [a["id"] for a in st["annotations"]])
        n2b = [n for n in st["notes"] if n["id"] == n2["id"]][0]
        self.assertEqual(n2b["resource_id"], "main")
        self.assertNotIn("anchor", n2b)
        self.assertEqual(st["texts_cached"], ["main"])
        self.req("DELETE", "/api/projects/%s/items/%s/resources/%s" % (pid, iid, rid), expect=404)

        # 一次粘贴多个链接 → 一个条目 + 资源
        multi = "%s\nhttps://github.com/foo/bar  %s" % (self.fbase + "/sample.pdf", blog_url)
        with mock.patch.object(fetcher, "fetch_github_readme", side_effect=fetcher.FetchError("offline")):
            item = self.req("POST", "/api/projects/%s/items" % pid, {"input": multi})
        self.assertEqual(item["type"], "paper")
        self.assertEqual([(r["kind"], r["url"]) for r in item["resources"]],
                         [("code", "https://github.com/foo/bar"), ("homepage", blog_url)])
        self.assertEqual(item["resources"][1]["title"], "Attention, intuitively")

        # 旧数据里的 link 条目也能打开阅读状态（自动补 paper_key）
        store.add_item(self.d, pid, {"type": "link", "url": "https://old.example/post", "title": "旧链接"})
        proj = store.get_project(self.d, pid)
        old = [x for x in proj["items"] if x.get("url") == "https://old.example/post"][0]
        st = self.req("GET", "/api/papers/%s/%s" % (pid, old["id"]))
        self.assertTrue(st["paper_key"].startswith("web-"))



# =============================================================================================
BLOG_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Attention, intuitively | Kai's Notes</title>
<meta property="og:site_name" content="Kai's Notes">
<meta property="og:title" content="Attention, intuitively">
<meta name="author" content="Kai">
<meta property="article:published_time" content="2026-03-01T10:00:00Z">
<meta name="description" content="A gentle walk through scaled dot-product attention.">
</head><body>
<header class="site-header"><a href="/">Kai's Notes</a><nav><a href="/about">About</a><a href="/tags">Tags</a></nav></header>
<div class="layout">
 <aside class="sidebar"><h3>Recent posts</h3><ul><li><a href="/p/1">Post one</a></li><li><a href="/p/2">Post two</a></li></ul></aside>
 <main><article class="post">
  <h1>Attention, intuitively</h1>
  <div class="share-buttons"><a href="https://twitter.com/share">Tweet</a> <a href="#">Share on LinkedIn</a></div>
  <div class="post-content">
   <p>Attention lets every token look at every other token. A query is compared with all keys, and a softmax over scores gives the weights, which are used to average the values. This is the core of the Transformer.</p>
   <p>The scaled version divides by <script type="math/tex">\\sqrt{d_k}</script> so that the softmax does not saturate when the dimension grows, which keeps gradients healthy during training.</p>
   <script type="math/tex; mode=display">\\mathrm{Attn}(Q,K,V)=\\mathrm{softmax}(QK^\\top/\\sqrt{d_k})V</script>
   <p>KaTeX also works: <span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>x</mi></mrow><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">x2</span></span> is rendered inline.</p>
   <figure><img src="data:image/gif;base64,R0lGOD" data-src="images/heads.png" alt="attention heads"><figcaption>Figure 1: heads attend to different things.</figcaption></figure>
   <img src="/static/plot.png" alt="plot">
   <pre><code class="language-python">def attn(q, k, v):
    w = softmax(q @ k.T / d ** 0.5)
    return w @ v</code></pre>
   <p>Read the <a href="../papers/attention.html">paper notes</a> for details.</p>
  </div>
  <section class="comments"><h3>Leave a comment</h3><p>Great post! Thanks for sharing this with everyone.</p></section>
 </article></main>
</div>
<div class="newsletter-signup"><p>Subscribe to my newsletter for weekly posts about machine learning and more.</p></div>
<footer><p>© 2026 Kai. All rights reserved.</p></footer>
<script>console.log("analytics")</script>
</body></html>
"""

NO_SEMANTIC_HTML = """<html><head><title>Plain page</title></head><body>
<div id="top"><a href="/a">Home</a> | <a href="/b">Archive</a> | <a href="/c">Links</a> | <a href="/d">Contact</a></div>
<div id="wrap">
 <div id="x1"><p>The first real paragraph of this essay explains, in some detail, why simple baselines matter, and why they are so often skipped.</p>
 <p>The second paragraph continues the argument, adds a few more sentences, and keeps going for a while so it is clearly the main text.</p>
 <p>A third paragraph, with commas, clauses, and examples, makes the content block obviously the densest part of the page.</p></div>
 <div id="x2"><p><a href="/1">Related link one</a> <a href="/2">Related link two</a> <a href="/3">Related link three</a></p></div>
</div></body></html>"""


class TestWebExtract(unittest.TestCase):
    def test_blog_with_noise(self):
        r = fetcher.extract_article(BLOG_HTML, "https://kai.example/posts/attention/")
        self.assertEqual(r["title"], "Attention, intuitively")
        self.assertEqual(r["site_name"], "Kai's Notes")
        self.assertEqual(r["byline"], "Kai")
        self.assertTrue(r["published"].startswith("2026-03-01"))
        self.assertEqual(r["lang"], "en")
        h = r["html"]
        for junk in ("Recent posts", "Tweet", "Leave a comment", "Subscribe to my newsletter", "All rights reserved",
                     "analytics", "About"):
            self.assertNotIn(junk, h, junk)
        self.assertIn("softmax over scores", h)
        # 懒加载 + 相对图片 → 绝对地址；占位 data: 图不出现
        self.assertIn('<img src="https://kai.example/posts/attention/images/heads.png" alt="attention heads">', h)
        self.assertIn('<img src="https://kai.example/static/plot.png" alt="plot">', h)
        self.assertNotIn("data:image", h)
        self.assertIn("<figcaption>Figure 1", h)
        self.assertIn('<a href="https://kai.example/posts/papers/attention.html">paper notes</a>', h)
        # 公式
        self.assertIn("$\\sqrt{d_k}$", h)
        self.assertIn("$$\\mathrm{Attn}(Q,K,V)", h)
        self.assertIn("$x^2$", h)
        self.assertNotIn("katex-html", h)
        # 代码块保留换行与语言
        self.assertIn('<pre><code class="language-python">def attn(q, k, v):\n    w = softmax', h)
        # 纯文本：段落之间空行
        self.assertIn("This is the core of the Transformer.\n\nThe scaled version", r["text"])
        self.assertGreater(r["word_count"], 60)
        self.assertFalse(r["weak"])
        for tag in ("script", "style", "div", "span", "nav", "aside"):
            self.assertNotIn("<%s" % tag, h)

    def test_gbk_chinese_page(self):
        page = ("<html><head><meta http-equiv='Content-Type' content='text/html; charset=gb2312'>"
                "<title>注意力机制详解 - 某某博客</title></head><body>"
                "<div class='nav'><a href='/'>首页</a><a href='/about'>关于</a></div>"
                "<div class='entry-content'><p>注意力机制让模型在处理每个词的时候，都能看到句子里所有其他的词，"
                "并根据相关程度分配不同的权重。这是 Transformer 的核心思想。</p>"
                "<p>缩放点积注意力把点积除以维度的平方根，避免 softmax 饱和，从而让训练更稳定。"
                "这一段继续补充更多说明文字，确保正文足够长。</p>"
                "<p>多头注意力把表示切成多份，在不同子空间里分别计算注意力，最后再拼接起来。</p></div>"
                "<div class='footer'>版权所有</div></body></html>")
        data = page.encode("gbk")
        text = fetcher.decode_html(data, "text/html")
        self.assertIn("注意力机制让模型", text)
        r = fetcher.extract_article(text, "https://blog.example.cn/a/1.html")
        self.assertEqual(r["title"], "注意力机制详解 - 某某博客")
        self.assertIn("缩放点积注意力", r["text"])
        self.assertNotIn("首页", r["text"])
        self.assertNotIn("版权所有", r["text"])
        self.assertGreater(r["word_count"], 80)
        # HTTP 头里的编码优先；BOM 最优先
        self.assertEqual(fetcher.decode_html("中文".encode("gbk"), "text/html; charset=GBK"), "中文")
        self.assertEqual(fetcher.decode_html(b"\xef\xbb\xbf" + "中文".encode("utf-8"), "text/html; charset=gbk"), "中文")

    def test_scoring_fallback_and_weak(self):
        r = fetcher.extract_article(NO_SEMANTIC_HTML, "https://plain.example/essay")
        self.assertIn("simple baselines matter", r["text"])
        self.assertNotIn("Related link one", r["text"])
        self.assertNotIn("Archive", r["text"])
        weak = fetcher.extract_article("<html><body><p>Too short.</p></body></html>", "https://x.example/")
        self.assertTrue(weak["weak"])
        self.assertEqual(fetcher.extract_article("", "https://x.example/")["text"], "")

    def test_mathml_and_mathjax3(self):
        h = ('<html><body><article class="post"><p>' + "word " * 60 + '</p>'
             '<p>Inline <math alttext="a+b"><mi>a</mi></math> and <mjx-container><svg></svg></mjx-container>.</p>'
             '<p><span class="MathJax_Preview">x</span><span class="MathJax">rendered</span>'
             '<script type="math/tex">y_i</script> done.</p></article></body></html>')
        r = fetcher.extract_article(h, "https://m.example/")
        self.assertIn("$a+b$", r["html"])
        self.assertIn("[公式]", r["html"])
        self.assertIn("$y_i$", r["html"])
        self.assertNotIn("rendered", r["html"])


class TestResourcesDiscovery(unittest.TestCase):
    def test_classify(self):
        cases = {
            "https://arxiv.org/abs/1706.03762": "paper", "https://openreview.net/forum?id=x": "paper",
            "https://example.org/a/paper.pdf": "paper", "https://huggingface.co/papers/2406.1": "paper",
            "https://github.com/foo/bar": "code", "https://github.com/foo/bar/tree/main/src": "code",
            "https://gitlab.com/foo/bar": "code", "https://bitbucket.org/foo/bar": "code",
            "https://codeberg.org/foo/bar": "code", "https://github.com/foo": "homepage",
            "https://www.youtube.com/watch?v=1": "video", "https://youtu.be/1": "video",
            "https://www.bilibili.com/video/BV1": "video", "https://vimeo.com/1": "video",
            "https://huggingface.co/datasets/a/b": "dataset", "https://huggingface.co/spaces/a/b": "demo",
            "https://huggingface.co/meta-llama/Llama-3": "model",
            "https://speakerdeck.com/a/b": "slides", "https://www.slideshare.net/a/b": "slides",
            "https://docs.google.com/presentation/d/1/edit": "slides", "https://x.org/talk.pptx": "slides",
            "https://medium.com/@a/post-1": "blog", "https://a.substack.com/p/hello": "blog",
            "https://distill.pub/2016/augmented-rnns/": "blog", "https://blog.example.com/x/y": "blog",
            "https://lilianweng.github.io/posts/2023-06-23-agent/": "blog",
            "https://nerf.github.io/": "homepage", "https://example.com/project": "homepage",
            "https://example.com/a/b/c": "blog",
        }
        for url, kind in cases.items():
            self.assertEqual(fetcher.classify(url), kind, url)
        self.assertEqual(fetcher.github_repo_of("https://github.com/foo/bar.git"), ("foo", "bar"))
        self.assertIsNone(fetcher.github_repo_of("https://github.com/features/copilot"))
        self.assertEqual(fetcher.split_inputs("1706.03762\nhttps://github.com/a/b, https://a.github.io/"),
                         ["1706.03762", "https://github.com/a/b", "https://a.github.io/"])
        self.assertEqual(fetcher.split_inputs("https://a.example/ some words"), ["https://a.example/ some words"])

    def test_arxiv_auto_discovery(self):
        atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><title>Looped Transformers</title><author><name>A. Author</name></author>
<summary>We propose X. Code is available at https://github.com/foo/looped. Models: https://huggingface.co/foo/looped-1b.
We compare with https://arxiv.org/abs/1706.03762.</summary>
<published>2024-06-01T00:00:00Z</published>
<arxiv:comment>NeurIPS 2024. Project page: https://foo.example.org/looped (with demo videos); see also github.com/foo/looped-extra</arxiv:comment>
</entry></feed>"""
        with mock.patch.object(fetcher, "_http_get", return_value=(atom, "application/atom+xml")):
            r = fetcher.resolve("2406.00001")
        self.assertEqual(r["title"], "Looped Transformers")
        got = [(x["kind"], x["url"], x["auto"]) for x in r["resources"]]
        self.assertIn(("code", "https://github.com/foo/looped", True), got)
        self.assertIn(("model", "https://huggingface.co/foo/looped-1b", True), got)
        self.assertIn(("homepage", "https://foo.example.org/looped", True), got)
        self.assertIn(("code", "https://github.com/foo/looped-extra", True), got)
        self.assertFalse(any("arxiv.org" in u for _, u, _ in got))

    def test_readme_auto_discovery(self):
        info = {"title": "foo/looped", "description": "Official code", "homepage": "https://foo.github.io/looped/",
                "markdown": "# Looped\n[![arXiv](badge)](https://arxiv.org/abs/2406.00001v2)\nSee also arxiv.org/abs/1706.03762"}
        with mock.patch.object(fetcher, "fetch_github_readme", return_value=info):
            r = fetcher.resolve("https://github.com/foo/looped")
        self.assertEqual((r["type"], r["kind"], r["title"]), ("web", "code", "foo/looped"))
        self.assertEqual(r["description"], "Official code")
        papers = [x for x in r["resources"] if x["kind"] == "paper"]
        self.assertEqual(len(papers), 1)
        self.assertEqual((papers[0]["arxiv_id"], papers[0]["pdf_url"]), ("2406.00001", "https://arxiv.org/pdf/2406.00001"))
        self.assertIn(("homepage", "https://foo.github.io/looped/"), [(x["kind"], x["url"]) for x in r["resources"]])

    def test_store_resources_markdown(self):
        d = tempfile.mkdtemp(prefix="pd-res-")
        try:
            store.ensure_library(d)
            it = store.add_item(d, "inbox", {
                "type": "paper", "title": "Looped Transformers", "url": "https://arxiv.org/abs/2406.00001",
                "pdf_url": "https://arxiv.org/pdf/2406.00001", "arxiv_id": "2406.00001",
                "resources": [{"kind": "code", "url": "https://github.com/foo/looped", "auto": True},
                              {"url": "https://arxiv.org/abs/2406.00001"},           # 与主链接相同 → 丢弃
                              {"url": "https://github.com/foo/looped/"},             # 重复 → 丢弃
                              {"url": "https://kai.example/posts/looped/", "title": "图解 Looped"}]})
            self.assertEqual([r["kind"] for r in it["resources"]], ["code", "blog"])
            blog_rid = it["resources"][1]["id"]
            store.add_annotation(d, "inbox", it["id"], {"kind": "highlight", "page": 3, "quote": "PDF 里的一句"})
            store.add_annotation(d, "inbox", it["id"], {"kind": "squiggly", "quote": "博客里的一句", "resource_id": blog_rid,
                                                        "anchor": {"exact": "博客里的一句", "start": 5, "end": 11}})
            store.add_note(d, "inbox", it["id"], {"text": "博客的 **解释** 更直观", "quote": "博客里的一句",
                                                  "resource_id": blog_rid, "anchor": {"exact": "博客里的一句"}})
            store.add_note(d, "inbox", it["id"], {"text": "PDF 笔记", "page": 3})
            store.write_cached_text(d, "inbox", it["paper_key"], [{"page": 1, "text": "PDF 第一页"}])
            store.write_cached_text(d, "inbox", it["paper_key"], [{"page": 1, "text": "博客正文"}], resource_id=blog_rid)
            txt = store.agent_text_string(d, "inbox", store.find_item(d, "inbox", it["id"]))
            self.assertIn("===== [p.1] =====\nPDF 第一页", txt)
            self.assertIn("===== [网页：图解 Looped] =====\n博客正文", txt)
            md = store.paper_markdown(d, "inbox", it["id"])
            self.assertIn("**相关资源**：📄 [论文](https://arxiv.org/abs/2406.00001) · 💻 [代码](https://github.com/foo/looped)"
                          " · 📝 [博客：图解 Looped](https://kai.example/posts/looped/)", md)
            self.assertIn("### 📄 论文", md)
            self.assertIn("### 📝 博客：图解 Looped", md)
            self.assertIn("**📝 博客：图解 Looped**", md)
            self.assertIn("- 🟨 波浪线：<ins>*“博客里的一句”*</ins>", md)
            self.assertIn("- **p.3** 🟨 高亮：“PDF 里的一句”", md)
            self.assertLess(md.index("PDF 笔记"), md.index("博客的 **解释** 更直观"))
            readme = _read_text(os.path.join(d, "projects", "inbox", "README.md"))
            self.assertIn("[💻](https://github.com/foo/looped", readme)
            # 删除博客资源：它的标注没了，笔记留下且不再有锚点，缓存删除
            store.delete_resource(d, "inbox", it["id"], blog_rid)
            st = store.get_paper_state(d, "inbox", it["id"])
            self.assertEqual([a["quote"] for a in st["annotations"]], ["PDF 里的一句"])
            moved = [n for n in st["notes"] if "博客" in n["text"]][0]
            self.assertEqual(moved["resource_id"], "main")
            self.assertNotIn("anchor", moved)
            self.assertEqual(store.cached_resources(d, "inbox", it["paper_key"]), ["main"])
            md = store.paper_markdown(d, "inbox", it["id"])
            self.assertIn("原关联资源「图解 Looped」已移除", md)
            # 移动条目时资源与缓存跟着走
            store.create_project(d, "Dst")
            store.add_resource(d, "inbox", it["id"], {"url": "https://youtu.be/abc", "title": "talk"})
            vid = [r for r in store.find_item(d, "inbox", it["id"])["resources"] if r["kind"] == "video"][0]
            store.write_cached_text(d, "inbox", it["paper_key"], [{"page": 1, "text": "字幕"}], resource_id=vid["id"])
            store.move_item(d, "inbox", it["id"], "dst")
            self.assertEqual(store.cached_resources(d, "dst", it["paper_key"]), ["main", vid["id"]])
            self.assertEqual(len(store.find_item(d, "dst", it["id"])["resources"]), 2)
            with self.assertRaises(ValueError):
                store.add_resource(d, "dst", it["id"], {"url": ""})
            note_item = store.add_item(d, "dst", {"type": "note", "note": "随手记"})
            with self.assertRaises(ValueError):
                store.add_resource(d, "dst", note_item["id"], {"url": "https://x.example/"})
            counts = [p for p in store.list_projects(d) if p["id"] == "dst"][0]["counts"]
            self.assertEqual(counts, {"paper": 1, "web": 0, "link": 0, "note": 1})
        finally:
            shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    unittest.main(verbosity=2)
