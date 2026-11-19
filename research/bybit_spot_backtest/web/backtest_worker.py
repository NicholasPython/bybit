"""Bounded, fixed-strategy backtest worker. Reads one JSON job on stdin."""

import hashlib
import json
import math
import os
import resource
import sqlite3
import sys
from collections import deque
from itertools import tee
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contextlib

from backtest_config import BACKTEST_CPU_SECONDS, MAX_BACKTEST_MINUTES, validate_backtest_window
from market_config import INSTRUMENTS, SUPPORTED_INSTRUMENTS
from strategy_catalog import MULTI_STRATEGIES, STRATEGIES
from strategy_engine import calculate_research
from turtle_strategy import calculate_turtle

BAR_MS = 60_000
MAX_BARS = MAX_BACKTEST_MINUTES
DB_PATH = os.environ.get("BYBIT_WEB_DB", "/home/nicholas/data/bybit-backtest/bybit_spot_1m.sqlite3")


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (BACKTEST_CPU_SECONDS, BACKTEST_CPU_SECONDS))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
    with contextlib.suppress(OSError):
        os.nice(10)


def stream_single_bars(params):
    """Read one coin from a consistent snapshot without materializing history."""
    if params["inst_id"] not in SUPPORTED_INSTRUMENTS:
        raise ValueError("交易对无效")
    start, end = params["start"], params["end"]
    validate_backtest_window(start, end)
    digest = hashlib.sha256()

    def stream():
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            rows = db.execute(
                """SELECT ts_ms,o,h,l,c FROM candles_1m
                WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms""",
                (params["inst_id"], start, end),
            )
            for expected_ts in range(start, end, BAR_MS):
                row = rows.fetchone()
                if row is None or row[0] != expected_ts:
                    raise ValueError(
                        f"{params['inst_id']} 区间不完整，在时间戳 {expected_ts} 缺少 K 线；"
                        "请缩小区间或等待历史回溯，上市前无现货数据"
                    )
                ts, o, h, low_price, c = row
                digest.update(f"{ts},{o},{h},{low_price},{c}\n".encode("ascii"))
                bar = (int(ts), float(o), float(h), float(low_price), float(c))
                _, opened, high, low, closed = bar
                if not all(math.isfinite(value) and value > 0 for value in bar[1:]):
                    raise ValueError("区间内有无效价格，回测已停止")
                if high < max(opened, closed) or low > min(opened, closed) or high < low:
                    raise ValueError("区间内有不合理的 OHLC 数据，回测已停止")
                yield bar
        finally:
            db.close()

    return stream(), digest


def load_bars(params):
    """Small-fixture compatibility helper; production always uses the stream."""
    stream, digest = stream_single_bars(params)
    try:
        return list(stream), digest.hexdigest()
    finally:
        stream.close()


def stream_research_bars(params):
    """Consistent OHLCV snapshots; never fill missing minutes or listing history."""
    assets = params["universe"] if params["strategy"] in MULTI_STRATEGIES else [params["inst_id"]]
    if (
        not assets
        or any(a not in INSTRUMENTS for a in assets)
        or len(set(assets)) != len(assets)
        or len(assets) > 20
    ):
        raise ValueError("策略币池无效")
    start, end = params["start"], params["end"]
    validate_backtest_window(start, end)
    digest = hashlib.sha256(("research-ohlcv-v1:" + ",".join(assets) + "\n").encode("ascii"))

    def stream():
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            cursors = [
                db.execute(
                    "SELECT ts_ms,o,h,l,c,vol FROM candles_1m "
                    "WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                    (a, start, end),
                )
                for a in assets
            ]
            for ts in range(start, end, BAR_MS):
                out = []
                for asset, cursor in zip(assets, cursors, strict=False):
                    row = cursor.fetchone()
                    if row is None or row[0] != ts:
                        raise ValueError(
                            f"{asset} 在时间戳 {ts} 缺少分钟 K 线；"
                            "请选择完整的共同区间，上市前无数据"
                        )
                    try:
                        opened, high, low, close, volume = map(float, row[1:])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{asset} 有无效 OHLCV") from exc
                    if (
                        not all(math.isfinite(x) and x > 0 for x in (opened, high, low, close))
                        or not math.isfinite(volume)
                        or volume < 0
                        or high < max(opened, close)
                        or low > min(opened, close)
                        or high < low
                    ):
                        raise ValueError(f"{asset} 有无效 OHLCV")
                    digest.update((asset + "," + ",".join(map(str, row)) + "\n").encode("ascii"))
                    out.append((opened, high, low, close, volume))
                yield ts, out
        finally:
            db.close()

    return stream(), digest


