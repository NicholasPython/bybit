"""公共层：合约条款与费用口径。

这里放**控制台与研究侧必须一致**的东西：期权符号的解释、成交过滤条件的
提取、以及 Bybit 的费用公式。

放在 ``bybitbot`` 包内而不是研究目录里，是为了让它随控制台一起部署：
研究脚本在服务器上跑时用的是同一份代码，不存在"研究口径和线上口径各一份"
的问题。

依赖约束：本子包只用标准库与 ``Decimal``，不引入 ORM、HTTP 客户端或
Web 框架，因此可以被任何一侧安全导入。
"""

from bybitbot.terms.fees import delivery_fee, option_fee, spot_fee
from bybitbot.terms.specs import (
    OptionFilters,
    OptionSpec,
    option_filters,
    option_spec,
)
from bybitbot.terms.symbols import (
    DELIVERY_HOUR_UTC,
    OptionTerms,
    option_symbol,
    parse_option_symbol,
    require_option_symbol,
)
from bybitbot.terms.valuation import ValuationPolicy, option_value

__all__ = [
    "DELIVERY_HOUR_UTC",
    "OptionFilters",
    "OptionSpec",
    "OptionTerms",
    "ValuationPolicy",
    "delivery_fee",
    "option_filters",
    "option_fee",
    "option_spec",
    "option_symbol",
    "option_value",
    "parse_option_symbol",
    "require_option_symbol",
    "spot_fee",
]
