# NAS 部署与运维手册

适用主机：CentOS Stream 9，Podman 5.8.5 rootless（`systemd --user` + Quadlet），用户 `nicholas`。
当前部署为 **paper 模拟环境**，策略禁用、实盘门槛关闭。手册中的"启用""接管""基线"等操作都需要人工在管理页确认，本文档只描述步骤，不代替执行。

- 验收现状与缺口：[docs/acceptance.md](docs/acceptance.md)
- 需求口径：[docs/requirements.md](docs/requirements.md)

---

## 1. 当前部署事实（2026-10-31）

| 项 | 值 |
|---|---|
| 主机 | `nicholas-4.tail068b64.ts.net`（NAS，192.168.1.3，Tailscale 内网） |
| 应用镜像 | `localhost/bybitbot:0.1.0` = `910874d2cf3b853ad9758f49b95a812eb2561a141aa5969f0eb79202f8c787c1` |
| 回滚镜像 | `localhost/bybitbot:rollback-20261031` = `710a347cad5135f56c16d6b77c84034d2cbc16cc091d43277f01f68c712a3003` |
| 运行时基础镜像 | `localhost/bybitbot-runtime:0.1.0` = `0590a0bde8c19590e5f6f3b5459a111c8541c45cb9ac05049e55b038f15b0dbe` |
| 数据库镜像 | `localhost/artex-postgres:16`（复用已有镜像，不新建） |
| 迁移 head | `786d42c1b808` |
| 管理入口 | `https://nicholas-4.tail068b64.ts.net/` → `127.0.0.1:18080`（Tailscale Serve，tailnet only） |
| 数据库 | `127.0.0.1:55432`，库 `bybitbot_paper` |
| 出口代理 | `http://127.0.0.1:7890`（mihomo） |
| 运行态 | `PAUSED_OPEN / NEEDS_CONFIRMATION`，权益 1000 USDT，无基线，两策略 disabled，成交 0 条 |
| 其他服务 | ARTEX（`artex.service` / `artex-postgres.service`）保持 inactive；OKX 采集已停用 |

> `PAUSED_OPEN / NEEDS_CONFIRMATION` 是预期状态：worker 已持有执行者租约，但尚未确认接管、没有风险基线，因此不会开仓。

### 主机上的文件位置

| 路径 | 内容 | 权限 |
|---|---|---|
| `~/.config/bybitbot/config.yaml` | 运行配置（含数据库口令） | 0600 |
| `~/.config/bybitbot/postgres.env` | PostgreSQL 环境变量 | 0600 |
| `~/.config/bybitbot/admin.json` | 管理身份（PBKDF2） | 0600 |
| `~/.config/bybitbot/backup-public.asc` | 备份加密公钥 | 0600 |
| `~/.config/containers/systemd/*.container` | Quadlet 单元（真源） | 0644 |
| `~/.local/share/bybitbot/bin/backup.sh` | 备份脚本 | 0700 |
| `~/.local/share/bybitbot/backups/` | 加密备份 `bybitbot-<UTC>.dump.gpg` | 0700 目录 |
| `~/.local/share/bybitbot/var/` | worker 运行期数据 | — |

**Quadlet 源文件才是开关。** 只 `systemctl --user stop bybitbot-worker` 不改源文件，下次开机或 `daemon-reload` 后会重新拉起；要真正禁用，编辑 `~/.config/containers/systemd/*.container` 的 `[Install]` 段（移除 `WantedBy=default.target`）后 `systemctl --user daemon-reload`。

---

## 2. 日常检查

```bash
ssh nas 'systemctl --user is-active bybitbot-postgres.service bybitbot-worker.service bybitbot-web.service bybitbot-backup.timer'
ssh nas 'podman ps --format "{{.Names}} {{.Status}}"'
ssh nas 'podman logs --tail=50 bybitbot-worker'
ssh nas 'curl -s http://127.0.0.1:18080/health'   # {"status":"ok"}
```

