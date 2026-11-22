# NAS 部署与运维手册

适用主机：CentOS Stream 9，Podman 5.8.5 rootless，用户 `nicholas`。所有 `ssh nas` 命令使用这台 Mac 已配置的 SSH 别名。

当前部署为 **paper 模拟环境**。两策略禁用、首次风险基线未建立、执行者等待人工接管确认，实盘门槛关闭。下列启用、接管与轮换步骤是操作说明，本轮没有代为执行这些交易控制。

- [验收矩阵](acceptance.md)
- [需求与原始验收条件](requirements.md)

## 1. 部署布局

| 项目 | 位置 |
|---|---|
| HTTPS 管理入口 | `https://nicholas-4.tail068b64.ts.net/`，仅 tailnet 可达 |
| 管理进程 | `127.0.0.1:18080`，由 Tailscale Serve 反向代理 |
| PostgreSQL | `127.0.0.1:55432`，库 `bybitbot_paper` |
| 应用镜像 | `localhost/bybitbot:0.1.0`；发布前记录完整 image ID |
| 基础运行时镜像 | `localhost/bybitbot-runtime:0.1.0` |
| 数据库镜像 | 复用已有 `localhost/artex-postgres:16` 镜像，使用独立的 Bybit 命名卷 |
| 数据库 schema head | `c9d3f7a2b6e4` |
| Quadlet 源文件 | `~/.config/containers/systemd/bybitbot-*.container` 和 `.volume` |
| 运行配置、数据库环境变量、管理身份 | `~/.config/bybitbot/{config.yaml,postgres.env,admin.json}`，0600 |
| 备份加密公钥 | `~/.config/bybitbot/backup-public.asc` |
| worker 运行目录 | `~/.local/share/bybitbot/var/` |
| NAS 构建上下文 | `~/.local/share/bybitbot/build/` |
| NAS 加密备份 | `~/.local/share/bybitbot/backups/` |
| NAS 备份脚本 | `~/.local/share/bybitbot/bin/backup.sh` |
| NAS 行情出口代理 | `http://127.0.0.1:7890` |

NAS 为 2 核、约 29 GiB 内存；这与需求中的 4 vCPU/8 GiB 性能参考环境不同。容器限制 memory/pids；rootless cgroup 未委托 CPU controller，因此当前单元没有 CPU 限制。

OKX 采集与 ARTEX 两个服务保持停止。复用其镜像不需要启动它们。

## 2. 访问与日常检查

在 tailnet 内打开管理入口。用户名 `admin`，初始密码保存在这台 Mac 的 `secrets/nas/admin-password.txt`；文件内容不写入本手册或终端输出。

```bash
ssh nas 'systemctl --user is-active bybitbot-postgres.service bybitbot-worker.service bybitbot-web.service bybitbot-backup.timer'
```

```bash
ssh nas 'podman ps --format "{{.Names}} {{.Status}}"'
```

```bash
ssh nas 'podman logs --tail=50 bybitbot-worker'
```

```bash
curl --fail --silent --show-error https://nicholas-4.tail068b64.ts.net/health
```

`/health` 从数据库读取 worker 心跳，年龄达到 120 秒返回 HTTP 503。Mac 每 30 秒检查一次，连续三次失败后尝试通知；从 worker 停止到通知还包含上述 120 秒新鲜度窗口及请求耗时。健康检查不证明交易所挂单已撤销或仓位已平。

```bash
ssh nas "podman exec bybitbot-pg psql -U bybitbot -d bybitbot_paper -Atc \"SELECT value->>'state',value->>'lease',value->>'equity',value->>'tick_at',value->>'last_error' FROM runtime_state WHERE key='status'; SELECT name,enabled FROM strategy ORDER BY name; SELECT count(*) FROM execution;\""
```

容器内 `psql` 使用本地 socket，命令不携带数据库口令。执行者实际互斥来自 PostgreSQL session advisory lock；旧版遗留的多条租约审计行不能当作多个活动执行者。

## 3. 首次安装与启动

本轮 NAS 已安装并启用数据库、worker、web 的 Quadlet 及备份 timer，用户 linger 已开启。现有配置和数据不可用初始化脚本覆盖。

为另一套全新 paper 部署生成文件时，在项目根目录使用空目录：

```bash
uv run python deploy/bootstrap.py --directory secrets/new-nas
```

