# 本地 paper 交易系统：运行与验收

交付日期：2026-11-16。本次把公共领域层接入操作台和研究，并建立可独立初始化、验收、启动的本地 paper 系统。现货 DCA 和单腿期权策略已经通过真实 worker 服务链路完成交易演练。生产环境、NAS 和原研究采样器未切换。

## 当前可用版本

| 环节 | 实现与验收 |
|---|---|
| 行情与合约 | 固定合成 BTC/ETH 现货和 Call，每次提供新时间戳；公共行情仍是普通 paper 默认来源 |
| 策略与调度 | 持久化策略版本、UTC 日历任务、信号去重、冷却、重启不补买 |
| 风控与预留 | 账户身份、估值、暂停、金额/库存/费用/Delta/期限检查与原子预留 |
| 执行与账本 | 独占执行锁、订单状态机、下一盘口撮合、幂等成交、手续费和可信库存 |
| 对账与恢复 | 余额/订单/成交检查点，重启保留余额及库存，重新要求接管确认 |
| 操作台 | 登录、心跳、账户、订单/成交、对账、策略、持久化控制命令、CSV 审计导出 |
| 运维 | 告警和暂停留档；外部通知未配置时如实保持未送达，超时暂停新增风险 |

这是一套已运行验证的 paper 系统。真实成交、Bybit 账户能力、真实盘口排队/延迟/冲击、到期交割、生产恢复与持续盈利仍无本次验收证据。既有完整 AC 验收矩阵见 `docs/acceptance.md`，本次没有把其中所有部分通过项升级为完成。

## 当前本机入口

- 管理页：`http://127.0.0.1:18082`，预览配置名 `paper-system`。
- 正常 worker 配置：`var/paper-system-v3/config.yaml`。
- 用户名：`admin`；密码只保存在 `var/paper-system-v3/admin-password.txt`，未写入本文。
- 管理身份：`var/paper-system-v3/admin.json`。配置及凭据文件权限 0600，目录权限 0700，均由 Git 忽略。
- 交易链路验收记录：`var/paper-system-v3/verification.json`。

交付时 worker 与 web 已启动，新增风险保持暂停。接管已从管理页确认。人工暂停仍保留；没有外部通知渠道，既有关键告警超时会产生 `NOTIFICATION_FAILURE`，不能把解除单条暂停解释为自动恢复全部交易。模拟执行资格只作用于该本地独立 schema。

## 从零建立一个沙盒

需要 Python 3.12、项目依赖和本机 PostgreSQL。已有数据库服务时复用，不初始化或清空原数据库。以下 DSN 是仓库既有本机测试服务的连接；自己的环境应替换为自己的本机连接。

```bash
.venv/bin/bybitbot sandbox init --directory var/paper-system-new --dsn postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot
```

命令只接受本机 `postgresql+psycopg` DSN，拒绝查询参数和 `BYBITBOT_ENV`/`BYBITBOT_DB_DSN` 环境覆盖，且拒绝已有输出目录。它创建随机 `sandbox_<uuid>` schema 和账户标识，应用真实 Alembic 迁移，生成 paper-only 配置及本地管理员。失败时保留现有证据，不自动删除 schema。

随后在首次启动常驻 worker 前验收：

```bash
.venv/bin/bybitbot sandbox verify --directory var/paper-system-new
```

验收只接受未交易沙盒：初始化基线、确认接管、投递现货/期权调度、验证旧盘口不成交、新盘口成交及费用入账、暂停拒绝开仓、暂停中期权减仓、重启保持余额/成交和重新确认要求。成功后写入唯一 `verification.json`，并保持暂停。重复验收拒绝；删除报告也不能让已交易沙盒重新验收。

验收显式投递调度任务。若当前时刻距离配置的日历时刻不足两分钟，会在连接数据库前拒绝，避免重复开仓；稍后重新运行或创建新沙盒。生成的日历为每天一次，首次时刻在初始化的一小时后，保留模板各策略原有冷却。合成行情的交易所日切禁开窗口设为空；合约到期与预算/持仓风控仍生效。

## 启动正常 worker 和管理页

两个独立进程；已有本机实例时不要再启动第二个 worker。运行中的实例持有数据库 advisory lock，另一实例无法接管。

```bash
.venv/bin/bybitbot --config var/paper-system-v3/config.yaml run
```

```bash
.venv/bin/bybitbot --config var/paper-system-v3/config.yaml web --auth var/paper-system-v3/admin.json --host 127.0.0.1 --port 18082 --insecure-local-cookie
```

新建其他沙盒时把两条命令中的目录替换为新目录。Web 仅绑定回环地址；开发 HTTP Cookie 参数只适用于本机入口。登录读取相应目录的密码文件。Web 只入队控制命令，由 worker 校验与执行；页面可访问不代表 worker 在线。

每次重启后先核对账户身份、估值、干净对账、未知订单和旧执行者停止证据，再确认接管。首次基线只建一次；已有高点和历史损益不能通过重新初始化清除。需要恢复时逐条处理暂停，不能跳过未解决的告警或对账问题。

## 合成行情的可重复性

`paper.market_source` 默认仍为 `public`；只有显式设为 `synthetic` 的 paper 配置使用离线行情，其他环境拒绝合成来源。该来源不构造 REST 客户端、不读取交易凭据、不访问行情网络。

初始化时固定 BTC/ETH 价格及到期日，重启后合约身份不变。报价和深度的时间戳随读取更新，订单只能在之后的新盘口成交。到期后不自动滚动合约、不伪造结算价；后续演练创建新的隔离沙盒。合成价格不能用来判断策略是否盈利。

## 2026-11-16 验证证据

- 全套 Python：701 passed，约 36 秒；一个既有 Alembic `path_separator` 弃用警告。
- 管理页 Node 用例：6 passed。
- Ruff：`src tests deploy alembic`、新研究 v2 和修改的比较脚本全部通过。
- Alembic：全新 schema 从初始版本迁移到 `8a4d17c2f905`；`alembic check` 显示无新增迁移操作。
- 实际 CLI：`sandbox init` 和 `sandbox verify` 成功，8 项闭环检查，3 笔模拟成交。两个调试沙盒留在本地，未通过清空历史获得成功。
- 实际网页：管理员登录；读取账户、订单和成交；接管、指定暂停解除与暂停中现货减仓经持久化队列交由 worker 执行；审计导出和浏览器/服务端错误检查。
- 配置指纹：修复集合序列化顺序导致的跨进程误判，4 个 `PYTHONHASHSEED` 下身份一致，真实预算/调度变动仍改变版本。
- 研究：原六份冻结文件哈希保持一致，原采样器心跳正常。比较研究四份结果重放逐字节一致；v2 的 40 项快照/逐腿/冻结保护测试通过，v2 未启动采样。

原始本地验收 JSON 和页面后续操作是两个时间点的证据，后续网页减仓不会重写初始 `verification.json`。新代码与本文随 PR #1 合入 `main`（合并提交 `ac950cb`）；本次未改动 NAS 部署。

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
