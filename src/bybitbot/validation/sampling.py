"""Durable read-only sampling. Slot identity is separate from order scheduling."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import select, text

from bybitbot.clock import now_utc
from bybitbot.marketdata import completed_closes
from bybitbot.validation import (
    DAY_MS,
    HOUR_MS,
    SignalPolicy,
    SpreadLeg,
    SpreadProtocol,
    choose_candidate,
    detect_gap,
    evaluate_signal,
    remaining_risk_budget,
)
from bybitbot.validation.models import (
    ValidationLedger,
    ValidationObservation,
    ValidationPosition,
    ValidationStudy,
)


def json_value(value):
    return json.loads(
        json.dumps(
            value,
            default=lambda obj: obj.isoformat() if isinstance(obj, dt.datetime) else str(obj),
            sort_keys=True,
            allow_nan=False,
        )
    )


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class ValidationStore:
    def __init__(self, sessionmaker, environment: str, account_uid: str):
        self.sessionmaker, self.environment, self.account_uid = (
            sessionmaker,
            environment,
            account_uid,
        )

    async def start(
        self,
        *,
        run_id: str,
        bases,
        policy: str,
        direction: str,
        protocol: SpreadProtocol,
        started_ms: int,
    ) -> ValidationStudy:
        if self.environment != "paper":
            raise ValueError("验证运行只允许 paper 环境")
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", run_id
        ):
            raise ValueError("非法运行编号")
        bases = sorted(set(bases))
        if not bases or not set(bases) <= {"BTC", "ETH"}:
            raise ValueError("验证标的只允许 BTC/ETH")
        policy = SignalPolicy(policy).value
        if (policy, direction) not in {("strong_up", "Call"), ("weak_down", "Put")}:
            raise ValueError("信号与价差方向不符")
        if type(started_ms) is not int or started_ms < 0:
            raise ValueError("起点必须为非负整数毫秒")
        values = {
            "environment": self.environment,
            "account_uid": self.account_uid,
            "run_id": run_id,
            "bases": bases,
            "policy": policy,
            "direction": direction,
            "protocol": json_value(asdict(protocol)),
            "started_ms": started_ms,
            "end_ms": started_ms + protocol.study_days * DAY_MS,
        }
        async with self.sessionmaker() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{self.environment}:{self.account_uid}:validation-start"},
            )
            existing = await session.scalar(
                select(ValidationStudy).where(
                    ValidationStudy.environment == self.environment,
                    ValidationStudy.account_uid == self.account_uid,
                    ValidationStudy.run_id == run_id,
                )
            )
            if existing:
                if any(getattr(existing, key) != value for key, value in values.items()):
                    raise ValueError("运行编号已绑定另一份协议或时间窗口")
                return existing
            study = ValidationStudy(**values, next_due_ms=started_ms)
            session.add(study)
            await session.flush()
            session.add_all(ValidationPosition(study_id=study.id, base=base) for base in bases)
            session.add(
                ValidationLedger(
                    study_id=study.id,
                    dedupe_key="started",
                    kind="started",
                    occurred_ms=started_ms,
                    payload=values,
                )
            )
            await session.commit()
            return study

    async def active(self):
        if self.environment != "paper":
            return []
        async with self.sessionmaker() as session:
            return list(
                await session.scalars(
                    select(ValidationStudy)
                    .where(
                        ValidationStudy.environment == self.environment,
                        ValidationStudy.account_uid == self.account_uid,
                        ValidationStudy.status == "running",
                    )
                    .order_by(ValidationStudy.id)
                )
            )

    async def locked(self, session, study_id):
        study = await session.scalar(
            select(ValidationStudy)
            .where(
                ValidationStudy.id == study_id,
                ValidationStudy.environment == self.environment,
                ValidationStudy.account_uid == self.account_uid,
            )
            .with_for_update()
        )
        if study is None:
            raise ValueError("验证运行不属于当前账户/环境")
        return study

    async def reserve(self, study_id: int, base: str, amount: Decimal) -> bool:
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            raise ValueError("风险预留必须为有限正数")
        async with self.sessionmaker() as session:
            study = await self.locked(session, study_id)
            positions = list(
                await session.scalars(
                    select(ValidationPosition).where(ValidationPosition.study_id == study_id)
                )
            )
            position = next((p for p in positions if p.base == base), None)
            if study.status != "running" or position is None or position.phase != "waiting":
                return False
            if position.reserved_risk:
                return position.reserved_risk == amount
            available = remaining_risk_budget(
                {p.base: p.reserved_risk for p in positions},
                SpreadProtocol.from_mapping(study.protocol),
            )
            if amount > available:
                return False
            position.reserved_risk = amount
            await session.commit()
            return True

    async def record(self, study_id, *, slot_ms, observed_ms, payload, error="") -> bool:
        """Commit cursor, observation and gaps together; a failed sample isn't a success."""
        async with self.sessionmaker() as session:
            study = await self.locked(session, study_id)
            if study.status != "running" or study.next_due_ms != slot_ms or observed_ms < slot_ms:
                return False
            protocol = SpreadProtocol.from_mapping(study.protocol)
            snapshot_hash = digest(payload)
            session.add(
                ValidationObservation(
                    study_id=study_id,
                    slot_ms=slot_ms,
                    observed_ms=observed_ms,
                    status="error" if error else "ok",
                    snapshot_hash=snapshot_hash,
                    payload=json_value(payload),
                )
            )
            if not error and study.last_observed_ms is not None:
                for base in study.bases:
                    gap = detect_gap(
                        base=base,
                        previous_ms=study.last_observed_ms,
                        observed_ms=observed_ms,
                        max_gap_seconds=protocol.max_observation_gap_seconds,
                    )
                    if gap:
                        session.add(
                            ValidationLedger(
                                study_id=study_id,
                                dedupe_key=f"gap:{slot_ms}:{base}",
                                kind="observation_gap",
                                occurred_ms=observed_ms,
                                payload=json_value(asdict(gap)),
                            )
                        )
            if not error:
                study.last_observed_ms = observed_ms
            study.heartbeat_ms, study.last_error = observed_ms, error
            period = protocol.sampling_seconds * 1000
            study.next_due_ms = (
                study.started_ms + ((observed_ms - study.started_ms) // period + 1) * period
            )
            session.add(
                ValidationLedger(
                    study_id=study_id,
                    dedupe_key=f"sample:{slot_ms}",
                    kind="sample_error" if error else "sample",
                    occurred_ms=observed_ms,
                    payload={"slot_ms": slot_ms, "snapshot_hash": snapshot_hash, "error": error},
                )
            )
            await session.commit()
            return True


async def collect_sample(market, study: ValidationStudy, protocol: SpreadProtocol, now_ms: int):
    """Only public reads. One common first-server-time cutoff for both candle intervals."""
    cutoff = int((await market.get_server_time()).timestamp() * 1000)
    _fresh(cutoff, max(now_ms, int(now_utc().timestamp() * 1000)), protocol)
    specs = await market.get_instruments("option")
    payload = {"cutoff_ms": cutoff, "bases": {}}
    for base in study.bases:
        hours = await market.get_klines(f"{base}USDT", "60", end_ms=cutoff, limit=200)
        days = await market.get_klines(f"{base}USDT", "D", end_ms=cutoff, limit=100)
        signal = evaluate_signal(
            hourly=completed_closes(hours, cutoff, HOUR_MS, 25),
            daily=completed_closes(days, cutoff, DAY_MS, 51 if study.policy == "strong_up" else 50),
            policy=study.policy,
        )
        tickers = {q.symbol: q for q in await market.get_tickers("option", base_coin=base)}
        legs = []
        for spec in specs:
            if spec.base_coin != base or spec.symbol not in tickers:
                continue
            ticker = tickers[spec.symbol]
            if ticker.source_ts is None or spec.delivery_time is None:
                continue
            source = int(ticker.source_ts.timestamp() * 1000)
            _fresh(source, max(now_ms, int(now_utc().timestamp() * 1000)), protocol)
            filters = spec.raw.get("priceFilter", {})
            try:
                leg = SpreadLeg.from_mapping(
                    {
                        "symbol": spec.symbol,
                        "base": base,
                        "status": spec.status,
                        "option_type": spec.options_type,
                        "expiry_ms": int(spec.delivery_time.timestamp() * 1000),
                        "strike": spec.strike_price,
                        "index": ticker.index_price,
                        "bid": ticker.bid_price,
                        "ask": ticker.ask_price,
                        "bidSize": ticker.bid_size,
                        "askSize": ticker.ask_size,
                        "tick": spec.tick_size,
                        "step": spec.qty_step,
                        "minQty": spec.min_order_qty,
                        "maxQty": spec.max_order_qty,
                        "minPrice": filters.get("minPrice", spec.tick_size),
                        "maxPrice": filters.get("maxPrice"),
                        "delta": ticker.delta,
                        "source_ms": source,
                        "quote_coin": spec.quote_coin,
                        "settle_coin": spec.settle_coin,
                    }
                )
            except ValueError:
                continue
            legs.append(leg)
        candidate = (
            choose_candidate(
                base=base,
                legs=legs,
                now_ms=cutoff,
                available=protocol.aggregate_risk_budget_usdt,
                protocol=protocol,
                direction=study.direction,
            )
            if signal.eligible
            else None
        )
        payload["bases"][base] = json_value(
            {
                "signal": asdict(signal),
                "hourly": hours,
                "daily": days,
                "legs": [asdict(leg) for leg in legs],
                "candidate": asdict(candidate) if candidate else None,
            }
        )
    _fresh(cutoff, max(now_ms, int(now_utc().timestamp() * 1000)), protocol)
    return payload


def _fresh(source_ms, now_ms, protocol):
    if not 0 <= now_ms - source_ms <= protocol.max_response_age_seconds * 1000:
        raise ValueError("行情时间过期或晚于观察时刻")


class ValidationSampler:
    def __init__(self, store: ValidationStore, market):
        self.store, self.market = store, market

    async def tick(self, *, now_ms=None):
        now_ms = int(now_utc().timestamp() * 1000) if now_ms is None else now_ms
        for study in await self.store.active():
            if now_ms < study.next_due_ms or now_ms >= study.end_ms:
                continue
            protocol = SpreadProtocol.from_mapping(study.protocol)
            error = ""
            try:
                payload = await collect_sample(self.market, study, protocol, now_ms)
            except Exception as exc:
                # Don't persist exception text: adapters may include URLs or connection details.
                error = f"sample_failed:{type(exc).__name__}"
                payload = {"error": error}
            await self.store.record(
                study.id,
                slot_ms=study.next_due_ms,
                observed_ms=now_ms,
                payload=payload,
                error=error,
            )