脚本拒绝非空目录；生成独立数据库口令、管理密码及默认禁用策略的配置。配置含本 NAS 的代理与路径，迁移到其他主机前先调整。文件以受限权限复制到 NAS；`postgres.env` 和 `config.yaml` 必须使用同一数据库口令。

安装 `deploy/quadlet/` 中的单元、准备镜像、备份脚本与公钥之后：

```bash
ssh nas 'systemctl --user daemon-reload && systemctl --user start bybitbot-postgres.service bybitbot-worker.service bybitbot-web.service'
```

worker 启动前执行数据库迁移；数据库不可达时启动可能失败，排除原因后重试。每次 worker 重启均丢失人工发单资格，需要重新对账并确认接管。首次接入还须显式建立风险起点；已有风险基线不能反复初始化或用当前净值覆盖。

**永久停用与临时停止不同。** 临时停止使用 `systemctl --user stop`。永久取消自启要修改/移出 Quadlet 源文件后 `daemon-reload`，generated unit 的 `disable` 不能替代源文件修改。依赖关系仍可能拉起服务；停机前核对挂单和仓位，停服务不会自动平仓。

## 4. 发布与回滚

先给运行中的镜像保存独立标签，再构建候选镜像，通过配置检查后才移动服务使用的标签。以下以 `release-20261031-r2` 为示例；下一次发布换用新的唯一名称。

一次完成的脚本形式（推荐，单次 SSH 往返完成全部步骤，包含迁移与只读核验，不做接管确认/策略启用/基线初始化）：

```bash
bash deploy/release-nas.sh release-YYYYMMDD
```

下列分步命令是脚本做的事，需要逐步观察或排障时使用：

```bash
ssh nas 'podman tag localhost/bybitbot:0.1.0 localhost/bybitbot:rollback-before-release-20261031-r2'
```

同步时保留 `src/`、`alembic/`、`config/`、`deploy/` 层级，只复制下列无密钥文件；不使用覆盖整个远端目录的 `--delete`：

```bash
rsync -azR --exclude '__pycache__/' src/ alembic/ alembic.ini config/config.example.yaml deploy/Containerfile.nas deploy/Containerfile.nas-runtime deploy/requirements.lock nas:.local/share/bybitbot/build/
```

```bash
ssh nas 'podman build --pull=never -f "$HOME/.local/share/bybitbot/build/deploy/Containerfile.nas" -t localhost/bybitbot:release-20261031-r2 "$HOME/.local/share/bybitbot/build"'
```

```bash
ssh nas 'podman run --rm --network=host -v "$HOME/.config/bybitbot:/run/bybitbot:ro,z" localhost/bybitbot:release-20261031-r2 --config /run/bybitbot/config.yaml check'
```

```bash
ssh nas 'podman tag localhost/bybitbot:release-20261031-r2 localhost/bybitbot:0.1.0 && systemctl --user restart bybitbot-worker.service bybitbot-web.service'
```

配置检查不是行情、账户或对账验收。启动后重复第 2 节检查，记录镜像 ID、schema head、心跳、暂停与订单状态。

2026-11-01 整改发布已核验：worker/web 镜像为 `474fbae0c7c501d3dd7fca2ec996c1536bc3c3c2c9b1549c87215eb5dcb7151f`，发布标签 `localhost/bybitbot:dogfood-20261101`；53 个应用、页面、迁移及配置示例文件与本地 SHA-256 一致。上一镜像为 `c1d89f9541d9b86f8b67ee173933b73f86808c48f135db95e1a55475937de049`，已保存为 `localhost/bybitbot:pre-dogfood-20261101`。更早的 `pre-valuation-20261031` 和 `rollback-20261031` 标签仍保留。当前保持 paper 暂停、基线未建立、等待接管确认，两策略禁用，订单/成交/正库存均为零。

本次配置变更将 `paper.fee_version`、`paper.spot_fee_rate`、`paper.option_fee_rate` 移至顶层 `fees` 公式配置；原文件以 0600 保存在 `~/.config/bybitbot/config.pre-dogfood-20261101.yaml`。无交易的旧 paper 检查点仅迁移费用元数据，保留 1000 USDT 余额，并在 `runtime_state` 的 `paper-fee-migration` 保存原检查点。存在订单、成交、交割或非零持仓的旧检查点禁止自动迁移；不得以清空数据或重置余额绕过。

