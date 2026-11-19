"""Cash-only, long Turtle System 2 adaptation with bounded minute streaming."""

import math
import re
from collections import deque
from decimal import Decimal, InvalidOperation

BAR_MS = 60_000
TIMEFRAMES = {60: "1 小时", 240: "4 小时", 1440: "1 天（UTC）"}
DEFAULTS = {
    "signal_minutes": 1440,
    "entry_period": 55,
    "exit_period": 20,
    "atr_period": 20,
    "risk_pct": 1,
    "stop_n": 2,
    "add_n": 0.5,
    "max_units": 4,
    "min_trade_usdt": 5,
}


def turtle_params(data):
    out = {}
    labels = {
        "signal_minutes": "信号周期",
        "entry_period": "入场周期",
        "exit_period": "退出周期",
        "atr_period": "ATR 周期",
        "risk_pct": "每单位 1N 风险",
        "stop_n": "止损 N 倍数",
        "add_n": "加仓 N 间距",
        "max_units": "最大持仓单位",
        "min_trade_usdt": "最小模拟成交额",
    }
    for key, default in DEFAULTS.items():
        raw = data.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(str(raw)) > 32:
            raise ValueError(f"{labels[key]} 格式无效")
        if key in ("signal_minutes", "entry_period", "exit_period", "atr_period", "max_units"):
            if not re.fullmatch(r"[0-9]{1,6}", str(raw)):
                raise ValueError(f"{labels[key]} 必须为整数")
            out[key] = int(raw)
        else:
            try:
                value = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"{labels[key]} 必须为数值") from exc
            if not value.is_finite():
                raise ValueError(f"{labels[key]} 必须为有限数值")
            out[key] = float(value)
    if out["signal_minutes"] not in TIMEFRAMES:
        raise ValueError("海龟信号周期请选择 1 小时、4 小时或 1 天")
    if not 5 <= out["entry_period"] <= 200 or not 2 <= out["exit_period"] < out["entry_period"]:
        raise ValueError("海龟周期须满足 5 ≤ 入场周期 ≤ 200，2 ≤ 退出周期 < 入场周期")
    if not 2 <= out["atr_period"] <= 100 or not 1 <= out["max_units"] <= 4:
        raise ValueError("ATR 周期须为 2 至 100，最大持仓单位须为 1 至 4")
    if (
        not 0.01 <= out["risk_pct"] <= 2
        or not 0.5 <= out["stop_n"] <= 5
        or not 0.25 <= out["add_n"] <= 2
    ):
        raise ValueError("每单位 1N 风险须为 0.01% 至 2%，止损为 0.5 至 5N，加仓间距为 0.25 至 2N")
    if not 0 <= out["min_trade_usdt"] <= 100_000:
        raise ValueError("最小模拟成交额须为 0 至 100000 USDT")
    if "start" in data and "end" in data:
        step = out["signal_minutes"] * BAR_MS
        first = ((data["start"] + step - 1) // step) * step
        complete = max(0, (data["end"] - first) // step)
        required = max(out["entry_period"], out["atr_period"]) + 1
        if complete < required:
            raise ValueError(
                f"海龟回测至少需要 {required} 根完整的"
                f"{TIMEFRAMES[out['signal_minutes']]}信号 K 线；"
                f"当前仅 {complete} 根。请扩大区间或缩短信号周期；预热使用所选区间内数据。"
            )
    return out


def calculate_turtle(params, bars, data_sha256=None):
    p = turtle_params(params)
    expected = (
        len(bars) if hasattr(bars, "__len__") else (params["end"] - params["start"]) // BAR_MS
    )
    fee, slip = params["fee_bps"] / 10_000, params["slippage_bps"] / 10_000
    capital = float(params["capital"])
    cash, qty = capital, 0.0
    fees = turnover = slippage_cost = invested = realized_sum = 0.0
    completed = wins = orders = entries = additions = stop_exits = channel_exits = 0
    skipped_entries = cash_limited_orders = exposure = max_units_seen = 0
    units = 0
    unit_qty = entry_n = stop = last_fill = None
    add_blocked = False
    pending = None
    highs, lows = deque(maxlen=p["entry_period"]), deque(maxlen=p["exit_period"])
    atr_sum = 0.0
    atr = previous_close = None
    signal_bars = 0
    bucket = None
    bucket_count = 0
    step = p["signal_minutes"] * BAR_MS
    peak, drawdown = capital, 0.0
    curve, trades = [], deque(maxlen=30)
    stride = max(1, math.ceil(expected / 500))
    first_ts = first_open = last_ts = last_close = None
    count = 0

    def buy(ts, reference, desired_qty, reason):
        nonlocal cash, qty, fees, turnover, slippage_cost, invested, orders, units
        nonlocal stop, last_fill, max_units_seen, cash_limited_orders
        px = reference * (1 + slip)
        affordable = cash / (px * (1 + fee))
        amount = min(desired_qty, affordable)
        notional = amount * px
        if amount <= 0 or notional + 1e-9 < p["min_trade_usdt"]:
            return False
        limited = affordable + desired_qty * 1e-12 < desired_qty
        cash_limited_orders += int(limited)
        paid_fee = notional * fee
        cost = notional + paid_fee
        cash = max(0.0, cash - cost)
        qty += amount
        invested += cost
        fees += paid_fee
        turnover += notional
        slippage_cost += amount * (px - reference)
        orders += 1
        units += 1
        max_units_seen = max(max_units_seen, units)
        last_fill = px
        candidate = px - p["stop_n"] * entry_n
        stop = candidate if stop is None else max(stop, candidate)
        trades.append(
            {
                "ts_ms": ts,
                "side": "buy",
                "reason": reason,
                "price": px,
                "qty": amount,
                "fee_usdt": paid_fee,
                "units_after": units,
                "stop_price": stop,
                "cash_limited": limited,
            }
        )
        return True

    def sell(ts, reference, reason):
        nonlocal cash, qty, invested, fees, turnover, slippage_cost, orders, completed, wins
        nonlocal realized_sum, units, stop, entry_n, unit_qty, last_fill, add_blocked
        nonlocal stop_exits, channel_exits
        px = reference * (1 - slip)
        notional = qty * px
        paid_fee = notional * fee
        proceeds = notional - paid_fee
        pnl = proceeds - invested
        cash += proceeds
        fees += paid_fee
        turnover += notional
        slippage_cost += qty * (reference - px)
        orders += 1
        completed += 1
        wins += int(pnl > 0)
        realized_sum += pnl / invested * 100
        stop_exits += int(reason == "stop")
        channel_exits += int(reason == "channel_exit")
        trades.append(
            {
                "ts_ms": ts,
                "side": "sell",
                "reason": reason,
                "price": px,
                "qty": qty,
                "fee_usdt": paid_fee,
                "pnl": pnl,
                "units_after": 0,
            }
        )
        qty = invested = 0.0
        units = 0
        stop = entry_n = unit_qty = last_fill = None
        add_blocked = False

    for i, (ts, opened, high, low, closed) in enumerate(bars):
        if i == 0:
            first_ts, first_open = ts, opened
        elif ts != last_ts + BAR_MS:
            raise ValueError("海龟回测分钟数据不连续")
        count, last_ts, last_close = i + 1, ts, closed
        exited = entered = False
        # Only signals from an already completed higher-timeframe candle exist here.
        if pending and pending[0] == "channel_exit" and qty > 0:
            sell(ts, opened, "channel_exit")
            exited = True
        if qty > 0 and low <= stop:
            sell(ts, min(opened, stop), "stop")
            exited = True
        if pending and pending[0] == "entry" and qty == 0 and not exited:
            entry_n = pending[1]
            unit_qty = cash * p["risk_pct"] / 100 / entry_n
            if buy(ts, opened, unit_qty, "entry"):
                entries += 1
                entered = True
            else:
                skipped_entries += 1
                entry_n = unit_qty = None
        pending = None
        if entered and low <= stop:
            sell(ts, min(opened, stop), "stop")
            exited = True
        # At most one add per minute; never add after a stop or on the entry minute.
        if qty > 0 and not entered and not exited and units < p["max_units"] and not add_blocked:
            trigger = last_fill + p["add_n"] * entry_n
            if high >= trigger:
                if buy(ts, max(opened, trigger), unit_qty, "add"):
                    additions += 1
                    # Minute OHLC cannot resolve high/low order: assume the raised stop hits.
                    if low <= stop:
                        sell(ts, min(opened, stop) if opened >= trigger else stop, "stop")
                        exited = True
                else:
                    add_blocked = True

        exposure += int(qty > 0 or entered or exited)
        equity = cash + qty * closed * (1 - slip) * (1 - fee)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        if i % stride == 0 or i == expected - 1:
            curve.append([ts, round(equity, 4)])

        current_bucket = ts // step
        if current_bucket != bucket:
            bucket, bucket_count = current_bucket, 0
            bucket_high, bucket_low = high, low
        bucket_count += 1
        bucket_high, bucket_low = max(bucket_high, high), min(bucket_low, low)
        if (ts + BAR_MS) % step == 0 and bucket_count == p["signal_minutes"]:
            signal_bars += 1
            tr = (
                bucket_high - bucket_low
                if previous_close is None
                else max(
                    bucket_high - bucket_low,
                    abs(bucket_high - previous_close),
                    abs(bucket_low - previous_close),
                )
            )
            if atr is None:
                atr_sum += tr
                if signal_bars == p["atr_period"]:
                    atr = atr_sum / p["atr_period"]
            else:
                atr = (atr * (p["atr_period"] - 1) + tr) / p["atr_period"]
            # Channels exclude the candle whose close is being tested.
            if qty > 0 and len(lows) == p["exit_period"] and closed < min(lows):
                pending = ("channel_exit", None)
            elif (
                qty == 0
                and len(highs) == p["entry_period"]
                and atr is not None
                and atr > 0
                and closed > max(highs)
            ):
                pending = ("entry", atr)
            highs.append(bucket_high)
            lows.append(bucket_low)
            previous_close = closed
    if not count or count != expected:
        raise ValueError("回测数据区间不完整")
    if signal_bars < max(p["entry_period"], p["atr_period"]) + 1:
        raise ValueError("海龟回测完整信号 K 线不足，请扩大区间")
    marked = cash + qty * last_close
    final = cash + qty * last_close * (1 - slip) * (1 - fee)
    hold_qty = capital / (first_open * (1 + slip) * (1 + fee))
    hold_final = hold_qty * last_close * (1 - slip) * (1 - fee)
    digest = data_sha256.hexdigest() if hasattr(data_sha256, "hexdigest") else data_sha256
    return {
        "engine_version": 4,
        "strategy": "turtle",
        "inst_id": params["inst_id"],
        "data_sha256": digest,
        "start_ms": first_ts,
        "end_ms": last_ts + BAR_MS,
        "bars": count,
        "signal_bars": signal_bars,
        "capital": capital,
        "final_equity": round(final, 4),
        "return_pct": round((final / capital - 1) * 100, 4),
        "buy_hold_pct": round((hold_final / capital - 1) * 100, 4),
        "max_drawdown_pct": round(drawdown * 100, 4),
        "exposure_pct": round(exposure / count * 100, 2),
        "fees_paid": round(fees, 4),
        "turnover_usdt": round(turnover, 4),
        "slippage_cost_usdt": round(slippage_cost, 4),
        "cash_usdt": round(cash, 4),
        "estimated_exit_cost": round(marked - final, 4),
        "completed_trades": completed,
        "win_rate_pct": round(wins / completed * 100, 2) if completed else None,
        "avg_realized_trade_pct": round(realized_sum / completed, 4) if completed else None,
        "open_position": qty > 0,
        "position_qty": qty,
        "position_units": units,
        "stop_price": stop,
        "position_n": entry_n,
        "entries": entries,
        "additions": additions,
        "stop_exits": stop_exits,
        "channel_exits": channel_exits,
        "max_units_seen": max_units_seen,
        "skipped_entries": skipped_entries,
        "cash_limited_orders": cash_limited_orders,
        "last_signal_unfilled": pending is not None,
        "order_count": orders,
        "orders_truncated": orders > len(trades),
        "recent_orders": list(trades),
        "equity_curve": curve,
        "turtle_params": p,
        "assumptions": {
            "variant": "system2_spot_close_confirmed",
            "long_only": True,
            "leverage": False,
            "fractional_quantities": True,
            "signal_timezone": "UTC",
            "warmup": "inside_requested_window",
            "entry_exit": "completed_signal_close_then_next_minute_open",
            "channel_excludes_current_bar": True,
            "n_frozen_at_entry": True,
            "unit_risk": "risk_pct_of_entry_equity_per_1N",
            "intraminute": "existing_stop_first_then_at_most_one_add_then_raised_stop",
            "max_adds_per_minute": 1,
            "forced_exit": False,
            "valuation": "estimated_liquidation_after_compound_costs",
            "campaign_return": "net_pnl_divided_by_total_entry_and_add_cost",
        },
    }