def load_portfolio_bars(params):
    """Stream aligned candles in a single SQLite read snapshot; never fill gaps."""
    start, end = params["start"], params["end"]
    (end - start) // BAR_MS
    validate_backtest_window(start, end)
    assets = [asset["inst_id"] for asset in params["allocations"]]
    if (
        not 2 <= len(assets) <= len(INSTRUMENTS)
        or len(set(assets)) != len(assets)
        or any(inst not in SUPPORTED_INSTRUMENTS for inst in assets)
    ):
        raise ValueError("组合币种无效、重复或超过 20 个")
    digest = hashlib.sha256()
    digest.update(("portfolio-ohlc-v1:" + ",".join(assets) + "\n").encode("ascii"))

    def stream():
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            cursors = [
                db.execute(
                    """SELECT ts_ms,o,h,l,c FROM candles_1m
                WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms""",
                    (inst, start, end),
                )
                for inst in assets
            ]
            for ts in range(start, end, BAR_MS):
                opens, closes = [], []
                for inst, cursor in zip(assets, cursors, strict=False):
                    row = cursor.fetchone()
                    if row is None or row[0] != ts:
                        raise ValueError(
                            f"{inst} 在时间戳 {ts} 缺少 K 线；"
                            "请选择所有币种上市后且数据完整的共同区间"
                        )
                    _, o, h, low_price, c = row
                    try:
                        opened, high, low, closed = map(float, (o, h, low_price, c))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{inst} 有无效价格，回测已停止") from exc
                    if (
                        not all(math.isfinite(v) and v > 0 for v in (opened, high, low, closed))
                        or high < max(opened, closed)
                        or low > min(opened, closed)
                        or high < low
                    ):
                        raise ValueError(f"{inst} 有无效 OHLC，回测已停止")
                    digest.update(f"{inst},{ts},{o},{h},{low_price},{c}\n".encode("ascii"))
                    opens.append(opened)
                    closes.append(closed)
                yield ts, opens, closes
        finally:
            db.close()

    return stream(), digest