发布前数据库加密备份 `bybitbot-20261101T035525Z.dump.gpg` 已成功生成并复制到 Mac 的异机备份目录。数据库迁移新增持有周期字段，不猜测回填历史库存；旧库存缺起点时自动退出保持待核实。

回退到 `pre-dogfood-20261101` 前，必须同时核对旧配置格式与旧 paper 检查点兼容性；仅切镜像标签不足以回滚本次费用口径。若发布后已有交易事实，先保持暂停并审计，不能直接用旧检查点覆盖新账本。完整整改记录见 [remediation-2026-11-01.md](remediation-2026-11-01.md)。

2026-11-02 期权收紧发布已核验：镜像 `a9bc0bca0a096c1097cef1d993bc272ed887511ef2b0885144b4e593e69f5f37`，发布标签 `localhost/bybitbot:option-controls-20261102`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:pre-option-controls-20261102`（`5ea93d176eca`）。配置原文件以 0600 保存为 `~/.config/bybitbot/config.pre-option-controls-20261102.yaml`。本次收紧 `eth-call-weekly` 的到期窗口、Delta 区间、行权价带、权利金与费用占比上限，并新增权利金止损、持有期限、ETH 权利金与 Delta 上限、权利金占权益比例上限；策略生成 v2 配置版本后停在对账暂停，`CONFIG_CHANGED` 锁定暂停未解除，两策略仍禁用，订单/成交/意图/库存均为零。改动依据与逐项验证见 [option-controls-2026-11-02.md](option-controls-2026-11-02.md)。

2026-11-04 复核补丁发布已核验：镜像 `abb970cb269ffde3c1ffc99cfab677be3af378694150e6021db723e9a964e86f`，发布标签 `localhost/bybitbot:option-controls-20261104-r2`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:pre-option-controls-guard-20261104`（`a9bc0bca0a096c1097cef1d993bc272ed887511ef2b0885144b4e593e69f5f37`，即 11-02 发布镜像）。同日中间镜像 `localhost/bybitbot:option-controls-20261104`（`edff2cbaedfb`）已被 r2 取代，不再使用。配置本次未变，仍以 0600 保存在 `~/.config/bybitbot/config.yaml`；`config check` 通过（两个策略 `enabled: false`、`live_allowed: false`）。补丁只补两处缺口：权利金止损在配置期就拒绝挂在现货策略上（现货按买一价判定权利金成本，永远不会触发），以及操作台的退出规则只展示业务字段（重试次数、有效期等执行细节不再淹没阈值）。配置版本仍为 v2，未写入新版本；运行状态 `PAUSED_OPEN`、无 `last_error`，`CONFIG_CHANGED`（锁定）、`NOTIFICATION_FAILURE`、`VALUATION_UNAVAILABLE` 三条暂停均未解除，订单/成交/意图/库存全部为零。

