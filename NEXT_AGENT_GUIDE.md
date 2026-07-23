# Next Agent Guide — Improving the Traffic Lights Controller

## 1. Mission

Your job is to improve `traffic-lights-arena/controller.py` for robust tournament
performance.

The objective is **not** to maximize one public map or one average benchmark.
The final evaluation is dominated by hidden traffic, takes the worse result from
each two-map traffic family, and geometrically combines six family results. A
single severe regression can erase gains everywhere else.

Work scientifically:

1. understand the simulator before changing policy;
2. reproduce the current results;
3. identify the weakest behavior;
4. form one falsifiable hypothesis;
5. make one coherent controller change;
6. test public, development, and held-out behavior in that order;
7. keep the change only if the lower tail improves without creating another
   severe regression.

## 2. Non-negotiable rules

### Never submit

Do not run:

- `submit.py`;
- any command that imports or invokes submission code;
- any event API or leaderboard mutation;
- any login or submission-related network request.

Local simulation is allowed. Submission is not.

### Preserve the challenge boundary

The competition artifact is:

```text
traffic-lights-arena/controller.py
```

For controller improvement, edit only that challenge file. The files under
`tournament_tools/` and `tournament-results/` are local evaluation support. Do
not modify the simulator, public scenarios, scoring profiles, or tests to make
the controller look better.

If a support tool has a genuine bug, document the evidence before changing it
and keep that repair separate from controller policy work.

### Preserve the current working tree

This repository already contains intentional uncommitted and untracked work.
Start with:

```powershell
git status --short
git diff -- traffic-lights-arena/controller.py
```

Do not reset, restore, clean, overwrite, or discard existing changes. Do not
commit unless the user explicitly asks for a commit.

### Keep the submitted controller self-contained

`controller.py` must:

- use only the Python standard library;
- define callable `control(state)`;
- return only `NS_GREEN` or `EW_GREEN`;
- return a decision for every current intersection;
- be deterministic;
- not import from `tournament_tools`;
- not read files, environment-specific configuration, scenario IDs, or network
  data;
- not assume the evaluator imports a fresh module for every scenario.

## 3. Read these files first

Read in this order:

1. `AGENTS.md`
2. `traffic-lights-arena/README.md`
3. `traffic-lights-arena/controller.py`
4. `traffic-lights-arena/traffic_arena/engine.py`
5. `traffic-lights-arena/traffic_arena/scenarios.py`
6. `traffic-lights-arena/traffic_arena/scoring.py`
7. `traffic-lights-arena/traffic_arena/score_profiles.py`
8. `TOURNAMENT_PLAN.md`
9. `tournament_tools/scenario_catalog.py`
10. `tournament_tools/benchmark.py`
11. `tournament_tools/tournament_score.py`
12. `tournament_tools/observer.py`
13. `tournament_tools/test_controller_contract.py`
14. `tournament-results/candidates.md`

Do not start by changing constants. First explain, in your own working notes,
how the simulator tick order, switching delay, spillback checks, and terminal
penalty interact with the current policy.

## 4. Current verified state

These measurements were taken on July 23, 2026 (evening session) with the
current working-tree controller (`cand-structural-backlog`, registered as
`R01` in `tournament-results/candidates.md`). It adds gated backlog-aware
split steering plus a target-respecting bilateral starvation floor to the
previously checked-in controller; all public scores and every development map
except the two structurally imbalanced 4x4 maps are byte-for-byte identical
to the prior baseline.

### Public scenarios

- **Balanced grid**
  - baseline cost: 16,648;
  - controller cost: 17,425;
  - controller score: 9,554;
  - wait ticks: 13,825;
  - unfinished vehicles: 12.
- **Northbound morning**
  - baseline cost: 36,398;
  - controller cost: 29,268;
  - controller score: 23,265;
  - wait ticks: 20,268;
  - unfinished vehicles: 30.
- **City rush**
  - baseline cost: 83,137;
  - controller cost: 72,529;
  - controller score: 14,663;
  - wait ticks: 54,529;
  - unfinished vehicles: 60.
- **Public geometric-mean score:** 14,826.

Interpretation:

- directional and congested public performance is strong;
- public balanced traffic is still below the fixed baseline;
- do not trade away the northbound and city-rush gains merely to improve one
  balanced seed.

### Held-out six-family proxy

Family minima are normalized as:

```text
fixed_baseline_cost / controller_cost
```

A ratio above 1.0 beats the fixed controller.