def calculate_portfolio(params, bars, digest=None):
    """Constant-weight long-only portfolio; close decisions execute next open."""
    allocations = params["allocations"]
    weights = [asset["weight_pct"] / 100 for asset in allocations]
    fee, slip = params["fee_bps"] / 10_000, params["slippage_bps"] / 10_000
    buy_factor, sell_factor = (1 + slip) * (1 + fee), (1 - slip) * (1 - fee)
    minimum = params["min_trade_usdt"]
    interval = params["interval_minutes"]
    threshold = params["threshold_pct"] / 100
    capital = float(params["capital"])
    cash = capital
    quantities = [0.0] * len(weights)
    initial_quantities = []
    orders = deque(maxlen=50)
    events = deque(maxlen=50)
    equity_curve, hold_curve = [], []
    fees_paid = turnover = 0.0
    order_count = rebalance_count = skipped = checks = 0
    pending = None
    peak = capital
    max_drawdown = 0.0
    expected = (params["end"] - params["start"]) // BAR_MS
    stride = max(1, math.ceil(expected / 500))
    last_prices, last_ts, count = None, None, 0

    def trade(index, quantity, side, ts, initial=False):
        nonlocal cash, fees_paid, turnover, order_count
        px = last_opens[index] * (1 + slip if side == "buy" else 1 - slip)
        notional = quantity * px
        commission = notional * fee
        if side == "buy":
            quantities[index] += quantity
            cash -= notional + commission
        else:
            quantities[index] -= quantity
            cash += notional - commission
        fees_paid += commission
        turnover += notional
        order_count += 1
        orders.append(
            {
                "ts_ms": ts,
                "inst_id": allocations[index]["inst_id"],
                "side": side,
                "price": px,
                "qty": quantity,
                "notional_usdt": notional,
                "fee_usdt": commission,
                "initial_allocation": initial,
            }
        )

    def rebalance(ts, prices):
        """Solve post-cost target equity, sell first, and cap buys at available cash."""
        marked = cash + sum(q * p for q, p in zip(quantities, prices, strict=False))
        low, high = 0.0, marked
        for _ in range(36):
            target = (low + high) / 2
            cost = 0.0
            for q, p, weight in zip(quantities, prices, weights, strict=False):
                delta = target * weight - q * p
                cost += delta * (buy_factor - 1) if delta > 0 else -delta * (1 - sell_factor)
            if target + cost > marked:
                high = target
            else:
                low = target
        # The lower bound prevents spending beyond cash through rounding.
        deltas = [
            low * weight / p - q for weight, p, q in zip(weights, prices, quantities, strict=False)
        ]
        for index, delta in enumerate(deltas):
            if delta < -1e-12 and -delta * prices[index] * (1 - slip) >= minimum:
                trade(index, min(-delta, quantities[index]), "sell", ts)
        buys = [
            (index, delta)
            for index, delta in enumerate(deltas)
            if delta > 1e-12 and delta * prices[index] * (1 + slip) >= minimum
        ]
        required = sum(delta * prices[index] * buy_factor for index, delta in buys)
        scale = min(1.0, max(0.0, cash) / required) if required else 0.0
        for index, delta in buys:
            quantity = delta * scale
            if quantity > 1e-12 and quantity * prices[index] * (1 + slip) >= minimum:
                trade(index, quantity, "buy", ts)

    for i, (ts, last_opens, closes) in enumerate(bars):
        if ts != params["start"] + i * BAR_MS:
            raise ValueError("组合时间戳未对齐")
        count = i + 1
        if i == 0:
            invested = capital / buy_factor
            for index, (weight, opened) in enumerate(zip(weights, last_opens, strict=False)):
                quantity = invested * weight / opened
                notional = quantity * opened * (1 + slip)
                if notional + max(1e-9, minimum * 1e-12) < minimum:
                    raise ValueError(
                        f"{allocations[index]['inst_id']} 初始配置成交额 {notional:.2f} USDT，"
                        f"低于单笔最低成交额 {minimum:.2f} USDT；"
                        "请降低门槛（默认 5 USDT）或提高组合资金/权重"
                    )
                trade(index, quantity, "buy", ts, initial=True)
            initial_quantities = quantities.copy()
        elif pending is not None:
            before_orders, before_fees = order_count, fees_paid
            rebalance(ts, last_opens)
            if order_count > before_orders:
                rebalance_count += 1
                events.append(
                    {
                        "ts_ms": ts,
                        "signal_ts_ms": pending[0],
                        "max_deviation_pp": round(pending[1] * 100, 6),
                        "orders": order_count - before_orders,
                        "fees_usdt": round(fees_paid - before_fees, 8),
                    }
                )
            else:
                skipped += 1
            pending = None
        if cash < -max(1e-7, capital * 1e-10) or any(q < -1e-12 for q in quantities):
            raise ValueError("组合资金校验失败，回测已停止")
        cash = max(0.0, cash)
        marked = cash + sum(q * p for q, p in zip(quantities, closes, strict=False))
        equity = cash + sum(q * p * sell_factor for q, p in zip(quantities, closes, strict=False))
        hold_equity = sum(
            q * p * sell_factor for q, p in zip(initial_quantities, closes, strict=False)
        )
        if not all(math.isfinite(value) for value in (marked, equity, hold_equity)) or marked <= 0:
            raise ValueError("组合估值无效，回测已停止")
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if i % stride == 0 or ts == params["end"] - BAR_MS:
            equity_curve.append([ts, round(equity, 6)])
            hold_curve.append([ts, round(hold_equity, 6)])
        # Initial allocation is known before the first bar; checks use only closes.
        if (i + 1) % interval == 0:
            checks += 1
            deviation = max(
                abs(q * p / marked - weight)
                for q, p, weight in zip(quantities, closes, weights, strict=False)
            )
            if deviation + 1e-12 >= threshold:
                pending = (ts, deviation)
        last_prices, last_ts = closes, ts
    if not count or count != expected:
        raise ValueError("组合数据区间不完整")
    holdings = []
    for allocation, q, initial_q, price in zip(
        allocations, quantities, initial_quantities, last_prices, strict=False
    ):
        holdings.append(
            {
                **allocation,
                "initial_qty": initial_q,
                "final_qty": q,
                "qty_change_pct": (q / initial_q - 1) * 100,
                "final_price": price,
                "market_value_usdt": q * price,
                "actual_weight_pct": q * price / marked * 100,
            }
        )
    return {
        "engine_version": 3,
        "strategy": "smart_portfolio",
        "inst_id": "多币种组合",
        "data_sha256": digest.hexdigest() if digest is not None else None,
        "start_ms": params["start"],
        "end_ms": last_ts + BAR_MS,
        "bars": count,
        "total_candles": count * len(weights),
        "capital": capital,
        "rebalance_mode": params["rebalance_mode"],
        "threshold_pct": params["threshold_pct"],
        "interval_minutes": interval,
        "min_trade_usdt": minimum,
        "final_equity": round(equity, 6),
        "final_market_value": round(marked, 6),
        "estimated_exit_cost": round(marked - equity, 6),
        "return_pct": round((equity / capital - 1) * 100, 6),
        "buy_hold_pct": round((hold_equity / capital - 1) * 100, 6),
        "buy_hold_final_equity": round(hold_equity, 6),
        "excess_return_pp": round((equity - hold_equity) / capital * 100, 6),
        "max_drawdown_pct": round(max_drawdown * 100, 6),
        "fees_paid": round(fees_paid, 6),
        "turnover_usdt": round(turnover, 6),
        "cash_usdt": round(cash, 8),
        "rebalances": rebalance_count,
        "rebalance_checks": checks,
        "skipped_rebalances": skipped,
        "order_count": order_count,
        "orders_truncated": order_count > len(orders),
        "rebalance_events_truncated": rebalance_count > len(events),
        "last_signal_unfilled": pending is not None,
        "open_position": any(q > 0 for q in quantities),
        "holdings": holdings,
        "equity_curve": equity_curve,
        "buy_hold_curve": hold_curve,
        "recent_orders": list(orders),
        "recent_rebalances": list(events),
        "assumptions": {
            "deviation_unit": "percentage_points",
            "execution": "next_bar_open",
            "fractional_quantities": True,
            "minimum_notional": "user_assumption",
            "valuation": "estimated_liquidation_after_fee_and_slippage",
            "fees_currency": "USDT",
            "forced_exit": False,
        },
    }


