"""配置模型 —— 对应需求文档 §5.2「实盘必填配置」与 §4.1「环境与接入」。

设计约束（需求文档 §5.2）：
    任何缺失、负值、币种不匹配或逻辑矛盾都应被校验拒绝。

因此这里的每个金额都带币种，数量/金额一律使用 Decimal，模型层用
``field_validator`` 做正数、币种一致性与上限合理性检查。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class Environment(StrEnum):
    """ENV-01：互相隔离的环境。"""

    PAPER = "paper"
    DEMO = "demo"
    TESTNET = "testnet"
    LIVE = "live"


class Product(StrEnum):
    """允许的产品类别（MKT-01 / ENV-02）。"""

    SPOT = "spot"
    OPTION = "option"


class AccountMode(StrEnum):
    """账户模式。期权要求统一账户（UTA）。"""

    UNIFIED = "unified"
    CLASSIC = "classic"


class RunState(StrEnum):
    """OPS-01 / §4.9 运行状态机。

    STARTING/RECONCILING 禁止新开仓；PAUSED_OPEN 禁止新开仓但允许可信退出；
    EXITING 只做指定 bot 仓位减仓；STOPPED 不执行任何自动动作。
    """

    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSED_OPEN = "PAUSED_OPEN"
    EXITING = "EXITING"
    STOPPED = "STOPPED"


class OrderSide(StrEnum):
    BUY = "Buy"
    SELL = "Sell"


class OrderType(StrEnum):
    LIMIT = "Limit"
    MARKET = "Market"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "PostOnly"


class IntentAction(StrEnum):
    """交易意图的业务目的（STR-01）。"""

    SPOT_BUY = "SPOT_BUY"
    SPOT_SELL = "SPOT_SELL"
    OPTION_BUY_OPEN = "OPTION_BUY_OPEN"
    OPTION_SELL_CLOSE = "OPTION_SELL_CLOSE"


class OrderStatus(StrEnum):
    """业务状态（EXE-01 / §4.5 状态说明）。

    ``UNKNOWN`` 表示结果或同步状态不确定，可与部分成交事实并存，
    不能用它覆盖已确认的成交数量。
    """

    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ReservationStatus(StrEnum):
    """预留状态（§5.1 Reservation）。"""

    HELD = "HELD"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    RELEASED = "RELEASED"


class DecimalAmount(BaseModel):
    """带币种的金额。金额一律 Decimal，禁止 float（需求文档 §5.1）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Annotated[Decimal, Field(ge=0)]
    currency: str = Field(min_length=2, max_length=16)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def __add__(self, other: DecimalAmount) -> DecimalAmount:
        self._assert_same_currency(other)
        return DecimalAmount(value=self.value + other.value, currency=self.currency)

    def __sub__(self, other: DecimalAmount) -> DecimalAmount:
        self._assert_same_currency(other)
        return DecimalAmount(value=self.value - other.value, currency=self.currency)

    def _assert_same_currency(self, other: DecimalAmount) -> None:
        if self.currency != other.currency:
            raise ValueError(f"币种不匹配：{self.currency} 与 {other.currency} 不能相加减")


# --------------------------------------------------------------------------
# 环境档案（ENV-01）
# --------------------------------------------------------------------------