- balanced: 1.0317;
- bursty: 1.0560;
- directional: 1.1584;
- lane hotspot: 0.9899;
- reversal: 1.1365;
- spillback: 1.1027;
- six-family geometric mean: 1.0776;
- absolute worst held-out map ratio: 0.9899.

The weakest held-out case remains the 4×4 rotating lane-hotspot map (0.9899,
improved from 0.9801 by the backlog-steering change; the development seed of
the same map is now 1.0455). The next-best investigation targets are the
balanced 2×2 development seed (0.9460) and public balanced (9,554); see the
notes in `tournament-results/candidates.md` for approaches already falsified.

### Development synthetic proxy

The latest development-suite report was:

- six-family geometric mean: approximately 1.0753;
- worst scenario ratio: approximately 0.9460 (balanced 2×2).

Reproduce this before relying on it. Development scenarios are the tuning set.
Held-out seeds are a validation gate and should not be repeatedly optimized
against.

### Important prior failure

An earlier controller scored only 0.4502 of baseline on tight 3×3 spillback.
The cause was a starvation path that bypassed scheduling after seven green
ticks, producing:

- 79 switches per intersection;
- 26.3% of time in transitions;
- severe network starvation.

The current controller fixed that with:

- a 13-tick emergency green floor;
- smoothed target splits;
- a generic light-balanced mode.

Do not remove those protections casually. Any change that shortens emergency
greens or increases transition frequency must be tested on both tight
spillback maps immediately.

## 5. Simulator facts that should drive every decision

### Tick order

Each simulation tick does this:

1. spawn vehicles;
2. build controller state;
3. call `control(state)`;
4. apply requested phases;
5. advance signals;
6. advance travelling vehicles;
7. release queues;
8. increment waiting time.

Controller observations are therefore pre-action snapshots. A link vehicle may
arrive later in the same tick. A newly arrived vehicle may also be released by
the next intersection in that tick, depending on iteration order and phase.

### Switching cost

- minimum engine green: 5 ticks;
- yellow: 2 ticks;
- all-red: 1 tick;
- total no-service transition: 3 ticks.

A switch request is expensive. Frequent switching loses throughput even when
the queue-pressure comparison appears locally correct.

Requests made before minimum green are latched by the engine. During yellow and
all-red, the controller cannot infer transition direction from the raw phase,
so it must preserve its own pending target.

### Throughput

For each green direction:

- at most one vehicle is released per tick;
- both directions on the green axis can release simultaneously;
- routes are straight across one row or one column;
- vehicles never turn.

Global axis coordination is valuable because all vehicles follow straight
corridors.

### Spillback

An upstream release requires:

- visible link occupancy below reported link capacity; and
- target queue plus in-flight reservations below physical approach capacity.

The controller sees reported link capacity, not exact physical approach
capacity. Physical capacity can be much smaller, especially on dense grids.
Absence of a link from `state["links"]` means zero visible occupancy.

### Cost

```text
cost = total_wait_ticks + 300 * unfinished_vehicles
```

One extra completion can be worth more than hundreds of small waiting-time
improvements. Endgame decisions must distinguish terminal queues from upstream
queues.

### Hidden variation

Hidden scenarios may vary:

- rows and columns;
- demand over time;
- N, S, E, and W demand independently;
- row and column lane weights;
- link capacity;
- travel time;
- random seed.

The controller does not receive scenario ID, rates, seed, or travel time.

## 6. Current controller architecture

The controller is intentionally one self-contained file.

### Runtime state

`_runtime` stores:

- previous tick, shape, and intersection IDs;
- previous queues and features;
- lane-demand EWMAs;
- remembered link capacities;
- last green, requested axis, and pending target;
- adaptive green targets;
- green start time;
- starvation counters;
- spillback ratio;
- coordination mode.

Reset occurs when:

- runtime is empty;
- tick is zero;
- tick moves backwards or repeats;
- map shape changes;
- intersection IDs change.

Any new state must obey the same reset behavior.

### Observation

`_observe` derives:

- topology from intersection IDs and map dimensions;
- external, intermediate, and terminal approaches;
- downstream queue and in-link reservations;
- conservative physical-capacity estimates;
- queue, wait, pressure, terminal count, and remaining-hop features;
- endgame pressure discounts.

### Demand estimation

`_estimate_demand` uses queue conservation on external approaches and an EWMA.
Current axis aggregation is:

```text
0.82 * mean_lane_demand + 0.18 * peak_lane_demand
```

This is a likely reason concentrated lane demand is underrepresented, but that
is a hypothesis—not a fact.

