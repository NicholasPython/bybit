"""Streaming OHLCV strategy simulations; cash-only orders and causal signals."""

import math
import statistics
from collections import deque

from strategy_catalog import MULTI_STRATEGIES, STRATEGIES, strategy_params, warmup

MINUTE = 60000


class EMA:
    def __init__(self, period, wilder=False):
        self.period, self.alpha = period, (1 / period if wilder else 2 / (period + 1))
        self.value, self.count, self.total = None, 0, 0.0

    def update(self, x):
        if self.value is None:
            self.count += 1
            self.total += x
            if self.count == self.period:
                self.value = self.total / self.period
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


def bands(values, period, multiple):
    if len(values) < period:
        return None
    v = list(values)[-period:]
    middle = sum(v) / period
    deviation = math.sqrt(sum((x - middle) ** 2 for x in v) / period)
    return (
        middle,
        middle - multiple * deviation,
        middle + multiple * deviation,
        2 * multiple * deviation / middle,
    )


class Signals:
    def __init__(self, p):
        self.p, self.count = p, 0
        self.closes = deque(maxlen=1001)
        self.volumes = deque(maxlen=1001)
        self.widths = deque(maxlen=p.get("squeeze_lookback", 1))
        self.atr = EMA(p.get("atr_period", 14), True)
        n = p.get("adx_period", 14)
        self.dmi_tr, self.plus, self.minus, self.adx = [EMA(n, True) for _ in range(4)]
        self.fast, self.slow = EMA(p.get("macd_fast", 12)), EMA(p.get("macd_slow", 26))
        self.macd_signal = EMA(p.get("macd_signal", 9))
        self.prev = self.prev_band = self.prev_hist = self.prev_short = self.prev_long = None
        self.upper = self.lower = None
        self.up = False

    def update(self, row):
        _ts, o, h, low_price, c, volume = row
        p, strategy = self.p, self.p["strategy"]
        self.count += 1
        prev_close = self.prev[4] if self.prev else None
        tr = (
            h - low_price
            if prev_close is None
            else max(h - low_price, abs(h - prev_close), abs(low_price - prev_close))
        )
        atr = self.atr.update(tr)
        old_volumes = list(self.volumes)
        self.closes.append(c)
        signal, trailing = None, None
        if strategy in ("bollinger_reversion", "squeeze_breakout"):
            b = bands(self.closes, p["band_period"], p["band_std"])
            if b and self.prev_band and prev_close is not None:
                mid, lower, upper, width = b
                if strategy == "bollinger_reversion":
                    if prev_close < self.prev_band[1] and c >= lower:
                        signal = "buy"
                    elif c >= mid:
                        signal = "sell"
                else:
                    if (
                        len(self.widths) == p["squeeze_lookback"]
                        and len(old_volumes) >= p["volume_period"]
                    ):
                        widths = sorted(self.widths)
                        index = (len(widths) - 1) * p["squeeze_percentile"] / 100
                        floor = int(index)
                        cutoff = widths[floor] + (
                            widths[min(floor + 1, len(widths) - 1)] - widths[floor]
                        ) * (index - floor)
                        volume_mean = sum(old_volumes[-p["volume_period"] :]) / p["volume_period"]
                        if (
                            self.prev_band[3] <= cutoff
                            and prev_close <= self.prev_band[2]
                            and c > self.prev_band[2]
                            and volume_mean > 0
                            and volume >= volume_mean * p["volume_multiple"]
                        ):
                            signal = "buy"
                    if c < mid:
                        signal = "sell"
            if b:
                self.widths.append(b[3])
                self.prev_band = b
        elif strategy == "trend_pullback":
            if len(self.closes) >= p["trend_slow"]:
                v = list(self.closes)
                short = sum(v[-p["pullback_period"] :]) / p["pullback_period"]
                medium = sum(v[-p["trend_fast"] :]) / p["trend_fast"]
                long = sum(v[-p["trend_slow"] :]) / p["trend_slow"]
                if self.prev_short is not None and self.prev_long is not None:
                    if (
                        medium > long
                        and long > self.prev_long
                        and prev_close <= self.prev_short
                        and c > short
                    ):
                        signal = "buy"
                    elif c < medium:
                        signal = "sell"
                self.prev_short, self.prev_long = short, long
        elif strategy == "macd_adx":
            plus_dm = minus_dm = 0.0
            if self.prev:
                up, down = h - self.prev[2], self.prev[3] - low_price
                plus_dm = up if up > down and up > 0 else 0.0
                minus_dm = down if down > up and down > 0 else 0.0
            true = self.dmi_tr.update(tr)
            positive, negative = self.plus.update(plus_dm), self.minus.update(minus_dm)
            strength = None
            if true is not None:
                di_sum = positive + negative
                dx = 100 * abs(positive - negative) / di_sum if di_sum else 0.0
                strength = self.adx.update(dx)
            fast, slow = self.fast.update(c), self.slow.update(c)
            if fast is not None and slow is not None:
                macd = fast - slow
                line = self.macd_signal.update(macd)
                if line is not None:
                    hist = macd - line
                    if self.prev_hist is not None and strength is not None:
                        if (
                            self.prev_hist <= 0 < hist
                            and strength >= p["adx_threshold"]
                            and positive > negative
                        ):
                            signal = "buy"
                        elif self.prev_hist >= 0 > hist:
                            signal = "sell"
                    self.prev_hist = hist
        elif strategy == "supertrend" and atr is not None:
            basic_upper, basic_lower = (
                (h + low_price) / 2 + p["multiplier"] * atr,
                (h + low_price) / 2 - p["multiplier"] * atr,
            )
            old_up = self.up
            if self.upper is None:
                self.upper, self.lower = basic_upper, basic_lower
            else:
                self.upper = (
                    basic_upper
                    if basic_upper < self.upper or prev_close > self.upper
                    else self.upper
                )
                self.lower = (
                    basic_lower
                    if basic_lower > self.lower or prev_close < self.lower
                    else self.lower
                )
                self.up = c >= self.lower if old_up else c > self.upper
                if self.up != old_up:
                    signal = "buy" if self.up else "sell"
            if self.up:
                trailing = self.lower
        self.prev = row
        self.volumes.append(volume)
        if self.count < warmup(p) or atr is None or atr <= 0:
            signal = None
        return signal, atr, trailing


