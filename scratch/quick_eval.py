"""Fast focused evaluation of a controller file on selected scenarios."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tournament_tools.benchmark import run_benchmark_case, build_cases
from tournament_tools.scenario_catalog import build_validation_suite

FOCUS = [
    ("balanced", "steady-2x2"),
    ("spillback", "tight-3x3"),
    ("spillback", "tight-4x4"),
    ("lane-hotspot", "edge-rotation-4x4"),
    ("lane-hotspot", "edge-rotation-3x3"),
    ("reversal", "ns-to-ew"),
    ("bursty", "alternating-75"),
]


def main() -> None:
    controller = sys.argv[1]
    full = len(sys.argv) > 2 and sys.argv[2] == "full"
    cases = list(build_cases("public"))
    dev = build_validation_suite(held_out=False)
    if full:
        cases += list(dev)
    else:
        cases += [c for c in dev if (c.family, c.variant) in FOCUS]
    for case in cases:
        r = run_benchmark_case(controller, case, candidate_id="quick")
        ratio = r.baseline_cost / r.cost
        extra = f" score={r.public_score}" if r.public_score is not None else ""
        print(
            f"{r.scenario_id:52s} ratio={ratio:.4f} cost={r.cost:6d} "
            f"wait={r.wait_ticks:6d} unf={r.unfinished:3d}{extra}"
        )


if __name__ == "__main__":
    main()
