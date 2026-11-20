# PaperDesk — 规格说明（三路并行开发的唯一契约）

一个本地运行的论文阅读工作台。用户在 **macOS（arm64）** 上运行，**只用 Python 3 标准库**（不允许任何 pip 依赖），
前端为原生 ES modules（无构建步骤），第三方 JS 全部 vendor 到 `web/vendor/`（从 `/opt/npm-tools/node_modules/` 拷贝）。
UI 文案全部中文。

## 0. 核心需求（来自用户）
1. 打开论文**不下载 PDF**：输入 arXiv ID / arXiv 链接 / 任意 PDF 链接，后端代理流式转发到浏览器内存，pdf.js 渲染。**任何时候都不把 PDF 写进磁盘。**
2. 边读边翻译：划词翻译、翻译当前页（段落对照）。
3. 边问 AI：调用用户本机已登录的 `claude`（Claude Code CLI）或 `codex`（Codex CLI），每篇论文有独立、可续接的会话。
4. 随手笔记 + 标注：高亮（5 色）、**下划线**、**删除线**、波浪线；锚定批注（选中文字写笔记）；笔记编辑器支持 Markdown，带工具栏：**加粗、斜体、下划线（存为 `<ins>…</ins>`）、行内代码、公式 `$…$`、列表、引用**，可预览（KaTeX 渲染公式）。
5. 读完一键「归档」：Agent 把笔记 + 高亮 + 问答 + 论文全文整理成结构化中文总结，写入该论文的 Markdown 笔记文件。
6. **项目文件夹**：每个项目一个文件夹，可放：论文、普通链接、文字笔记；条目可在项目间移动。默认有「收件箱」项目。
7. **GitHub 同步**：整个文献库（数据目录）是一个 git 仓库，改动后自动 commit + push 到用户私有 GitHub 仓库。PDF 与抽取的全文不入库。
8. **每篇论文一个固定网页地址**：`http://localhost:8765/r/<pid>/<iid>`，打开直接进入阅读页；GitHub 上的 README / 笔记里也放这个链接。
9. **再次打开时**：PDF 与之前的高亮/下划线/批注在原位置准确还原，右侧笔记完整呈现（Markdown 渲染，加粗/下划线/公式都正确显示）。
10. （加分）MCP server：让用户在终端里的 Claude Code / Codex 也能读「当前在读的论文」和笔记、追加笔记。

## 1. 目录结构（代码）
```
paperdesk/
  server.py                 # 入口：python3 server.py [--port 8765] [--data DIR] [--no-browser]
  paperdesk/__init__.py     # VERSION = "0.1.0"
  paperdesk/config.py       # 数据目录、settings 读写、CLI 路径探测
  paperdesk/store.py        # 项目/条目/论文状态的读写 + Markdown 渲染 + README 索引生成
  paperdesk/fetcher.py      # arXiv 解析、元数据、PDF 流式代理
  paperdesk/agents.py       # claude / codex 桥接（流式）、翻译（google/claude/codex）
  paperdesk/gitsync.py      # git 自动同步（防抖）
  paperdesk/api.py          # HTTP 路由（ThreadingHTTPServer + BaseHTTPRequestHandler）
  mcp_server.py             # stdio MCP server（复用 paperdesk.store）
  web/index.html  web/app.js  web/styles.css  (可再拆 web/js/*.js)
  web/vendor/pdfjs/{pdf.min.mjs,pdf.worker.min.mjs,pdf_viewer.css,cmaps/,standard_fonts/}
  web/vendor/{marked.esm.js,purify.es.mjs,katex/(katex.min.js|katex.mjs, katex.min.css, fonts/)}
  PaperDesk.command         # macOS 双击启动
  start.sh                  # 通用启动
  README.md                 # 中文使用说明
  tests/mocks/claude  tests/mocks/codex   # 可执行的 Python 模拟 CLI
  tests/make_sample_pdf.py  tests/test_backend.py  tests/e2e_test.py
```

## 2. 数据目录（= GitHub 文献库仓库）
默认 `~/PaperDesk-Library`，可用 `--data` 或环境变量 `PAPERDESK_HOME` 覆盖。
```
<data>/
  .gitignore                # .cache/  .agent/  .trash/  settings.local.json  .current.json  .DS_Store
  settings.json             # 非敏感设置（入库）
  README.md                 # 自动生成：所有项目列表
  projects/
    <pid>/                  # pid = slug（小写字母数字和-，中文名则用拼音不可得 → 用 "p-" + 6位hex；同时保留 name）
      project.json          # {"id","name","description","created","updated","items":[item,...]}
      README.md             # 自动生成：项目索引（每个条目一行，含原文链接、「在 PaperDesk 中打开」链接、笔记链接、状态）
      papers/
        <paper_key>.json    # 论文状态（见 §3）
        <paper_key>.md      # 由 json 渲染的人类可读笔记（每次保存重写）
  .cache/text/<pid>/<paper_key>.json   # pdf.js 抽取的每页文字（不入库）
  .agent/<pid>/<paper_key>/            # agent 工作目录：paper.txt、notes.md、CLAUDE.md、AGENTS.md（不入库）
  .trash/                   # 删除的项目移到这里（不入库）
  .current.json             # 当前在读论文 {"pid","iid","title","at"}（不入库，MCP 用）
```
写文件一律「写临时文件再 os.replace」原子写；全局一个 `threading.RLock` 保护 store。