`/health` 的判定来自管理服务读到的心跳年龄（阈值 120 秒）。返回 `degraded` 表示 worker 心跳过期，**不代表挂单已撤销或持仓已平**——此时必须按第 6 节核对交易所实际状态。

查运行态（示例 DSN 走本机 socket，口令不落到命令行）：

```bash
ssh nas "podman exec bybitbot-pg psql -U bybitbot -d bybitbot_paper -Atc \
  \"SELECT value->>'state',value->>'lease',value->>'equity',value->>'tick_at',value->>'last_error' \
    FROM runtime_state WHERE key='status';\""
```

---

## 3. 发布新版本

流程固定为：构建镜像 → 打回滚标签 → 重建服务 → 核对运行态。

```bash
# 1. 在 Mac 上同步构建上下文到 NAS（不含任何密钥文件）
rsync -az --delete \
  --exclude '__pycache__' --exclude 'secrets' --exclude 'var' \
  src alembic alembic.ini config/config.example.yaml Containerfile \
  nas:~/build/bybitbot/

# 2. 构建并打版本标签
ssh nas 'cd ~/build/bybitbot && podman build -t localhost/bybitbot:0.1.0 .'

# 3. 发布前给当前镜像打回滚标签
ssh nas 'podman tag localhost/bybitbot:0.1.0 localhost/bybitbot:rollback-$(date -u +%Y%m%d)'

# 4. 重建服务（Quadlet 会重新拉起容器）
ssh nas 'systemctl --user daemon-reload && systemctl --user restart bybitbot-worker.service bybitbot-web.service'

# 5. 核对
ssh nas 'podman ps && podman logs --tail=50 bybitbot-worker'
```

`deploy/Containerfile.nas` 复用 NAS 上已有的 `localhost/bybitbot-runtime:0.1.0` 基础层（内含 Python 3.12 与带哈希的 `deploy/requirements.lock` 依赖）。基础层只在依赖锁变化时重建：

```bash
ssh nas 'cd ~/build/bybitbot && podman build -f deploy/Containerfile.nas-runtime -t localhost/bybitbot-runtime:0.1.0 .'
```

### 回滚

```bash
ssh nas 'podman tag localhost/bybitbot:rollback-20261031 localhost/bybitbot:0.1.0'
ssh nas 'systemctl --user restart bybitbot-worker.service bybitbot-web.service'
```

回滚只回退代码，**不回退数据库**。迁移为前向兼容设计；若新版本写过数据，旧版本可能不认识新字段，回滚前先确认该版本能否读取当前 schema。回滚后 worker 仍需重新确认接管。

---

## 4. 备份与恢复

### 备份（NAS 侧，每日）

`bybitbot-backup.timer` 每天 03:23（CST）触发，随机延迟 0～300 秒，持久化（`Persistent=true`，错过会补跑）。实际动作在 `~/.local/share/bybitbot/bin/backup.sh`：

```
podman exec bybitbot-pg pg_dump -U bybitbot -d bybitbot_paper -Fc \
  | gpg --batch --yes --trust-model always --recipient-file ~/.config/bybitbot/backup-public.asc --encrypt
```

产物 `~/.local/share/bybitbot/backups/bybitbot-<UTC>.dump.gpg`，只对公钥加密，**私钥不在 NAS 上**。

> `backup.sh` 当前硬编码备份 `bybitbot_paper` 一个库。新增环境时必须同步扩展，否则该环境没有备份。

### 异机复制（Mac 侧，每 30 分钟）

Mac 上的 launchd 任务 `io.bybitbot.offsite-backup` 通过 rsync 拉取加密备份到 `~/.local/share/bybitbot-monitor/backups/`（0600）。安装与启用：

```bash
cd /Users/nicholaswang/Desktop/code/bybit
uv run python deploy/install_watch.py \
  --health-url 'https://nicholas-4.tail068b64.ts.net/health' \
  --rsync-source 'nas:.local/share/bybitbot/backups/*.dump.gpg'
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.bybitbot.nas-heartbeat.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.bybitbot.offsite-backup.plist
```

被监控地址与 rsync 源由部署方通过参数提供，仓库里不写死具体主机名。

