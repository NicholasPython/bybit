"""关键路径耗时记录（NFR-02 / AC-22）。

需求原文：
    **NFR-02** 在指定容量、4 vCPU/8GB 参考环境、数据库正常且不含外部网络/API等待时，
    意图校验与持久化预留处理 P95 ≤ 500ms。

系统必须能**记录**自己的延迟分布，否则性能基线无从复核。本模块在进程内保存
最近若干个样本，按名称给出计数与分位数摘要；摘要由服务写入运行状态，
因此重启后仍能在操作台看到最近的分布。

样本是进程内的滚动窗口 —— 它回答"最近这段时间有多慢"，不回答历史全量分布；
需要长期基线时应把摘要导出归档。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class LatencySummary:
    """一个指标名的延迟分布摘要。"""

    name: str
    count: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "count": self.count,
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
        }


class LatencyRecorder:
    """滚动窗口延迟记录器。

    Args:
        max_samples: 每个指标名保留的样本数上限。
    """

    def __init__(self, *, max_samples: int = 500) -> None:
        if max_samples < 1:
            raise ValueError("样本上限必须为正数")
        self._max_samples = max_samples
        self._windows: dict[str, deque[float]] = {}

    def record(self, name: str, seconds: float) -> None:
        """记录一次耗时（秒）。"""
        if seconds < 0:
            raise ValueError("耗时不能为负")
        window = self._windows.setdefault(name, deque(maxlen=self._max_samples))
        window.append(seconds * 1000.0)

    def summary(self, name: str) -> LatencySummary | None:
        """某个指标名的分布摘要；没有样本时返回 ``None``。"""
        window = self._windows.get(name)
        if not window:
            return None
        return _summarize(name, sorted(window))

    def summaries(self) -> list[LatencySummary]:
        """全部指标名的分布摘要，按名称排序。"""
        return [
            summary
            for name in sorted(self._windows)
            if (summary := self.summary(name)) is not None
        ]

    def as_dict(self) -> dict[str, object]:
        return {summary.name: summary.as_dict() for summary in self.summaries()}


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """最近秩法分位数；样本为空时返回 0。"""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _summarize(name: str, values: list[float]) -> LatencySummary:
    return LatencySummary(
        name=name,
        count=len(values),
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        max_ms=values[-1],
        mean_ms=sum(values) / len(values),
    )


__all__ = ["LatencyRecorder", "LatencySummary"]
