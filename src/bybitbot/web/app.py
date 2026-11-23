"""Private NAS console. Reads database facts and queues commands; never executes them."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import hmac
import importlib
import io
import json
import math
import os
import re
import secrets
import stat
import threading
import zipfile
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeSerializer
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from bybitbot.db.models import (
    AccountSnapshot,
    Alert,
    CashFlow,
    ControlCommand,
    EquityBaseline,
    Execution,
    Instrument,
    Inventory,
    Order,
    Position,
    QuoteSnapshot,
    ReconciliationRun,
    RiskDecision,
    RiskPause,
    RuntimeState,
    Strategy,
    StrategyConfigVersion,
    TradeIntent,
)
from bybitbot.db.session import Database
from bybitbot.ops.control import ControlCommandManager
from bybitbot.settings.loader import AppConfig
from bybitbot.settings.models import Environment
from bybitbot.settings.writer import (
    ReadOnlyConfig,
    RevisionConflict,
    ShadowConfigError,
    field_errors,
    read_shadow_config,
    write_spread,
)
from bybitbot.spread.shadow import ShadowEngine

SESSION_COOKIE = "bybitbot_session"
SESSION_SECONDS = 30 * 60
HEARTBEAT_SECONDS = 120
TABLE_LIMIT = 100
BODY_LIMIT = 16_384
HERE = Path(__file__).resolve().parent
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,63}$")]
LinkID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
PositiveID = Annotated[int, Field(strict=True, gt=0, le=9_223_372_036_854_775_807)]
Amount = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?$", max_length=39),
]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_RequestModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class PausePayload(_RequestModel):
    reason: Reason


class CancelOpenPayload(_RequestModel):
    strategy_id: PositiveID | None = None
    symbol: Symbol | None = None


class CancelPayload(_RequestModel):
    order_link_ids: list[LinkID] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_orders(self) -> CancelPayload:
        if len(set(self.order_link_ids)) != len(self.order_link_ids):
            raise ValueError("duplicate orders")
        return self


class ReducePayload(_RequestModel):
    strategy_id: PositiveID
    category: Literal["spot", "option"]
    symbol: Symbol
    qty: Amount
    price_floor: Amount
    reason: Reason

    @model_validator(mode="after")
    def positive_amounts(self) -> ReducePayload:
        if Decimal(self.qty) <= 0 or Decimal(self.price_floor) <= 0:
            raise ValueError("amount must be positive")
        return self


class ResumePayload(_RequestModel):
    pause_id: PositiveID
    reason: Reason


class TakeoverPayload(_RequestModel):
    evidence: Reason
    reason: Reason


ShadowDecimal = Annotated[
    str,
    StringConstraints(pattern=r"^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$", max_length=64),
]
BoundedInt = Annotated[int, Field(strict=True)]


def _int_range(lower: int, upper: int | None = None) -> Any:
    range_text = f"{lower}–{upper}" if upper is not None else f"≥{lower}"

    def check(value: int) -> int:
        if value < lower or (upper is not None and value > upper):
            raise ValueError(f"必须在 {range_text} 之间" if upper else f"必须 {range_text}")
        return value

    return Annotated[BoundedInt, AfterValidator(check)]


class ShadowAmountPayload(_RequestModel):
    value: ShadowDecimal
    currency: Literal["USDT"]


class ShadowPairPayload(_RequestModel):
    """Only editable research fields; execution controls stay on disk."""

    pair_key: str = Field(min_length=3, max_length=64)
    symbol_a: Symbol
    symbol_b: Symbol
    hedge_ratio: ShadowDecimal
    z_entry: ShadowDecimal
    z_exit: ShadowDecimal
    z_stop: ShadowDecimal
    max_notional: ShadowAmountPayload
    max_unhedged_notional: ShadowAmountPayload


class ShadowAutoPayload(_RequestModel):
    window: _int_range(5, 500)
    sample_interval_seconds: _int_range(2, 3600)
    fee_rate: ShadowDecimal
    slippage_bps: ShadowDecimal
    funding_buffer: ShadowDecimal
    max_signal_age_ms: _int_range(1000)
    stop_cooldown_seconds: _int_range(60)
    target_notional: ShadowDecimal


class ShadowSettingsPayload(_RequestModel):
    enabled: bool = Field(strict=True)
    public_market_url: Literal["https://api.bybit.com"]
    observation_days: _int_range(1, 30)
    min_depth_notional: ShadowDecimal
    impact_bps_per_depth: ShadowDecimal
    min_sharpe: ShadowDecimal
    max_drawdown: ShadowDecimal
    max_signal_age_ms: _int_range(1000)


class ShadowConfigPayload(_RequestModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    shadow: ShadowSettingsPayload
    auto: ShadowAutoPayload
    pairs: list[ShadowPairPayload] = Field(max_length=20)


class ValidationStartPayload(_RequestModel):
    """验证运行只接受 paper 只读研究参数，不含任何交易所请求字段。"""

    run_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    bases: list[Annotated[str, StringConstraints(pattern=r"^(BTC|ETH)$")]] = Field(
        min_length=1, max_length=2
    )
    policy: Literal["strong_up", "weak_down"]
    direction: Literal["Call", "Put"]
    protocol: dict[str, Any] = Field(min_length=1, max_length=100)
    other_run_ids: list[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    ] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_scope(self) -> ValidationStartPayload:
        if len(set(self.bases)) != len(self.bases):
            raise ValueError("duplicate bases")
        if len(set(self.other_run_ids)) != len(self.other_run_ids):
            raise ValueError("duplicate opposing runs")
        if (self.policy, self.direction) not in {
            ("strong_up", "Call"),
            ("weak_down", "Put"),
        }:
            raise ValueError("policy and direction do not match")
        for key, value in self.protocol.items():
            if not isinstance(key, str) or len(key) > 64 or _SENSITIVE.search(key):
                raise ValueError("invalid protocol field")
            if value is None or isinstance(value, (dict, list)) or len(str(value)) > 200:
                raise ValueError("invalid protocol value")
            if isinstance(value, str) and _SENSITIVE.search(value):
                raise ValueError("invalid protocol value")
        return self


class ValidationRunPayload(_RequestModel):
    run_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class CashFlowPayload(_RequestModel):
    """登记外部充值/提现/划转。金额填正数，方向由类型决定。"""

    asset: Annotated[
        str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z0-9]{2,16}$")
    ]
    amount: Amount
    kind: Literal["DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"]
    reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    occurred_at: dt.datetime | None = None
    reason: Reason

    @model_validator(mode="after")
    def positive_amount(self) -> CashFlowPayload:
        if Decimal(self.amount) <= 0:
            raise ValueError("amount must be positive")
        return self


_PAYLOAD_MODELS = {
    "PAUSE_NEW_RISK": PausePayload,
    "CANCEL_BOT_OPEN_ORDERS": CancelOpenPayload,
    "CANCEL_ORDER": CancelPayload,
    "REDUCE": ReducePayload,
    "RESUME": ResumePayload,
    "CONFIRM_TAKEOVER": TakeoverPayload,
    "INITIALIZE_BASELINE": PausePayload,
    "REGISTER_EXTERNAL_CASH_FLOW": CashFlowPayload,
    "VALIDATION_START": ValidationStartPayload,
    "VALIDATION_STOP": ValidationRunPayload,
}


class CommandRequest(_RequestModel):
    """The web boundary accepts business scope only, never exchange request parameters."""

    command_id: UUID
    kind: Literal[
        "PAUSE_NEW_RISK",
        "CANCEL_BOT_OPEN_ORDERS",
        "CANCEL_ORDER",
        "REDUCE",
        "RESUME",
        "CONFIRM_TAKEOVER",
        "INITIALIZE_BASELINE",
        "REGISTER_EXTERNAL_CASH_FLOW",
        "VALIDATION_START",
        "VALIDATION_STOP",
    ]
    payload: dict[str, Any] = Field(max_length=8)
    confirmation: str | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def validate_scope(self) -> CommandRequest:
        if self.kind != "PAUSE_NEW_RISK" and self.confirmation != "确认":
            raise ValueError("explicit confirmation required")
        validated = _PAYLOAD_MODELS[self.kind].model_validate(self.payload)
        self.payload = validated.model_dump(mode="json", exclude_none=True)
        return self


@dataclass(frozen=True, repr=False)
class _Admin:
    username: str
    salt: bytes
    digest: bytes
    session_secret: str


def _load_admin(path: Path | str) -> _Admin:
    """Open once, reject symlinks and broad permissions, and keep errors secret-free."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in {0, os.geteuid()}
                or info.st_size > 8192
            ):
                raise ValueError("invalid auth permissions")
            data = json.loads(handle.read(8193))
        if not isinstance(data, dict) or set(data) != {
            "username",
            "password_hash",
            "session_secret",
        }:
            raise ValueError("invalid auth schema")
        username = data["username"]
        secret = data["session_secret"]
        if (
            not isinstance(username, str)
            or not 1 <= len(username) <= 128
            or any(ord(char) < 32 for char in username)
            or not isinstance(secret, str)
            or not 32 <= len(secret) <= 512
        ):
            raise ValueError("invalid auth fields")
        match = re.fullmatch(
            r"pbkdf2_sha256\$600000\$([a-fA-F0-9]{32,128})\$([a-fA-F0-9]{64})",
            data["password_hash"],
        )
        if match is None:
            raise ValueError("invalid password hash")
        return _Admin(username, bytes.fromhex(match[1]), bytes.fromhex(match[2]), secret)
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("管理员认证文件无效；请使用 init-admin 创建权限为 0600 的文件。") from None