class EnvironmentProfile(BaseModel):
    """单个环境的隔离档案。

    ENV-01 要求隔离端点、凭据引用、数据库及日志标识；
    Paper 不装载交易凭据，也不发送交易写请求。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment
    display_name: str
    base_url: str | None = None
    ws_url: str | None = None
    ws_public_url: str | None = None
    db_dsn: str
    log_prefix: str
    credential_ref: str | None = None
    #: 是否允许加载交易凭据。Paper 必须为 False。
    allow_credentials: bool = False
    #: 是否允许发送交易写请求。Paper 必须为 False。
    allow_trade_writes: bool = False
    site: str = "bybit"

    @model_validator(mode="after")
    def _paper_is_inert(self) -> EnvironmentProfile:
        expected = {
            Environment.LIVE: ("api.bybit.com", "stream.bybit.com"),
            Environment.TESTNET: ("api-testnet.bybit.com", "stream-testnet.bybit.com"),
            Environment.DEMO: ("api-demo.bybit.com", "stream-demo.bybit.com"),
        }
        if self.environment is Environment.PAPER:
            if self.allow_credentials or self.allow_trade_writes or self.credential_ref:
                raise ValueError(
                    "Paper 环境不得装载交易凭据或发送交易写请求"
                    "（ENV-01）：allow_credentials/allow_trade_writes 必须为 False"
                )
            if self.base_url or self.ws_url or self.ws_public_url:
                raise ValueError("Paper 环境不应配置任何交易所端点（ENV-01）")
        else:
            if not self.base_url or not self.ws_url:
                raise ValueError(f"{self.environment} 环境必须配置 base_url 与 ws_url（ENV-01）")
            rest, private = expected[self.environment]
            for address, scheme, host in (
                (self.base_url, "https", rest),
                (self.ws_url, "wss", private),
            ):
                parsed = urlsplit(address)
                if parsed.scheme != scheme or parsed.hostname != host or parsed.username:
                    raise ValueError(f"{self.environment} 端点与环境不匹配（ENV-02）")
            if self.ws_public_url:
                public = "stream.bybit.com" if self.environment is Environment.DEMO else private
                parsed = urlsplit(self.ws_public_url)
                if parsed.scheme != "wss" or parsed.hostname != public:
                    raise ValueError("公共行情端点与环境不匹配（ENV-02）")
        return self


# --------------------------------------------------------------------------
# 账户（§5.2 账户）
# --------------------------------------------------------------------------


class AccountConfig(BaseModel):
    """账户相关配置。

    ``expected_uid`` 为空表示尚未绑定真实账户；此时系统只能运行只读
    或 paper 模式，不得进入实盘（ENV-02 / ENV-03）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment
    expected_uid: str | None = None
    account_mode: AccountMode = AccountMode.UNIFIED
    allowed_products: frozenset[Product] = frozenset({Product.SPOT})
    #: 要求 API Key 具备的权限（ENV-02 启动校验）。
    required_permissions: frozenset[str] = frozenset({"Read", "Trade"})
    #: 允许的站点端点标识，与 profile.site 一起做 ENV-02 校验。
    allowed_sites: frozenset[str] = frozenset({"bybit"})
    #: 绑定 IP（留空表示不限制，SEC-01 建议绑定固定出口 IP）。
    bound_ip: str | None = None


# --------------------------------------------------------------------------
# 资金与风险（§5.2 资金 / 风险）
# --------------------------------------------------------------------------


