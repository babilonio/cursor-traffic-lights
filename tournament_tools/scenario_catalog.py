"""Synthetic scenarios for local controller validation."""

from __future__ import annotations

from dataclasses import dataclass

from .common import ensure_traffic_arena_importable


ensure_traffic_arena_importable()

from traffic_arena.scenarios import DemandWindow, Scenario  # noqa: E402


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """A scenario and the tournament family it represents."""

    family: str
    variant: str
    scenario: Scenario
    held_out: bool


_DEVELOPMENT_SEEDS = (
    11_083,
    17_021,
    23_059,
    29_083,
    31_091,
    37_103,
    41_129,
    43_163,
    47_171,
    53_201,
    59_219,
    61_237,
)

_HELD_OUT_SEEDS = (
    101_419,
    103_471,
    107_507,
    109_537,
    113_567,
    127_601,
    131_617,
    137_653,
    139_681,
    149_701,
    151_739,
    157_771,
)


def _window(
    start: int,
    end: int,
    *,
    ns_rate: float,
    ew_rate: float,
    row_weights: tuple[float, ...] = (),
    col_weights: tuple[float, ...] = (),
) -> DemandWindow:
    return DemandWindow(
        start,
        end,
        north_rate=ns_rate,
        south_rate=ns_rate,
        east_rate=ew_rate,
        west_rate=ew_rate,
        row_weights=row_weights,
        col_weights=col_weights,
    )


def _bursty_windows(period: int, *, starts_ns_heavy: bool) -> tuple[DemandWindow, ...]:
    windows: list[DemandWindow] = []
    for start in range(0, 900, period):
        regime = start // period
        ns_heavy = (regime % 2 == 0) == starts_ns_heavy
        windows.append(
            _window(
                start,
                min(start + period, 900),
                ns_rate=0.23 if ns_heavy else 0.07,
                ew_rate=0.07 if ns_heavy else 0.23,
            )
        )
    return tuple(windows)


def _scenario_specs(seed: tuple[int, ...], suffix: str) -> tuple[ScenarioCase, ...]:
    held_out = suffix == "held-out"

    def case(family: str, variant: str, index: int, **kwargs: object) -> ScenarioCase:
        scenario_id = f"local-{family}-{variant}-{suffix}"
        scenario = Scenario(
            id=scenario_id,
            name=f"{family.title()} / {variant} ({suffix})",
            seed=seed[index],
            ticks=900,
            **kwargs,
        )
        return ScenarioCase(family, variant, scenario, held_out)

    return (
        case(
            "balanced",
            "steady-2x2",
            0,
            rows=2,
            cols=2,
            horizontal_rate=0.12,
            vertical_rate=0.12,
        ),
        case(
            "balanced",
            "steady-3x3",
            1,
            rows=3,
            cols=3,
            horizontal_rate=0.16,
            vertical_rate=0.16,
        ),
        case(
            "directional",
            "ns-heavy",
            2,
            rows=3,
            cols=3,
            horizontal_rate=0.07,
            vertical_rate=0.23,
        ),
        case(
            "directional",
            "ew-heavy",
            3,
            rows=3,
            cols=3,
            horizontal_rate=0.23,
            vertical_rate=0.07,
        ),
        case(
            "reversal",
            "ns-to-ew",
            4,
            rows=3,
            cols=3,
            demand_windows=(
                _window(0, 450, ns_rate=0.23, ew_rate=0.07),
                _window(450, 900, ns_rate=0.07, ew_rate=0.23),
            ),
        ),
        case(
            "reversal",
            "ew-to-ns",
            5,
            rows=3,
            cols=3,
            demand_windows=(
                _window(0, 450, ns_rate=0.07, ew_rate=0.23),
                _window(450, 900, ns_rate=0.23, ew_rate=0.07),
            ),
        ),
        case(
            "lane-hotspot",
            "edge-rotation-3x3",
            6,
            rows=3,
            cols=3,
            demand_windows=(
                _window(
                    0,
                    450,
                    ns_rate=0.11,
                    ew_rate=0.11,
                    row_weights=(2.2, 0.4, 0.4),
                    col_weights=(0.4, 0.4, 2.2),
                ),
                _window(
                    450,
                    900,
                    ns_rate=0.11,
                    ew_rate=0.11,
                    row_weights=(0.4, 0.4, 2.2),
                    col_weights=(2.2, 0.4, 0.4),
                ),
            ),
        ),
        case(
            "lane-hotspot",
            "edge-rotation-4x4",
            7,
            rows=4,
            cols=4,
            demand_windows=(
                _window(
                    0,
                    450,
                    ns_rate=0.10,
                    ew_rate=0.10,
                    row_weights=(2.8, 0.4, 0.4, 0.4),
                    col_weights=(0.4, 0.4, 0.4, 2.8),
                ),
                _window(
                    450,
                    900,
                    ns_rate=0.10,
                    ew_rate=0.10,
                    row_weights=(0.4, 0.4, 0.4, 2.8),
                    col_weights=(2.8, 0.4, 0.4, 0.4),
                ),
            ),
        ),
        case(
            "bursty",
            "alternating-90",
            8,
            rows=3,
            cols=3,
            demand_windows=_bursty_windows(90, starts_ns_heavy=True),
        ),
        case(
            "bursty",
            "alternating-75",
            9,
            rows=3,
            cols=3,
            demand_windows=_bursty_windows(75, starts_ns_heavy=False),
        ),
        case(
            "spillback",
            "tight-3x3",
            10,
            rows=3,
            cols=3,
            travel_ticks=9,
            link_capacity=3,
            horizontal_rate=0.16,
            vertical_rate=0.16,
        ),
        case(
            "spillback",
            "tight-4x4",
            11,
            rows=4,
            cols=4,
            travel_ticks=12,
            link_capacity=5,
            horizontal_rate=0.15,
            vertical_rate=0.15,
        ),
    )


def build_validation_suite(held_out: bool = False) -> tuple[ScenarioCase, ...]:
    """Build the twelve development maps or their held-out seed variants."""

    if held_out:
        return _scenario_specs(_HELD_OUT_SEEDS, "held-out")
    return _scenario_specs(_DEVELOPMENT_SEEDS, "development")