class _LoginLimits:
    """Bounded process-local attempt buckets; do not trust proxy-supplied IP headers."""

    def __init__(self) -> None:
        self.clients: OrderedDict[str, deque[float]] = OrderedDict()
        self.global_attempts: deque[float] = deque(maxlen=60)
        self.lock = threading.Lock()

    def allow(self, client: str, now: float) -> bool:
        with self.lock:
            cutoff = now - 300
            while self.global_attempts and self.global_attempts[0] <= cutoff:
                self.global_attempts.popleft()
            if len(self.global_attempts) >= 60:
                return False
            attempts = self.clients.pop(client, deque(maxlen=8))
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            self.clients[client] = attempts
            while len(self.clients) > 512:
                self.clients.popitem(last=False)
            if len(attempts) >= 8:
                return False
            attempts.append(now)
            self.global_attempts.append(now)
            return True


class _Sessions:
    """Signed sessions with absolute expiry and bounded, revocable server-side IDs."""

    def __init__(self, secret: str) -> None:
        self.signer = URLSafeSerializer(secret, salt="bybitbot-admin-v1")
        self.active: OrderedDict[str, int] = OrderedDict()
        self.lock = threading.Lock()

    def issue(self, username: str, now: int) -> tuple[str, dict[str, Any]]:
        session = {
            "username": username,
            "csrf_token": secrets.token_urlsafe(32),
            "sid": secrets.token_urlsafe(32),
            "issued_at": now,
            "expires_at": now + SESSION_SECONDS,
        }
        with self.lock:
            self.active = OrderedDict(
                (sid, expiry) for sid, expiry in self.active.items() if expiry > now
            )
            self.active[session["sid"]] = session["expires_at"]
            while len(self.active) > 1024:
                self.active.popitem(last=False)
        return self.signer.dumps(session), session

    def read(self, cookie: str | None, now: int) -> dict[str, Any] | None:
        if not cookie or len(cookie) > 4096:
            return None
        try:
            session = self.signer.loads(cookie)
            if (
                not isinstance(session, dict)
                or type(session.get("issued_at")) is not int
                or type(session.get("expires_at")) is not int
                or not session["issued_at"] <= now < session["expires_at"]
                or session["expires_at"] - session["issued_at"] != SESSION_SECONDS
                or not isinstance(session.get("csrf_token"), str)
                or not isinstance(session.get("sid"), str)
            ):
                return None
            with self.lock:
                if self.active.get(session["sid"]) != session["expires_at"]:
                    return None
            return session
        except (BadData, ValueError, TypeError, KeyError):
            return None

    def revoke(self, session: dict[str, Any]) -> None:
        with self.lock:
            self.active.pop(session["sid"], None)