### item（project.json 中）
```json
{"id":"a1b2c3d4", "type":"paper|link|note", "title":"...", "url":"原始链接（abs 页或网页）",
 "pdf_url":"(paper) 实际 PDF 地址", "arxiv_id":"1706.03762"|null, "authors":["..."], "year":"2017"|null,
 "abstract":"..."|null, "paper_key":"arxiv-1706.03762"|"url-<sha1前10>", "note":"(note 类型) markdown 正文",
 "tags":["..."], "status":"unread|reading|done", "added":"ISO8601", "updated":"ISO8601"}
```
paper_key：arXiv → `arxiv-<id 去版本号, '/' 换成 '_'>`；其他 PDF → `url-<sha1(url)[:10]>`。

## 3. 论文状态 `<paper_key>.json`
```json
{"pid":"...","iid":"...","paper_key":"...","meta":{"title","authors","url","pdf_url","arxiv_id","year","abstract"},
 "annotations":[{"id":"h_xxxx","kind":"highlight|underline|strike|squiggly","color":"yellow|green|blue|pink|purple",
                 "page":4,"quote":"选中原文","rects":[[x,y,w,h],...],   // 相对页面宽高归一化到 0..1，左上角原点
                 "comment":"可选，markdown","created":"ISO"}],
 "notes":[{"id":"n_xxxx","page":4|null,"quote":"可选原文","text":"markdown 正文","source":"manual|qa|translation",
           "annotation_id":"可选，关联的标注","created":"ISO","updated":"ISO"}],
 "qa":[{"id":"q_xxxx","question":"...","answer":"markdown","engine":"claude|codex","mode":"ask|explain",
        "quote":"可选","page":4|null,"at":"ISO"}],
 "translations":[{"id":"t_xxxx","source":"原文","translation":"译文","engine":"...","page":4|null,"at":"ISO"}],  // 最多保留 200 条
 "summary":{"markdown":"...","engine":"...","at":"ISO"}|null,
 "sessions":{"claude":"session-id"|null,"codex":"thread-id"|null},
 "last_page":1, "zoom":1.25, "status":"unread|reading|done", "created":"ISO","updated":"ISO"}
```

### `<paper_key>.md` 渲染格式（GitHub 友好）
```
---
title: "..."
authors: ["..."]
arxiv: "1706.03762"
url: "https://arxiv.org/abs/1706.03762"
project: "项目名"
status: done
added: 2026-11-19
tags: [..]
---
# 论文标题

[📖 在 PaperDesk 中打开](http://localhost:8765/r/<pid>/<iid>) · [原文](url) · 作者…

## 总结           （有 summary 时）
...
## 笔记           （按页码排序；每条：> 引文（p.N） + 正文 markdown 原样保留，含 <ins>、**、$…$）
## 标注           （按页码：- p.4 🟨 高亮：“quote” — comment）
## 问答           （### Q: … / 回答 markdown / 引用 & 引擎 & 时间）
```
app_url 取 settings.app_url（默认 `http://localhost:8765`）。

## 4. HTTP API（全部 JSON，前缀 /api；错误返回 `{"error":"中文消息"}` + 4xx/5xx）
### 系统
- `GET /api/health` → `{"ok":true,"version","data_dir","claude":{"found":bool,"path"},"codex":{"found":bool,"path"},"git":{"found":bool},"gh":{"found":bool}}`
- `GET /api/settings` → settings；`PUT /api/settings` 局部更新 → 新 settings
  settings 字段及默认值：`{"default_engine":"claude","translate_engine":"google","target_lang":"zh-CN","answer_lang":"中文",
  "claude_model":"","claude_translate_model":"haiku","codex_model":"","claude_path":"","codex_path":"",
  "app_url":"http://localhost:8765","auto_sync":true,"sync_debounce_sec":30,"github_remote":""}`
- `POST /api/current` `{"pid","iid"}` → 写 .current.json（打开阅读页时前端调用）
- `POST /api/open_folder` `{"pid"?:..}` → macOS `open <path>`（无 pid 打开数据目录）

### 项目与条目
- `GET /api/projects` → `[{"id","name","description","created","updated","counts":{"paper":n,"link":n,"note":n}}]`（收件箱 id 固定 `inbox`，首次启动自动创建）
- `POST /api/projects` `{"name","description"?}` → project（含 items）
- `GET /api/projects/<pid>` → project（含 items）
- `PATCH /api/projects/<pid>` `{"name"?,"description"?}`；`DELETE /api/projects/<pid>`（移入 .trash；inbox 不可删）
- `GET /api/resolve?input=<文本>` → `{"type":"paper|link","title","url","pdf_url","arxiv_id","authors","year","abstract","paper_key"}`
  规则：arXiv ID（`2406.12345`、`2406.12345v2`、`hep-th/9901001`）或 arxiv.org/abs|pdf|html 链接 → paper，元数据走
  `https://export.arxiv.org/api/query?id_list=`（Atom XML，用 xml.etree 解析；失败则降级只给 id 当标题）；
  URL 以 .pdf 结尾或 HEAD/GET 的 Content-Type 为 application/pdf → paper；openreview.net/forum?id=X → pdf_url `https://openreview.net/pdf?id=X`；其他 → link（尝试抓 <title>，失败用 URL）。
- `POST /api/projects/<pid>/items` `{"input"?:原始文本,"type"?,"title"?,"url"?,"note"?,"tags"?}` → item
  （有 input 时先 resolve；type=note 时用 note/title。重复添加同一 paper_key 到同一项目时返回已有 item，`"existing":true`）
