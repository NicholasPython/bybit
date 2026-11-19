"""配置加载与校验。

配置来源（按优先级从低到高）：
    1. ``config/config.yaml`` —— 非密配置，含各环境档案与策略定义
    2. 环境变量 ``BYBITBOT_*``  —— 覆盖少量部署期参数（主要是 DSN）

密钥不在这里：SEC-01 要求密钥仅供交易适配器使用，通过受限配置文件注入，
不进 Git、不进日志。凭据文件由 :mod:`bybitbot.settings.secrets` 按环境档案的
``credential_ref`` 读取，且只在允许装载凭据的环境里才会被打开。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from bybitbot.fees import FeeModel
from bybitbot.settings.models import (
    FIRST_PHASE_MAX_OPTION_CANDIDATES,
    FIRST_PHASE_MAX_SPOT_PAIRS,
    FIRST_PHASE_MAX_STRATEGIES,
    AccountConfig,
    BudgetConfig,
    Environment,
    EnvironmentProfile,
    ExecutionConfig,
    FeeEstimationConfig,
    LiveGateConfig,
    MarketConfig,
    OpsConfig,
    OptionConfig,
    PaperConfig,
    RiskLimits,
    RuntimeConfig,
    StrategyConfig,
)

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class AppConfig(BaseModel):
    """顶层配置对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_environment: Environment
    environments: dict[Environment, EnvironmentProfile]
    account: AccountConfig
    budget: BudgetConfig
    risk: RiskLimits
    market: MarketConfig
    execution: ExecutionConfig
    option: OptionConfig
    ops: OpsConfig
    live_gates: LiveGateConfig = LiveGateConfig()
    paper: PaperConfig = PaperConfig()
    #: 手续费估算口径（模拟与实盘共用，见 CR-02）。
    fees: FeeEstimationConfig = FeeEstimationConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    strategies: tuple[StrategyConfig, ...] = ()

    @model_validator(mode="after")
    def _first_phase_capacity(self) -> AppConfig:
        """首期容量校验（NFR-01）。

        首期容量是**已验证**的运行边界：1 个账户、最多 5 个策略实例、
        10 个现货交易对、200 个候选期权合约。超出边界的部署必须另做容量
        及订阅验证 —— 系统在这里直接拒绝，而不是悄悄跑在未验证的容量上。
        """
        if len(self.strategies) > FIRST_PHASE_MAX_STRATEGIES:
            raise ValueError(
                f"策略实例 {len(self.strategies)} 个超过首期容量上限 "
                f"{FIRST_PHASE_MAX_STRATEGIES} 个（NFR-01）；超出需另做容量及订阅验证"
            )
        spot_pairs = {
            symbol
            for cfg in self.strategies
            if cfg.kind == "spot_fixed_budget_dca"
            for symbol in cfg.symbols
        }
        if len(spot_pairs) > FIRST_PHASE_MAX_SPOT_PAIRS:
            raise ValueError(
                f"现货交易对 {len(spot_pairs)} 个超过首期容量上限 "
                f"{FIRST_PHASE_MAX_SPOT_PAIRS} 个（NFR-01）；超出需另做容量及订阅验证"
            )
        if self.runtime.max_option_candidates > FIRST_PHASE_MAX_OPTION_CANDIDATES:
            raise ValueError(
                f"候选期权合约 {self.runtime.max_option_candidates} 个超过首期容量上限 "
                f"{FIRST_PHASE_MAX_OPTION_CANDIDATES} 个（NFR-01）"
            )
        return self

    @property
    def profile(self) -> EnvironmentProfile:
        return self.environments[self.active_environment]

    @property
    def fee_model(self) -> FeeModel:
        """当前配置对应的费用估算口径。"""
        return FeeModel(
            version=self.fees.version,
            spot_taker_rate=self.fees.spot_taker_rate,
            option_underlying_rate=self.fees.option_underlying_rate,
            option_premium_cap_rate=self.fees.option_premium_cap_rate,
            option_per_contract=self.fees.option_per_contract,
        )

    def strategy(self, name: str) -> StrategyConfig:
        for s in self.strategies:
            if s.name == name:
                return s
        raise KeyError(f"未找到策略实例 {name!r}")


class ConfigError(Exception):
    """配置无法通过校验。附带 pydantic 的逐项错误，便于定位。"""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"配置文件不存在：{path}\n请从 config/config.example.yaml 复制一份并按 §5.2 填写。"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 依赖具体 YAML 错误
        raise ConfigError(f"配置文件不是合法 YAML：{path}\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{path}")
    return raw