def _utc(value: Any) -> dt.datetime | None:
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)
    return None


def _timestamp(value: Any) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


_SENSITIVE = re.compile(
    r"(?i)(?:api[_-]?key|api[_-]?secret|password|passwd|session[_-]?secret|"
    r"authorization|signature|credential|\bdsn\b|password_hash|"
    r"\b(?:params|headers|retMsg|retCode)\s*[=:])"
)
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")


def _text(value: Any) -> str:
    text = str(value)
    if _SENSITIVE.search(text):
        return "敏感诊断内容已隐藏；请查看服务器日志。"
    text = _URL.sub("[连接信息已隐藏]", text)
    return "".join(char for char in text if ord(char) >= 32 or char == "\n")[:2000]


def _number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number.adjusted() > 38 or number.adjusted() < -38:
            return None
        return format(number, "f")
    except (InvalidOperation, ValueError, TypeError):
        return None


def _value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, dt.datetime):
        return _timestamp(value)
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value)
    return None


def _row(row: Any, fields: str) -> dict[str, Any]:
    return {field: _value(getattr(row, field, None)) for field in fields.split()}


#: 策略参数白名单：只有这些键可以出现在页面上，其余一律丢弃。
_STRATEGY_PARAM_KEYS = frozenset(
    [
        "options_type",
        "min_premium",
        "max_premium",
        "min_open_interest",
        "max_fee_ratio",
        "min_days_to_expiry",
        "max_days_to_expiry",
        "delta_low",
        "delta_high",
        "strike_low_pct",
        "strike_high_pct",
        "max_price_deviation",
        "min_qty",
    ]
)
#: 退出规则白名单。
_EXIT_RULE_KEYS = frozenset(
    [
        "kind",
        "hold_seconds",
        "profit_threshold_pct",
        "stop_loss_pct",
        "max_reprice_attempts",
        "exit_validity_seconds",
        "seconds_before_expiry",
    ]
)


def _strategy_params(payload: Any) -> dict[str, str]:
    """生效版本的策略参数（白名单 + 脱敏）。

    版本行的 ``params`` 存的是整个策略配置，这里只取其中的 params 子字典，
    并逐项做数值/枚举清洗，避免把运维自由文本带到页面上。
    """
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("params")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _STRATEGY_PARAM_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            continue
        text = _number(value) if not isinstance(value, str) else _text(value)
        if text is None or len(text) > 40:
            continue
        out[key] = text
    return out


