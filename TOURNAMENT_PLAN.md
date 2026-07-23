# Traffic Lights Arena — Tournament Plan

## Objective

Win through robust hidden-scenario performance, not public-map overfitting.

The provisional score is 20% public and 80% private. The sealed final contains six traffic families, each represented by two maps; only the worse score from each pair counts, and the six family scores are combined geometrically. One weak traffic family can therefore ruin an otherwise strong controller.

## Winning thesis

Build a globally coordinated, demand-estimated controller with:

- adaptive NS/EW green splits;
- switching hysteresis and minimum/maximum green durations;
- spillback-aware pressure;
- starvation protection using oldest wait;
- explicit terminal-queue priority near the end.

All intersections should normally request the same axis. Vehicles never turn, so network-wide synchronization creates reliable corridor progression. Independent greedy switching tends to destroy coordination and waste capacity.

## Important simulator mechanics

- A phase change causes two yellow ticks and one all-red tick: three ticks with no service.
- Each green approach releases at most one vehicle per direction per tick.
- Every unfinished vehicle adds 300 to cost.
- Downstream queues can block upstream releases even when the visible link is not full.
- Reported link capacity can exceed the physical approach capacity.
- Controller state is observed before the current tick's signal transition, movement, and release.
- Private scenarios can change demand by time, direction, and lane.
- Controller memory must reset at tick 0 because an evaluator may reuse the module across scenarios.

## Controller architecture

### 1. Observe

For every intersection and direction, collect:

- queue length;
- oldest wait;
- current phase and phase age;
- whether switching is allowed;
- downstream link occupancy;
- remaining simulation ticks.

### 2. Estimate demand

Estimate arrivals at external approaches from queue changes and likely departures:

```text
observed_arrivals = clamp(
    current_queue - previous_queue + estimated_departures,
    0,
    1
)
```

Smooth each lane using an exponentially weighted moving average:

```text
demand = (1 - alpha) * previous_demand + alpha * observed_arrivals
```

Initial search range for `alpha`: 0.02–0.15.

For each axis, blend mean demand with the busiest lane. The mean measures total load; the maximum protects against lane hotspots.

### 3. Allocate green time

Use one coordinated axis network-wide and divide a green-time budget according to estimated demand:

```text
NS_green = clamp(
    green_budget * NS_demand / (NS_demand + EW_demand),
    minimum_green,
    green_budget - minimum_green
)
```

Promising initial ranges:

- total green budget: 16–48 ticks;
- per-axis minimum: 6–14 ticks;
- maximum continuous green: 24–60 ticks.

Recalculate targets at cycle boundaries instead of reacting to every one-tick queue fluctuation.

### 4. Guard switching

Switch only when:

- `can_switch` is true;
- the minimum useful green has elapsed;
- the competing axis exceeds the current axis by an absolute and relative margin;
- downstream capacity allows useful releases.

Use queue pressure and oldest wait as vetoes or emergency signals, not as the entire policy. Excessive oldest-wait weighting can cause oscillation.

During yellow and all-red, preserve the already selected target phase. A transition cannot be cancelled.

### 5. Handle spillback

Approximate reserved downstream space as:

```text
downstream_queue + vehicles_in_link
```

Reduce pressure for blocked approaches. If many links are saturated, shorten cycles to drain downstream queues more frequently. Permit local phase overrides only for severe blockage or starvation; normal operation should remain globally synchronized.

### 6. Optimize the endgame

Releasing a vehicle from its final intersection avoids the 300-point unfinished penalty. Releasing one from an upstream intersection may not.

During roughly the last 30–50 ticks:

- prioritize terminal approaches;
- discount upstream queues by remaining hops and travel time;
- avoid switches that cannot produce useful green before the simulation ends;
- hold a productive green instead of starting an unpayable transition.

## Evidence so far

Exploratory local simulations against the three public scenarios produced these public geometric-mean scores:

- synchronized 15/15 baseline: 10,000;
- synchronized fixed 20-tick greens: 12,012;
- coordinated slow-EWMA/peak-lane policy: 12,534;
- coordinated faster-EWMA/mean-lane policy: 12,818.

