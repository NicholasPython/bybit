#!/usr/bin/env python3
"""Analyze a saved public snapshot. Quotes are observations, not fill evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter
from decimal import Decimal
from pathlib import Path

HOUR = 3_600_000
DAY = 24 * HOUR
ROOT = Path(__file__).resolve().parent


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def num(record, key):
    return float(record.get(key) or 0)


def strike(symbol):
    return float(symbol.rsplit("-", 3)[1])


def fee(price, index, qty):
    # VIP0 taker assumption. Examples have >3 days remaining, standard 7% cap.
    return min(
        Decimal("0.0003") * Decimal(str(index)), Decimal("0.07") * Decimal(str(price))
    ) * Decimal(str(qty))


def usable(ticker, instrument):
    q = float(instrument["lotSizeFilter"]["minOrderQty"])
    return (
        0 < num(ticker, "bid1Price") <= num(ticker, "ask1Price")
        and num(ticker, "bid1Size") >= q
        and num(ticker, "ask1Size") >= q
    )


def quote(ticker, instrument, now):
    bid, ask = num(ticker, "bid1Price"), num(ticker, "ask1Price")
    return {
        "symbol": ticker["symbol"],
        "strike": strike(ticker["symbol"]),
        "expiryUtc": utc(int(instrument["deliveryTime"])),
        "daysToExpiry": (int(instrument["deliveryTime"]) - now) / DAY,
        "type": instrument["optionsType"],
        "indexPrice": num(ticker, "indexPrice"),
        "underlyingPrice": num(ticker, "underlyingPrice"),
        "bid": bid,
        "ask": ask,
        "bidSize": num(ticker, "bid1Size"),
        "askSize": num(ticker, "ask1Size"),
        "minQty": float(instrument["lotSizeFilter"]["minOrderQty"]),
        "qtyStep": float(instrument["lotSizeFilter"]["qtyStep"]),
        "usableTopOfBook": usable(ticker, instrument),
        "spreadPctMid": (ask - bid) / ((ask + bid) / 2) * 100 if bid > 0 and ask >= bid else None,
        "markIvPct": num(ticker, "markIv") * 100,
        "bidIvPct": num(ticker, "bid1Iv") * 100,
        "askIvPct": num(ticker, "ask1Iv") * 100,
        "delta": num(ticker, "delta"),
        "volume24h": num(ticker, "volume24h"),
        "openInterest": num(ticker, "openInterest"),
    }


def analyze(snapshot, source):
    now = snapshot["asOfMs"]
    instruments = snapshot["instruments"]
    symbols = [i["symbol"] for i in instruments]
    assert len(symbols) == len(set(symbols)), "Duplicate catalog symbols"
    records = snapshot["records"]
    assert all("error" not in r and r["payload"]["retCode"] == 0 for r in records)

    def rec(path, category, base, interval=None):
        matches = [
            r
            for r in records
            if r["path"] == path
            and r["params"].get("category") == category
            and (r["params"].get("baseCoin") == base or r["params"].get("symbol") == base + "USDT")
            and (interval is None or r["params"].get("interval") == interval)
        ]
        assert len(matches) <= 1
        return matches[0]["payload"]["result"].get("list", []) if matches else []

    out = {
        "sourcePath": str(source),
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "asOfUtc": utc(now),
        "startedUtc": snapshot["startedUtc"],
        "completedUtc": snapshot["completedUtc"],
        "statusCounts": dict(Counter(i["status"] for i in instruments)),
        "duplicateSymbols": 0,
        "publicRequestErrors": 0,
        "rvConvention": (
            "Sample stdev of exactly N completed hourly log returns times sqrt(8760); "
            "N=24*days. Windows end at last completed candle. Not a forecast."
        ),
        "feeAssumption": (
            "VIP0 taker min(0.03%*index,7%*leg premium)*qty per leg. Examples >3d. "
            "Entry fees only; delivery/exit fees separately discussed."
        ),
        "bases": {},
    }
    all_quotes = {}
    for base in snapshot["bases"]:
        ins = {
            i["symbol"]: i
            for i in instruments
            if i["baseCoin"] == base and i["status"] == "Trading"
        }
        raw_tickers = rec("/v5/market/tickers", "option", base)
        assert len(raw_tickers) == len({t["symbol"] for t in raw_tickers})
        ts = [t for t in raw_tickers if t["symbol"] in ins]
        qs = [quote(t, ins[t["symbol"]], now) for t in ts]
        all_quotes.update({q["symbol"]: q for q in qs})
        expiries = sorted({int(i["deliveryTime"]) for i in ins.values()})
        good = [q for q in qs if q["usableTopOfBook"]]
        focused = [
            q for q in good if 3 <= q["daysToExpiry"] <= 45 and 0.1 <= abs(q["delta"]) <= 0.9
        ]
        d = {
            "contracts": len(ins),
            "callPut": dict(Counter(i["optionsType"] for i in ins.values())),
            "quoteSettleCoins": sorted({(i["quoteCoin"], i["settleCoin"]) for i in ins.values()}),
            "minQtyAndStep": sorted(
                {
                    (i["lotSizeFilter"]["minOrderQty"], i["lotSizeFilter"]["qtyStep"])
                    for i in ins.values()
                }
            ),
            "deliveryFeeRates": sorted({i["deliveryFeeRate"] for i in ins.values()}),
            "expiries": [{"utc": utc(e), "days": (e - now) / DAY} for e in expiries],
            "liquidity": {
                "tickerRows": len(raw_tickers),
                "tradingTickerRows": len(ts),
                "missingTradingTickers": len(ins) - len(ts),
                "usableMinSizeTwoSided": len(good),
                "zeroVolume24h": sum(q["volume24h"] == 0 for q in qs),
                "zeroOpenInterest": sum(q["openInterest"] == 0 for q in qs),
                "focused3to45DaysDelta10to90Count": len(focused),
                "focusedMedianSpreadPctMid": statistics.median(q["spreadPctMid"] for q in focused)
                if focused
                else None,
            },
        }
        market = {}
        spot = rec("/v5/market/tickers", "spot", base)
        if spot:
            t = spot[0]
            market.update(
                {
                    "spot": num(t, "lastPrice"),
                    "spot24hPct": num(t, "price24hPcnt") * 100,
                    "low24h": num(t, "lowPrice24h"),
                    "high24h": num(t, "highPrice24h"),
                    "spotTurnover24h": num(t, "turnover24h"),
                }
            )
            bars = sorted(rec("/v5/market/kline", "spot", base, "60"), key=lambda r: int(r[0]))
            closed = [r for r in bars if int(r[0]) + HOUR <= now]
            assert len({r[0] for r in closed}) == len(closed)
            assert all(
                int(b[0]) - int(a[0]) == HOUR for a, b in zip(closed[:-1], closed[1:], strict=True)
            )
            assert all(
                float(r[3]) > 0
                and float(r[2]) >= max(float(r[1]), float(r[4]))
                and float(r[3]) <= min(float(r[1]), float(r[4]))
                for r in closed
            )
            market["rv"] = {}
            for days in (1, 3, 7, 14, 30):
                count = days * 24
                assert len(closed) >= count + 1
                v = closed[-count - 1 :]
                returns = [
                    math.log(float(b[4]) / float(a[4])) for a, b in zip(v[:-1], v[1:], strict=True)
                ]
                assert len(returns) == count
                market["rv"][str(days)] = {
                    "returns": count,
                    "fromCloseUtc": utc(int(v[0][0]) + HOUR),
                    "toCloseUtc": utc(int(v[-1][0]) + HOUR),
                    "annualizedPct": statistics.stdev(returns) * math.sqrt(8760) * 100,
                    "priceReturnPct": (float(v[-1][4]) / float(v[0][4]) - 1) * 100,
                }
            daily = sorted(rec("/v5/market/kline", "spot", base, "D"), key=lambda r: int(r[0]))
            daily = [r for r in daily if int(r[0]) + DAY <= now]
            market["daily"] = {"lastCloseUtc": utc(int(daily[-1][0]) + DAY)}
            for n in (20, 50):
                market["daily"][f"sma{n}"] = statistics.mean(float(r[4]) for r in daily[-n:])
            linear = rec("/v5/market/tickers", "linear", base)
            if linear:
                market["perpetualFundingRatePct"] = num(linear[0], "fundingRate") * 100
        d["market"] = market
        d["atmByExpiry"] = []
        for e in expiries:
            expiry_qs = [q for q in qs if q["expiryUtc"] == utc(e)]
            if not expiry_qs:
                continue
            forward = statistics.median(q["underlyingPrice"] for q in expiry_qs)
            for side in ("Call", "Put"):
                side_qs = [q for q in expiry_qs if q["type"] == side]
                if side_qs:
                    d["atmByExpiry"].append(min(side_qs, key=lambda q: abs(q["strike"] - forward)))
        out["bases"][base] = d

    # Illustrations selected for understandable strikes, not optimized backtest results.
    specs = [
        (
            "ETH bear put debit",
            "ETH-27NOV26-1800-P-USDT",
            "ETH-27NOV26-1700-P-USDT",
            ".1",
            [1500, 1700, 1750, 1800, 2000],
        ),
        (
            "BTC bear put debit",
            "BTC-27NOV26-65000-P-USDT",
            "BTC-27NOV26-60000-P-USDT",
            ".01",
            [55000, 60000, 63000, 65000, 70000],
        ),
        (
            "BTC bear call credit",
            "BTC-27NOV26-71000-C-USDT",
            "BTC-27NOV26-69000-C-USDT",
            ".01",
            [60000, 64850, 69000, 70000, 71000],
        ),
        (
            "ETH bear call credit",
            "ETH-27NOV26-2050-C-USDT",
            "ETH-27NOV26-1975-C-USDT",
            ".1",
            [1700, 1809, 1975, 2000, 2050],
        ),
        (
            "ETH bull put credit",
            "ETH-27NOV26-1550-P-USDT",
            "ETH-27NOV26-1650-P-USDT",
            ".1",
            [1500, 1550, 1600, 1650, 1809],
        ),
    ]
    examples = []
    for name, buy_symbol, sell_symbol, quantity, scenario_prices in specs:
        buy, sell = all_quotes[buy_symbol], all_quotes[sell_symbol]
        qty = Decimal(quantity)
        assert buy["expiryUtc"] == sell["expiryUtc"] and buy["daysToExpiry"] > 3
        assert buy["type"] == sell["type"] and buy["usableTopOfBook"] and sell["usableTopOfBook"]
        assert Decimal(str(buy["askSize"])) >= qty and Decimal(str(sell["bidSize"])) >= qty
        for leg in (buy, sell):
            assert qty >= Decimal(str(leg["minQty"]))
            assert qty % Decimal(str(leg["qtyStep"])) == 0
        entry_fee = fee(buy["ask"], buy["indexPrice"], qty) + fee(
            sell["bid"], sell["indexPrice"], qty
        )
        premium_debit = (Decimal(str(buy["ask"])) - Decimal(str(sell["bid"]))) * qty
        debit = premium_debit + entry_fee
        kbuy, ksell = Decimal(str(buy["strike"])), Decimal(str(sell["strike"]))
        width = abs(kbuy - ksell) * qty
        if buy["type"] == "Put":
            maximum = width - debit if kbuy > ksell else -debit
            minimum = -debit if kbuy > ksell else -width - debit
            breakeven = kbuy - debit / qty if kbuy > ksell else ksell + debit / qty
        else:
            maximum = -debit if kbuy > ksell else width - debit
            minimum = -width - debit if kbuy > ksell else -debit
            breakeven = ksell - debit / qty if kbuy > ksell else kbuy + debit / qty

        def payoff(price, buy=buy, kbuy=kbuy, ksell=ksell, qty=qty, debit=debit):
            s = Decimal(str(price))
            if buy["type"] == "Put":
                long_pay, short_pay = max(kbuy - s, 0), max(ksell - s, 0)
            else:
                long_pay, short_pay = max(s - kbuy, 0), max(s - ksell, 0)
            return (long_pay - short_pay) * qty - debit

        assert payoff(breakeven) == 0
        assert payoff(0) in (minimum, maximum)
        assert payoff(10 * max(kbuy, ksell)) in (minimum, maximum)
        # Cost of immediately closing at the same displayed opposite-side quotes.
        closing_value = (Decimal(str(buy["bid"])) - Decimal(str(sell["ask"]))) * qty
        exit_fee = fee(buy["bid"], buy["indexPrice"], qty) + fee(
            sell["ask"], sell["indexPrice"], qty
        )
        examples.append(
            {
                "name": name,
                "buy": buy,
                "sell": sell,
                "quantity": float(qty),
                "premiumDebit": float(premium_debit),
                "entryFees": float(entry_fee),
                "netEntryDebit": float(debit),
                "maxProfitExcludingFutureFees": float(maximum),
                "maxLossExcludingFutureFees": float(-minimum),
                "breakevenExcludingFutureFees": float(breakeven),
                "maxLossPerMaxProfit": float(-minimum / maximum),
                "sameSnapshotRoundTripLossIncludingTakerFees": float(
                    debit - closing_value + exit_fee
                ),
                "expiryScenariosBeforeDeliveryFees": [
                    {"settlementPrice": s, "pnl": float(payoff(s))} for s in scenario_prices
                ],
                "illustrativeEquityFor0_5PctRiskIgnoringMargin": float(-minimum / Decimal(".005")),
            }
        )
    out["examples"] = examples
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "analysis.json")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    output = analyze(snapshot, args.snapshot)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print("ASOF", output["asOfUtc"], output["statusCounts"])
    for base, d in output["bases"].items():
        print(base, d["contracts"], d["minQtyAndStep"], d["liquidity"])
        if base in ("BTC", "ETH"):
            print("MARKET", base, json.dumps(d["market"]))
            for q in d["atmByExpiry"]:
                if q["type"] == "Call" and 3 < q["daysToExpiry"] < 50:
                    print("ATM", json.dumps(q))
    for e in output["examples"]:
        print("EXAMPLE", json.dumps({k: v for k, v in e.items() if k not in ("buy", "sell")}))


if __name__ == "__main__":
    main()