def load_config(
    path: Path | str | None = None,
    env_override: Environment | str | None = None,
) -> AppConfig:
    """读取并校验配置。

    Args:
        path: 配置文件路径，默认 ``config/config.yaml``。
        env_override: 强制使用的环境；优先级高于配置文件里的
            ``active_environment``。部署时通常由 ``BYBITBOT_ENV`` 传入。
    """
    cfg_path = Path(path or os.environ.get("BYBITBOT_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = _read_yaml(cfg_path)
    unknown = raw.keys() - AppConfig.model_fields.keys()
    if unknown:
        raise ConfigError(f"未知顶层配置字段：{', '.join(sorted(unknown))}")

    environments_raw = raw.get("environments") or {}
    if not environments_raw:
        raise ConfigError("配置缺少 environments 段（至少要定义 paper）")

    try:
        environments = {
            Environment(key): EnvironmentProfile.model_validate(value)
            for key, value in environments_raw.items()
        }
    except ValidationError as exc:
        raise ConfigError(f"environments 段校验失败：\n{exc}") from exc

    active_raw = (
        env_override
        or os.environ.get("BYBITBOT_ENV")
        or raw.get("active_environment", Environment.PAPER)
    )
    try:
        active = Environment(active_raw)
    except ValueError as exc:
        raise ConfigError(
            f"未知环境 {active_raw!r}，可选：{[e.value for e in Environment]}"
        ) from exc

    if active not in environments:
        raise ConfigError(f"配置里没有 {active.value} 环境档案")

    # 部署期允许用环境变量覆盖数据库 DSN（每个容器一套库）。
    dsn_override = os.environ.get("BYBITBOT_DB_DSN")
    if dsn_override:
        environments = {
            k: (v.model_copy(update={"db_dsn": dsn_override}) if k is active else v)
            for k, v in environments.items()
        }

    try:
        account = AccountConfig.model_validate({**raw.get("account", {}), "environment": active})
        budget = BudgetConfig.model_validate(raw.get("budget") or {})
        risk = RiskLimits.model_validate(raw.get("risk") or {})
        market = MarketConfig.model_validate(raw.get("market") or {})
        execution = ExecutionConfig.model_validate(raw.get("execution") or {})
        option = OptionConfig.model_validate(raw.get("option") or {})
        ops = OpsConfig.model_validate(raw.get("ops") or {})
        live_gates = LiveGateConfig.model_validate(raw.get("live_gates") or {})
        paper = PaperConfig.model_validate(raw.get("paper") or {})
        fees = FeeEstimationConfig.model_validate(raw.get("fees") or {})
        runtime = RuntimeConfig.model_validate(raw.get("runtime") or {})
        strategies = tuple(StrategyConfig.model_validate(s) for s in (raw.get("strategies") or []))
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败：\n{exc}") from exc

    try:
        cfg = AppConfig(
            active_environment=active,
            environments=environments,
            account=account,
            budget=budget,
            risk=risk,
            market=market,
            execution=execution,
            option=option,
            ops=ops,
            live_gates=live_gates,
            paper=paper,
            fees=fees,
            runtime=runtime,
            strategies=strategies,
        )
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败：\n{exc}") from exc
    _cross_validate(cfg)
    return cfg


def _cross_validate(cfg: AppConfig) -> None:
    """跨段一致性检查 —— 单元校验看不到的关系在这里兜底。"""
    profile = cfg.profile
    account = cfg.account
    if cfg.active_environment is not Environment.PAPER and cfg.paper.market_source == "synthetic":
        raise ConfigError("合成行情只允许用于 paper 环境")

    if account.allowed_sites and profile.site not in account.allowed_sites:
        raise ConfigError(
            f"环境 {profile.environment.value} 的站点 {profile.site!r} 不在 "
            f"allowed_sites {sorted(account.allowed_sites)} 内（ENV-02）"
        )

    if profile.environment is Environment.LIVE:
        if account.expected_uid is None:
            raise ConfigError("实盘必须绑定 expected_uid（ENV-02 / §5.2）")
        if not profile.allow_trade_writes:
            raise ConfigError(
                "实盘环境的 allow_trade_writes 必须为 True；"
                "实盘发单资格另由 LiveGateConfig 控制（ENV-03）"
            )

    if profile.environment is Environment.PAPER and account.expected_uid is None:
        # paper 不需要真实账户身份，这是正常路径。
        pass

    valuation_currency = cfg.ops.valuation.currency
    for name, amount in (
        ("max_spend_per_trade", cfg.budget.max_spend_per_trade),
        ("max_spot_exposure_per_strategy", cfg.budget.max_spot_exposure_per_strategy),
        ("max_spot_exposure_account", cfg.budget.max_spot_exposure_account),
        ("option_premium_budget", cfg.budget.option_premium_budget),
        ("fee_buffer", cfg.budget.fee_buffer),
    ):
        if amount.currency != valuation_currency:
            raise ConfigError(
                f"{name} 的币种 {amount.currency} 与估值币种 "
                f"{valuation_currency} 不一致（§5.2 / LED-04）"
            )

    if len(cfg.strategies) > 5 or len({s.name for s in cfg.strategies}) != len(cfg.strategies):
        raise ConfigError("首期最多 5 个策略，名称不得重复（NFR-01）")
    for key, item in cfg.environments.items():
        if key != item.environment:
            raise ConfigError("环境档案的键与 environment 不一致（ENV-01）")
    if len({p.db_dsn for p in cfg.environments.values()}) != len(cfg.environments):
        raise ConfigError("各环境必须使用独立数据库（ENV-01）")
    for amount in (
        cfg.risk.max_daily_loss,
        cfg.risk.max_drawdown,
        cfg.execution.max_intent_total_spend,
    ):
        if amount.currency != valuation_currency:
            raise ConfigError("资金、损失和总支出上限必须使用统一估值币种")
    if profile.environment is Environment.LIVE and cfg.ops.max_clock_skew_seconds >= 5:
        raise ConfigError("实盘最大时钟偏差必须小于 5 秒请求签名窗口")
    if cfg.option.no_open_window_order_ttl_seconds >= cfg.execution.order_ttl_seconds:
        raise ConfigError("禁开仓窗口内的买单 TTL 必须短于常规订单 TTL（OPT-04）")


__all__ = [
    "AppConfig",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "load_config",
]