class Book:
    def __init__(self, p, assets):
        self.p, self.assets = p, assets
        self.cash = self.capital = float(p["capital"])
        self.fee, self.slip = p["fee_bps"] / 10000, p["slippage_bps"] / 10000
        self.qty, self.cost = [0.0] * len(assets), [0.0] * len(assets)
        self.orders = deque(maxlen=50)
        self.fees = self.turnover = self.slippage = self.realized = self.realized_pct = 0.0
        self.order_count = self.sells = self.wins = self.skipped = 0

    def buy(self, ts, index, reference, budget, reason):
        px = reference * (1 + self.slip)
        spend = min(max(0.0, budget), self.cash)
        notional = spend / (1 + self.fee)
        if spend <= 0 or notional + 1e-9 < self.p["min_trade_usdt"]:
            self.skipped += 1
            return None
        q = notional / px
        commission = notional * self.fee
        self.cash = max(0.0, self.cash - spend)
        self.qty[index] += q
        self.cost[index] += spend
        self.record(ts, index, "buy", px, reference, q, commission, reason)
        return q, spend, px

    def sell(self, ts, index, reference, q, reason, basis_override=None):
        q = min(q, self.qty[index])
        px = reference * (1 - self.slip)
        notional = q * px
        if q <= 0 or (reason != "stop" and notional + 1e-9 < self.p["min_trade_usdt"]):
            self.skipped += 1
            return False
        basis = self.cost[index] * q / self.qty[index] if basis_override is None else basis_override
        commission = notional * self.fee
        pnl = notional - commission - basis
        self.cash += notional - commission
        if q >= self.qty[index]:
            self.qty[index] = self.cost[index] = 0.0
        else:
            self.qty[index] -= q
            self.cost[index] = max(0.0, self.cost[index] - basis)
        self.sells += 1
        self.wins += int(pnl > 0)
        self.realized += pnl
        self.realized_pct += pnl / basis * 100 if basis else 0
        self.record(ts, index, "sell", px, reference, q, commission, reason, pnl)
        return True

    def record(self, ts, index, side, price, reference, q, fee, reason, pnl=None):
        self.fees += fee
        self.turnover += q * price
        self.slippage += q * abs(price - reference)
        self.order_count += 1
        order = {
            "ts_ms": ts,
            "inst_id": self.assets[index],
            "side": side,
            "price": price,
            "qty": q,
            "fee_usdt": fee,
            "reason": reason,
        }
        if pnl is not None:
            order["pnl"] = pnl
        self.orders.append(order)

    def marked(self, prices):
        return self.cash + sum(q * x for q, x in zip(self.qty, prices, strict=False))

    def equity(self, prices):
        return self.cash + sum(
            q * x * (1 - self.slip) * (1 - self.fee) for q, x in zip(self.qty, prices, strict=False)
        )


