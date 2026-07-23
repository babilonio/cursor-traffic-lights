"""Reproducible, replay-free benchmarks for Traffic Lights Arena controllers.

The module is intentionally independent of the local viewer: it never starts a
server or writes replay data.  It can be imported as an API or executed
directly as a JSONL-producing command-line program.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Protocol, TextIO, cast


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ARENA_ROOT = REPOSITORY_ROOT / "traffic-lights-arena"

# Support both ``python -m tournament_tools.benchmark`` and direct execution.
for _import_root in (REPOSITORY_ROOT, ARENA_ROOT):
    _import_root_text = str(_import_root)
    if _import_root_text not in sys.path:
        sys.path.insert(0, _import_root_text)

try:
    from traffic_arena.engine import Controller, fixed_time_controller, run_scenario
    from traffic_arena.score_profiles import score_profile
    from traffic_arena.scenarios import PUBLIC_SCENARIOS, Scenario
    from traffic_arena.scoring import scenario_score
except ImportError as exc:  # pragma: no cover - depends on invocation environment
    raise ImportError(
        f"Could not import the simulator from {ARENA_ROOT}. "
        "Run this benchmark from a complete traffic-lights repository."
    ) from exc


BenchmarkMode = Literal["smoke", "public", "synthetic", "held-out", "all"]


class ScenarioCase(Protocol):
    """Structural type shared with ``tournament_tools.scenario_catalog``."""

    scenario: Scenario
    family: str
    variant: str


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    """One JSONL-compatible candidate/scenario result."""

    candidate_id: str
    controller_path: str
    scenario_id: str
    family: str
    variant: str
    seed: int
    spawned: int
    completed: int
    unfinished: int
    wait_ticks: int
    cost: int
    baseline_cost: int
    elapsed_seconds: float
    diagnostics: dict[str, Any] | None = None
    public_score: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping, omitting absent optional fields."""

        record = {
            "candidate_id": self.candidate_id,
            "controller_path": self.controller_path,
            "scenario_id": self.scenario_id,
            "family": self.family,
            "variant": self.variant,
            "seed": self.seed,
            "spawned": self.spawned,
            "completed": self.completed,
            "unfinished": self.unfinished,
            "wait_ticks": self.wait_ticks,
            "cost": self.cost,
            "baseline_cost": self.baseline_cost,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.diagnostics is not None:
            record["diagnostics"] = self.diagnostics
        if self.public_score is not None:
            record["public_score"] = self.public_score
        return record

    def to_json(self) -> str:
        """Serialize this record as one strict, compact JSONL line."""

        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )


@dataclass(frozen=True, slots=True)
class _PublicScenarioCase:
    scenario: Scenario
    family: str = "public"
    variant: str = "official"