2026-11-18 版本追赶发布已核验：镜像 `2bab4ab7b726949039caa6160b32c3b8e752d93841760cd51974f4b60bba48c8`，发布标签 `localhost/bybitbot:release-20261118-r3`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:rollback-before-release-20261118`（`abb970cb269ffde3c1ffc99cfab677be3af378694150e6021db723e9a964e86f`，即 11-04 发布镜像）。中间镜像 `release-20261118`（`2f59af038c53`）与 `release-20261118-r2`（`6579372ece94`）仅因构建上下文混入 `.DS_Store` 与 `__pycache__` 而被取代，应用代码与 r3 完全一致，不再使用；`src/.DS_Store` 本就在 `.gitignore`，是同步与构建上下文未排除导致进入镜像，已在仓库根目录新增 `.containerignore`（必须用 `**/__pycache__/` 与 `**/.DS_Store`，单层模式只覆盖构建根目录）。

本次发布把 NAS 从 11-04 的 `abb970cb269f` 追到本地 `34c0f44`（`feat/p0-acceptance-evidence-20261117`，含 main 的 `2b79bd6` 校验生命周期合并），跨度约两周：新增持久化校验采样/执行/方向库存/账本链、两腿价差合成执行、ops 的时钟/指标/限流/留存模块、按 AST 审计修复的对账会话复用缺陷。数据库从 `8a4d17c2f905` 升到 `d438b9a83a10`（两级迁移一次性执行成功，均事务性 DDL）。发布前加密备份 `bybitbot-20261118T023318Z.dump.gpg`（6,846,946 字节）已生成并复制到 Mac 的 `secrets/offsite-backups/`，两端 SHA-256 一致为 `dc2a70856c7e41ac26597e63d3b668f4336a109e2b0fc2d0ac255a68ab6130a0`。87 个应用/迁移/配置示例文件本地与 NAS 的 SHA-256 全部一致；运行时基础层重建缓存命中、镜像 ID 保持 `0590a0bde8c1`，证明依赖锁（2026-10-31 后未变）与运行时一致。

发布前旧 worker 自 2026-11-14 21:26:00 起每周期 `交易服务周期失败：TransientError`（`risk_pause` id 89），`reconciled_at` 冻结、`account_snapshot` 停增。升级后该暂停被自动解除（`resolved_by=system:technical-resync`），心跳恢复推进、`last_error` 为空、`account_snapshot` 重新增长、`valuation_complete=true`、`account.complete=true`。**根因未最终定位**：在旧镜像上手工重放完整 tick 连续三次成功，说明不是配置或网络层面的确定性故障，更像是旧版本特有的偶发路径（旧 tick 的调度与校验生命周期代码与新版不同）。本次以"升级后连续观察约 20 分钟无复发"作为缓解证据，不宣称根因已修复；若再出现同型 `SERVICE_FAILURE`，按本手册第 2 节抓 `last_error` 与对账快照再查。

当前保持：`state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION`、`baseline_available=false`，两条策略 `enabled=false`，订单/成交/意图/非零库存全为零；`CONFIG_CHANGED`（锁定）、`NOTIFICATION_FAILURE`、`VALUATION_UNAVAILABLE` 三条既有暂停**未解除**，未代为执行接管确认、风险基线初始化或策略启用。`/health` 在 NAS 本机 `127.0.0.1:18080` 与 Tailscale 入口 `https://nicholas-4.tail068b64.ts.net` 均返回 HTTP 200；控制台 `/api/status` 登录后返回完整数据，心跳 `fresh=true`。配置本次未变。

