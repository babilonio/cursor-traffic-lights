"""Run the probe controller on one synthetic case and summarize its internals."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "traffic-lights-arena"))
sys.path.insert(0, str(REPO / "scratch"))

from traffic_arena.engine import run_scenario
from traffic_arena.scenarios import PUBLIC_SCENARIOS
from tournament_tools.scenario_catalog import build_validation_suite

import controller_probe


def summarize(rows: list[dict], start: int, end: int) -> str:
    window = [r for r in rows if start <= r["tick"] < end]
    if not window:
        return "(empty)"
    n = len(window)
    ns_green = sum(1 for r in window if r["requested"] == "NS")
    mean = lambda key: sum(r[key] for r in window) / n
    modes = {}
    for r in window:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    return (
        f"dNS={mean('demand_ns'):.3f} dEW={mean('demand_ew'):.3f} "
        f"tNS={mean('target_ns'):.1f} tEW={mean('target_ew'):.1f} "
        f"reqNS={ns_green/n:.2f} "
        f"pNS={mean('pressure_ns'):.1f} pEW={mean('pressure_ew'):.1f} "
        f"qNS={mean('queue_ns'):.1f} qEW={mean('queue_ew'):.1f} "
        f"spill={mean('spillback'):.2f} ewma={mean('share_ewma'):.3f} "
        f"modes={modes}"
    )


def main() -> None:
    family, variant = sys.argv[1], sys.argv[2]
    if family == "public":
        scenario = next(s for s in PUBLIC_SCENARIOS if s.id == variant)
    else:
        scenario = next(
            c
            for c in build_validation_suite(held_out=False)
            if c.family == family and c.variant == variant
        ).scenario
    result = run_scenario(scenario, controller_probe.control, record_replay=False)
    print(f"{scenario.id}: cost={result.metrics.cost} wait={result.metrics.wait_ticks} unf={result.metrics.unfinished}")
    rows = controller_probe.PROBE_LOG
    for start in range(0, 900, 100):
        print(f"[{start:3d}-{start + 100:3d})", summarize(rows, start, start + 100))


if __name__ == "__main__":
    main()