### Target allocation

`_update_targets`:

- computes a blocked-approach ratio;
- slightly contracts the cycle under spillback;
- uses a 24-tick, even split in light-balanced traffic;
- otherwise smooths demand-proportional targets over a 30-tick green budget.

### Switching

`_choose_axis`:

- preserves pending transitions;
- uses a globally coordinated axis;
- requires switchability and useful competing pressure;
- applies absolute and relative hysteresis;
- applies target duration, starvation, and maximum-green checks;
- requires a 13-tick emergency floor;
- avoids obviously unpayable endgame switches.

## 7. Current constants and what they control

Change constants only with a written hypothesis.

- `DEMAND_ALPHA = 0.10`
  - demand reaction speed;
  - higher values react faster but amplify queue noise;
  - sensible investigation range: 0.04–0.16.
- `TARGET_ALPHA = 0.35`
  - smoothing of green-split changes;
  - higher values adapt faster but can destabilize cycles;
  - sensible range: 0.20–0.50.
- `GREEN_BUDGET = 30`
  - total normal green budget across both axes;
  - longer budgets reduce transition loss but react more slowly;
  - sensible range: 24–40.
- `MIN_TARGET_GREEN = 7`
  - minimum target allocation;
  - this is not the same as the 13-tick emergency floor.
- `EMERGENCY_MIN_GREEN = 13`
  - prevents starvation logic from causing rapid oscillation;
  - high-risk constant; test spillback after every change.
- `MAX_CONTINUOUS_GREEN = 48`
  - starvation escape and maximum green.
- `ABS_SWITCH_MARGIN = 0.30`
  - absolute pressure advantage needed for a normal switch.
- `REL_SWITCH_MARGIN = 1.08`
  - relative pressure advantage needed for a normal switch.
- `STARVATION_WAIT = 34`
  - oldest-wait threshold used in pressure and switching.
- `ENDGAME_TICKS = 44`
  - horizon at which route usefulness and terminal queues receive special
    treatment.
- `TRANSITION_TICKS = 3`
  - exact engine transition duration; do not tune.
- `LIGHT_QUEUE_LIMIT = 6.0`
  - queue-per-intersection threshold for light-balanced mode.
- `LIGHT_DEMAND_RATIO = 1.35`
  - maximum axis imbalance for light-balanced mode.
- `LIGHT_GREEN_BUDGET = 24`
  - even-split budget used in light-balanced mode.

Do not run a blind Cartesian search across all constants. Interactions are
strong, and the held-out suite is too small to support that amount of tuning.

## 8. Reproduce before editing

Run from repository root with the existing virtual environment.

### Compile

```powershell
.\.venv\Scripts\python.exe -m compileall -q `
  "traffic-lights-arena/controller.py" `
  "tournament_tools"
```

### Contract and simulator tests

```powershell
.\.venv\Scripts\python.exe -m pytest `
  "tournament_tools/test_controller_contract.py" `
  "traffic-lights-arena/tests/test_simulator.py"
```

Expected: 25 tests pass.

### Public benchmark

```powershell
.\.venv\Scripts\python.exe -m tournament_tools.benchmark `
  ".\traffic-lights-arena\controller.py" `
  --mode public `
  --candidate-id current
```

### Development-family score

```powershell
.\.venv\Scripts\python.exe -m tournament_tools.benchmark `
  ".\traffic-lights-arena\controller.py" `
  --mode synthetic `
  --candidate-id current |
  .\.venv\Scripts\python.exe -m tournament_tools.tournament_score - --json
```

### Held-out-family score

```powershell
.\.venv\Scripts\python.exe -m tournament_tools.benchmark `
  ".\traffic-lights-arena\controller.py" `
  --mode held-out `
  --candidate-id current |
  .\.venv\Scripts\python.exe -m tournament_tools.tournament_score - --json
```

### Observer diagnostics

Start with one smoke scenario because observer JSON is large:

```powershell
.\.venv\Scripts\python.exe -m tournament_tools.benchmark `
  ".\traffic-lights-arena\controller.py" `
  --mode smoke `
  --candidate-id observed-current `
  --observe
