from __future__ import annotations

import copy
import math
import random
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .scenarios import DemandWindow, Scenario

GreenPhase = Literal["NS_GREEN", "EW_GREEN"]
SignalPhase = Literal["NS_GREEN", "EW_GREEN", "NS_YELLOW", "EW_YELLOW", "ALL_RED"]
Direction = Literal["N", "S", "E", "W"]
Controller = Callable[[dict[str, Any]], dict[str, GreenPhase]]


@dataclass(slots=True)
class Signal:
    phase: SignalPhase = "NS_GREEN"
    phase_age: int = 0
    requested: GreenPhase = "NS_GREEN"
    next_phase: GreenPhase = "EW_GREEN"


@dataclass(slots=True)
class Vehicle:
    id: int
    direction: Direction
    route: tuple[str, ...]
    spawn_tick: int
    route_index: int = 0
    wait_ticks: int = 0
    travel_remaining: int = 0
    travel_total: int = 0
    from_intersection: str | None = None
    to_intersection: str | None = None
    finished_tick: int | None = None


@dataclass(frozen=True, slots=True)
class SimulationMetrics:
    spawned: int
    completed: int
    unfinished: int
    wait_ticks: int
    cost: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    scenario_id: str
    metrics: SimulationMetrics
    replay: dict[str, Any] | None


@dataclass(slots=True)
class _World:
    scenario: Scenario
    rng: random.Random
    signals: dict[str, Signal]
    queues: dict[str, dict[Direction, deque[int]]]
    vehicles: dict[int, Vehicle] = field(default_factory=dict)
    travelling: set[int] = field(default_factory=set)
    completed: set[int] = field(default_factory=set)
    next_vehicle_id: int = 1


MIN_GREEN = 5
YELLOW_TICKS = 2
ALL_RED_TICKS = 1
WORLD_WIDTH = 1200
WORLD_HEIGHT = 700
LANE_OFFSET = 14
STOP_DISTANCE = 55
CAR_SPACING = 29
ENTRY_TICKS = 5
EXIT_TICKS = 6
OFFSCREEN_X = 0.02
OFFSCREEN_Y = 0.035

PUBLIC_LAYOUTS: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {
    "balanced-grid": (
        ((0.27, 0.27), (0.72, 0.24)),
        ((0.25, 0.74), (0.75, 0.70)),
    ),
    "northbound-morning": (
        ((0.31, 0.17), (0.58, 0.14)),
        ((0.36, 0.48), (0.64, 0.50)),
        ((0.31, 0.82), (0.70, 0.84)),
    ),
    "city-rush": (
        ((0.17, 0.20), (0.49, 0.14), (0.82, 0.23)),
        ((0.13, 0.51), (0.52, 0.47), (0.87, 0.54)),
        ((0.22, 0.82), (0.57, 0.86), (0.80, 0.79)),
    ),
}


def intersection_id(row: int, col: int) -> str:
    return f"{chr(65 + row)}{col + 1}"


def _make_world(scenario: Scenario) -> _World:
    ids = [intersection_id(row, col) for row in range(scenario.rows) for col in range(scenario.cols)]
    return _World(
        scenario=scenario,
        rng=random.Random(scenario.seed),
        signals={item: Signal() for item in ids},
        queues={item: {direction: deque() for direction in ("N", "S", "E", "W")} for item in ids},
    )


def _axis(direction: Direction) -> str:
    return "NS" if direction in ("N", "S") else "EW"


def _route_for(world: _World, direction: Direction, lane: int) -> tuple[str, ...]:
    scenario = world.scenario
    if direction == "E":
        return tuple(intersection_id(lane, col) for col in range(scenario.cols))
    if direction == "W":
        return tuple(intersection_id(lane, col) for col in reversed(range(scenario.cols)))
    if direction == "S":
        return tuple(intersection_id(row, lane) for row in range(scenario.rows))
    return tuple(intersection_id(row, lane) for row in reversed(range(scenario.rows)))


