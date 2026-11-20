#!/usr/bin/env python3
"""Tests for mcp_server.py: builds a temp data dir in the SPEC §2/§3 format and
drives the server over stdio: initialize -> tools/list -> every tool.

Run: python3 tests/test_mcp.py           (file-fallback write path)
     PAPERDESK_TEST_WITH_STORE=1 python3 tests/test_mcp.py   (use paperdesk.store if importable)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp_server.py")


def make_data_dir():
    d = tempfile.mkdtemp(prefix="pd-mcp-")
    pid = "ml-reading"
    key = "arxiv-1706.03762"
    pdir = os.path.join(d, "projects", pid)
    os.makedirs(os.path.join(pdir, "papers"))
    os.makedirs(os.path.join(d, "projects", "inbox", "papers"))
    item = {"id": "a1b2c3d4", "type": "paper", "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762", "pdf_url": "https://arxiv.org/pdf/1706.03762",
            "arxiv_id": "1706.03762", "authors": ["Vaswani"], "year": "2017", "abstract": "...",
            "paper_key": key, "note": "", "tags": ["transformer"], "status": "reading",
            "added": "2026-11-19T10:00:00-06:00", "updated": "2026-11-19T10:00:00-06:00"}
    link = {"id": "l0000001", "type": "link", "title": "Blog", "url": "https://example.com", "tags": [],
            "status": "unread", "added": "2026-11-19T10:00:00-06:00", "updated": "2026-11-19T10:00:00-06:00"}
    note = {"id": "n0000001", "type": "note", "title": "想法", "note": "**looped** transformer?", "tags": [],
            "status": "unread", "added": "2026-11-19T10:00:00-06:00", "updated": "2026-11-19T10:00:00-06:00"}
    proj = {"id": pid, "name": "ML 阅读", "description": "test", "created": "2026-11-19", "updated": "2026-11-19",
            "items": [item, link, note]}
    with open(os.path.join(pdir, "project.json"), "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False)
    with open(os.path.join(d, "projects", "inbox", "project.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "inbox", "name": "收件箱", "description": "", "items": []}, f, ensure_ascii=False)
    state = {"pid": pid, "iid": "a1b2c3d4", "paper_key": key, "meta": {"title": item["title"]},
             "annotations": [{"id": "h_1", "kind": "highlight", "color": "yellow", "page": 4,
                              "quote": "dot products grow large", "rects": [[0.1, 0.2, 0.3, 0.02]]}],
             "notes": [{"id": "n_1", "page": 4, "quote": "scaled", "text": "**缩放**很重要", "source": "manual"}],
             "qa": [{"id": "q_1", "question": "为什么除以 sqrt(d_k)?", "answer": "防止 softmax 饱和", "engine": "claude"}],
             "translations": [], "summary": None, "sessions": {"claude": None, "codex": None},
             "vocab": [{"id": "v_1", "word": "magnitude", "lemma": "magnitude", "phonetic": "/ˈmæɡnɪtuːd/",
                        "pos": "n.", "meaning": "量级", "context": "dot products grow large in magnitude", "page": 4,
                        "mastered": False, "created": "2026-11-19T11:00:00"},
                       {"id": "v_2", "word": "softmax", "lemma": "softmax", "meaning": "归一化指数函数",
                        "page": 4, "mastered": True, "created": "2026-11-19T10:00:00"}],
             "last_page": 4, "zoom": 1.25, "status": "reading"}
    with open(os.path.join(pdir, "papers", key + ".json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.makedirs(os.path.join(d, ".cache", "text", pid))
    with open(os.path.join(d, ".cache", "text", pid, key + ".json"), "w", encoding="utf-8") as f:
        json.dump({"pages": [{"page": i, "text": f"page {i} text"} for i in range(1, 6)]}, f)
    with open(os.path.join(d, ".current.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "iid": "a1b2c3d4", "title": item["title"], "at": "2026-11-19T11:00:00"}, f)
    return d, pid, key


class Client:
    def __init__(self, data_dir, use_store):
        env = dict(os.environ, PAPERDESK_HOME=data_dir)
        if not use_store:
            env["PAPERDESK_MCP_NO_STORE"] = "1"
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
        self.n = 0

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("server died: " + self.p.stderr.read())
        return json.loads(line)

    def notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def call(self, name, args=None):
        r = self.request("tools/call", {"name": name, "arguments": args or {}})
        res = r["result"]
        return res["content"][0]["text"], res.get("isError", False)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)
        self.p.stdout.close()
        self.p.stderr.close()


class MCPTest(unittest.TestCase):
    def setUp(self):
        self.data, self.pid, self.key = make_data_dir()
        self.use_store = bool(os.environ.get("PAPERDESK_TEST_WITH_STORE"))
        self.c = Client(self.data, self.use_store)

    def tearDown(self):
        self.c.close()

    def test_full_flow(self):
        r = self.c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "paperdesk")
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.c.notify("notifications/initialized")
        self.assertEqual(self.c.request("ping")["result"], {})
        tools = {t["name"] for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertEqual(tools, {"list_projects", "list_items", "get_current_paper", "get_paper_notes",
                                 "get_paper_text", "add_note", "add_item", "list_vocab"})

        txt, err = self.c.call("list_projects")
        self.assertFalse(err)
        data = json.loads(txt)
        names = {p["name"] for p in data["projects"]}
        self.assertIn("ML 阅读", names)
        ml = [p for p in data["projects"] if p["id"] == self.pid][0]
        self.assertEqual(ml["counts"], {"paper": 1, "link": 1, "note": 1})

        txt, err = self.c.call("list_items", {"project": "ML 阅读"})
        self.assertFalse(err)
        self.assertEqual(len(json.loads(txt)["items"]), 3)

        txt, err = self.c.call("get_current_paper")
        self.assertFalse(err, txt)
        cur = json.loads(txt)
        self.assertEqual(cur["item"]["arxiv_id"], "1706.03762")
        self.assertTrue(cur["text_cached"])
        self.assertEqual(cur["notes_count"], 1)

        txt, err = self.c.call("get_paper_notes", {"project": self.pid, "item": "1706.03762"})
        self.assertFalse(err)
        self.assertIn("**缩放**很重要", txt)
        self.assertIn("防止 softmax 饱和", txt)

        txt, err = self.c.call("get_paper_text", {"project": self.pid, "item": "Attention", "pages": "2-3"})
        self.assertFalse(err, txt)
        self.assertIn("[p.2]", txt)
        self.assertIn("page 3 text", txt)
        self.assertNotIn("page 4 text", txt)

        txt, err = self.c.call("add_note", {"project": self.pid, "item": "a1b2c3d4",
                                            "text": "来自终端的 <ins>笔记</ins>", "page": 2, "quote": "q"})
        self.assertFalse(err, txt)
        st = json.load(open(os.path.join(self.data, "projects", self.pid, "papers", self.key + ".json"),
                            encoding="utf-8"))
        self.assertTrue(any("来自终端" in n["text"] for n in st["notes"]))

        txt, err = self.c.call("list_vocab")
        self.assertFalse(err, txt)
        v = json.loads(txt)
        self.assertEqual(v["count"], 2)
        self.assertEqual(v["vocab"][0]["word"], "magnitude")
        self.assertEqual(v["vocab"][0]["paper_title"], "Attention Is All You Need")
        txt, _ = self.c.call("list_vocab", {"project": "ML 阅读", "mastered": False})
        self.assertEqual([e["word"] for e in json.loads(txt)["vocab"]], ["magnitude"])

        if not self.use_store:
            txt, err = self.c.call("add_item", {"project": "inbox", "input": "2406.12345v2"})
            self.assertFalse(err, txt)
            it = json.loads(txt)["item"]
            self.assertEqual(it["paper_key"], "arxiv-2406.12345")
            self.assertEqual(it["pdf_url"], "https://arxiv.org/pdf/2406.12345")
            txt, _ = self.c.call("add_item", {"project": "inbox", "input": "arxiv.org/abs/2406.12345"})
            self.assertTrue(json.loads(txt).get("existing"))
            txt, err = self.c.call("add_item", {"project": "inbox", "input": "https://example.com/blog"})
            self.assertEqual(json.loads(txt)["item"]["type"], "link")

        # error paths
        txt, err = self.c.call("list_items", {"project": "不存在"})
        self.assertTrue(err)
        self.assertIn("找不到项目", txt)
        r = self.c.request("tools/call", {"name": "nope", "arguments": {}})
        self.assertIn("error", r)
        r = self.c.request("unknown/method")
        self.assertEqual(r["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
