# Bybit 现货与单腿期权自动交易系统

面向个人交易的 Bybit 现货与单腿期权运行服务。**当前仓库实现的是 M0～M3 范围内的模拟（paper）闭环**，已在 NAS 上以 paper 配置部署并保持暂停；没有接入任何真实交易凭据，也不具备实盘发单资格。

- GitHub 私有仓库：[NicholasPython/bybit](https://github.com/NicholasPython/bybit)，默认分支 `main`；[自动检查](https://github.com/NicholasPython/bybit/actions)
- 需求与验收口径：[docs/requirements.md](docs/requirements.md)、[docs/requirements-review.md](docs/requirements-review.md)
- 架构决策记录（已落地决策的理由与取舍）：[docs/architecture-decisions.md](docs/architecture-decisions.md)
- 当前验收矩阵（逐条 AC-01～AC-28 的证据与缺口）：[docs/acceptance.md](docs/acceptance.md)
- NAS 部署与运维手册：[docs/runbook-nas.md](docs/runbook-nas.md)
- 2026-11-01 测试报告整改与发布证据：[docs/remediation-2026-11-01.md](docs/remediation-2026-11-01.md)
- 2026-11-16 公共层与本地 paper 系统交付：[docs/paper-system-2026-11-16.md](docs/paper-system-2026-11-16.md)
- 公共层边界与研究迁移：[docs/shared-domain.md](docs/shared-domain.md)
- 研究前向账本与交易系统模块映射（2026-11-16 暂停后的证据与缺口）：[docs/research-ledger-mapping-2026-11-16.md](docs/research-ledger-mapping-2026-11-16.md)
- 现货－永续价差套利的只读采集与回测（2026-11-18，含真实数据结论）：[docs/cash-futures-spread-2026-11-18.md](docs/cash-futures-spread-2026-11-18.md)
- 现货－永续价差实盘前风险清单（2026-11-19，含产品侧缺口与验证清单）：[docs/cash-futures-live-risk-checklist-2026-11-19.md](docs/cash-futures-live-risk-checklist-2026-11-19.md)
- 现货 1 分钟采集与回测子工程（2026-11-19，Bybit 公共接口，只读研究）：[research/bybit_spot_backtest/README.md](research/bybit_spot_backtest/README.md)
- 双腿价差套利阶段一与 P0 阻断项修复（2026-11-20，含能力边界与下一阶段顺序）：[docs/spread-arbitrage-phase1-2026-11-20.md](docs/spread-arbitrage-phase1-2026-11-20.md)
- 双腿价差套利阶段二（2026-11-22，独立 paper 持久化执行、线性腿对账与重启恢复）：[docs/spread-arbitrage-phase2-2026-11-22.md](docs/spread-arbitrage-phase2-2026-11-22.md)
- 双腿价差套利阶段三（2026-11-22，滚动价差信号、成本过滤与受控触发）：[docs/spread-arbitrage-phase3-2026-11-22.md](docs/spread-arbitrage-phase3-2026-11-22.md)
- 双腿价差套利影子盘与管理页（2026-11-22，FR-07.3 公开行情模拟与只读核验）：[docs/spread-arbitrage-shadow-2026-11-22.md](docs/spread-arbitrage-shadow-2026-11-22.md)

## 组成

| 进程 | 职责 |
|---|---|
| `bybitbot run` | 唯一交易 worker：行情同步、风控与预留、订单执行、账本与对账、调度、退出管理、告警 |
| `bybitbot web` | 只读管理页与控制命令入队（控制命令由 worker 领取执行，页面本身不发单） |
| `bybitbot migrate` | 数据库版本迁移 |
| `bybitbot init-admin` | 创建管理身份（PBKDF2，口令不写入日志） |
| `bybitbot sandbox init / verify` | 新建本地独立 schema 与固定合成行情，验收策略到重启恢复的 paper 闭环 |

内部模块（`src/bybitbot/`）：

- `adapters/` —— `paper`（模拟撮合）、`bybit_rest`（V5 私有/公开 REST）、`bybit_ws`（公开与私有流）
- `risk/` —— 账户授权锁、现金与库存预留、Delta 区间、费用与成本
- `execution/` —— 物理订单状态机、执行者租约（PostgreSQL advisory lock）
- `ledger.py` / `reconciliation.py` —— 账本事件、去重成交、权益政策与对账修复
- `fees.py` —— 版本化的手续费估算口径（模拟、预算与止盈判断共用）
- `strategy/` —— 现货定预算 DCA、期权单腿买入，退出规则
- `spread/` —— U 本位双腿价差套利：配对状态机、腿订单模型、瘸腿自愈（默认关闭）
- `ops/` —— 运行状态、暂停管理、告警投递、受限文件注入的通知渠道
- `web/` —— FastAPI 管理页（模板 + 原生 JS/CSS）

## 环境要求

- Python 3.12（`>=3.12,<3.13`）
- PostgreSQL 16
- [uv](https://docs.astral.sh/uv/) 0.9.9（依赖由 `uv.lock` 固定）

## 本地安装与测试

在项目根目录安装固定版本的依赖：

```bash
uv sync --frozen --extra dev
```

数据库测试需要 PostgreSQL。下面的口令只用于本机隔离测试；已有同名容器时复用并检查其回环端口，不重复创建：

```bash
podman run -d --name bybitbot-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_PASSWORD=bybitbot -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=bybitbot docker.io/library/postgres:16-alpine
```

数据库就绪后运行：

```bash
uv run ruff check src tests deploy
```

```bash
uv run pytest -q
```

2026-11-22 影子盘后最新结果为 **1839 项通过**（Alembic `path_separator` 弃用警告）。测试在 `BYBITBOT_TESTING=1` 下运行；`tests/conftest.py` 阻断 httpx 的默认同步和异步 HTTP transport，交易所 HTTP 响应使用 `httpx.MockTransport`，交易行为使用 `PaperAdapter` 合成。数据库用例各自使用独立 PostgreSQL schema，结束后只删除自己的 schema。这不是操作系统级网络隔离，也不构成真实 Bybit 能力验收。管理页脚本另有 `node --test tests/console_ui.test.mjs`（16 项）覆盖。

现货采集与回测子工程的测试随同 `pytest` 一起运行（`research` 在 `testpaths` 内，Flask 已在 `dev` 附加依赖中固定为 3.1.3）；它的页面交互另有 `node --test research/bybit_spot_backtest/tests/test_ui.cjs`（7 项）。两者都不需要网络与交易所凭据。

## 配置

复制示例并按需修改：`config/config.example.yaml`。该文件不含任何密钥。

关键约束（不满足会在启动时被拒绝，见 `settings/loader.py`）：

- `paper` 环境不得配置 `base_url` / `ws_url` / `credential_ref`，不得装载交易凭据，不得发送交易写请求
- `live` 环境必须填写 `account.expected_uid`，且 `live_gates` 三项（`live_allowed`、`acceptance_record_ref`、`manual_enable_ref`）全部满足才允许发单
- 各环境的 `db_dsn` 必须互不相同；`ops.valuation.currency` 与各金额限额的币种必须一致
- 策略模板默认 `enabled: false`

手续费口径写在顶层 `fees` 段，模拟与实盘共用同一套费率（`src/bybitbot/fees.py`）：

```yaml
fees:
  version: bybit-fee-v1
  spot_taker_rate: "0.001"
  option_underlying_rate: "0.0003"   # 标的指数价的 0.03%（Bybit 非 VIP taker）
  option_premium_cap_rate: "0.07"    # 权利金的 7%（公式封顶）
  option_per_contract: "0"
```

期权费用按 `min(标的指数价 × option_underlying_rate, 权利金 × option_premium_cap_rate) × 数量` 估算，再加 `option_per_contract × 数量`；指数价缺失、无效或过期时按封顶比例估算。模拟撮合使用该模型，真实成交按交易所回报费用入账。模型参数或版本变化会拒绝直接恢复旧模拟检查点；仅在数据库及检查点均无交易事实、无非零持仓时，允许保留余额迁移费用元数据，并保存原检查点审计副本。有交易历史时须人工核对迁移，不能重新初始化余额。

交易凭据（仅非 paper 环境）放在受限文件中，格式见 `config/credentials.example.json`：

```json
{
  "api_key": "REPLACE_WITH_NON_LIVE_KEY",
  "api_secret": "REPLACE_WITH_NON_LIVE_SECRET"
}
```

凭据应保存在当前用户拥有、权限为 0600 的普通文件。当前装载器检查属主及组/其他用户权限，但尚未采用管理身份装载器的软链接拒绝和文件描述符校验，不能将其视为同等加固。凭据不进入 Git、镜像、业务表、URL 或日志。

告警渠道用受限文件引用，不在配置里内联 URL：

```yaml
ops:
  alert_channels:
    - /home/nicholas/.config/bybitbot/alert-webhook.json
```

该文件同样是 0600、当前用户拥有，内容形如 `{"url": "https://..."}`；投递只接受 HTTPS。

## 运行

先复制运行配置。然后将 `environments.paper.db_dsn` 改为实际的本机数据库连接；若使用前面的隔离测试容器，对应值为 `postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot`，其中口令仅用于本机测试。确认其他环境仍使用不同 DSN 后，再检查配置并迁移：

```bash
cp config/config.example.yaml config/config.yaml
```

```bash
uv run bybitbot --config config/config.yaml check
```

```bash
uv run bybitbot --config config/config.yaml migrate
```

创建管理身份时交互输入密码；命令拒绝覆盖已有文件：

```bash
uv run bybitbot init-admin --path secrets/admin.json
```

worker 与管理页分别运行在两个终端：

```bash
uv run bybitbot --config config/config.yaml run
```

```bash
uv run bybitbot --config config/config.yaml web --auth secrets/admin.json --host 127.0.0.1 --port 18080
```

上述 web 命令用于 HTTPS 入口；本机直接使用 HTTP 调试时加 `--insecure-local-cookie`，使浏览器能够携带开发会话 cookie。

管理页只允许绑定 `127.0.0.1`；对外访问走 Tailscale Serve 等受控 HTTPS 入口（NAS 上已配置为 `https://nicholas-4.tail068b64.ts.net/` → `127.0.0.1:18080`）。数据库同样只监听回环地址。

启动后 worker 处于 `PAUSED_OPEN / NEEDS_CONFIRMATION`，需要按顺序完成：

1. 账户身份与权限核对通过
2. 完成一次对账
3. 仅在首次接入且尚无基线时，用管理页 `INITIALIZE_BASELINE` 建立风险基线（首日标记为不完整交易日）；已有基线和历史高点需要保留并对账
4. 用管理页 `CONFIRM_TAKEOVER` 确认旧执行者已停止（重启后必须重新确认）
5. 解除对应暂停

策略默认禁用。启用任何一个策略前，先确认该策略的限额、退出规则与告警渠道都已就位。

## 本地隔离 paper 系统

不接触交易所凭据即可跑通完整链路：新建随机命名的独立 schema，固定合成行情与合约到期日，应用真实迁移，生成 paper-only 配置和本地管理员，然后走基线、接管、策略下单、费用入账、暂停拒单、减仓与重启恢复。

```bash
uv run bybitbot sandbox init --directory var/paper-system --dsn postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot
```

```bash
uv run bybitbot sandbox verify --directory var/paper-system
```

详情、限制与证据见 [docs/paper-system-2026-11-16.md](docs/paper-system-2026-11-16.md)。

## 目录

```
src/bybitbot/     应用代码
research/         研究脚本（含现货－永续价差只读采集与回测）
research/bybit_spot_backtest/  现货 1 分钟采集与回测子工程（独立于交易服务）
tests/            1839 项 Python 隔离测试、16 项页面脚本测试
alembic/          数据库迁移（当前 head: c9d3f7a2b6e4）
config/           配置示例（无密钥）
deploy/           容器、Quadlet、备份、Mac 端监控与安装脚本
docs/             需求、评审、验收矩阵、NAS 手册
```

## 研究子工程：现货 1 分钟采集与回测

`research/bybit_spot_backtest/` 是独立的**研究**子工程：采集 Bybit 公共接口的现货 1 分钟 K 线写入本地 SQLite，并提供参数化策略回测页面（屯币宝、双均线、RSI、海龟及九种研究策略）。它与 `src/bybitbot` 的交易闭环完全分离：

- 只读公共行情，不需要也不持有任何 API 密钥，不创建账户、不下单、不写交易所
- 不属于部署产物；实盘镜像只包含 `src/`，此子工程不会进入交易服务
- 采集与回测的取值口径（信号在收盘生成、下一分钟开盘成交、缺口拒绝回测）与交易服务各自的策略实现彼此独立

细节见 [子工程说明](research/bybit_spot_backtest/README.md) 与[数据库说明](research/bybit_spot_backtest/docs/database.md)。它的 Python 测试随仓库主测试一起运行（`research` 已在 pytest 的 `testpaths` 内），页面交互另有 Node 测试；全部不需要网络与交易所凭据。

## 安全边界

- 密钥只供交易适配器使用，通过受限文件注入；不进 Git、镜像、业务表、URL 或日志
- 管理界面走私有网络或受控 HTTPS 入口，具身份认证、会话过期与写操作 CSRF 保护；数据库不向公网开放
- 依赖版本固定（`uv.lock`、`deploy/requirements.lock` 带哈希），部署产物带版本号，测试不含真实密钥
- 实盘发单需要独立的实盘允许配置、完整风控参数、验收记录与人工启用操作；切换环境不复用任务运行记录

2026-11-01 用一把真实**只读** Bybit key 核对了读取链路（主账户、只读、无 IP 限制、净值约 0.02 USDT），并因此修复了钱包余额中空 `accruedInterest` 导致的账户读取失败。写路径、成交、撤单与到期交割仍无真实账户证据，详见 [整改记录](docs/remediation-2026-11-01.md) 第 5 节。
