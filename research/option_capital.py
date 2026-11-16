#!/usr/bin/env python3
"""测量 Bybit 现货与期权的**最低现金投入**：一笔最小可成交订单要占多少资金。

只用公开只读接口，不带密钥。它回答的是"账户至少要多少钱才能下出第一单"，
不是策略期望收益；期权以**卖一价**（不是标记价）计价，并区分有双边报价与
只有挂单的合约。

用法（在能访问 Bybit 的机器上）::

    python option_capital.py --out results/option_capital.json --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
from typing import Any

import httpx
from fetch_bybit_data import fetch_instruments, get_result

#: 期权 taker 手续费 = min(指数价 × 0.0003, 权利金 × 0.07) × 数量。
OPTION_FEE_INDEX_RATE = 0.0003
OPTION_FEE_PREMIUM_RATE = 0.07
#: 近似平值筛选带：执行价偏离标的价不超过该比例，到期日在该区间内。
ATM_STRIKE_BAND = 0.08
ATM_MIN_DTE = 20
ATM_MAX_DTE = 45


def spot_snapshot(client: httpx.Client) -> dict[str, Any]:
    """现货每个 USDT 对的最小订单金额与最小数量。"""
    instruments = fetch_instruments(client, "spot")
    tickers = {
        row["symbol"]: row
        for row in get_result(client, "/v5/market/tickers", {"category": "spot"})["list"]
    }
    pairs = []
    for item in instruments:
        if item.get("quoteCoin") != "USDT" or item.get("status") != "Trading":
            continue
        lot = item.get("lotSizeFilter", {})
        ticker = tickers.get(item["symbol"], {})
        amount = float(lot.get("minOrderAmt") or 0)
        quantity = float(lot.get("minOrderQty") or 0)
        last = float(ticker.get("lastPrice") or 0)
        pairs.append(
            {
                "symbol": item["symbol"],
                "min_order_amt": amount,
                "min_order_qty": quantity,
                "last_price": last,
                "turnover_24h": float(ticker.get("turnover24h") or 0),
                "min_cash": max(amount, quantity * last),
            }
        )
    pairs.sort(key=lambda row: row["min_cash"])
    return {
        "trading_usdt_pairs": len(pairs),
        "pairs_below_5_usdt": [row for row in pairs if 0 < row["min_order_amt"] < 5],
        "min_order_amt_distribution": {
            str(value): sum(1 for row in pairs if row["min_order_amt"] == value)
            for value in sorted({row["min_order_amt"] for row in pairs})
        },
        "cheapest_by_cash": pairs[:10],
        "majors": [
            row
            for row in pairs
            if row["symbol"] in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
        ],
    }


def option_snapshot(client: httpx.Client, base_coin: str) -> dict[str, Any]:
    """期权的最小权利金支出：最小数量 × 卖一价，并标出双边报价。"""
    instruments = fetch_instruments(client, "option", base_coin)
    tickers = {
        row["symbol"]: row
        for row in get_result(
            client, "/v5/market/tickers", {"category": "option", "baseCoin": base_coin}
        )["list"]
    }
    spot_row = get_result(
        client, "/v5/market/tickers", {"category": "spot", "symbol": f"{base_coin}USDT"}
    )["list"]
    spot_price = float(spot_row[0]["lastPrice"]) if spot_row else 0.0
    today = dt.datetime.now(dt.UTC).date()

    contracts = []
    for item in instruments:
        symbol = item["symbol"]
        ticker = tickers.get(symbol)
        if ticker is None or item.get("status") != "Trading":
            continue
        parts = symbol.split("-")
        try:
            strike = float(parts[2])
        except (IndexError, ValueError):
            continue
        quantity = float(item.get("lotSizeFilter", {}).get("minOrderQty") or 0)
        bid = float(ticker.get("bid1Price") or 0)
        ask = float(ticker.get("ask1Price") or 0)
        delivery = dt.datetime.fromtimestamp(
            int(item["deliveryTime"]) / 1000, tz=dt.UTC
        ).date()
        two_sided = bid > 0 and ask > 0
        premium = quantity * ask
        index_fee = min(
            spot_price * OPTION_FEE_INDEX_RATE, ask * OPTION_FEE_PREMIUM_RATE
        ) * quantity
        contracts.append(
            {
                "symbol": symbol,
                "option_type": item.get("optionsType"),
                "strike": strike,
                "dte": (delivery - today).days,
                "min_order_qty": quantity,
                "bid": bid,
                "ask": ask,
                "iv": float(ticker.get("iv") or 0),
                "two_sided": two_sided,
                "min_premium": premium if two_sided else None,
                "taker_fee": index_fee,
                "delivery_fee_rate": float(item.get("deliveryFeeRate") or 0),
            }
        )
    liquid = [row for row in contracts if row["two_sided"]]
    liquid.sort(key=lambda row: row["min_premium"])
    premiums = [row["min_premium"] for row in liquid]
    atm = [
        row
        for row in liquid
        if ATM_MIN_DTE <= row["dte"] <= ATM_MAX_DTE
        and spot_price
        and abs(row["strike"] / spot_price - 1) <= ATM_STRIKE_BAND
    ]
    atm.sort(key=lambda row: row["min_premium"])
    return {
        "base_coin": base_coin,
        "spot_price": spot_price,
        "listed_contracts": len(contracts),
        "two_sided_contracts": len(liquid),
        "min_order_qty": liquid[0]["min_order_qty"] if liquid else None,
        "min_premium_usdt": {
            "min": premiums[0] if premiums else None,
            "p10": statistics.quantiles(premiums, n=10)[0] if len(premiums) > 9 else None,
            "median": statistics.median(premiums) if premiums else None,
        },
        "count_by_premium_cap": {
            str(cap): sum(1 for value in premiums if value <= cap)
            for cap in (1, 5, 10, 20, 50, 100, 200)
        },
        "cheapest_two_sided": liquid[:5],
        "near_atm_20_45d": atm[:5],
        "near_atm_count": len(atm),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="测量 Bybit 现货与期权的最低现金投入")
    parser.add_argument("--out", default="research/results/option_capital.json")
    parser.add_argument("--proxy", default="", help="HTTP 代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--bases", default="BTC,ETH", help="期权标的币")
    args = parser.parse_args()

    with httpx.Client(
        proxy=args.proxy or None,
        timeout=30.0,
        trust_env=False,
        headers={"Accept": "application/json"},
    ) as client:
        report = {
            "snapshot_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Bybit V5 public market endpoints; no credentials",
            "spot": spot_snapshot(client),
            "options": [
                option_snapshot(client, base.strip().upper())
                for base in args.bases.split(",")
                if base.strip()
            ],
        }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