2026-11-18 控制台目标对齐发布已核验：镜像 `52e9022d2b83d7b6368c57fb65af035eb67353f15766a9599a696b2b669a3b42`，发布标签 `localhost/bybitbot:release-20261118-console-v1`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:rollback-before-release-20261118-console-v1`（`2bab4ab7b726949039caa6160b32c3b8e752d93841760cd51974f4b60bba48c8`，即同日版本追赶发布镜像）。本次使用 `deploy/release-nas.sh` 一次 SSH 往返完成发布（备份之后的所有步骤），脚本的迁移步骤因已是 head 而空跑成功。发布前加密备份 `bybitbot-20261118T060507Z.dump.gpg`（7,001,641 字节）已生成；异机复制时 `scp` 两次静默截断（3,394,560 / 3,916,800 字节），改用 `rsync --partial` 重传后才得到完整文件，两端 SHA-256 均为 `b3938d7c2692aee0e86a55a3c75bb5e29868a2527115824ba8be20ead11196af`。**以后异机备份复制一律用 rsync 并核对哈希，不用 scp 的静默成功判断完整性。**

应用变化：操作台从六个数据实体 tab 改为五个运维目标（对账与恢复/运行健康/风控态势/交易明细/人工控制），对账与恢复为默认首屏；新增恢复结论、证据链（对账水位/游标、UNKNOWN 订单、锁定暂停、租约、账本锚点）与四步引导；命令按钮带前置条件提示；审计 CSV 改为 fetch 下载并显式提示截断；表格按内容签名跳过无变化的重建。`/api/status` 只补已落库事实的只读字段（`clock_checked`/`capabilities`/`latency`/`retention`、对账 `watermark`/`cursor`、`cash_flows[]`），无新写接口，命令语义未变。

发布后核验：schema head 仍为 `d438b9a83a10`；心跳持续推进（06:09:31 → 06:10:00 UTC），`last_error` 为空，`held=false`、账户 `complete=true` 且 `stale=false`；`/health` 在 NAS 本机与 Tailscale 入口均 HTTP 200；`/api/status` 登录后返回完整数据，新增字段均已上线（`cash_flows` 与 latency 为空、watermark/cursor 为 null 属 NAS 当前无对应记录，不是渲染故障）。保持 `state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION`、`baseline_available=false`，两策略禁用，订单/成交/意图/非零库存均为零；`CONFIG_CHANGED`（锁定）、`NOTIFICATION_FAILURE`、`VALUATION_UNAVAILABLE` 三条暂停未解除。未代为执行接管确认、风险基线初始化或策略启用。

依赖锁变化时先重建基础层；该文件复用 NAS 已有的 Python 3.12.15 基础镜像，并通过 `--require-hashes` 安装依赖：

```bash
ssh nas 'podman build --pull=never -f "$HOME/.local/share/bybitbot/build/deploy/Containerfile.nas-runtime" -t localhost/bybitbot-runtime:0.1.0 "$HOME/.local/share/bybitbot/build"'
```

本轮标准 `Containerfile` 在 Mac 的构建停在 Docker Hub Python 基础镜像拉取，原因为 TLS 握手超时；不能据此声称标准镜像已构建成功。NAS 版本使用本地基础层另行构建并验证。

回滚代码前确认旧代码能读取当前 schema；数据库迁移与新写入的数据不会随镜像标签回退。不能笼统假定所有后续迁移都向前兼容。

```bash
ssh nas 'podman tag localhost/bybitbot:rollback-before-release-20261031-r2 localhost/bybitbot:0.1.0 && systemctl --user restart bybitbot-worker.service bybitbot-web.service'
```

回滚后继续保持暂停，核对在途请求与对账，再决定是否重新确认接管。

## 5. 加密备份与恢复

### NAS 每日备份

`bybitbot-backup.timer` 在 NAS 本地时区每天 03:23 触发，随机延迟至多 300 秒，`Persistent=true`。`backup.sh` 运行 `pg_dump -Fc` 并直接流式加密，成功后将 `.partial` 原子改名为 `bybitbot-<UTC>.dump.gpg`。当前脚本只备份 `bybitbot_paper`。

```bash
ssh nas 'systemctl --user start bybitbot-backup.service && systemctl --user list-timers bybitbot-backup.timer --no-pager'
```

数据库 dump 不含数据库角色、运行配置、管理员身份和私钥；恢复这些文件需要单独受限保管。NAS 只有加密公钥。

### Mac 心跳与备份复制

安装器把脚本、状态、备份和日志放在 `~/.local/share/bybitbot-monitor/`。实际观察到 Desktop 路径下的 launchd 任务无输出、无状态，改用该目录后正常结束；未单独取得系统隐私子系统的拒绝日志，因此不将具体原因视为已证实。

```bash
uv run python deploy/install_watch.py --health-url 'https://nicholas-4.tail068b64.ts.net/health' --rsync-source 'nas:.local/share/bybitbot/backups/*.dump.gpg'
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.bybitbot.nas-heartbeat.plist
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.bybitbot.offsite-backup.plist
```

已加载的任务更新前先 `bootout` 对应标签再重新 `bootstrap`。两个任务分别每 30 秒、1800 秒运行，避免慢 rsync 阻塞心跳。rsync 的权限参数兼容这台 Mac，复制后的 dump 为 0600，目录为 0700。

检查 `state/nas-watch.json` 的时间是否继续前进，以及 `state/backup-sync.json` 的 `backup_sync_ok`；仅有 launchd 进程不代表同步成功。备份年龄按文件名中的 dump 时间计算，超过 24 小时尝试通知。

Mac 睡眠、关机或离线时监控与复制会中断；本地通知还受 macOS 通知权限和专注模式影响。本轮已验证实际心跳检查和加密复制，未验证完整失联通知展示，也未配置业务告警的远端 HTTPS 渠道，不能认定已满足无人值守告警要求。

### 私钥

本机 GnuPG home：`secrets/backup-gnupg/`（0700）；指纹：

```text
CCA09EDDB905C807F03D697A1D9AEFAD85C1BB92
```

加密文件没有私钥就无法恢复。另存受限离线副本；换钥时保留旧私钥用于历史备份，直到这些备份也已按保留政策退役。私钥、口令和运行配置不进入 Git。

### 在本机独立库重复恢复演练

从异机复制目录选择实际文件，下面使用本轮已核验的时间戳。`umask`、解密、恢复与清理在同一个 shell 块内运行；`--exit-on-error` 使失败退出，`--no-owner --no-privileges` 避免 NAS 角色在本机不存在造成属主/ACL 错误。

```bash
(
  set -eu
  umask 077
  drill_dir=$(mktemp -d)
  drill_db="bybitbot_restore_drill_$(date -u +%Y%m%dT%H%M%S)_$$"
  trap 'rm -rf "$drill_dir"' EXIT
  GNUPGHOME="$PWD/secrets/backup-gnupg" gpg --quiet --decrypt \
    "$HOME/.local/share/bybitbot-monitor/backups/bybitbot-20261031T105346Z.dump.gpg" \
    > "$drill_dir/restore.dump"
  podman exec bybitbot-pg createdb -U postgres "$drill_db"
  podman exec -i bybitbot-pg pg_restore -U postgres --exit-on-error --no-owner --no-privileges \
    -d "$drill_db" < "$drill_dir/restore.dump"
  podman exec bybitbot-pg psql -v ON_ERROR_STOP=1 -U postgres -d "$drill_db" -Atc \
    'SELECT version_num FROM alembic_version; SELECT count(*) FROM strategy; SELECT count(*) FROM runtime_state; SELECT count(*) FROM execution;'
  podman exec bybitbot-pg dropdb -U postgres "$drill_db"
)
```

2026-10-31 已验证：解密/恢复成功，schema head `786d42c1b808`，策略 2 条、运行态 2 条、成交 0 条。演练数据库与明文 dump 已清理；若上述命令在建库后失败，先核对失败原因和目标库名再单独清理该演练库。

这是空成交 paper 数据的恢复演练；尚未覆盖“备份后真实成交回填”、只读服务启动耗时或完整 RTO/RPO 测量。恢复数据库不得启动第二个能发单的 worker。正式恢复后核对完整历史、身份、在途订单、持仓、现金与预留，保留已有基线及历史高点；历史无法证实时保持暂停，不能通过重新初始化基线消除差异。

## 6. 密钥轮换

2026-11-22 双腿套利阶段二发布已核验：代码提交 `bcd5d92` 已推送至 `origin/feat/spot-lab-module`；镜像 `297fdf917162172b3284333a9830098b71ce7f3dce1959b96848ef4e67f6d482`，发布标签 `localhost/bybitbot:release-20261122-spread-v1`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:rollback-before-release-20261122-spread-v1`（`cd8448e1b1cb1ea622c86d00a693a0011485488697ff54e4df974999da6d8557`）。依赖锁未变，基础层未重建。提交前完整 Python 回归 1788 项通过、页面测试 24 项通过。

