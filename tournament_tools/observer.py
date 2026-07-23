"""Non-invasive diagnostics for Traffic Lights Arena controllers.

``ObservedController`` wraps any controller callable accepted by the simulator.
It observes the state before invoking the controller, gives the controller a
deep copy, and returns the controller's result unchanged.

Some measurements are necessarily proxies because the public controller state
does not contain vehicle routes or post-tick movements:

* a switch start is inferred when a switchable green receives the other green;
* a blocked green requires a queued, green approach whose downstream
  reservation reaches the last reported link capacity;
* a final opportunity is queued traffic on a terminal approach whose axis is
  green (or requested green).

The diagnostics contain only JSON-compatible values.  No controller or engine
changes are required, and unavailable optional pressure internals are ignored.
"""

from __future__ import annotations

import copy
import functools
import json
import math
from collections.abc import Callable, Mapping
from typing import Any


_DIRECTIONS = ("N", "S", "E", "W")
_GREEN_PHASES = ("NS_GREEN", "EW_GREEN")
_TRANSITION_PHASES = ("YELLOW", "NS_YELLOW", "EW_YELLOW", "ALL_RED")


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> float:
    """Coerce a numeric observation to a finite non-negative float."""
    number = _finite_number(value)
    return max(number, 0.0) if number is not None else 0.0


def _natural_number(value: Any) -> int | None:
    """Return an integer-like value, or ``None`` when it is unavailable."""
    number = _finite_number(value)
    if number is None:
        return None
    return int(number)


def _display_number(value: float) -> int | float:
    """Keep integral diagnostics compact while preserving fractional pressure."""
    return int(value) if value.is_integer() else value


def _phase_axis(phase: Any) -> str | None:
    """Map a green phase to its served axis."""
    if phase == "NS_GREEN":
        return "NS"
    if phase == "EW_GREEN":
        return "EW"
    return None


def _direction_axis(direction: str) -> str:
    """Map a cardinal travel direction to its signal axis."""
    return "NS" if direction in ("N", "S") else "EW"


def _row_from_letters(letters: str) -> int | None:
    """Decode the alphabetic row prefix used by arena intersection IDs."""
    if not letters or not letters.isalpha():
        return None
    row = 0
    for character in letters.upper():
        row = row * 26 + ord(character) - ord("A") + 1
    return row - 1


def _intersection_position(intersection_id: str) -> tuple[int, int] | None:
    """Parse IDs such as ``A1`` and ``AA12`` into zero-based coordinates."""
    split = 0
    while split < len(intersection_id) and intersection_id[split].isalpha():
        split += 1
    row = _row_from_letters(intersection_id[:split])
    column_text = intersection_id[split:]
    if row is None or not column_text.isdigit():
        return None
    column = int(column_text) - 1
    return (row, column) if column >= 0 else None


def _map_signature(state: Mapping[str, Any]) -> tuple[Any, ...]:
    """Build the scenario identity used to prevent diagnostic leakage."""
    map_data = state.get("map")
    if not isinstance(map_data, Mapping):
        map_data = {}
    intersections = state.get("intersections")
    ids = intersections.keys() if isinstance(intersections, Mapping) else ()
    return (
        _natural_number(map_data.get("rows")),
        _natural_number(map_data.get("cols")),
        tuple(sorted(str(item) for item in ids)),
    )