- `PATCH /api/projects/<pid>/items/<iid>` `{"title"?,"tags"?,"status"?,"note"?,"url"?}`
- `DELETE /api/projects/<pid>/items/<iid>`（paper 的 json/md 移入 .trash）
- `POST /api/projects/<pid>/items/<iid>/move` `{"to":"<pid2>"}` → 移动条目及其 papers 文件 → `{"item","pid":"<pid2>"}`

### PDF 代理（不落盘）
- `GET /api/pdf?url=<urlencoded>` → 以 64KB 块从上游读取并直接写给浏览器（Content-Type: application/pdf，带 Content-Length 若上游有），
  设置 User-Agent `PaperDesk/0.1`，跟随重定向，超时 60s；只允许 http/https。**不写磁盘、不缓存。**

### 论文
- `GET /api/papers/<pid>/<iid>` → 论文状态（不存在则按 item 初始化并保存）+ `"text_cached":bool`
- `PATCH /api/papers/<pid>/<iid>` `{"last_page"?,"zoom"?,"status"?}`（status 同步到 item）
- `PUT /api/papers/<pid>/<iid>/text` `{"pages":[{"page":1,"text":"..."}]}` → 写 .cache（不入库）→ `{"ok":true,"chars":n}`
- `POST /api/papers/<pid>/<iid>/annotations` `{"kind","color","page","quote","rects","comment"?}` → annotation
- `PATCH|DELETE /api/papers/<pid>/<iid>/annotations/<aid>`（PATCH 可改 color/kind/comment）
- `POST /api/papers/<pid>/<iid>/notes` `{"text","page"?,"quote"?,"source"?,"annotation_id"?}` → note
- `PATCH|DELETE /api/papers/<pid>/<iid>/notes/<nid>`
- `DELETE /api/papers/<pid>/<iid>/qa/<qid>`
- `POST /api/papers/<pid>/<iid>/reset_session` `{"engine":"claude|codex"}`
- `GET /api/papers/<pid>/<iid>/markdown` → text/markdown; charset=utf-8（渲染结果）
- 每次修改论文状态后：重写 json + md + 项目 README + 根 README，并调用 `gitsync.schedule(msg)`。

### 翻译与 AI（流式用 SSE 格式写在 POST 响应里：`Content-Type: text/event-stream`，每条 `data: <json>\n\n`）
- `POST /api/translate` `{"text","engine"?,"target"?,"pid"?,"iid"?,"page"?}` → 非流式 `{"translation","engine"}`；
  有 pid/iid 时把结果追加到论文 translations。内存 LRU 缓存（engine+target+text 哈希）。
  - google：`https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=<target>&dt=t&q=`，按句子切块（每块 ≤1500 字符），拼接。
  - claude：`claude -p --output-format json --model <claude_translate_model> --tools ""`，stdin 给提示词（专业学术翻译、保留 LaTeX/术语括注英文、只输出译文），取 result。
  - codex：`codex exec --skip-git-repo-check --sandbox read-only -o <tmp> "<提示词>"`，读 tmp。
- `POST /api/papers/<pid>/<iid>/ask` `{"question","quote"?,"page"?,"engine"?,"mode"?:"ask|explain"}` → SSE：
  `{"type":"start","engine"}` → 多个 `{"type":"delta","text":"..."}` → `{"type":"done","qa":{...}}` | `{"type":"error","message"}`。
  可选 `{"type":"status","text":"正在读取论文…"}`（工具调用时）。完成后保存 qa 与 session id。
- `POST /api/papers/<pid>/<iid>/archive` `{"engine"?}` → SSE 同上，done 为 `{"type":"done","summary":{...},"md_path":"projects/<pid>/papers/<key>.md"}`；
  保存 summary，status=done，触发同步。

### Agent 桥接细节（agents.py）
- 工作目录 `.agent/<pid>/<paper_key>/`，每次调用前写入：
  `paper.txt`（来自 .cache，每页以 `\n\n===== [p.N] =====\n` 分隔；无缓存则写元数据+摘要并在提示中说明）、
  `notes.md`（当前渲染的笔记 md）、`CLAUDE.md` 与 `AGENTS.md`（相同内容的系统说明：你是论文阅读助手；论文全文在 ./paper.txt，
  用户笔记在 ./notes.md；回答用 <answer_lang>；引用原文时注明页码 (p.N)；公式用 $…$；不要修改任何文件）。
- claude：`[claude,"-p","--output-format","stream-json","--verbose","--include-partial-messages","--allowedTools","Read,Grep,Glob","--permission-mode","default"] + (["--model",m] if m) + (["--resume",sid] if sid)`，
  提问正文走 stdin。解析：`type=system,subtype=init` 取 session_id；`type=stream_event` 且 `event.type=content_block_delta` 且 `delta.type=text_delta` → delta；
  `type=assistant` 中 tool_use → status；`type=result` → 最终 result 文本与 session_id（若没收到任何 delta，就把 result 作为一次 delta 发出）。
  若带 --resume 失败（非零退出且无输出），去掉 resume 重试一次。
- codex：首次 `[codex,"exec","--json","--skip-git-repo-check","--sandbox","read-only","-o",tmp] + (["-m",m] if m) + [prompt]`；
  续接 `[codex,"exec","resume",thread_id,"--json","--skip-git-repo-check","-o",tmp,prompt]`；subprocess cwd=工作目录。
  解析 JSONL：`thread.started` 取 `thread_id`；`item.completed` 且 item.type 为 `agent_message` → 文本（作为 delta）；
  兼容旧格式 `{"msg":{"type":"agent_message_delta","delta"}}` / `{"msg":{"type":"agent_message","message"}}` / `session_configured.session_id`；
  非 JSON 行忽略；结束后若没有文本则读 `-o` 文件。续接失败则去掉 resume 重试一次。