class Aggregate:
    def __init__(self, minutes, size):
        self.minutes, self.size = minutes, size
        self.bucket, self.count, self.rows = None, 0, None

    def update(self, ts, rows):
        bucket = ts // (self.minutes * MINUTE)
        if bucket != self.bucket:
            self.bucket, self.count = bucket, 0
            self.rows = [[o, h, low, c, v] for o, h, low, c, v in rows]
        else:
            for old, row in zip(self.rows, rows, strict=False):
                old[1], old[2], old[3], old[4] = (
                    max(old[1], row[1]),
                    min(old[2], row[2]),
                    row[3],
                    old[4] + row[4],
                )
        self.count += 1
        if (ts + MINUTE) % (self.minutes * MINUTE) == 0 and self.count == self.minutes:
            return [(ts + MINUTE - self.minutes * MINUTE, *row) for row in self.rows]
        return None


def pearson(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va, vb = sum((x - ma) ** 2 for x in a), sum((x - mb) ** 2 for x in b)
    if va <= 1e-30 or vb <= 1e-30:
        return None
    return max(
        -1.0,
        min(1.0, sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False)) / math.sqrt(va * vb)),
    )


def allocation(p, histories):
    """Only completed historical closes enter selection; no full-range matrix."""
    n = len(histories)
    weights, diagnostics = [0.0] * n, []
    if p["strategy"] == "momentum_rotation":
        ranking = []
        for i, history in enumerate(histories):
            values = list(history)
            if len(values) <= p["long_lookback"]:
                continue
            short = values[-1] / values[-1 - p["short_lookback"]] - 1
            long = values[-1] / values[-1 - p["long_lookback"]] - 1
            if long > 0:
                ranking.append(((short + long) / 2, i))
        chosen = [i for _, i in sorted(ranking, key=lambda x: (-x[0], x[1]))[: p["hold_count"]]]
        for i in chosen:
            weights[i] = 1 / len(chosen)
        diagnostics = [
            {"inst_id": p["universe"][i], "score": score}
            for score, i in sorted(ranking, reverse=True)
        ]
    else:
        returns, vols = {}, {}
        for i, history in enumerate(histories):
            values = list(history)
            if len(values) <= p["corr_lookback"]:
                continue
            values = values[-p["corr_lookback"] - 1 :]
            r = [math.log(b / a) for a, b in zip(values, values[1:], strict=False)]
            vol = statistics.pstdev(r)
            if vol > 1e-12:
                returns[i], vols[i] = r, vol
        chosen = []
        for i in sorted(vols, key=lambda x: (vols[x], x)):
            coefficients = [pearson(returns[i], returns[j]) for j in chosen]
            if all(x is not None and abs(x) <= p["max_correlation"] for x in coefficients):
                chosen.append(i)
            if len(chosen) == p["hold_count"]:
                break
        total = sum(1 / vols[i] for i in chosen)
        for i in chosen:
            weights[i] = min(p["max_weight_pct"] / 100, (1 / vols[i]) / total)
        diagnostics = [{"inst_id": p["universe"][i], "volatility": vols[i]} for i in chosen]
    return weights, diagnostics


