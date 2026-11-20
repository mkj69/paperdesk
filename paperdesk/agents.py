"""调用本机 claude / codex CLI（流式），以及翻译（google / claude / codex）。

公共接口：
    cli_status(settings) -> {"claude": {...}, "codex": {...}}
    prepare_workdir(data_dir, pid, state, settings) -> str
    build_ask_prompt(question, quote=None, page=None, mode="ask", style="text", answer_lang="中文") -> str
    build_archive_prompt(state) -> str
    run_agent(engine, prompt, workdir, settings, session_id=None) -> generator
        产出事件：{"type":"status","text"} / {"type":"delta","text"} /
                  {"type":"final","text","session_id"}；失败抛 AgentError
    translate(text, engine, target, settings) -> str
    lookup_word(word, context, engine, settings) -> dict
        返回 {"phonetic","pos","meaning","context_meaning","context_translation","engine","lookup_error"}；
        查询失败不抛异常，而是把原因写进 lookup_error。
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from . import config, store

AGENT_TIMEOUT = 600
TRANSLATE_TIMEOUT = 180

LANG_NAMES = {
    "zh-CN": "简体中文", "zh": "简体中文", "zh-TW": "繁體中文", "en": "English", "ja": "日本語",
    "ko": "한국어", "fr": "Français", "de": "Deutsch", "es": "Español",
}


class AgentError(Exception):
    pass


def cli_status(settings):
    out = {}
    for name in ("claude", "codex"):
        p = config.find_cli(name, settings)
        out[name] = {"found": bool(p), "path": p}
    return out


# ----------------------------------------------------------------- 工作目录
def _instructions(settings, has_text):
    lang = settings.get("answer_lang") or "中文"
    src = ("论文全文在 ./paper.txt：PDF 每页以 `===== [p.N] =====` 分隔；如果这篇工作还关联了博客、项目主页或代码仓库，"
           "它们的正文 / README 也在里面，以 `===== [网页：标题] =====`、`===== [代码 README：仓库] =====` 开头。"
           "需要细节时用 Read / Grep 查找相关段落再回答。"
           if has_text else
           "注意：./paper.txt 里暂时只有论文元数据和摘要（全文还没抽取完成）。如果问题需要细节，请说明你只看到了摘要。")
    return """# PaperDesk 论文阅读助手

你正在 PaperDesk 里陪用户精读一篇论文。

