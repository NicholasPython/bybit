"""Deterministic, collateralised linear paper trading alongside spot/options.

Linear fills move only realised PnL and fees through the wallet. Positions and
all non-reduce-only resting orders reserve synthetic 1x collateral, without
cross-symbol hedging or credit for unrealised gains. Losses reduce free cash.
This is deliberately conservative, not Bybit's margin engine: liquidation,
funding, leverage, portfolio offsets and queue priority are not modelled.

Like PaperAdapter, only a subsequent valid book can fill an order. IOC uses
that first book and cancels its remainder. PostOnly requires a fresh book at
submission and rejects a crossing price; subsequent fills use displayed depth.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

from bybitbot.adapters.base import (
    AccountView,
    ExecutionView,
    InstrumentSpec,
    OrderAck,
    OrderBook,
    OrderRejected,
    OrderRequest,
    OrderUncertain,
    OrderView,
    PositionView,
    Ticker,
)
from bybitbot.adapters.paper import ACTIVE_STATUSES, FeeModelMismatch, PaperAdapter, PaperOrder
from bybitbot.clock import now_utc
from bybitbot.fees import FeeModel
from bybitbot.money import dec
from bybitbot.settings.models import OrderSide, OrderType, TimeInForce

ZERO = Decimal("0")
LINEAR_TAKER_RATE = Decimal("0.00055")
LINEAR_FEE_VERSION = "linear-paper-fee-v1"
# Informational only; this never triggers simulated liquidation.
SYNTHETIC_MAINTENANCE_RATE = Decimal("0.005")
STABLE_COINS = frozenset({"USDT", "USDC", "USD"})


class LinearPaperAdapter(PaperAdapter):
    """PaperAdapter with one-way, quote-settled linear positions (positionIdx=0).

    The parent constructor contract is preserved. Fee changes require an
    explicit checkpoint migration, just as they do for the parent's FeeModel.
    All linear maker/taker fills use the same conservative linear fee rate.
    """

    def __init__(
        self,
        *,
        initial_balances: dict[str, Decimal] | None = None,
        fee_model: FeeModel | None = None,
        account_uid: str = "paper-account",
        account_mode: str = "unified",
        clock: Callable[[], dt.datetime] = now_utc,
        linear_taker_rate: Decimal = LINEAR_TAKER_RATE,
        linear_fee_version: str = LINEAR_FEE_VERSION,
    ) -> None:
        super().__init__(
            initial_balances=initial_balances,
            fee_model=fee_model,
            account_uid=account_uid,
            account_mode=account_mode,
            clock=clock,
        )
        self.linear_taker_rate = dec(linear_taker_rate)
        if not self.linear_taker_rate.is_finite() or self.linear_taker_rate < 0:
            raise ValueError("linear_taker_rate must be finite and non-negative")
        if not linear_fee_version:
            raise ValueError("linear fees require a version")
        self.linear_fee_version = linear_fee_version
        # Reuse parent quote validation and shared depth pools, but isolate the
        # symbol namespace: spot BTCUSDT and linear BTCUSDT must not overwrite.
        self._linear_market = PaperAdapter(clock=clock)

    def set_instrument(self, spec: InstrumentSpec) -> None:
        if spec.category == "linear":
            if not spec.settle_coin or spec.settle_coin != spec.quote_coin:
                raise ValueError("linear paper requires quote-settled instruments")
            if spec.settle_coin not in STABLE_COINS:
                raise ValueError("linear paper supports USD stablecoin settlement only")
            self._linear_market.set_instrument(spec)
        else:
            super().set_instrument(spec)

    def set_ticker(self, ticker: Ticker) -> None:
        if ticker.category == "linear":
            self._linear_market.set_ticker(ticker)
        else:
            super().set_ticker(ticker)

    def set_book(self, book: OrderBook) -> None:
        if book.category != "linear":
            super().set_book(book)
            return
        previous = self._linear_market._snapshot_seq
        self._linear_market.set_book(book)
        if self._linear_market._snapshot_seq != previous:
            self._match_linear_orders(book.symbol)

    async def get_instruments(self, category: str) -> list[InstrumentSpec]:
        if category == "linear":
            return await self._linear_market.get_instruments(category)
        return await super().get_instruments(category)

    async def get_ticker(self, category: str, symbol: str) -> Ticker:
        if category != "linear":
            return await super().get_ticker(category, symbol)
        market = self._linear_market
        ticker = market._tickers.get(symbol)
        book = market._books.get(symbol)
        if ticker is not None and not self._fresh(ticker.source_ts, ticker.received_at):
            ticker = None
        if book is not None and not self._fresh(book.source_ts, book.received_at):
            book = None
        result = ticker or Ticker(category="linear", symbol=symbol)
        if book is not None:
            # Never attach a new timestamp to an old mark. When combining a
            # fresh mark and fresh depth, expose the older of their timestamps.
            result = replace(
                result,
                bid_price=book.bid(),
                ask_price=book.ask(),
                bid_size=book.bids[0][1],
                ask_size=book.asks[0][1],
                source_ts=min(book.source_ts, ticker.source_ts) if ticker else book.source_ts,
                received_at=min(book.received_at, ticker.received_at)
                if ticker
                else book.received_at,
            )
        return replace(result, mark_price=self._mark(market, symbol))

    async def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> OrderBook:
        if category == "linear":
            return await self._linear_market.get_orderbook(category, symbol, limit)
        return await super().get_orderbook(category, symbol, limit)

    @contextmanager
    def _parent_orders(self) -> Iterator[None]:
        """Parent spot/option reservation/matching must not see linear orders.

        Parent create_order has no suspension points; this scope never yields
        control to another task while the parent operates on its own orders.
        """
        linear = {k: o for k, o in self._orders.items() if o.request.category == "linear"}
        self._orders = {k: o for k, o in self._orders.items() if k not in linear}
        try:
            yield
        finally:
            self._orders.update(linear)

    def _match_resting_orders(self, symbol: str) -> None:
        with self._parent_orders():
            super()._match_resting_orders(symbol)

    async def create_order(self, request: OrderRequest) -> OrderAck:
        if request.category != "linear":
            # Keep the parent's order semantics, but don't let spot/option spend
            # cash already securing a linear position or resting order.
            if request.order_link_id in self._orders:
                return await super().create_order(request)
            locked = self._linear_collateral()
            pnl, missing = self._pnl_by_coin()
            adjustments = {}
            for coin, balance in self._account.coins.items():
                reduction = locked.get(coin, ZERO) - min(pnl.get(coin, ZERO), ZERO)
                if coin in missing:
                    reduction = max(reduction, balance.available)
                adjustments[coin] = reduction
                self._account.coins[coin] = replace(
                    balance, available=balance.available - reduction
                )
            try:
                with self._parent_orders():
                    return await super().create_order(request)
            finally:
                for coin, reduction in adjustments.items():
                    balance = self._account.coins[coin]
                    self._account.coins[coin] = replace(
                        balance, available=balance.available + reduction
                    )

        existing = self._orders.get(request.order_link_id)
        if existing is not None:
            if existing.request != request:
                raise OrderUncertain("paper orderLinkId already identifies a different request")
            return OrderAck(existing.order_id, existing.order_link_id, existing.status)
        self._validate_linear(request)
        if self._consume_fault("network_error"):
            raise ConnectionError("injected paper network error")
        if self._consume_fault("reject"):
            raise OrderRejected("injected paper rejection", ret_code=110003, ret_msg="injected")
        order = self._register(request)
        if self._consume_fault("unknown_result") or self._consume_fault("after_send_timeout"):
            raise OrderUncertain(f"paper order {request.order_link_id} accepted; response lost")
        return OrderAck(order.order_id, order.order_link_id, order.status)

    def _validate_linear(self, request: OrderRequest) -> None:
        spec = self._linear_market._instruments.get(request.symbol)
        if (
            spec is None
            or not spec.is_tradable
            or request.order_type is not OrderType.LIMIT
            or request.side not in (OrderSide.BUY, OrderSide.SELL)
            or request.price is None
            or not request.price.is_finite()
            or request.price <= 0
            or not request.qty.is_finite()
            or request.qty <= 0
            or not request.order_link_id
            or request.position_idx not in (None, 0)
            or request.time_in_force
            not in (TimeInForce.GTC, TimeInForce.IOC, TimeInForce.POST_ONLY)
        ):
            raise OrderRejected("linear paper requires valid one-way Limit GTC/IOC/PostOnly orders")
        for value, step in ((request.price, spec.tick_size), (request.qty, spec.qty_step)):
            if step is not None and (not step.is_finite() or step <= 0 or value % step != 0):
                raise OrderRejected("linear paper price/quantity violates instrument step")
        if (
            (spec.min_order_qty is not None and request.qty < spec.min_order_qty)
            or (spec.max_order_qty is not None and request.qty > spec.max_order_qty)
            or (spec.min_order_amt is not None and request.price * request.qty < spec.min_order_amt)
            or (spec.max_order_amt is not None and request.price * request.qty > spec.max_order_amt)
        ):
            raise OrderRejected("linear paper order violates instrument bounds")
        closable = self._closable(request)
        if request.reduce_only and closable == 0:
            raise OrderRejected("reduceOnly requires an opposite linear position")
        if request.time_in_force is TimeInForce.POST_ONLY:
            book = self._linear_market._books.get(request.symbol)
            if book is None or not self._fresh(book.source_ts, book.received_at):
                raise OrderRejected("PostOnly requires a fresh linear order book")
            crossing = (
                request.price >= book.ask()
                if request.side is OrderSide.BUY
                else request.price <= book.bid()
            )
            if crossing:
                raise OrderRejected("PostOnly would take liquidity")
        # Safe reductions can release collateral even in a deficient account.
        if request.reduce_only or request.qty <= closable:
            return
        coin = spec.settle_coin
        pnl, missing = self._pnl_by_coin()
        held = self._all_collateral().get(coin, ZERO)
        needed = self._order_margin(request, request.qty)
        available = self._account.balance(coin).available + min(pnl.get(coin, ZERO), ZERO) - held
        if coin in missing or needed > available:
            raise OrderRejected("linear paper has insufficient unreserved 1x collateral")

    def _closable(self, request: OrderRequest) -> Decimal:
        position = self._positions.get(("linear", request.symbol, 0))
        if position is not None and position.side != request.side.value:
            return position.size
        return ZERO

    def _fresh(self, *stamps: dt.datetime | None) -> bool:
        return all(
            stamp is not None and -5 <= (self._clock() - stamp).total_seconds() <= 15
            for stamp in stamps
        )

    def _valuation(self, market: PaperAdapter, symbol: str) -> tuple[Decimal | None, str]:
        ticker = market._tickers.get(symbol)
        if ticker is not None and self._fresh(ticker.source_ts, ticker.received_at):
            for price, source in (
                (ticker.mark_price, "ticker_mark"),
                (ticker.last_price, "ticker_last_estimate"),
            ):
                if price is not None and price.is_finite() and price > 0:
                    return price, source
            if (
                ticker.bid_price is not None
                and ticker.ask_price is not None
                and ticker.bid_price.is_finite()
                and ticker.ask_price.is_finite()
                and ZERO < ticker.bid_price <= ticker.ask_price
            ):
                return (ticker.bid_price + ticker.ask_price) / 2, "ticker_mid_estimate"
        book = market._books.get(symbol)
        if book is not None and self._fresh(book.source_ts, book.received_at):
            return (book.bid() + book.ask()) / 2, "book_mid_estimate"
        return None, "missing"

    def _mark(self, market: PaperAdapter, symbol: str) -> Decimal | None:
        return self._valuation(market, symbol)[0]

    def _position_margin(self, position: PositionView) -> Decimal:
        if position.size == 0:
            return ZERO
        mark = self._mark(self._linear_market, position.symbol)
        return position.size * max(position.avg_price or ZERO, mark or ZERO)

    def _order_margin(self, request: OrderRequest, qty: Decimal) -> Decimal:
        if request.reduce_only:
            return ZERO
        mark = self._mark(self._linear_market, request.symbol)
        return qty * max(request.price, mark or ZERO) * (1 + self.linear_taker_rate)

    def _linear_collateral(self, exclude: str | None = None) -> dict[str, Decimal]:
        held: dict[str, Decimal] = {}
        for position in self._positions.values():
            if position.category == "linear" and position.size > 0:
                coin = position.settle_coin
                held[coin] = held.get(coin, ZERO) + self._position_margin(position)
        for order in self._orders.values():
            if (
                order.request.category == "linear"
                and order.status in ACTIVE_STATUSES
                and order.order_link_id != exclude
            ):
                coin = self._linear_market._instruments[order.request.symbol].settle_coin
                held[coin] = held.get(coin, ZERO) + self._order_margin(
                    order.request, order.leaves_qty
                )
        return held

    def _all_collateral(self, exclude: str | None = None) -> dict[str, Decimal]:
        held = self._linear_collateral(exclude)
        for order in self._orders.values():
            request = order.request
            if (
                request.category != "linear"
                and order.status in ACTIVE_STATUSES
                and request.side is OrderSide.BUY
            ):
                coin = super()._cash_coin(request)
                cost = (
                    order.leaves_qty
                    * request.price
                    * (1 + super()._buy_cost_ratio(request, request.price))
                )
                held[coin] = held.get(coin, ZERO) + cost
        return held

    def _pnl(self, position: PositionView) -> Decimal | None:
        if position.size == 0:
            return ZERO
        mark = self._mark(self._linear_market, position.symbol)
        if mark is None:
            return None
        direction = 1 if position.side == "Buy" else -1
        return (mark - position.avg_price) * position.size * direction

    def _pnl_by_coin(self) -> tuple[dict[str, Decimal], set[str]]:
        pnl: dict[str, Decimal] = {}
        missing: set[str] = set()
        for position in self._positions.values():
            if position.category != "linear" or position.size == 0:
                continue
            value = self._pnl(position)
            coin = position.settle_coin
            if value is None:
                missing.add(coin)
            else:
                pnl[coin] = pnl.get(coin, ZERO) + value
        return pnl, missing

    def _project_position(
        self, request: OrderRequest, price: Decimal, qty: Decimal
    ) -> tuple[PositionView, Decimal]:
        current = self._positions.get(("linear", request.symbol, 0))
        size = current.size if current else ZERO
        avg = current.avg_price if current and current.avg_price is not None else ZERO
        realised = ZERO
        if size == 0 or current.side == request.side.value:
            new_size = size + qty
            new_avg = (size * avg + qty * price) / new_size
            new_side = request.side.value
        else:
            closed = min(size, qty)
            realised = (price - avg) * closed * (1 if current.side == "Buy" else -1)
            new_size = abs(size - qty)
            new_side = current.side if qty < size else request.side.value
            new_avg = avg if qty < size else price
            if new_size == 0:
                new_side, new_avg = None, ZERO
        return PositionView(
            category="linear",
            symbol=request.symbol,
            position_idx=0,
            size=new_size,
            side=new_side,
            avg_price=new_avg,
            realised_pnl=(current.realised_pnl or ZERO) + realised if current else realised,
            settle_coin=self._linear_market._instruments[request.symbol].settle_coin,
            raw={"simulated": True, "margin_model": "synthetic-1x"},
        ), realised

    def _can_fill(self, order: PaperOrder, price: Decimal, qty: Decimal) -> bool:
        request = order.request
        if qty <= self._closable(request):
            return True
        projected, realised = self._project_position(request, price, qty)
        coin = projected.settle_coin
        pnl, missing = self._pnl_by_coin()
        if coin in missing:
            return False
        old = self._positions.get(("linear", request.symbol, 0))
        held = self._all_collateral(exclude=order.order_link_id).get(coin, ZERO)
        if old is not None:
            held -= self._position_margin(old)
        held += self._position_margin(projected)
        held += self._order_margin(request, order.leaves_qty - qty)
        projected_pnl = self._pnl(projected)
        if projected_pnl is None:
            return False
        unrealised = pnl.get(coin, ZERO) - (self._pnl(old) if old else ZERO) + projected_pnl
        cash = (
            self._account.balance(coin).available + realised - price * qty * self.linear_taker_rate
        )
        return cash + min(unrealised, ZERO) >= held

    def _match_linear_orders(self, symbol: str) -> None:
        market = self._linear_market
        book, pool = market._books[symbol], market._depth_pool[symbol]
        candidates = sorted(
            (
                order
                for order in self._orders.values()
                if order.request.category == "linear"
                and order.request.symbol == symbol
                and order.status in ACTIVE_STATUSES
                and book.source_ts > order.created_at
            ),
            key=lambda order: (order.created_at, order.order_link_id),
        )
        for order in candidates:
            request = order.request
            wanted = order.leaves_qty
            if request.reduce_only:
                wanted = min(wanted, self._closable(request))
                if wanted == 0:
                    order.status = "Cancelled"
                    continue
            if self._consume_fault("partial"):
                wanted *= self.partial_ratio
            buying = request.side is OrderSide.BUY
            levels = pool.asks if buying else pool.bids
            consumed = pool._ask_consumed if buying else pool._bid_consumed
            for index, (price, size) in enumerate(levels):
                if wanted <= 0 or (price > request.price if buying else price < request.price):
                    break
                qty = min(wanted, size - consumed[index])
                if qty <= 0:
                    continue
                if not self._can_fill(order, price, qty):
                    # Funds may have fallen since acceptance. Cancel rather than
                    # silently over-leveraging or consuming unfilled book depth.
                    order.status = "Cancelled"
                    break
                consumed[index] += qty
                self._apply_fill(order, price, qty)
                wanted -= qty
            if order.status in ACTIVE_STATUSES and (
                request.time_in_force is TimeInForce.IOC
                or (request.reduce_only and self._closable(request) == 0)
            ):
                order.status = "Cancelled"

    def _apply_fill(self, order: PaperOrder, price: Decimal, qty: Decimal) -> None:
        request = order.request
        if request.category != "linear":
            super()._apply_fill(order, price, qty)
            return
        # Defensive clipping also protects direct fault/test fill injection.
        qty = min(qty, order.leaves_qty)
        if request.reduce_only:
            qty = min(qty, self._closable(request))
        if qty <= 0:
            return
        position, realised = self._project_position(request, price, qty)
        fee = price * qty * self.linear_taker_rate
        self._account.credit(position.settle_coin, realised - fee)
        self._positions[("linear", request.symbol, 0)] = position
        previous = order.avg_price * order.qty_filled
        order.qty_filled += qty
        order.avg_price = (previous + price * qty) / order.qty_filled
        order.status = "Filled" if order.leaves_qty == 0 else "PartiallyFilled"
        self._exec_seq += 1
        execution = ExecutionView(
            exec_id=f"paper-exec-{self._exec_seq:08d}",
            order_id=order.order_id,
            order_link_id=order.order_link_id,
            category="linear",
            symbol=request.symbol,
            side=request.side.value,
            exec_price=price,
            exec_qty=qty,
            exec_time=self._clock(),
            exec_fee=fee,
            exec_fee_asset=position.settle_coin,
            is_maker=request.time_in_force is TimeInForce.POST_ONLY,
            raw={"simulated": True, "fee_version": self.linear_fee_version},
        )
        order.executions.append(execution)
        self._executions.append(execution)

    async def get_positions(self, category: str) -> list[PositionView]:
        positions = await super().get_positions(category)
        if category != "linear":
            return positions
        return [
            replace(
                position,
                mark_price=self._mark(self._linear_market, position.symbol),
                unrealised_pnl=self._pnl(position),
            )
            for position in positions
        ]

    def _coin_usd_price(self, coin: str) -> Decimal | None:
        if coin in STABLE_COINS:
            return Decimal("1")
        for spec in self._instruments.values():
            if (
                spec.category == "spot"
                and spec.base_coin == coin
                and spec.quote_coin in STABLE_COINS
            ):
                mark = self._mark(self, spec.symbol)
                if mark is not None:
                    return mark
        return None

    async def get_account(self) -> AccountView:
        base = await super().get_account()
        pnl, missing = self._pnl_by_coin()
        held = self._all_collateral()
        coins = {}
        total = ZERO
        complete = not missing
        for coin, balance in base.coins.items():
            unrealised = pnl.get(coin, ZERO)
            locked = held.get(coin, ZERO)
            available = max(ZERO, balance.available + min(unrealised, ZERO) - locked)
            # CoinBalance.equity cannot express None. Its cash-only fallback is
            # explicit in raw; aggregate equity remains unknown until requoted.
            value = balance.equity + unrealised
            usd_price = self._coin_usd_price(coin)
            usd_value = value * usd_price if usd_price is not None and coin not in missing else None
            if value != 0 and usd_value is None:
                complete = False
            if usd_value is not None:
                total += usd_value
            coins[coin] = replace(
                balance,
                equity=value,
                available=ZERO if coin in missing else available,
                locked=balance.locked + locked,
                usd_value=usd_value,
            )
        # Option premiums have already moved through cash; mark remaining
        # option assets/liabilities to obtain account equity when quoted.
        for position in self._positions.values():
            if position.category == "option" and position.size != 0:
                mark = self._mark(self, position.symbol)
                conversion = self._coin_usd_price(position.settle_coin)
                if mark is None or conversion is None:
                    complete = False
                else:
                    total += (
                        mark * position.size * conversion * (1 if position.side == "Buy" else -1)
                    )
        notional = ZERO
        for position in self._positions.values():
            if position.category == "linear" and position.size > 0:
                mark = self._mark(self._linear_market, position.symbol)
                if mark is not None:
                    notional += mark * position.size
        total_equity = total if complete else None
        mm_rate = notional * SYNTHETIC_MAINTENANCE_RATE / total if complete and total > 0 else None
        raw = {
            **base.raw,
            "linear_fee_version": self.linear_fee_version,
            "linear_taker_rate": str(self.linear_taker_rate),
            "linear_mark_sources": {
                symbol: self._valuation(self._linear_market, symbol)[1]
                for symbol in self._linear_market._instruments
            },
            "account_mm_rate": str(mm_rate) if mm_rate is not None else None,
            "total_notional": str(notional) if not missing else None,
            "linear_unrealised_pnl": str(sum(pnl.values(), ZERO)) if not missing else None,
            "linear_initial_margin": str(sum(self._linear_collateral().values(), ZERO)),
            "linear_missing_quotes": sorted(
                p.symbol
                for p in self._positions.values()
                if p.category == "linear" and p.size > 0 and self._pnl(p) is None
            ),
            "equity_complete": complete,
            "margin_model": "synthetic-1x",
            "margin_estimate": (
                "Synthetic 1x collateral; account_mm_rate = 0.5% of gross marked linear "
                "notional / total equity; USD stablecoins assumed at par. Missing quotes "
                "make aggregate equity/PnL/MM unknown; affected coin equity is cash-only. "
                "No exchange liquidation, funding, leverage or portfolio margin is modelled."
            ),
        }
        return replace(
            base, coins=coins, total_equity=total_equity, exchange_equity=total_equity, raw=raw
        )

    def _to_view(self, order: PaperOrder) -> OrderView:
        view = super()._to_view(order)
        if order.request.category == "linear":
            return replace(view, position_idx=0)
        return view

    def export_state(self) -> dict[str, object]:
        state = super().export_state()
        market = self._linear_market.export_state()
        state["linear"] = {
            "version": 1,
            "fee_version": self.linear_fee_version,
            "taker_rate": str(self.linear_taker_rate),
            "margin_model": "synthetic-1x",
            "instruments": market["instruments"],
            "last_book_tokens": market["last_book_tokens"],
            "counters": market["counters"],
            "idempotency": {
                link: order.order_id
                for link, order in self._orders.items()
                if order.request.category == "linear"
            },
        }
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        metadata = state.get("linear")
        market = PaperAdapter(clock=self._clock)
        if metadata is not None:
            if metadata.get("version") != 1 or metadata.get("margin_model") != "synthetic-1x":
                raise ValueError("unsupported linear paper checkpoint version/margin model")
            if (
                metadata.get("fee_version") != self.linear_fee_version
                or dec(metadata["taker_rate"]) != self.linear_taker_rate
            ):
                raise FeeModelMismatch("linear paper checkpoint fee model mismatch")
            market_state = market.export_state()
            for key in ("instruments", "last_book_tokens", "counters"):
                market_state[key] = metadata[key]
            market.restore_state(market_state)
            expected = {
                row["request"]["order_link_id"]: row["order_id"]
                for row in state["orders"]
                if row["request"]["category"] == "linear"
            }
            if metadata.get("idempotency") != expected:
                raise ValueError("linear paper checkpoint idempotency map mismatch")
        elif any(row["request"]["category"] == "linear" for row in state["orders"]):
            raise ValueError("linear orders require versioned linear checkpoint metadata")
        super().restore_state(state)
        self._linear_market = market


__all__ = ["LINEAR_FEE_VERSION", "LINEAR_TAKER_RATE", "LinearPaperAdapter"]