def _legacy_spawn_rate(scenario: Scenario, tick: int, axis: str) -> float:
    base = scenario.vertical_rate if axis == "NS" else scenario.horizontal_rate
    if scenario.rush_axis == axis:
        base *= 1.2 if tick < scenario.ticks * 0.6 else 0.75
    if scenario.burst_period and (tick // scenario.burst_period) % 2 == 0:
        base *= 1.45
    return min(base, 0.48)


def _demand_window(scenario: Scenario, tick: int) -> DemandWindow:
    for window in scenario.demand_windows:
        if window.start_tick <= tick < window.end_tick:
            return window
    raise RuntimeError(f"no demand window covers tick {tick}")


def _spawn_rate(scenario: Scenario, tick: int, direction: Direction, lane: int) -> float:
    if not scenario.demand_windows:
        return _legacy_spawn_rate(scenario, tick, _axis(direction))

    window = _demand_window(scenario, tick)
    direction_rate = {
        "N": window.north_rate,
        "S": window.south_rate,
        "E": window.east_rate,
        "W": window.west_rate,
    }[direction]
    weights = window.row_weights if direction in ("E", "W") else window.col_weights
    lane_weight = weights[lane] if weights else 1.0
    return min(direction_rate * lane_weight, 0.48)


def _spawn(world: _World, tick: int) -> None:
    scenario = world.scenario
    candidates: list[tuple[Direction, int]] = []
    candidates.extend((direction, row) for row in range(scenario.rows) for direction in ("E", "W"))
    candidates.extend((direction, col) for col in range(scenario.cols) for direction in ("N", "S"))
    for direction, lane in candidates:
        if world.rng.random() >= _spawn_rate(scenario, tick, direction, lane):
            continue
        route = _route_for(world, direction, lane)
        vehicle = Vehicle(world.next_vehicle_id, direction, route, tick)
        world.vehicles[vehicle.id] = vehicle
        world.queues[route[0]][direction].append(vehicle.id)
        world.next_vehicle_id += 1


def _queue_metrics(world: _World, intersection: str) -> tuple[dict[str, int], dict[str, int]]:
    sizes: dict[str, int] = {}
    oldest: dict[str, int] = {}
    for direction, queue in world.queues[intersection].items():
        sizes[direction] = len(queue)
        oldest[direction] = max((world.vehicles[vehicle_id].wait_ticks for vehicle_id in queue), default=0)
    return sizes, oldest


def _controller_state(world: _World, tick: int) -> dict[str, Any]:
    intersections: dict[str, Any] = {}
    for item, signal in world.signals.items():
        queues, oldest = _queue_metrics(world, item)
        controller_phase = "YELLOW" if signal.phase.endswith("_YELLOW") else signal.phase
        intersections[item] = {
            "phase": controller_phase,
            "phase_age": signal.phase_age,
            "can_switch": signal.phase in ("NS_GREEN", "EW_GREEN") and signal.phase_age >= MIN_GREEN,
            "queues": queues,
            "oldest_wait": oldest,
        }

    links: dict[str, Any] = {}
    occupancy: dict[tuple[str, str], int] = defaultdict(int)
    for vehicle_id in world.travelling:
        vehicle = world.vehicles[vehicle_id]
        if vehicle.from_intersection and vehicle.to_intersection:
            occupancy[(vehicle.from_intersection, vehicle.to_intersection)] += 1
    for (source, target), count in occupancy.items():
        links[f"{source}->{target}"] = {
            "from": source,
            "to": target,
            "vehicles": count,
            "capacity": world.scenario.link_capacity,
        }

    return {
        "tick": tick,
        "remaining_ticks": world.scenario.ticks - tick,
        "map": {"rows": world.scenario.rows, "cols": world.scenario.cols},
        "intersections": intersections,
        "links": links,
        "vehicles": {
            "spawned": len(world.vehicles),
            "active": len(world.vehicles) - len(world.completed),
            "completed": len(world.completed),
        },
    }


def _apply_requests(world: _World, decisions: dict[str, GreenPhase]) -> None:
    if not isinstance(decisions, dict):
        raise TypeError("control(state) must return a dict")
    unknown = set(decisions) - set(world.signals)
    if unknown:
        raise ValueError(f"unknown intersection: {sorted(unknown)[0]}")
    for item, requested in decisions.items():
        if requested not in ("NS_GREEN", "EW_GREEN"):
            raise ValueError(f"invalid phase for {item}: {requested!r}")
        world.signals[item].requested = requested


def _advance_signals(world: _World) -> None:
    for signal in world.signals.values():
        if signal.phase in ("NS_GREEN", "EW_GREEN"):
            if signal.requested != signal.phase and signal.phase_age >= MIN_GREEN:
                signal.next_phase = signal.requested
                signal.phase = "NS_YELLOW" if signal.phase == "NS_GREEN" else "EW_YELLOW"
                signal.phase_age = 0
            else:
                signal.phase_age += 1
        elif signal.phase in ("NS_YELLOW", "EW_YELLOW"):
            signal.phase_age += 1
            if signal.phase_age >= YELLOW_TICKS:
                signal.phase = "ALL_RED"
                signal.phase_age = 0
        else:
            signal.phase_age += 1
            if signal.phase_age >= ALL_RED_TICKS:
                signal.phase = signal.next_phase
                signal.phase_age = 0


def _link_occupancy(world: _World, source: str, target: str) -> int:
    return sum(
        1
        for vehicle_id in world.travelling
        if world.vehicles[vehicle_id].from_intersection == source
        and world.vehicles[vehicle_id].to_intersection == target
    )


def _approach_capacity(world: _World, source: str, target: str) -> int:
    source_x, source_y = _point(world, source)
    target_x, target_y = _point(world, target)
    length = math.hypot(
        (target_x - source_x) * WORLD_WIDTH,
        (target_y - source_y) * WORLD_HEIGHT,
    )
    physical_capacity = max(1, int((length - 2 * STOP_DISTANCE) / CAR_SPACING) + 1)
    return min(world.scenario.link_capacity, physical_capacity)


def _reserved_approach(world: _World, target: str, direction: Direction) -> int:
    return len(world.queues[target][direction]) + sum(
        1
        for vehicle_id in world.travelling
        if world.vehicles[vehicle_id].to_intersection == target
        and world.vehicles[vehicle_id].direction == direction
    )


def _release_queues(world: _World, tick: int) -> None:
    for item, by_direction in world.queues.items():
        phase = world.signals[item].phase
        for direction, queue in by_direction.items():
            if not queue or phase != f"{_axis(direction)}_GREEN":
                continue
            vehicle = world.vehicles[queue[0]]
            if tick - vehicle.spawn_tick < ENTRY_TICKS:
                continue
            if vehicle.route_index == len(vehicle.route) - 1:
                queue.popleft()
                vehicle.finished_tick = tick
                world.completed.add(vehicle.id)
                continue
            target = vehicle.route[vehicle.route_index + 1]
            if _link_occupancy(world, item, target) >= world.scenario.link_capacity:
                continue
            if _reserved_approach(world, target, direction) >= _approach_capacity(world, item, target):
                continue
            queue.popleft()
            vehicle.from_intersection = item
            vehicle.to_intersection = target
            vehicle.travel_total = world.scenario.travel_ticks
            vehicle.travel_remaining = world.scenario.travel_ticks
            world.travelling.add(vehicle.id)


def _advance_vehicles(world: _World) -> None:
    arrived: list[int] = []
    for vehicle_id in world.travelling:
        vehicle = world.vehicles[vehicle_id]
        vehicle.travel_remaining -= 1
        if vehicle.travel_remaining <= 0:
            arrived.append(vehicle_id)
    for vehicle_id in arrived:
        vehicle = world.vehicles[vehicle_id]
        world.travelling.remove(vehicle_id)
        vehicle.route_index += 1
        world.queues[vehicle.route[vehicle.route_index]][vehicle.direction].append(vehicle_id)
        vehicle.from_intersection = None
        vehicle.to_intersection = None


def _increment_wait(world: _World) -> None:
    for by_direction in world.queues.values():
        for queue in by_direction.values():
            for vehicle_id in queue:
                world.vehicles[vehicle_id].wait_ticks += 1


def _point(world: _World, intersection: str) -> tuple[float, float]:
    row = ord(intersection[0]) - 65
    col = int(intersection[1:]) - 1
    layout = PUBLIC_LAYOUTS.get(world.scenario.id)
    if layout and len(layout) == world.scenario.rows and len(layout[row]) == world.scenario.cols:
        return layout[row][col]
    width = max(world.scenario.cols - 1, 1)
    height = max(world.scenario.rows - 1, 1)
    x_margin = 0.26 if world.scenario.cols == 2 else 0.16
    y_margin = 0.24 if world.scenario.rows == 2 else 0.15
    return (
        x_margin + (1 - 2 * x_margin) * col / width,
        y_margin + (1 - 2 * y_margin) * row / height,
    )


def _extend_route(
    centers: list[tuple[float, float]], direction: Direction
) -> list[tuple[float, float]]:
    if len(centers) == 1:
        x, y = centers[0]
        if direction == "E":
            return [(-OFFSCREEN_X, y), (x, y), (1 + OFFSCREEN_X, y)]
        if direction == "W":
            return [(1 + OFFSCREEN_X, y), (x, y), (-OFFSCREEN_X, y)]
        if direction == "S":
            return [(x, -OFFSCREEN_Y), (x, y), (x, 1 + OFFSCREEN_Y)]
        return [(x, 1 + OFFSCREEN_Y), (x, y), (x, -OFFSCREEN_Y)]

    first, second = centers[0], centers[1]
    before_last, last = centers[-2], centers[-1]
    if direction in ("E", "W"):
        start_x = -OFFSCREEN_X if direction == "E" else 1 + OFFSCREEN_X
        end_x = 1 + OFFSCREEN_X if direction == "E" else -OFFSCREEN_X
        start_dx = first[0] - second[0]
        end_dx = last[0] - before_last[0]
        start_y = first[1] + (start_x - first[0]) * (first[1] - second[1]) / start_dx
        end_y = last[1] + (end_x - last[0]) * (last[1] - before_last[1]) / end_dx
        return [(start_x, start_y), *centers, (end_x, end_y)]

    start_y = -OFFSCREEN_Y if direction == "S" else 1 + OFFSCREEN_Y
    end_y = 1 + OFFSCREEN_Y if direction == "S" else -OFFSCREEN_Y
    start_dy = first[1] - second[1]
    end_dy = last[1] - before_last[1]
    start_x = first[0] + (start_y - first[1]) * (first[0] - second[0]) / start_dy
    end_x = last[0] + (end_y - last[1]) * (last[0] - before_last[0]) / end_dy
    return [(start_x, start_y), *centers, (end_x, end_y)]


def _route_points(world: _World, direction: Direction, route: tuple[str, ...]) -> list[tuple[float, float]]:
    return _extend_route([_point(world, intersection) for intersection in route], direction)


def _path_distances(points: list[tuple[float, float]]) -> list[float]:
    distances = [0.0]
    for start, end in zip(points, points[1:]):
        length = math.hypot(
            (end[0] - start[0]) * WORLD_WIDTH,
            (end[1] - start[1]) * WORLD_HEIGHT,
        )
        distances.append(distances[-1] + length)
    return distances


def _point_along(
    points: list[tuple[float, float]], distances: list[float], distance: float
) -> tuple[float, float, float]:
    segment = 0
    if distance >= distances[-1]:
        segment = len(points) - 2
    elif distance > 0:
        segment = next(
            index
            for index in range(len(distances) - 1)
            if distances[index] <= distance <= distances[index + 1]
        )
    start = points[segment]
    end = points[segment + 1]
    segment_length = distances[segment + 1] - distances[segment]
    amount = (distance - distances[segment]) / max(segment_length, 1)
    start_x = start[0] * WORLD_WIDTH
    start_y = start[1] * WORLD_HEIGHT
    dx = (end[0] - start[0]) * WORLD_WIDTH / max(segment_length, 1)
    dy = (end[1] - start[1]) * WORLD_HEIGHT / max(segment_length, 1)
    x = start_x + (end[0] * WORLD_WIDTH - start_x) * amount - dy * LANE_OFFSET
    y = start_y + (end[1] * WORLD_HEIGHT - start_y) * amount + dx * LANE_OFFSET
    heading = math.degrees(math.atan2(dy, dx)) % 360
    return (x / WORLD_WIDTH, y / WORLD_HEIGHT, heading)


def _in_flight_rank(world: _World, vehicle: Vehicle) -> int:
    if vehicle.to_intersection is None:
        return 0
    return sum(
        1
        for other_id in world.travelling
        if other_id != vehicle.id
        and world.vehicles[other_id].to_intersection == vehicle.to_intersection
        and world.vehicles[other_id].direction == vehicle.direction
        and world.vehicles[other_id].travel_remaining < vehicle.travel_remaining
    )


def _vehicle_positions(world: _World, tick: int) -> list[list[Any]]:
    positions: list[list[Any]] = []
    queued_slots: dict[int, int] = {}
    for by_direction in world.queues.values():
        for queue in by_direction.values():
            queued_slots.update({vehicle_id: index for index, vehicle_id in enumerate(queue)})

    for vehicle in world.vehicles.values():
        points = _route_points(world, vehicle.direction, vehicle.route)
        distances = _path_distances(points)
        intersection_distances = distances[1:-1]
        if vehicle.finished_tick is not None:
            exit_age = tick - vehicle.finished_tick
            if exit_age > EXIT_TICKS:
                continue
            progress = (exit_age + 1) / (EXIT_TICKS + 1)
            start = intersection_distances[-1] - STOP_DISTANCE
            distance = start + (distances[-1] - start) * progress
        elif vehicle.id in world.travelling and vehicle.from_intersection and vehicle.to_intersection:
            target_queue = world.queues[vehicle.to_intersection][vehicle.direction]
            target_rank = len(target_queue) + _in_flight_rank(world, vehicle)
            start = intersection_distances[vehicle.route_index] - STOP_DISTANCE
            end = (
                intersection_distances[vehicle.route_index + 1]
                - STOP_DISTANCE
                - target_rank * CAR_SPACING
            )
            progress = min(
                1,
                (vehicle.travel_total - vehicle.travel_remaining + 1)
                / max(vehicle.travel_total, 1),
            )
            distance = start + (end - start) * progress
        else:
            rank = queued_slots.get(vehicle.id, 0)
            distance = (
                intersection_distances[vehicle.route_index]
                - STOP_DISTANCE
                - rank * CAR_SPACING
            )
        entry_age = tick - vehicle.spawn_tick
        if entry_age < ENTRY_TICKS:
            first_stop = intersection_distances[0] - STOP_DISTANCE
            entry_start = distance - first_stop
            progress = entry_age / ENTRY_TICKS
            distance = entry_start + (distance - entry_start) * progress
        x, y, heading = _point_along(points, distances, distance)
        positions.append([vehicle.id, round(x, 4), round(y, 4), round(heading, 2)])
    return positions


def _map_payload(world: _World) -> dict[str, Any]:
    intersections = []
    roads = []
    for row in range(world.scenario.rows):
        for col in range(world.scenario.cols):
            item = intersection_id(row, col)
            x, y = _point(world, item)
            intersections.append({"id": item, "x": x, "y": y})
    for row in range(world.scenario.rows):
        route = tuple(intersection_id(row, col) for col in range(world.scenario.cols))
        points = _route_points(world, "E", route)
        roads.append(
            {
                "from": f"west-{row}",
                "to": f"east-{row}",
                "x1": points[0][0],
                "y1": points[0][1],
                "x2": points[-1][0],
                "y2": points[-1][1],
                "points": [{"x": round(x, 4), "y": round(y, 4)} for x, y in points],
            }
        )
    for col in range(world.scenario.cols):
        route = tuple(intersection_id(row, col) for row in range(world.scenario.rows))
        points = _route_points(world, "S", route)
        roads.append(
            {
                "from": f"north-{col}",
                "to": f"south-{col}",
                "x1": points[0][0],
                "y1": points[0][1],
                "x2": points[-1][0],
                "y2": points[-1][1],
                "points": [{"x": round(x, 4), "y": round(y, 4)} for x, y in points],
            }
        )
    return {"rows": world.scenario.rows, "cols": world.scenario.cols, "intersections": intersections, "roads": roads}


def _frame(world: _World, tick: int) -> dict[str, Any]:
    return {
        "tick": tick,
        "vehicles": _vehicle_positions(world, tick),
        "signals": {item: signal.phase for item, signal in world.signals.items()},
        "completed": len(world.completed),
        "waiting": sum(vehicle.wait_ticks for vehicle in world.vehicles.values()),
    }


def run_scenario(scenario: Scenario, controller: Controller, *, record_replay: bool = True) -> SimulationResult:
    world = _make_world(scenario)
    frames: list[dict[str, Any]] = []

    for tick in range(scenario.ticks):
        _spawn(world, tick)
        decisions = controller(copy.deepcopy(_controller_state(world, tick)))
        _apply_requests(world, decisions)
        _advance_signals(world)
        _advance_vehicles(world)
        _release_queues(world, tick)
        _increment_wait(world)
        if record_replay:
            frames.append(_frame(world, tick))

    unfinished = len(world.vehicles) - len(world.completed)
    wait_ticks = sum(vehicle.wait_ticks for vehicle in world.vehicles.values())
    metrics = SimulationMetrics(
        spawned=len(world.vehicles),
        completed=len(world.completed),
        unfinished=unfinished,
        wait_ticks=wait_ticks,
        cost=wait_ticks + unfinished * 300,
    )
    replay = None
    if record_replay:
        replay = {
            "version": 2,
            "scenario": {"id": scenario.id, "name": scenario.name, "ticks": scenario.ticks},
            "map": _map_payload(world),
            "frames": frames,
            "metrics": {
                "spawned": metrics.spawned,
                "completed": metrics.completed,
                "unfinished": metrics.unfinished,
                "waitTicks": metrics.wait_ticks,
                "cost": metrics.cost,
            },
        }
    return SimulationResult(scenario.id, metrics, replay)


def fixed_time_controller(state: dict[str, Any]) -> dict[str, GreenPhase]:
    phase: GreenPhase = "NS_GREEN" if state["tick"] % 30 < 15 else "EW_GREEN"
    return {item: phase for item in state["intersections"]}
