# Bybit 现货 1 分钟数据与回测页面

这个工程采集 20 个 Bybit USDT 现货交易对加 10 个历史对照币种的已完结 1 分钟 K 线，保存到 SQLite，并通过 Tailscale Serve 提供仅限指定 Tailscale 用户访问的查询与回测页面。

回测选币与 2026-11-19 相关性分析 Top20 一致：BTC、ETH、SOL、AAVE、HYPE、ENA、XRP、LINK、ASTER、PIEVERSE、NEAR、MNT、WLD、XAUT、BNB、VVV、GRAM、LIT、ZRO、HOODX。另有 DOGE、PEPE、SUI、ADA、AVAX、TRUMP、UNI、FIL、ATOM、SHIB 保留为单币持有对照，不出现在选币列表。采集使用 Bybit 公共接口，不需要 API 密钥。数据库、日志和回测结果不纳入 Git。

这是本仓库的研究子工程，与 `src/bybitbot` 的实盘交易服务完全独立：只读公共行情，不持有密钥，不下单，不改动交易服务的镜像与部署。

## 文件

- `collector.py`：建库、增量同步、断点回溯、缺口修复和状态查询。
- `backup_db.py`：SQLite 在线一致性备份、校验和及轮换。
- `web/`：K 线图、采集状态、屯币宝（比例/定时平衡）、双均线交叉、RSI 均值回归和海龟现货趋势回测页面。
- `/correlation`：Bybit 热门现货 Top20 收益相关性页面，支持 30/90 天窗口、完整矩阵、任意币对与价格收益比较，以及受身份保护的数据下载。
- `docs/database.md`：表结构、字段含义、传输方式与查询示例。
- `docs/smart-portfolio.md`：屯币宝规则分析、成交假设、基准比较与模型边界。
- `docs/turtle.md`：海龟 55/20 长期系统的现货适配、ATR 仓位、加仓和止损执行假设。
- `docs/strategies.md`：新增九种现货策略的参数、选币、成交、费用与持有对照规则。
- `web/strategy_catalog.py` 与 `web/strategy_engine.py`：共享参数定义及九种策略的流式计算。
- `web/run.sh`：当前服务器的启动脚本，包含部署路径和允许访问的 Tailscale 用户。
- `tests/`：认证、数据质量、回测、修复和备份的回归测试。

## 采集与传输

本机无法直连 Bybit，采集脚本通过 `ssh nas` 转发到远端执行，远端只用 Python 标准库 `urllib` 请求已校验的公共 GET：本地先按固定白名单校验端点与参数，白名单外的请求一律拒绝；远端只执行这些 GET，不读取本地文件、不持有任何凭据。

整个运行复用同一条 SSH 连接：每次握手在远端要数秒，若每页重连，多年回填会被握手时间拖垮。一个币种一页、20 个币种一批批量请求，一批只做一次往返。

Bybit 的 `end` 参数是闭区间，一次请求返回截至 `end` 的至多 1000 根 K 线，因此向后翻页把下一次 `end` 设为上一页最旧一根的时间戳。Bybit 最新一行是仍在形成的当前分钟，收盘时间未到时整行丢弃，下次同步再写入已完结版本；回测永远不会读到半根 K 线。

## 当前服务器

代码部署在 `/home/nicholas/data/bybit-backtest/`，数据库在同目录的 `bybit_spot_1m.sqlite3`。网页由 Gunicorn 监听 `127.0.0.1:18080`，Tailscale Serve 将 tailnet 内的 HTTPS 请求转发到该端口。没有启用 Funnel。

```bash
python3 collector.py status
python3 collector.py init
python3 collector.py sync
python3 collector.py backfill --max-pages 800 --since 2021-07-05T00:00:00Z
python3 collector.py repair --max-pages 80
python3 backup_db.py
tailscale serve status
```

网页依赖安装命令：

```bash
python3 -m venv web/venv
web/venv/bin/pip install -r web/requirements.txt
```

当前服务器使用 crontab 定时同步、回溯、修复缺口、备份，并每分钟检查网页服务。备份只保存在服务器受限目录，默认保留最近 14 份，不放入 GitHub。部署代码更新后，应在服务器上重启网页进程。网页运行环境要求 Python 3.10 或更新版本；Gunicorn 版本固定在 `web/requirements.txt`。运行与安全细节见 [网页说明](web/README.md)。

## 数据范围

现货数据库仅包含公开的现货 K 线，不包含逐笔成交、历史订单簿或账户记录。K 线缺口或异常 OHLC 会阻止回测；页面可检查任意 31 天区间的覆盖率与首批缺口，并导出最多 7 天的 CSV。手续费和滑点由使用者在回测时设置。回测结果记录数据指纹和引擎版本，便于复现。表结构与查询示例见 [数据库说明](docs/database.md)。

屯币宝可选 2 至 20 个币种，权重合计 100%。比例平衡每 5 分钟检查，定时平衡按周期检查且使用 3 个百分点的门槛；下一分钟开盘调仓，先卖后买并计入成本。网页展示与同权重买入持有的净值对照、收益差、回撤、调仓记录及各币数量变化。单次回测最多 5 个日历年，所有币种须有完整的共同数据区间。详见 [屯币宝模型说明](docs/smart-portfolio.md)。

所有策略的已完成结果均可选择单币现货持有对照（Top20 币种任选一个，也可不比较），包括之前保存的回测。在结果页选择币种，即按原回测资金、时间、费用和滑点计算全仓买入持有，显示收益、最大回撤、净值曲线及策略相对单币的收益差。可下载包含原回测与当前对照的 JSON；原历史文件保持原样。

## Top20 分钟数据

`web/market_config.py` 是前后端、计算进程及采集程序共享的固定币种配置；页面通过服务端数据初始化选币，不重复硬编码白名单。当前名单是 2026-11-19 按 24 小时成交额取样的固定快照，不自动跟随实时热度变动。

新增币种通过 Bybit 公共接口直接采集已确认 1 分钟 K 线；小时分析数据不转换或填充为分钟数据。先补最近数据，再沿用定时回溯扩展至交易所提供的最早时间或实际上市时间。页面显示每币已有历史区间，组合快捷区间取交集，提交回测逐分钟检查缺口。上市较晚的币没有上市前数据，XAUT 为黄金资产代币。

## 本地测试

```bash
uv run pytest research/bybit_spot_backtest/tests -q
node --test research/bybit_spot_backtest/tests/test_ui.cjs
```

回测台的 Web 依赖只有 Flask 与 gunicorn，固定在 `web/requirements.txt`；仓库的 `dev` 附加依赖已包含 Flask，便于在 CI 中直接跑这套测试。测试不需要网络，也不需要任何交易所凭据。