These results identify promising policy families. They do not prove hidden-scenario performance.

Independent local max-pressure policies and fixed spatial offsets performed poorly enough to deprioritize.

## Validation suite

Create twelve synthetic maps grouped into six plausible traffic families:

1. **Balanced:** 2×2 and 3×3 grids with steady low/medium demand.
2. **Directional:** one NS-heavy and one EW-heavy map.
3. **Reversal:** NS→EW and EW→NS demand changes halfway through.
4. **Lane hotspot:** concentrated row and column demand, then rotated.
5. **Bursty:** alternating 60–120 tick demand regimes.
6. **Spillback:** capacity 3–5, longer travel times, and 3×3/4×4 grids.

Use multiple unseen random seeds after tuning.

For every candidate:

1. Compute `fixed_controller_cost / candidate_cost` for each map.
2. Keep the worse result from each two-map family.
3. Geometrically combine the six family minima.
4. Track the absolute worst family separately.
5. Use public score only as a secondary tie-breaker.

Also record:

- unfinished vehicles and waiting ticks separately;
- switch count and transition-time fraction;
- blocked-green ticks;
- maximum oldest wait;
- link saturation;
- completions during the final 30 ticks.

## Parameter search

Use successive halving:

1. Test 100–200 broad configurations on shorter scenarios.
2. Run the top 20 on all twelve full-length synthetic maps.
3. Run the top five on held-out seeds.
4. Choose primarily by six-family minimum aggregation.

Mandatory ablations:

- global synchronization on/off;
- fixed versus demand-adaptive split;
- mean, maximum, and blended lane aggregation;
- spillback cycle contraction on/off;
- oldest-wait term on/off;
- endgame priority on/off;
- local emergency override on/off;
- different switching margins and maximum greens.

## 105-minute execution plan

### Minutes 0–15

- Reproduce the baseline.
- Build a fast benchmark harness.
- Generate the six synthetic families.
- Record waiting and unfinished costs separately.

Exit gate: deterministic baseline results and usable metrics.

### Minutes 15–35

- Implement synchronized fixed-cycle variants.
- Implement the demand estimator and adaptive split.
- Add safe state reset and transition handling.

Exit gate: at least one candidate beats baseline on every public scenario.

### Minutes 35–60

- Run broad successive-halving search.
- Compare peak-lane, mean-lane, and blended demand.
- Make the first materially distinct submissions.

Exit gate: two promising policy families with no major synthetic failure.

### Minutes 60–80

- Test unseen seeds.
- Add and ablate spillback protection.
- Add and ablate terminal-queue endgame logic.

Exit gate: no catastrophic regression in any family.

### Minutes 80–97

- Refine the best two families.
- Use leaderboard reveals only as weak evidence.
- Preserve a conservative synchronized candidate.

Exit gate: stable ranking across public and held-out synthetic tests.

### Minutes 97–105

- Freeze the controller.
- Run syntax, tests, and all public scenarios.
- Preserve enough time for the submission cooldown.

## Submission portfolio

Allocate the 20 unique attempts deliberately:

- 5 policy-family representatives;
- 8 refinements around the best two families;
- 4 robustness variants covering spillback, endgame, starvation, and conservative hysteresis;
- 3 reserves for reveal-driven adjustments or final hedges.

Do not spend attempts on tiny parameter changes without local evidence. Every submitted variant should have a plausible path to winning private validation.

## Deprioritize

- independent per-intersection greedy/max-pressure switching;
- linear oldest-wait priority without hysteresis;
- fixed spatial phase offsets;
- identifying or hardcoding public scenarios;
- frequent switching based on single-tick queue noise;
- optimizing only average cost;
- public-score improvements that introduce a weak hidden family.

## Definition of done

The final controller should:

- be deterministic and reset safely at tick 0;
- adapt to direction-, lane-, and time-varying demand;
- preserve network coordination in normal operation;
- avoid unnecessary three-tick transitions;
- resist spillback and starvation;
- maximize feasible terminal completions near the horizon;
- beat the public baseline without sacrificing held-out family robustness.
