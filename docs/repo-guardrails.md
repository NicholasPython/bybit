# main 分支保护：现状、约束与做法

## 结论先说

GitHub 提示「Your main branch isn't protected」，**不是配置漏了，而是当前套餐拿不到这个功能**。
本仓库是 **private**，账号套餐下调用分支保护 API 直接被拒：

```
$ gh api repos/NicholasPython/bybit/branches/main/protection
{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature.","status":"403"}
```

`rulesets` 接口返回同样的 403。GitHub 的规则是：**private 仓库的 branch protection / ruleset 需要
GitHub Pro（或 Team / Enterprise）；公开仓库则免费提供。**

因此服务端「禁止强推 / 禁止删除 / 合并前必须过状态检查」这三条，当前**无法**在仓库设置里打开。
可行的路径只有两条，都要账户层面动作：

| 方案 | 代价 | 效果 |
|---|---|---|
| 升级 GitHub Pro | 个人账号 $4/月 | private 仓库完整支持 branch protection 与 ruleset；**顺带解决 Actions 计费闸门**（当前 CI 因 "recent account payments have failed / spending limit" 无法启动 job） |
| 把仓库改成 public | 免费 | 公开仓库免费获得 branch protection / ruleset；代价是**代码对所有人可见** |

升级后一条命令即可开启：

```bash
scripts/enable-branch-protection.sh NicholasPython/bybit
```

## 在没有 Pro 之前，实际生效的护栏

服务端拿不到强制点，就用能在当前套餐下生效的三层手段补上。

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

这两条是**检测**而非**阻止**——CI 跑在推送之后，挡不住已经发生的推送。真正的阻止靠上面的本地钩子与
squash-only 设置。三者叠加的效果接近分支保护，只是少了服务端那道「不满足就不给合并」的硬门槛。

## 还没覆盖的

* **合并前强制状态检查**：CI 会跑回归，但 GitHub 不会因此拦住合并按钮。要真正强制，需要 Pro。
* **禁止他人直接推送 main**：只有维护者一个写权限账号，当前靠约定与钩子；一旦出现第二个写权限账号，
  建议直接升级 Pro 并加上 `restrictions`（见 `scripts/enable-branch-protection.sh` 里的注释）。
* **CODEOWNERS**：private + 免费套餐同样不生效，因此没有添加；升级 Pro 后再加才有意义。

## 相关

* 交付节奏与验收口径见 [acceptance.md](acceptance.md)。
* P0 验收证据见 [p0-acceptance-evidence-2026-11-17.md](p0-acceptance-evidence-2026-11-17.md)。