发布前使用新版 `backup.sh` 生成 `bybitbot-20261122T071709Z-bybitbot_paper.dump.gpg`（8,879,642 字节），脚本已在容器内通过 `pg_restore --list` 验证归档可恢复，随后复制到 NAS 的 `~/.local/share/bybitbot/bin/backup.sh`。发布过程先把数据库从 `d438b9a83a10` 升至 `a612e47bc903`（两级事务性 DDL），再切换服务镜像。切换瞬间旧镜像 worker 的 `ExecStartPre` 迁移无法识别新 head，容器以 137 退出；已先将 `0.1.0` 暂时指回旧镜像恢复心跳，再重新指向新镜像并重启。最终 worker 持续运行、无自动重启，`NRestarts=0`；心跳从 `07:22:16Z` 持续推进，`last_error` 为空， NAS 本机与 Tailscale `/health` 均 HTTP 200。

当前仍保持 `state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION`、`baseline_available=false`，活动暂停为 `CONFIG_CHANGED`（锁定）、`MANUAL`、`NOTIFICATION_FAILURE`、`VALUATION_UNAVAILABLE`，两条旧策略均禁用；物理订单、成交、意图、非零库存及套利配对/腿单/逐笔成交均为零。套利配置默认关闭，未确认接管、未建立风险基线、未启用策略，也未使用任何交易凭据。回滚到 `cd8448e1b1cb` 前必须先把 schema 降回 `d438b9a83a10`，否则旧镜像的启动前迁移无法识别新 revision；仅切换镜像标签不足够。

