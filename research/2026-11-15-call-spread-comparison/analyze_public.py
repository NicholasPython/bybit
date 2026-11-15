#!/usr/bin/env python3
"""Deterministic exploratory call-spread screen; stdlib only; run with -I.

Inputs are saved public responses. No network, credentials, account or order API.
Budget = synchronized terminal payoff loss including entry fees and adverse ticks,
excluding future exit/delivery fees and execution/liquidation risk. It is not IM.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

D = Decimal
HOUR = 3_600_000
DAY = 24 * HOUR
BUDGET = D("25")
RATE = D(".0003")
CAP = D(".07")


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def number(row, key):
    return D(str(row.get(key) or "0"))


def fee(price, index, qty=D(1)):
    return min(RATE * index, CAP * price) * qty


def delivery_fee(price, strike, rate, qty):
    return min(rate * price, D(".125") * max(price - strike, D(0))) * qty


def read_records(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    records = []
    for item in manifest["records"]:
        raw = (manifest_path.parent / item["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"], "Raw response hash mismatch"
        payload = json.loads(raw)
        assert payload["retCode"] == 0
        elapsed = (
            dt.datetime.fromisoformat(item["receivedUtc"])
            - dt.datetime.fromisoformat(item["startedUtc"])
        ).total_seconds()
        lag = (
            dt.datetime.fromisoformat(item["receivedUtc"]).timestamp() - int(payload["time"]) / 1000
        )
        assert 0 <= elapsed <= 30 and -2 <= lag <= 30
        records.append(
            {
                **item,
                "payload": payload,
                "elapsedSeconds": elapsed,
                "responseAgeAtReceiveSeconds": lag,
            }
        )
    return manifest, records


def record_for(records, path, category, base, interval=None):
    matches = [
        r
        for r in records
        if r["path"] == path
        and r["params"].get("category") == category
        and (r["params"].get("baseCoin") == base or r["params"].get("symbol") == base + "USDT")
        and (interval is None or r["params"].get("interval") == interval)
    ]
    assert matches
    return matches


def market_info(records, base, now):
    def bars(interval, span):
        rows = record_for(records, "/v5/market/kline", "spot", base, interval)[0]["payload"][
            "result"
        ]["list"]
        data = sorted([r for r in rows if int(r[0]) + span <= now], key=lambda r: int(r[0]))
        assert len({r[0] for r in data}) == len(data)
        assert all(int(b[0]) - int(a[0]) == span for a, b in zip(data[:-1], data[1:], strict=True))
        return data

    hours, days = bars("60", HOUR), bars("D", DAY)
    assert len(hours) >= 721 and len(days) >= 51
    close = D(hours[-1][4])
    sma20 = sum(D(r[4]) for r in days[-20:]) / 20
    sma50 = sum(D(r[4]) for r in days[-50:]) / 50
    prev20 = sum(D(r[4]) for r in days[-21:-1]) / 20
    ret24 = close / D(hours[-25][4]) - 1
    daily_close = D(days[-1][4])
    last3 = [D(r[4]) for r in hours[-3:]]
    rv = {}
    for n in (1, 3, 7, 30):
        window = hours[-24 * n - 1 :]
        returns = [
            math.log(float(b[4]) / float(a[4]))
            for a, b in zip(window[:-1], window[1:], strict=True)
        ]
        rv[str(n)] = {
            "observations": len(returns),
            "annualizedPct": statistics.stdev(returns) * math.sqrt(8760) * 100,
            "priceReturnPct": (D(window[-1][4]) / D(window[0][4]) - 1) * 100,
        }
    spot = record_for(records, "/v5/market/tickers", "spot", base)[0]["payload"]["result"]["list"][
        0
    ]
    # Descriptive regime gates. They have no independently measured predictive value.
    weak = close < sma20 and close < sma50 and ret24 < 0
    strong = (
        daily_close > sma20
        and sma20 >= prev20
        and ret24 > 0
        and all(c > sma20 and c > sma50 for c in last3)
    )
    return {
        "spot": number(spot, "lastPrice"),
        "lastHourlyClose": close,
        "lastHourlyCloseUtc": utc(int(hours[-1][0]) + HOUR),
        "lastDailyClose": daily_close,
        "lastDailyCloseUtc": utc(int(days[-1][0]) + DAY),
        "dailySma20": sma20,
        "previousDailySma20": prev20,
        "dailySma50": sma50,
        "hourlyReturn24hPct": ret24 * 100,
        "last3HourlyCloses": last3,
        "weakRegime": weak,
        "strongRegime": strong,
        "rv": rv,
    }


def quotes_for(records, base, now):
    raw_ins = [
        i
        for r in record_for(records, "/v5/market/instruments-info", "option", base)
        for i in r["payload"]["result"]["list"]
    ]
    assert len(raw_ins) == len({i["symbol"] for i in raw_ins})
    instruments = {
        i["symbol"]: i
        for i in raw_ins
        if i["status"] == "Trading"
        and i["optionsType"] == "Call"
        and i["quoteCoin"] == "USDT"
        and i["settleCoin"] == "USDT"
    }
    tr = record_for(records, "/v5/market/tickers", "option", base)[0]
    tickers = tr["payload"]["result"]["list"]
    assert len(tickers) == len({r["symbol"] for r in tickers})
    quotes = []
    for ticker in tickers:
        ins = instruments.get(ticker["symbol"])
        if not ins:
            continue
        dte = D(int(ins["deliveryTime"]) - now) / D(DAY)
        if not D(10) <= dte <= D(40):
            continue
        bid, ask = number(ticker, "bid1Price"), number(ticker, "ask1Price")
        q = {
            "symbol": ticker["symbol"],
            "strike": D(ticker["symbol"].rsplit("-", 3)[1]),
            "expiryMs": int(ins["deliveryTime"]),
            "expiryUtc": utc(int(ins["deliveryTime"])),
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "bidSize": number(ticker, "bid1Size"),
            "askSize": number(ticker, "ask1Size"),
            "delta": number(ticker, "delta"),
            "markIvPct": number(ticker, "markIv") * 100,
            "index": number(ticker, "indexPrice"),
            "tick": number(ins["priceFilter"], "tickSize"),
            "minQty": number(ins["lotSizeFilter"], "minOrderQty"),
            "step": number(ins["lotSizeFilter"], "qtyStep"),
            "maxQty": number(ins["lotSizeFilter"], "maxOrderQty"),
            "minPrice": number(ins["priceFilter"], "minPrice"),
            "maxPrice": number(ins["priceFilter"], "maxPrice"),
            "deliveryFeeRate": number(ins, "deliveryFeeRate"),
            "responseTimeUtc": utc(int(tr["payload"]["time"])),
            "volume24h": number(ticker, "volume24h"),
            "openInterest": number(ticker, "openInterest"),
        }
        q["spreadPctMid"] = (
            (ask - bid) / ((ask + bid) / 2) * 100 if bid > 0 and ask >= bid else None
        )
        q["liquidMinSize"] = (
            bid > 0
            and ask >= bid
            and q["bidSize"] >= q["minQty"]
            and q["askSize"] >= q["minQty"]
            and q["spreadPctMid"] <= 5
        )
        quotes.append(q)
    return quotes, {
        "catalogTotal": len(raw_ins),
        "tradingCalls": len(instruments),
        "calls10to40dWithTicker": len(quotes),
        "liquidMinSizeCalls10to40d": sum(q["liquidMinSize"] for q in quotes),
        "responseTimeUtc": utc(int(tr["payload"]["time"])),
    }


def payoff(strategy, long, short, qty, debit, settlement):
    return (
        max(settlement - long["strike"], D(0)) - max(settlement - short["strike"], D(0))
    ) * qty - debit


def calculate(base, strategy, long, short, signal):
    """Both strategies: buy long ask+tick; sell short bid-tick. No mid/mark fills."""
    width = abs(short["strike"] - long["strike"])
    index = (long["index"] + short["index"]) / 2
    buy, sell = long["ask"] + long["tick"], short["bid"] - short["tick"]
    if sell <= 0 or buy > long["maxPrice"] or sell < short["minPrice"]:
        return None
    if long["step"] != short["step"] or long["minQty"] != short["minQty"]:
        raise AssertionError("Mismatched quantity grid needs explicit LCM handling")
    fees = fee(buy, long["index"]) + fee(sell, short["index"])
    debit = buy - sell + fees
    if strategy == "bear_call_credit":
        if not -width < debit < 0:
            return None
        loss, profit = width + debit, -debit
        breakeven = short["strike"] - debit
    else:
        if not 0 < debit < width:
            return None
        loss, profit = debit, width - debit
        breakeven = long["strike"] + debit
    step = long["step"]
    budget_qty = (BUDGET / loss / step).to_integral_value(rounding=ROUND_FLOOR) * step
    # Top size must suffice in both directions for a later exit illustration.
    visible_cap = min(
        long["askSize"],
        long["bidSize"],
        short["askSize"],
        short["bidSize"],
        long["maxQty"],
        short["maxQty"],
    )
    visible_qty = (visible_cap / step).to_integral_value(rounding=ROUND_FLOOR) * step
    qty = min(budget_qty, visible_qty)
    if qty < long["minQty"]:
        return None
    exit_buy = short["ask"] + short["tick"]
    exit_sell = max(D(0), long["bid"] - long["tick"])
    if exit_sell <= 0:
        return None
    exit_fees = fee(exit_buy, short["index"]) + fee(exit_sell, long["index"])
    closing_cash = exit_sell - exit_buy - exit_fees
    rt_loss = debit - closing_cash
    mark_debit = (long["ask"] + long["bid"] - short["ask"] - short["bid"]) / 2
    entry_friction = debit - mark_debit
    two_tick_buy, two_tick_sell = long["ask"] + 2 * long["tick"], short["bid"] - 2 * short["tick"]
    stressed_debit = (
        two_tick_buy
        - two_tick_sell
        + fee(two_tick_buy, long["index"])
        + fee(max(two_tick_sell, D(0)), short["index"])
    )
    stressed_loss = width + stressed_debit if strategy == "bear_call_credit" else stressed_debit
    stressed_profit = -stressed_debit if strategy == "bear_call_credit" else width - stressed_debit
    filters = {
        "legsSpreadLe5Pct": long["liquidMinSize"] and short["liquidMinSize"],
        "rewardAtLeast25PctRisk": profit / loss >= D(".25"),
        "entryFrictionLe15PctMaxProfit": entry_friction / profit <= D(".15"),
        "roundTripLe15PctRisk": rt_loss / loss <= D(".15"),
        "roundTripLe25PctMaxProfit": rt_loss / profit <= D(".25"),
        "twoTickRewardAtLeast25PctRisk": stressed_profit > 0
        and stressed_profit / stressed_loss >= D(".25"),
        "twoTickLossWithinBudgetAtSameQty": stressed_loss * qty <= BUDGET,
    }
    scenarios = []
    prices = sorted(
        {
            D(0),
            index * D(".90"),
            index,
            index * D("1.05"),
            index * D("1.10"),
            index * D("1.20"),
            long["strike"],
            short["strike"],
            breakeven,
            (long["strike"] + short["strike"]) / 2,
        }
    )
    for s in prices:
        gross = payoff(strategy, long, short, qty, debit * qty, s)
        df = delivery_fee(s, long["strike"], long["deliveryFeeRate"], qty) + delivery_fee(
            s, short["strike"], short["deliveryFeeRate"], qty
        )
        scenarios.append(
            {
                "settlementPrice": s,
                "pnlBeforeDelivery": gross,
                "deliveryFees": df,
                "pnlAfterDelivery": gross - df,
            }
        )
    # Invariants at piecewise-payoff knots and beyond both strikes.
    assert abs(payoff(strategy, long, short, qty, debit * qty, breakeven)) < D("1e-20")
    raw_values = [payoff(strategy, long, short, qty, debit * qty, s) for s in prices]
    assert abs(min(raw_values) + loss * qty) < D("1e-20")
    assert abs(max(raw_values) - profit * qty) < D("1e-20")
    assert loss * qty <= BUDGET and qty % step == 0
    assert qty <= visible_cap and qty >= long["minQty"]
    assert rt_loss >= 0 and entry_friction >= 0
    return {
        "base": base,
        "strategy": strategy,
        "expiryUtc": long["expiryUtc"],
        "dte": long["dte"],
        "index": index,
        "long": long,
        "short": short,
        "width": width,
        "quantity": qty,
        "budgetOnlyQty": budget_qty,
        "visibleQtyCap": visible_qty,
        "quantityStep": step,
        "entryBuyPrice": buy,
        "entrySellPrice": sell,
        "entryFees": fees * qty,
        "premiumDebitBeforeFees": (buy - sell) * qty,
        "netEntryDebit": debit * qty,
        "maxLossBeforeFutureFees": loss * qty,
        "maxProfitBeforeFutureFees": profit * qty,
        "minQtyLoss": loss * long["minQty"],
        "budgetUnused": BUDGET - loss * qty,
        "breakevenBeforeFutureFees": breakeven,
        "breakevenDistancePctFromIndex": (breakeven / index - 1) * 100,
        "rewardRisk": profit / loss,
        "lossPerMaxProfit": loss / profit,
        "binaryEndpointHitRateNotProbabilityPct": loss / (profit + loss) * 100,
        "firstLongCashOutlay": (buy + fee(buy, long["index"])) * qty,
        "entryFrictionVsMid": entry_friction * qty,
        "entryFrictionPctMaxProfit": entry_friction / profit * 100,
        "sameSnapshotRoundTripLoss": rt_loss * qty,
        "roundTripPctRisk": rt_loss / loss * 100,
        "roundTripPctMaxProfit": rt_loss / profit * 100,
        "sameSnapshotExitFees": exit_fees * qty,
        "twoTickMaxLossSameQty": stressed_loss * qty,
        "twoTickMaxProfitSameQty": stressed_profit * qty,
        "twoTickRewardRisk": stressed_profit / stressed_loss,
        "filters": filters,
        "costEligible": all(filters.values()),
        "regimeEligible": signal,
        "scenarios": scenarios,
    }


def to_json(value):
    if isinstance(value, D):
        return str(value)
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    manifest, records = read_records(args.manifest)
    now = manifest["asOfMs"]
    output = {
        "manifest": str(args.manifest),
        "manifestSha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "collectionStartedUtc": manifest["startedUtc"],
        "collectionCompletedUtc": manifest["completedUtc"],
        "asOfUtc": utc(now),
        "budgetUSDT": BUDGET,
        "feeRate": RATE,
        "premiumCap": CAP,
        "perLegAdverseTicks": 1,
        "futureFeesIncludedInRiskBudget": False,
        "selectionIsExploratoryNotIndependent": True,
        "recordTiming": [
            {k: r[k] for k in ("path", "params", "elapsedSeconds", "responseAgeAtReceiveSeconds")}
            for r in records
        ],
        "markets": {},
        "universe": {},
        "counts": {},
        "selected": [],
        "nearestRejected": [],
    }
    all_rows = []
    for base in ("BTC", "ETH"):
        market = market_info(records, base, now)
        output["markets"][base] = market
        quotes, counts = quotes_for(records, base, now)
        counts["byExpiry"] = dict(Counter(q["expiryUtc"] for q in quotes))
        output["universe"][base] = counts
        for strategy in ("bear_call_credit", "bull_call_debit"):
            candidates = []
            for long in quotes:
                for short in quotes:
                    if long["expiryMs"] != short["expiryMs"]:
                        continue
                    index = (long["index"] + short["index"]) / 2
                    if strategy == "bear_call_credit":
                        valid = (
                            long["strike"] > short["strike"] > index
                            and D(".15") <= short["delta"] <= D(".30")
                            and D(".05") <= long["delta"] <= D(".20")
                            and D(".02") <= (long["strike"] - short["strike"]) / index <= D(".05")
                        )
                        signal = market["weakRegime"]
                    else:
                        valid = (
                            long["strike"] < short["strike"]
                            and D(".50") <= long["delta"] <= D(".65")
                            and D(".25") <= short["delta"] <= D(".40")
                            and D(".03") <= (short["strike"] - long["strike"]) / index <= D(".08")
                        )
                        signal = market["strongRegime"]
                    if valid:
                        row = calculate(base, strategy, long, short, signal)
                        if row:
                            candidates.append(row)
            output["counts"][base + ":" + strategy] = {
                "structuralBudgetCandidates": len(candidates),
                "allCostFiltersPass": sum(c["costEligible"] for c in candidates),
                "costAndRegimePass": sum(
                    c["costEligible"] and c["regimeEligible"] for c in candidates
                ),
                "failedFilters": dict(
                    Counter(k for c in candidates for k, v in c["filters"].items() if not v)
                ),
            }
            # Transparent deterministic choice by expiry; never rank by claimed expected profit.
            for expiry in sorted({q["expiryUtc"] for q in quotes}):
                pool = [c for c in candidates if c["expiryUtc"] == expiry]
                target_short = D(".25") if strategy == "bear_call_credit" else D(".35")

                def rank(c, target_short=target_short, strategy=strategy):
                    return (
                        abs(c["short"]["delta"] - target_short),
                        abs(c["long"]["delta"] - D(".55"))
                        if strategy == "bull_call_debit"
                        else abs(c["width"] / c["index"] - D(".03")),
                        c["long"]["strike"],
                        c["short"]["strike"],
                    )

                good = sorted([c for c in pool if c["costEligible"]], key=rank)
                if good:
                    output["selected"].append(good[0])
                elif pool:
                    output["nearestRejected"].append(
                        min(
                            pool,
                            key=lambda c: (sum(not v for v in c["filters"].values()), *rank(c)),
                        )
                    )
            all_rows.extend(candidates)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "analysis.json").write_text(
        json.dumps(output, default=to_json, ensure_ascii=False, indent=2) + "\n"
    )
    (args.output_dir / "all_candidates.json").write_text(
        json.dumps(all_rows, default=to_json, ensure_ascii=False, indent=2) + "\n"
    )
    flat = []
    for c in all_rows:
        flat.append(
            {
                k: c[k]
                for k in (
                    "base",
                    "strategy",
                    "expiryUtc",
                    "dte",
                    "quantity",
                    "netEntryDebit",
                    "maxLossBeforeFutureFees",
                    "maxProfitBeforeFutureFees",
                    "rewardRisk",
                    "entryFrictionPctMaxProfit",
                    "roundTripPctRisk",
                    "roundTripPctMaxProfit",
                    "costEligible",
                    "regimeEligible",
                )
            }
            | {
                "long": c["long"]["symbol"],
                "short": c["short"]["symbol"],
                "failedFilters": ";".join(k for k, v in c["filters"].items() if not v),
            }
        )
    with (args.output_dir / "candidates.csv").open("w", newline="") as stream:
        if flat:
            writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    print(
        json.dumps(
            {
                k: v
                for k, v in output.items()
                if k not in ("selected", "nearestRejected", "recordTiming")
            },
            default=to_json,
            ensure_ascii=False,
            indent=2,
        )
    )
    for key in ("selected", "nearestRejected"):
        for c in output[key]:
            print(
                key.upper(),
                json.dumps(
                    {k: v for k, v in c.items() if k not in ("long", "short", "scenarios")}
                    | {"long": c["long"]["symbol"], "short": c["short"]["symbol"]},
                    default=to_json,
                ),
            )


if __name__ == "__main__":
    main()
