"""数据目录的 git 自动同步（防抖 commit → fetch → 快进或语义合并 → push），支持多台电脑。

用法：
    gs = GitSync(data_dir, settings_getter)
    gs.init_repo()                 # 启动时调用；git 不存在则 enabled=False
    gs.schedule("添加论文 X")       # 防抖，sync_debounce_sec 秒后后台同步
    gs.sync_now() -> status        # 立即同步
    gs.status() -> dict
    gs.setup(remote_url=None, create=None, private=False) -> status
    gs.enable_pages() -> dict      # gh api 开启 GitHub Pages（main 分支根目录）
    pages_url_from_remote(remote) -> str | None
    clone_library(url, data_dir)   # 新电脑：把远端文献库 clone 到空目录

多台电脑：setup(remote_url) 时若远端已有提交——本地是新库则直接采用远端（adopt），
否则做三方语义合并（store.merge_data_file：按 id 合并 items / 标注 / 笔记 / 问答 / 生词，自动生成的文件重新生成）。
日常同步时若远端领先：本地无新提交则快进，否则同样语义合并后再推送。
构造参数 pre_sync：每次同步（commit）前调用的回调（用于导出 GitHub Pages 只读站）；
on_pages_url_change：pages_url 自动推导结果变化时调用（用于重渲染 md/README）。
"""
from __future__ import annotations

import os
import subprocess
import threading

from . import config

GIT_TIMEOUT = 60