- {src}
- 用户自己的笔记、标注和以前的问答在 ./notes.md，可以参考它理解用户关心什么。
- 回答使用{lang}；专业术语第一次出现时在括号里保留英文。
- 引用或依据原文时注明出处：PDF 写页码，例如（p.4）；网页或 README 写来源，例如（博客）（README）。
- 数学公式用 LaTeX：行内 `$...$`，独立公式 `$$...$$`。
- 用 Markdown 组织回答：该加粗的关键结论要加粗，必要时用列表或小标题；不要冗长的开场白。
- 只读：不要创建、修改或删除任何文件，不要运行命令。
""".format(src=src, lang=lang)


def prepare_workdir(data_dir, pid, state, settings):
    key = state["paper_key"]
    wd = os.path.join(data_dir, ".agent", pid, key)
    os.makedirs(wd, exist_ok=True)
    try:
        item = store.find_item(data_dir, pid, state["iid"])
        text = store.agent_text_string(data_dir, pid, item)
    except Exception:
        item = None
        text = store.cached_text_string(data_dir, pid, key)
    meta = state.get("meta") or {}
    if text:
        header = "标题：%s\n作者：%s\n链接：%s\n" % (meta.get("title") or "", ", ".join(meta.get("authors") or []),
                                                meta.get("url") or "")
        config.atomic_write_text(os.path.join(wd, "paper.txt"), header + text)
    else:
        config.atomic_write_text(os.path.join(wd, "paper.txt"),
                                 "标题：%s\n作者：%s\n链接：%s\n\n摘要：\n%s\n\n（全文尚未抽取）\n" % (
                                     meta.get("title") or "", ", ".join(meta.get("authors") or []),
                                     meta.get("url") or "", meta.get("abstract") or "（无）"))
    project_name = ""
    try:
        project_name = store.get_project(data_dir, pid).get("name", "")
    except Exception:
        pass
    md = store.render_paper_markdown(state, project_name, settings.get("app_url"))
    config.atomic_write_text(os.path.join(wd, "notes.md"), md)
    ins = _instructions(settings, bool(text))
    config.atomic_write_text(os.path.join(wd, "CLAUDE.md"), ins)
    config.atomic_write_text(os.path.join(wd, "AGENTS.md"), ins)
    return wd


_RES_NAMES = {"paper": "论文 PDF", "code": "代码仓库 README", "homepage": "项目主页", "blog": "博客", "video": "视频",
              "slides": "幻灯片", "dataset": "数据集页面", "model": "模型页面", "demo": "Demo 页面", "other": "网页"}


def resource_label(res):
    """资源的中文称呼，例如「博客《标题》」，用于提示词里说明引文出处。"""
    name = _RES_NAMES.get((res or {}).get("kind") or "other", "网页")
    title = ((res or {}).get("title") or "").strip()
    if title and title != (res or {}).get("url"):
        return "%s《%s》" % (name, title[:80])
    return name


def build_ask_prompt(question, quote=None, page=None, mode="ask", style="text", answer_lang="中文",
                     source_label=None):
    q = (question or "").strip()
    parts = []
    if quote:
        if source_label:
            parts.append("我在%s中选中了这段原文：\n\n\"\"\"\n%s\n\"\"\"\n" % (source_label, quote.strip()))
        else:
            loc = "（第 %s 页）" % page if page else ""
            parts.append("我在论文中选中了这段原文%s：\n\n\"\"\"\n%s\n\"\"\"\n" % (loc, quote.strip()))
    if mode == "explain":
        parts.append("请结合论文上下文，用通俗但准确的方式解释这段话：它在说什么、为什么这么做、涉及的符号/公式分别是什么意思。"
                     + ("\n\n补充要求：" + q if q else ""))
    else:
        parts.append(q or "请解释这段内容。")
    if style == "voice":
        parts.append("\n（这是语音对话：请用口语化的%s回答，2–6 句，先说结论；不要用表格、代码块、标题；公式用口语描述。）"
                     % (answer_lang or "中文"))
    return "\n".join(parts)


def build_archive_prompt(state):
    return """我已经读完这篇论文了。请阅读 ./paper.txt（论文全文；如果这篇工作还关联了博客、项目主页或代码仓库，它们的正文 / README 也在里面）和 ./notes.md（我的笔记、标注和问答），
为我写一份可以长期保存的中文精读总结（Markdown）。要求：

1. 使用下面这些二级以下的小标题（用 ###），不要输出一级/二级标题：
   ### 一句话总结
   ### 研究问题与动机
   ### 方法要点
   ### 关键结果
   ### 我的笔记要点（整合我写过的笔记和标注，保留我的原意，注明页码）
   ### 我问过的问题（每个问题一行 + 答案要点）
   ### 局限与疑问
   ### 和我的研究可能的联系（只根据我的笔记推断；没有依据就写"暂无"）