def sma_signals(closes, fast, slow):
    fast_sum = slow_sum = 0.0
    fast_window, slow_window = deque(), deque()
    previous = None
    for i, close in enumerate(closes):
        fast_sum += close
        slow_sum += close
        fast_window.append(close)
        slow_window.append(close)
        if len(fast_window) > fast:
            fast_sum -= fast_window.popleft()
        if len(slow_window) > slow:
            slow_sum -= slow_window.popleft()
        signal = None
        if i >= slow - 1:
            state = fast_sum / fast > slow_sum / slow
            if previous is not None and state != previous:
                signal = "buy" if state else "sell"
            previous = state
        yield signal


def rsi_signals(closes, period, buy_below, sell_above):
    gain_sum = loss_sum = 0.0
    avg_gain = avg_loss = None
    previous_rsi = None
    previous_close = None
    for i, close in enumerate(closes):
        if i == 0:
            previous_close = close
            yield None
            continue
        delta = close - previous_close
        previous_close = close
        gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if i <= period:
            gain_sum += gain
            loss_sum += loss
            if i == period:
                avg_gain = gain_sum / period
                avg_loss = loss_sum / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        signal = None
        if avg_gain is not None:
            rsi = (
                (50.0 if avg_gain == 0 else 100.0)
                if avg_loss == 0
                else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
            )
            if previous_rsi is not None:
                if previous_rsi >= buy_below and rsi < buy_below:
                    signal = "buy"
                elif previous_rsi <= sell_above and rsi > sell_above:
                    signal = "sell"
            previous_rsi = rsi
        yield signal


