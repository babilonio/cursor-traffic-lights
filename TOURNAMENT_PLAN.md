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

## Artifact map

The implementation is divided into one submission artifact and several local experimentation artifacts.

Only `traffic-lights-arena/controller.py` is part of the competition controller. All runtime components must live inside that file. Benchmarking artifacts are local-only and should live outside `traffic-lights-arena` so the challenge package remains unchanged.

### Submission artifact

- **S1 — Controller facade:** implements the required `control(state)` entry point.
- **S2 — Runtime state store:** retains demand estimates and phase history between ticks.
- **S3 — State observer:** converts raw simulator state into stable policy features.
- **S4 — Demand estimator:** infers lane and axis demand over time.
- **S5 — Pressure and spillback model:** scores useful service while discounting blocked approaches.
- **S6 — Network coordinator:** chooses the globally coordinated NS/EW schedule.
- **S7 — Switch guard:** applies transition cost, hysteresis, and starvation rules.
- **S8 — Endgame optimizer:** prioritizes feasible terminal completions near the horizon.

### Local experimentation artifacts

- **L1 — Scenario catalog:** defines the twelve synthetic validation maps and held-out seeds.
- **L2 — Benchmark runner:** executes controllers against public and synthetic scenarios.
- **L3 — Experiment observer:** records decisions and derived diagnostics without changing the simulator.
- **L4 — Tournament scorer:** reproduces family minima, geometric aggregation, and public/private weighting.
- **L5 — Parameter tuner:** performs successive halving and ablation runs.
- **L6 — Results ledger:** stores configurations, metrics, rankings, and reproducibility metadata.
- **L7 — Controller contract tests:** verifies valid output, reset behavior, transitions, and determinism.
- **L8 — Candidate registry:** preserves materially distinct controller configurations for the submission portfolio.

### Dependency flow

```text
Raw state
  -> S3 State observer
  -> S4 Demand estimator + S5 Pressure model
  -> S6 Network coordinator
  -> S7 Switch guard
  -> S8 Endgame optimizer
  -> S1 Controller facade
  -> phase requests

L1 Scenario catalog
  -> L2 Benchmark runner
  -> L3 Experiment observer
  -> L4 Tournament scorer
  -> L6 Results ledger
  -> L5 Parameter tuner
  -> L8 Candidate registry
```

## Submission artifact specifications

### S1 — Controller facade

**Location:** `traffic-lights-arena/controller.py`

**Purpose:** expose the required `control(state)` function and compose the runtime components.

**Inputs:** the simulator state dictionary.

**Output:** one valid `NS_GREEN` or `EW_GREEN` request for every intersection.

**Acceptance criteria:**

- deterministic for the same state history;
- no imports from local experimentation artifacts;
- returns a complete decision dictionary;
- resets all runtime memory when a new scenario starts;
- remains valid during green, yellow, and all-red phases.

### S2 — Runtime state store

**Purpose:** retain information unavailable from a single tick.

**State:**

- previous tick and map dimensions;
- previous queues by intersection and direction;
- demand EWMA by external lane;
- last known green and requested phase;
- pending transition target;
- current NS/EW green targets;
- starvation and congestion indicators.

**Acceptance criteria:**

- resets when `tick == 0` or the tick moves backwards;
- rebuilds safely if the map dimensions or intersection IDs change;
- contains no random state;
- cannot leak observations between scenarios.

### S3 — State observer

**Purpose:** normalize raw state into features used by every policy component.

**Output features:**

- queue length;
- oldest wait;
- current phase and phase age;
- whether switching is allowed;
- downstream link occupancy;
- axis totals and peak lane;
- external versus intermediate approaches;
- terminal versus nonterminal approaches;
- estimated downstream reservation;
- remaining simulation ticks.

**Acceptance criteria:**

- treats absent links as zero occupancy;
- handles the generic `YELLOW` phase without guessing a new target;
- derives topology only from map dimensions and intersection IDs;
- does not mutate the simulator state.

### S4 — Demand estimator

**Purpose:** infer changing lane and axis demand from queue observations.

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

