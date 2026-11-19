"""Shared, bounded parameter definitions for nine additional spot strategies."""

import math
import re

from market_config import INSTRUMENTS


def field(label, default, low=None, high=None, kind="int", options=None, step=None):
    return {
        "label": label,
        "default": default,
        "min": low,
        "max": high,
        "type": kind,
        "options": options,
        "step": step or (1 if kind == "int" else 0.01),
    }


TF = field(
    "信号周期", 240, kind="select", options=[[60, "1 小时"], [240, "4 小时"], [1440, "1 天（UTC）"]]
)
DAILY = {**TF, "default": 1440}
COMMON = {
    "signal_minutes": TF,
    "min_trade_usdt": field("单笔最低成交额（USDT）", 5, 0, 100000, "float"),
}
RISK = {
    "position_pct": field("最大资金投入（%）", 95, 1, 100, "float"),
    "atr_period": field("ATR 周期（根）", 14, 2, 100),
    "stop_atr": field("初始止损 ATR 倍数", 2, 0.5, 10, "float"),
}


def spec(name, note, fields, multi=False):
    return {"name": name, "note": note, "multi": multi, "fields": {**COMMON, **fields}}


STRATEGIES = {
    "momentum_rotation": spec(
        "多币种动量轮动",
        "按短、长周期收益平均排名，只有长周期收益为正的币种可入选；定期等权配置，其余保留现金。固定币池含事后选币偏差。",
        {
            "signal_minutes": DAILY,
            "short_lookback": field("短期收益周期（根）", 30, 2, 365),
            "long_lookback": field("长期收益周期（根）", 90, 3, 730),
            "hold_count": field("最多持有币种", 3, 1, 10),
            "rebalance_bars": field("调仓检查间隔（根）", 7, 1, 90),
            "rebalance_threshold_pct": field("最小调仓偏离（百分点）", 3, 0.1, 20, "float"),
        },
        True,
    ),
    "bollinger_reversion": spec(
        "布林带均值回归",
        "上一收盘在下轨外，当前收盘重回下轨内时买入；回到中轨退出，并设置 ATR 初始止损。",
        {
            **RISK,
            "band_period": field("布林带周期（根）", 20, 5, 500),
            "band_std": field("标准差倍数", 2, 0.5, 4, "float"),
        },
    ),
    "trend_pullback": spec(
        "趋势过滤＋均线回踩",
        "中期均线高于长期均线且长期均线上升，价格回踩短线后收盘重新站上短线时买入；跌破中线退出。",
        {
            **RISK,
            "pullback_period": field("回踩均线周期（根）", 20, 2, 100),
            "trend_fast": field("中期均线周期（根）", 50, 3, 300),
            "trend_slow": field("长期均线周期（根）", 200, 4, 500),
        },
    ),
    "spot_grid": spec(
        "现货网格",
        "网格上下界相对回测首根开盘确定后固定。每档预算固定，先卖已有档位、再买空档；新买档位最早下一分钟卖出。超出区间仍持币。",
        {
            "lower_pct": field("下界 / 初始价格（%）", 70, 1, 99, "float"),
            "upper_pct": field("上界 / 初始价格（%）", 130, 101, 1000, "float"),
            "grid_count": field("网格数量", 10, 2, 100),
            "grid_mode": field(
                "网格间距",
                "geometric",
                kind="select",
                options=[["arithmetic", "等差"], ["geometric", "等比"]],
            ),
        },
    ),
    "low_corr_portfolio": spec(
        "低相关组合＋波动率配权",
        "仅使用过去收益估计相关性和波动率，先选低波动币，再筛选最大绝对相关性符合阈值的币。按波动率倒数配权并限制单币占比，剩余留现金；不等同于完整风险平价。",
        {
            "signal_minutes": DAILY,
            "corr_lookback": field("相关性与波动窗口（根）", 60, 10, 365),
            "max_correlation": field("最大绝对相关系数", 0.7, 0, 0.99, "float"),
            "hold_count": field("最多持有币种", 5, 1, 10),
            "max_weight_pct": field("单币权重上限（%）", 40, 5, 100, "float"),
            "rebalance_bars": field("调仓检查间隔（根）", 30, 1, 90),
            "rebalance_threshold_pct": field("最小调仓偏离（百分点）", 3, 0.1, 20, "float"),
        },
        True,
    ),
    "squeeze_breakout": spec(
        "波动收缩突破",
        "此前布林带宽度位于历史低分位，随后收盘向上突破前一根上轨且放量时买入；跌破中轨退出。成交量使用基础币成交量。",
        {
            **RISK,
            "band_period": field("布林带周期（根）", 20, 5, 200),
            "band_std": field("标准差倍数", 2, 0.5, 4, "float"),
            "squeeze_lookback": field("带宽分位窗口（根）", 60, 10, 365),
            "squeeze_percentile": field("收缩分位（%）", 20, 1, 50, "float"),
            "volume_period": field("成交量均值周期（根）", 20, 2, 100),
            "volume_multiple": field("放量倍数", 1.5, 0.5, 5, "float"),
        },
    ),
    "macd_adx": spec(
        "MACD＋ADX 趋势过滤",
        "MACD 金叉且 ADX 达到阈值、+DI 高于 −DI 时买入；MACD 死叉退出，另有 ATR 止损。",
        {
            **RISK,
            "macd_fast": field("MACD 快线（根）", 12, 2, 100),
            "macd_slow": field("MACD 慢线（根）", 26, 3, 200),
            "macd_signal": field("MACD 信号线（根）", 9, 2, 100),
            "adx_period": field("ADX 周期（根）", 14, 2, 100),
            "adx_threshold": field("ADX 入场门槛", 25, 5, 60, "float"),
        },
    ),
    "supertrend": spec(
        "Supertrend 趋势跟随",
        "ATR 上下带按收盘递推，转多时买入、转空时退出；"
        "已知的下轨作为分钟止损，只在信号 K 线完成后抬高。",
        {
            **RISK,
            "atr_period": field("ATR 周期（根）", 10, 2, 100),
            "multiplier": field("Supertrend ATR 倍数", 3, 1, 10, "float"),
        },
    ),
    "budget_dca": spec(
        "固定预算分批买入",
        "初始资金分成固定份数，从首根开盘开始按固定间隔买入；预算全部预先存入，未投入部分保留现金，期间不卖出。",
        {
            "tranches": field("预算份数", 52, 2, 1000),
            "dca_minutes": field(
                "买入间隔",
                10080,
                kind="select",
                options=[[60, "每小时"], [1440, "每天"], [10080, "每周"], [43200, "每 30 天"]],
            ),
        },
    ),
}
MULTI_STRATEGIES = tuple(k for k, v in STRATEGIES.items() if v["multi"])
for key in ("spot_grid", "budget_dca"):
    STRATEGIES[key]["fields"].pop("signal_minutes")


