"""Synchronized, demand-proportional signal timing.

All intersections switch together, which keeps platoons moving through the
grid in both directions. Each axis receives green time proportional to a
rolling estimate of its demand, and the total cycle grows with network size
because larger grids lose more throughput per transition.
"""

# Rolling demand estimate, reset whenever a new simulation starts (tick 0).
_demand = {"ns": 1.0, "ew": 1.0}

_SMOOTHING = 0.1
_MIN_SHARE = 0.2

# (intersection_count, cycle_ticks) anchors tuned on the public maps.
_CYCLE_ANCHORS = ((4, 18), (6, 37), (9, 60))


def _cycle_length(intersection_count):
    """Interpolate total cycle length from the tuned anchor points."""
    anchors = _CYCLE_ANCHORS
    if intersection_count <= anchors[0][0]:
        return anchors[0][1]
    for (n0, c0), (n1, c1) in zip(anchors, anchors[1:]):
        if intersection_count <= n1:
            return c0 + (c1 - c0) * (intersection_count - n0) / (n1 - n0)
    # Extrapolate gently for larger grids, capped to stay responsive.
    n_last, c_last = anchors[-1]
    return min(80.0, c_last + 6.0 * (intersection_count - n_last))


def control(state):
    """Return the requested green phase for every intersection."""
    intersections = state["intersections"]
    count = len(intersections)
    cycle = _cycle_length(count)
    minimum_green = 9 if count <= 6 else 7

    ns_queue = sum(
        info["queues"]["N"] + info["queues"]["S"]
        for info in intersections.values()
    )
    ew_queue = sum(
        info["queues"]["E"] + info["queues"]["W"]
        for info in intersections.values()
    )

    if state["tick"] == 0:
        _demand["ns"] = 1.0
        _demand["ew"] = 1.0
    _demand["ns"] = (1 - _SMOOTHING) * _demand["ns"] + _SMOOTHING * ns_queue
    _demand["ew"] = (1 - _SMOOTHING) * _demand["ew"] + _SMOOTHING * ew_queue

    ns_share = _demand["ns"] / (_demand["ns"] + _demand["ew"] + 1e-9)
    ns_share = max(_MIN_SHARE, min(1 - _MIN_SHARE, ns_share))
    ns_green_limit = cycle * ns_share
    ew_green_limit = cycle - ns_green_limit

    # All signals share the same phase; read timing from any one of them.
    signal = next(iter(intersections.values()))
    phase = signal["phase"]
    age = signal["phase_age"]

    if phase == "NS_GREEN":
        should_switch = (
            age >= minimum_green
            and ew_queue > 0
            and (ns_queue == 0 or age >= ns_green_limit)
        )
        requested = "EW_GREEN" if should_switch else "NS_GREEN"
    elif phase == "EW_GREEN":
        should_switch = (
            age >= minimum_green
            and ns_queue > 0
            and (ew_queue == 0 or age >= ew_green_limit)
        )
        requested = "NS_GREEN" if should_switch else "EW_GREEN"
    else:
        requested = "NS_GREEN" if ns_queue >= ew_queue else "EW_GREEN"

    return {intersection_id: requested for intersection_id in intersections}
