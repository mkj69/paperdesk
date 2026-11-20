"""配置、设置读写、通用文件工具与 CLI 路径探测。

公共函数：
    get_data_dir(override=None) -> str
    now_iso() -> str
    read_json(path, default=None)
    atomic_write_text(path, text)
    atomic_write_json(path, obj)
    load_settings(data_dir) -> dict               # settings.json + settings.local.json（本机字段 LOCAL_KEYS）
    save_settings(data_dir, patch) -> dict        # 共享字段写 settings.json，本机字段写 settings.local.json
    extra_bin_dirs() -> list[str]
    find_cli(name, settings=None) -> str | None      # name: "claude" | "codex" | "git" | "gh"
    subprocess_env() -> dict
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import shutil
import tempfile

DEFAULT_DATA_DIR = os.path.join("~", "PaperDesk-Library")

DEFAULT_SETTINGS = {
    "default_engine": "claude",
    "translate_engine": "google",
    "vocab_engine": "google",
    "target_lang": "zh-CN",
    "answer_lang": "中文",
    "claude_model": "",
    "claude_translate_model": "haiku",
    "codex_model": "",
    "claude_path": "",
    "codex_path": "",
    "app_url": "http://localhost:8765",
    "auto_sync": True,
    "sync_debounce_sec": 30,
    "github_remote": "",
    "pages_url": "",
}

# 设置项的类型，用于 PUT /api/settings 校验
SETTINGS_TYPES = {
    "default_engine": str,
    "translate_engine": str,
    "vocab_engine": str,
    "target_lang": str,
    "answer_lang": str,
    "claude_model": str,
    "claude_translate_model": str,
    "codex_model": str,
    "claude_path": str,
    "codex_path": str,
    "app_url": str,
    "auto_sync": bool,
    "sync_debounce_sec": int,
    "github_remote": str,
    "pages_url": str,
}

ENV_CLI = {"claude": "PAPERDESK_CLAUDE", "codex": "PAPERDESK_CODEX"}

# 与具体电脑有关的设置：存在不入库的 settings.local.json，换电脑不会互相覆盖
LOCAL_KEYS = ("claude_path", "codex_path")
SETTINGS_FILE = "settings.json"
LOCAL_SETTINGS_FILE = "settings.local.json"


def get_data_dir(override=None):
    path = override or os.environ.get("PAPERDESK_HOME") or DEFAULT_DATA_DIR
    return os.path.abspath(os.path.expanduser(path))


def now_iso():
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        return default


def atomic_write_text(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def load_settings(data_dir):
    """合并 settings.json（可共享，入库）与 settings.local.json（本机，不入库）。"""
    s = dict(DEFAULT_SETTINGS)
    shared = read_json(os.path.join(data_dir, SETTINGS_FILE), {}) or {}
    local_path = os.path.join(data_dir, LOCAL_SETTINGS_FILE)
    has_local = os.path.exists(local_path)
    local = read_json(local_path, {}) or {}
    if isinstance(shared, dict):
        for k, v in shared.items():
            if k not in DEFAULT_SETTINGS:
                continue
            if k in LOCAL_KEYS and has_local:
                continue          # 本机文件存在时，忽略仓库里残留的机器相关字段
            s[k] = v
    if isinstance(local, dict):
        for k, v in local.items():
            if k in LOCAL_KEYS:
                s[k] = v
    return s


def _coerce(key, value):
    t = SETTINGS_TYPES.get(key)
    if t is None:
        raise KeyError(key)
    if t is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t is int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError("设置项 %s 需要整数" % key)
        return max(1, min(v, 3600)) if key == "sync_debounce_sec" else v
    if value is None:
        return ""
    return str(value).strip()


def save_settings(data_dir, patch):
    s = load_settings(data_dir)
    for k, v in (patch or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        s[k] = _coerce(k, v)
    if s["default_engine"] not in ("claude", "codex"):
        s["default_engine"] = "claude"
    if s["translate_engine"] not in ("google", "claude", "codex"):
        s["translate_engine"] = "google"
    if s["vocab_engine"] not in ("google", "claude", "codex"):
        s["vocab_engine"] = "google"
    if s["app_url"]:
        s["app_url"] = s["app_url"].rstrip("/")
    if s["pages_url"]:
        s["pages_url"] = s["pages_url"].rstrip("/") + "/"
    shared = {k: v for k, v in s.items() if k not in LOCAL_KEYS}
    local = {k: s.get(k, "") for k in LOCAL_KEYS}
    atomic_write_json(os.path.join(data_dir, SETTINGS_FILE), shared)
    atomic_write_json(os.path.join(data_dir, LOCAL_SETTINGS_FILE), local)
    return s


def extra_bin_dirs():
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".claude", "local"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".bun", "bin"),
    ]
    dirs.extend(sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")), reverse=True))
    return [d for d in dirs if os.path.isdir(d)]


def _is_exe(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def find_cli(name, settings=None):
    env_key = ENV_CLI.get(name)
    if env_key and os.environ.get(env_key):
        p = os.path.expanduser(os.environ[env_key])
        if _is_exe(p):
            return p
    if settings:
        p = os.path.expanduser(settings.get("%s_path" % name, "") or "")
        if _is_exe(p):
            return p
    p = shutil.which(name)
    if p:
        return p
    for d in extra_bin_dirs():
        cand = os.path.join(d, name)
        if _is_exe(cand):
            return cand
    return None


def subprocess_env():
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for d in extra_bin_dirs():
        if d not in parts:
            parts.append(d)
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    env.setdefault("LANG", "en_US.UTF-8")
    env["PYTHONIOENCODING"] = "utf-8"
    return env