**Acceptance criteria:**

- adapts to direction changes and lane hotspots;
- filters single-tick noise;
- does not mistake downstream blockage for new external demand;
- exposes NS/EW demand estimates to the coordinator.

### S5 — Pressure and spillback model

**Purpose:** estimate how much useful work each phase can perform.

Approximate reserved downstream space as:

```text
downstream_queue + vehicles_in_link
```

For an approach, combine queue size and a bounded oldest-wait term, then discount pressure when downstream space is nearly exhausted.

**Acceptance criteria:**

- suppresses blocked-green demand;
- preserves emergency priority for starved approaches;
- distinguishes terminal queues, which can complete immediately;
- remains useful when physical capacity is lower than reported capacity.

### S6 — Network coordinator

**Purpose:** select the network-wide base axis and adaptive green split.

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

**Acceptance criteria:**

- preserves global synchronization during normal operation;
- reacts to sustained changes in demand;
- supports shorter cycles under severe spillback;
- never depends on public scenario IDs.

### S7 — Switch guard

**Purpose:** determine whether a requested phase change is worth three no-service ticks.

Switch only when:

- `can_switch` is true;
- the minimum useful green has elapsed;
- the competing axis exceeds the current axis by an absolute and relative margin;
- downstream capacity allows useful releases.

Use queue pressure and oldest wait as vetoes or emergency signals, not as the entire policy. Excessive oldest-wait weighting can cause oscillation.

During yellow and all-red, preserve the already selected target phase. A transition cannot be cancelled.

**Acceptance criteria:**

- respects `can_switch` and minimum green;
- uses absolute and relative hysteresis;
- enforces a maximum green or starvation escape;
- does not oscillate on one-tick queue changes;
- preserves the pending target throughout a transition.

### S8 — Endgame optimizer

**Purpose:** minimize the 300-point penalty for unfinished vehicles.

Releasing a vehicle from its final intersection avoids the 300-point unfinished penalty. Releasing one from an upstream intersection may not.

During roughly the last 30–50 ticks:

- prioritize terminal approaches;
- discount upstream queues by remaining hops and travel time;
- avoid switches that cannot produce useful green before the simulation ends;
- hold a productive green instead of starting an unpayable transition.

**Acceptance criteria:**

- activates only near the horizon;
- estimates whether a switch can reach productive green in time;
- prioritizes feasible terminal completions;
- does not strand a productive current phase for unreachable upstream work.

## Evidence so far

Exploratory local simulations against the three public scenarios produced these public geometric-mean scores:

- synchronized 15/15 baseline: 10,000;
- synchronized fixed 20-tick greens: 12,012;
- coordinated slow-EWMA/peak-lane policy: 12,534;
- initial coordinated faster-EWMA/mean-lane policy: 12,818;
- implemented spillback-safe coordinated controller: 14,826.

The implemented controller also achieved a 1.0758 geometric mean across six held-out
family minima. Its weakest held-out family ratio was 0.9801, so lane-hotspot robustness
remains the clearest refinement target. Synthetic results are useful risk proxies, not
proof of sealed-final performance.

Independent local max-pressure policies and fixed spatial offsets performed poorly enough to deprioritize.

## Local experimentation artifact specifications

### L1 — Scenario catalog

**Location:** `tournament_tools/scenario_catalog.py`

**Purpose:** generate public-compatible synthetic scenarios without modifying the simulator.

Create twelve maps grouped into six plausible traffic families:

1. **Balanced:** 2×2 and 3×3 grids with steady low/medium demand.
2. **Directional:** one NS-heavy and one EW-heavy map.
3. **Reversal:** NS→EW and EW→NS demand changes halfway through.
4. **Lane hotspot:** concentrated row and column demand, then rotated.
5. **Bursty:** alternating 60–120 tick demand regimes.
6. **Spillback:** capacity 3–5, longer travel times, and 3×3/4×4 grids.

Use multiple unseen random seeds after tuning.

### L2 — Benchmark runner

**Location:** `tournament_tools/benchmark.py`

**Purpose:** run one or more controller configurations quickly and reproducibly.