def rebalance(book, ts, prices, weights):
    marked = book.marked(prices)
    actual = [q * price / marked for q, price in zip(book.qty, prices, strict=False)]
    if (
        max(abs(a - b) for a, b in zip(actual, weights, strict=False))
        < book.p["rebalance_threshold_pct"] / 100
    ):
        return False
    buy_factor, sell_factor = (1 + book.slip) * (1 + book.fee), (1 - book.slip) * (1 - book.fee)
    low, high = 0.0, marked
    for _ in range(40):
        target = (low + high) / 2
        deltas = [target * w - q * px for w, q, px in zip(weights, book.qty, prices, strict=False)]
        cost = sum(d * (buy_factor - 1) if d > 0 else -d * (1 - sell_factor) for d in deltas)
        if target + cost > marked:
            high = target
        else:
            low = target
    deltas = [low * w / px - q for w, px, q in zip(weights, prices, book.qty, strict=False)]
    before = book.order_count
    for i, delta in enumerate(deltas):
        if delta < 0:
            book.sell(ts, i, prices[i], -delta, "rebalance")
    required = sum(max(0, d) * px * buy_factor for d, px in zip(deltas, prices, strict=False))
    scale = min(1.0, book.cash / required) if required else 0
    for i, delta in enumerate(deltas):
        if delta > 0:
            book.buy(ts, i, prices[i], delta * prices[i] * buy_factor * scale, "rebalance")
    return book.order_count > before


