#!/bin/sh
# 把仓库里共享的 git 钩子装到本地 .git/hooks/。
#
# .git/hooks 不随仓库分发，所以每个克隆都要装一次。
# 装完以后：推送到 main 会被拒绝，强推会被拒绝。

set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
git_dir=$(git -C "$root" rev-parse --absolute-git-dir)
mkdir -p "$git_dir/hooks"
cp "$root/.githooks/pre-push" "$git_dir/hooks/pre-push"
chmod +x "$git_dir/hooks/pre-push"
echo "已安装 pre-push 钩子：$git_dir/hooks/pre-push"