```

Record the exact baseline before editing. If your reproduced metrics differ,
stop and explain why before optimizing.

## 9. Recommended improvement workflow

### Step 1: Rank the lower tail

For each candidate, inspect:

1. absolute worst scenario ratio;
2. family minima;
3. family geometric mean;
4. unfinished vehicles;
5. transition fraction and switch count;
6. public geometric-mean score.

Do not rank primarily by average cost.

### Step 2: Diagnose one weakness

Use observer output to distinguish:

- excessive switching;
- greens serving blocked approaches;
- starvation or maximum-green forcing;
- slow adaptation after a demand reversal;
- underweighting of one hot lane;
- poor endgame terminal completion;
- bad light-balanced classification.

Do not infer causality from queue length alone.

### Step 3: Form a falsifiable hypothesis

Good:

> The 4×4 lane-hotspot regression occurs because mean-heavy demand aggregation
> underweights one saturated external lane. Raising peak-lane influence should
> reduce that family's worst cost without increasing transition fraction.

Bad:

> Change several constants and see whether the score improves.

Write down:

- expected maps to improve;
- metrics that should move;
- maps most likely to regress;
- rollback condition.

### Step 4: Make one coherent change

Examples:

- adjust lane aggregation;
- improve a spillback estimate;
- improve light-mode classification;
- improve endgame completion logic;
- add a carefully gated local override.

Do not combine unrelated demand, switching, spillback, and endgame changes in
one experiment.

### Step 5: Run gates in order

1. compile;
2. contract tests;
3. public benchmark;
4. development synthetic suite;
5. observer diagnostics on changed behavior;
6. held-out suite only after the candidate survives development.

This order reduces accidental tuning to held-out seeds.

### Step 6: Compare, do not eyeball

For every candidate, retain:

- controller diff;
- complete constants;
- public scenario costs and scores;
- development family minima and GM;
- held-out family minima and GM;
- worst map;
- switch count and transition fraction;
- wait and unfinished components.

Update `tournament-results/candidates.md` only with real measurements. Do not
claim a submission.

### Step 7: Keep or revert the logical change

Keep a change only when the evidence supports its hypothesis. If the hypothesis
fails, undo only your own change; do not reset the working tree.

## 10. Highest-priority hypotheses

These are investigation directions, not required changes.

### A. Peak-lane sensitivity

Problem signal:

- held-out lane-hotspot minimum is 0.9801;
- current demand aggregation gives only 18% weight to the peak lane.

Experiment:

- test 25%, 30%, and 35% peak-lane weight while preserving normalization;
- keep demand EWMA and switching constants unchanged;
- inspect lane-hotspot costs, reversal adaptation, and switch frequency.

Failure condition:

- bursty/reversal family minimum falls materially;
- transition fraction rises;
- balanced demand becomes unstable.

### B. Light-balanced mode and public balanced traffic

Problem signal:

- public balanced score is 9,554, below baseline;
- held-out balanced is above baseline, so this may be seed sensitivity rather
  than a universally bad split.

Experiment:

- inspect when `coordination_mode` enters and leaves `light-balanced`;
- compare queue per intersection, demand ratio, and terminal completions;
- test light budget or classification thresholds one at a time.

Failure condition:

- held-out balanced drops below baseline;
- city-rush or spillback switches more frequently;
- public gain comes only from one extra terminal completion with worse waiting
  and poor seed robustness.

### C. Terminal completion quality

Problem signal:

- public balanced has relatively low waiting but 12 unfinished vehicles;
- the 300-point terminal penalty dominates small wait improvements.

Experiment:

- inspect final 44 ticks with observer diagnostics;
- distinguish terminal queues from upstream queues;
- estimate whether switching can provide enough productive ticks;
- consider a stronger terminal-value comparison rather than a blanket phase
  rule.

Failure condition:

- upstream queues are drained into links but remain unfinished;
- endgame switching increases;
- other families reduce waiting but increase unfinished count.

### D. Travel-time inference

Problem signal:

- endgame usefulness assumes roughly five ticks per hop;
- synthetic spillback maps use travel times of 9 and 12;
- hidden travel time is not exposed.

Experiment:

- determine whether link occupancy appearance/disappearance can provide a
  stable online travel-time estimate;
- use a conservative estimate only if enough observations exist;
- keep a safe fallback.

Failure condition:

- estimator confuses congestion/reservations with travel duration;
- state or computation grows without bound;
- early-scenario decisions become unstable.

### E. Capacity inference

Problem signal:

- `_estimated_physical_capacity` approximates geometry from grid dimensions;
- public layouts can be irregular;
- reported capacity does not reveal physical approach capacity.

Experiment:

- infer repeated blocked-release behavior from queue and link changes;
- reduce approach service pressure only after sustained evidence;
- reset inferred capacities per scenario.

Failure condition:

- low demand is mistaken for blockage;
- one direction is permanently suppressed;
- inference depends on scenario IDs or exact public geometry.

### F. Rare local override

Problem signal:

- global synchronization is robust but may be too coarse for isolated lane
  hotspots.

Experiment:

- permit an override only under severe, sustained local imbalance;
- require a long local green floor and downstream space;
- cap the number or duration of simultaneous overrides;
- return to global coordination deterministically.

Failure condition:

- corridor progression breaks;
- neighboring intersections oscillate;
- spillback family regresses;
- controller transition state becomes ambiguous.

This is a high-risk architectural experiment. Try simpler demand and endgame
improvements first.

## 11. Observer interpretation

`ObservedController` is diagnostic only and must never be imported by the
submitted controller.

Useful fields include:

- `inferred_switch_starts`;
- `inferred_switch_starts_by_intersection`;
- `transition_time_fraction`;
- `maximum_oldest_wait`;
- `queue_total_mean` and `queue_total_peak`;
- `queue_axis_observed_totals`;
- `peak_link_saturation_ratio`;
- `blocked_green_proxy_ticks`;
- `axis_pressure`;
- `final_30_tick_proxies`.

Important cautions:

- switch starts are counted per intersection; divide or compare consistently;
- blocked-green and saturation are proxies, not simulator truth;
- absent links are zero occupancy;
- controller axis pressure is optional instrumentation;
- observer overhead is acceptable for diagnosis but should not be used for
  timing comparisons.

## 12. Tuning guidance

`tournament_tools/tune.py` is controller-agnostic. It does not automatically
rewrite constants in `controller.py`.

Do not assume the tuner can evaluate source-level parameters without an
evaluator adapter.

Safe options:

1. manually test a small number of hypothesis-driven constant variants;
2. create temporary controller copies outside `traffic-lights-arena` and point
   the benchmark at each copy;
3. write a local evaluator that generates isolated temporary candidates,
   without modifying the live controller concurrently.

Never make the submitted controller depend on environment variables or local
configuration just to simplify tuning.

Run the tuner self-test before relying on it:

```powershell
.\.venv\Scripts\python.exe -m tournament_tools.tune --self-test
```

Prefer successive halving:

- broad smoke/development subset;
- full development suite;
- unseen seeds or held-out suite for finalists only.

Use deterministic candidate IDs and retain complete parameter dictionaries.

## 13. Acceptance gates

A candidate is not automatically better because public GM increases.

Minimum engineering gates:

- compilation passes;
- all 25 tests pass;
- complete valid decisions on all map sizes;
- deterministic repeated simulations;
- no state leakage;
- no lint or whitespace errors;
- no submission-related command was run.

Recommended performance gates:

- no catastrophic scenario regression;
- development worst ratio should improve beyond the current ~0.946;
- held-out worst ratio should stay near or above the current 0.9801;
- held-out six-family GM should not fall materially below 1.0758;
- spillback minimum should remain near or above 1.10;
- public GM should remain strong unless a lower-tail improvement clearly
  justifies a small trade;
- transition fraction should not rise without corresponding throughput gains.

Treat thresholds as comparison anchors, not hidden-score guarantees.

## 14. Changes that are usually unsafe

Avoid:

- hardcoding public scenario dimensions as identities;
- using `tick % fixed_cycle` as the main policy;
- switching on one-tick queue differences;
- linear, unbounded oldest-wait priority;
- local greedy phases at every intersection;
- relying on a link entry being present when occupancy is zero;
- treating reported link capacity as exact physical capacity;
- assuming travel time is always five;
- switching in the last ticks when the new green cannot be used;
- keeping runtime state without a robust scenario reset;
- using random decisions;
- optimizing the benchmark, scenarios, scorer, or tests instead of the
  controller;
- repeatedly looking at held-out results after every minor change.

## 15. Final verification

Before handing off:

1. run `git status --short`;
2. inspect the complete controller diff;
3. run compilation;
4. run all 25 tests;
5. run public benchmark;
6. run development scoring;
7. run held-out scoring once for the finalist;
8. run observer diagnostics on any behavior you changed;
9. run `git diff --check`;
10. check lints;
11. update measured candidate evidence;
12. state clearly that no submission occurred.

Your final report should include:

- the hypothesis;
- exact controller changes;
- root cause addressed;
- before/after public metrics;
- before/after family minima;
- before/after worst scenario ratio;
- wait versus unfinished tradeoffs;
- switch/transition changes;
- tests and commands run;
- remaining weakness;
- confirmation that no commit or submission was made unless explicitly
  requested.

## 16. Definition of a successful next iteration

A strong next iteration does at least one of the following:

- lifts lane-hotspot and development worst-case performance without damaging
  spillback;
- restores public balanced above baseline while preserving hidden-family
  robustness;
- improves terminal completions without increasing transition waste;
- replaces a brittle fixed assumption with reliable online inference.

The best controller is not the one with the highest attractive single score.
It is the one with the strongest evidence that no hidden traffic family can
break it.

## 17. Recommended prompt for the next agent

Copy and paste this prompt into a fresh agent session:

```text
Work autonomously on improving the Traffic Lights Arena controller in this
repository.