def _topology(
    state: Mapping[str, Any],
) -> tuple[int | None, int | None, dict[str, tuple[int, int]], dict[tuple[int, int], str]]:
    """Derive grid coordinates from map dimensions and intersection IDs."""
    signature = _map_signature(state)
    rows, cols, ids = signature
    positions: dict[str, tuple[int, int]] = {}
    for item in ids:
        position = _intersection_position(item)
        if position is not None:
            positions[item] = position

    # A row-major fallback keeps custom IDs useful without assuming more than
    # the dimensions and the complete ID set supplied by the state.
    if (
        rows is not None
        and cols is not None
        and rows >= 0
        and cols >= 0
        and len(ids) == rows * cols
        and len(positions) != len(ids)
    ):
        positions = {
            item: (index // cols, index % cols)
            for index, item in enumerate(ids)
        }
    by_position = {position: item for item, position in positions.items()}
    return rows, cols, positions, by_position


def _downstream_position(
    row: int, col: int, direction: str
) -> tuple[int, int]:
    """Return the next grid coordinate for a vehicle direction."""
    if direction == "N":
        return row - 1, col
    if direction == "S":
        return row + 1, col
    if direction == "E":
        return row, col + 1
    return row, col - 1


def _pressure_pair(value: Any) -> tuple[float, float] | None:
    """Extract an NS/EW pressure pair from a known diagnostics mapping."""
    if not isinstance(value, Mapping):
        return None
    normalized = {
        str(key).upper().replace("-", "_").replace(" ", "_"): item
        for key, item in value.items()
    }
    key_pairs = (
        ("NS", "EW"),
        ("NS_GREEN", "EW_GREEN"),
        ("NORTH_SOUTH", "EAST_WEST"),
        ("NS_PRESSURE", "EW_PRESSURE"),
    )
    for ns_key, ew_key in key_pairs:
        ns = _finite_number(normalized.get(ns_key))
        ew = _finite_number(normalized.get(ew_key))
        if ns is not None and ew is not None:
            return ns, ew
        ns_item = normalized.get(ns_key)
        ew_item = normalized.get(ew_key)
        if isinstance(ns_item, Mapping) and isinstance(ew_item, Mapping):
            ns = _finite_number(ns_item.get("pressure"))
            ew = _finite_number(ew_item.get("pressure"))
            if ns is not None and ew is not None:
                return ns, ew
    for key in ("AXIS_PRESSURE", "PRESSURE_BY_AXIS", "PRESSURES"):
        pair = _pressure_pair(normalized.get(key))
        if pair is not None:
            return pair
    axis_pair = _pressure_pair(normalized.get("AXIS"))
    if axis_pair is not None:
        return axis_pair
    for key in ("FEATURES", "PREVIOUS_FEATURES"):
        features = normalized.get(key)
        if not isinstance(features, Mapping):
            continue
        totals = {"NS": 0.0, "EW": 0.0}
        found = False
        for feature in features.values():
            if not isinstance(feature, Mapping):
                continue
            axis = feature.get("axis")
            pressure = _finite_number(feature.get("pressure"))
            if axis in totals and pressure is not None:
                totals[axis] += pressure
                found = True
        if found:
            return totals["NS"], totals["EW"]
    return None


def _optional_axis_pressure(
    controller: Callable[[dict[str, Any]], Any],
    state: Mapping[str, Any],
) -> tuple[float, float] | None:
    """Read common pressure exports without requiring a controller contract."""
    candidates: list[Any] = [
        state.get("axis_pressure"),
        state.get("pressure_by_axis"),
        state.get("pressures"),
        state.get("diagnostics"),
    ]
    owners = [controller, getattr(controller, "__self__", None)]
    attribute_names = (
        "axis_pressure",
        "last_axis_pressure",
        "pressure_by_axis",
        "pressures",
        "last_diagnostics",
        "diagnostic_state",
        "runtime_state",
        "_runtime",
        "_state",
    )
    for owner in owners:
        if owner is None:
            continue
        for name in attribute_names:
            try:
                candidates.append(getattr(owner, name))
            except (AttributeError, RuntimeError):
                continue
    globals_mapping = getattr(controller, "__globals__", None)
    if isinstance(globals_mapping, Mapping):
        for name in (
            "axis_pressure",
            "_axis_pressure",
            "diagnostics",
            "_diagnostics",
            "runtime",
            "_runtime",
        ):
            candidates.append(globals_mapping.get(name))

    for candidate in candidates:
        pair = _pressure_pair(candidate)
        if pair is not None:
            return pair
        if isinstance(candidate, Mapping):
            for key in ("axis_pressure", "pressure_by_axis", "pressures"):
                pair = _pressure_pair(candidate.get(key))
                if pair is not None:
                    return pair
    return None


class ObservedController:
    """Callable controller wrapper that accumulates reset-safe diagnostics.

    The returned decision object is exactly the wrapped callable's return value.
    Observation failures caused by missing or malformed optional fields do not
    replace controller or simulator validation errors.
    """

    def __init__(self, controller: Callable[[dict[str, Any]], Any]) -> None:
        if not callable(controller):
            raise TypeError("controller must be callable")
        self.controller = controller
        functools.update_wrapper(self, controller, updated=())
        self.reset()

    def reset(self) -> None:
        """Clear all observations, including inferred cross-tick state."""
        self._map_signature: tuple[Any, ...] | None = None
        self._last_tick: int | None = None
        self._first_tick: int | None = None
        self._ticks_observed = 0

        self._requested_phase_counts = {phase: 0 for phase in _GREEN_PHASES}
        self._observed_phase_counts: dict[str, int] = {}
        self._inferred_switch_starts = 0
        self._switch_starts_by_intersection: dict[str, int] = {}
        self._switch_start_ticks: list[int] = []

        self._transition_observation_ticks = 0
        self._transition_intersection_observations = 0
        self._transition_ticks: list[int] = []

        self._queue_observed_total = 0.0
        self._queue_peak_total = 0.0
        self._queue_peak_approach = 0.0
        self._queue_latest_total = 0.0
        self._queue_axis_observed_totals = {"NS": 0.0, "EW": 0.0}
        self._queue_axis_peak_totals = {"NS": 0.0, "EW": 0.0}
        self._maximum_oldest_wait = 0.0

        self._known_link_capacity: float | None = None
        self._link_observations = 0
        self._link_saturation_observations = 0
        self._link_saturation_ticks = 0
        self._link_saturation_tick_values: list[int] = []
        self._peak_link_saturation_ratio = 0.0

        self._blocked_green_proxy_ticks = 0
        self._blocked_green_approach_observations = 0
        self._blocked_green_tick_values: list[int] = []

        self._axis_pressure_observations = 0
        self._axis_pressure_sums = {"NS": 0.0, "EW": 0.0}
        self._axis_pressure_peaks: dict[str, float | None] = {
            "NS": None,
            "EW": None,
        }
        self._axis_pressure_latest: dict[str, float] | None = None

        self._final_ticks_observed = 0
        self._final_queue_observed_total = 0.0
        self._final_queue_peak_total = 0.0
        self._final_terminal_queue_observed_total = 0.0
        self._final_terminal_queue_peak = 0.0
        self._final_actual_green_terminal_queue = 0.0
        self._final_requested_green_terminal_queue = 0.0
        self._final_actual_terminal_opportunity_approaches = 0
        self._final_requested_terminal_opportunity_approaches = 0
        self._final_completed_delta_observed = 0
        self._previous_completed: int | None = None

        self._current_final_30 = False
        self._current_terminal_queues: dict[tuple[str, str], float] = {}

    def __call__(self, state: dict[str, Any]) -> Any:
        """Observe one state, invoke the controller on a deep copy, and return."""
        controller_state = copy.deepcopy(state)
        if isinstance(state, Mapping):
            try:
                self._record_state(state)
            except (AttributeError, KeyError, TypeError, ValueError):
                # Instrumentation is deliberately best-effort; the wrapped
                # controller and engine remain the authorities on state validity.
                self._current_final_30 = False
                self._current_terminal_queues = {}

        decisions = self.controller(controller_state)

        if isinstance(state, Mapping):
            try:
                self._record_decisions(state, decisions)
                self._record_axis_pressure(state)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        return decisions

    def _record_state(self, state: Mapping[str, Any]) -> None:
        """Reset when needed, then aggregate one pre-transition state."""
        tick = _natural_number(state.get("tick"))
        signature = _map_signature(state)
        should_reset = (
            tick == 0
            or (tick is not None and self._last_tick is not None and tick < self._last_tick)
            or (self._map_signature is not None and signature != self._map_signature)
        )
        if should_reset:
            self.reset()
        self._map_signature = signature
        self._last_tick = tick
        if self._first_tick is None:
            self._first_tick = tick
        self._ticks_observed += 1

        intersections = state.get("intersections")
        if not isinstance(intersections, Mapping):
            intersections = {}
        rows, cols, positions, by_position = _topology(state)
        links = self._read_links(state)

        tick_queue_total = 0.0
        tick_axis_totals = {"NS": 0.0, "EW": 0.0}
        tick_terminal_total = 0.0
        tick_actual_terminal_queue = 0.0
        actual_terminal_opportunities = 0
        transition_count = 0
        blocked_count = 0
        terminal_queues: dict[tuple[str, str], float] = {}

        for raw_item, raw_data in intersections.items():
            item = str(raw_item)
            data = raw_data if isinstance(raw_data, Mapping) else {}
            phase = data.get("phase")
            phase_name = str(phase)
            self._observed_phase_counts[phase_name] = (
                self._observed_phase_counts.get(phase_name, 0) + 1
            )
            if phase in _TRANSITION_PHASES:
                transition_count += 1

            queues = data.get("queues")
            if not isinstance(queues, Mapping):
                queues = {}
            oldest = data.get("oldest_wait")
            if not isinstance(oldest, Mapping):
                oldest = {}
            position = positions.get(item)

            for direction in _DIRECTIONS:
                queue = _nonnegative_number(queues.get(direction))
                axis = _direction_axis(direction)
                tick_queue_total += queue
                tick_axis_totals[axis] += queue
                self._queue_peak_approach = max(self._queue_peak_approach, queue)
                self._maximum_oldest_wait = max(
                    self._maximum_oldest_wait,
                    _nonnegative_number(oldest.get(direction)),
                )
                if position is None or rows is None or cols is None:
                    continue
                downstream_position = _downstream_position(*position, direction)
                target = by_position.get(downstream_position)
                is_terminal = target is None
                if is_terminal:
                    terminal_queues[(item, direction)] = queue
                    tick_terminal_total += queue
                    if queue > 0 and _phase_axis(phase) == axis:
                        tick_actual_terminal_queue += queue
                        actual_terminal_opportunities += 1
                    continue
                if queue <= 0 or _phase_axis(phase) != axis:
                    continue
                edge = links.get((item, target))
                link_vehicles = edge[0] if edge is not None else 0.0
                capacity = edge[1] if edge is not None else self._known_link_capacity
                target_data = intersections.get(target)
                target_queues = (
                    target_data.get("queues")
                    if isinstance(target_data, Mapping)
                    else None
                )
                downstream_queue = (
                    _nonnegative_number(target_queues.get(direction))
                    if isinstance(target_queues, Mapping)
                    else 0.0
                )
                if capacity is not None and downstream_queue + link_vehicles >= capacity:
                    blocked_count += 1

        self._queue_observed_total += tick_queue_total
        self._queue_peak_total = max(self._queue_peak_total, tick_queue_total)
        self._queue_latest_total = tick_queue_total
        for axis in ("NS", "EW"):
            self._queue_axis_observed_totals[axis] += tick_axis_totals[axis]
            self._queue_axis_peak_totals[axis] = max(
                self._queue_axis_peak_totals[axis], tick_axis_totals[axis]
            )

        if transition_count:
            self._transition_observation_ticks += 1
            self._transition_intersection_observations += transition_count
            if tick is not None:
                self._transition_ticks.append(tick)
        if blocked_count:
            self._blocked_green_proxy_ticks += 1
            self._blocked_green_approach_observations += blocked_count
            if tick is not None:
                self._blocked_green_tick_values.append(tick)

        remaining = _natural_number(state.get("remaining_ticks"))
        self._current_final_30 = remaining is not None and 0 <= remaining <= 30
        self._current_terminal_queues = terminal_queues
        completed = self._read_completed(state)
        if self._current_final_30:
            self._final_ticks_observed += 1
            self._final_queue_observed_total += tick_queue_total
            self._final_queue_peak_total = max(
                self._final_queue_peak_total, tick_queue_total
            )
            self._final_terminal_queue_observed_total += tick_terminal_total
            self._final_terminal_queue_peak = max(
                self._final_terminal_queue_peak, tick_terminal_total
            )
            self._final_actual_green_terminal_queue += tick_actual_terminal_queue
            self._final_actual_terminal_opportunity_approaches += (
                actual_terminal_opportunities
            )
            if completed is not None and self._previous_completed is not None:
                self._final_completed_delta_observed += max(
                    completed - self._previous_completed, 0
                )
        self._previous_completed = completed

    def _read_links(
        self, state: Mapping[str, Any]
    ) -> dict[tuple[str, str], tuple[float, float | None]]:
        """Normalize visible links and aggregate reported saturation."""
        raw_links = state.get("links")
        if not isinstance(raw_links, Mapping):
            return {}
        links: dict[tuple[str, str], tuple[float, float | None]] = {}
        saturated_this_tick = False
        for raw_key, raw_link in raw_links.items():
            if not isinstance(raw_link, Mapping):
                continue
            source = raw_link.get("from")
            target = raw_link.get("to")
            if source is None or target is None:
                key = str(raw_key)
                if "->" not in key:
                    continue
                source, target = key.split("->", 1)
            vehicles = _nonnegative_number(raw_link.get("vehicles"))
            capacity_value = _finite_number(raw_link.get("capacity"))
            capacity = (
                capacity_value
                if capacity_value is not None and capacity_value > 0
                else None
            )
            if capacity is not None:
                if self._known_link_capacity is None:
                    self._known_link_capacity = capacity
                else:
                    self._known_link_capacity = min(
                        self._known_link_capacity, capacity
                    )
                ratio = vehicles / capacity
                self._peak_link_saturation_ratio = max(
                    self._peak_link_saturation_ratio, ratio
                )
                if vehicles >= capacity:
                    self._link_saturation_observations += 1
                    saturated_this_tick = True
            self._link_observations += 1
            links[(str(source), str(target))] = (vehicles, capacity)
        if saturated_this_tick:
            self._link_saturation_ticks += 1
            if self._last_tick is not None:
                self._link_saturation_tick_values.append(self._last_tick)
        return links

    @staticmethod
    def _read_completed(state: Mapping[str, Any]) -> int | None:
        """Read the cumulative completion counter when the engine provides it."""
        vehicles = state.get("vehicles")
        if not isinstance(vehicles, Mapping):
            return None
        return _natural_number(vehicles.get("completed"))

    def _record_decisions(
        self, state: Mapping[str, Any], decisions: Any
    ) -> None:
        """Aggregate requests, inferred starts, and requested opportunities."""
        if not isinstance(decisions, Mapping):
            return
        intersections = state.get("intersections")
        if not isinstance(intersections, Mapping):
            intersections = {}
        tick = _natural_number(state.get("tick"))
        switch_count = 0

        for raw_item, requested in decisions.items():
            item = str(raw_item)
            if requested in _GREEN_PHASES:
                self._requested_phase_counts[requested] += 1
            data = intersections.get(raw_item)
            if data is None:
                data = intersections.get(item)
            if not isinstance(data, Mapping):
                data = {}
            observed = data.get("phase")
            can_switch = data.get("can_switch")
            if can_switch is None:
                phase_age = _natural_number(data.get("phase_age"))
                can_switch = phase_age is None or phase_age >= 5
            if (
                requested in _GREEN_PHASES
                and observed in _GREEN_PHASES
                and requested != observed
                and bool(can_switch)
            ):
                switch_count += 1
                self._switch_starts_by_intersection[item] = (
                    self._switch_starts_by_intersection.get(item, 0) + 1
                )

            if not self._current_final_30:
                continue
            requested_axis = _phase_axis(requested)
            for direction in _DIRECTIONS:
                queue = self._current_terminal_queues.get((item, direction), 0.0)
                if queue > 0 and requested_axis == _direction_axis(direction):
                    self._final_requested_green_terminal_queue += queue
                    self._final_requested_terminal_opportunity_approaches += 1

        if switch_count:
            self._inferred_switch_starts += switch_count
            if tick is not None:
                self._switch_start_ticks.extend([tick] * switch_count)

    def _record_axis_pressure(self, state: Mapping[str, Any]) -> None:
        """Aggregate optional pressure values exported by the controller."""
        pair = _optional_axis_pressure(self.controller, state)
        if pair is None:
            return
        self._axis_pressure_observations += 1
        self._axis_pressure_latest = {"NS": pair[0], "EW": pair[1]}
        for axis, value in zip(("NS", "EW"), pair):
            self._axis_pressure_sums[axis] += value
            peak = self._axis_pressure_peaks[axis]
            self._axis_pressure_peaks[axis] = (
                value if peak is None else max(peak, value)
            )

    def diagnostics(self) -> dict[str, Any]:
        """Return an independent, JSON-serializable aggregate snapshot."""
        ticks = self._ticks_observed
        pressure_count = self._axis_pressure_observations
        queue_mean = self._queue_observed_total / ticks if ticks else 0.0
        transition_fraction = (
            self._transition_observation_ticks / ticks if ticks else 0.0
        )
        final_ticks = self._final_ticks_observed

        result: dict[str, Any] = {
            "version": 1,
            "ticks_observed": ticks,
            "first_tick": self._first_tick,
            "last_tick": self._last_tick,
            "map": {
                "rows": self._map_signature[0] if self._map_signature else None,
                "cols": self._map_signature[1] if self._map_signature else None,
                "intersection_ids": (
                    list(self._map_signature[2]) if self._map_signature else []
                ),
            },
            "requested_phase_counts": dict(self._requested_phase_counts),
            "observed_phase_counts": dict(self._observed_phase_counts),
            "inferred_switch_starts": self._inferred_switch_starts,
            "inferred_switch_starts_by_intersection": dict(
                self._switch_starts_by_intersection
            ),
            "inferred_switch_start_ticks": list(self._switch_start_ticks),
            "transition_observation_ticks": self._transition_observation_ticks,
            "transition_intersection_observations": (
                self._transition_intersection_observations
            ),
            "transition_tick_values": list(self._transition_ticks),
            "transition_time_fraction": transition_fraction,
            "queue_total_observed": _display_number(self._queue_observed_total),
            "queue_total_mean": queue_mean,
            "queue_total_peak": _display_number(self._queue_peak_total),
            "queue_total_latest": _display_number(self._queue_latest_total),
            "queue_approach_peak": _display_number(self._queue_peak_approach),
            "queue_axis_observed_totals": {
                axis: _display_number(value)
                for axis, value in self._queue_axis_observed_totals.items()
            },
            "queue_axis_peak_totals": {
                axis: _display_number(value)
                for axis, value in self._queue_axis_peak_totals.items()
            },
            "maximum_oldest_wait": _display_number(self._maximum_oldest_wait),
            "link_observations": self._link_observations,
            "link_saturation_observations": self._link_saturation_observations,
            "link_saturation_ticks": self._link_saturation_ticks,
            "link_saturation_tick_values": list(
                self._link_saturation_tick_values
            ),
            "peak_link_saturation_ratio": self._peak_link_saturation_ratio,
            "blocked_green_proxy_ticks": self._blocked_green_proxy_ticks,
            "blocked_green_proxy_approach_observations": (
                self._blocked_green_approach_observations
            ),
            "blocked_green_proxy_tick_values": list(
                self._blocked_green_tick_values
            ),
            "axis_pressure": {
                "available": pressure_count > 0,
                "observations": pressure_count,
                "latest": (
                    dict(self._axis_pressure_latest)
                    if self._axis_pressure_latest is not None
                    else None
                ),
                "mean": (
                    {
                        axis: total / pressure_count
                        for axis, total in self._axis_pressure_sums.items()
                    }
                    if pressure_count
                    else None
                ),
                "peak": dict(self._axis_pressure_peaks),
            },
            "final_30_tick_proxies": {
                "ticks_observed": final_ticks,
                "queue_total_observed": _display_number(
                    self._final_queue_observed_total
                ),
                "queue_total_mean": (
                    self._final_queue_observed_total / final_ticks
                    if final_ticks
                    else 0.0
                ),
                "queue_total_peak": _display_number(
                    self._final_queue_peak_total
                ),
                "terminal_queue_observed": _display_number(
                    self._final_terminal_queue_observed_total
                ),
                "terminal_queue_mean": (
                    self._final_terminal_queue_observed_total / final_ticks
                    if final_ticks
                    else 0.0
                ),
                "terminal_queue_peak": _display_number(
                    self._final_terminal_queue_peak
                ),
                "actual_green_terminal_queue": _display_number(
                    self._final_actual_green_terminal_queue
                ),
                "requested_green_terminal_queue": _display_number(
                    self._final_requested_green_terminal_queue
                ),
                "actual_terminal_opportunity_approaches": (
                    self._final_actual_terminal_opportunity_approaches
                ),
                "requested_terminal_opportunity_approaches": (
                    self._final_requested_terminal_opportunity_approaches
                ),
                "completed_delta_observed": self._final_completed_delta_observed,
            },
        }
        # This assertion catches accidental non-stdlib/non-JSON diagnostics during
        # development and returns a defensive copy to callers.
        return json.loads(json.dumps(result, allow_nan=False))


def _smoke_check() -> None:
    """Exercise copying, transitions, JSON output, and scenario reset."""
    def mutating_controller(state: dict[str, Any]) -> dict[str, str]:
        state["intersections"]["A1"]["queues"]["N"] = 999
        mutating_controller.last_axis_pressure = {"NS": 4.0, "EW": 2.0}
        return {"A1": "EW_GREEN"}

    base_state: dict[str, Any] = {
        "tick": 6,
        "remaining_ticks": 20,
        "map": {"rows": 1, "cols": 1},
        "intersections": {
            "A1": {
                "phase": "NS_GREEN",
                "phase_age": 6,
                "can_switch": True,
                "queues": {"N": 2, "S": 0, "E": 1, "W": 0},
                "oldest_wait": {"N": 7, "S": 0, "E": 3, "W": 0},
            }
        },
        "links": {},
        "vehicles": {"completed": 3},
    }
    observed = ObservedController(mutating_controller)
    decision = observed(base_state)
    assert decision == {"A1": "EW_GREEN"}
    assert base_state["intersections"]["A1"]["queues"]["N"] == 2
    report = observed.diagnostics()
    assert report["inferred_switch_starts"] == 1
    assert report["maximum_oldest_wait"] == 7
    assert report["axis_pressure"]["available"] is True
    json.dumps(report, allow_nan=False)

    reset_state = copy.deepcopy(base_state)
    reset_state["tick"] = 0
    observed(reset_state)
    assert observed.diagnostics()["ticks_observed"] == 1


if __name__ == "__main__":
    _smoke_check()