**Inputs:**

- controller factory or parameter configuration;
- scenario list;
- seed set;
- run length and worker count.

**Outputs:**

- raw simulation metrics;
- elapsed runtime;
- controller configuration hash;
- records consumed by the experiment observer and scorer.

**Acceptance criteria:**

- runs without replay generation;
- reproduces the fixed baseline exactly;
- isolates controller state between scenarios;
- never invokes `submit.py`;
- supports public-only, smoke, full, and held-out suites.

### L3 — Experiment observer

**Location:** `tournament_tools/observer.py`

**Purpose:** wrap a controller and record policy behavior without editing the engine.

**Recorded diagnostics:**

- requested phases and actual observed phases;
- switch count and transition-time fraction;
- queue pressure and demand estimates;
- blocked-green ticks;
- maximum oldest wait;
- link saturation;
- terminal completions during the final 30 ticks.

The observer must be optional so benchmark timings can also be measured without instrumentation overhead.

### L4 — Tournament scorer

**Location:** `tournament_tools/tournament_score.py`

**Purpose:** rank candidates using the competition's risk structure rather than average cost.

For every candidate:

1. Compute `fixed_controller_cost / candidate_cost` for each map.
2. Keep the worse result from each two-map family.
3. Geometrically combine the six family minima.
4. Track the absolute worst family separately.
5. Use public score only as a secondary tie-breaker.

Also report:

- unfinished vehicles and waiting ticks separately;
- every individual scenario ratio;
- family minima;
- six-family geometric mean;
- absolute worst scenario and family;
- public geometric-mean score.

### L5 — Parameter tuner

**Location:** `tournament_tools/tune.py`

**Purpose:** search promising configurations while spending most compute on robust candidates.

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

### L6 — Results ledger

**Proposed location:** `tournament-results/results.jsonl`

**Purpose:** make every benchmark reproducible and prevent repeated experiments.

Each record should contain:

- timestamp and source revision;
- controller family and complete parameters;
- scenario and seed;
- cost, wait ticks, unfinished, and completed;
- observer diagnostics when enabled;
- aggregate ranking fields;
- candidate identifier.

### L7 — Controller contract tests

**Location:** `tournament_tools/test_controller_contract.py`

**Purpose:** catch controller failures before expensive benchmark or submission attempts.

Tests should cover:

- complete and valid return values;
- tick-0 and backwards-tick reset;
- changing grid dimensions;
- deterministic repeated runs;
- yellow/all-red target preservation;
- no unpayable final-tick switching;
- no state leakage across sequential scenarios.

### L8 — Candidate registry

**Proposed location:** `tournament-results/candidates.md`

**Purpose:** track the 20-attempt portfolio and the evidence supporting every candidate.

For each candidate, record:

- identifier and controller parameters;
- policy family and intended hedge;
- public, synthetic, and held-out results;
- known weak family;
- submission status and observed rank;
- whether it remains eligible as the final preferred candidate.

## 105-minute execution plan

### Minutes 0–15

- Reproduce the baseline.
- Build **L2 Benchmark runner** and **L4 Tournament scorer**.
- Generate **L1 Scenario catalog**.
- Add the minimum **L3 Experiment observer** metrics.

Exit gate: deterministic baseline results and usable metrics.

### Minutes 15–35

- Implement synchronized fixed-cycle variants.
- Implement **S1–S4**: facade, state store, observer, and demand estimator.
- Implement the first **S6 Network coordinator**.
- Add safe reset and transition handling through **S7 Switch guard**.

Exit gate: at least one candidate beats baseline on every public scenario.

### Minutes 35–60

- Run broad successive-halving search with **L5 Parameter tuner**.
- Compare peak-lane, mean-lane, and blended demand.
- Record materially distinct candidates in **L8 Candidate registry**.

Exit gate: two promising policy families with no major synthetic failure.

### Minutes 60–80

- Test unseen seeds.
- Add and ablate **S5 Pressure and spillback model**.
- Add and ablate **S8 Endgame optimizer**.
- Run **L7 Controller contract tests**.

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