- CLI 路径探测：settings 中的 `claude_path`/`codex_path` 优先；否则 `shutil.which`，再查
  `~/.local/bin ~/.claude/local /opt/homebrew/bin /usr/local/bin ~/.npm-global/bin ~/.bun/bin ~/.nvm/versions/node/*/bin`。
  环境变量 `PAPERDESK_CLAUDE` / `PAPERDESK_CODEX` 最高优先（测试用 mocks）。子进程 env 里把以上目录补进 PATH。
- 超时 600s；进程句柄登记，客户端断开（写失败）时 kill 子进程。

### Git 同步（gitsync.py）
- 启动时：数据目录不是 git 仓库 → `git init -b main`、写 .gitignore、首次 commit。git 不存在则同步功能禁用（状态里说明）。
- `schedule(msg)`：防抖 `sync_debounce_sec` 秒后在后台线程执行：`git add -A`；有变更则 `git commit -m "PaperDesk: <msg 合并>"`；
  若有 remote origin：`git pull --rebase --autostash`（失败记录错误不中断）然后 `git push -u origin HEAD`。
- `GET /api/sync/status` → `{"enabled":bool,"repo":bool,"remote":"url|null","branch","dirty":bool,"ahead":n,"last_sync":"ISO|null","last_error":"...|null","gh":bool,"pending":bool}`
- `POST /api/sync/now` → 立即同步，返回状态
- `POST /api/sync/setup` `{"remote_url":"git@github.com:u/r.git"}` 或 `{"create":"paper-library","private":true}`
  （后者用 `gh repo create <name> --private --source <data> --remote origin --push`）→ 状态。
- 所有 git 命令 `GIT_TERMINAL_PROMPT=0`，超时 60s。

### 静态与路由
- `/` 、`/p/<pid>`、`/r/<pid>/<iid>`、`/settings` → 都返回 `web/index.html`（前端用 History API 路由）。
- `/static/...` → `web/` 下文件（正确的 MIME：.mjs/.js → text/javascript，.css，.woff2，.svg，.json，.bcmap → application/octet-stream）。
  index.html 中引用路径一律以 `/static/` 开头。
- 只监听 127.0.0.1。

## 5. 前端（web/）
- 视图：
  1. **左侧栏**（常驻）：PaperDesk 标志、同步状态小圆点（绿=已同步/黄=待同步/红=错误/灰=未连接，点开同步面板）、项目列表（名称+条目数）、「新建项目」、底部「设置」。
  2. **项目页 `/p/<pid>`**：标题与描述（可编辑）、「在 Finder 中打开」、添加栏（一个输入框：粘贴 arXiv ID/链接、PDF 链接或任意网页；
     回车添加；旁边「写一条笔记」）。条目按类型分组：论文（卡片：标题、作者、年份、状态徽标、标签、按钮：阅读/复制阅读链接/移动/删除）、
     链接（点击新标签页打开）、笔记（Markdown 渲染，可编辑）。
  3. **阅读页 `/r/<pid>/<iid>`**：顶栏（← 返回项目、标题、引擎选择 Claude Code/Codex、翻译引擎选择、缩放 −/+、页码 N/M、复制本页链接、「读完，归档笔记」主按钮）。
     左：PDF（pdf.js 连续滚动，IntersectionObserver 懒渲染，每页一个容器：canvas + textLayer + annotationLayer；高 DPI 适配；
     记住 last_page/zoom）。右侧面板（~400px，可拖拽宽度）标签页：**笔记**（默认）｜**问答**｜**翻译**｜**总结**。
- **划词工具条**（在 textLayer 选中文字后浮现）：翻译｜解释｜问 Agent｜记一笔｜高亮（5 色点）｜下划线｜删除线｜波浪线。
  选区 rects 由 `range.getClientRects()` 相对页面容器换算成 0..1 归一化坐标，合并同一行相邻矩形。
- **标注渲染**：annotationLayer 内绝对定位 div；highlight=半透明色块（mix-blend-mode:multiply）；underline=底边 2px 线；
  strike=中线；squiggly=波浪底线（CSS background 或 SVG）。点击标注弹出小菜单：改颜色/类型、写批注、删除。有批注的标注右侧显示小气泡图标。
- **笔记标签**：按页码排序的卡片（引文 + 正文 Markdown 渲染 + 页码徽标，点击滚动到页并闪烁对应标注）；
  顶部「+ 新笔记」；编辑器 = textarea + 工具栏（B / I / U / `code` / ∑ / 列表 / 引用）+ 预览切换；Cmd/Ctrl+Enter 保存。
  底部：「查看 Markdown」「在 GitHub 上查看」（若 remote 为 github，链接到 `https://github.com/<owner>/<repo>/blob/main/projects/<pid>/papers/<key>.md`）。
- **问答标签**：历史对话（Markdown + KaTeX 渲染，代码块），输入框（可带引文 chip），流式显示；每条回答下「收进笔记」「复制」；「新会话」按钮（reset_session）。
- **翻译标签**：划词翻译历史（原文/译文对照，「收进笔记」）；「翻译当前页」按钮：取当前页文字按段落切分，逐段调用 /api/translate，对照显示。
- **总结标签**：summary Markdown 渲染；没有时显示说明 + 归档按钮。归档过程流式显示。
- Markdown 渲染：marked（GFM）+ DOMPurify（允许 ins/u/mark 标签）+ KaTeX（先把 `$$…$$`、`$…$` 抽出占位再渲染，避免 marked 破坏公式）。
- 加载时：GET 论文状态 → 加载 PDF（`/api/pdf?url=`）→ 若 `text_cached` 为 false，后台抽取全部页文字 PUT 上去 → POST /api/current。
- 视觉：干净、学术风；浅色为主并支持 `prefers-color-scheme: dark`；系统字体 + 中文字体回退；无外部 CDN 请求（全部本地 vendor）。
- 所有网络错误用右下角 toast 中文提示。

