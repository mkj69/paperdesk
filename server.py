#!/usr/bin/env python3
"""PaperDesk 启动入口。

    python3 server.py [--port 8765] [--data ~/PaperDesk-Library] [--no-browser] [--clone <git 仓库地址>]

换电脑：python3 server.py --clone git@github.com:<你>/paper-library.git
（数据目录不存在或为空时先 clone 远端文献库，再启动）
"""
from __future__ import annotations

import argparse
import errno
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if sys.version_info < (3, 8):
    sys.stderr.write("PaperDesk 需要 Python 3.8 或更高版本\n")
    sys.exit(1)

from paperdesk import VERSION, config  # noqa: E402
from paperdesk.api import make_server  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="PaperDesk — 本地论文阅读工作台")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PAPERDESK_PORT", "8765")))
    ap.add_argument("--data", default=None, help="文献库目录（默认 ~/PaperDesk-Library，或环境变量 PAPERDESK_HOME）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--clone", metavar="URL", default=None,
                    help="数据目录不存在或为空时，先从这个 git 地址 clone 文献库（换电脑时用）")
    args = ap.parse_args(argv)

    data_dir = config.get_data_dir(args.data)
    if args.clone:
        from paperdesk.gitsync import clone_library
        try:
            if clone_library(args.clone, data_dir):
                print("已从 %s clone 文献库到 %s" % (args.clone, data_dir))
            else:
                print("数据目录 %s 已有内容，跳过 clone（会在同步时与远端合并）" % data_dir)
        except RuntimeError as e:
            print("clone 失败：%s" % e)
            return 1
    os.makedirs(data_dir, exist_ok=True)
    try:
        srv = make_server(data_dir, port=args.port)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 48, 98):
            url = "http://localhost:%d" % args.port
            print("端口 %d 已被占用：PaperDesk 可能已经在运行了 → %s" % (args.port, url))
            if not args.no_browser:
                webbrowser.open(url)
            return 1
        raise
    url = "http://localhost:%d" % args.port
    app = srv.app
    app.start()
    s = app.settings()
    from paperdesk import agents
    cli = agents.cli_status(s)
    print("=" * 60)
    print(" PaperDesk %s 已启动：%s" % (VERSION, url))
    print(" 文献库目录：%s" % data_dir)
    print(" Claude Code：%s" % (cli["claude"]["path"] or "未找到"))
    print(" Codex      ：%s" % (cli["codex"]["path"] or "未找到"))
    print(" 关闭：在这个窗口按 Ctrl+C")
    print("=" * 60)
    sys.stdout.flush()
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在退出，最后同步一次…")
        try:
            app.git.sync_now()
        except Exception:
            pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