- `io.bybitbot.nas-heartbeat`：每 30 秒检查一次 `https://nicholas-4.tail068b64.ts.net/health`，连续 3 次失败才发本地通知，恢复时另发一条；与备份检查相互独立。
- `io.bybitbot.offsite-backup`：每 30 分钟同步加密备份，失败时退出码 1 并写入 `state/backup-sync.json`。

安装脚本把脚本与状态放在 `~/.local/share/bybitbot-monitor/`，**不依赖 Desktop 目录**——launchd 任务此前因从 Desktop 路径启动而被系统隐私机制卡住（进程存活但无输出、无状态文件）。改用标准路径后两个任务均 `last exit code = 0`，心跳状态文件持续更新。

监控脚本使用的 rsync 参数必须兼容 macOS 自带的 rsync 2.6.9：`--chmod=Fu=rw,Fgo=,Du=rwx,Dgo=`（`--chmod=F600,D700` 在该版本上直接报 `invalid argument`）。

### 私钥保管

备份私钥只在 Mac 上：`~/Desktop/code/bybit/secrets/backup-gnupg/`，GnuPG home 权限 0700，指纹：

```
CCA09EDDB905C807F03D697A1D9AEFAD85C1BB92
```

密钥目录已被 `.gitignore` 排除。**这把私钥是唯一能解密备份的东西**，请另行离线备份（打印/离线介质），并确认自己能找回它；丢失后 NAS 上的加密备份无法恢复。

### 恢复演练（已执行过的步骤）

```bash
# 1. 解密到独立数据库，不覆盖现网
GNUPGHOME=~/Desktop/code/bybit/secrets/backup-gnupg \
  gpg --decrypt ~/Desktop/code/bybit/secrets/offsite-backups/bybitbot-<UTC>.dump.gpg \
  > /tmp/restore.dump

# 2. 恢复到独立库（备份里的属主是 NAS 上的 bybitbot 角色，本机没有该角色）
podman exec bybitbot-pg psql -U postgres -Atc \
  "DO \$\$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bybitbot') THEN CREATE ROLE bybitbot LOGIN; END IF; END\$\$;"
podman exec bybitbot-pg createdb -U postgres -O bybitbot bybitbot_restore_drill_<日期>
podman exec -i bybitbot-pg pg_restore -U postgres -d bybitbot_restore_drill_<日期> \
  --no-owner --role=bybitbot < /tmp/restore.dump

# 3. 核对 schema head 与关键表
podman exec bybitbot-pg psql -U postgres -d bybitbot_restore_drill_<日期> \
  -Atc "SELECT version_num FROM alembic_version; SELECT count(*) FROM strategy;"

# 4. 用完删除演练库与明文 dump
podman exec bybitbot-pg dropdb -U postgres bybitbot_restore_drill_<日期>
rm -f /tmp/restore.dump
```

不带 `--no-owner --role=bybitbot` 时 `pg_restore` 会因本机缺少 `bybitbot` 角色报 47 条属主错误；数据仍然恢复，但演练结论会带噪声，不应据此判断备份可用。

2026-10-31 的演练结果：head `786d42c1b808`，策略 2 条，运行态 2 条，与源库一致。

恢复演练的两个硬约束：

- 恢复目标必须是**独立数据库**，不要直接 `pg_restore` 进现网库
- 恢复交易资格与"能读出数据"是两件事。恢复后必须重新完成身份核对、对账与基线初始化；确认不了历史完整性就一直保持暂停

演练脚本产物（`secrets/restore.dump` 明文、演练数据库）目前留在本机受限目录内，用完应删除。

---

## 5. 密钥轮换

适用范围：Bybit API 凭据、管理口令、备份密钥。

### Bybit API 凭据

顺序不能颠倒——需求 SEC-03 要求先暂停开仓、核对在途订单，验证新凭据后再恢复：

