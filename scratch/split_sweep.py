"""Empirical cost of static NS/EW split cycles on one synthetic case."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "traffic-lights-arena"))

from traffic_arena.engine import run_scenario
from tournament_tools.scenario_catalog import build_validation_suite


def make_static(ns_green: int, ew_green: int):
    cycle = ns_green + ew_green

    def control(state):
        phase = "NS_GREEN" if state["tick"] % cycle < ns_green else "EW_GREEN"
        return {item: phase for item in state["intersections"]}

    return control


def main() -> None:
    family, variant = sys.argv[1], sys.argv[2]
    case = next(
        c
        for c in build_validation_suite(held_out=False)
        if c.family == family and c.variant == variant
    )
    for ns, ew in [(15, 15), (18, 12), (21, 9), (24, 12), (28, 14), (12, 18), (9, 21), (20, 16), (16, 20), (24, 8), (30, 10)]:
        result = run_scenario(case.scenario, make_static(ns, ew), record_replay=False)
        m = result.metrics
        print(f"NS={ns:2d} EW={ew:2d} cost={m.cost:7d} wait={m.wait_ticks:7d} unf={m.unfinished:4d}")


if __name__ == "__main__":
    main()
