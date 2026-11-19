# main 分支保护：现状、约束与做法

## 结论先说

GitHub 提示「Your main branch isn't protected」的根因是**仓库原来是 private，而当前套餐下
branch protection / ruleset 对 private 仓库需要 GitHub Pro**；当时 API 直接返回 403：

```
$ gh api repos/NicholasPython/bybit/branches/main/protection
{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature.","status":"403"}
```

2026-11-18 已将仓库改为 **public**，公开仓库免费提供 branch protection / ruleset。
随后调用 `scripts/enable-branch-protection.sh NicholasPython/bybit` 开启了服务端保护，
并已通过 API 验证：`main` 的 `protected = true`。

| 设置 | 值 |
|---|---|
| `required_status_checks` | `verify`、`PR 分支需包含 base 最新提交`，`strict = true` |
| `enforce_admins` | `true`（管理员同样受保护） |
| `required_linear_history` | `true` |
| `allow_force_pushes` | `false` |
| `allow_deletions` | `false` |
| `required_conversation_resolution` | `true` |
| `required_pull_request_reviews` | 未开启（单维护者仓库无法给自己审批，会锁死合并） |
| `restrictions` | 未开启（保留紧急修复通道；要完全禁止直推见脚本注释） |

公开化的前置检查：扫描了当前工作树与全部历史 blob（密钥、私钥、硬编码密码、token、
内网 IP、邮箱/个人信息），未发现凭据；`secrets/`、`.env`、`config/credentials*.json`
等路径由 `.gitignore` 排除且从未进入 Git。唯一涉及基础设施的是
`docs/runbook-nas.md` 里的 Tailscale 私网域名和 SSH 别名，无凭据。
仓库体积约 15 MB（pack 9.8 MiB），公开后不存在历史大文件泄露问题。

## 仍然生效的补充护栏

服务端保护是主门槛，下面两层继续作为纵深防御。

### 1. 仓库级设置（已应用，GitHub 侧立即生效）

| 设置 | 值 | 作用 |
|---|---|---|
| `allow_squash_merge` | `true` | main 只有一种历史形态 |
| `allow_merge_commit` | `false` | 杜绝 merge commit 造成的历史分叉 |
| `allow_rebase_merge` | `false` | 杜绝 rebase 合并造成的强制推送需求 |
| `delete_branch_on_merge` | `true` | 合并后自动清理分支 |
| `allow_update_branch` | `true` | PR 页面可一键同步 base，减少合并冲突与「落后 main」的 PR |

单一合并方式（squash-only）本身就是一条防强推措施：rebase 合并与 merge commit 才是强推的主要成因，
只留 squash 之后，日常流程不再需要改写已发布历史。

### 2. 本地 git 钩子（`.githooks/pre-push`）

服务端不能拦的，本地可以拦。钩子拒绝：

* 任何带 `+` 的 refspec（强制推送）；
* 任何指向 `refs/heads/main` 的推送，含删除。

紧急情况可以显式放行，且会留下痕迹：

```bash
ALLOW_MAIN_PUSH=1 git push origin main
```

安装（`.git/hooks` 不随仓库分发，每个克隆装一次）：

```bash
scripts/install-git-hooks.sh
```

### 3. CI 护栏（`.github/workflows/branch-guard.yml`）

| 检查 | 触发 | 作用 |
|---|---|---|
| `PR 分支需包含 base 最新提交` | `pull_request` | 等价于分支保护里的 strict 行为：PR 必须先同步 base 才允许合并 |
| `main 只能通过 PR 合入` | `push` 到 `main` | 校验落到 main 的提交是不是某个已合并 PR 的 `merge_commit_sha`；不是就红 |

这两条是**检测**而非**阻止**——CI 跑在推送之后，挡不住已经发生的推送。
服务端保护已经打开，合并按钮会被 status checks 真正拦住；本地钩子与 squash-only 设置则是纵深防御。
需要注意：CI job 目前仍被账户计费闸门挡住（"recent account payments have failed / spending limit"），
状态检查未通过时分支保护会拒绝合并，这是预期的 fail-closed 行为，需账户侧恢复 billing 后才能合入。

## 还没覆盖的

* **CODEOWNERS**：尚未添加；公开仓库可以配置，但目前只有单维护者，实际收益有限。
* **他人直推 main**：`restrictions` 未开启（保留紧急修复通道），目前只有维护者一个写权限账号；
  若新增写权限账号，把 `restrictions` 改为 `{"users": [], "teams": [], "apps": []}` 即可完全禁止直推。

## 相关

* 交付节奏与验收口径见 [acceptance.md](acceptance.md)。
* P0 验收证据见 [p0-acceptance-evidence-2026-11-17.md](p0-acceptance-evidence-2026-11-17.md)。