2026-11-22 双腿套利阶段三发布已核验：代码提交 `763820d` 已推送至 `origin/feat/spot-lab-module`；镜像 `d1b98c8d3b2035e4168c1907a1b087f3c4870131cff3f3e5acd4959a16ab4483`，发布标签 `localhost/bybitbot:release-20261122-spread-v2`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:rollback-before-release-20261122-spread-v2`（`297fdf917162172b3284333a9830098b71ce7f3dce1959b96848ef4e67f6d482`）。提交前完整 Python 回归 1833 项通过，Ruff、`git diff --check` 和 Alembic 迁移收敛检查通过。

发布前使用既有 `backup.sh` 生成 `bybitbot-20261122T090632Z-bybitbot_paper.dump.gpg`（8,920,926 字节），脚本已通过 `pg_restore --list` 验证归档可恢复。发布先停 worker，再将数据库从 `a612e47bc903` 升至 `b7c9e1f4a2d8`（新增 `spread_signal_sample`），随后切换镜像并重启。最终 worker/web/postgres 均 active，`NRestarts=0`，心跳持续推进，`last_error` 为空，NAS 本机与 Tailscale `/health` 均返回 `{"status":"ok"}`。

当前仍保持 `state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION`、`baseline_available=false`，活动暂停为 `CONFIG_CHANGED`（锁定）、`MANUAL`、`NOTIFICATION_FAILURE`、`VALUATION_UNAVAILABLE`，两条旧策略均禁用；物理订单、成交、意图、非零库存、套利配对/腿单/逐笔成交及信号样本均为零。阶段三自动信号配置默认关闭，未确认接管、未建立风险基线，也未使用任何交易凭据。回滚到 `297fdf917162` 前必须先把 schema 降回 `a612e47bc903` 并删除 `spread_signal_sample`，否则旧镜像的启动前迁移无法识别新 revision；仅切换镜像标签不足够。

2026-11-22 影子盘与管理页发布已核验：代码提交 `c9da8f4`；镜像 `2e956ebac8df451e913e579be1f78eecbdc13faf987a10b315ffffca42b00841`，发布标签 `localhost/bybitbot:release-20261122-shadow`（已移动为 `0.1.0`），回滚点 `localhost/bybitbot:rollback-before-release-20261122-shadow`（`d1b98c8d3b2035e4168c1907a1b087f3c4870131cff3f3e5acd4959a16ab4483`，即阶段三发布镜像）。提交前完整 Python 回归 1839 项通过（新增影子盘 6 项），Ruff、`git diff --check` 与迁移收敛检查通过。

发布先停 worker，再把数据库从 `b7c9e1f4a2d8` 升至 `c9d3f7a2b6e4`（新增 `shadow_trade`），随后切换镜像并重启。最终 worker/web/postgres 均 active，`NRestarts=0`，tick 从 `10:12:50` 起正常推进，`last_error` 为空，NAS 本机与 Tailscale `/health` 均返回 `{"status":"ok"}`。

当前仍保持 `state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION`、`baseline_available=false`，四条活动暂停未解除，两条旧策略均禁用；物理订单、成交、意图、非零库存及套利配对/腿单/逐笔成交/信号样本均为零，`shadow_trade` 为空表。`spread.shadow.enabled` 默认关闭（NAS 配置未加 `spread.shadow` 段，走默认值），影子盘未采集任何样本，管理页「影子盘」标签上线并显示关闭状态。回滚到 `d1b98c8d3b20` 前必须先把 schema 降回 `b7c9e1f4a2d8` 并删除 `shadow_trade`，否则旧镜像的启动前迁移无法识别新 revision；仅切换镜像标签不足够。影子盘启用需另行在配置中打开 `spread.shadow.enabled`，且仅消费公开行情，不影响交易凭据边界。

2026-11-22 影子盘启用与采样修复发布已核验：代码提交 `4b9408b` 已推送至 `origin/feat/spot-lab-module`；发布标签 `release-20261122-shadow-v10`（已移动为 `0.1.0`），schema 仍为 `c9d3f7a2b6e4`（无新迁移）。提交前完整 Python 回归 1845 项通过，Ruff 通过。

本轮首先在 NAS 配置启用 `spread.shadow.enabled=true`（配对 `BTCUSDT:ETHUSDT` 保持 `enabled=false`，仅作影子盘采样对象），随后连发 v5–v10 排障版本定位「影子盘开启但样本始终为 0」的根因链：中间价落在半档被 tick 对齐误拒（`2e8763d`）、采样拒绝静默无痕（限频警告，`85236fc`）、单腿行情失败拖垮整轮缓存（逐符号容错，`f543ee1`），最终 v10 确认真因——worker tick 后段耗时约 33 秒（p50），`_shadow_tick` 排在末尾时 `_market()` 拉的行情已超过 `max_quote_age_seconds=15`，缓存有数据但永远判过期。修复：`_shadow_tick` 提前到 `_market()` 之后，且缓存过期腿用公共只读适配器（`allow_trade_writes=False`）当场刷新（`4b9408b`）。

启用后影子盘仅消费公开行情（REST ticker/orderbook），不加载交易凭据、不发私有请求、不产生真实订单，样本与模拟交易仅落 `spread_signal_sample`/`shadow_trade` 的 `environment='shadow'` 命名空间，管理页「影子盘」标签只读展示，不自动改参数、不推进 canary 或实盘。worker/web 保持 active，`last_error` 为空，四条活动暂停与禁用策略不变。

### Bybit API 凭据

先暂停新增风险并核对所有在途订单；需要撤单时，必须取得终态及累计成交证据。未知结果期间不能靠换 key 清空预留。准备只含必要权限、关闭提现、绑定固定出口 IP 的新凭据，以 0600 文件注入，核对实际环境、UID、权限和账户模式后再重启。重启后重新对账及接管确认，逐条满足恢复条件。

本轮未把 Bybit 凭据接入服务；仅在 2026-11-01 用一把真实**只读** key 做过一次读取链路核验（主账户 UID 587220933，`readOnly=1`，无 IP 限制，净值约 0.02 USDT），并因此修复了钱包余额中空 `accruedInterest` 导致的账户读取失败。写路径（下单、撤单、交割）仍无真实账户演练，换用可写凭据前需先补齐外部资金流登记命令。失效或泄漏的旧 key 在交易所侧撤销；保存订单编号与审计，不删除本地订单记录。

### 管理身份

先在可写挂载中生成新文件，命令拒绝覆盖已有目标：

```bash
ssh nas 'podman run --rm -v "$HOME/.config/bybitbot:/run/bybitbot:rw,z" localhost/bybitbot:0.1.0 init-admin --path /run/bybitbot/admin.json.new --generate-password-file /run/bybitbot/admin-password.txt.new'
```

核对新文件权限后原子替换 `admin.json`，重启 web 使旧会话失效；完成新密码登录验证后，受限保管新密码并清理退役副本。不要在对话或日志中输出密码。本轮未轮换现有管理身份。

## 7. 异常处置与实现边界

- `SUBMITTING / UNKNOWN` 表示结果未确定，不等于未发送。保留预留，按 `orderLinkId` 和成交历史补查；单次空查询或重复编号报错不足以认定拒绝。人工恢复不能代替交易证据。
- 撤单受理不等于最终撤销；累计成交入账完整前不释放相应资金。
- 管理页受限减仓仍需身份、租约、可信持仓、库存预留、价格下限和可写审计。退出失败时展示余量，不把关机当作退出。
- 自动退出按策略/合约聚合库存管理：零库存后的第一笔实际买入建立周期，追加买入保留起点，全平后新买入重新计时，退出重建共享有效期和次数。历史库存缺少周期归属时保持待核实；未实现逐笔 FIFO。
- 真实交割解析已接入 delivery-record、DELIVERY 资金流水及必要的零支付平仓证明。完整持仓、归属数量、先前可信快照的现金变化均一致后才释放成本并独立记 SETTLEMENT；持仓消失本身不能替代交割证据。普通 execution 继续只接受 Trade。缺证据保持暂停并在后续轮询核对；Demo/Testnet 实际交割能力尚未验证。
- 运行服务当前使用 REST 轮询；WebSocket 适配器有契约测试，尚未作为 worker 的主行情链路集成。
- 业务告警渠道为空时只落库；真实告警送达、数据保留清理、容量/性能、真实 Demo/Testnet 期权能力仍有验收缺口，详见矩阵。

实盘启用必须另行取得适用阶段验收证据和账户所有者的启用操作；本轮部署保持 paper 暂停。
