# 双腿价差套利阶段四：影子盘多配对并行观察（2026-11-24）

日期：2026-11-24。接在[阶段三](spread-arbitrage-phase3-2026-11-22.md)、[影子盘](spread-arbitrage-shadow-2026-11-22.md)、
[FR-07.1 协整检验](spread-arbitrage-cointegration-2026-11-23.md)与[FR-07.2 滚动寻优](spread-arbitrage-optimization-2026-11-23.md)之后。

PRD FR-07.3 的门槛是「**某组参数 + 某个标的**」：影子 Sharpe > 2.0、最大回撤 < 3%、闭环样本不少于 5 笔，
7 天影子验证通过后**仍需人工确认**才能进入灰度。此前 FR-07.1 提名清单里的配对只存在于离线报告，
要进 PRD 那条验证链路，必须先出现在影子盘的采样清单里。本阶段做这件事。

## 一、做了什么

- 配置写回走仓库自带的 `settings.writer.write_spread`——与管理页「影子盘配置」写回是**同一个写入器**：
  校验 → 备份 → 原子替换 → revision 冲突保护，不另造一条写配置的旁路。
- NAS `config.yaml` 的 `spread.pairs` 从 1 个配对扩到 5 个，**全部 `enabled: false`**；
  `spread.enabled` 与 `spread.auto.enabled` 仍为 `false`。
- 新增配对的 β 取自 FR-07.1 实跑提名（固定 2 阶滞后，`var/cointegration/2026-11-23-top50`）：

| 配对 | β | 半衰期(h) | FR-07.1 稳健性 |
| --- | --- | --- | --- |
| `NEARUSDT:ANTUSDT` | 0.924546 | 19.7 | 固定 2 阶与 BIC 两种滞后口径共同入榜 |
| `DOGEUSDT:LINKUSDT` | 1.065449 | 27.6 | 两种口径共同入榜 |
| `ACEUSDT:ADAUSDT` | 1.382475 | 17.3 | 仅固定 2 阶口径入榜 |
| `ETHUSDT:LINKUSDT` | 0.901600 | 44.8 | 两种口径共同入榜 |

- 参数沿用当前默认口径（`z_entry=2.0`、`z_exit=0.5`、`z_stop=3.5`、`auto.window=60`），
  与在册配对**同一组参数**，正好对应 PRD「某组参数 + 某个标的」的判定语义；
  `max_notional=100`、`max_unhedged_notional=20` 也与在册配对一致。

## 二、明确没有做的事

- **没有启用执行面**：配对 `enabled: false` 只控制真实执行面，影子盘仍只读公开行情，
  不加载凭据、不下真实订单、不改参数、不灰度、不实盘晋级。
- **没有替换在册配对**：在册 `BTCUSDT:ETHUSDT`（`hedge_ratio=1.0`）原样保留，作为基线继续积累。
- **没有推翻 FR-07.1 的结论**：提名清单仍只是人工审阅材料；本阶段只是让候选进入观察，
  不构成「自动晋级」，也不构成「换标的」。

## 三、发现的三个差距（留给人工决策，本阶段不改）

1. **提名池的符号空间大于配对模型的符号空间。** `LINKUSDT:ETHPERP` 的 β 为正（1.060481）且通过协整检验，
   但 `SpreadPairSettings` 的符号规则只接受 `*USDT` 与 `...-26MAR27` 这类交割合约，
   `ETHPERP` 进不了配置，写回时被校验拒绝。该提名项本轮未进影子盘。
2. **反向关系（β<0）无法用当前价差口径建模。** FR-07.1 提名里 5 个反向配对
   （`NEARUSDT:ARIAUSDT`、`BLESSUSDT:MSTRUSDT`、`BLESSUSDT:BNBUSDT`、`BLESSUSDT:ETHPERP`、`BLESSUSDT:SUIUSDT`）
   过不了 `hedge_ratio > 0` 校验。要评估它们需要另立建模口径（例如改比价而非价差），是独立工作项。
3. **在册配对的 β 与 FR-07.1 估计值不一致。** 配置是 `hedge_ratio=1.0`，FR-07.1 对同一对估出 β=0.7143。
   本轮**不动**它：滚动窗口的价差序列由 β 在评估时决定，中途改 β 会把已积累样本的解释口径改写。
   β 修正与「换不换标的」一起作为人工审阅项。

