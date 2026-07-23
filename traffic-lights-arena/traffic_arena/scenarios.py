from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemandWindow:
    """Direction- and lane-specific demand over ``[start_tick, end_tick)``."""

    start_tick: int
    end_tick: int
    north_rate: float
    south_rate: float
    east_rate: float
    west_rate: float
    row_weights: tuple[float, ...] = ()
    col_weights: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.start_tick < 0:
            raise ValueError("demand window start_tick must be nonnegative")
        if self.end_tick <= self.start_tick:
            raise ValueError("demand window end_tick must be greater than start_tick")
        rates = (self.north_rate, self.south_rate, self.east_rate, self.west_rate)
        if any(rate < 0 for rate in rates):
            raise ValueError("demand window rates must be nonnegative")
        if any(weight < 0 for weight in (*self.row_weights, *self.col_weights)):
            raise ValueError("demand window weights must be nonnegative")


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    name: str
    rows: int
    cols: int
    seed: int
    ticks: int = 900
    travel_ticks: int = 5
    link_capacity: int = 8
    horizontal_rate: float = 0.13
    vertical_rate: float = 0.13
    rush_axis: str | None = None
    burst_period: int = 0
    demand_windows: tuple[DemandWindow, ...] = ()

    def __post_init__(self) -> None:
        if self.rows <= 0 or self.cols <= 0 or self.ticks <= 0:
            raise ValueError("scenario dimensions and ticks must be positive")
        if not self.demand_windows:
            return
        next_tick = 0
        for window in self.demand_windows:
            if window.start_tick != next_tick:
                raise ValueError("demand windows must cover the scenario without gaps or overlap")
            if window.row_weights and len(window.row_weights) != self.rows:
                raise ValueError("demand window row_weights must be empty or match scenario rows")
            if window.col_weights and len(window.col_weights) != self.cols:
                raise ValueError("demand window col_weights must be empty or match scenario cols")
            next_tick = window.end_tick
        if next_tick != self.ticks:
            raise ValueError("demand windows must exactly cover [0, ticks)")


PUBLIC_SCENARIOS = (
    Scenario("balanced-grid", "Balanced grid", 2, 2, 1403),
    Scenario(
        "northbound-morning",
        "Northbound morning",
        3,
        2,
        8191,
        vertical_rate=0.22,
        horizontal_rate=0.09,
        rush_axis="NS",
    ),
    Scenario(
        "city-rush",
        "City rush",
        3,
        3,
        27183,
        horizontal_rate=0.17,
        vertical_rate=0.17,
        burst_period=90,
    ),
)