2. 关键结论用 **加粗**；公式用 $...$；引用原文注明 (p.N)。
3. 直接输出总结正文，不要开场白，不要修改任何文件。
"""


# ----------------------------------------------------------------- 子进程流式读取
def _reader(stream, q, tag):
    try:
        for line in iter(stream.readline, ""):
            q.put((tag, line))
    except Exception:
        pass
    finally:
        q.put((tag, None))


class _Proc:
    def __init__(self, args, cwd, stdin_text=None, timeout=AGENT_TIMEOUT):
        self.args = args
        self.timeout = timeout
        try:
            self.p = subprocess.Popen(
                args, cwd=cwd, env=config.subprocess_env(),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as e:
            raise AgentError("无法启动 %s：%s" % (os.path.basename(args[0]), e))
        self.q = queue.Queue()
        self.stderr = []
        threading.Thread(target=_reader, args=(self.p.stdout, self.q, "out"), daemon=True).start()
        threading.Thread(target=_reader, args=(self.p.stderr, self.q, "err"), daemon=True).start()
        if stdin_text is not None:
            def feed():
                try:
                    self.p.stdin.write(stdin_text)
                    self.p.stdin.close()
                except Exception:
                    pass
            threading.Thread(target=feed, daemon=True).start()

    def lines(self):
        """逐行产出 stdout，直到两个流都结束；超时则 kill。"""
        deadline = time.time() + self.timeout
        open_streams = 2
        while open_streams:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.kill()
                raise AgentError("等待超时（%d 秒）" % self.timeout)
            try:
                tag, line = self.q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            if tag == "err":
                self.stderr.append(line)
                if len(self.stderr) > 200:
                    self.stderr = self.stderr[-200:]
                continue
            yield line
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.kill()

    @property
    def returncode(self):
        return self.p.returncode

    def kill(self):
        try:
            if self.p.poll() is None:
                self.p.kill()
                self.p.wait(timeout=5)
        except Exception:
            pass
        for f in (self.p.stdout, self.p.stderr, self.p.stdin):
            try:
                if f and not f.closed:
                    f.close()
            except Exception:
                pass

    def err_text(self):
        return "".join(self.stderr).strip()[-1500:]


# ----------------------------------------------------------------- Claude Code
def _claude_args(claude, settings, session_id):
    args = [claude, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--allowedTools", "Read,Grep,Glob", "--permission-mode", "default"]
    if settings.get("claude_model"):
        args += ["--model", settings["claude_model"]]
    if session_id:
        args += ["--resume", session_id]
    return args


_TOOL_STATUS = {"Read": "正在阅读论文…", "Grep": "正在检索原文…", "Glob": "正在查找文件…"}


def _run_claude_once(claude, prompt, workdir, settings, session_id, state):
    proc = _Proc(_claude_args(claude, settings, session_id), workdir, stdin_text=prompt)
    try:
        for raw in proc.lines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                if ev.get("session_id"):
                    state["session_id"] = ev["session_id"]
            elif t == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        state["text"].append(d["text"])
                        state["got_output"] = True
                        yield {"type": "delta", "text": d["text"]}
                elif e.get("type") == "content_block_start":
                    cb = e.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        state["got_output"] = True
                        yield {"type": "status", "text": _TOOL_STATUS.get(cb.get("name"), "正在使用工具 %s…" % cb.get("name"))}
                    elif cb.get("type") == "text" and state["text"] and not "".join(state["text"]).endswith("\n"):
                        # 新的文本块（工具调用之后），补一个换行
                        state["text"].append("\n\n")
                        yield {"type": "delta", "text": "\n\n"}
            elif t == "assistant":
                msg = ev.get("message") or {}
                for c in msg.get("content") or []:
                    if c.get("type") == "tool_use" and not state.get("partial_seen"):
                        yield {"type": "status", "text": _TOOL_STATUS.get(c.get("name"), "正在使用工具…")}
                state["got_output"] = True
            elif t == "result":
                state["got_output"] = True
                if ev.get("session_id"):
                    state["session_id"] = ev["session_id"]
                res = ev.get("result")
                if isinstance(res, str):
                    state["result"] = res
                if ev.get("is_error") or ev.get("subtype", "success") not in ("success",):
                    state["error"] = (res if isinstance(res, str) and res else ev.get("subtype")) or "Claude Code 返回错误"
    finally:
        proc.kill()
    state["returncode"] = proc.returncode
    state["stderr"] = proc.err_text()


def _run_claude(prompt, workdir, settings, session_id=None):
    claude = config.find_cli("claude", settings)
    if not claude:
        raise AgentError("没有找到 claude 命令。请先安装并登录 Claude Code（终端里能运行 `claude`），或在设置里填写 claude 的路径。")
    attempts = [session_id, None] if session_id else [None]
    for i, sid in enumerate(attempts):
        state = {"text": [], "result": None, "session_id": sid, "got_output": False, "error": None}
        for ev in _run_claude_once(claude, prompt, workdir, settings, sid, state):
            yield ev
        text = "".join(state["text"]).strip()
        # 续接失败（没有输出，或直接返回错误且没有任何正文）→ 开新会话重试一次
        if sid and i == 0 and not text and (not state["got_output"] or state.get("error")):
            yield {"type": "status", "text": "原会话无法续接，已开启新会话…"}
            continue
        if state.get("error") and not text:
            # 注意：出错时 result 字段里是错误信息，不能当成回答
            raise AgentError("Claude Code 出错：%s" % state["error"])
        if not text and state.get("result"):
            text = state["result"].strip()
            yield {"type": "delta", "text": text}
        if not text:
            msg = state.get("stderr") or "没有输出（退出码 %s）" % state.get("returncode")
            raise AgentError("Claude Code 没有返回内容：%s" % msg)
        yield {"type": "final", "text": text, "session_id": state.get("session_id")}
        return


# ----------------------------------------------------------------- Codex
def _codex_args(codex, settings, thread_id, out_file, prompt):
    if thread_id:
        # exec 的选项放在 resume 子命令之前：无论这些选项在 clap 里是否声明为 global 都能被识别
        args = [codex, "exec", "--json", "--skip-git-repo-check", "-o", out_file, "resume", thread_id]
    else:
        args = [codex, "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "-o", out_file]
        if settings.get("codex_model"):
            args += ["-m", settings["codex_model"]]
    args.append(prompt)
    return args


def _run_codex_once(codex, prompt, workdir, settings, thread_id, state):
    fd, out_file = tempfile.mkstemp(prefix="pd-codex-", suffix=".txt")
    os.close(fd)
    state["out_file"] = out_file
    proc = _Proc(_codex_args(codex, settings, thread_id, out_file, prompt), workdir)
    try:
        for raw in proc.lines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            state["got_output"] = True
            t = ev.get("type")
            if t == "thread.started" and ev.get("thread_id"):
                state["session_id"] = ev["thread_id"]
            elif t in ("item.completed", "item.updated", "item.started"):
                item = ev.get("item") or {}
                itype = item.get("type") or item.get("item_type")
                if t == "item.completed" and itype in ("agent_message", "assistant_message"):
                    txt = item.get("text") or ""
                    if txt:
                        if state["text"]:
                            state["text"].append("\n\n")
                            yield {"type": "delta", "text": "\n\n"}
                        state["text"].append(txt)
                        yield {"type": "delta", "text": txt}
                elif t == "item.started" and itype in ("command_execution", "mcp_tool_call", "file_search", "web_search"):
                    yield {"type": "status", "text": "Codex 正在查阅资料…"}
                elif itype == "reasoning" and t == "item.started":
                    yield {"type": "status", "text": "Codex 正在思考…"}
            elif t in ("turn.failed", "error"):
                err = ev.get("error") or {}
                state["error"] = (err.get("message") if isinstance(err, dict) else str(err)) or ev.get("message") or "Codex 出错"
            elif "msg" in ev and isinstance(ev["msg"], dict):
                m = ev["msg"]
                mt = m.get("type")
                if mt == "session_configured" and m.get("session_id"):
                    state["session_id"] = m["session_id"]
                elif mt == "agent_message_delta" and m.get("delta"):
                    state["text"].append(m["delta"])
                    state["legacy_delta"] = True
                    yield {"type": "delta", "text": m["delta"]}
                elif mt == "agent_message" and m.get("message") and not state.get("legacy_delta"):
                    state["text"].append(m["message"])
                    yield {"type": "delta", "text": m["message"]}
                elif mt == "error":
                    state["error"] = m.get("message") or "Codex 出错"
    finally:
        proc.kill()
    state["returncode"] = proc.returncode
    state["stderr"] = proc.err_text()


def _run_codex(prompt, workdir, settings, session_id=None):
    codex = config.find_cli("codex", settings)
    if not codex:
        raise AgentError("没有找到 codex 命令。请先安装并登录 Codex CLI（终端里能运行 `codex`），或在设置里填写 codex 的路径。")
    attempts = [session_id, None] if session_id else [None]
    for i, sid in enumerate(attempts):
        state = {"text": [], "session_id": sid, "got_output": False, "error": None}
        try:
            for ev in _run_codex_once(codex, prompt, workdir, settings, sid, state):
                yield ev
            text = "".join(state["text"]).strip()
            if not text:
                try:
                    with open(state["out_file"], "r", encoding="utf-8") as f:
                        text = f.read().strip()
                except OSError:
                    text = ""
                if text:
                    yield {"type": "delta", "text": text}
        finally:
            try:
                os.unlink(state.get("out_file", ""))
            except OSError:
                pass
        if sid and i == 0 and not text and (state["returncode"] or 0) != 0:
            yield {"type": "status", "text": "原会话无法续接，已开启新会话…"}
            continue
        if not text:
            msg = state.get("error") or state.get("stderr") or "没有输出（退出码 %s）" % state.get("returncode")
            raise AgentError("Codex 没有返回内容：%s" % msg)
        yield {"type": "final", "text": text, "session_id": state.get("session_id") or sid}
        return


def run_agent(engine, prompt, workdir, settings, session_id=None):
    if engine == "codex":
        return _run_codex(prompt, workdir, settings, session_id)
    if engine == "claude":
        return _run_claude(prompt, workdir, settings, session_id)
    raise AgentError("未知的引擎：%s" % engine)


# ----------------------------------------------------------------- 翻译
_CACHE = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 500


def _cache_key(engine, target, text):
    return hashlib.sha1(("%s|%s|%s" % (engine, target, text)).encode("utf-8")).hexdigest()


def _split_chunks(text, limit=1500):
    text = text.strip()
    if len(text) <= limit:
        return [text]
    sents = re.split(r"(?<=[\.\!\?。！？；;])\s+", text)
    chunks, cur = [], ""
    for s in sents:
        while len(s) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:limit])
            s = s[limit:]
        if len(cur) + len(s) + 1 > limit and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur)
    return chunks


def _google(text, target):
    out = []
    for chunk in _split_chunks(text):
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=%s&dt=t&q=%s"
               % (urllib.parse.quote(target or "zh-CN"), urllib.parse.quote(chunk)))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PaperDesk"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise AgentError("Google 翻译请求失败（%s）。可以在设置里把翻译引擎换成 Claude 或 Codex。" % e)
        segs = data[0] if isinstance(data, list) and data else []
        out.append("".join(s[0] for s in segs if isinstance(s, list) and s and isinstance(s[0], str)))
    return "\n".join(out).strip()


def _translate_prompt(text, target):
    lang = LANG_NAMES.get(target, target or "简体中文")
    return ("你是专业的学术论文翻译。把下面三引号里的内容翻译成%s。要求：准确、通顺、符合学术表达；"
            "专业术语第一次出现时在括号中保留英文；LaTeX 公式、变量名、引用编号原样保留；"
            "只输出译文本身，不要任何解释、前言或引号。\n\n\"\"\"\n%s\n\"\"\"" % (lang, text))


def _claude_translate(text, target, settings):
    claude = config.find_cli("claude", settings)
    if not claude:
        raise AgentError("没有找到 claude 命令，无法用 Claude 翻译。")
    args = [claude, "-p", "--output-format", "json", "--tools", ""]
    if settings.get("claude_translate_model"):
        args += ["--model", settings["claude_translate_model"]]
    with tempfile.TemporaryDirectory(prefix="pd-tr-") as td:
        try:
            r = subprocess.run(args, input=_translate_prompt(text, target), cwd=td, env=config.subprocess_env(),
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=TRANSLATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise AgentError("Claude 翻译超时")
        except OSError as e:
            raise AgentError("无法启动 claude：%s" % e)
    out = (r.stdout or "").strip()
    res = None
    for line in reversed(out.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            if obj.get("is_error"):
                raise AgentError("Claude 翻译出错：%s" % (obj.get("result") or ""))
            res = obj.get("result")
            break
    if res is None:
        try:
            obj = json.loads(out)
            res = obj.get("result") if isinstance(obj, dict) else None
        except ValueError:
            res = None
    if not res:
        raise AgentError("Claude 翻译没有返回内容：%s" % ((r.stderr or "").strip()[-500:] or "退出码 %s" % r.returncode))
    return res.strip()


def _codex_translate(text, target, settings):
    codex = config.find_cli("codex", settings)
    if not codex:
        raise AgentError("没有找到 codex 命令，无法用 Codex 翻译。")
    with tempfile.TemporaryDirectory(prefix="pd-tr-") as td:
        out_file = os.path.join(td, "out.txt")
        args = [codex, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", out_file]
        if settings.get("codex_model"):
            args += ["-m", settings["codex_model"]]
        args.append(_translate_prompt(text, target))
        try:
            r = subprocess.run(args, cwd=td, env=config.subprocess_env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TRANSLATE_TIMEOUT,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise AgentError("Codex 翻译超时")
        except OSError as e:
            raise AgentError("无法启动 codex：%s" % e)
        res = ""
        if os.path.exists(out_file):
            with open(out_file, "r", encoding="utf-8") as f:
                res = f.read().strip()
    if not res:
        raise AgentError("Codex 翻译没有返回内容：%s" % ((r.stderr or "").strip()[-500:] or "退出码 %s" % r.returncode))
    return res


def translate(text, engine, target, settings):
    text = (text or "").strip()
    if not text:
        raise AgentError("没有要翻译的文字")
    if len(text) > 20000:
        raise AgentError("文字太长（超过 20000 字符），请分段翻译")
    engine = engine or settings.get("translate_engine") or "google"
    target = target or settings.get("target_lang") or "zh-CN"
    key = _cache_key(engine, target, text)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    if engine == "google":
        res = _google(text, target)
    elif engine == "claude":
        res = _claude_translate(text, target, settings)
    elif engine == "codex":
        res = _codex_translate(text, target, settings)
    else:
        raise AgentError("未知的翻译引擎：%s" % engine)
    with _CACHE_LOCK:
        _CACHE[key] = res
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return res


# ----------------------------------------------------------------- 查词（生词本）
_POS_ABBR = {
    "noun": "n.", "verb": "v.", "adjective": "adj.", "adverb": "adv.", "preposition": "prep.",
    "conjunction": "conj.", "pronoun": "pron.", "interjection": "interj.", "abbreviation": "abbr.",
    "article": "art.", "prefix": "prefix", "suffix": "suffix", "phrase": "phr.", "numeral": "num.",
    "auxiliary verb": "aux.", "particle": "part.",
}


def _google_dict(word, target):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=%s&dt=t&dt=bd&q=%s"
           % (urllib.parse.quote(target or "zh-CN"), urllib.parse.quote(word)))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PaperDesk"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    trans = ""
    if isinstance(data, list) and data and isinstance(data[0], list):
        trans = "".join(s[0] for s in data[0] if isinstance(s, list) and s and isinstance(s[0], str)).strip()
    pos_list, lines = [], []
    bd = data[1] if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) else []
    for entry in bd:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        pos = _POS_ABBR.get(str(entry[0]).lower(), str(entry[0]) or "")
        words = [w for w in (entry[1] or []) if isinstance(w, str)][:5]
        if not words:
            continue
        if pos:
            pos_list.append(pos)
        lines.append(("%s %s" % (pos, "；".join(words))).strip())
    meaning = "\n".join(lines) if lines else trans
    return {"pos": " ".join(dict.fromkeys(pos_list)), "meaning": meaning}


def _phonetic(word):
    if not re.match(r"^[A-Za-z][A-Za-z\-']*$", word or ""):
        return ""
    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word.lower())
    req = urllib.request.Request(url, headers={"User-Agent": "PaperDesk"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, list):
        return ""
    for e in data:
        if isinstance(e, dict):
            if e.get("phonetic"):
                return e["phonetic"]
            for ph in e.get("phonetics") or []:
                if isinstance(ph, dict) and ph.get("text"):
                    return ph["text"]
    return ""


def _vocab_prompt(word, context, target):
    lang = LANG_NAMES.get(target, target or "简体中文")
    return ("你是英语词典与学术写作助手。请解释单词/短语「%s」，它出现在论文的这句话里：\n\"\"\"\n%s\n\"\"\"\n\n"
            "只输出一个 JSON 对象，不要代码块、不要任何其它文字，字段如下（除 phonetic 外用%s）：\n"
            '{"phonetic": "国际音标，如 /əˈtenʃn/（短语可留空）", "pos": "词性缩写，如 n. / v. / adj.", '
            '"meaning": "常用释义，多个用；分隔", "context_meaning": "在这句话里的具体含义", '
            '"context_translation": "整句的翻译"}' % (word, context or "（无原句）", lang))


def _extract_json(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            return None
    return None


def _cli_vocab(word, context, engine, target, settings):
    prompt = _vocab_prompt(word, context, target)
    # 复用翻译用的一次性调用（不保留会话），只是提示词不同
    if engine == "claude":
        claude = config.find_cli("claude", settings)
        if not claude:
            raise AgentError("没有找到 claude 命令")
        args = [claude, "-p", "--output-format", "json", "--tools", ""]
        if settings.get("claude_translate_model"):
            args += ["--model", settings["claude_translate_model"]]
        with tempfile.TemporaryDirectory(prefix="pd-vocab-") as td:
            r = subprocess.run(args, input=prompt, cwd=td, env=config.subprocess_env(), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=TRANSLATE_TIMEOUT)
        raw = None
        for line in reversed((r.stdout or "").strip().splitlines()):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                if obj.get("is_error"):
                    raise AgentError(obj.get("result") or "Claude 出错")
                raw = obj.get("result")
                break
        if raw is None:
            raise AgentError((r.stderr or "").strip()[-300:] or "Claude 没有返回内容")
    else:
        codex = config.find_cli("codex", settings)
        if not codex:
            raise AgentError("没有找到 codex 命令")
        with tempfile.TemporaryDirectory(prefix="pd-vocab-") as td:
            out_file = os.path.join(td, "out.txt")
            args = [codex, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", out_file]
            if settings.get("codex_model"):
                args += ["-m", settings["codex_model"]]
            args.append(prompt)
            r = subprocess.run(args, cwd=td, env=config.subprocess_env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TRANSLATE_TIMEOUT,
                               stdin=subprocess.DEVNULL)
            raw = ""
            if os.path.exists(out_file):
                with open(out_file, "r", encoding="utf-8") as f:
                    raw = f.read()
        if not raw.strip():
            raise AgentError((r.stderr or "").strip()[-300:] or "Codex 没有返回内容")
    obj = _extract_json(raw)
    if obj is None:
        return {"meaning": raw.strip()}
    return {k: (str(obj.get(k)).strip() if obj.get(k) is not None else "")
            for k in ("phonetic", "pos", "meaning", "context_meaning", "context_translation")}


def lookup_word(word, context, engine, settings):
    word = " ".join((word or "").split())
    engine = engine or settings.get("vocab_engine") or "google"
    target = settings.get("target_lang") or "zh-CN"
    res = {"phonetic": "", "pos": "", "meaning": "", "context_meaning": "", "context_translation": "",
           "engine": engine, "lookup_error": None}
    try:
        if engine == "google":
            d = _google_dict(word, target)
            res.update(d)
            res["phonetic"] = _phonetic(word)
            if context:
                try:
                    res["context_translation"] = translate(context, "google", target, settings)
                except Exception:
                    pass
        elif engine in ("claude", "codex"):
            res.update(_cli_vocab(word, context, engine, target, settings))
        else:
            raise AgentError("未知的查词引擎：%s" % engine)
    except subprocess.TimeoutExpired:
        res["lookup_error"] = "查词超时"
    except Exception as e:
        res["lookup_error"] = "查词失败：%s" % e
    if not res.get("meaning") and not res.get("lookup_error"):
        res["lookup_error"] = "没有查到释义"
    return res
