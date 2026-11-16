# 公共层 bull-call 前向引擎 v2

这是独立、尚未启动的新研究引擎。原 v1 继续按冻结协议观察至 2026-11-20；本次交付不切换或重启旧采样器。

- `market.py` 只保留策略信号；公共传输、快照校验、K线与期权规格来自 `bybitbot.marketdata`。
- `forward.py` 复用 `bybitbot.terms` 的费用、严格规格与估值计算，以及 `bybitbot.money` 的共同步长与价格精度。
- `protocol.json` 固定 v2 身份及独立的可执行平仓估值政策；完整规则见 [PROTOCOL.md](PROTOCOL.md)。
- `prepare_run` 冻结本地引擎、协议和全部递归共享源码；当前源码与归档副本变动均拒绝续跑。
- 默认 `run-v2` 与本目录 `snapshots/` 是全新证据空间。原 bull-call/bear-put 目录及别名会在任何写入前拒绝。

代码需使用本项目已安装的 Python 环境。以下是以后另行启动独立实验的命令说明，交付过程没有执行它：

```bash
.venv/bin/python -I research/2026-11-15-bull-call-forward-v2/forward.py --watch
```

`--run-dir` 可指定全新的 v2 账本目录，不得复用任何旧研究账本。输入只来自公开行情；不读取交易凭据、不发送交易写入。默认对向账本只读指向原 bear-put 的 run-v1，缺证据即阻挡入场。

自动化验证（合成数据，不采集网络）：

```bash
.venv/bin/python -m pytest tests/test_marketdata_snapshots.py tests/test_bull_call_v2.py -q
```

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
