from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


PUBLIC_WEIGHT = 0.2
HIDDEN_WEIGHT = 1 - PUBLIC_WEIGHT


@dataclass(frozen=True, slots=True)
class ScoreProfile:
    baseline_cost: int
    target_cost: int
    expected_spawned: int

    def __post_init__(self) -> None:
        if self.target_cost <= 0:
            raise ValueError("target_cost must be positive")
        if self.baseline_cost <= self.target_cost:
            raise ValueError("baseline_cost must be greater than target_cost")
        if self.expected_spawned <= 0:
            raise ValueError("expected_spawned must be positive")


def scenario_score(cost: int, baseline_cost: int, target_cost: int) -> int:
    """Map a scenario cost onto the fixed 1..25,000 competition scale."""
    if cost <= 0:
        raise ValueError("cost must be positive")
    if target_cost <= 0:
        raise ValueError("target_cost must be positive")
    if baseline_cost <= target_cost:
        raise ValueError("baseline_cost must be greater than target_cost")

    if cost >= baseline_cost:
        return max(1, round(10_000 * baseline_cost / cost))

    progress = min(1.0, (baseline_cost - cost) / (baseline_cost - target_cost))
    return round(10_000 + 15_000 * progress**2)


def geometric_mean(scores: Sequence[int]) -> int:
    if not scores:
        raise ValueError("at least one score is required")
    if any(score <= 0 for score in scores):
        return 0
    return round(math.exp(sum(math.log(score) for score in scores) / len(scores)))


def final_total_score(public_score: int, hidden_score: int) -> int:
    return round(public_score * PUBLIC_WEIGHT + hidden_score * HIDDEN_WEIGHT)


def aggregate_scores(public_scores: Sequence[int], hidden_scores: Sequence[int]) -> tuple[int, int, int]:
    public = geometric_mean(public_scores)
    hidden = geometric_mean(hidden_scores)
    return public, hidden, final_total_score(public, hidden)
