#!/usr/bin/env bash
# 把 PaperDesk 的「代码」发布到你的 GitHub（默认公开仓库 paperdesk）。
# 注意：这是代码仓库，不是文献库。文献库（笔记）在 PaperDesk 的同步面板里单独连接。
#
#   scripts/publish_to_github.sh                 # 创建公开仓库 paperdesk 并推送
#   scripts/publish_to_github.sh my-name --private
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_NAME="${1:-paperdesk}"
VISIBILITY="${2:---public}"

if ! command -v git >/dev/null 2>&1; then
  echo "没有找到 git。macOS 请先运行：xcode-select --install" >&2
  exit 1
fi

if [ ! -d .git ]; then
  git init -b main
fi

if [ ! -f .gitignore ]; then
  cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.DS_Store
/tmp/
EOF
fi

git add -A
if ! git diff --cached --quiet; then
  git commit -m "PaperDesk: publish code" >/dev/null
  echo "已提交本地改动。"
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "已存在 remote origin：$(git remote get-url origin)，直接推送…"
  git push -u origin HEAD
  exit 0
fi

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh repo create "$REPO_NAME" "$VISIBILITY" --source . --remote origin --push \
    --description "PaperDesk: 不下载 PDF 的论文阅读台（翻译 / Claude Code & Codex 问答 / 标注笔记 / GitHub 同步）"
  echo "完成：$(gh repo view "$REPO_NAME" --json url -q .url 2>/dev/null || echo "$REPO_NAME")"
else
  cat <<EOF
没有可用的 GitHub CLI（gh 未安装或未登录）。两种办法任选其一：

  A. 安装并登录 gh 后重新运行本脚本：
       brew install gh && gh auth login
       scripts/publish_to_github.sh

  B. 手动：先在 https://github.com/new 建一个名为 ${REPO_NAME} 的空仓库（不要勾选 README），然后：
       git remote add origin git@github.com:<你的用户名>/${REPO_NAME}.git
       git push -u origin main
EOF
  exit 1
fi
