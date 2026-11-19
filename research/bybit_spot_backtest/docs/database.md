# Bybit 现货 1 分钟回测数据库

服务器位置：`/home/nicholas/data/bybit-backtest/`
数据库：`bybit_spot_1m.sqlite3`（SQLite）
采集脚本：`collector.py`

覆盖 2026-11-19 Top20 现货名单，并保留若干历史对照币种。完整列表见 `web/market_config.py`，新增币种分钟历史正在回溯。仅存储已完结的 1 分钟现货 K 线，按 UTC 时间戳索引。脚本使用 [Bybit V5 公共行情接口](https://bybit-exchange.github.io/docs/v5/market/)，不需要 API 密钥。

## 字段

`candles_1m` 表以 `(symbol, ts_ms)` 为主键，其中 `ts_ms` 是 K 线开始时刻的 Unix 毫秒时间戳。`o,h,l,c` 是开高低收；`vol` 与 `turnover` 直接对应 Bybit 响应中的 `volume` 与 `turnover`。价格与成交量保存为文本，以保留 API 原始小数精度；计算时可转换为浮点或 Python `Decimal`。未完结 K 线不会入库：Bybit 最新一行是仍在形成的当前分钟，其收盘时间未到时整行丢弃，下次同步再写入已完结版本。

`instruments` 保存当前产品状态与上市时间；`ingest_state` 保存断点、同步时间和最近错误。回溯按 Top20 轮流分页，任务中断后可从断点恢复；每次写入按主键去重。

Bybit 的 `end` 参数是**闭区间**：一次请求返回截至 `end` 的至多 1000 根 K 线，因此向后翻页时把下一次 `end` 设为上一页最旧一根的时间戳。向前同步使用 `end = now`。

## 传输方式

本机无法直连 Bybit，采集脚本通过 `ssh nas` 转发到远端执行，远端只用 Python 标准库 `urllib` 请求已校验的公共 GET：

- 本地先按固定白名单校验每个端点与参数（`/v5/market/time`、`/v5/market/instruments-info`、`/v5/market/tickers`、`/v5/market/kline`），白名单外的请求一律拒绝。
- 校验后的请求以 JSON 行发送给远端固定脚本；远端只执行这些 GET，不读取本地文件、不持有任何凭据。
- 整个运行复用同一条 SSH 连接。每次握手在远端要数秒，若每页重连，多年回填会被握手时间拖垮。
- 一个币种一页、20 个币种一批批量请求，一批只做一次往返。

## 常用命令

```bash
cd /home/nicholas/data/bybit-backtest
python3 collector.py status
python3 collector.py init
python3 collector.py sync
python3 collector.py backfill --max-pages 800 --since 2021-07-05T00:00:00Z
python3 collector.py repair --max-pages 80
python3 -c "import sqlite3; print(sqlite3.connect('bybit_spot_1m.sqlite3').execute('PRAGMA quick_check').fetchone()[0])"
```

服务器的 `crond` 每 5 分钟执行一次 `sync`，每 10 分钟执行一次有限批次的 `backfill`。两个任务分别使用 `flock` 防止重叠，日志写在同一目录的 `sync.log` 和 `backfill.log`。`status` 的 `backfill_complete` 为 `true` 表示该交易对已回溯到交易所提供的最早数据。`missing_minutes_within_range` 是已采集首尾时间之间的分钟缺口数；回溯仍在进行时，请以该字段和 `backfill_cursor_utc` 判断进度。

## 查询示例

```sql
SELECT datetime(ts_ms / 1000, 'unixepoch') AS utc_time,
       CAST(o AS REAL) AS open,
       CAST(h AS REAL) AS high,
       CAST(l AS REAL) AS low,
       CAST(c AS REAL) AS close,
       CAST(vol AS REAL) AS base_volume,
       CAST(turnover AS REAL) AS usdt_volume
FROM candles_1m
WHERE symbol = 'BTCUSDT'
  AND ts_ms >= 1790812800000
  AND ts_ms < 1790899200000
ORDER BY ts_ms;
```

该库是公开市场 K 线库，不包含逐笔成交、历史订单簿、账户成交、交易费率或滑点模型。回测时需自行设定手续费与滑点，并检查策略时段的缺口数。Bybit 历史接口能返回的最早日期以采集完成后的 `status` 结果为准。
