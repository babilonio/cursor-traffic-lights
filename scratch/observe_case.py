"""Run one synthetic case with the observer and print a diagnostics digest."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tournament_tools.benchmark import run_benchmark_case
from tournament_tools.scenario_catalog import build_validation_suite


def main() -> None:
    controller = sys.argv[1]
    family = sys.argv[2]
    variant = sys.argv[3]
    held_out = len(sys.argv) > 4 and sys.argv[4] == "held-out"
    cases = [
        case
        for case in build_validation_suite(held_out=held_out)
        if case.family == family and case.variant == variant
    ]
    if not cases:
        raise SystemExit(f"no case for {family}/{variant}")
    record = run_benchmark_case(controller, cases[0], candidate_id="observed", observe=True)
    d = record.diagnostics or {}
    ticks = d.get("ticks_observed", 0)
    n_int = len(d.get("map", {}).get("intersection_ids", []))
    print(f"scenario={record.scenario_id}")
    print(f"cost={record.cost} baseline={record.baseline_cost} ratio={record.baseline_cost/record.cost:.4f}")
    print(f"wait={record.wait_ticks} unfinished={record.unfinished} spawned={record.spawned} completed={record.completed}")
    print(f"switch_starts={d.get('inferred_switch_starts')} per_intersection={d.get('inferred_switch_starts', 0)/max(1, n_int):.1f}")
    print(f"transition_fraction={d.get('transition_time_fraction'):.4f}")
    print(f"max_oldest_wait={d.get('maximum_oldest_wait')}")
    print(f"queue_mean={d.get('queue_total_mean'):.1f} queue_peak={d.get('queue_total_peak')}")
    print(f"queue_axis_totals={d.get('queue_axis_observed_totals')}")
    print(f"queue_axis_peaks={d.get('queue_axis_peak_totals')}")
    print(f"blocked_green_ticks={d.get('blocked_green_proxy_ticks')} approaches={d.get('blocked_green_proxy_approach_observations')}")
    print(f"peak_link_saturation={d.get('peak_link_saturation_ratio')} sat_ticks={d.get('link_saturation_ticks')}")
    switch_ticks = d.get("inferred_switch_start_ticks", [])
    if switch_ticks:
        buckets: dict[int, int] = {}
        for t in switch_ticks:
            buckets[t // 100] = buckets.get(t // 100, 0) + 1
        print("switch_ticks_by_100:", dict(sorted(buckets.items())))
        distinct = sorted(set(switch_ticks))
        print(f"distinct_switch_ticks={len(distinct)}")
    final = d.get("final_30_tick_proxies", {})
    print("final30:", json.dumps(final))
    by_int = d.get("inferred_switch_starts_by_intersection", {})
    print("switches_by_intersection:", dict(sorted(by_int.items())))


if __name__ == "__main__":
    main()
