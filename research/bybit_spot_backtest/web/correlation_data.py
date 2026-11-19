"""Read the precomputed public-market research snapshot; no runtime API calls."""

import json
import math

DOWNLOADS = {
    "top20-ranking.csv": "text/csv; charset=utf-8",
    "correlation-30.csv": "text/csv; charset=utf-8",
    "correlation-90.csv": "text/csv; charset=utf-8",
    "correlation-common-161h.csv": "text/csv; charset=utf-8",
    "pair-correlations.csv": "text/csv; charset=utf-8",
    "analysis.json": "application/json",
    "report.md": "text/markdown; charset=utf-8",
    "bybit-top20-analysis.zip": "application/zip",
}


def load_analysis(directory):
    def reject_constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")

    data = json.loads(
        (directory / "analysis.json").read_text(encoding="utf-8"), parse_constant=reject_constant
    )
    coins = data["coins"]
    if len(coins) != 20 or len(set(coins)) != 20:
        raise ValueError("Expected twenty unique instruments")
    if [row["symbol"] for row in data["ranking"]] != coins:
        raise ValueError("Ranking and matrix instrument order differ")
    for days in ("30", "90"):
        matrix = data["matrices"][days]
        if len(matrix) != 20 or any(len(row) != 20 for row in matrix):
            raise ValueError("Invalid matrix dimensions")
        for i, row in enumerate(matrix):
            for j, value in enumerate(row):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not -1.0000001 <= value <= 1.0000001
                ):
                    raise ValueError("Invalid correlation coefficient")
                if value != matrix[j][i]:
                    raise ValueError("Asymmetric matrix")
    return data


def public_analysis(data):
    return {
        key: data[key]
        for key in (
            "snapshot",
            "analysis_end_ms",
            "analysis_end_cst",
            "windows_cst",
            "method",
            "ranking",
            "coins",
            "matrices",
            "pairs",
            "common_short_window",
            "exploratory_clusters_90d",
            "independent_validation",
        )
    }