class GitSync:
    def __init__(self, data_dir, settings_getter, pre_sync=None, on_pages_url_change=None):
        self.data_dir = data_dir
        self.get_settings = settings_getter
        self.pre_sync = pre_sync
        self.on_pages_url_change = on_pages_url_change
        self.git = config.find_cli("git")
        self.enabled = bool(self.git)
        self._lock = threading.Lock()          # 同一时间只有一个同步在跑
        self._sched_lock = threading.Lock()
        self._git_lock = threading.RLock()     # 串行化每一条 git 命令
        self._timer = None
        self._pending = []
        self.last_sync = None
        self.last_error = None if self.enabled else "没有找到 git 命令，同步功能已关闭"
        self.adopted = False            # 本次运行中是否采用了远端文献库（换电脑接入）
        self.last_merge = None          # {"kind": "adopt|merge|ff", "at", "unrelated", "files"}
        self._branch = None

    # ------------------------------------------------------------ 基础
    def _env(self):
        env = config.subprocess_env()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        return env

    def _run(self, *args, check=False, timeout=GIT_TIMEOUT):
        # 所有 git 命令串行执行（状态轮询、后台同步、连接远端可能并发），
        # 并加 --no-optional-locks，避免 `git status` 顺手刷新 index 时抢占 index.lock。
        with self._git_lock:
            r = subprocess.run([self.git, "--no-optional-locks"] + list(args), cwd=self.data_dir,
                               env=self._env(), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(("git %s 失败：" % args[0]) + (r.stderr or r.stdout).strip()[-800:])
        return r

    def is_repo(self):
        return os.path.isdir(os.path.join(self.data_dir, ".git"))

    def remote(self):
        if not self.enabled or not self.is_repo():
            return None
        r = self._run("remote", "get-url", "origin")
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def init_repo(self):
        if not self.enabled:
            return
        try:
            if not self.is_repo():
                r = self._run("init", "-b", "main")
                if r.returncode != 0:
                    self._run("init", check=True)
                    self._run("symbolic-ref", "HEAD", "refs/heads/main")
            # 没配置身份时给仓库一个本地身份，避免 commit 失败
            if not self._run("config", "user.email").stdout.strip():
                self._run("config", "user.email", "paperdesk@localhost")
            if not self._run("config", "user.name").stdout.strip():
                self._run("config", "user.name", "PaperDesk")
            self._run_pre_sync()
            self._commit_all("初始化论文库")
            s = self.get_settings() or {}
            if s.get("github_remote") and not self.remote():
                self._run("remote", "add", "origin", s["github_remote"])
            self.ensure_pages_url()
        except Exception as e:
            self.last_error = str(e)

    def _run_pre_sync(self):
        if self.pre_sync:
            try:
                self.pre_sync()
            except Exception as e:
                self.last_error = "导出在线只读站失败：%s" % e

    def ensure_pages_url(self, old_remote=None):
        """settings.pages_url 为空（或等于旧远端推导出的地址）时，按当前 GitHub 远端自动推导。"""
        s = self.get_settings() or {}
        cur = (s.get("pages_url") or "").strip()
        derived = pages_url_from_remote(self.remote())
        old_derived = pages_url_from_remote(old_remote) if old_remote else None
        if derived and (not cur or (old_derived and cur.rstrip("/") == old_derived.rstrip("/"))) \
                and cur.rstrip("/") != derived.rstrip("/"):
            config.save_settings(self.data_dir, {"pages_url": derived})
            if self.on_pages_url_change:
                try:
                    self.on_pages_url_change()
                except Exception:
                    pass
        return config.load_settings(self.data_dir).get("pages_url") or ""

    def _commit_all(self, msg):
        self._run("add", "-A", check=True)
        r = self._run("diff", "--cached", "--quiet")
        if r.returncode == 0:
            return False
        self._run("commit", "-m", msg, check=True)
        return True

    # ------------------------------------------------------------ 防抖调度
    def schedule(self, msg):
        if not self.enabled:
            return
        s = self.get_settings() or {}
        if not s.get("auto_sync", True):
            return
        delay = s.get("sync_debounce_sec", 30) or 30
        with self._sched_lock:
            if msg and msg not in self._pending:
                self._pending.append(msg)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(float(delay), self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        try:
            self.sync_now()
        except Exception as e:
            self.last_error = str(e)

    def _take_message(self):
        with self._sched_lock:
            msgs, self._pending = self._pending, []
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if not msgs:
            return "PaperDesk: 更新"
        head = "；".join(msgs[:3])
        if len(msgs) > 3:
            head += " 等 %d 项" % len(msgs)
        body = "\n".join("- " + m for m in msgs) if len(msgs) > 1 else ""
        return "PaperDesk: " + head + ("\n\n" + body if body else "")

    # ------------------------------------------------------------ 同步
    def sync_now(self):
        if not self.enabled:
            return self.status()
        from . import store
        with self._lock:
            msg = self._take_message()
            try:
                if not self.is_repo():
                    self.init_repo()
                with store.LOCK:
                    self._run_pre_sync()
                    self._commit_all(msg)
                err = None
                if self.remote():
                    err = self._pull_and_push()
                self.last_error = err
                self.last_sync = config.now_iso()
            except subprocess.TimeoutExpired:
                self.last_error = "git 操作超时（网络或认证问题？）"
            except Exception as e:
                self.last_error = str(e)
        return self.status()

    def _remote_branch(self, refresh=False):
        """远端默认分支名；远端还没有任何提交时返回 None。"""
        if self._branch and not refresh:
            return self._branch
        r = self._run("ls-remote", "--symref", "origin", "HEAD")
        if r.returncode != 0:
            raise RuntimeError("无法访问远端仓库：" + (r.stderr or r.stdout).strip()[-500:])
        for line in r.stdout.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                self._branch = line[len("ref: refs/heads/"):-len("\tHEAD")]
                return self._branch
        r = self._run("ls-remote", "--heads", "origin")
        heads = [ln.split("refs/heads/", 1)[1] for ln in r.stdout.splitlines() if "refs/heads/" in ln]
        if not heads:
            return None
        for cand in ("main", "master"):
            if cand in heads:
                self._branch = cand
                return cand
        self._branch = heads[0]
        return self._branch

    def _count(self, spec):
        r = self._run("rev-list", "--count", spec)
        try:
            return int(r.stdout.strip() or 0)
        except ValueError:
            return 0

    def _pull_and_push(self):
        """fetch → （快进 | 语义合并）→ push。返回错误信息或 None。"""
        branch = self._remote_branch()
        if branch:
            r = self._run("fetch", "origin", branch)
            if r.returncode != 0:
                return "拉取远端失败：" + (r.stderr or r.stdout).strip()[-500:]
            ref = "origin/" + branch
            behind = self._count("HEAD..%s" % ref)
            if behind:
                ahead = self._count("%s..HEAD" % ref)
                if ahead == 0:
                    self._run("merge", "--ff-only", ref, check=True)
                    self.last_merge = {"kind": "ff", "at": config.now_iso(), "unrelated": False, "files": behind}
                else:
                    self._smart_merge(branch)
        target = branch or (self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main")
        r = self._run("push", "-u", "origin", "HEAD:refs/heads/%s" % target)
        if r.returncode != 0:
            return "推送失败：" + (r.stderr or r.stdout).strip()[-500:]
        if not self._has_upstream():
            self._run("branch", "--set-upstream-to=origin/%s" % target)
        self._branch = target
        return None

    def _show_json(self, rev, path):
        if not rev:
            return None
        r = subprocess.run([self.git, "show", "%s:%s" % (rev, path)], cwd=self.data_dir, env=self._env(),
                           capture_output=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0:
            return None
        try:
            import json as _json
            return _json.loads(r.stdout.decode("utf-8"))
        except ValueError:
            return None

    def _ls_tree(self, rev):
        r = self._run("ls-tree", "-r", "--name-only", rev)
        return set(r.stdout.split("\n")) - {""} if r.returncode == 0 else set()

    def _regenerate(self):
        from . import store
        store.ensure_library(self.data_dir)
        store.rerender_all(self.data_dir)
        self._run_pre_sync()

    def _smart_merge(self, branch):
        """把 origin/<branch> 合并进来：数据 json 三方语义合并，自动生成的文件全部重新生成。"""
        from . import store
        ref = "origin/" + branch
        base = self._run("merge-base", "HEAD", ref).stdout.strip() or None
        args = ["merge", "--no-commit", "--no-ff", "-X", "ours"]
        if not base:
            args.append("--allow-unrelated-histories")
        args.append(ref)
        with store.LOCK:
            r = self._run(*args)
            merging = os.path.exists(os.path.join(self.data_dir, ".git", "MERGE_HEAD"))
            if r.returncode != 0 and not merging:
                raise RuntimeError("合并远端失败：" + (r.stderr or r.stdout).strip()[-600:])
            if not merging:
                return      # 已经是最新
            try:
                paths = [p for p in (self._ls_tree("HEAD") | self._ls_tree(ref)) if store.is_data_file(p)]
                changed = 0
                for path in sorted(paths):
                    b = self._show_json(base, path)
                    o = self._show_json("HEAD", path)
                    t = self._show_json(ref, path)
                    merged = store.merge_data_file(path, b, o, t)
                    full = os.path.join(self.data_dir, *path.split("/"))
                    if merged is None:
                        if os.path.exists(full):
                            os.remove(full)
                            changed += 1
                    else:
                        if path == "settings.json":
                            merged = {k: v for k, v in merged.items() if k not in config.LOCAL_KEYS}
                        cur = config.read_json(full)
                        if cur != merged:
                            config.atomic_write_json(full, merged)
                            changed += 1
                self._regenerate()
                self._run("add", "-A", check=True)
                self._run("commit", "--no-verify", "-m",
                          "PaperDesk: 合并另一台电脑的修改" + ("（首次合并两个文献库）" if not base else ""), check=True)
                self.last_merge = {"kind": "merge", "at": config.now_iso(), "unrelated": not base, "files": changed}
            except Exception:
                self._run("merge", "--abort")
                raise

    def _adopt(self, branch):
        """本地是新库：直接采用远端文献库（保留本机 settings.local.json）。"""
        from . import store
        ref = "origin/" + branch
        with store.LOCK:
            self._run("fetch", "origin", branch, check=True)
            self._run("reset", "--hard", ref, check=True)
            cur = self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if cur != branch:
                self._run("branch", "-M", branch, check=True)
            self._run("branch", "--set-upstream-to=" + ref, check=True)
            self._branch = branch
            config.save_settings(self.data_dir, {})     # 把机器相关字段移出 settings.json
            self._regenerate()
            self._commit_all("PaperDesk: 在新电脑上接入文献库")
        self.adopted = True
        self.last_merge = {"kind": "adopt", "at": config.now_iso(), "unrelated": False, "files": 0}

    def _has_upstream(self):
        r = self._run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        return r.returncode == 0

    def status(self):
        st = {"enabled": self.enabled, "repo": False, "remote": None, "branch": None, "dirty": False,
              "ahead": 0, "last_sync": self.last_sync, "last_error": self.last_error,
              "gh": bool(config.find_cli("gh")), "pending": False, "web_url": None,
              "pages_url": ((self.get_settings() or {}).get("pages_url") or None),
              "adopted": self.adopted, "last_merge": self.last_merge}
        with self._sched_lock:
            st["pending"] = bool(self._pending) or bool(self._timer)
        if not self.enabled:
            return st
        try:
            st["repo"] = self.is_repo()
            if not st["repo"]:
                return st
            st["remote"] = self.remote()
            st["web_url"] = github_web_url(st["remote"])
            r = self._run("rev-parse", "--abbrev-ref", "HEAD")
            st["branch"] = r.stdout.strip() or None
            st["dirty"] = bool(self._run("status", "--porcelain").stdout.strip())
            if st["remote"]:
                if self._has_upstream():
                    r = self._run("rev-list", "--count", "@{u}..HEAD")
                else:
                    r = self._run("rev-list", "--count", "HEAD")
                try:
                    st["ahead"] = int(r.stdout.strip() or 0)
                except ValueError:
                    st["ahead"] = 0
        except Exception as e:
            st["last_error"] = str(e)
        return st

    def setup(self, remote_url=None, create=None, private=False):
        if not self.enabled:
            raise RuntimeError("没有找到 git 命令，无法连接 GitHub")
        with self._lock:
            if not self.is_repo():
                self.init_repo()
        old_remote = self.remote()
        with self._lock:
            self._run_pre_sync()
            self._commit_all("PaperDesk: 准备连接远端")
            if create:
                gh = config.find_cli("gh")
                if not gh:
                    raise RuntimeError("没有找到 gh（GitHub CLI）。请先 `brew install gh && gh auth login`，或者手动在 GitHub 建仓库后填写仓库地址。")
                if self.remote():
                    raise RuntimeError("已经连接了远端仓库：%s" % self.remote())
                args = [gh, "repo", "create", create, "--private" if private else "--public",
                        "--source", self.data_dir, "--remote", "origin", "--push",
                        "--description", "我的论文库（PaperDesk 自动维护）"]
                r = subprocess.run(args, cwd=self.data_dir, env=self._env(), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
                if r.returncode != 0:
                    raise RuntimeError("创建 GitHub 仓库失败：" + (r.stderr or r.stdout).strip()[-800:])
            elif remote_url:
                remote_url = remote_url.strip()
                if self.remote():
                    self._run("remote", "set-url", "origin", remote_url, check=True)
                else:
                    self._run("remote", "add", "origin", remote_url, check=True)
            else:
                raise ValueError("请提供仓库地址，或要创建的仓库名")
        url = self.remote()
        self._branch = None
        if remote_url and not create:
            from . import store
            branch = self._remote_branch(refresh=True)
            if branch:
                if store.is_library_empty(self.data_dir):
                    self._adopt(branch)
                # 本地也有内容：交给 sync_now 里的 fetch + 语义合并
        try:
            config.save_settings(self.data_dir, {"github_remote": url or ""})
        except Exception:
            pass
        self.ensure_pages_url(old_remote=old_remote)
        return self.sync_now()

    def enable_pages(self):
        """调用 gh api 开启 GitHub Pages（main 分支 / 根目录）。已开启则视为成功。"""
        web = github_web_url(self.remote())
        if not web:
            raise RuntimeError("还没有连接 GitHub 仓库，无法开启 GitHub Pages")
        gh = config.find_cli("gh")
        if not gh:
            raise RuntimeError("没有找到 gh（GitHub CLI）。请在仓库网页 Settings → Pages → Source 选 Deploy from a branch，"
                               "分支 main、目录 / (root)。")
        owner_repo = web[len("https://github.com/"):]
        args = [gh, "api", "-X", "POST", "repos/%s/pages" % owner_repo,
                "-f", "source[branch]=main", "-f", "source[path]=/"]
        r = subprocess.run(args, cwd=self.data_dir, env=self._env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        already = r.returncode != 0 and ("already" in out.lower() or "409" in out)
        if r.returncode != 0 and not already:
            raise RuntimeError("开启 GitHub Pages 失败：" + out.strip()[-600:])
        pages = self.ensure_pages_url() or pages_url_from_remote(self.remote())
        return {"ok": True, "already": already, "pages_url": pages,
                "message": "GitHub Pages 已开启" + ("（之前就开着）" if already else "") + "，首次部署通常需要 1–2 分钟。"}


def github_web_url(remote):
    """把 git 远端地址转成 https://github.com/owner/repo（不是 GitHub 则返回 None）。"""
    if not remote:
        return None
    r = remote.strip()
    if r.startswith("git@github.com:"):
        path = r[len("git@github.com:"):]
    elif r.startswith("ssh://git@github.com/"):
        path = r[len("ssh://git@github.com/"):]
    elif "github.com/" in r and r.startswith(("https://", "http://")):
        path = r.split("github.com/", 1)[1]
        if "@" in r.split("github.com/")[0]:
            pass
    else:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if path.count("/") != 1:
        return None
    return "https://github.com/" + path


def pages_url_from_remote(remote):
    """GitHub 远端 → GitHub Pages 地址：https://<o>.github.io/<r>/（r 为 <o>.github.io 时是根站点）。"""
    web = github_web_url(remote)
    if not web:
        return None
    owner, repo = web[len("https://github.com/"):].split("/", 1)
    host = "%s.github.io" % owner.lower()
    if repo.lower() == host:
        return "https://%s/" % host
    return "https://%s/%s/" % (host, repo)


def clone_library(url, data_dir):
    """把远端文献库 clone 到 data_dir（目录不存在或为空时）。返回 True 表示执行了 clone。"""
    if os.path.isdir(data_dir) and os.listdir(data_dir):
        return False
    git = config.find_cli("git")
    if not git:
        raise RuntimeError("没有找到 git 命令，无法 clone 文献库")
    parent = os.path.dirname(os.path.abspath(data_dir))
    os.makedirs(parent, exist_ok=True)
    if os.path.isdir(data_dir):
        os.rmdir(data_dir)
    env = config.subprocess_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run([git, "clone", url, data_dir], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:
        raise RuntimeError("clone 失败：" + (r.stderr or r.stdout).strip()[-800:])
    return True
