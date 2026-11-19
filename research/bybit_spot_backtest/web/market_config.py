"""Fixed Top20 observation universe, shared by collector, web and worker.

Snapshot taken 2026-11-19 from Bybit V5 public ``/v5/market/tickers``: USDT
spot pairs ranked by 24h turnover, with stablecoin pairs (USDC, FDUSD, USD1,
RLUSD, USDY, ...) and liquid-staking receipts (STETH, BBSOL, ...) removed.
``XAUT`` is Tether Gold rather than a stablecoin, so it stays in the universe.

This is a fixed, post-hoc snapshot (survivorship bias); it never follows live
rankings. The collector keeps every symbol here so a missing listing or halted
instrument is reported, never silently replaced.
"""

TOP20_SNAPSHOT_DATE = "2026-11-19"
BASES = (
    "BTC",
    "ETH",
    "SOL",
    "AAVE",
    "HYPE",
    "ENA",
    "XRP",
    "LINK",
    "ASTER",
    "PIEVERSE",
    "NEAR",
    "MNT",
    "WLD",
    "XAUT",
    "BNB",
    "VVV",
    "GRAM",
    "LIT",
    "ZRO",
    "HOODX",
)
INSTRUMENTS = tuple(f"{base}USDT" for base in BASES)
# Outside the Top20 but listed on Bybit spot: valid as a buy-and-hold
# comparison, never collected and never selectable as a portfolio member.
LEGACY_INSTRUMENTS = (
    "DOGEUSDT",
    "PEPEUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "TRUMPUSDT",
    "UNIUSDT",
    "FILUSDT",
    "ATOMUSDT",
    "SHIBUSDT",
)
SUPPORTED_INSTRUMENTS = INSTRUMENTS + LEGACY_INSTRUMENTS
