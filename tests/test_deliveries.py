"""Synthetic option-expiry contracts; no exchange or database is contacted."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import httpx
import pytest

from bybitbot.adapters.base import InstrumentSpec, InsufficientCapability, OptionDeliveryView
from bybitbot.adapters.bybit_rest import BybitRestAdapter, BybitRestError, _parse_execution

D = Decimal
NOW = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)
EXPIRY = NOW.replace(hour=8)
SYMBOL = "BTC-1NOV26-100000-C-USDT"
DELIVERY_PATH = "/v5/asset/delivery-record"
LOG_PATH = "/v5/account/transaction-log"
CLOSED_PATH = "/v5/position/get-closed-positions"


def millis(moment):
    return int(moment.timestamp() * 1000)


def spec(**changes):
    return replace(
        InstrumentSpec(
            category="option",
            symbol=SYMBOL,
            base_coin="BTC",
            quote_coin="USDT",
            settle_coin="USDT",
            status="Delivering",
            options_type="Call",
            strike_price=D("100000"),
            delivery_time=EXPIRY,
        ),
        **changes,
    )


def delivery(**changes):
    return {
        "symbol": SYMBOL,
        "side": "Buy",
        "deliveryTime": millis(EXPIRY) + 860,
        "strike": "100000",
        "position": "0.02",
        "deliveryPrice": "101000",
        # These are deliberately unsuitable as payout/fee inputs. Only the
        # separate DELIVERY cash log establishes their economic meaning.
        "deliveryRpl": "-123456.789",
        "fee": "999",
        "entryPrice": "2500",
        **changes,
    }


def cash_log(**changes):
    return {
        "id": "delivery-log-1",
        "symbol": SYMBOL,
        "side": "Buy",
        "category": "option",
        "type": "DELIVERY",
        "currency": "USDT",
        "transactionTime": str(millis(EXPIRY) + 1000),
        "qty": "0.02",
        "cashFlow": "20",
        "fee": "0.3",
        "funding": "0",
        "change": "19.7",
        **changes,
    }


def closed_position(**changes):
    return {
        "symbol": SYMBOL,
        "side": "Buy",
        "qty": "0.02",
        "closeTime": millis(EXPIRY) + 1100,
        "deliveryPrice": "99000",
        "deliveryFee": "0",
        "totalCloseFee": "0",
        "totalPnl": "-50",
        **changes,
    }


def response(rows, *, category="option", cursor=""):
    result = {"list": rows, "nextPageCursor": cursor}
    if category is not None:
        result["category"] = category
    return httpx.Response(200, json={"retCode": 0, "result": result, "time": millis(NOW)})


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("bybitbot.adapters.bybit_rest.now_utc", lambda: NOW)


@asynccontextmanager
async def adapter(handler):
    instance = BybitRestAdapter(
        base_url="https://api-testnet.bybit.com",
        api_key="synthetic-key",
        api_secret="synthetic-secret",
        environment="testnet",
        read_max_attempts=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        instance._client = client
        yield instance
        instance._client = None


def handler_for(deliveries, logs=(), closed=()):
    calls = []
    payloads = {DELIVERY_PATH: deliveries, LOG_PATH: logs, CLOSED_PATH: closed}

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        return response(list(payloads[request.url.path]))

    return handle, calls


async def fetch(instance, instrument=None, **times):
    return await instance.get_option_deliveries(
        instrument or spec(),
        start_time=times.get("start_time", EXPIRY - dt.timedelta(minutes=1)),
        end_time=times.get("end_time", NOW),
    )


async def test_positive_payout_uses_delivery_intrinsic_and_exact_cash_log():
    handle, calls = handler_for([delivery()], [cash_log()])
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
        (repeat,) = await fetch(instance)
    assert isinstance(fact, OptionDeliveryView)
    assert fact == repeat
    assert (fact.symbol, fact.side, fact.qty, fact.currency) == (SYMBOL, "Buy", D("0.02"), "USDT")
    assert (fact.payout, fact.fee, fact.delivery_price, fact.strike_price) == (
        D("20"),
        D("0.3"),
        D("101000"),
        D("100000"),
    )
    assert fact.occurred_at == EXPIRY + dt.timedelta(milliseconds=860)
    assert fact.evidence["source"] == "bybit"
    assert fact.evidence["transaction_log_ids"] == ["delivery-log-1"]
    assert fact.evidence["fee_source"] == LOG_PATH
    assert fact.settlement_id.startswith("bybit-option-") and len(fact.settlement_id) < 128
    assert fact.reference.startswith(DELIVERY_PATH)
    assert "balance_reconciled" not in fact.evidence
    assert "positions_reconciled" not in fact.evidence
    assert all(request.url.path != CLOSED_PATH for request in calls)
    with pytest.raises(FrozenInstanceError):
        fact.fee = D("123")


async def test_put_intrinsic_and_legacy_usdc_symbol_are_supported():
    instrument = spec(
        symbol="BTC-1NOV26-100000-P",
        options_type="Put",
        quote_coin="USDC",
        settle_coin="USDC",
    )
    handle, _ = handler_for(
        [delivery(symbol=instrument.symbol, deliveryPrice="99000")],
        [cash_log(symbol=instrument.symbol, currency="USDC")],
    )
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance, instrument)
    assert fact.payout == D("20") and fact.currency == "USDC"


async def test_allowlisted_evidence_does_not_copy_arbitrary_bodies():
    handle, _ = handler_for(
        [delivery(secret_echo="do-not-keep", deliveryRpl="not-a-number")],
        [cash_log(secret_echo="do-not-keep", raw={"credentials": "do-not-keep"})],
    )
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    rendered = json.dumps(fact.evidence)
    assert "do-not-keep" not in rendered
    assert "synthetic-" not in rendered
    assert "deliveryRpl" not in rendered
    assert set(fact.evidence["delivery"]) == {
        "symbol",
        "side",
        "position",
        "deliveryTime",
        "deliveryPrice",
        "strike",
    }


async def test_zero_payout_without_log_requires_closed_position_proof():
    handle, calls = handler_for(
        [delivery(deliveryPrice="99000", fee="0")], closed=[closed_position()]
    )
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    assert fact.payout == 0 and fact.fee == 0 and fact.qty == D("0.02")
    assert fact.evidence["fee_source"] == CLOSED_PATH
    assert fact.evidence["transaction_log_ids"] == []
    assert fact.evidence["closed_position"]["deliveryFee"] == "0"
    proof_query = next(request.url.params for request in calls if request.url.path == CLOSED_PATH)
    assert int(proof_query["startTime"]) >= millis(EXPIRY)
    assert int(proof_query["endTime"]) <= millis(EXPIRY) + 60_000


async def test_zero_with_log_uses_verified_fee_and_does_not_query_closed_positions():
    handle, calls = handler_for(
        [delivery(deliveryPrice="99000")],
        [cash_log(cashFlow="0", fee="0", change="0")],
    )
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    assert fact.payout == 0 and fact.evidence["transaction_log_ids"] == ["delivery-log-1"]
    assert all(request.url.path != CLOSED_PATH for request in calls)


@pytest.mark.parametrize("price", ["99000", "100000"])
async def test_otm_and_atm_require_actual_zero_expiry_corroboration(price):
    handle, _ = handler_for([delivery(deliveryPrice=price, fee="0")])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="closed-position proof"):
            await fetch(instance)


async def test_positive_payout_cannot_use_closed_position_instead_of_cash_log():
    handle, calls = handler_for([delivery()], closed=[closed_position(deliveryPrice="101000")])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="missing DELIVERY"):
            await fetch(instance)
    assert all(request.url.path != CLOSED_PATH for request in calls)


async def test_absent_delivery_does_not_synthesize_zero_from_other_sources():
    handle, calls = handler_for([], [cash_log()], [closed_position()])
    async with adapter(handle) as instance:
        assert await fetch(instance) == []
    assert [request.url.path for request in calls] == [DELIVERY_PATH]


@pytest.mark.parametrize("changes", [{}, {"position": "0.03"}, {"deliveryPrice": "102000"}])
async def test_multiple_delivery_rows_even_identical_are_ambiguous(changes):
    handle, _ = handler_for([delivery(), delivery(**changes)], [cash_log()])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="Multiple option delivery"):
            await fetch(instance)


@pytest.mark.parametrize("changes", [{}, {"id": "second-id"}, {"qty": "0.03"}])
async def test_multiple_cash_log_rows_even_identical_are_ambiguous(changes):
    handle, _ = handler_for([delivery()], [cash_log(), cash_log(**changes)])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="Multiple option DELIVERY"):
            await fetch(instance)


@pytest.mark.parametrize(
    "changes",
    [
        {"position": "0"},
        {"position": "-1"},
        {"position": "NaN"},
        {"position": ""},
        {"position": True},
        {"position": None},
        {"deliveryPrice": "0"},
        {"deliveryPrice": "Infinity"},
        {"deliveryPrice": "oops"},
        {"strike": "99999"},
        {"strike": None},
        {"side": "Sell"},
        {"side": "None"},
        {"symbol": "BTC-2NOV26-100000-C-USDT"},
        {"deliveryTime": None},
        {"deliveryTime": True},
        {"deliveryTime": millis(EXPIRY) - 1},
        {"deliveryTime": millis(EXPIRY) + 60_001},
    ],
)
async def test_malformed_or_mismatching_delivery_fails_closed(changes):
    handle, _ = handler_for([delivery(**changes)], [cash_log()])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "BTC"},
        {"currency": "USDC"},
        {"currency": ""},
        {"category": "linear"},
        {"type": "SETTLEMENT"},
        {"type": "TRADE"},
        {"qty": "0.01"},
        {"qty": "-0.02"},
        {"qty": ""},
        {"cashFlow": "19.7"},
        {"cashFlow": "-20"},
        {"cashFlow": None},
        {"fee": "-0.3"},
        {"fee": "NaN"},
        {"fee": ""},
        {"fee": "0.4"},
        {"funding": "0.1"},
        {"funding": ""},
        {"change": "20"},
        {"side": "Sell"},
        {"id": ""},
        {"id": "not a valid id"},
        {"transactionTime": millis(EXPIRY) + 60_001},
        {"transactionTime": None},
    ],
)
async def test_malformed_or_mismatching_cash_log_fails_closed(changes):
    handle, _ = handler_for([delivery()], [cash_log(**changes)])
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "Sell"},
        {"qty": "0.01"},
        {"deliveryPrice": "99000.01"},
        {"deliveryFee": "0.1"},
        {"deliveryFee": None},
        {"totalCloseFee": "0.01"},
        {"totalCloseFee": None},
        {"closeTime": millis(EXPIRY) - 1},
        {"closeTime": millis(EXPIRY) + 60_001},
        {"closeTime": None},
    ],
)
async def test_closed_sell_or_mismatching_zero_expiry_is_excluded(changes):
    handle, _ = handler_for(
        [delivery(deliveryPrice="99000", fee="0")], closed=[closed_position(**changes)]
    )
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


async def test_multiple_zero_closed_position_proofs_are_ambiguous():
    handle, _ = handler_for(
        [delivery(deliveryPrice="99000", fee="0")],
        closed=[closed_position(), closed_position()],
    )
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="unique closed-position proof"):
            await fetch(instance)


@pytest.mark.parametrize("fee", ["0.1", "-1", "", None, "NaN"])
async def test_zero_without_cash_log_rejects_unverified_delivery_record_fee(fee):
    handle, calls = handler_for(
        [delivery(deliveryPrice="99000", fee=fee)], closed=[closed_position()]
    )
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)
    assert all(request.url.path != CLOSED_PATH for request in calls)


async def test_ordinary_sell_closed_before_expiry_cannot_be_expiry_proof():
    handle, _ = handler_for(
        [delivery(deliveryPrice="99000", fee="0")],
        closed=[closed_position(closeTime=millis(EXPIRY) - 30_000, totalCloseFee="0.01")],
    )
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


async def test_cash_log_other_symbols_same_base_and_currency_are_not_anchors():
    handle, _ = handler_for(
        [delivery()],
        [cash_log(symbol="BTC-1NOV26-110000-C-USDT")],
    )
    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="missing DELIVERY"):
            await fetch(instance)


async def test_explicit_filters_and_opaque_cursor_pagination_for_delivery_and_logs():
    calls = []
    opaque_cursor = "a%2B +/="

    def handle(request):
        calls.append(request)
        params = request.url.params
        assert params["category"] == "option"
        assert int(params["endTime"]) - int(params["startTime"]) < 7 * 86_400_000
        if request.url.path == DELIVERY_PATH:
            assert params["symbol"] == SYMBOL and params["limit"] == "50"
            rows = [delivery()] if "cursor" in params else []
        else:
            assert request.url.path == LOG_PATH
            assert params["accountType"] == "UNIFIED" and params["type"] == "DELIVERY"
            assert params["currency"] == "USDT" and params["baseCoin"] == "BTC"
            assert "symbol" not in params and params["limit"] == "50"
            rows = [cash_log()] if "cursor" in params else []
        if "cursor" not in params:
            return response(rows, cursor=opaque_cursor)
        assert params["cursor"] == opaque_cursor
        return response(rows)

    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    assert fact.payout == 20 and len(calls) == 4


async def test_zero_proof_paginates_and_preserves_opaque_cursor():
    def handle(request):
        if request.url.path == DELIVERY_PATH:
            return response([delivery(deliveryPrice="99000", fee="0")])
        if request.url.path == LOG_PATH:
            return response([])
        if "cursor" not in request.url.params:
            return response([], cursor="closed%2Bcursor")
        assert request.url.params["cursor"] == "closed%2Bcursor"
        return response([closed_position()])

    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    assert fact.payout == 0


async def test_long_range_splits_into_contiguous_at_most_seven_day_windows():
    calls = {DELIVERY_PATH: [], LOG_PATH: []}
    start = EXPIRY - dt.timedelta(days=15)

    def handle(request):
        params = request.url.params
        window = int(params["startTime"]), int(params["endTime"])
        calls[request.url.path].append(window)
        candidate = delivery() if request.url.path == DELIVERY_PATH else cash_log()
        key = "deliveryTime" if request.url.path == DELIVERY_PATH else "transactionTime"
        rows = [candidate] if window[0] <= int(candidate[key]) <= window[1] else []
        return response(rows)

    async with adapter(handle) as instance:
        assert len(await fetch(instance, start_time=start)) == 1
    for windows in calls.values():
        assert len(windows) == 3
        assert windows[0][0] == millis(start) and windows[-1][1] == millis(NOW)
        assert all(end - begin < 7 * 86_400_000 for begin, end in windows)
        assert all(
            left[1] + 1 == right[0] for left, right in zip(windows, windows[1:], strict=False)
        )


@pytest.mark.parametrize("path", [DELIVERY_PATH, LOG_PATH, CLOSED_PATH])
async def test_repeated_cursor_aborts_without_returning_partial_facts(path):
    def handle(request):
        if request.url.path == path:
            return response([], cursor="same")
        if request.url.path == DELIVERY_PATH:
            price = "99000" if path == CLOSED_PATH else "101000"
            return response([delivery(deliveryPrice=price, fee="0")])
        return response([])

    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="repeated"):
            await fetch(instance)


@pytest.mark.parametrize("path", [DELIVERY_PATH, LOG_PATH])
async def test_out_of_query_window_records_fail_closed(path):
    def handle(request):
        if request.url.path == DELIVERY_PATH:
            rows = (
                [delivery(deliveryTime=millis(NOW) + 1)] if path == DELIVERY_PATH else [delivery()]
            )
        else:
            rows = [cash_log(transactionTime=millis(NOW) + 1)]
        return response(rows)

    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="outside requested window"):
            await fetch(instance)


@pytest.mark.parametrize("category", [None, "spot"])
async def test_delivery_requires_option_response_category(category):
    async with adapter(lambda _: response([delivery()], category=category)) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


@pytest.mark.parametrize("body", [{}, {"list": None}, {"list": ["bad"]}])
async def test_malformed_response_cannot_be_treated_as_no_delivery(body):
    async with adapter(
        lambda _: httpx.Response(200, json={"retCode": 0, "result": body})
    ) as instance:
        with pytest.raises(BybitRestError):
            await fetch(instance)


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "spot"},
        {"options_type": None},
        {"strike_price": None},
        {"strike_price": D("NaN")},
        {"strike_price": D("99999")},
        {"settle_coin": None},
        {"settle_coin": "BTC"},
        {"settle_coin": "USDC"},
        {"base_coin": "ETH"},
        {"quote_coin": "USDC"},
        {"delivery_time": None},
        {"delivery_time": EXPIRY.replace(tzinfo=None)},
        {"delivery_time": EXPIRY + dt.timedelta(days=1)},
        {"symbol": "BTC-2NOV26-100000-C-USDT"},
        {"symbol": "BTC-bad-date-100000-C-USDT"},
        {"symbol": "BTC-1NOV26-100000-P-USDT"},
    ],
)
async def test_unverified_spec_rejected_before_http(changes):
    def no_http(_):
        pytest.fail("invalid spec must not send HTTP")

    async with adapter(no_http) as instance:
        with pytest.raises(InsufficientCapability):
            await fetch(instance, spec(**changes))


@pytest.mark.parametrize(
    "times,exception",
    [
        ({"start_time": EXPIRY.replace(tzinfo=None)}, ValueError),
        ({"end_time": NOW + dt.timedelta(minutes=1)}, ValueError),
        ({"start_time": NOW + dt.timedelta(days=1)}, ValueError),
        ({"start_time": NOW - dt.timedelta(days=731)}, InsufficientCapability),
        ({"start_time": None}, ValueError),
        ({"end_time": None}, ValueError),
    ],
)
async def test_invalid_or_out_of_retention_windows_rejected_before_http(times, exception):
    def no_http(_):
        pytest.fail("invalid interval must not send HTTP")

    async with adapter(no_http) as instance:
        with pytest.raises(exception):
            await fetch(instance, **times)


async def test_zero_without_log_outside_closed_position_retention_fails_closed():
    expiry = EXPIRY - dt.timedelta(days=181)
    instrument = spec(symbol=f"BTC-{expiry:%-d%b%y}-100000-C-USDT".upper(), delivery_time=expiry)
    handle, calls = handler_for(
        [
            delivery(
                symbol=instrument.symbol,
                deliveryTime=millis(expiry),
                deliveryPrice="99000",
                fee="0",
            )
        ]
    )
    async with adapter(handle) as instance:
        with pytest.raises(InsufficientCapability, match="six-month"):
            await fetch(
                instance, instrument, start_time=expiry, end_time=expiry + dt.timedelta(days=1)
            )
    assert all(request.url.path != CLOSED_PATH for request in calls)


async def test_expiry_timestamp_tolerance_accepts_exact_sixty_seconds():
    handle, _ = handler_for(
        [delivery(deliveryTime=millis(EXPIRY) + 60_000)],
        [cash_log(transactionTime=millis(EXPIRY) + 60_000)],
    )
    async with adapter(handle) as instance:
        (fact,) = await fetch(instance)
    assert fact.occurred_at == EXPIRY + dt.timedelta(seconds=60)


@pytest.mark.parametrize("exec_type", ["Settlement", "Settle", "Delivery", "unknown"])
def test_trade_execution_parser_remains_fail_closed_for_non_trade_types(exec_type):
    with pytest.raises(InsufficientCapability, match="separate settlement accounting"):
        _parse_execution(
            "option",
            {"execTime": str(millis(EXPIRY)), "side": "Buy", "execType": exec_type},
        )
