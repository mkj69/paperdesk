#!/bin/zsh
# PaperDesk — macOS 双击启动脚本
# 第一次如果提示“无法验证开发者”：在 Finder 里右键 → 打开；或在终端执行
#   xattr -d com.apple.quarantine /path/to/PaperDesk.command
cd "$(dirname "$0")" || exit 1

PORT="${PAPERDESK_PORT:-8765}"
URL="http://localhost:${PORT}"

# Finder 启动的进程拿不到你 shell 里的 PATH，这里补上常见位置（claude / codex / git / gh）
if [ -x /usr/libexec/path_helper ]; then
  eval "$(/usr/libexec/path_helper -s)"
fi
for d in "$HOME/.local/bin" "$HOME/.claude/local" /opt/homebrew/bin /usr/local/bin \
         "$HOME/.npm-global/bin" "$HOME/.bun/bin" "$HOME"/.nvm/versions/node/*/bin(N); do
  [ -d "$d" ] && PATH="$d:$PATH"
done
export PATH

# 已经在运行？直接打开浏览器
if curl -s -m 2 "${URL}/api/health" >/dev/null 2>&1; then
  echo "PaperDesk 已经在运行：${URL}"
  open "${URL}"
  exit 0
fi

PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "没有找到 python3。请先安装 Xcode 命令行工具：xcode-select --install"
  read -r "?按回车键退出…"
  exit 1
fi

echo "正在启动 PaperDesk：${URL}"
echo "（关闭这个终端窗口或按 Ctrl+C 即可停止）"
exec "$PY" server.py --port "${PORT}" "$@"