def fingerprint(value):
    return value.hexdigest() if hasattr(value, "hexdigest") else value


def expected_count(params, bars):
    return len(bars) if hasattr(bars, "__len__") else (params["end"] - params["start"]) // BAR_MS


def calculate(params, bars, data_sha256=None):
    if params["strategy"] == "turtle":
        return calculate_turtle(params, bars, data_sha256)
    expected = expected_count(params, bars)
    # Both consumers advance once per bar, so tee retains at most one candle.
    bars, signal_bars = tee(bars)
    closes = (bar[4] for bar in signal_bars)
    if params["strategy"] == "sma_cross":
        if expected <= params["slow"]:
            raise ValueError("回测区间不足以完成均线预热")
        signals = sma_signals(closes, params["fast"], params["slow"])
    elif params["strategy"] == "rsi_reversion":
        if expected <= params["period"] + 1:
            raise ValueError("回测区间不足以完成 RSI 预热")
        signals = rsi_signals(closes, params["period"], params["buy_below"], params["sell_above"])
    else:
        raise ValueError("不支持的策略")
    fee = params["fee_bps"] / 10_000
    slip = params["slippage_bps"] / 10_000
    cash = float(params["capital"])
    qty = 0.0
    pending = None
    entry_cash = 0.0
    completed = wins = 0
    fee_total = 0.0
    exposure_bars = 0
    realized_sum = 0.0
    peak = cash
    max_drawdown = 0.0
    equity_curve = []
    trades = deque(maxlen=30)
    order_count = 0
    stride = max(1, math.ceil(expected / 500))
    first_open = first_ts = last_ts = last_close = None
    count = 0
    for i, ((ts, opened, _high, _low, closed), signal) in enumerate(
        zip(bars, signals, strict=False)
    ):
        if i == 0:
            first_open, first_ts = opened, ts
        count = i + 1
        last_ts, last_close = ts, closed
        if pending == "buy" and qty == 0 and cash > 0:
            px = opened * (1 + slip)
            entry_cash = cash
            qty = cash / (px * (1 + fee))
            fee_total += qty * px * fee
            cash = 0.0
            trades.append({"ts_ms": ts, "side": "buy", "price": px, "qty": qty})
            order_count += 1
        elif pending == "sell" and qty > 0:
            px = opened * (1 - slip)
            cash = qty * px * (1 - fee)
            fee_total += qty * px * fee
            pnl = cash - entry_cash
            realized_sum += pnl / entry_cash * 100
            completed += 1
            wins += int(pnl > 0)
            trades.append({"ts_ms": ts, "side": "sell", "price": px, "qty": qty, "pnl": pnl})
            order_count += 1
            qty = 0.0
        pending = signal
        exposure_bars += int(qty > 0)
        equity = cash + qty * closed * (1 - fee - slip)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0)
        if i % stride == 0 or i == expected - 1:
            equity_curve.append([ts, round(equity, 4)])
    if not count or count != expected:
        raise ValueError("回测数据区间不完整")
    final_equity = cash + qty * last_close * (1 - fee - slip)
    hold_qty = params["capital"] / (first_open * (1 + slip) * (1 + fee))
    hold_final = hold_qty * last_close * (1 - fee - slip)
    return {
        "engine_version": 2,
        "data_sha256": fingerprint(data_sha256),
        "inst_id": params["inst_id"],
        "strategy": params["strategy"],
        "start_ms": first_ts,
        "end_ms": last_ts + BAR_MS,
        "bars": count,
        "capital": params["capital"],
        "final_equity": round(final_equity, 4),
        "return_pct": round((final_equity / params["capital"] - 1) * 100, 4),
        "buy_hold_pct": round((hold_final / params["capital"] - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "exposure_pct": round(exposure_bars / count * 100, 2),
        "fees_paid": round(fee_total, 4),
        "avg_realized_trade_pct": round(realized_sum / completed, 4) if completed else None,
        "completed_trades": completed,
        "win_rate_pct": round(wins / completed * 100, 2) if completed else None,
        "open_position": qty > 0,
        "equity_curve": equity_curve,
        "recent_orders": list(trades),
        "order_count": order_count,
        "orders_truncated": order_count > len(trades),
    }


def calculate_buy_hold(params, bars, data_sha256=None):
    """All capital buys at the first open; value each close after exit costs."""
    capital = float(params["capital"])
    fee, slip = params["fee_bps"] / 10_000, params["slippage_bps"] / 10_000
    # Saved engine-v2 strategies used an additive exit-cost approximation.
    legacy = params.get("valuation_version", 3) <= 2
    sell_factor = 1 - fee - slip if legacy else (1 - fee) * (1 - slip)
    peak, drawdown = capital, 0.0
    expected = expected_count(params, bars)
    stride = max(1, math.ceil(expected / 500))
    curve = []
    first_ts = last_ts = last_close = None
    count = 0
    for i, (ts, opened, _h, _l, close) in enumerate(bars):
        if i == 0:
            first_ts = ts
            price = opened * (1 + slip)
            quantity = capital / (price * (1 + fee))
            entry_fee = quantity * price * fee
        count = i + 1
        last_ts, last_close = ts, close
        equity = quantity * close * sell_factor
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("持有对照估值无效")
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        if i % stride == 0 or i == expected - 1:
            curve.append([ts, round(equity, 6)])
    if not count or count != expected:
        raise ValueError("持有对照数据区间不完整")
    marked = quantity * last_close
    return {
        "benchmark_version": 1,
        "inst_id": params["inst_id"],
        "start_ms": first_ts,
        "end_ms": last_ts + BAR_MS,
        "bars": count,
        "capital": capital,
        "fee_bps": params["fee_bps"],
        "slippage_bps": params["slippage_bps"],
        "data_sha256": fingerprint(data_sha256),
        "quantity": quantity,
        "entry_price": price,
        "entry_fee_usdt": round(entry_fee, 6),
        "final_equity": round(equity, 6),
        "final_market_value": round(marked, 6),
        "estimated_exit_cost": round(marked - equity, 6),
        "return_pct": round((equity / capital - 1) * 100, 6),
        "max_drawdown_pct": round(drawdown * 100, 6),
        "equity_curve": curve,
        "assumptions": {
            "entry": "first_bar_open",
            "fractional_quantities": True,
            "forced_exit": False,
            "valuation": "estimated_liquidation",
            "exit_cost_method": "legacy_additive" if legacy else "compound",
        },
    }


def main():
    limits()
    params = json.loads(sys.stdin.read(4096))
    if params["strategy"] in STRATEGIES:
        bars, digest = stream_research_bars(params)
        try:
            result = calculate_research(params, bars, digest)
        finally:
            bars.close()
    elif params["strategy"] == "smart_portfolio":
        bars, digest = load_portfolio_bars(params)
        try:
            result = calculate_portfolio(params, bars, digest)
        finally:
            bars.close()
    else:
        bars, digest = stream_single_bars(params)
        try:
            result = (
                calculate_buy_hold(params, bars, digest)
                if params["strategy"] == "buy_hold_benchmark"
                else calculate(params, bars, digest)
            )
        finally:
            bars.close()
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
