"""Bull strength signal over the shared, validated public observation reader."""

from pathlib import Path

from bybitbot.marketdata import DAY, HOUR, load_snapshot, verify_snapshot
from bybitbot.marketdata import collect as collect_snapshot


def bull_signal(hours: list[tuple], days: list[tuple]) -> dict:
    hour_close, day_close = hours[-1][1], days[-1][1]
    daily = [r[1] for r in days]
    sma20 = sum(daily[-20:]) / 20
    previous = sum(daily[-21:-1]) / 20
    sma50 = sum(daily[-50:]) / 50
    last3 = [r[1] for r in hours[-3:]]
    ret24 = hour_close / hours[-25][1] - 1
    return {
        "eligible": bool(
            day_close > sma20
            and sma20 >= previous
            and all(c > sma20 and c > sma50 for c in last3)
            and ret24 > 0
        ),
        "hourClose": hour_close,
        "dailyClose": day_close,
        "sma20": sma20,
        "previousSma20": previous,
        "sma50": sma50,
        "return24h": ret24,
        "last3HourlyCloses": last3,
        "lastHourMs": hours[-1][0] + HOUR,
        "lastDayMs": days[-1][0] + DAY,
    }


def load_market(manifest_path: Path, max_age_seconds=30) -> dict:
    observation = load_snapshot(manifest_path, max_age_seconds, minimum_hours=25, minimum_days=51)
    return {
        "ms": observation["ms"],
        "options": observation["options"],
        "signals": {
            base: bull_signal(rows["hours"], rows["days"])
            for base, rows in observation["candles"].items()
        },
    }


def collect(output_parent: Path) -> Path:
    path = collect_snapshot(output_parent)
    load_market(path)
    return path


__all__ = ["bull_signal", "collect", "load_market", "verify_snapshot"]
