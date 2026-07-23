"""Demand-responsive, network-coordinated traffic light controller.

The evaluator may keep this module loaded for several scenarios, so all
history is deliberately held in one resettable runtime dictionary.
"""

DIRECTIONS = ("N", "S", "E", "W")
AXIS_DIRECTIONS = {"NS": ("N", "S"), "EW": ("E", "W")}
OPPOSITE = {"NS": "EW", "EW": "NS"}

DEMAND_ALPHA = 0.10
TARGET_ALPHA = 0.35
GREEN_BUDGET = 30
MIN_TARGET_GREEN = 7
EMERGENCY_MIN_GREEN = 13
MAX_CONTINUOUS_GREEN = 48
ABS_SWITCH_MARGIN = 0.30
REL_SWITCH_MARGIN = 1.08
STARVATION_WAIT = 34
ENDGAME_TICKS = 44
TRANSITION_TICKS = 3
LIGHT_QUEUE_LIMIT = 6.0
LIGHT_DEMAND_RATIO = 1.35
LIGHT_GREEN_BUDGET = 24
BACKLOG_WEIGHT_MAX = 0.65
BACKLOG_QUEUE_SCALE = 12.0
PRESSURE_SHARE_ALPHA = 0.05

_runtime = {}


def _reset(state):
    """Build fresh state without retaining observations from another map."""
    intersections = state.get("intersections", {})
    dimensions = state.get("map", {})
    _runtime.clear()
    _runtime.update(
        {
            "tick": -1,
            "shape": (dimensions.get("rows", 0), dimensions.get("cols", 0)),
            "ids": frozenset(intersections),
            "previous_queues": {},
            "previous_features": {},
            "demand": {},
            "link_capacities": {},
            "last_green": "NS",
            "requested": "NS",
            "pending_target": None,
            "targets": {"NS": GREEN_BUDGET // 2, "EW": GREEN_BUDGET // 2},
            "green_started": 0,
            "starvation": {"NS": 0, "EW": 0},
            "spillback": 0.0,
            "pressure_share": 0.5,
            "coordination_mode": "adaptive",
        }
    )


def _coordinates(intersection_id):
    """Decode the engine's row-letter/column-number intersection IDs."""
    split = 0
    while split < len(intersection_id) and intersection_id[split].isalpha():
        split += 1
    letters = intersection_id[:split].upper()
    number = intersection_id[split:]
    row = 0
    for letter in letters:
        row = row * 26 + ord(letter) - 64
    try:
        col = int(number)
    except (TypeError, ValueError):
        col = 1
    return max(0, row - 1), max(0, col - 1)


def _id_lookup(intersections):
    return {_coordinates(item): item for item in intersections}


def _neighbor(row, col, direction, rows, cols, lookup):
    if direction == "N":
        row -= 1
    elif direction == "S":
        row += 1
    elif direction == "E":
        col += 1
    else:
        col -= 1
    if 0 <= row < rows and 0 <= col < cols:
        return lookup.get((row, col))
    return None


def _is_external(row, col, direction, rows, cols):
    return (
        (direction == "N" and row == rows - 1)
        or (direction == "S" and row == 0)
        or (direction == "E" and col == 0)
        or (direction == "W" and col == cols - 1)
    )


def _estimated_physical_capacity(axis, rows, cols):
    """Conservative capacity implied by the generic grid geometry."""
    if axis == "EW":
        if cols <= 1:
            return 8
        segment = (576.0 if cols == 2 else 816.0 / (cols - 1))
    else:
        if rows <= 1:
            return 8
        segment = (364.0 if rows == 2 else 490.0 / (rows - 1))
    return max(1, int((segment - 110.0) / 29.0) + 1)


def _read_links(state):
    occupancy = {}
    for link in state.get("links", {}).values():
        source = link.get("from")
        target = link.get("to")
        if source is None or target is None:
            continue
        occupancy[(source, target)] = max(0, int(link.get("vehicles", 0)))
        capacity = max(1, int(link.get("capacity", 1)))
        _runtime["link_capacities"][(source, target)] = capacity
    return occupancy


def _observe(state):
    """Normalize queues, topology, reservations, and horizon features."""
    raw_intersections = state.get("intersections", {})
    rows, cols = _runtime["shape"]
    lookup = _id_lookup(raw_intersections)
    link_occupancy = _read_links(state)
    remaining = max(0, int(state.get("remaining_ticks", 0)))
    features = {}
    axis_totals = {
        "NS": {"queue": 0.0, "oldest": 0, "terminal": 0.0, "pressure": 0.0},
        "EW": {"queue": 0.0, "oldest": 0, "terminal": 0.0, "pressure": 0.0},
    }

    for item, raw in raw_intersections.items():
        row, col = _coordinates(item)
        queues = raw.get("queues", {})
        waits = raw.get("oldest_wait", {})
        phase = raw.get("phase", "NS_GREEN")
        for direction in DIRECTIONS:
            axis = "NS" if direction in ("N", "S") else "EW"
            queue = max(0, int(queues.get(direction, 0)))
            oldest = max(0, int(waits.get(direction, 0)))
            target = _neighbor(row, col, direction, rows, cols, lookup)
            terminal = target is None
            external = _is_external(row, col, direction, rows, cols)
            in_link = 0 if terminal else link_occupancy.get((item, target), 0)
            downstream_queue = (
                0
                if terminal
                else max(
                    0,
                    int(raw_intersections[target].get("queues", {}).get(direction, 0)),
                )
            )
            reserved = downstream_queue + in_link
            reported = _runtime["link_capacities"].get((item, target), 8)
            physical = _estimated_physical_capacity(axis, rows, cols)
            capacity = max(1, min(reported, physical))

            if terminal:
                space_factor = 1.0
            else:
                free_ratio = (capacity - reserved) / capacity
                if free_ratio <= 0:
                    space_factor = 0.03
                elif free_ratio < 0.34:
                    space_factor = 0.18 + 1.2 * free_ratio
                else:
                    space_factor = min(1.0, 0.55 + 0.75 * free_ratio)

            wait_term = min(oldest, 45) / 45.0
            base_pressure = queue + min(1.8, 1.8 * wait_term)
            pressure = base_pressure * space_factor
            if terminal:
                pressure *= 1.35
            elif oldest >= STARVATION_WAIT:
                # A blocked queue should remain visible without dominating.
                pressure += min(1.5, (oldest - STARVATION_WAIT + 1) / 14.0)

            hops = 0
            probe = target
            probe_row, probe_col = row, col
            while probe is not None:
                hops += 1
                probe_row, probe_col = _coordinates(probe)
                probe = _neighbor(
                    probe_row, probe_col, direction, rows, cols, lookup
                )

            if remaining <= ENDGAME_TICKS:
                useful_by = hops * 5 + TRANSITION_TICKS
                if terminal:
                    pressure *= 1.75
                elif useful_by >= remaining:
                    pressure *= 0.12
                else:
                    pressure *= max(0.25, (remaining - useful_by) / ENDGAME_TICKS)

            feature = {
                "queue": queue,
                "oldest": oldest,
                "axis": axis,
                "phase": phase,
                "phase_age": max(0, int(raw.get("phase_age", 0))),
                "can_switch": bool(raw.get("can_switch", False)),
                "external": external,
                "terminal": terminal,
                "target": target,
                "downstream_occupancy": in_link,
                "reservation": reserved,
                "capacity": capacity,
                "space_factor": space_factor,
                "hops": hops,
                "pressure": pressure,
            }
            features[(item, direction)] = feature
            totals = axis_totals[axis]
            totals["queue"] += queue
            totals["oldest"] = max(totals["oldest"], oldest)
            totals["pressure"] += pressure
            if terminal:
                totals["terminal"] += queue

    return {
        "tick": int(state.get("tick", 0)),
        "remaining": remaining,
        "features": features,
        "axis": axis_totals,
        "intersections": raw_intersections,
    }


def _estimate_demand(observation):
    """Update external-lane arrival EWMAs from queue conservation."""
    previous_queues = _runtime["previous_queues"]
    previous_features = _runtime["previous_features"]
    demand = _runtime["demand"]
    lanes = {"NS": [], "EW": []}

    for key, feature in observation["features"].items():
        if not feature["external"]:
            continue
        queue = feature["queue"]
        previous_queue = previous_queues.get(key, 0)
        previous = previous_features.get(key)
        estimated_departure = 0.0
        if (
            previous
            and previous_queue > 0
            and previous["oldest"] >= 5
            and previous["phase"] == previous["axis"] + "_GREEN"
            and previous["space_factor"] > 0.30
        ):
            estimated_departure = min(1.0, previous["space_factor"])
        arrivals = max(0.0, min(1.0, queue - previous_queue + estimated_departure))
        old = demand.get(key, 0.10)
        updated = (1.0 - DEMAND_ALPHA) * old + DEMAND_ALPHA * arrivals
        demand[key] = updated
        lanes[feature["axis"]].append(updated)

    estimates = {}
    for axis in ("NS", "EW"):
        values = lanes[axis]
        if not values:
            estimates[axis] = 0.10
            continue
        mean = sum(values) / len(values)
        peak = max(values)
        estimates[axis] = 0.82 * mean + 0.18 * peak
    return estimates


def _update_targets(demand, observation):
    queued_internal = [
        feature
        for feature in observation["features"].values()
        if not feature["terminal"] and feature["queue"] > 0
    ]
    blocked = sum(
        1 for feature in queued_internal if feature["space_factor"] <= 0.25
    )
    blocked_ratio = blocked / max(1, len(queued_internal))
    _runtime["spillback"] = blocked_ratio
    budget = GREEN_BUDGET
    if blocked >= 3 and blocked_ratio >= 0.35:
        budget = max(2 * MIN_TARGET_GREEN, GREEN_BUDGET - 4)

    total = demand["NS"] + demand["EW"]
    intersection_count = max(1, len(observation["intersections"]))
    queue_per_intersection = (
        observation["axis"]["NS"]["queue"] + observation["axis"]["EW"]["queue"]
    ) / intersection_count
    demand_ratio = max(demand.values()) / max(min(demand.values()), 0.001)
    light_balanced = (
        blocked == 0
        and queue_per_intersection < LIGHT_QUEUE_LIMIT
        and demand_ratio < LIGHT_DEMAND_RATIO
    )
    if light_balanced:
        budget = max(2 * MIN_TARGET_GREEN, LIGHT_GREEN_BUDGET)
        ns_target = budget // 2
        _runtime["coordination_mode"] = "light-balanced"
    else:
        desired_share = demand["NS"] / max(total, 0.001)
        # Arrival-based shares under-allocate green when one axis is
        # throughput-limited by tight links: its serviceable backlog keeps
        # growing while its external arrival rate looks ordinary. As
        # congestion builds, steer the split toward the cycle-averaged
        # axis-pressure share. The instantaneous share is biased toward the
        # currently green axis (red-axis links fill and get discounted), so
        # only the slow EWMA is safe to steer with.
        backlog_weight = BACKLOG_WEIGHT_MAX * min(
            1.0, queue_per_intersection / BACKLOG_QUEUE_SCALE
        )
        desired_share += backlog_weight * (
            _runtime["pressure_share"] - desired_share
        )
        old_total = _runtime["targets"]["NS"] + _runtime["targets"]["EW"]
        old_share = _runtime["targets"]["NS"] / max(old_total, 1)
        ns_target = round(
            budget
            * ((1.0 - TARGET_ALPHA) * old_share + TARGET_ALPHA * desired_share)
        )
        _runtime["coordination_mode"] = "adaptive"
    ns_target = max(
        MIN_TARGET_GREEN,
        min(budget - MIN_TARGET_GREEN, ns_target),
    )
    _runtime["targets"]["NS"] = ns_target
    _runtime["targets"]["EW"] = budget - ns_target


def _actual_axis(observation):
    phases = [
        raw.get("phase", "NS_GREEN")
        for raw in observation["intersections"].values()
    ]
    greens = [phase[:2] for phase in phases if phase in ("NS_GREEN", "EW_GREEN")]
    if greens and len(greens) == len(phases):
        return greens[0]
    return None


def _choose_axis(observation, demand):
    """Coordinate one axis globally while guarding costly transitions."""
    current = _actual_axis(observation)
    pending = _runtime["pending_target"]

    # Yellow and all-red expose no target in the public state. Preserve ours.
    if current is None:
        return pending or _runtime["requested"]

    if pending is not None:
        if current == pending:
            _runtime["pending_target"] = None
            _runtime["last_green"] = current
            _runtime["green_started"] = observation["tick"]
            _update_targets(demand, observation)
        else:
            return pending

    other = OPPOSITE[current]
    _runtime["last_green"] = current
    current_data = observation["axis"][current]
    other_data = observation["axis"][other]
    elapsed = max(
        (
            raw.get("phase_age", 0)
            for raw in observation["intersections"].values()
            if raw.get("phase") == current + "_GREEN"
        ),
        default=max(0, observation["tick"] - _runtime["green_started"]),
    )
    all_can_switch = all(
        bool(raw.get("can_switch", False))
        for raw in observation["intersections"].values()
    )

    if other_data["queue"] > 0:
        _runtime["starvation"][other] += 1
    else:
        _runtime["starvation"][other] = 0
    _runtime["starvation"][current] = 0

    remaining = observation["remaining"]
    if not all_can_switch or elapsed < MIN_TARGET_GREEN:
        return current
    if remaining <= TRANSITION_TICKS + 1:
        return current

    current_value = current_data["pressure"] + 3.0 * demand[current]
    other_value = other_data["pressure"] + 3.0 * demand[other]
    useful_other = other_data["pressure"] > 0.10
    margin_met = (
        other_value >= current_value * REL_SWITCH_MARGIN
        and other_value - current_value >= ABS_SWITCH_MARGIN
    )
    if _runtime["coordination_mode"] == "light-balanced":
        margin_met = True
    target_elapsed = elapsed >= _runtime["targets"][current]
    starvation_floor = EMERGENCY_MIN_GREEN
    if current_data["oldest"] >= STARVATION_WAIT:
        # Bilateral congestion: both axes hold starving vehicles, so an early
        # escape only shortens every cycle. Let the allocated target govern.
        starvation_floor = max(EMERGENCY_MIN_GREEN, _runtime["targets"][current])
    starved = elapsed >= starvation_floor and (
        other_data["oldest"] >= STARVATION_WAIT
        or _runtime["starvation"][other] >= MAX_CONTINUOUS_GREEN
    )
    forced = elapsed >= MAX_CONTINUOUS_GREEN

    should_switch = useful_other and (
        (target_elapsed and margin_met) or starved or forced
    )

    if remaining <= ENDGAME_TICKS:
        current_terminal = current_data["terminal"]
        other_terminal = other_data["terminal"]
        productive_ticks = remaining - TRANSITION_TICKS
        if productive_ticks <= 1:
            should_switch = False
        elif remaining <= 8 and current_terminal > 0 and other_terminal == 0:
            should_switch = False

    if should_switch:
        _runtime["pending_target"] = other
        _runtime["requested"] = other
        return other
    return current


def control(state):
    """Return one complete, deterministic, globally coordinated decision."""
    tick = int(state.get("tick", 0))
    dimensions = state.get("map", {})
    shape = (dimensions.get("rows", 0), dimensions.get("cols", 0))
    ids = frozenset(state.get("intersections", {}))
    if (
        not _runtime
        or tick == 0
        or tick <= _runtime.get("tick", -1)
        or shape != _runtime.get("shape")
        or ids != _runtime.get("ids")
    ):
        _reset(state)

    observation = _observe(state)
    pressure_ns = observation["axis"]["NS"]["pressure"]
    pressure_total = pressure_ns + observation["axis"]["EW"]["pressure"]
    if pressure_total > 0.001:
        _runtime["pressure_share"] += PRESSURE_SHARE_ALPHA * (
            pressure_ns / pressure_total - _runtime["pressure_share"]
        )
    demand = _estimate_demand(observation)
    requested_axis = _choose_axis(observation, demand)
    requested_phase = requested_axis + "_GREEN"

    _runtime["tick"] = tick
    _runtime["requested"] = requested_axis
    _runtime["previous_queues"] = {
        key: feature["queue"] for key, feature in observation["features"].items()
    }
    _runtime["previous_features"] = observation["features"]

    return {item: requested_phase for item in state.get("intersections", {})}
