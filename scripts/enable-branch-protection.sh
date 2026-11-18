#!/bin/sh
# 给 main 加服务端分支保护。
#
# 前置条件（二选一）：
#   * 仓库是 public（公开仓库免费提供 branch protection）；或
#   * 账号升级到 GitHub Pro（private 仓库需要 Pro）。
# 两种都不满足时 GitHub 会返回 403：
#   "Upgrade to GitHub Pro or make this repository public to enable this feature."
# 本仓库已经改为 public，此脚本已成功执行并验证 main.protected = true。
#
# 用法：scripts/enable-branch-protection.sh [owner/repo]
#
# 说明：
#   * required_status_checks 使用 strict（PR 必须同步 base），并要求两个检查：
#     verify（完整回归）与 "PR 分支需包含 base 最新提交"（CI 护栏）。
#   * 不要求 approving review：单维护者仓库无法给自己审批，会把合并锁死。
#   * restrictions 留空（任何有写权限的人仍可直接推送），以便紧急修复；
#     要完全禁止直推 main，把 restrictions 换成下面注释里的写法。

set -eu

repo=${1:-NicholasPython/bybit}

echo "目标仓库：$repo"
echo "先确认仓库可见性与套餐是否允许分支保护……"

payload=$(cat <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["verify", "PR 分支需包含 base 最新提交"]
  },
  "enforce_admins": true,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "restrictions": null,
  "required_pull_request_reviews": null
}
JSON
)

# 完全禁止直推 main 时（把维护者自己也排除，只能走 PR）：
#   "restrictions": {"users": [], "teams": [], "apps": []}

if printf '%s' "$payload" | gh api -X PUT "repos/$repo/branches/main/protection" --input - >/dev/null; then
    echo "已启用 main 的分支保护。"
    gh api "repos/$repo/branches/main/protection" --jq '{
        status: .required_status_checks.contexts,
        strict: .required_status_checks.strict,
        enforce_admins: .enforce_admins.enabled,
        linear: .required_linear_history.enabled,
        force_push: .allow_force_pushes.enabled,
        deletions: .allow_deletions.enabled
    }'
else
    echo "未能启用。当前套餐或仓库可见性不支持该功能，见文件头说明。" >&2
    exit 1
fi