Before doing anything, read:
- AGENTS.md
- NEXT_AGENT_GUIDE.md
- TOURNAMENT_PLAN.md
- traffic-lights-arena/README.md
- traffic-lights-arena/controller.py
- the simulator engine, scenarios, scoring, and score profiles
- the relevant tournament_tools modules

Follow every repository rule. In particular:
- NEVER run submit.py, submission login, an event API, or any submission-related
  command.
- Do not commit unless I explicitly ask.
- Preserve all existing uncommitted and untracked work.
- For controller improvement, edit only
  traffic-lights-arena/controller.py.
- Do not weaken or alter the simulator, scenarios, scorer, benchmark, observer,
  or tests to improve reported results.
- Keep controller.py self-contained, deterministic, standard-library-only, and
  generic across hidden map sizes, demand windows, lane weights, capacities,
  travel times, and seeds.
- Never hardcode public or synthetic scenario identities.

Your objective is robust tournament performance, not the highest public or
average score. Rank candidates primarily by:
1. absolute worst scenario ratio;
2. the minimum result in each two-map family;
3. geometric mean of the six family minima;
4. unfinished vehicles and transition waste;
5. public geometric-mean score.

First reproduce the current baseline exactly:
- 25 tests passing;
- public GM approximately 14,826;
- held-out family-minimum GM approximately 1.0758;
- held-out worst ratio approximately 0.9801.