def warmup(p):
    s = p["strategy"]
    if s == "momentum_rotation":
        return p["long_lookback"] + 1
    if s == "low_corr_portfolio":
        return p["corr_lookback"] + 1
    if s in ("spot_grid", "budget_dca"):
        return 0
    if s == "trend_pullback":
        return max(p["trend_slow"] + 1, p["atr_period"])
    if s == "macd_adx":
        return max(p["macd_slow"] + p["macd_signal"], 2 * p["adx_period"], p["atr_period"]) + 1
    if s == "squeeze_breakout":
        return max(
            p["band_period"] + p["squeeze_lookback"], p["volume_period"] + 1, p["atr_period"]
        )
    if s == "supertrend":
        return p["atr_period"] + 1
    return max(p["band_period"] + 1, p["atr_period"])


def strategy_params(data):
    s = data.get("strategy")
    if s not in STRATEGIES:
        raise ValueError("策略无效")
    p = {"strategy": s}
    for key, f in STRATEGIES[s]["fields"].items():
        raw = data.get(key, f["default"])
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(str(raw)) > 32:
            raise ValueError(f"{f['label']} 格式无效")
        if f["type"] == "select":
            allowed = [item[0] for item in f["options"]]
            value = (
                str(raw)
                if isinstance(f["default"], str)
                else int(raw)
                if re.fullmatch(r"[0-9]{1,6}", str(raw))
                else None
            )
            if value not in allowed:
                raise ValueError(f"{f['label']} 选项无效")
        elif f["type"] == "int":
            if not re.fullmatch(r"[0-9]{1,6}", str(raw)):
                raise ValueError(f"{f['label']} 必须为整数")
            value = int(raw)
        else:
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{f['label']} 必须为数值") from exc
        if f["type"] != "select" and (
            not math.isfinite(value) or not f["min"] <= value <= f["max"]
        ):
            raise ValueError(f"{f['label']} 须为 {f['min']} 至 {f['max']}")
        p[key] = value
    if s == "momentum_rotation" and p["short_lookback"] >= p["long_lookback"]:
        raise ValueError("短期收益周期须小于长期收益周期")
    if s == "trend_pullback" and not p["pullback_period"] < p["trend_fast"] < p["trend_slow"]:
        raise ValueError("回踩周期须小于中期周期，中期周期须小于长期周期")
    if s == "macd_adx" and p["macd_fast"] >= p["macd_slow"]:
        raise ValueError("MACD 快线周期须小于慢线周期")
    if s in MULTI_STRATEGIES:
        universe = data.get("universe")
        if (
            not isinstance(universe, list)
            or not 2 <= len(universe) <= 20
            or any(x not in INSTRUMENTS for x in universe)
            or len(set(universe)) != len(universe)
        ):
            raise ValueError("币池请选择 2 至 20 个不同的 Top20 币种")
        if p["hold_count"] > len(universe):
            raise ValueError("持有数量不可超过所选币池数量")
        # Preserve the cursor order: reordering here would mislabel streamed prices.
        p["universe"] = list(universe)
    p.setdefault("signal_minutes", 60)
    needed = warmup(p)
    if needed and "start" in data and "end" in data:
        step = p["signal_minutes"] * 60000
        first = ((data["start"] + step - 1) // step) * step
        complete = max(0, (data["end"] - first) // step)
        if complete < needed:
            raise ValueError(
                f"该策略至少需要 {needed} 根完整的 {p['signal_minutes']} 分钟信号 K 线，"
                f"当前 {complete} 根；请扩大区间。预热使用区间内数据。"
            )
    p.pop("strategy")
    return p
