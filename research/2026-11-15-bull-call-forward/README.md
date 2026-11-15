# 转强后的 BTC/ETH 看涨价差纸面观察

这是独立于生产机器人和原看跌实验的新研究。用户已授权持续观察并在固定条件满足后开始纸面验证；没有实盘授权。使用 Bybit 公开数据，持仓及成交是模拟。规则见 [PROTOCOL.md](PROTOCOL.md)，当前结果见 [run-v1/STATUS.md](run-v1/STATUS.md)。

## 运行

在仓库根目录，用项目 Python 3.12 和隔离模式启动；持续进程已启动时不能再开第二份。

```bash
caffeinate -i .venv/bin/python -I research/2026-11-15-bull-call-forward/forward.py --watch
```

单次采集并更新（只有未运行同一账本时才可用）：

```bash
.venv/bin/python -I research/2026-11-15-bull-call-forward/forward.py
```

停止使用该终端的 Ctrl-C。恢复使用原命令，同一 run-v1 校验完整历史后续跑，五天期限从原始冻结计算，不因重启延长。不要修改冻结代码、协议、快照、账本或 manifest；若要改策略，另建版本和研究目录。运行锁防止同一运行重复采样。原看跌采样进程保持独立。

## 数据和证据

- forward.py：选合约、组合预算、信号/对向冲突门槛、逐腿意图及模拟账本。
- market.py：只允许公开行情 GET，原始响应保存于 snapshots/market-*/request-*/response.json 各自的新空目录；哈希清单支持核验。数据目录不运行解释器或构建工具。
- protocol.json、PROTOCOL.md：冻结规则，含精确转强条件、选合约顺序和退出条件。
- run-v1/manifest.json 与 frozen/：首次采集之前冻结的文件 SHA-256 和副本。
- run-v1/evidence/：每次读取原看跌账本时的独立副本与门槛结论；原文件不改动。
- run-v1/state.json、STATUS.md、heartbeat.json：模拟账本、可读状态和运行健康信息。
- run-v1/FINAL_REPORT.md：全部结束或五天观察截止后自动生成。未完成平仓明确标记 unresolved，不补造结算。
- test_bull_forward.py、test_market.py：合成输入测试；与真实采样账本区分。

后续原始行情保留本地并由账本引用校验，不应将整个五天数据目录无条件加入版本库。若归档账本，需要一起保存它引用的原始响应和对向证据，否则完整性检查会失败。

## 运行边界

每15分钟采集；行情缺口超过30分钟记异常，运行依赖此电脑、终端、网络及应用（通知任务）。保持唤醒只防空闲睡眠，关闭笔记本盖子仍可能暂停。正常未转强时零成交；原看跌同标的未平仓也不入场。组合25 USDT只是完整价差开仓理论风险口径，不包含未来费用、异常执行及保证金。单独长腿支出另限100 USDT。

盘口响应时间比决策更晚不等于报价逐合约更新，也不能证明真实成交。任何少量样本或零交易都不能证明持续盈利。

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