def _exit_rules(value: Any) -> list[dict[str, Any]]:
    """生效版本的退出规则（白名单 + 脱敏）。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for rule in value[:20]:
        if not isinstance(rule, dict):
            continue
        clean: dict[str, Any] = {}
        for key in _EXIT_RULE_KEYS:
            item = rule.get(key)
            if item is None or isinstance(item, bool):
                continue
            text = _number(item) if not isinstance(item, str) else _text(item)
            if text is None or len(text) > 40:
                continue
            clean[key] = text
        if clean:
            out.append(clean)
    return out


_RESULT_FIELDS = frozenset(
    [
        "pause_id",
        "resumed",
        "paused",
        "scope",
        "scope_key",
        "locked",
        "resolved_by",
        "cancelled",
        "failed",
        "order_link_id",
        "detail",
        "error",
        "symbol",
        "qty",
        "operator",
        "intent_id",
        "signal_id",
        "status",
        "reconciliation_id",
        "takeover_confirmed",
        "strategy_id",
        "category",
        "price_floor",
        "terminal_confirmed",
        "pending",
        "baseline_created",
        "incomplete_day",
        "lease",
        "reconciled_at",
        "resolved_pause_id",
        "remaining_pauses",
        "reason",
        "run_id",
        "study_id",
        "unresolved_bases",
        "final_report_ready",
    ]
)


def _result(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {
            key: _result(item, depth + 1)
            for key, item in list(value.items())[:100]
            if key in _RESULT_FIELDS
        }
    if isinstance(value, list):
        return [_result(item, depth + 1) for item in value[:100]]
    return _value(value)


def _command(row: Any) -> dict[str, Any]:
    public = _row(row, "id command_id kind status requested_by created_at executed_at")
    try:
        public["payload"] = {
            key: (
                [_text(item) for item in value]
                if isinstance(value, list)
                else {str(name): _value(item) for name, item in value.items()}
                if isinstance(value, dict)
                else _value(value)
            )
            for key, value in _PAYLOAD_MODELS[row.kind]
            .model_validate(row.payload or {})
            .model_dump(mode="json", exclude_none=True)
            .items()
        }
    except (ValidationError, KeyError, TypeError):
        public["payload"] = {}
    public["result"] = (
        _result(row.result or {}) if row.status in {"EXECUTED", "FAILED", "REJECTED"} else {}
    )
    return public


def _heartbeat(runtime: Any, now: dt.datetime) -> dict[str, Any]:
    timestamp = _utc(runtime.updated_at) if runtime is not None else None
    age = (now - timestamp).total_seconds() if timestamp else None
    return {
        "at": _timestamp(timestamp),
        "age_seconds": round(max(0, age), 1) if age is not None else None,
        "fresh": age is not None and -5 <= age < HEARTBEAT_SECONDS,
    }


def _runtime_ops_row(value: Any) -> dict[str, Any] | None:
    """投影 runtime_state 里的运维报告；只保留已定义的非密字段。"""
    if not isinstance(value, dict):
        return None
    return {
        key: _value(value[key])
        for key in (
            "ran_at",
            "cutoff",
            "raw_market_data_deleted",
            "raw_market_data_remaining",
            "decision_snapshots_kept",
        )
        if key in value
    }


def _runtime_ops_list(value: Any) -> list[dict[str, Any]]:
    """延迟摘要可能是单个 dict 或 dict 列表，统一成安全白名单数组。"""
    rows = value if isinstance(value, list) else [value]
    result = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        projected = {
            key: _value(row[key])
            for key in ("name", "count", "p50_ms", "p95_ms", "max_ms", "mean_ms")
            if key in row
        }
        if projected:
            result.append(projected)
    return result


def _runtime(row: Any) -> dict[str, Any]:
    data = row.value if row is not None and isinstance(row.value, dict) else {}
    public = {
        key: _value(data.get(key)) for key in ("state", "account_uid", "config_version", "source")
    }
    public.update(
        {
            key: _timestamp(data.get(key))
            for key in ("account_captured_at", "tick_at", "reconciled_at")
        }
    )
    public.update({key: _number(data.get(key)) for key in ("equity", "daily_pnl", "drawdown")})
    public["valuation_complete"] = data.get("valuation_complete") is True
    public["baseline_available"] = data.get("baseline_available") is True
    public["clock_checked"] = data.get("clock_checked") is True
    capabilities = data.get("capabilities")
    public["capabilities"] = (
        {key: capabilities.get(key) is True for key in ("option_units", "option_reduce_only")}
        if isinstance(capabilities, dict)
        else {}
    )
    # 延迟摘要与留存报告已经落在 runtime_state，但此前没有进入页面；
    # 这里只做白名单投影，不把任意 JSON 原样回传。
    public["latency"] = _runtime_ops_list(data.get("latency"))
    public["retention"] = _runtime_ops_row(data.get("retention"))
    public["last_error"] = (
        "服务报告异常；请检查暂停原因及服务器日志。" if data.get("last_error") else None
    )
    lease = data.get("lease")
    if isinstance(lease, dict):
        fields = (
            "held",
            "holder_id",
            "state",
            "heartbeat_at",
            "acquired_at",
            "released_at",
            "takeover_required",
            "takeover_confirmed",
        )
        public["lease"] = {key: _value(lease[key]) for key in fields if key in lease}
    else:
        public["lease"] = _value(lease)
    return public


def _validation_scope(config: AppConfig) -> tuple[str, str]:
    """服务与验证层使用同一个环境/账户标识；不读取任何凭据。"""
    return str(config.active_environment), config.account.expected_uid or "paper-account"


def get_validation_lifecycle() -> Any:
    """延迟导入，便于在验证模块缺席时仍可导入操作台（测试也可 monkeypatch）。"""
    module = importlib.import_module("bybitbot.validation.lifecycle")
    return module.ValidationLifecycle


def _validation_millis(value: Any) -> str | None:
    parsed = _number(value)
    if parsed is None:
        return None
    try:
        seconds = float(parsed) / 1000
        formatted = dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat(timespec="milliseconds")
        return formatted.replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _validation_view(
    study: Any, environment: str, account_uid: str, now: dt.datetime | None = None
) -> dict[str, Any] | None:
    """验证摘要白名单；协议正文、来源内容、凭据与堆栈一律不进入页面。"""
    if not isinstance(study, dict) or study.get("environment") != environment:
        return None
    if study.get("account_uid") != account_uid:
        return None
    run_id = _text(study.get("run_id"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        return None
    bases = study.get("bases")
    positions = study.get("positions")
    chain_head = _text(study.get("chain_head"))
    return {
        "run_id": run_id,
        "status": _text(study.get("status")),
        "policy": _text(study.get("policy")),
        "direction": _text(study.get("direction")),
        "bases": [_text(base) for base in bases[:10]] if isinstance(bases, list) else [],
        "started_at": _validation_millis(study.get("started_ms")),
        "end_at": _validation_millis(study.get("end_ms")),
        "next_due_at": _validation_millis(study.get("next_due_ms")),
        "last_observed_at": _validation_millis(study.get("last_observed_ms")),
        "heartbeat_at": _validation_millis(study.get("heartbeat_ms")),
        "heartbeat_age_seconds": (
            round(max(0, (now - stamp).total_seconds()), 1)
            if (stamp := _utc(_validation_millis(study.get("heartbeat_ms")))) and now
            else None
        ),
        "gap_count": _value(study.get("gap_count")),
        "error_count": _value(study.get("error_count")),
        "observation_count": _value(study.get("observation_count")),
        "chain_head": chain_head[:12],
        "frozen": study.get("frozen") is True,
        "final_report_ready": study.get("final_report_ready") is True,
        "last_error": "验证服务报告异常；请查看服务器日志。" if study.get("last_error") else None,
        "phases": [
            {"base": _text(item.get("base")), "phase": _text(item.get("phase"))}
            for item in positions[:10]
            if isinstance(item, dict)
        ]
        if isinstance(positions, list)
        else [],
    }


async def _validation_status(
    database: Database, config: AppConfig, now: dt.datetime
) -> dict[str, Any]:
    environment, account_uid = _validation_scope(config)
    lifecycle_cls = get_validation_lifecycle()
    lifecycle = lifecycle_cls(database)
    raw = await lifecycle.status(database, environment=environment, account_uid=account_uid)
    studies = raw.get("studies", []) if isinstance(raw, dict) else []
    views = [
        view
        for view in (
            _validation_view(study, environment, account_uid, now)
            for study in studies[:TABLE_LIMIT]
        )
        if view is not None
    ]
    return {"studies": views, "environment": environment, "account_uid": account_uid}


def _validation_filename(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-") or "run"
    return f'attachment; filename="bybitbot-validation-{safe[:64]}.zip"'


async def _validation_export(database: Database, config: AppConfig, run_id: Any) -> bytes:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise HTTPException(422, "运行编号格式无效。")
    environment, account_uid = _validation_scope(config)
    lifecycle_cls = get_validation_lifecycle()
    lifecycle = lifecycle_cls(database)
    try:
        data = await lifecycle.export(
            database, run_id, environment=environment, account_uid=account_uid
        )
    except ValueError as error:
        detail = str(error)
        if "非法" in detail or "格式" in detail:
            raise HTTPException(422, "运行编号格式无效。") from None
        if "不存在" in detail or "不属于" in detail or "无权限" in detail:
            raise HTTPException(404, "验证运行不存在或不属于当前账户。") from None
        raise HTTPException(409, "验证账本暂不可导出；请先核对运行完整性与检查点。") from None
    if not isinstance(data, (bytes, bytearray)):
        raise HTTPException(409, "验证导出结果无效；请稍后重试。")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(409, "验证导出内容不是完整的 ZIP 归档。")
    return bytes(data)


def _configuration(config: AppConfig) -> dict[str, Any]:
    """Explicit safe config sections. Never serialize the root model or profiles."""
    public = {
        "budget": config.budget.model_dump(mode="json"),
        "risk": config.risk.model_dump(mode="json"),
        "market": config.market.model_dump(mode="json"),
        "execution": config.execution.model_dump(mode="json"),
        "option": config.option.model_dump(mode="json"),
        "valuation": config.ops.valuation.model_dump(mode="json"),
        "day_boundary_tz": config.ops.day_boundary_tz,
        "risk_start_at": config.ops.risk_start_at,
        "allowed_products": sorted(config.account.allowed_products),
        "allow_trade_writes": config.profile.allow_trade_writes,
        "configured_strategies": [
            strategy.model_dump(mode="json", exclude={"params"}) for strategy in config.strategies
        ],
    }

    # Even nominally non-secret identifiers can contain operator-supplied text.
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if not _SENSITIVE.search(key)}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return _value(value)

    return clean(public)


async def _status(database: Database, config: AppConfig, now: dt.datetime) -> dict[str, Any]:
    environment = str(config.active_environment)
    async with database.sessionmaker() as session:
        runtime = await session.scalar(
            select(RuntimeState).where(
                RuntimeState.environment == environment, RuntimeState.key == "status"
            )
        )
        account = await session.scalar(
            select(AccountSnapshot)
            .where(AccountSnapshot.environment == environment)
            .order_by(AccountSnapshot.captured_at.desc(), AccountSnapshot.id.desc())
            .limit(1)
        )
        baseline = await session.scalar(
            select(EquityBaseline)
            .where(EquityBaseline.environment == environment)
            .order_by(EquityBaseline.day.desc())
            .limit(1)
        )
        data: dict[str, Any] = {
            "environment": environment,
            "display_name": _text(config.profile.display_name),
            "generated_at": _timestamp(now),
            "currency": config.ops.valuation.currency,
            "runtime": _runtime(runtime),
            "heartbeat": _heartbeat(runtime, now),
            "configuration": _configuration(config),
            "validation": await _validation_status(database, config, now),
            "table_limit": TABLE_LIMIT,
            "baseline": _row(
                baseline, "day equity currency policy_version complete drawdown_peak established_at"
            )
            if baseline is not None
            else None,
        }
        if account is not None:
            view = _row(account, "account_uid account_mode total_equity complete captured_at")
            age = (now - _utc(account.captured_at)).total_seconds()
            view["age_seconds"] = round(max(0, age), 1)
            view["stale"] = age < -5 or age >= config.market.account_state_max_age_seconds
            coin_fields = (
                "equity",
                "available",
                "wallet_balance",
                "usd_value",
                "borrowed",
                "accrued_interest",
                "locked",
            )
            view["coins"] = [
                {"coin": _text(coin), **{key: _number(balance.get(key)) for key in coin_fields}}
                for coin, balance in sorted((account.coins or {}).items())[:200]
                if isinstance(balance, dict) and re.fullmatch(r"[A-Z0-9]{1,16}", coin)
            ]
            data["account"] = view
        else:
            data["account"] = None
        collections = (
            ("strategies", Strategy, Strategy.id, "id name kind enabled created_at"),
            (
                "positions",
                Position,
                Position.updated_at,
                "id category symbol position_idx side size avg_price mark_price delta gamma "
                "vega theta unrealised_pnl realised_pnl settle_coin delivery_time "
                "trusted updated_at",
            ),
            (
                "inventory",
                Inventory,
                Inventory.updated_at,
                "id strategy_id symbol qty avg_cost currency trusted cost_source updated_at",
            ),
            (
                "orders",
                Order,
                Order.created_at,
                "id intent_id order_link_id exchange_order_id category symbol side order_type "
                "time_in_force price qty qty_filled avg_price status reduce_only sync_state "
                "reservation_id rebuild_index parent_order_id local_ttl_expires_at "
                "created_at updated_at",
            ),
            (
                "executions",
                Execution,
                Execution.exec_time,
                "id order_id order_link_id exchange_exec_id category symbol side exec_price "
                "exec_qty exec_fee exec_fee_asset is_maker exec_time received_at",
            ),
            (
                "pauses",
                RiskPause,
                RiskPause.created_at,
                "id scope scope_key reason_code reason_detail locked active created_at resolved_at "
                "resolved_by",
            ),
            (
                "alerts",
                Alert,
                Alert.first_seen_at,
                "id severity title detail status first_seen_at resolved_at delivery_attempts",
            ),
            (
                "reconciliations",
                ReconciliationRun,
                ReconciliationRun.started_at,
                "id scope started_at finished_at repaired allows_resume watermark cursor",
            ),
        )
        for key, model, order, fields in collections:
            rows = list(
                await session.scalars(
                    select(model)
                    .where(model.environment == environment)
                    .order_by(order.desc(), model.id.desc())
                    .limit(TABLE_LIMIT)
                )
            )
            data[key] = [_row(row, fields) for row in rows]
            if key == "reconciliations":
                for view, row in zip(data[key], rows, strict=True):
                    differences = row.differences
                    view["difference_count"] = (
                        len(differences) if isinstance(differences, list) else 0
                    )
                    view["differences"] = [
                        {
                            field: _value(difference.get(field))
                            for field in ("kind", "detail", "severity", "symbol", "reference")
                        }
                        for difference in (differences if isinstance(differences, list) else [])[
                            :100
                        ]
                        if isinstance(difference, dict)
                    ]
        symbols = {
            row["symbol"]
            for key in ("positions", "inventory", "orders")
            for row in data[key]
            if row.get("symbol")
        }
        symbols.update(symbol for strategy in config.strategies for symbol in strategy.symbols)
        specs = await session.scalars(
            select(Instrument)
            .where(Instrument.environment == environment, Instrument.symbol.in_(symbols))
            .order_by(Instrument.category, Instrument.symbol)
            .limit(300)
        )
        data["instruments"] = [
            _row(spec, "category symbol base_coin quote_coin settle_coin") for spec in specs
        ]
        quote_rank = (
            select(
                QuoteSnapshot.id,
                func.row_number()
                .over(
                    partition_by=(QuoteSnapshot.category, QuoteSnapshot.symbol),
                    order_by=(QuoteSnapshot.received_at.desc(), QuoteSnapshot.id.desc()),
                )
                .label("rank"),
            )
            .where(QuoteSnapshot.environment == environment)
            .subquery()
        )
        quotes = await session.scalars(
            select(QuoteSnapshot)
            .join(quote_rank, quote_rank.c.id == QuoteSnapshot.id)
            .where(quote_rank.c.rank == 1)
            .order_by(QuoteSnapshot.category, QuoteSnapshot.symbol)
            .limit(210)
        )
        data["quotes"] = []
        for quote in quotes:
            captured = _utc(quote.source_ts)
            received = _utc(quote.received_at)
            source_age = (now - captured).total_seconds() if captured else None
            receive_age = (now - received).total_seconds() if received else None
            age = (
                max(source_age, receive_age)
                if source_age is not None and receive_age is not None
                else None
            )
            data["quotes"].append(
                {
                    **_row(quote, "category symbol source_ts received_at anomalous"),
                    "age_seconds": round(max(0, age), 1) if age is not None else None,
                    "stale": (
                        age is None
                        or age >= config.market.max_quote_age_seconds
                        or source_age < -5
                        or receive_age < -5
                        or quote.anomalous
                    ),
                }
            )
        commands = await session.scalars(
            select(ControlCommand)
            .where(ControlCommand.environment == environment)
            .order_by(ControlCommand.created_at.desc(), ControlCommand.id.desc())
            .limit(TABLE_LIMIT)
        )
        data["control_commands"] = [_command(row) for row in commands]
        flows = await session.scalars(
            select(CashFlow)
            .where(CashFlow.environment == environment)
            .order_by(CashFlow.occurred_at.desc(), CashFlow.id.desc())
            .limit(TABLE_LIMIT)
        )
        data["cash_flows"] = [
            {
                **_row(flow, "id asset amount kind external occurred_at created_at"),
                # dedupe_key 形如 external-cash-flow:<reference>，只展示用户登记的引用，
                # 不把整条 key 或其他内部字段暴露到页面。
                "reference": (
                    _text(flow.dedupe_key.removeprefix("external-cash-flow:"))[:128]
                    if isinstance(flow.dedupe_key, str)
                    else None
                ),
            }
            for flow in flows
        ]
        versions = await session.scalars(
            select(StrategyConfigVersion)
            .join(Strategy, Strategy.id == StrategyConfigVersion.strategy_id)
            .where(Strategy.environment == environment)
            .order_by(StrategyConfigVersion.created_at.desc(), StrategyConfigVersion.id.desc())
            .limit(TABLE_LIMIT)
        )
        data["config_versions"] = [
            {
                **_row(
                    row,
                    "id strategy_id version budget_per_run max_spend_per_trade currency "
                    "active activated_at created_at",
                ),
                "symbols": [_text(symbol) for symbol in (row.symbols or [])[:100]],
                "schedule_utc": [_text(at) for at in (row.schedule_utc or [])[:100]],
                "params": _strategy_params(row.params),
                "exit_rules": _exit_rules(row.exit_rules or (row.params or {}).get("exit_rules")),
            }
            for row in versions
        ]
        rejects = await session.execute(
            select(RiskDecision, TradeIntent)
            .join(TradeIntent, TradeIntent.id == RiskDecision.intent_id)
            .where(TradeIntent.environment == environment, RiskDecision.decision == "REJECTED")
            .order_by(RiskDecision.created_at.desc(), RiskDecision.id.desc())
            .limit(TABLE_LIMIT)
        )
        data["risk_rejects"] = [
            {
                **_row(
                    risk,
                    "id intent_id decision reason_code reason_detail config_version created_at",
                ),
                "symbol": _text(intent.symbol),
                "strategy_id": intent.strategy_id,
                "signal_id": _text(intent.signal_id),
            }
            for risk, intent in rejects
        ]
    return data


_AUDIT_COLUMNS = [
    "intent_id",
    "signal_id",
    "intent_at",
    "strategy_id",
    "strategy_version",
    "action",
    "category",
    "symbol",
    "side",
    "intent_qty",
    "qty_unit",
    "spend_limit",
    "currency",
    "intent_status",
    "risk_id",
    "risk_decision",
    "risk_reason",
    "risk_config_version",
    "order_id",
    "order_link_id",
    "order_status",
    "order_qty",
    "filled_qty",
    "order_price",
    "sync_state",
    "execution_id",
    "exchange_exec_id",
    "exec_qty",
    "exec_price",
    "fee",
    "fee_asset",
    "exec_at",
]


def _csv_cell(value: Any) -> str:
    text = "" if value is None else _text(_value(value))
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


async def _audit_csv(database: Database, environment: str, limit: int) -> Response:
    stmt = (
        select(TradeIntent, RiskDecision, Order, Execution)
        .outerjoin(RiskDecision, RiskDecision.intent_id == TradeIntent.id)
        .outerjoin(Order, and_(Order.intent_id == TradeIntent.id, Order.environment == environment))
        .outerjoin(
            Execution,
            and_(
                Execution.environment == environment,
                Execution.category == Order.category,
                or_(
                    Execution.order_id == Order.id,
                    and_(
                        Execution.order_id.is_(None), Execution.order_link_id == Order.order_link_id
                    ),
                ),
            ),
        )
        .where(TradeIntent.environment == environment)
        .order_by(
            TradeIntent.created_at.desc(),
            TradeIntent.id.desc(),
            RiskDecision.id,
            Order.id,
            Execution.id,
        )
        .limit(limit + 1)
    )
    async with database.sessionmaker() as session:
        rows = list(await session.execute(stmt))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(_AUDIT_COLUMNS)
    for intent, risk, order, execution in rows[:limit]:

        def field(row: Any, key: str) -> Any:
            return getattr(row, key, None) if row is not None else None

        values = [
            intent.id,
            intent.signal_id,
            intent.created_at,
            intent.strategy_id,
            intent.strategy_version,
            intent.action,
            intent.category,
            intent.symbol,
            intent.side,
            intent.qty,
            intent.qty_unit,
            intent.spend_limit,
            intent.currency,
            intent.status,
            field(risk, "id"),
            field(risk, "decision"),
            field(risk, "reason_code"),
            field(risk, "config_version"),
            field(order, "id"),
            field(order, "order_link_id"),
            field(order, "status"),
            field(order, "qty"),
            field(order, "qty_filled"),
            field(order, "price"),
            field(order, "sync_state"),
            field(execution, "id"),
            field(execution, "exchange_exec_id"),
            field(execution, "exec_qty"),
            field(execution, "exec_price"),
            field(execution, "exec_fee"),
            field(execution, "exec_fee_asset"),
            field(execution, "exec_time"),
        ]
        writer.writerow([_csv_cell(value) for value in values])
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="bybitbot-audit.csv"',
            "X-Export-Limit": str(limit),
            "X-Export-Truncated": str(len(rows) > limit).lower(),
        },
    )


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin", "")
    if not origin or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "需要同源请求。")
    try:
        source = urlsplit(origin)
        target = urlsplit(str(request.base_url))
        if (
            source.scheme not in {"http", "https"}
            or source.username
            or source.password
            or source.path
            or source.query
            or source.fragment
            or source.scheme != target.scheme
            or source.hostname != target.hostname
            or (source.port or (443 if source.scheme == "https" else 80))
            != (target.port or (443 if target.scheme == "https" else 80))
        ):
            raise ValueError("origin mismatch")
    except ValueError:
        raise HTTPException(403, "需要同源请求。") from None


async def _parse(request: Request, model: type[BaseModel]) -> Any:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "请发送 JSON 请求。")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise HTTPException(413, "请求内容过长。")
    try:
        if model is ShadowConfigPayload:

            def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            return model.model_validate(json.loads(body, object_pairs_hook=unique_keys))
        return model.model_validate_json(body)
    except ValidationError as exc:
        if model is ShadowConfigPayload:
            # Locations are allowlisted, since extra-field names can themselves
            # contain secrets. Never echo validator messages or rejected inputs.
            allowed = set().union(
                *(
                    schema.model_fields.keys()
                    for schema in (
                        ShadowConfigPayload,
                        ShadowSettingsPayload,
                        ShadowAutoPayload,
                        ShadowPairPayload,
                        ShadowAmountPayload,
                    )
                )
            )
            problems = field_errors(exc, allowed=allowed)
            detail = "配置参数无效：" + "；".join(problems[:6])
            raise HTTPException(422, detail) from None
        raise HTTPException(422, "请求格式、操作范围或确认文字无效。") from None
    except (ValueError, UnicodeError):
        raise HTTPException(422, "JSON 格式无效或含重复字段。") from None


class _NoMarketAdapter:
    """仅供只读投影使用；没有任何交易写方法，也不发网络请求。"""

    async def get_ticker(self, category: str, symbol: str) -> Any:
        raise RuntimeError("影子盘投影不读取行情")

    async def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> Any:
        raise RuntimeError("影子盘投影不读取行情")


def create_app(
    config: AppConfig,
    database: Database,
    auth_path: Path | str,
    secure_cookies: bool = True,
    *,
    clock: Callable[[], dt.datetime] | None = None,
    config_path: Path | str | None = None,
) -> FastAPI:
    """Build the console without owning the database lifecycle or launching a worker.

    ``config_path`` 是配置文件路径；提供后启用影子盘配置页的受控写入
    （GET/PUT ``/api/shadow/config``）。不提供则相关端点返回 404，页面仍可
    只读展示。
    """
    admin = _load_admin(auth_path)
    sessions = _Sessions(admin.session_secret)
    limits = _LoginLimits()
    password_slots = asyncio.Semaphore(2)
    manager = ControlCommandManager(database.sessionmaker, str(config.active_environment))
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app = FastAPI(title="Bybit 操作台", docs_url=None, redoc_url=None, openapi_url=None)

    def now() -> dt.datetime:
        return _utc(clock() if clock else dt.datetime.now(dt.UTC))

    def authenticated(request: Request) -> dict[str, Any]:
        session = sessions.read(request.cookies.get(SESSION_COOKIE), int(now().timestamp()))
        if session is None or session.get("username") != admin.username:
            raise HTTPException(401, "登录已失效，请重新登录。")
        return session

    def write_session(
        request: Request, session: dict[str, Any] = Depends(authenticated)
    ) -> dict[str, Any]:
        _same_origin(request)
        token = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(token.encode(), session["csrf_token"].encode()):
            raise HTTPException(403, "请求校验失效，请刷新页面。")
        return session

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self'; font-src 'self'; "
                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                ),
            }
        )
        if secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "请求参数无效。"}, status_code=422)

    @app.get("/health")
    async def health() -> Response:
        try:
            async with database.sessionmaker() as session:
                await session.execute(select(1))
                runtime = await session.scalar(
                    select(RuntimeState).where(
                        RuntimeState.environment == str(config.active_environment),
                        RuntimeState.key == "status",
                    )
                )
            healthy = _heartbeat(runtime, now())["fresh"]
        except SQLAlchemyError:
            healthy = False
        return JSONResponse(
            {"status": "ok" if healthy else "degraded"}, status_code=200 if healthy else 503
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        return templates.TemplateResponse(request=request, name="login.html")

    @app.get("/", response_class=HTMLResponse)
    async def console(request: Request) -> Response:
        if sessions.read(request.cookies.get(SESSION_COOKIE), int(now().timestamp())) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="console.html")

    @app.post("/api/login")
    async def login(request: Request) -> Response:
        _same_origin(request)
        client = request.client.host if request.client else "unknown"
        if not limits.allow(client, now().timestamp()):
            raise HTTPException(
                429, "登录尝试过多，请 5 分钟后重试。", headers={"Retry-After": "300"}
            )
        body: LoginRequest = await _parse(request, LoginRequest)
        async with password_slots:
            digest = await run_in_threadpool(
                hashlib.pbkdf2_hmac, "sha256", body.password.encode(), admin.salt, 600_000
            )
        valid_name = hmac.compare_digest(body.username.encode(), admin.username.encode())
        valid_password = hmac.compare_digest(digest, admin.digest)
        if not (valid_name and valid_password):
            raise HTTPException(401, "用户名或密码错误。")
        previous = sessions.read(request.cookies.get(SESSION_COOKIE), int(now().timestamp()))
        if previous:
            sessions.revoke(previous)
        cookie, session = sessions.issue(admin.username, int(now().timestamp()))
        response = JSONResponse(
            {
                "username": admin.username,
                "csrf_token": session["csrf_token"],
                "expires_at": _timestamp(dt.datetime.fromtimestamp(session["expires_at"], dt.UTC)),
            }
        )
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/session")
    async def session_status(session: dict[str, Any] = Depends(authenticated)) -> dict[str, Any]:
        return {
            "username": session["username"],
            "csrf_token": session["csrf_token"],
            "expires_at": _timestamp(dt.datetime.fromtimestamp(session["expires_at"], dt.UTC)),
        }

    @app.post("/api/logout")
    async def logout(session: dict[str, Any] = Depends(write_session)) -> Response:
        sessions.revoke(session)
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE, httponly=True, secure=secure_cookies, samesite="strict", path="/"
        )
        return response

    @app.get("/api/status", dependencies=[Depends(authenticated)])
    async def status_view() -> dict[str, Any]:
        try:
            return await _status(database, config, now())
        except SQLAlchemyError:
            raise HTTPException(503, "暂时无法读取数据库；请检查服务与数据库连接。") from None

    async def saved_shadow_config():
        if config_path is None:
            raise HTTPException(404, "当前部署未启用配置写入；请在服务器侧维护配置。")
        try:
            return await run_in_threadpool(read_shadow_config, config_path)
        except OSError:
            raise HTTPException(503, "无法读取配置；请在服务器侧核对权限与磁盘。") from None
        except ShadowConfigError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/shadow", dependencies=[Depends(authenticated)])
    async def shadow_view() -> dict[str, Any]:
        """Metrics use the explicitly labeled web-start configuration, not a worker claim."""
        try:
            engine = ShadowEngine(
                adapter=_NoMarketAdapter(),
                sessionmaker=database.sessionmaker,
                settings=config.spread,
            )
            result = await engine.status()
            result.update(configuration_source="web_start", runtime_verified=False)
            if config_path is not None:
                saved = await saved_shadow_config()
                result.update(
                    saved_enabled=saved.config.spread.shadow.enabled,
                    saved_revision=saved.revision,
                    pending_restart=saved.config.spread != config.spread,
                )
            return result
        except SQLAlchemyError:
            raise HTTPException(503, "暂时无法读取影子盘记录；请检查数据库。") from None

    @app.get("/api/shadow/config", dependencies=[Depends(authenticated)])
    async def shadow_config_view() -> dict[str, Any]:
        """Read the saved file afresh; expose only research settings and safe metadata."""
        result = (await saved_shadow_config()).public()
        if config.active_environment is not Environment.PAPER:
            result.update(writable=False, read_only_reason="仅 paper 环境允许编辑影子盘配置。")
        elif config.spread.enabled or config.spread.auto.enabled:
            result.update(
                writable=False,
                read_only_reason="执行面已在 web 启动配置中启用；关闭并重启服务后再编辑。",
            )
        return result

    @app.put("/api/shadow/config")
    async def shadow_config_write(
        request: Request, session: dict[str, Any] = Depends(write_session)
    ) -> Response:
        if config_path is None:
            raise HTTPException(404, "当前部署未启用配置写入；请在服务器侧维护配置。")
        body: ShadowConfigPayload = await _parse(request, ShadowConfigPayload)
        # A running paper execution process might still use the startup settings
        # even after someone manually disables execution in the saved file.
        if config.active_environment is not Environment.PAPER:
            raise HTTPException(403, "仅 paper 环境允许编辑影子盘配置。")
        if config.spread.enabled or config.spread.auto.enabled:
            raise HTTPException(403, "执行面已在 web 启动配置中启用；关闭并重启服务后再编辑。")
        try:
            result = await run_in_threadpool(
                write_spread,
                config_path,
                body.model_dump(mode="json", exclude={"revision"}),
                revision=body.revision,
            )
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ReadOnlyConfig as exc:
            raise HTTPException(403, str(exc)) from None
        except ShadowConfigError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(503, "配置文件写入失败；请在服务器侧核对权限与磁盘。") from None
        return JSONResponse(
            {
                "written": True,
                **result,
                "restart_required": True,
                "note": "配置已校验并保存；请手动重启 worker 后生效，未自动重启任何服务。",
            }
        )

    @app.get("/api/validation/{run_id}/export", dependencies=[Depends(authenticated)])
    async def validation_export(run_id: str) -> Response:
        try:
            data = await _validation_export(database, config, run_id)
        except SQLAlchemyError:
            raise HTTPException(503, "暂时无法导出验证归档；请检查数据库后重试。") from None
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": _validation_filename(run_id),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/commands")
    async def command(
        request: Request, session: dict[str, Any] = Depends(write_session)
    ) -> Response:
        body: CommandRequest = await _parse(request, CommandRequest)
        try:
            row, created = await manager.submit(
                command_id=str(body.command_id),
                kind=body.kind,
                payload=body.payload,
                requested_by=session["username"],
            )
        except ValueError:
            raise HTTPException(409, "此命令编号已用于其他操作；请刷新命令记录。") from None
        except SQLAlchemyError:
            raise HTTPException(503, "命令登记结果未知；请保留编号并使用同一编号重试。") from None
        if row.kind != body.kind or row.payload != body.payload:
            raise HTTPException(409, "此命令编号已用于其他操作；请刷新命令记录。")
        return JSONResponse(
            {"command": _command(row), "created": created},
            status_code=202 if row.status in {"PENDING", "EXECUTING"} else 200,
        )

    @app.get("/api/audit.csv", dependencies=[Depends(authenticated)])
    async def audit(limit: int = Query(default=5000, ge=1, le=5000)) -> Response:
        try:
            return await _audit_csv(database, str(config.active_environment), limit)
        except SQLAlchemyError:
            raise HTTPException(503, "暂时无法导出审计记录；请稍后重试。") from None

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    return app


__all__ = ["CommandRequest", "create_app", "get_validation_lifecycle"]