## 四、运行观察（NAS，2026-11-24）

- 5 个配对都已开始写入 `spread_signal_sample`（`environment='shadow'`），新配对与在册配对同批采样。
- **新配对需要预热**：滚动窗口 60 根样本，实测采样间隔约 87 秒，约 1.5 小时后才能产出第一个信号；
  在此之前的日志是「影子盘采样被拒：`XXX`：样本不足」。
- **「样本过期」拒绝是既有行为**：信号评估要求最新样本年龄 ≤ `shadow.max_signal_age_ms`（30 秒），
  而采样间隔约 87 秒，所以多数 tick 的评估会被拒，只有采样后 30 秒内的那次可能产出信号。
  v13 发布前后该拒绝频率相同，本轮未改口径。
- **修复后的账本在产生真实数据**：`ledger_version=1` 的新账本在约 13 小时内已有 8 条 OPEN、6 条 CLOSE、
  2 条 STOP；旧账（原 26 条 OPEN、0 条 CLOSE）原样留存但排除在审阅指标之外。
- 服务侧：postgres / worker / web 均 active，worker `NRestarts=0`，`last_error` 为空，
  `state=PAUSED_OPEN`、`lease=NEEDS_CONFIRMATION` 等既有暂停保持不变，未代为解除。

## 五、审阅时间表

- 观察窗仍是 `observation_days=7` 自然日滚动。候选配对自 2026-11-24 09:17（UTC+8）起算，
  **最早 2026-11-30 之后**才可能凑够「闭环 ≥ 5 笔」的样本门槛；Sharpe 与回撤还要再等闭环序列。
- 判定仍按**单个配对**：达标与否只看该配对自己的闭环序列；汇总口径（所有配对盈亏交织）只作参考，
  不能用来判门槛。
- 候选只是人工审阅提示。系统不会自动生成配置、不推送晋级、不灰度放量。

## 六、复现

```bash
# NAS 上执行；worker 需先停机，避免配置指纹变化触发 SERVICE_FAILURE 暂停。
podman run --rm -i --network=host --entrypoint python \
  -v "$HOME/.config/bybitbot:/run/bybitbot:rw,z" localhost/bybitbot:0.1.0 - <<'PY'
from pathlib import Path
from bybitbot.settings.writer import read_shadow_config, write_spread

CANDIDATES = [
    ("NEARUSDT:ANTUSDT", "NEARUSDT", "ANTUSDT", "0.924546"),
    ("DOGEUSDT:LINKUSDT", "DOGEUSDT", "LINKUSDT", "1.065449"),
    ("ACEUSDT:ADAUSDT", "ACEUSDT", "ADAUSDT", "1.382475"),
    ("ETHUSDT:LINKUSDT", "ETHUSDT", "LINKUSDT", "0.901600"),
]

path = Path("/run/bybitbot/config.yaml")
saved = read_shadow_config(path)
if not saved.writable:
    raise SystemExit(f"配置不可写：{saved.read_only_reason}")

pairs = [
    {
        "pair_key": p.pair_key, "symbol_a": p.symbol_a, "symbol_b": p.symbol_b,
        "hedge_ratio": str(p.hedge_ratio), "z_entry": str(p.z_entry),
        "z_exit": str(p.z_exit), "z_stop": str(p.z_stop),
        "max_notional": {"value": str(p.max_notional.value), "currency": p.max_notional.currency},
        "max_unhedged_notional": {"value": str(p.max_unhedged_notional.value), "currency": p.max_unhedged_notional.currency},
        "enabled": p.enabled,
    }
    for p in saved.config.spread.pairs
]
known = {p["pair_key"] for p in pairs}
for key, a, b, beta in CANDIDATES:
    if key in known:
        continue
    pairs.append({
        "pair_key": key, "symbol_a": a, "symbol_b": b, "hedge_ratio": beta,
        "z_entry": "2.0", "z_exit": "0.5", "z_stop": "3.5",
        "max_notional": {"value": "100", "currency": "USDT"},
        "max_unhedged_notional": {"value": "20", "currency": "USDT"},
        "enabled": False,
    })
print(write_spread(path, {"pairs": pairs, "auto": {}, "shadow": {}}, revision=saved.revision))
PY
```

写回后需重启 worker 与 web 才生效：worker 加载新配对清单，web 的按配对审阅表用启动时配置。
