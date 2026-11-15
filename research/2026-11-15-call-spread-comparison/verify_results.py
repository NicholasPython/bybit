#!/usr/bin/env python3
"""Independently reconcile saved candidate arithmetic with raw public responses.

No imports from the analyzer, no network or account access. Run with -I.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path


def near(a, b):
    assert abs(D(a) - D(b)) < D("1e-18"), (a, b)


def charge(price, index, qty):
    return min(D(".0003") * index, D(".07") * price) * qty


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    catalog, tickers = {}, {}
    raw_hashes = 0
    for record in manifest["records"]:
        content = (args.manifest.parent / record["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == record["sha256"]
        raw_hashes += 1
        payload = json.loads(content)
        if record["path"].endswith("instruments-info"):
            catalog.update({r["symbol"]: r for r in payload["result"]["list"]})
        if record["path"].endswith("tickers") and record["params"]["category"] == "option":
            tickers.update({r["symbol"]: r for r in payload["result"]["list"]})
    candidates = json.loads((args.results / "all_candidates.json").read_text())
    summary = json.loads((args.results / "analysis.json").read_text())
    scenarios = 0
    cap_legs = 0
    for candidate in candidates:
        long, short = candidate["long"], candidate["short"]
        qty = D(candidate["quantity"])
        assert catalog[long["symbol"]]["deliveryTime"] == catalog[short["symbol"]]["deliveryTime"]
        for leg in (long, short):
            raw = tickers[leg["symbol"]]
            metadata = catalog[leg["symbol"]]
            assert metadata["status"] == "Trading"
            assert metadata["quoteCoin"] == metadata["settleCoin"] == "USDT"
            assert metadata["optionsType"] == "Call"
            for field, api_field in (
                ("bid", "bid1Price"),
                ("ask", "ask1Price"),
                ("bidSize", "bid1Size"),
                ("askSize", "ask1Size"),
            ):
                near(leg[field], raw[api_field])
            assert qty >= D(metadata["lotSizeFilter"]["minOrderQty"])
            assert qty % D(metadata["lotSizeFilter"]["qtyStep"]) == 0
            assert qty <= min(D(leg["bidSize"]), D(leg["askSize"]))
        buy = D(long["ask"]) + D(long["tick"])
        sell = D(short["bid"]) - D(short["tick"])
        assert buy % D(long["tick"]) == 0 and sell % D(short["tick"]) == 0
        fees = charge(buy, D(long["index"]), qty) + charge(sell, D(short["index"]), qty)
        for leg, price in ((long, buy), (short, sell)):
            cap_legs += D(".07") * price < D(".0003") * D(leg["index"])
        debit = (buy - sell) * qty + fees
        near(candidate["entryFees"], fees)
        near(candidate["netEntryDebit"], debit)
        near(candidate["firstLongCashOutlay"], buy * qty + charge(buy, D(long["index"]), qty))
        width = abs(D(long["strike"]) - D(short["strike"])) * qty
        if D(long["strike"]) > D(short["strike"]):
            near(candidate["maxLossBeforeFutureFees"], width + debit)
            near(candidate["maxProfitBeforeFutureFees"], -debit)
        else:
            near(candidate["maxLossBeforeFutureFees"], debit)
            near(candidate["maxProfitBeforeFutureFees"], width - debit)
        assert D(candidate["maxLossBeforeFutureFees"]) <= 25
        close_sell = D(long["bid"]) - D(long["tick"])
        close_buy = D(short["ask"]) + D(short["tick"])
        close_cost = (close_buy - close_sell) * qty + charge(close_buy, D(short["index"]), qty)
        close_cost += charge(close_sell, D(long["index"]), qty)
        near(candidate["sameSnapshotRoundTripLoss"], debit + close_cost)
        for scenario in candidate["scenarios"]:
            s = D(scenario["settlementPrice"])
            intrinsic_long = max(s - D(long["strike"]), D(0))
            intrinsic_short = max(s - D(short["strike"]), D(0))
            pnl = (intrinsic_long - intrinsic_short) * qty - debit
            delivery = sum(
                min(D(leg["deliveryFeeRate"]) * s, D(".125") * iv) * qty
                for leg, iv in ((long, intrinsic_long), (short, intrinsic_short))
            )
            near(scenario["pnlBeforeDelivery"], pnl)
            near(scenario["deliveryFees"], delivery)
            near(scenario["pnlAfterDelivery"], pnl - delivery)
            scenarios += 1
    assert len(candidates) == 29
    assert sum(c["costEligible"] for c in candidates) == 14
    assert not any(c["regimeEligible"] and c["costEligible"] for c in candidates)
    assert len(summary["selected"]) == 4 and len(summary["nearestRejected"]) == 5
    validation = {
        "rawResponseHashesVerified": raw_hashes,
        "candidateCalculationsIndependentlyVerified": len(candidates),
        "payoffAndDeliveryScenariosVerified": scenarios,
        "candidateEntryLegsUsingPremiumFeeCap": cap_legs,
        "costPassCount": 14,
        "costAndRegimePassCount": 0,
        "checkedAgainstRawMetadataAndBidAsk": True,
        "analysisSha256": hashlib.sha256((args.results / "analysis.json").read_bytes()).hexdigest(),
        "realFillsVerified": False,
        "accountMarginVerified": False,
    }
    (args.results / "verification.json").write_text(json.dumps(validation, indent=2) + "\n")
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