1. 管理页 `PAUSE_NEW_RISK`（人工暂停，锁定）
2. 核对交易所侧在途订单与持仓，确认没有处于"结果未知"状态的 bot 订单
3. 在交易所创建新 key：只保留现货/期权读取与交易权限，**关闭提现**，绑定固定出口 IP
4. 本机写入新凭据文件（0600，当前用户拥有），格式同 `config/credentials.example.json`
5. `systemctl --user restart bybitbot-worker.service`，确认启动日志只打印脱敏身份与权限摘要
6. 只读核对账户身份与权限通过后，再逐条解除暂停

### 管理口令

```bash
ssh nas 'cd ~/build/bybitbot && podman run --rm -v ~/.config/bybitbot:/run/bybitbot:ro \
  localhost/bybitbot:0.1.0 --config /run/bybitbot/config.yaml \
  init-admin --path /run/bybitbot/admin.json.new --generate-password-file /run/bybitbot/admin-password.txt.new'
```

生成新文件后原子替换 `admin.json` 并重启 `bybitbot-web.service`（重启会使旧会话失效）。旧文件删除。口令值只写入受限文件，不打印到日志。

### 备份密钥

换密钥时先在 Mac 上生成新密钥对，把新公钥替换到 NAS 的 `~/.config/bybitbot/backup-public.asc`，再确认新备份能解密，最后再移除旧私钥。**先确认新备份可解密再退役旧私钥**。

---

## 6. 异常处置

### 未知订单（UNKNOWN / SUBMITTING）

`UNKNOWN` 表示"发出去了但结果没拿到"，不是"没发出去"。

1. 不要用人工点击去释放预留——`RESUME` 会因为存在未知订单而拒绝
2. 到交易所按 `orderLinkId` 核对真实状态：成交、部分成交、仍然挂单、还是根本不存在
3. 把结论写进对账记录；由 worker 的对账流程确认后状态才会从"待核实"转为确定
4. 在核实之前，系统保持暂停新增风险

### 退出未完成

管理页的"受限减仓"是唯一的人工减仓路径：它走与自动退出相同的授权逻辑（身份、租约、可信库存、库存预留、价格下限、审计写入），但不受开仓预算与亏损熔断限制。数量不得超过账户总量、bot 归属量、策略归属量减去在途预留。

### 告警

`ops.alert_channels` 为空时，所有告警只落库，不会外发。配置真实渠道后：

- 通知失败会重试并落库；关键告警持续超时未送达会暂停新增风险
- 告警渠道文件（0600）指向一个 JSON，里面是 HTTPS URL

---

## 7. Mac 端监控的已知限制

- 监控跑在这台 Mac 上。**Mac 睡眠、关机或离线时不会有任何外部告警**——这是单机监控的结构性限制，不是实现缺陷。要做到 7×24 外部探测，需要把心跳检查放到一台常在线的机器上。
- 本地通知走 macOS 通知中心；系统可能处于专注模式而延迟展示。
- 心跳检查的是管理服务的 `/health`，它反映 worker 心跳新鲜度，**不反映交易所挂单状态**。

---

## 8. 尚未完成、需要继续推进的事项

按 [docs/acceptance.md](docs/acceptance.md) 的缺口清单，部署层面仍未完成：

| 事项 | 状态 |
|---|---|
| 真实 Demo/Testnet 能力验证（期权权限、减仓、成交、撤单、结算） | 未开始，缺凭据 |
| 真实告警渠道配置与送达/失败/暂停演练 | 未开始 |
| 容量与性能基线（P95、CPU/内存、磁盘增长） | 无测量记录 |
| 数据保留清理实现（`retention_days` 只有配置项，无 purge） | 未实现 |
| 磁盘不足自动暂停演练 | 未测试 |
| 可重复执行的备份恢复演练脚本 + RTO/RPO 实测 | 未脚本化 |
| 密钥泄漏自动化扫描 | 无 |
| 双实例租约真实进程级实测 | 未做 |

实盘启用另需：版本化配置、正确的环境与账户、M4a/M4b 阶段 P0 验收证据、无未解释的账务差异、可信的退出路径、可用告警与恢复记录，以及账户所有者的人工启用操作。