## 6. MCP server（mcp_server.py，stdio，JSON-RPC 2.0，按行分隔）
- 支持 `initialize`（protocolVersion 回显客户端版本或 "2025-06-18"）、`notifications/initialized`、`tools/list`、`tools/call`、`ping`。
- 工具：`list_projects`、`list_items {project}`、`get_current_paper`、`get_paper_notes {project,item}`（返回渲染的 md）、
  `get_paper_text {project,item,pages?:"3-5"}`（来自 .cache）、`add_note {project,item,text,page?,quote?}`、`add_item {project,input}`。
- 直接 import `paperdesk.store`；数据目录同 server（`PAPERDESK_HOME` 或默认）。写操作后也写 md/README（git 同步交给正在运行的 server 或下次启动）。
- README 中给出 `claude mcp add paperdesk -- python3 /path/to/mcp_server.py` 与 Codex 的 `~/.codex/config.toml` 配置示例。

## 7. 启动
- `PaperDesk.command`：`cd "$(dirname "$0")"`，补 PATH（同上目录列表 + `eval "$(/usr/libexec/path_helper -s)"`），若 8765 已在运行则直接 `open http://localhost:8765`，否则 `python3 server.py` 并自动打开浏览器。
- server.py 启动后打印地址，默认 `webbrowser.open`（`--no-browser` 关闭）。

## 8. 朗读（新增需求，纯前端）
- 用浏览器 Web Speech API（`speechSynthesis`，macOS 自带高质量英文/中文语音，免费离线）。
- 阅读页顶栏加「朗读」按钮 → 小播放条：朗读选区 / 从当前页开始朗读 / 暂停 / 继续 / 停止，语速 0.6–1.6，英文语音选择（优先 en-US 的 Samantha / Ava / Alex / 含 "Enhanced" "Premium" 的），中文语音选择（zh-CN）。
- 「从当前页朗读」：取当前页文字按句切分逐句朗读，播放条显示当前句；尽量在 textLayer 中给当前句所在 span 加高亮类；读完自动翻到下一页并滚动。
- 划词工具条加「朗读」按钮；翻译条目、问答回答、生词条目都带 🔊 小按钮（译文用中文语音）。
- 语速/语音选择存 localStorage。

## 9. 生词本（新增需求）
- 划词工具条加「生词」按钮（双击单词即选中单词；短语 ≤6 个词也可）。点击后：
  1) 创建一个 `kind:"vocab"` 的标注（紫色点状下划线），2) `POST /api/papers/<pid>/<iid>/vocab` `{"word","context","page","annotation_id"}`。
- 后端查词（settings 新字段 `"vocab_engine":"google"`，可选 google|claude|codex）：
  - google：translate_a/single 用 `dt=t&dt=bd`（bd 返回词性+候选释义，结构 `resp[1] = [[pos, [词...], ...], ...]`）得到中文释义与词性；
    音标 best-effort 查 `https://api.dictionaryapi.dev/api/v2/entries/en/<word>`（取 phonetic）；再把 context 原句翻译一次作为 `context_translation`。
  - claude/codex：提示词要求只输出 JSON：`{"phonetic","pos","meaning","context_meaning","context_translation"}`（中文），解析失败则把原文当 meaning。
  - 查询失败也要保存条目（meaning 为空，`lookup_error` 写原因）。同一论文重复的同一词（lemma 小写比较）→ 只追加 context，不重复建条目。
- 条目：`{"id":"v_xxxx","word","lemma","phonetic","pos","meaning","context_meaning","context","context_translation","page","annotation_id","created","mastered":false,"lookup_error":null}`；论文状态新增 `"vocab":[...]`。
- `PATCH /api/papers/<pid>/<iid>/vocab/<vid>` `{"mastered"?,"meaning"?,"context_meaning"?}`；`DELETE` 同路径（同时删除关联 vocab 标注）。
- `GET /api/vocab?mastered=0|1` → 全库所有生词 `[{...entry,"pid","iid","paper_title","paper_key"}]`，按 created 倒序。
- 论文 .md 新增「## 生词」表格：`| 单词 | 音标 | 词性 | 释义 | 原句 |`，原句中该词加粗并注明 (p.N)。
- 数据目录根新增 `vocabulary.md`（任何生词变动时重写）：全库生词按字母分组，每条含音标、释义、语境义、原句、来源论文链接（相对路径到 md + 「在 PaperDesk 中打开」链接）、已掌握的单独放最后；根 README 链接它。
- 前端：阅读页右侧新增标签 **生词**（本篇生词卡片：单词+音标+🔊、释义、原句（单词加粗）+页码跳转、已掌握勾选、删除）；
  左侧栏新增「生词本」入口 → 路由 `/vocab`：全库生词列表，搜索、只看未掌握、🔊、卡片复习模式（先显示单词，点击翻出释义，"认识/不认识"），每条可跳回论文阅读页对应页。
- MCP 新增工具 `list_vocab {project?, mastered?}`。