def calculate_research(params, bars, digest=None):
    p = {**params, **strategy_params(params)}
    strategy = p["strategy"]
    multi = strategy in MULTI_STRATEGIES
    assets = p["universe"] if multi else [p["inst_id"]]
    expected = len(bars) if hasattr(bars, "__len__") else (p["end"] - p["start"]) // MINUTE
    book = Book(p, assets)
    aggregator = Aggregate(p["signal_minutes"], len(assets))
    indicators = None if multi or strategy in ("spot_grid", "budget_dca") else Signals(p)
    history_length = p.get("long_lookback", p.get("corr_lookback", 1)) + 1
    histories = [deque(maxlen=history_length) for _ in assets]
    pending = None
    stop = None
    signal_count = exposure = stops = rebalances = dca_buys = grid_rounds = 0
    last_check = None
    weights = [0.0] * len(assets)
    events = deque(maxlen=30)
    grid = lots = None
    peak = book.capital
    drawdown = 0.0
    curve, hold_curve = [], []
    stride = max(1, math.ceil(expected / 500))
    hold_qty = None
    first_ts = last_ts = last_prices = None
    previous_grid_close = None
    for i, (ts, rows) in enumerate(bars):
        if i == 0:
            first_ts = ts
            first_prices = [r[0] for r in rows]
            hold_qty = [
                book.capital / len(assets) / (x * (1 + book.slip) * (1 + book.fee))
                for x in first_prices
            ]
            if strategy == "spot_grid":
                lower, upper = (
                    first_prices[0] * p["lower_pct"] / 100,
                    first_prices[0] * p["upper_pct"] / 100,
                )
                grid = [
                    lower + (upper - lower) * j / p["grid_count"]
                    if p["grid_mode"] == "arithmetic"
                    else lower * (upper / lower) ** (j / p["grid_count"])
                    for j in range(p["grid_count"] + 1)
                ]
                lots = [None] * p["grid_count"]
        elif ts != last_ts + MINUTE:
            raise ValueError("策略分钟数据不连续")
        last_ts = ts
        prices = [r[0] for r in rows]
        closes = [r[3] for r in rows]
        if pending:
            if multi:
                weights, diagnostic = pending
                if rebalance(book, ts, prices, weights):
                    rebalances += 1
                events.append(
                    {
                        "ts_ms": ts,
                        "weights": dict(zip(assets, weights, strict=False)),
                        "diagnostics": diagnostic,
                    }
                )
            else:
                action, atr, trailing = pending
                if action == "sell" and book.qty[0] > 0:
                    book.sell(ts, 0, prices[0], book.qty[0], "signal_exit")
                    if book.qty[0] == 0:
                        stop = None
                elif action == "buy" and book.qty[0] == 0:
                    bought = book.buy(
                        ts, 0, prices[0], book.cash * p["position_pct"] / 100, "signal_entry"
                    )
                    if bought:
                        stop = bought[2] - atr * p["stop_atr"]
                        if trailing is not None:
                            stop = max(stop, trailing)
            pending = None
        if indicators and book.qty[0] > 0 and stop is not None and rows[0][2] <= stop:
            book.sell(ts, 0, min(prices[0], stop), book.qty[0], "stop")
            stop = None
            stops += 1
        if strategy == "budget_dca" and i % p["dca_minutes"] == 0 and dca_buys < p["tranches"]:
            if book.buy(ts, 0, prices[0], book.capital / p["tranches"], "budget_buy"):
                dca_buys += 1
        elif strategy == "spot_grid":
            # Orders are armed from the previous minute close. No sell of a new lot this minute.
            previous = previous_grid_close if previous_grid_close is not None else prices[0]
            old_lots = list(lots)
            for j, lot in enumerate(old_lots):
                if (
                    lot
                    and lot[2] < ts
                    and previous < grid[j + 1]
                    and rows[0][1] >= grid[j + 1]
                    and book.sell(ts, 0, grid[j + 1], lot[0], "grid_sell", lot[1])
                ):
                    lots[j] = None
                    grid_rounds += 1
            for j, old_lot in enumerate(old_lots):
                # Newly vacated slots are rearmed next minute, avoiding fictional same-bar cycles.
                if (
                    old_lot is None
                    and lots[j] is None
                    and previous > grid[j]
                    and rows[0][2] <= grid[j]
                ):
                    bought = book.buy(ts, 0, grid[j], book.capital / p["grid_count"], "grid_buy")
                    if bought:
                        lots[j] = (bought[0], bought[1], ts)
            previous_grid_close = closes[0]
        if book.cash < -1e-7 or any(q < 0 for q in book.qty):
            raise ValueError("资金或持仓约束失效")
        equity = book.equity(closes)
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("策略净值无效")
        held = any(q > 0 for q in book.qty)
        exposure += int(held)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        hold = sum(
            q * x * (1 - book.slip) * (1 - book.fee) for q, x in zip(hold_qty, closes, strict=False)
        )
        if i % stride == 0 or i == expected - 1:
            curve.append([ts, round(equity, 6)])
            hold_curve.append([ts, round(hold, 6)])
        complete = aggregator.update(ts, rows) if multi or indicators else None
        if complete:
            signal_count += 1
            if multi:
                for history, row in zip(histories, complete, strict=False):
                    history.append(row[4])
                if signal_count >= warmup(p) and (
                    last_check is None or signal_count - last_check >= p["rebalance_bars"]
                ):
                    pending = allocation(p, histories)
                    last_check = signal_count
            elif indicators:
                action, atr, trailing = indicators.update(complete[0])
                if trailing is not None and book.qty[0] > 0:
                    stop = max(stop, trailing) if stop is not None else trailing
                if action and (
                    (action == "buy" and book.qty[0] == 0) or (action == "sell" and book.qty[0] > 0)
                ):
                    pending = action, atr, trailing
        last_prices = closes
    if first_ts is None or i + 1 != expected:
        raise ValueError("回测数据区间不完整")
    if warmup(p) and signal_count < warmup(p):
        raise ValueError("完整信号 K 线不足，请扩大区间")
    marked, equity = book.marked(last_prices), book.equity(last_prices)
    holdings = [
        {
            "inst_id": a,
            "final_qty": q,
            "market_value_usdt": q * price,
            "actual_weight_pct": q * price / marked * 100,
            "target_weight_pct": w * 100,
        }
        for a, q, price, w in zip(assets, book.qty, last_prices, weights, strict=False)
    ]
    fingerprint = digest.hexdigest() if hasattr(digest, "hexdigest") else digest
    return {
        "engine_version": 5,
        "strategy": strategy,
        "inst_id": "多币种组合" if multi else assets[0],
        "is_multi": multi,
        "universe": assets,
        "strategy_params": {
            "signal_minutes": p["signal_minutes"],
            **{k: p[k] for k in STRATEGIES[strategy]["fields"]},
        },
        "data_sha256": fingerprint,
        "start_ms": first_ts,
        "end_ms": last_ts + MINUTE,
        "bars": expected,
        "signal_bars": signal_count,
        "warmup_bars": warmup(p),
        "capital": book.capital,
        "final_equity": round(equity, 6),
        "return_pct": round((equity / book.capital - 1) * 100, 6),
        "buy_hold_pct": round((hold / book.capital - 1) * 100, 6),
        "excess_return_pp": round((equity - hold) / book.capital * 100, 6),
        "max_drawdown_pct": round(drawdown * 100, 6),
        "exposure_pct": round(exposure / expected * 100, 2),
        "cash_usdt": round(book.cash, 6),
        "estimated_exit_cost": round(marked - equity, 6),
        "fees_paid": round(book.fees, 6),
        "turnover_usdt": round(book.turnover, 6),
        "slippage_cost_usdt": round(book.slippage, 6),
        "realized_pnl_usdt": round(book.realized, 6),
        "completed_trades": book.sells,
        "win_rate_pct": round(book.wins / book.sells * 100, 2) if book.sells else None,
        "avg_realized_trade_pct": round(book.realized_pct / book.sells, 6) if book.sells else None,
        "order_count": book.order_count,
        "orders_truncated": book.order_count > len(book.orders),
        "recent_orders": list(book.orders),
        "open_position": any(q > 0 for q in book.qty),
        "holdings": holdings,
        "stop_price": stop,
        "stop_exits": stops,
        "rebalances": rebalances,
        "skipped_orders": book.skipped,
        "dca_buys": dca_buys,
        "grid_rounds": grid_rounds,
        "grid_levels": grid,
        "recent_allocations": list(events),
        "last_signal_unfilled": pending is not None,
        "equity_curve": curve,
        "buy_hold_curve": hold_curve,
        "assumptions": {
            "signal_timezone": "UTC",
            "warmup": "inside_requested_window",
            "execution": "closed_signal_then_next_minute_open",
            "volume": "base_coin_volume",
            "long_only": True,
            "leverage": False,
            "forced_exit": False,
            "fractional_quantities": True,
            "valuation": "compound_estimated_liquidation",
            "universe": "fixed_selected_top20_survivorship_bias" if multi else "single_coin",
            "grid_execution": "fixed_budget_slots_previous_close_arming"
            "_sell_existing_first_no_same_minute_roundtrip",
            "dca_budget": "entire_capital_deposited_at_start_no_external_cashflows",
            "completed_trade_unit": "sell_order_with_proportional_cost_basis",
            "low_corr_weights": "inverse_volatility_capped_without_redistribution",
        },
    }
