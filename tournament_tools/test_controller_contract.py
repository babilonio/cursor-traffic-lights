"""Observable contract tests for the competition controller.

These tests deliberately treat ``controller.py`` as a black box.  They use
fresh module instances where a clean controller is needed so module-level
runtime state is covered as well as purely functional implementations.
"""

from __future__ import annotations

import copy
import importlib.util
import itertools
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ARENA_ROOT = REPOSITORY_ROOT / "traffic-lights-arena"
CONTROLLER_PATH = ARENA_ROOT / "controller.py"
if str(ARENA_ROOT) not in sys.path:
    sys.path.insert(0, str(ARENA_ROOT))

from traffic_arena.engine import run_scenario  # noqa: E402
from traffic_arena.scenarios import Scenario  # noqa: E402


Control = Callable[[dict[str, Any]], dict[str, str]]
VALID_REQUESTS = {"NS_GREEN", "EW_GREEN"}
_MODULE_IDS = itertools.count()


def _load_controller_module() -> ModuleType:
    """Load an isolated copy so one test's controller memory cannot hide bugs."""
    module_name = f"_contract_controller_{next(_MODULE_IDS)}"
    spec = importlib.util.spec_from_file_location(module_name, CONTROLLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_control() -> Control:
    module = _load_controller_module()
    control = getattr(module, "control", None)
    assert callable(control), f"{CONTROLLER_PATH} must define callable control(state)"
    return control


def _intersection_ids(rows: int, cols: int) -> list[str]:
    return [f"{chr(65 + row)}{col + 1}" for row in range(rows) for col in range(cols)]


def _state(
    *,
    tick: int,
    rows: int = 2,
    cols: int = 2,
    phase: str = "NS_GREEN",
    phase_age: int = 8,
    can_switch: bool = True,
    remaining_ticks: int = 100,
    ns_queue: int = 2,
    ew_queue: int = 2,
) -> dict[str, Any]:
    intersections = {
        item: {
            "phase": phase,
            "phase_age": phase_age,
            "can_switch": can_switch,
            "queues": {"N": ns_queue, "S": ns_queue, "E": ew_queue, "W": ew_queue},
            "oldest_wait": {
                "N": ns_queue * 3,
                "S": ns_queue * 3,
                "E": ew_queue * 3,
                "W": ew_queue * 3,
            },
        }
        for item in _intersection_ids(rows, cols)
    }
    return {
        "tick": tick,
        "remaining_ticks": remaining_ticks,
        "map": {"rows": rows, "cols": cols},
        "intersections": intersections,
        "links": {},
        "vehicles": {
            "spawned": 4 * rows * cols,
            "active": 4 * rows * cols,
            "completed": 0,
        },
    }


def _assert_valid_complete(state: dict[str, Any], decisions: object) -> None:
    assert isinstance(decisions, dict), "control(state) must return a dict"
    assert set(decisions) == set(state["intersections"]), (
        "controller decisions must contain every intersection exactly once"
    )
    assert set(decisions.values()) <= VALID_REQUESTS, (
        "every requested phase must be NS_GREEN or EW_GREEN"
    )


def _checked(control: Control) -> Control:
    def checked(state: dict[str, Any]) -> dict[str, str]:
        before = copy.deepcopy(state)
        decisions = control(state)
        assert state == before, "control(state) must not mutate simulator state"
        _assert_valid_complete(state, decisions)
        return decisions

    return checked


def _scenario(
    scenario_id: str,
    *,
    rows: int = 2,
    cols: int = 2,
    seed: int = 17,
    ticks: int = 45,
    horizontal_rate: float = 0.18,
    vertical_rate: float = 0.18,
) -> Scenario:
    return Scenario(
        scenario_id,
        scenario_id.replace("-", " ").title(),
        rows,
        cols,
        seed,
        ticks=ticks,
        horizontal_rate=horizontal_rate,
        vertical_rate=vertical_rate,
    )


def test_run_scenario_gets_complete_valid_outputs_without_state_mutation() -> None:
    calls = 0
    checked_control = _checked(_fresh_control())

    def recording_control(state: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return checked_control(state)

    scenario = _scenario("contract-run", rows=2, cols=3, ticks=50)
    result = run_scenario(scenario, recording_control, record_replay=False)

    assert calls == scenario.ticks
    assert result.scenario_id == scenario.id


@pytest.mark.parametrize("phase", ["NS_GREEN", "EW_GREEN", "YELLOW", "ALL_RED"])
def test_each_observed_signal_phase_is_handled_safely(phase: str) -> None:
    state = _state(tick=11, phase=phase, can_switch=phase in VALID_REQUESTS)
    before = copy.deepcopy(state)

    decisions = _fresh_control()(state)

    assert state == before
    _assert_valid_complete(state, decisions)


def test_repeated_runs_are_deterministic() -> None:
    scenario = _scenario("deterministic-repeat", seed=919, ticks=65)
    control = _checked(_fresh_control())

    first = run_scenario(scenario, control, record_replay=True)
    second = run_scenario(scenario, control, record_replay=True)

    assert second == first, "repeating the same scenario and history must give the same result"


@pytest.mark.parametrize("reset_tick", [0, 3], ids=["tick-zero", "backwards-tick"])
def test_new_or_backwards_tick_resets_runtime_memory(reset_tick: int) -> None:
    reset_state = _state(
        tick=reset_tick,
        rows=2,
        cols=3,
        phase="EW_GREEN",
        ns_queue=19,
        ew_queue=1,
    )
    expected = _fresh_control()(copy.deepcopy(reset_state))

    control = _fresh_control()
    for tick in range(9):
        history_state = _state(
            tick=tick,
            rows=3,
            cols=2,
            phase="NS_GREEN",
            ns_queue=1,
            ew_queue=20 + tick,
        )
        _assert_valid_complete(history_state, control(history_state))

    actual = control(copy.deepcopy(reset_state))

    _assert_valid_complete(reset_state, actual)
    assert actual == expected, "controller memory must reset when a scenario restarts"


def test_sequential_scenarios_support_changed_grid_dimensions() -> None:
    first = _scenario("dimensions-first", rows=2, cols=2, seed=41, ticks=35)
    second = _scenario("dimensions-second", rows=1, cols=3, seed=42, ticks=40)
    reused_control = _checked(_fresh_control())

    run_scenario(first, reused_control, record_replay=False)
    reused_result = run_scenario(second, reused_control, record_replay=True)
    clean_result = run_scenario(second, _checked(_fresh_control()), record_replay=True)

    assert reused_result == clean_result


def test_sequential_scenarios_do_not_leak_observations() -> None:
    polluting = _scenario(
        "state-pollution",
        seed=73,
        ticks=55,
        horizontal_rate=0.45,
        vertical_rate=0.01,
    )
    measured = _scenario(
        "state-leak-check",
        seed=74,
        ticks=55,
        horizontal_rate=0.02,
        vertical_rate=0.35,
    )
    reused_control = _checked(_fresh_control())

    run_scenario(polluting, reused_control, record_replay=False)
    after_other_scenario = run_scenario(measured, reused_control, record_replay=True)
    in_isolation = run_scenario(measured, _checked(_fresh_control()), record_replay=True)

    assert after_other_scenario == in_isolation, (
        "a tick-0 scenario must behave the same regardless of earlier observations"
    )


def test_selected_transition_target_is_preserved_through_yellow_and_all_red() -> None:
    # Delaying can_switch makes these coherent boundary cases: a target may be
    # selected immediately before a controller's ordinary schedule boundary.
    cases = (
        ("NS_GREEN", 29, 1, 25),
        ("EW_GREEN", 14, 25, 1),
    )
    exercised_targets = 0

    for current_phase, trigger_tick, ns_queue, ew_queue in cases:
        control = _checked(_fresh_control())
        target = current_phase
        for tick in range(trigger_tick + 1):
            state = _state(
                tick=tick,
                rows=1,
                cols=1,
                phase=current_phase,
                phase_age=max(5, tick),
                can_switch=tick == trigger_tick,
                remaining_ticks=100 - tick,
                ns_queue=ns_queue,
                ew_queue=ew_queue,
            )
            target = control(state)["A1"]

        if target == current_phase:
            continue

        exercised_targets += 1
        transition_observations = (
            _state(
                tick=trigger_tick + 1,
                rows=1,
                cols=1,
                phase="YELLOW",
                phase_age=0,
                can_switch=False,
                remaining_ticks=99 - trigger_tick,
                ns_queue=ns_queue,
                ew_queue=ew_queue,
            ),
            _state(
                tick=trigger_tick + 2,
                rows=1,
                cols=1,
                phase="YELLOW",
                phase_age=1,
                can_switch=False,
                remaining_ticks=98 - trigger_tick,
                ns_queue=ns_queue,
                ew_queue=ew_queue,
            ),
            _state(
                tick=trigger_tick + 3,
                rows=1,
                cols=1,
                phase="ALL_RED",
                phase_age=0,
                can_switch=False,
                remaining_ticks=97 - trigger_tick,
                ns_queue=ns_queue,
                ew_queue=ew_queue,
            ),
        )
        for observation in transition_observations:
            assert control(observation)["A1"] == target, (
                "once a transition starts, its target cannot be cancelled"
            )

    if not exercised_targets:
        pytest.skip("controller did not initiate a transition for the focused fixtures")


def test_controller_does_not_start_an_obviously_unpayable_switch() -> None:
    control = _checked(_fresh_control())

    # Keep a productive NS green until only three ticks remain.  Switching now
    # would spend every remaining tick in yellow/all-red and serve no vehicle.
    for tick in range(15):
        control(
            _state(
                tick=tick,
                rows=1,
                cols=1,
                phase="NS_GREEN",
                phase_age=max(5, tick),
                can_switch=False,
                remaining_ticks=18 - tick,
                ns_queue=4,
                ew_queue=25,
            )
        )

    final_state = _state(
        tick=15,
        rows=1,
        cols=1,
        phase="NS_GREEN",
        phase_age=15,
        can_switch=True,
        remaining_ticks=3,
        ns_queue=4,
        ew_queue=25,
    )
    decisions = control(final_state)

    assert decisions == {"A1": "NS_GREEN"}, (
        "a productive green must be held when no useful target green can be reached"
    )
