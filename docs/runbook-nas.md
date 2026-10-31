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
| 数据库 schema head | `786d42c1b808` |
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

2026-10-31 本轮发布已核验：worker/web 镜像为 `c1d89f9541d9b86f8b67ee173933b73f86808c48f135db95e1a55475937de049`，41 个 Python 源文件与本地 SHA-256 完全一致。上一版本保存在 `localhost/bybitbot:pre-valuation-20261031`（`910874d2cf3b853ad9758f49b95a812eb2561a141aa5969f0eb79202f8c787c1`），更早版本保存在 `localhost/bybitbot:rollback-20261031`（`710a347cad5135f56c16d6b77c84034d2cbc16cc091d43277f01f68c712a3003`）。这些是已存在的标签；上面的 `rollback-before-release-20261031-r2` 仅为后续发布操作示例。

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

### Bybit API 凭据

先暂停新增风险并核对所有在途订单；需要撤单时，必须取得终态及累计成交证据。未知结果期间不能靠换 key 清空预留。准备只含必要权限、关闭提现、绑定固定出口 IP 的新凭据，以 0600 文件注入，核对实际环境、UID、权限和账户模式后再重启。重启后重新对账及接管确认，逐条满足恢复条件。

本轮未接入 Bybit 凭据，以上轮换尚未做真实账户演练。失效或泄漏的旧 key 在交易所侧撤销；保存订单编号与审计，不删除本地订单记录。

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
- 自动退出当前按策略/合约聚合库存管理，退出有效期和次数保持不变；不是逐笔 FIFO 持仓批次。真实期权交割解析尚未接入，期权消失/到期不会自动释放成本；paper 有显式结算注入及独立 SETTLEMENT 记账。
- 运行服务当前使用 REST 轮询；WebSocket 适配器有契约测试，尚未作为 worker 的主行情链路集成。
- 业务告警渠道为空时只落库；真实告警送达、数据保留清理、容量/性能、真实 Demo/Testnet 期权能力仍有验收缺口，详见矩阵。

实盘启用必须另行取得适用阶段验收证据和账户所有者的启用操作；本轮部署保持 paper 暂停。