def _resolved_controller_path(controller_path: str | Path) -> Path:
    path = Path(controller_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Controller file does not exist: {path}")
    if path.suffix.lower() != ".py":
        raise ValueError(f"Controller path must name a Python file: {path}")
    return path


def default_candidate_id(controller_path: str | Path) -> str:
    """Build a stable candidate identifier from the controller file contents."""

    path = _resolved_controller_path(controller_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{path.stem}-{digest}"


@contextmanager
def _fresh_controller(controller_path: Path) -> Iterator[Controller]:
    """Load a controller under a unique module name for one scenario only."""

    module_name = f"_tournament_controller_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, controller_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create an import spec for controller: {controller_path}")

    module = importlib.util.module_from_spec(spec)
    controller_directory = str(controller_path.parent)
    inserted_path = controller_directory not in sys.path
    if inserted_path:
        sys.path.insert(0, controller_directory)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        control = getattr(module, "control", None)
        if not callable(control):
            raise TypeError(f"{controller_path} must define a callable control(state)")
        yield cast(Controller, control)
    except Exception as exc:
        if isinstance(exc, (ImportError, TypeError)):
            raise
        raise RuntimeError(f"Failed while loading or running controller {controller_path}: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)
        if inserted_path:
            try:
                sys.path.remove(controller_directory)
            except ValueError:
                pass


def load_controller(controller_path: str | Path) -> Controller:
    """Dynamically load ``control`` once.

    Benchmark runs use :func:`run_benchmark_case`, which keeps the fresh module
    alive for the entire scenario and then unloads it.
    """

    path = _resolved_controller_path(controller_path)
    with _fresh_controller(path) as controller:
        return controller


def _load_observer_module() -> ModuleType:
    try:
        module = importlib.import_module("tournament_tools.observer")
    except ModuleNotFoundError as exc:
        if exc.name == "tournament_tools.observer":
            raise ImportError(
                "Observer instrumentation was requested, but "
                "tournament_tools.observer is not available."
            ) from exc
        raise ImportError(
            f"Could not import a dependency of tournament_tools.observer: {exc.name}"
        ) from exc

    # Reloading prevents observer module globals from leaking across scenarios.
    return importlib.reload(module)


def _observed_controller(controller: Controller) -> tuple[Controller, Any]:
    module = _load_observer_module()
    observer_type = getattr(module, "ObservedController", None)
    if not callable(observer_type):
        raise TypeError(
            "tournament_tools.observer must expose callable ObservedController"
        )
    observer = observer_type(controller)
    if callable(observer):
        return cast(Controller, observer), observer
    observed_control = getattr(observer, "control", None)
    if callable(observed_control):
        return cast(Controller, observed_control), observer
    raise TypeError(
        "ObservedController(controller) must be callable or expose control(state)"
    )


def _json_compatible(value: Any, *, context: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_compatible(asdict(value), context=context)
    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible(item, context=context)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item, context=context) for item in value]
    if isinstance(value, set):
        return [
            _json_compatible(item, context=context)
            for item in sorted(value, key=repr)
        ]
    raise TypeError(f"{context} contains a non-JSON value: {type(value).__name__}")


def _observer_diagnostics(observer: Any) -> dict[str, Any]:
    diagnostics: Any = None
    for attribute in ("diagnostics", "get_diagnostics", "summary"):
        if not hasattr(observer, attribute):
            continue
        diagnostics = getattr(observer, attribute)
        if callable(diagnostics):
            diagnostics = diagnostics()
        break
    if diagnostics is None:
        return {}
    normalized = _json_compatible(diagnostics, context="Observer diagnostics")
    if not isinstance(normalized, dict):
        raise TypeError("Observer diagnostics must be a mapping or dataclass")
    return normalized


def _validate_case(case: ScenarioCase) -> tuple[Scenario, str, str]:
    scenario = getattr(case, "scenario", None)
    family = getattr(case, "family", None)
    variant = getattr(case, "variant", None)
    if scenario is None:
        raise TypeError("ScenarioCase must expose a scenario attribute")
    if not isinstance(family, str) or not family:
        raise TypeError("ScenarioCase.family must be a non-empty string")
    if not isinstance(variant, str) or not variant:
        raise TypeError("ScenarioCase.variant must be a non-empty string")
    for attribute in ("id", "seed"):
        if not hasattr(scenario, attribute):
            raise TypeError(f"ScenarioCase.scenario must expose {attribute}")
    return cast(Scenario, scenario), family, variant


def run_benchmark_case(
    controller_path: str | Path,
    case: ScenarioCase,
    *,
    candidate_id: str | None = None,
    observe: bool = False,
) -> BenchmarkRecord:
    """Run a fresh candidate and fixed-time baseline on one scenario."""

    path = _resolved_controller_path(controller_path)
    scenario, family, variant = _validate_case(case)
    resolved_candidate_id = candidate_id or default_candidate_id(path)

    baseline = run_scenario(
        scenario,
        fixed_time_controller,
        record_replay=False,
    )

    observer: Any = None
    started = time.perf_counter()
    with _fresh_controller(path) as controller:
        benchmark_controller = controller
        if observe:
            benchmark_controller, observer = _observed_controller(controller)
        result = run_scenario(
            scenario,
            benchmark_controller,
            record_replay=False,
        )
    elapsed_seconds = time.perf_counter() - started

    diagnostics = _observer_diagnostics(observer) if observer is not None else None
    public_score: int | None = None
    if scenario.id in {public.id for public in PUBLIC_SCENARIOS}:
        profile = score_profile(scenario.id)
        public_score = scenario_score(
            result.metrics.cost,
            baseline.metrics.cost,
            profile.target_cost,
        )

    metrics = result.metrics
    return BenchmarkRecord(
        candidate_id=resolved_candidate_id,
        controller_path=str(path),
        scenario_id=scenario.id,
        family=family,
        variant=variant,
        seed=scenario.seed,
        spawned=metrics.spawned,
        completed=metrics.completed,
        unfinished=metrics.unfinished,
        wait_ticks=metrics.wait_ticks,
        cost=metrics.cost,
        baseline_cost=baseline.metrics.cost,
        elapsed_seconds=elapsed_seconds,
        diagnostics=diagnostics,
        public_score=public_score,
    )


def run_benchmark(
    controller_path: str | Path,
    cases: Iterable[ScenarioCase],
    *,
    candidate_id: str | None = None,
    observe: bool = False,
) -> Iterator[BenchmarkRecord]:
    """Yield one record per case while reloading the controller each time."""

    path = _resolved_controller_path(controller_path)
    resolved_candidate_id = candidate_id or default_candidate_id(path)
    for case in cases:
        yield run_benchmark_case(
            path,
            case,
            candidate_id=resolved_candidate_id,
            observe=observe,
        )


def _scenario_catalog() -> ModuleType:
    try:
        module = importlib.import_module("tournament_tools.scenario_catalog")
    except ModuleNotFoundError as exc:
        if exc.name == "tournament_tools.scenario_catalog":
            raise ImportError(
                "Synthetic modes require tournament_tools.scenario_catalog "
                "with ScenarioCase and build_validation_suite."
            ) from exc
        raise ImportError(
            f"Could not import a dependency of tournament_tools.scenario_catalog: {exc.name}"
        ) from exc
    if not hasattr(module, "ScenarioCase"):
        raise ImportError("tournament_tools.scenario_catalog does not expose ScenarioCase")
    if not callable(getattr(module, "build_validation_suite", None)):
        raise ImportError(
            "tournament_tools.scenario_catalog does not expose "
            "callable build_validation_suite"
        )
    return module


def _call_builder(builder: Callable[..., Any], held_out: bool) -> Sequence[ScenarioCase]:
    """Call common ScenarioCase builder APIs and fail clearly on mismatches."""

    signature = inspect.signature(builder)
    parameters = signature.parameters
    arguments: dict[str, Any] = {}
    if "held_out" in parameters:
        arguments["held_out"] = held_out
    elif "include_held_out" in parameters:
        arguments["include_held_out"] = held_out
    elif "split" in parameters:
        arguments["split"] = "held-out" if held_out else "synthetic"
    elif "mode" in parameters:
        arguments["mode"] = "held-out" if held_out else "synthetic"
    elif held_out:
        raise TypeError(
            "build_validation_suite must accept held_out, include_held_out, "
            "split, or mode to build the held-out suite"
        )

    try:
        cases = builder(**arguments)
    except TypeError as exc:
        raise TypeError(
            f"Could not call scenario_catalog.build_validation_suite{signature}: {exc}"
        ) from exc
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
        raise TypeError("build_validation_suite must return a sequence of ScenarioCase")
    for case in cases:
        _validate_case(case)
    return cast(Sequence[ScenarioCase], cases)


def build_cases(mode: BenchmarkMode) -> tuple[ScenarioCase, ...]:
    """Build the selected public and/or catalog-backed benchmark suite."""

    if mode not in {"smoke", "public", "synthetic", "held-out", "all"}:
        raise ValueError(f"Unknown benchmark mode: {mode}")

    public_cases = tuple(
        cast(ScenarioCase, _PublicScenarioCase(scenario))
        for scenario in PUBLIC_SCENARIOS
    )
    if mode == "smoke":
        return public_cases[:1]
    if mode == "public":
        return public_cases

    catalog = _scenario_catalog()
    builder = cast(Callable[..., Any], catalog.build_validation_suite)
    synthetic_cases = tuple(_call_builder(builder, held_out=False))
    if mode == "synthetic":
        return synthetic_cases
    held_out_cases = tuple(_call_builder(builder, held_out=True))
    if mode == "held-out":
        return held_out_cases

    combined = (*public_cases, *synthetic_cases, *held_out_cases)
    unique: list[ScenarioCase] = []
    seen: set[tuple[str, str, str, int]] = set()
    for case in combined:
        scenario, family, variant = _validate_case(case)
        key = (scenario.id, family, variant, scenario.seed)
        if key not in seen:
            seen.add(key)
            unique.append(case)
    return tuple(unique)


def write_jsonl(records: Iterable[BenchmarkRecord], stream: TextIO) -> None:
    """Write and flush records incrementally so partial runs remain useful."""

    for record in records:
        stream.write(record.to_json())
        stream.write("\n")
        stream.flush()


def _output_path(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    forbidden = (ARENA_ROOT / ".arena").resolve()
    if path == forbidden or forbidden in path.parents:
        raise ValueError(
            f"Refusing to write benchmark output inside simulator replay directory: {forbidden}"
        )
    return path


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run replay-free Traffic Lights Arena benchmarks and emit JSONL"
    )
    parser.add_argument("controller_path", type=Path, help="Path to controller.py")
    parser.add_argument(
        "--mode",
        choices=("smoke", "public", "synthetic", "held-out", "all"),
        default="smoke",
    )
    parser.add_argument(
        "--candidate-id",
        help="Stable candidate label (defaults to filename plus content hash)",
    )
    parser.add_argument(
        "--observe",
        action="store_true",
        help="Wrap each fresh controller with observer.ObservedController",
    )
    parser.add_argument(
        "--output-file",
        help="Write JSONL here instead of stdout (parent directories are created)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    try:
        cases = build_cases(cast(BenchmarkMode, args.mode))
        records = run_benchmark(
            args.controller_path,
            cases,
            candidate_id=args.candidate_id,
            observe=args.observe,
        )
        if args.output_file is None:
            write_jsonl(records, sys.stdout)
        else:
            output_path = _output_path(args.output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8", newline="\n") as stream:
                write_jsonl(records, stream)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(2, f"benchmark: error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
