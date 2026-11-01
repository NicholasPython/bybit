#!/usr/bin/env python3
"""Public-data spot research. Signals use completed closes; fills use later opens.

Cash and quantities are explicit. Targets are fractions of *post-cost* NAV, so a
fully invested purchase cannot borrow the fee. None means hold quantities; {} means
sell. The end index is a valuation boundary, never a signal day.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import math
import pathlib
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

DAY_MS = 86_400_000
DAYS_YEAR = 365.0
MIN_AGE = 365
MIN_TURNOVER = 1_000_000.0  # Prior 30 completed days, USDT per day.
CORRELATION = 0.8


def day_str(stamp: int) -> str:
    return dt.datetime.fromtimestamp(stamp / 1000, tz=dt.UTC).strftime("%Y-%m-%d")


def bar_str(stamp: int, periods_per_year: float) -> str:
    moment = dt.datetime.fromtimestamp(stamp / 1000, tz=dt.UTC)
    return moment.strftime("%Y-%m-%d" if periods_per_year == DAYS_YEAR else "%Y-%m-%dT%H:%M:%SZ")


def timestamp(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp() * 1000)


@dataclass
class Panel:
    dates: list[int]
    symbols: list[str]
    close: list[list[float]]
    open: list[list[float]]
    has: list[list[bool]]
    vol20: list[list[float]]
    eligible: list[list[int]]
    #: 每年多少根 K 线，决定年化口径（日线 365，小时线 8760）。
    periods_per_year: float = DAYS_YEAR
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def n_dates(self) -> int:
        return len(self.dates)

    def day_index(self, day: str) -> int:
        try:
            return self.dates.index(timestamp(day))
        except ValueError as exc:
            raise ValueError(f"Required boundary absent from dataset: {day}") from exc


def load_panel(
    data_dir: pathlib.Path,
    universe_cutoff: str = "2024-01-01",
    as_of: str = "2026-11-01",
    only: set[str] | None = None,
) -> Panel:
    """Use currently surviving pairs; this is explicitly NOT a survivorship-free pool.

    Metadata has no spot launchTime. The first available candle proxies history,
    not necessarily the exchange's actual listing date. Missing marks use the last
    known close; missing bars cannot produce signals or executable prices.
    """
    cutoff, end = timestamp(universe_cutoff), timestamp(as_of)
    series: dict[str, dict[int, tuple[float, float, float]]] = {}
    files = []
    rejected, gaps = [], []
    total_rows = 0
    for path in sorted((data_dir / "klines").glob("*.csv")):
        raw = path.read_bytes()
        files.append((path.name, hashlib.sha256(raw).hexdigest()))
        rows: dict[int, tuple[float, float, float]] = {}
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                stamp = int(row["timestamp"])
                if stamp > end:
                    continue
                o, h, low, c, v, turnover = (
                    float(row[key])
                    for key in ("open", "high", "low", "close", "volume", "turnover")
                )
                if (
                    stamp % DAY_MS
                    or not all(math.isfinite(x) for x in (o, h, low, c, v, turnover))
                    or min(o, h, low, c) <= 0
                    or low > min(o, c)
                    or h < max(o, c)
                    or v < 0
                    or turnover < 0
                    or stamp in rows
                ):
                    raise ValueError(f"Invalid or duplicate candle: {path.name}, {stamp}")
                # The final candle may be unclosed. Only its opening price is usable.
                rows[stamp] = (o, c if stamp < end else math.nan, turnover)
        total_rows += len(rows)
        if not rows:
            continue
        if min(rows) > cutoff or len(rows) < MIN_AGE or (only and path.stem not in only):
            rejected.append(path.stem)
            continue
        stamps = sorted(rows)
        for a, b in zip(stamps, stamps[1:], strict=False):
            if b - a != DAY_MS:
                gaps.append({"symbol": path.stem, "after": day_str(a), "before": day_str(b)})
        series[path.stem] = rows
    if "BTCUSDT" not in series:
        raise ValueError("BTCUSDT history required for market filter")
    first = min(min(rows) for rows in series.values())
    dates = list(range(first, end + 1, DAY_MS))
    n = len(dates)
    symbols = sorted(series)
    closes, opens, masks, vols = [], [], [], []
    eligible = [[] for _ in dates]
    for k, symbol in enumerate(symbols):
        rows = series[symbol]
        c_row, o_row, mask = [], [], []
        v_row = [0.0] * n
        volume_row = []
        age, last_close = 0, 0.0
        for i, stamp in enumerate(dates):
            hit = rows.get(stamp)
            mask.append(hit is not None)
            if hit is None:
                o_row.append(last_close)
                c_row.append(last_close)
                volume_row.append(0.0)
                age = 0
            else:
                o, c, turnover = hit
                o_row.append(o)
                c_row.append(c)
                volume_row.append(turnover)
                if math.isfinite(c):
                    last_close = c
                age += 1
            if age >= 21 and math.isfinite(c_row[-1]):
                chunk = c_row[i - 20 : i + 1]
                rets = [math.log(b / a) for a, b in zip(chunk, chunk[1:], strict=False)]
                v_row[i] = statistics.stdev(rets) * math.sqrt(DAYS_YEAR)
            if (
                age >= MIN_AGE
                and math.isfinite(c_row[-1])
                and statistics.fmean(volume_row[i - 29 : i + 1]) >= MIN_TURNOVER
                and v_row[i] > 0.01
            ):
                eligible[i].append(k)
        closes.append(c_row)
        opens.append(o_row)
        masks.append(mask)
        vols.append(v_row)
    digest = hashlib.sha256("\n".join(f"{f}:{h}" for f, h in files).encode()).hexdigest()
    audit = {
        "source": "Bybit V5 public spot daily kline",
        "files": len(files),
        "rows_downloaded": total_rows,
        "universe": len(symbols),
        "universe_cutoff": universe_cutoff,
        "as_of_open_utc": as_of,
        "first_day": day_str(first),
        "days": n,
        "min_contiguous_bars": MIN_AGE,
        "min_30d_mean_turnover_usdt": MIN_TURNOVER,
        "daily_eligible_min": min(map(len, eligible)),
        "daily_eligible_max": max(map(len, eligible)),
        "gaps_in_universe": gaps,
        "excluded_symbols": rejected,
        "sha256_dataset": digest,
        "file_sha256": dict(files),
        "survivorship_bias": True,
        "last_candle_close_ignored": True,
    }
    return Panel(dates, symbols, closes, opens, masks, vols, eligible, DAYS_YEAR, audit)


@dataclass(frozen=True)
class Cost:
    fee: float = 0.001
    slippage: float = 0.0005

    def __post_init__(self) -> None:
        if min(self.fee, self.slippage) < 0 or self.rate >= 0.05:
            raise ValueError("Invalid execution cost")

    @property
    def rate(self) -> float:
        return self.fee + self.slippage


@dataclass
class Backtest:
    name: str
    daily_returns: list[float]
    equity: list[float]  # NAV starts at 1 before the first cost and return.
    dates: list[str]  # End-of-interval valuation dates, one per return.
    turnover: list[float] = field(default_factory=list)  # Gross traded notional / pre-trade NAV.
    exposure: list[float] = field(default_factory=list)
    trades: int = 0
    periods_per_year: float = DAYS_YEAR
    meta: dict[str, Any] = field(default_factory=dict)


def rebalance(
    cash: float,
    holdings: dict[int, float],
    prices: list[float],
    target: dict[int, float],
    rate: float,
) -> tuple[float, dict[int, float], float, int]:
    if (
        any(not math.isfinite(w) or w < 0 for w in target.values())
        or sum(target.values()) > 1 + 1e-10
    ):
        raise ValueError("Only unleveraged long/cash targets are allowed")
    if any(prices[k] <= 0 for k in set(holdings) | set(target)):
        raise ValueError("Missing executable price")
    values = {k: q * prices[k] for k, q in holdings.items()}
    nav = cash + sum(values.values())
    keys = set(holdings) | set(target)
    after = nav
    for _ in range(16):
        turn = sum(abs(target.get(k, 0.0) * after - values.get(k, 0.0)) for k in keys)
        after = nav - rate * turn
    deltas = [abs(target.get(k, 0.0) * after - values.get(k, 0.0)) for k in keys]
    turn = sum(deltas)
    holdings = {k: w * after / prices[k] for k, w in target.items() if w > 0}
    cash = after * (1 - sum(target.values()))
    if cash < -1e-9 or after <= 0:
        raise ValueError("Cash constraint or solvency violated")
    return max(cash, 0.0), holdings, turn / nav, sum(x > nav * 1e-10 for x in deltas)


def run_backtest(
    panel: Panel,
    weights_fn,
    cost: Cost,
    start: int,
    end: int,
    name: str,
    initial_capital: float = 1.0,
    liquidate_at_end: bool = True,
) -> Backtest:
    """Trade at opens start..end-1; value at open[end].

    The default includes an executable final sale and exit costs. Training windows
    may end with marked holdings instead; an absent quote never creates cash.
    """
    if not 0 <= start < end < panel.n_dates or initial_capital <= 0:
        raise ValueError("Invalid backtest boundaries or capital")
    cash, holdings = initial_capital, {}
    lookup = {s: k for k, s in enumerate(panel.symbols)}
    curve, returns, turns, exposure, dates = [1.0], [], [], [], []
    trades, skipped, marked_missing = 0, 0, 0
    fees, slippage = 0.0, 0.0
    for i in range(start, end):
        prices = [row[i] for row in panel.open]
        nav = cash + sum(q * prices[k] for k, q in holdings.items())
        target = weights_fn(i, nav)
        turn = 0.0
        if target is not None:
            mapped = {lookup[s]: w for s, w in target.items()}
            affected = set(mapped) | set(holdings)
            if all(panel.has[k][i] for k in affected):
                cash, holdings, turn, count = rebalance(cash, holdings, prices, mapped, cost.rate)
                fees += nav * turn * cost.fee
                slippage += nav * turn * cost.slippage
                trades += count
            else:
                skipped += 1
            on_execution = getattr(weights_fn, "on_execution", None)
            if on_execution is not None:
                on_execution(all(panel.has[k][i] for k in affected))
        next_prices = [row[i + 1] for row in panel.open]
        marked_missing += sum(not panel.has[k][i + 1] for k in holdings)
        invested = sum(q * next_prices[k] for k, q in holdings.items())
        ending_nav = cash + invested
        exposure.append(invested / ending_nav)
        if i + 1 == end and holdings and liquidate_at_end:
            missing = [panel.symbols[k] for k in holdings if not panel.has[k][end]]
            if missing:
                raise ValueError(f"Cannot liquidate without boundary quotes: {missing}")
            cash, holdings, exit_turn, count = rebalance(
                cash,
                holdings,
                next_prices,
                {},
                cost.rate,
            )
            fees += ending_nav * exit_turn * cost.fee
            slippage += ending_nav * exit_turn * cost.slippage
            trades += count
            turn += exit_turn
            ending_nav = cash
        turns.append(turn)
        curve.append(ending_nav / initial_capital)
        returns.append(curve[-1] / curve[-2] - 1.0)
        dates.append(bar_str(panel.dates[i + 1], panel.periods_per_year))
    return Backtest(
        name,
        returns,
        curve,
        dates,
        turns,
        exposure,
        trades,
        panel.periods_per_year,
        {
            "skipped_rebalances": skipped,
            "marked_missing_quotes": marked_missing,
            "terminal_liquidation": liquidate_at_end,
            "fees_initial_capital": fees / initial_capital,
            "slippage_initial_capital": slippage / initial_capital,
            "start_open": bar_str(panel.dates[start], panel.periods_per_year),
            "end_open": bar_str(panel.dates[end], panel.periods_per_year),
        },
    )


def metrics(result: Backtest) -> dict[str, float]:
    returns = result.daily_returns
    if not returns:
        raise ValueError("Empty return series")
    per_year = result.periods_per_year
    years = len(returns) / per_year
    mean = statistics.fmean(returns)
    vol = statistics.stdev(returns) * math.sqrt(per_year) if len(returns) > 1 else 0.0
    downside = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns) * per_year)
    total = result.equity[-1] - 1
    cagr = result.equity[-1] ** (1 / years) - 1
    peak, drawdown = 1.0, 0.0
    for nav in result.equity:
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    return {
        "total_return": total,
        "cagr": cagr,
        "vol": vol,
        "sharpe": mean * per_year / vol if vol else 0.0,
        "sortino": mean * per_year / downside if downside else 0.0,
        "max_drawdown": drawdown,
        "calmar": cagr / abs(drawdown) if drawdown else 0.0,
        "turnover_per_year": sum(result.turnover) / years,
        "exposure": statistics.fmean(result.exposure) if result.exposure else 0.0,
        "trades": result.trades,
        "periods": len(returns),
        "years": years,
        "positive_day_fraction": sum(r > 0 for r in returns) / len(returns),
    }


def periodic_returns(result: Backtest, length: int = 4) -> dict[str, float]:
    out: dict[str, float] = {}
    step = dt.timedelta(seconds=365 * 24 * 3600 / result.periods_per_year)
    for day, ret in zip(result.dates, result.daily_returns, strict=True):
        # open-to-open returns belong to the interval's starting calendar date.
        begin = dt.datetime.fromisoformat(day.replace("Z", "+00:00")) - step
        key = begin.isoformat()[:length]
        out[key] = (1 + out.get(key, 0.0)) * (1 + ret) - 1
    return out


def join_results(results: list[Backtest], name: str) -> Backtest:
    if not results:
        raise ValueError("No walk-forward intervals")
    per_year = results[0].periods_per_year
    if any(result.periods_per_year != per_year for result in results):
        raise ValueError("Mixed bar frequencies")
    curve, rets, dates, turns, exposures = [1.0], [], [], [], []
    for result in results:
        for ret in result.daily_returns:
            rets.append(ret)
            curve.append(curve[-1] * (1 + ret))
        dates.extend(result.dates)
        turns.extend(result.turnover)
        exposures.extend(result.exposure)
    if len(dates) != len(set(dates)) or dates != sorted(dates):
        raise ValueError("Overlapping or unordered walk-forward intervals")
    return Backtest(
        name,
        rets,
        curve,
        dates,
        turns,
        exposures,
        sum(x.trades for x in results),
        per_year,
        {
            "start_open": results[0].meta.get("start_open"),
            "end_open": results[-1].meta.get("end_open"),
            "skipped_rebalances": sum(x.meta.get("skipped_rebalances", 0) for x in results),
        },
    )


def bootstrap_mean_ci(
    returns: list[float],
    seed: int = 20261101,
    draws: int = 1000,
    block: int = 20,
    periods_per_year: float = DAYS_YEAR,
) -> dict[str, Any]:
    """Circular block bootstrap; annualised arithmetic mean, not CAGR or profit chance."""
    rng = random.Random(seed)  # noqa: S311 - deterministic research resampling
    means = []
    n = len(returns)
    for _ in range(draws):
        sample = []
        while len(sample) < n:
            offset = rng.randrange(n)
            sample.extend(returns[(offset + j) % n] for j in range(min(block, n - len(sample))))
        means.append(statistics.fmean(sample) * periods_per_year)
    means.sort()
    return {
        "annual_arithmetic_mean_ci_95": [means[int(draws * 0.025)], means[int(draws * 0.975)]],
        "block_periods": block,
        "draws": draws,
        "seed": seed,
        "periods_per_year": periods_per_year,
    }


def deflated_sharpe(
    returns: list[float],
    trial_sharpes: list[float],
    total_trials: int,
    periods_per_year: float = DAYS_YEAR,
) -> dict[str, Any]:
    """DSR diagnostic using expected maximum of trials (Bailey/Lopez de Prado).

    Trials are treated as independent, which is conservative for correlated grids.
    It is NOT a probability of future profit. Non-normality is estimated from daily
    returns. Annual Sharpe values are converted to daily units before applying PSR.
    """
    n = len(returns)
    sd = statistics.stdev(returns) if n > 1 else 0
    if sd <= 0 or total_trials < 2:
        return {"probability": None, "reason": "Insufficient variation or trials"}
    sr = statistics.fmean(returns) / sd
    euler = 0.5772156649015329
    normal = statistics.NormalDist()
    spread = statistics.stdev(trial_sharpes) / math.sqrt(periods_per_year)
    threshold = spread * (
        (1 - euler) * normal.inv_cdf(1 - 1 / total_trials)
        + euler * normal.inv_cdf(1 - 1 / (total_trials * math.e))
    )
    center = statistics.fmean(returns)
    m2 = statistics.fmean((r - center) ** 2 for r in returns)
    skew = statistics.fmean((r - center) ** 3 for r in returns) / m2**1.5
    kurt = statistics.fmean((r - center) ** 4 for r in returns) / m2**2
    denominator = math.sqrt(max(1 - skew * sr + (kurt - 1) * sr * sr / 4, 1e-12))
    z = (sr - threshold) * math.sqrt(n - 1) / denominator
    return {
        "probability": normal.cdf(z),
        "expected_max_sharpe_annual": threshold * math.sqrt(periods_per_year),
        "total_trials": total_trials,
        "skew": skew,
        "kurtosis": kurt,
        "caveat": "IS diagnostic; correlated trials assumed independent; not a profit forecast",
    }


class StrategySet:
    def __init__(
        self,
        panel: Panel,
        allowed: set[str] | None = None,
        delay: int = 0,
    ) -> None:
        self.panel = panel
        self.allowed = allowed
        self.delay = delay
        self.periods_per_year = panel.periods_per_year
        self.btc = panel.symbols.index("BTCUSDT")
        self.cache: dict[int, list[list[float]]] = {}

    def momentum(self, k: int, j: int, lookback: int) -> float:
        if lookback not in self.cache:
            out = []
            for closes in self.panel.close:
                row = [0.0] * len(closes)
                for t in range(lookback, len(closes)):
                    if closes[t - lookback] > 0 and math.isfinite(closes[t]):
                        row[t] = closes[t] / closes[t - lookback] - 1
                out.append(row)
            self.cache[lookback] = out
        return self.cache[lookback][k][j]

    def eligible(self, j: int) -> list[int]:
        return [
            k
            for k in self.panel.eligible[j]
            if self.allowed is None or self.panel.symbols[k] in self.allowed
        ]

    def scale(self, picks: list[tuple[int, float]], target_vol: float) -> dict[str, float]:
        if not picks:
            return {}
        raw = {k: 1 / sigma for k, sigma in picks}
        total = sum(raw.values())
        weights = {k: w / total for k, w in raw.items()}
        terms = [weights[k] * sigma for k, sigma in picks]
        vol = math.sqrt(
            (1 - CORRELATION) * sum(t * t for t in terms) + CORRELATION * sum(terms) ** 2
        )
        factor = min(1.0, target_vol / vol) if vol else 0.0
        # Excess above the cap stays in cash, so the cap cannot amplify risk.
        return {self.panel.symbols[k]: min(0.30, w * factor) for k, w in weights.items()}

    def make(self, family: str, params: dict[str, Any], force_initial: bool = False):
        positions: dict[str, int] = {}
        entered, initialized = False, False
        previous_positions, previous_entered = {}, False
        pnl = self.panel

        def weights(i: int, nav: float) -> dict[str, float] | None:
            nonlocal entered, previous_positions, previous_entered
            previous_positions, previous_entered = positions.copy(), entered
            del nav
            j = i - 1 - self.delay
            if j < 0:
                return None
            if family == "buy_hold":
                s = params["symbol"]
                k = pnl.symbols.index(s)
                if entered or not pnl.has[k][i] or not pnl.has[k][j]:
                    return None
                entered = True
                return {s: 1.0}
            if family == "equal_weight":
                if j % params.get("rebalance", 30):
                    return None
                picks = self.eligible(j)
                return {pnl.symbols[k]: 1 / len(picks) for k in picks} if picks else {}
            lookback = params["lookback"]
            if j < max(lookback, 20):
                return None
            if family == "reversion":
                changed = force_initial and not initialized
                for s in list(positions):
                    if i >= positions[s]:
                        del positions[s]
                        changed = True
                candidates = []
                for k in self.eligible(j):
                    s = pnl.symbols[k]
                    if s in positions:
                        continue
                    sigma = pnl.vol20[k][j]
                    z = self.momentum(k, j, lookback) / (
                        sigma * math.sqrt(lookback / self.periods_per_year)
                    )
                    if z <= -params["z_threshold"]:
                        candidates.append((z, s))
                for _, s in sorted(candidates)[: max(0, 10 - len(positions))]:
                    positions[s] = i + params["hold_days"]
                    changed = True
                return dict.fromkeys(positions, params.get("per_trade", 0.05)) if changed else None
            if j % params["rebalance"] and not (force_initial and not initialized):
                return None
            if family == "tsmom" and params["market_filter"]:
                closes = pnl.close[self.btc]
                if j < 199 or not all(pnl.has[self.btc][j - 199 : j + 1]):
                    return {}
                if closes[j] <= statistics.fmean(closes[j - 199 : j + 1]):
                    return {}
            scored = []
            for k in self.eligible(j):
                sigma = pnl.vol20[k][j]
                mom = self.momentum(k, j, lookback)
                if mom > 0:
                    scored.append((mom, k, sigma))
            if family == "cross":
                scored = sorted(scored, reverse=True)[: params["top_k"]]
            elif family != "tsmom":
                raise ValueError(f"Unknown family: {family}")
            return self.scale([(k, sigma) for _, k, sigma in scored], params["target_vol"])

        def on_execution(accepted: bool) -> None:
            nonlocal positions, entered, initialized
            if accepted:
                initialized = True
            else:
                positions, entered = previous_positions, previous_entered

        weights.on_execution = on_execution
        return weights
