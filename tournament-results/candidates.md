# Candidate Portfolio Registry

Use one row per materially distinct controller configuration. Link evidence to local result records or reports; do not promote a candidate on public performance alone.

Status values: `planned`, `evaluating`, `eligible`, or `retired`. Record a submission or observed rank only after it actually exists. `Final?` means the candidate remains eligible to become the final preferred controller.

| Slot | Candidate ID | Parameters / config hash | Policy family and intended hedge | Evidence (public / synthetic / held-out) | Known weak family | Status | Observed rank | Final? |
|---|---|---|---|---|---|---|---|---|
| Family 1/5 | `F01` (spillback-safe coordinated, pre-2026-07-23) | Stock constants; no backlog steering | Globally coordinated adaptive split; hedge: proven robustness | Public GM 14,826 (9,554/23,265/14,663) / dev family GM 1.06473, worst 0.9460 (bal-2x2), hotspot-4x4 0.9853, spill-3x3 1.0249 / held-out GM 1.07579, worst 0.9801 (hotspot-4x4), spillback min 1.1027 (`repro-*.jsonl`, 2026-07-23) | lane-hotspot (4x4) | retired; superseded by `R01`; no submission recorded | — | No |
| Family 2/5 | `F02` | — | Family representative: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Family 3/5 | `F03` | — | Family representative: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Family 4/5 | `F04` | — | Family representative: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Family 5/5 | `F05` | — | Family representative: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 1/8 | `R01` (`cand-structural-backlog`, 2026-07-23) | F01 + BACKLOG_WEIGHT_MAX 0.65, BACKLOG_QUEUE_SCALE 12, PRESSURE_SHARE_ALPHA 0.02, QUEUE_SHARE_ALPHA 0.02, QUEUE_IMBALANCE_ZONE 0.08, QUEUE_SHARE_MIN_TOTAL 6, BACKLOG_DEMAND_INFORMATIVE 0.12 | Backlog-aware split for structurally throughput-limited axes (tight 4x4 NS links) plus target-respecting bilateral starvation floor; hedge: hotspot/spillback-4x4 lower tail | Public GM 14,826 (identical 9,554/23,265/14,663) / dev family GM 1.07531, worst 0.9460 (bal-2x2), hotspot-4x4 1.0455, spill-3x3 1.0249, all other dev maps identical to F01 / held-out GM 1.07759, worst 0.9899 (hotspot-4x4), spillback min 1.1027 (`cand-structural-backlog-*.jsonl`, 2026-07-23) | balanced (2x2, seed-sensitive) | eligible; no submission recorded | — | Yes |
| Refinement 2/8 | `R02` | — | Best-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 3/8 | `R03` | — | Best-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 4/8 | `R04` | — | Best-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 5/8 | `R05` | — | Second-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 6/8 | `R06` | — | Second-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 7/8 | `R07` | — | Second-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Refinement 8/8 | `R08` | — | Second-family refinement: — | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Hedge 1/4 | `H01` | — | Robustness hedge: spillback | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Hedge 2/4 | `H02` | — | Robustness hedge: endgame | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Hedge 3/4 | `H03` | — | Robustness hedge: starvation | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Hedge 4/4 | `H04` | — | Robustness hedge: conservative hysteresis | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Reserve 1/3 | `X01` | — | Reveal-driven adjustment or final hedge | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Reserve 2/3 | `X02` | — | Reveal-driven adjustment or final hedge | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |
| Reserve 3/3 | `X03` | — | Reveal-driven adjustment or final hedge | Pending / Pending / Pending | Unknown | planned; no submission recorded | — | Yes |

For each evidence entry, record the ledger/config hash, suite and seeds, family minima, geometric aggregate, worst scenario, and date. Explain any retirement or rank-based decision in a short note below the row.

## Notes (2026-07-23)

- `F01` retired: `R01` dominates it on every ranking criterion (held-out worst 0.9899 vs 0.9801, held-out family GM 1.07759 vs 1.07579, dev family GM 1.07531 vs 1.06473) with identical public scores and identical behavior on all non-imbalanced maps.
- `R01` mechanism: a slow queue-share EWMA detects a persistent axis backlog imbalance (structural on 4x4 grids, whose short row spacing caps NS approach capacity at 2 vehicles, throttling NS flow to ~0.4/tick). Only then does the split steer toward the cycle-averaged pressure share, and the bilateral starvation escape defers to the allocated target so the split can actually express. Static-split sweep on dev hotspot-4x4 confirmed the gradient (NS 20 / EW 16 cost 134,405 vs 15/15 cost 165,639).
- Falsified along the way (evidence in `cand-backlog-*.jsonl` and session experiments): unconditional pressure-share steering (spill-3x3 0.9146, northbound 17,728), amplify-only steering without an informative gate (dir-ew 1.0784, bursty-75 1.2353), light-balanced mode exit hysteresis (public balanced 8,791, northbound 18,519).
- Known remaining weakness: balanced 2x2 dev seed 0.9460 and public balanced-grid 9,554 (both below baseline; held-out balanced 1.0317, so seed-sensitive). Mode-flapping was investigated and is NOT the cause worth fixing via stickiness; next candidate direction is endgame terminal completion (observer shows final-30 actual green terminal queue near zero on 2x2 maps).