## 10. GitHub 仓库公开与否
- 用户表示文献库仓库可以是公开的。`POST /api/sync/setup {"create":name,"private":false}` 用 `--public`；前端同步面板提供「公开 / 私有」选项（默认公开）。

## 11. 语音模式（新增需求）：完整听论文 + 用语音和 Claude Code / Codex 对话
背景：Claude Code 的 /voice 只是终端里的按住空格语音输入；Codex CLI 的语音输入已在 0.118 移除，语音在桌面 App 里。它们的界面没法嵌进网页，
所以我们在网页里自己实现「语音输入 → 用户本机 claude/codex 作为大脑 → 语音播报」，效果等价且和论文上下文打通。

### 11.1 听整篇论文（Audiobook 模式，纯前端 + 一个状态字段）
- 阅读页顶栏「朗读」播放条增加「听整篇」：从第 1 页（或上次停下的位置）一直读到最后。
- 文本清洗（js 内）：合并 pdf.js 行内碎片与断行；去连字符断词（`trans-\nformer` → `transformer`）；去掉页眉页脚/孤立页码（各页重复的首尾短行）；
  去掉引用标记 `[12]`、`[3, 7]`、`(Vaswani et al., 2017)` 可选；公式密集行（符号占比高）用「（公式略）」代替；
  选项「跳过参考文献与附录」（遇到标题 References / Bibliography / 参考文献 后停止，默认开）。
- 逐句朗读（每句 ≤220 字符再切分，规避 Chrome 长句截断 bug），textLayer 当前句高亮 + 自动滚动跟随；进度显示「第 N/M 页 · 第 k 句」与进度条（可拖动跳转）；
  章节跳转：用 `pdf.getOutline()`，没有就用检测到的标题行（如 "1 Introduction"、"3.2 Method"）生成目录下拉。
- 「双语朗读」开关：每读完一句英文，用中文语音读它的译文（译文通过 /api/translate 以段落为单位预取、缓存）。
- 快捷键（阅读页、焦点不在输入框时）：K 播放/暂停（空格保留给滚动），J/L 上一句/下一句。
- Media Session API：`navigator.mediaSession` 设置标题/作者与 play/pause/previoustrack/nexttrack，可用 Mac 键盘媒体键与控制中心控制。
- 位置记忆：每读完一句节流保存 `PATCH /api/papers/<pid>/<iid>` `{"tts_pos":{"page":N,"sentence":k}}`，下次「从上次位置继续」。

### 11.2 语音对话模式（Voice mode）
- 问答标签输入框旁加 🎤 按钮 + 「语音模式」开关；阅读页右下角浮动一个语音球（语音模式开启时显示状态：聆听中 / 思考中 / 回答中）。
- 识别：Web Speech `SpeechRecognition || webkitSpeechRecognition`，语言可选 中文(zh-CN)/English(en-US)，`interimResults` 实时显示转写。
  触发：点击 🎤 切换；或按住 ⌥ Option 键说话、松开发送（push-to-talk，与 Claude Code 的按住说话一致）。
- 识别到最终文本后，先匹配本地语音指令（中英都支持几种说法）：
  「朗读/读一下/开始读」→ 从当前位置朗读；「暂停/停」→ 暂停；「继续」→ 继续；「下一页/上一页」；「翻译这句/翻译这段」→ 翻译当前朗读句或当前选区并读出译文；
  「记下来 …/记一下 …/note …」→ 把后面内容作为笔记存到当前页（引用当前句）；「生词 xxx/加入生词 xxx」→ 加生词；「结束语音模式/退出语音」。
  否则作为问题 → `POST .../ask` 带 `"style":"voice"`，quote = 当前选区或当前朗读句，page = 当前页 → 流式显示在问答历史中，同时**边流式边按句朗读**回答（朗读前去掉 Markdown 标记，公式 `$…$` 做简单口语化：去掉 `$`、`\`，`\sqrt{x}`→「根号 x」，`^2`→「平方」，`_k`→「下标 k」）。
- 连续对话（hands-free）开关：回答读完后自动再次聆听；TTS 播报时暂停识别避免回声；点击语音球或开始说话可打断播报（barge-in）。
- 与听论文联动：朗读中触发语音提问 → 暂停朗读 → 回答完毕后若开启「答完继续朗读」则自动从原位置继续。
- 浏览器不支持识别时（如 Firefox）显示中文提示「请用 Chrome / Edge / Safari」。提示用户：Chrome 的语音识别会把音频发到 Google 服务器转写，Safari 走 Apple。
- 后端：ask 接收 `"style":"voice"` → 在提问后追加说明：「这是语音对话：请用口语化的<answer_lang>回答，2–6 句，先说结论；不要用表格、代码块、标题；公式用口语描述。」
  qa 记录增加 `"style":"voice"` 字段。

## 12. GitHub Pages 静态只读站（新增需求）
用户：记笔记走本地；只是看的话，用 GitHub 公开网页静态地看笔记。除了 GitHub 自带的 Markdown 渲染外，再导出一个**只读版阅读器**到文献库仓库，开启 GitHub Pages 后可在线浏览。

### 12.1 后端导出（store/gitsync 在每次同步前执行，或任何内容改动后防抖执行）
- 在数据目录根写：
  - `library.json`：`{"generated":"ISO","app_url":"...","projects":[{"id","name","description","updated","counts"}]}`
  - `vocabulary.json`：全库生词数组（同 GET /api/vocab 的结构）
  - `index.html`：由 `web/index.html` 生成——把所有 `/static/` 替换为 `static/`，并在 `<head>` 最前面注入 `<script>window.PAPERDESK_STATIC=true;</script>`
  - `static/`：`web/` 下全部静态资源的拷贝（含 vendor；仅当内容变化时复制，按文件 mtime/大小比较）
  - `.nojekyll`（空文件，防止 Jekyll 忽略下划线文件）
  - `404.html`：与 index.html 相同（兜底）
- settings 新字段 `"pages_url":""`：为空时若 remote 是 GitHub（`git@github.com:o/r.git` 或 `https://github.com/o/r(.git)`）则自动推导为 `https://<o>.github.io/<r>/`（若 r == `<o>.github.io` 则为 `https://<o>.github.io/`）。`GET /api/sync/status` 返回 `pages_url`。
- 论文 md 顶部链接行改为：`[📖 在 PaperDesk 中打开（本地，可编辑）](app_url/r/pid/iid) · [🌐 在线只读版](pages_url#/r/pid/iid)（pages_url 为空则省略） · [原文](url)`；项目 README 与根 README 同理加在线只读链接。
- `.gitignore` 不要忽略 index.html / static / library.json / vocabulary.json。
- 同步面板提示：首次需要在 GitHub 仓库 Settings → Pages → Source 选 `Deploy from a branch`，分支 `main`、目录 `/ (root)`；若 gh 可用，`POST /api/sync/enable_pages` 调 `gh api -X POST repos/<o>/<r>/pages -f "source[branch]=main" -f "source[path]=/"`（已开启则忽略错误）。

