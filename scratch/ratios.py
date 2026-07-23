"""Print per-map ratios from benchmark JSONL files (local scratch tool)."""
import json
import sys
from pathlib import Path

for name in sys.argv[1:]:
    print("===", name)
    for line in Path(name).read_text().splitlines():
        r = json.loads(line)
        ratio = r["baseline_cost"] / r["cost"]
        print(
            f"{r['scenario_id']:52s} ratio={ratio:.4f} cost={r['cost']:6d} "
            f"base={r['baseline_cost']:6d} wait={r['wait_ticks']:6d} "
            f"unf={r['unfinished']:3d} spawned={r['spawned']:5d}"
        )
