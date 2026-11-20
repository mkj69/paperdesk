#!/usr/bin/env bash
# PaperDesk 通用启动脚本（macOS / Linux）
#   ./start.sh                 # 默认端口 8765，自动打开浏览器
#   ./start.sh --no-browser    # 其他参数原样传给 server.py（--port / --data / --no-browser）
cd "$(dirname "$0")" || exit 1

PORT="${PAPERDESK_PORT:-8765}"
URL="http://localhost:${PORT}"

for d in "$HOME/.local/bin" "$HOME/.claude/local" /opt/homebrew/bin /usr/local/bin \
         "$HOME/.npm-global/bin" "$HOME/.bun/bin"; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
for d in "$HOME"/.nvm/versions/node/*/bin; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
export PATH

if curl -s -m 2 "${URL}/api/health" >/dev/null 2>&1; then
  echo "PaperDesk 已经在运行：${URL}"
  if command -v open >/dev/null 2>&1; then open "${URL}"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "${URL}"; fi
  exit 0
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "没有找到 python3，请先安装 Python 3.9+。" >&2
  exit 1
fi

echo "正在启动 PaperDesk：${URL}"
exec "$PY" server.py --port "${PORT}" "$@"