### 12.2 前端静态只读模式（`window.PAPERDESK_STATIC === true`）
- 路由改用 hash：`#/`、`#/p/<pid>`、`#/r/<pid>/<iid>`、`#/vocab`（本地模式仍用 History API；两种都能解析）。
- 数据读取改为相对路径文件：`library.json`、`projects/<pid>/project.json`、`projects/<pid>/papers/<paper_key>.json`、`vocabulary.json`、`projects/<pid>/papers/<key>.md`。
  所有写操作、AI、翻译、生词查询、同步、设置、语音对话入口隐藏；页面顶部显示细横幅「只读在线版 · 编辑请在本地 PaperDesk 中打开」+ 跳转本地链接（app_url 来自 library.json）。朗读/听整篇（纯浏览器）保留。
- PDF：先尝试 pdf.js 直接加载 `pdf_url`（若源站允许跨域就能完整显示高亮/下划线等标注）；失败则降级为 `<iframe src="<pdf_url>#page=N">` 用浏览器自带 PDF 查看器，
  右侧笔记/标注/问答/生词/总结照常渲染，点击笔记切换 iframe 到对应页（`#page=N`），并显示提示「此 PDF 源站不允许跨域，标注无法叠加显示，已按页码定位」。
  再失败（iframe 也被拒）则只显示右侧内容 + 「在 arXiv 打开」按钮。

## 13. 网页文章（博客）阅读 + 一篇工作的多资源（新增需求）
用户：除了 PDF，blog 这类网页也要能嵌进来读（同样能翻译、问 AI、标注、记笔记、生词、朗读）；一篇工作常常同时有论文、代码、主页、博客，要能记录并存在一起。

### 13.1 网页文章
- resolve：非 PDF 的 http(s) 链接 → `type:"web"`（取代原来的 "link"；旧数据里的 "link" 前后端一律按 "web" 处理），返回 `title, site_name, description, kind`（kind 见 13.2）。
  web 条目的 paper_key = `web-<sha1(url)[:10]>`；`GET /api/papers/<pid>/<iid>` 对 web/link 条目同样可用（状态结构相同，meta 里多 `site_name`）。
- `GET /api/web?url=<urlencoded>`（不落盘、不缓存；上限 8MB；超时 30s；跟随重定向；按 header / `<meta charset>` 解码）→
  `{"url":最终地址,"title","byline","site_name","published","lang","html":清洗后的正文 HTML,"text":纯文本（段落间空行）,"word_count","mode":"article|readme"}`。
  正文抽取（fetcher.py，纯标准库 html.parser，实现一个简化版 Readability）：
  - 丢弃 script/style/noscript/iframe/form/nav/header/footer/aside/svg/button 以及 class/id 匹配
    `comment|sidebar|footer|nav|menu|share|social|related|advert|promo|cookie|subscribe|newsletter|breadcrumb` 的元素；
  - 候选容器优先 `<article>`、`<main>`、`[role=main]`、常见博客正文类（post-content、entry-content、article-body、markdown-body、post、content）；否则按「段落文本长度 − 链接密度惩罚」给块级容器打分取最高；
  - 输出白名单标签：h1–h6 p ul ol li blockquote pre code img figure figcaption table thead tbody tr th td a em strong b i u sup sub br hr；
    a/img 的相对地址转绝对地址；img 保留 src/alt（懒加载的 data-src / srcset 首项也要处理）；去掉所有 on* 属性与 style；
  - 公式：`<script type="math/tex">x</script>` → `$x$`（`mode=display` → `$$x$$`）；KaTeX 渲染结果取其中 `<annotation encoding="application/x-tex">` → `$…$`；MathJax 的 `\(...\)` 原样保留；
  - 抽取结果太短（< 200 字符）时 `mode:"article"` 仍返回，同时 `"weak":true`，前端提示可「在新标签页打开原网页」。