class BudgetConfig(BaseModel):
    """资金限额。语义必须在配置界面可见（§5.2 末段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_spend_per_trade: DecimalAmount
    max_spot_exposure_per_strategy: DecimalAmount
    max_spot_exposure_account: DecimalAmount
    option_premium_budget: DecimalAmount
    #: 费用余量：最大支出的额外缓冲。
    fee_buffer: DecimalAmount
    max_open_orders: Annotated[int, Field(ge=1, le=1000)]

    @model_validator(mode="after")
    def _consistent(self) -> BudgetConfig:
        if self.max_spend_per_trade.currency != self.option_premium_budget.currency:
            raise ValueError("单笔上限与期权权利金预算必须是同一币种")
        if self.max_spot_exposure_account.value < self.max_spot_exposure_per_strategy.value:
            raise ValueError("账户级现货敞口不得小于策略级现货敞口")
        if self.max_spend_per_trade.value > self.max_spot_exposure_account.value:
            raise ValueError("单笔最大支出不得大于账户现货敞口上限")
        return self


class RiskLimits(BaseModel):
    """风险限额（§5.2 风险）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_daily_loss: DecimalAmount
    max_drawdown: DecimalAmount
    #: 每标的绝对 Delta 限额 L（RSK-05）。键为标的，值为绝对数量限额。
    delta_limit_per_underlying: dict[str, Decimal] = Field(default_factory=dict)
    #: 按标的的权利金成本上限。
    premium_cost_limit_per_underlying: dict[str, DecimalAmount] = Field(default_factory=dict)
    #: 按到期日的权利金成本上限（键为 UTC 到期日）。
    premium_cost_limit_per_expiry: dict[str, DecimalAmount] = Field(default_factory=dict)

    @field_validator("delta_limit_per_underlying")
    @classmethod
    def _positive_deltas(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        for symbol, limit in v.items():
            if limit <= 0:
                raise ValueError(f"标的 {symbol} 的 Delta 限额必须为正数")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> RiskLimits:
        if self.max_daily_loss.currency != self.max_drawdown.currency:
            raise ValueError("日亏损上限与回撤上限必须是同一币种")
        for limits in self.premium_cost_limit_per_underlying.values():
            if limits.currency != self.max_daily_loss.currency:
                raise ValueError("权利金上限与风险限额必须是同一估值币种")
        for limits in self.premium_cost_limit_per_expiry.values():
            if limits.currency != self.max_daily_loss.currency:
                raise ValueError("权利金上限与风险限额必须是同一估值币种")
        return self


# --------------------------------------------------------------------------
# 行情（§5.2 行情）
# --------------------------------------------------------------------------


class MarketConfig(BaseModel):
    """行情新鲜度与可执行价格约束（MKT-02 / MKT-03）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_quote_age_seconds: Annotated[int, Field(ge=1)]
    account_state_check_interval_seconds: Annotated[int, Field(ge=5)]
    account_state_max_age_seconds: Annotated[int, Field(ge=5)]
    max_spread_bps: Annotated[Decimal, Field(ge=0)]
    max_price_deviation_bps: Annotated[Decimal, Field(ge=0)]

    @model_validator(mode="after")
    def _consistent(self) -> MarketConfig:
        if self.account_state_max_age_seconds < self.account_state_check_interval_seconds:
            raise ValueError("账户状态有效期不得短于校验周期")
        return self


# --------------------------------------------------------------------------
# 执行（§5.2 执行）
# --------------------------------------------------------------------------


class ExecutionConfig(BaseModel):
    """执行参数（EXE-04）。

    ``signal_ttl_seconds`` 只控制新提交；``order_ttl_seconds`` 到期只触发撤单，
    不代表交易所终态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_ttl_seconds: Annotated[int, Field(ge=1)]
    order_ttl_seconds: Annotated[int, Field(ge=1)]
    cancel_query_deadline_seconds: Annotated[int, Field(ge=1)]
    max_rebuild_attempts: Annotated[int, Field(ge=0, le=10)]
    max_intent_total_spend: DecimalAmount
    exit_price_floor_bps: Annotated[Decimal, Field(ge=0)]
    request_timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    max_rate_limit_retries: Annotated[int, Field(ge=0, le=10)]

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionConfig:
        if self.order_ttl_seconds <= self.signal_ttl_seconds:
            raise ValueError("本地订单 TTL 必须长于信号有效期，否则订单一挂就过期")
        if self.max_rebuild_attempts > 0 and self.max_intent_total_spend.value <= 0:
            raise ValueError("允许重建订单时必须给出意图总支出上限")
        return self


# --------------------------------------------------------------------------
# 期权（§5.2 期权）
# --------------------------------------------------------------------------


class OptionConfig(BaseModel):
    """期权筛选与禁开仓窗口（STR-03 / OPT-03 / OPT-04）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_option_types: frozenset[Literal["Call", "Put"]] = frozenset({"Call"})
    min_days_to_expiry: Annotated[int, Field(ge=0)]
    max_days_to_expiry: Annotated[int, Field(ge=1)]
    #: 行权价筛选区间，相对于标的现价的百分比；None 表示不按行权价筛。
    strike_range_pct: tuple[Decimal, Decimal] | None = None
    #: 绝对 Delta 筛选区间；None 表示不按 Delta 筛。
    delta_range: tuple[Decimal, Decimal] | None = None
    #: 禁开仓窗口（UTC 小时，闭区间起点、开端点）。
    no_open_window_start_hour_utc: Annotated[int, Field(ge=0, le=23)] = 0
    no_open_window_end_hour_utc: Annotated[int, Field(ge=0, le=23)] = 0
    #: 禁开仓窗口内仍活动买单的本地 TTL 上限（秒），必须早于窗口起点。
    no_open_window_order_ttl_seconds: Annotated[int, Field(ge=1)] = 3600
    #: 到期前多久开始执行退出规则。
    exit_before_expiry_seconds: Annotated[int, Field(ge=0)] = 86400
    max_settle_coin_cash_ratio: Annotated[Decimal, Field(gt=0)] = Decimal("1")

    @model_validator(mode="after")
    def _consistent(self) -> OptionConfig:
        if self.min_days_to_expiry > self.max_days_to_expiry:
            raise ValueError("min_days_to_expiry 不得大于 max_days_to_expiry")
        if self.strike_range_pct is not None:
            low, high = self.strike_range_pct
            if low > high:
                raise ValueError("strike_range_pct 下界不得大于上界")
        if self.strike_range_pct is not None and self.strike_range_pct[0] <= 0:
            raise ValueError("strike_range_pct 必须是正的现价百分比，如 [90, 110]")
        if self.delta_range is not None:
            low, high = self.delta_range
            if not 0 <= low <= high <= 1:
                raise ValueError("delta_range 必须是 [0, 1] 内的绝对 Delta 区间")
        return self


# --------------------------------------------------------------------------
# 策略（§5.2 策略）
# --------------------------------------------------------------------------


class ExitRuleConfig(BaseModel):
    """退出规则（STR-03）。

    首期只支持本地退出规则；触发后按可成交报价（买一及所需深度）估值。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["hold_period", "profit_threshold", "before_expiry"]
    hold_seconds: Annotated[int, Field(ge=0)] | None = None
    profit_threshold_pct: Annotated[Decimal, Field(ge=0)] | None = None
    max_reprice_attempts: Annotated[int, Field(ge=0, le=10)] = 1
    exit_validity_seconds: Annotated[int, Field(ge=1)] = 300

    @model_validator(mode="after")
    def _kind_specific(self) -> ExitRuleConfig:
        if self.kind == "hold_period" and self.hold_seconds is None:
            raise ValueError("hold_period 退出规则必须给出 hold_seconds")
        if self.kind == "profit_threshold" and self.profit_threshold_pct is None:
            raise ValueError("profit_threshold 退出规则必须给出 profit_threshold_pct")
        return self


class StrategyConfig(BaseModel):
    """单个策略实例的配置。

    STR-04：配置变更生成不可变版本；这里描述的是「一个版本」的内容，
    版本号与生效原子性由 Strategy/ConfigVersion 实体负责。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["spot_fixed_budget_dca", "option_single_leg_buy"]
    enabled: bool = False
    symbols: tuple[str, ...] = Field(min_length=1)
    #: 计划运行的 UTC 时刻（HH:MM），每个时刻一个信号去重键。
    schedule_utc: tuple[str, ...] = Field(min_length=1)
    signal_ttl_seconds: Annotated[int, Field(ge=1)]
    cooldown_seconds: Annotated[int, Field(ge=0)]
    budget_per_run: DecimalAmount
    max_spend_per_trade: DecimalAmount
    exit_rules: tuple[ExitRuleConfig, ...] = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _dedupe(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("symbols 不得重复")
        return v

    @field_validator("schedule_utc")
    @classmethod
    def _times(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) != len(set(v)):
            raise ValueError("调度时刻不得重复")
        if any(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None for value in v):
            raise ValueError("调度时刻必须为 UTC HH:MM")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> StrategyConfig:
        if self.budget_per_run.currency != self.max_spend_per_trade.currency:
            raise ValueError("每轮预算与单笔上限必须是同一币种")
        if self.max_spend_per_trade.value > self.budget_per_run.value:
            raise ValueError("单笔上限不得大于每轮预算")
        allowed_params = (
            {"max_price_deviation", "min_qty"}
            if self.kind == "spot_fixed_budget_dca"
            else {"options_type", "min_premium", "max_premium", "min_open_interest"}
        )
        if self.params.keys() - allowed_params:
            raise ValueError("策略 params 存在未支持的字段")
        for key, value in self.params.items():
            if key == "options_type":
                if value not in {"Call", "Put"}:
                    raise ValueError("options_type 必须为 Call 或 Put")
                continue
            number = Decimal(value)
            if not number.is_finite() or number < 0:
                raise ValueError(f"{key} 必须为非负有限数")
            if key == "max_price_deviation" and number > 1:
                raise ValueError("max_price_deviation 必须为 [0, 1] 内的比例")
        minimum = Decimal(self.params.get("min_premium", "0"))
        maximum = Decimal(self.params.get("max_premium", "0"))
        if maximum and minimum > maximum:
            raise ValueError("min_premium 不得大于 max_premium")
        for rule in self.exit_rules:
            if (
                rule.kind == "profit_threshold"
                and rule.profit_threshold_pct is not None
                and rule.profit_threshold_pct > 100
            ):
                raise ValueError("收益阈值百分比不得大于 100")
        return self


# --------------------------------------------------------------------------
# 运维（§5.2 运维）
# --------------------------------------------------------------------------


class ValuationPolicy(BaseModel):
    """估值政策（LED-04）。

    唯一、版本化的风控权益口径；交易所返回的 equity 仅用于核对。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    currency: str = Field(min_length=2, max_length=16)
    #: 现货估值来源：账户余额按该来源换算。
    spot_valuation_source: Literal["wallet_balance", "ticker_bid"] = "wallet_balance"
    #: 期权估值使用标记价还是买一。
    option_valuation_source: Literal["mark", "bid"] = "mark"
    #: 汇率来源标识。
    fx_source: Literal["ticker_bid"] = "ticker_bid"
    #: 金额容差（绝对值）。
    tolerance: Annotated[Decimal, Field(ge=0)] = Decimal("0.01")

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class OpsConfig(BaseModel):
    """运维参数（§5.2 运维）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_channels: tuple[str, ...] = ()
    notification_failure_pause_seconds: Annotated[int, Field(ge=0)] = 900
    external_heartbeat_interval_seconds: Annotated[int, Field(ge=10)] = 30
    external_heartbeat_miss_count: Annotated[int, Field(ge=1)] = 3
    retention_days: Annotated[int, Field(ge=30)] = 365
    raw_market_data_retention_days: Annotated[int, Field(ge=1)] = 7
    backup_target: str | None = None
    max_clock_skew_seconds: Annotated[int, Field(ge=1, le=600)] = 5
    day_boundary_tz: str = "Asia/Shanghai"
    valuation: ValuationPolicy
    #: 首次接入的独立风险起点；缺失时系统保持暂停开仓（LED-04）。
    risk_start_at: str | None = None
    disk_free_threshold_gb: Annotated[int, Field(ge=1)] = 10
    disk_pause_threshold_gb: Annotated[int, Field(ge=1)] = 5

    @field_validator("alert_channels")
    @classmethod
    def _channels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 3 or any("://" in value for value in values):
            raise ValueError("通知渠道最多三个，只能引用受限配置文件，不能内嵌 URL 或密钥")
        return values

    @field_validator("day_boundary_tz")
    @classmethod
    def _timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("risk_start_at")
    @classmethod
    def _start(cls, value: str | None) -> str | None:
        if value is not None and dt.datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("risk_start_at 必须带时区")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> OpsConfig:
        if self.disk_pause_threshold_gb >= self.disk_free_threshold_gb:
            raise ValueError("磁盘暂停阈值必须低于告警阈值")
        return self


# --------------------------------------------------------------------------
# 实盘启用门槛（ENV-03）
# --------------------------------------------------------------------------


class FeeEstimationConfig(BaseModel):
    """手续费估算口径（版本化）。

    CR-02：费率不能只藏在 ``paper`` 配置里 —— 同一口径必须同时用于模拟撮合、
    仓位测算、预算校验和止盈判断。``version`` 一变，历史结果不可比，因此
    版本号随快照与报告一起记录。

    期权费用按官方公式估算：``min(标的指数价 × option_underlying_rate,
    权利金 × option_premium_cap_rate) × 数量``。缺指数价时按封顶比例计，
    属于保守方向。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "bybit-fee-v1"
    #: 现货 taker 费率（按成交额计收）。
    spot_taker_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.001")
    #: 期权：标的指数价费率（Bybit 非 VIP taker 为 0.03%）。
    option_underlying_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0003")
    #: 期权：权利金封顶比例（官方公式上界 7%）。
    option_premium_cap_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.07")
    #: 期权每张合约的额外费用。
    option_per_contract: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0")

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("费用口径版本号不能为空")
        return value


class PaperConfig(BaseModel):
    """独立模拟资金，不读取交易密钥（STR-05）。

    费用口径不在这里：模拟与实盘必须共用同一套费率（CR-02），
    统一由顶层 ``fees`` 段提供。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    initial_balances: dict[str, Decimal] = Field(default_factory=lambda: {"USDT": Decimal("1000")})
    public_market_url: Literal["https://api.bybit.com"] = "https://api.bybit.com"

    @field_validator("initial_balances")
    @classmethod
    def _balances(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        if any(not value.is_finite() or value < 0 for value in values.values()):
            raise ValueError("模拟初始资金必须是非负有限数")
        return {key.upper(): value for key, value in values.items()}


class RuntimeConfig(BaseModel):
    """低中频服务运行参数；期权真实能力须有独立验证记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    proxy: str | None = None
    poll_seconds: Annotated[int, Field(ge=2, le=60)] = 5
    catalog_refresh_seconds: Annotated[int, Field(ge=300)] = 3600
    max_option_candidates: Annotated[int, Field(ge=1, le=200)] = 200
    option_units_verified: bool = False
    option_reduce_only_verified: bool = False
    option_capability_record_ref: str | None = None

    @model_validator(mode="after")
    def _capabilities(self) -> RuntimeConfig:
        if (self.option_units_verified or self.option_reduce_only_verified) and not (
            self.option_capability_record_ref
        ):
            raise ValueError("期权能力启用必须提供验证记录引用")
        return self


class LiveGateConfig(BaseModel):
    """实盘发单资格。

    ENV-03：实盘必须同时具备独立的实盘允许配置、完整风控参数、
    相应阶段验收记录及人工启用操作。缺一不可。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 独立的实盘允许开关，与环境选择分离。
    live_allowed: bool = False
    #: 相应阶段的验收记录标识（M4a / M4b）。
    acceptance_record_ref: str | None = None
    #: 人工启用操作记录（操作者、时间、范围）。
    manual_enable_ref: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> LiveGateConfig:
        if self.live_allowed and not (self.acceptance_record_ref and self.manual_enable_ref):
            raise ValueError("实盘允许开关打开时必须同时提供验收记录与人工启用记录（ENV-03）")
        return self