If reproduction differs, stop optimizing and diagnose the discrepancy.

Then inspect the current lower tail. The leading known weaknesses are:
- held-out 4x4 rotating lane-hotspot ratio around 0.9801;
- development worst ratio around 0.946;
- public balanced score around 9,554.

Do not assume the proposed explanations are correct. Use simulator mechanics,
benchmark records, and observer diagnostics to determine the cause. Form one
explicit, falsifiable hypothesis. State:
- what behavior is wrong;
- why the current code causes it;
- which metrics should improve;
- which scenarios are most likely to regress;
- the rollback condition.

Make one coherent controller change at a time. Prefer investigating, in this
order:
1. peak-lane sensitivity in demand aggregation;
2. light-balanced classification and cycle budget;
3. terminal completion behavior;
4. generic travel-time or capacity inference;
5. only then, a tightly gated local override.

Protect the previously fixed spillback behavior. Do not reintroduce short-green
starvation loops or excessive switching. The held-out spillback minimum should
remain near or above 1.10 unless overwhelming evidence justifies a trade.

Run validation in this order after each viable change:
1. compile controller.py;
2. controller contract and simulator tests;
3. public benchmark;
4. development synthetic family scoring;
5. observer diagnostics for changed behavior;
6. held-out scoring only for finalists.

Do not repeatedly tune against held-out seeds. Use the development suite for
iteration and held-out only as a validation gate.

Keep a candidate only if the evidence supports its hypothesis and it does not
create a severe lower-tail regression. Aim to:
- raise development worst ratio above the current ~0.946;
- keep held-out worst ratio near or above 0.9801;
- keep held-out family-minimum GM near or above 1.0758;
- keep spillback minimum near or above 1.10;
- preserve strong public northbound and city-rush gains;
- reduce public balanced regression if it can be done robustly;
- avoid increased transition fraction without clear throughput benefit.

You have authority to investigate, edit controller.py, and run local tests and
benchmarks. Continue until you either produce a demonstrably stronger
controller or have falsified the most promising safe hypotheses. Do not stop
after only proposing a change.

At the end, provide:
- root cause and hypothesis;
- exact controller changes;
- before/after public costs and scores;
- before/after development and held-out family minima;
- before/after worst scenario ratio;
- wait-tick versus unfinished-vehicle tradeoffs;
- switch count and transition-fraction changes where relevant;
- all commands and tests run;
- remaining weakness;
- explicit confirmation that no commit and no submission occurred.
```