- GitHub 仓库链接（`github.com/<o>/<r>` 及其子路径）→ `mode:"readme"`：经 `https://api.github.com/repos/<o>/<r>`（描述、stars、language、homepage）与
  `https://api.github.com/repos/<o>/<r>/readme`（Accept: application/vnd.github.raw）获取，返回 `{"mode":"readme","markdown","title":"o/r","description","stars","language","homepage","raw_base":"https://raw.githubusercontent.com/<o>/<r>/HEAD/","html_base":"https://github.com/<o>/<r>/blob/HEAD/"}`（失败则 error 中文说明）。
- 网页的「全文缓存」与标注：
  - `PUT /api/papers/<pid>/<iid>/text` 增加可选 `"resource_id"`（默认 "main"）；缓存按资源分别存；agent 的 `paper.txt` 依次拼接：PDF（`===== [p.N] =====`）→ 每个网页/README 资源（`===== [网页：<标题>] =====` / `===== [代码 README：o/r] =====`）。没缓存的资源不拼。
  - 标注新增字段：`"resource_id"`（默认 "main"）、`"anchor":{"type":"text","exact","prefix","suffix","start","end"}`（start/end 为该资源规范化纯文本中的字符偏移；prefix/suffix 各取 32 字符）；网页标注 `page:null, rects:[]`。笔记/问答/翻译/生词同样可带 `resource_id` 与 `anchor`。

### 13.2 一篇工作的多资源
- item 新增 `"resources":[{"id":"r_xxxx","kind","url","title","pdf_url"?,"auto":bool,"added"}]`。item 自己的主链接视为 id 为 `"main"` 的资源（不写进数组）。
- kind 取值与识别（fetcher.classify(url)）：arXiv/OpenReview/.pdf/Content-Type pdf → `paper`；github.com / gitlab.com / bitbucket.org / codeberg.org → `code`；
  youtube.com / youtu.be / bilibili.com / vimeo.com → `video`；huggingface.co/datasets/ → `dataset`；huggingface.co/spaces/ → `demo`；huggingface.co/<o>/<m> → `model`；
  speakerdeck / slideshare / docs.google.com/presentation / .pptx|.key → `slides`；medium.com / substack.com / distill.pub / 路径含 /blog/ /posts/ /p/ 或域名含 blog → `blog`；
  其余：路径为空或只有一级 → `homepage`，否则 `blog`。
- API：`POST /api/projects/<pid>/items/<iid>/resources` `{"url","kind"?,"title"?}` → resource（resolve 取标题与 kind，网络失败降级）；
  `PATCH .../resources/<rid>` `{"kind"?,"title"?,"url"?}`；`DELETE .../resources/<rid>`（同时删除 resource_id 相同的标注；笔记保留但去掉锚点）。
- 添加条目时 input 含多个 URL（换行/空格分隔）→ 只建一个条目：主链接取第一个 paper 类链接（没有就取第一个），其余作为 resources。
- 自动发现（auto:true）：arXiv 论文的 abstract 与 comment 字段里出现的 github.com/... 链接 → code；`project page|website|homepage` 附近的链接或 *.github.io → homepage；
  GitHub 仓库 README 里出现的 arxiv.org/abs|pdf/<id> → paper（标题后台补）。自动资源去重（同 URL 不重复）。
- 论文 .md 标题下加一行：`**相关资源**：📄 [论文](…) · 💻 [代码](…) · 🏠 [主页](…) · 📝 [博客](…) · 🎬 [视频](…) · 🤗 [模型/数据](…) · 🖼️ [幻灯片](…)`；
  项目 README 的条目行带这些图标链接；笔记/标注按资源分组时注明来源（📝 博客：标题）。
- 前端：
  - 项目页条目卡片显示资源小标签（图标 + 域名，点开新标签页）与「+ 关联资源」（粘贴 URL，自动识别类型，可改类型）；web 条目卡片显示站点名与摘要，「阅读」进入阅读页。
  - 阅读页左栏顶部「资源条」：`📄 论文 · 📝 博客：xxx · 🏠 主页 · 💻 代码 · 🎬 视频 …` + 「＋」。切换资源：
    paper → pdf.js；web/blog/homepage → 文章阅读器（`/api/web`，DOMPurify 清洗，排版成舒适的阅读宽度，图片懒加载，KaTeX 渲染 `$…$` / `\(...\)` / `$$…$$`，代码块等宽）；
    code → README 阅读器（marked 渲染，相对链接与图片按 raw_base/html_base 补全，顶部显示仓库描述/stars/语言）；video → YouTube/Bilibili 嵌入 iframe（无标注，可记笔记）；
    抽取失败或 weak → 提示 + 「在新标签页打开原网页」+「仍然用 iframe 嵌入（可能被网站拒绝，无法标注）」。路由 `?res=<rid>` 记住当前资源。
  - 文章 / README 阅读器复用划词工具条（翻译 / 解释 / 问 Agent / 记一笔 / 高亮 5 色 / 下划线 / 删除线 / 波浪线 / 朗读 / 生词）；标注用 Range 包裹 `<mark class="anno anno-<kind> c-<color>">`，
    恢复时先按 exact+prefix+suffix 在规范化文本中定位，失败再用 start/end 偏移；点击标注弹出同样的菜单。
  - 右侧笔记/标注/生词卡片显示资源徽标；点击会切换到对应资源并滚动、闪烁锚点。
  - 文章加载后把纯文本 PUT 到 text 接口（带 resource_id），问 Agent 时它就能读到博客/README。
  - 朗读 / 听整篇：对文章资源按段落/句子朗读并高亮当前句。
  - 静态只读版：文章资源在 GitHub Pages 上无法经代理抓取 → 显示「在新标签页打开」+ 该资源的笔记与标注列表（引文）。
