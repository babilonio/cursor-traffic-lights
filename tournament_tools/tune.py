"""Deterministic, controller-agnostic successive-halving parameter tuner.

The module deliberately knows nothing about ``controller.py``.  Candidate
configuration dictionaries are passed to an evaluator callable together with
an opaque stage budget and a small context dictionary.

Evaluator protocol::

    result = evaluator(candidate, budget, context)

Two-argument ``evaluator(candidate, budget)`` callables are also accepted.
The result may be a JSON-compatible mapping or a number.  A scorer callable,
when supplied, receives that result and may return a mapping or number.

Run ``python tournament_tools/tune.py --help`` for the CLI and
``python tournament_tools/tune.py --self-test`` for dependency-free checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import importlib
import inspect
import itertools
import json
import math
import os
import pathlib
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any


JsonValue = Any
Evaluator = Callable[..., Any]
Scorer = Callable[[Any], Any]
MAX_WORKERS = 32
DEFAULT_ABLATIONS: dict[str, tuple[JsonValue, ...]] = {
    "global_sync": (False, True),
    "adaptive_split": (False, True),
    "lane_aggregation": ("mean", "max", "blended"),
    "spillback_contraction": (False, True),
    "oldest_wait": (False, True),
    "endgame_priority": (False, True),
    "local_emergency_override": (False, True),
}
_STAGE_CONTROL_KEYS = frozenset({"name", "budget", "keep", "retention_fraction"})


@dataclasses.dataclass(frozen=True)
class Stage:
    """One successive-halving stage.

    ``budget`` is opaque to the tuner. ``keep`` takes precedence over the
    stage-specific or global retention fraction.
    """

    name: str
    budget: JsonValue
    keep: int | None = None
    retention_fraction: float | None = None


@dataclasses.dataclass(frozen=True)
class Candidate:
    """Canonical candidate representation with a content-derived identity."""

    candidate_id: str
    candidate_hash: str
    parameters: dict[str, JsonValue]


@dataclasses.dataclass
class TuningResult:
    """Result returned by :func:`successive_halving`."""

    selected: list[Candidate]
    records: list[dict[str, JsonValue]]
    stages: list[dict[str, JsonValue]]

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "selected": [
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate_hash": candidate.candidate_hash,
                    "parameters": candidate.parameters,
                }
                for candidate in self.selected
            ],
            "stages": self.stages,
            "record_count": len(self.records),
        }


def canonical_json(value: JsonValue) -> str:
    """Return a stable JSON representation, rejecting non-portable values."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def candidate_digest(parameters: Mapping[str, JsonValue]) -> str:
    """Return the full SHA-256 digest for a parameter dictionary."""

    if not isinstance(parameters, Mapping):
        raise TypeError("candidate parameters must be a mapping")
    return hashlib.sha256(canonical_json(dict(parameters)).encode("utf-8")).hexdigest()


def make_candidate(parameters: Mapping[str, JsonValue]) -> Candidate:
    """Create a candidate whose stable ID is derived only from its parameters."""

    normalized = json.loads(canonical_json(dict(parameters)))
    digest = candidate_digest(normalized)
    return Candidate(f"c-{digest[:16]}", digest, normalized)


def normalize_candidates(
    candidates: Iterable[Mapping[str, JsonValue] | Candidate],
) -> list[Candidate]:
    """Canonicalize, deduplicate, and deterministically order candidates."""

    by_hash: dict[str, Candidate] = {}
    by_id: dict[str, str] = {}
    for value in candidates:
        candidate = value if isinstance(value, Candidate) else make_candidate(value)
        previous_hash = by_id.get(candidate.candidate_id)
        if previous_hash is not None and previous_hash != candidate.candidate_hash:
            raise ValueError(
                f"candidate ID collision for {candidate.candidate_id}; "
                "use full candidate hashes to distinguish these configurations"
            )
        by_hash[candidate.candidate_hash] = candidate
        by_id[candidate.candidate_id] = candidate.candidate_hash
    return sorted(by_hash.values(), key=lambda item: item.candidate_id)


def expand_grid(grid: Mapping[str, Sequence[JsonValue]]) -> list[dict[str, JsonValue]]:
    """Expand a parameter grid in stable key/value order."""

    if not isinstance(grid, Mapping):
        raise TypeError("grid must be a mapping of parameter names to value lists")
    names = sorted(grid)
    values: list[list[JsonValue]] = []
    for name in names:
        choices = grid[name]
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence):
            raise TypeError(f"grid value for {name!r} must be a sequence")
        if not choices:
            raise ValueError(f"grid value for {name!r} must not be empty")
        values.append(list(choices))
    if not names:
        return [{}]
    return [dict(zip(names, combination)) for combination in itertools.product(*values)]


def apply_ablation_grid(
    candidates: Iterable[Mapping[str, JsonValue]],
    ablations: Mapping[str, Sequence[JsonValue]],
) -> list[dict[str, JsonValue]]:
    """Cross candidate dictionaries with generic ablation dimensions."""

    variants = expand_grid(ablations)
    output: list[dict[str, JsonValue]] = []
    for candidate in candidates:
        for variant in variants:
            merged = dict(candidate)
            merged.update(variant)
            output.append(merged)
    return [item.parameters for item in normalize_candidates(output)]


def normalize_stages(budgets: Sequence[JsonValue | Stage]) -> list[Stage]:
    """Turn opaque budget values or stage dictionaries into validated stages."""

    if not budgets:
        raise ValueError("at least one stage budget is required")
    stages: list[Stage] = []
    seen_names: set[str] = set()
    for index, value in enumerate(budgets):
        if isinstance(value, Stage):
            stage = value
        elif isinstance(value, Mapping):
            raw = dict(value)
            name = str(raw.get("name", f"stage-{index + 1}"))
            if "budget" in raw:
                budget = raw["budget"]
            else:
                budget = {
                    key: item
                    for key, item in raw.items()
                    if key not in _STAGE_CONTROL_KEYS
                }
            keep_value = raw.get("keep")
            fraction_value = raw.get("retention_fraction")
            stage = Stage(
                name=name,
                budget=budget,
                keep=None if keep_value is None else int(keep_value),
                retention_fraction=(
                    None if fraction_value is None else float(fraction_value)
                ),
            )
        else:
            stage = Stage(name=f"stage-{index + 1}", budget=value)
        if not stage.name:
            raise ValueError(f"stage {index + 1} has an empty name")
        if stage.name in seen_names:
            raise ValueError(f"duplicate stage name: {stage.name!r}")
        if stage.keep is not None and stage.keep < 1:
            raise ValueError(f"stage {stage.name!r} keep must be positive")
        if stage.retention_fraction is not None:
            _validate_retention(stage.retention_fraction)
        canonical_json(stage.budget)
        stages.append(stage)
        seen_names.add(stage.name)
    return stages


def budget_hash(budget: JsonValue) -> str:
    """Hash a stage budget so resume data cannot silently use another budget."""

    return hashlib.sha256(canonical_json(budget).encode("utf-8")).hexdigest()[:16]


def metric_value(result: Mapping[str, JsonValue], metric: str) -> float:
    """Extract a numeric ranking metric using a dotted mapping path."""

    current: Any = result
    for part in metric.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(f"ranking metric {metric!r} is missing")
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise TypeError(f"ranking metric {metric!r} must be numeric")
    value = float(current)
    if not math.isfinite(value):
        raise ValueError(f"ranking metric {metric!r} must be finite")
    return value


def rank_records(
    records: Iterable[Mapping[str, JsonValue]],
    metric: str,
    maximize: bool = True,
) -> list[dict[str, JsonValue]]:
    """Rank successful records, breaking equal metrics by stable candidate ID."""

    ranked: list[tuple[float, str, dict[str, JsonValue]]] = []
    for original in records:
        record = dict(original)
        if record.get("status") != "ok":
            continue
        value = metric_value(record["result"], metric)
        candidate_id = str(record["candidate_id"])
        ranked.append((value, candidate_id, record))
    if maximize:
        ranked.sort(key=lambda item: (-item[0], item[1]))
    else:
        ranked.sort(key=lambda item: (item[0], item[1]))
    output: list[dict[str, JsonValue]] = []
    for position, (value, _candidate_id, record) in enumerate(ranked, 1):
        record["metric"] = value
        record["rank"] = position
        output.append(record)
    return output


def normalize_worker_count(workers: int, task_count: int | None = None) -> int:
    """Clamp requested concurrency to a conservative, documented bound."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    bounded = min(workers, MAX_WORKERS)
    if task_count is not None and task_count > 0:
        bounded = min(bounded, task_count)
    return max(1, bounded)


def _call_with_supported_arity(
    callable_object: Callable[..., Any],
    positional_options: Sequence[tuple[Any, ...]],
    role: str,
) -> Any:
    try:
        signature = inspect.signature(callable_object)
    except (TypeError, ValueError):
        return callable_object(*positional_options[0])
    for arguments in positional_options:
        try:
            signature.bind(*arguments)
        except TypeError:
            continue
        return callable_object(*arguments)
    expected = " or ".join(str(len(arguments)) for arguments in positional_options)
    raise TypeError(f"{role} callable must accept {expected} positional argument(s)")


def invoke_evaluator(
    evaluator: Evaluator,
    candidate: Candidate,
    stage: Stage,
    stage_index: int,
) -> Any:
    """Invoke the documented evaluator API without masking evaluator errors."""

    context = {
        "candidate_id": candidate.candidate_id,
        "candidate_hash": candidate.candidate_hash,
        "stage": stage.name,
        "stage_index": stage_index,
    }
    return _call_with_supported_arity(
        evaluator,
        (
            (dict(candidate.parameters), stage.budget, context),
            (dict(candidate.parameters), stage.budget),
        ),
        "evaluator",
    )


def normalize_result(value: Any) -> dict[str, JsonValue]:
    """Normalize an evaluator/scorer value to a JSON-compatible mapping."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, bool):
        raise TypeError("evaluation result must be a mapping or numeric value")
    if isinstance(value, (int, float)):
        result: dict[str, JsonValue] = {"score": value}
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise TypeError("evaluation result must be a mapping or numeric value")
    return json.loads(canonical_json(result))


def score_evaluation(raw_result: Any, scorer: Scorer | None = None) -> dict[str, JsonValue]:
    """Normalize an evaluation and optionally merge a scorer's aggregates."""

    if scorer is None:
        return normalize_result(raw_result)
    scorer_input = _json_compatible_value(raw_result)
    scored = _call_with_supported_arity(scorer, ((scorer_input,),), "scorer")
    if isinstance(scored, Sequence) and not isinstance(
        scored, (str, bytes, bytearray, Mapping)
    ):
        if len(scored) != 1:
            raise ValueError(
                "scorer must return exactly one candidate score per evaluation"
            )
        scored = scored[0]
    aggregates = normalize_result(scored)
    if (
        "score" not in aggregates
        and isinstance(aggregates.get("family_geometric_mean"), (int, float))
    ):
        aggregates["score"] = aggregates["family_geometric_mean"]
    if isinstance(scorer_input, Mapping):
        merged = dict(scorer_input)
        for key, value in aggregates.items():
            if key in merged and merged[key] != value:
                merged.setdefault("evaluation", dict(scorer_input))
            merged[key] = value
        return json.loads(canonical_json(merged))
    return json.loads(
        canonical_json({"benchmark_records": scorer_input, **aggregates})
    )


def _json_compatible_value(value: Any) -> JsonValue:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_compatible_value(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible_value(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_compatible_value(item) for item in value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_compatible_value(item) for item in value]
    canonical_json(value)
    return value


def resume_index(
    records: Iterable[Mapping[str, JsonValue]],
) -> dict[tuple[str, str, str], dict[str, JsonValue]]:
    """Index valid prior evaluations by candidate, stage, and budget hash."""

    index: dict[tuple[str, str, str], dict[str, JsonValue]] = {}
    for raw in records:
        record = dict(raw)
        if record.get("type", "evaluation") != "evaluation":
            continue
        required = ("candidate_id", "stage", "budget_hash", "status")
        if any(key not in record for key in required):
            continue
        key = (
            str(record["candidate_id"]),
            str(record["stage"]),
            str(record["budget_hash"]),
        )
        # Last JSONL entry wins, which permits an explicit successful retry.
        index[key] = record
    return index


def _validate_retention(retention_fraction: float) -> None:
    if not 0.0 < retention_fraction <= 1.0:
        raise ValueError("retention_fraction must be in the interval (0, 1]")


def _retained_count(stage: Stage, count: int, default_fraction: float) -> int:
    if stage.keep is not None:
        return min(count, stage.keep)
    fraction = (
        default_fraction
        if stage.retention_fraction is None
        else stage.retention_fraction
    )
    return min(count, max(1, math.ceil(count * fraction)))


def _evaluate_one(
    evaluator: Evaluator,
    scorer: Scorer | None,
    candidate: Candidate,
    stage: Stage,
    stage_index: int,
) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "type": "evaluation",
        "candidate_id": candidate.candidate_id,
        "candidate_hash": candidate.candidate_hash,
        "parameters": candidate.parameters,
        "stage": stage.name,
        "stage_index": stage_index,
        "budget": stage.budget,
        "budget_hash": budget_hash(stage.budget),
    }
    try:
        raw_result = invoke_evaluator(evaluator, candidate, stage, stage_index)
        base["result"] = score_evaluation(raw_result, scorer)
        base["status"] = "ok"
    except Exception as error:  # Keep a broad search alive; record exact failure.
        base["status"] = "error"
        base["error"] = f"{type(error).__name__}: {error}"
    return base


def successive_halving(
    candidates: Iterable[Mapping[str, JsonValue] | Candidate],
    evaluator: Evaluator,
    budgets: Sequence[JsonValue | Stage],
    retention_fraction: float = 0.2,
    metric: str = "score",
    maximize: bool = True,
    workers: int = 1,
    resume_records: Iterable[Mapping[str, JsonValue]] = (),
    scorer: Scorer | None = None,
    record_callback: Callable[[dict[str, JsonValue]], None] | None = None,
) -> TuningResult:
    """Evaluate and retain candidates over increasingly expensive stages.

    The function is deterministic provided the evaluator is deterministic.
    Work completion order never affects ranking, output records, or callbacks.
    Failed candidates are recorded but cannot advance. Resume records are used
    only when candidate ID, stage name, and budget hash all match.
    """

    _validate_retention(retention_fraction)
    normalized = normalize_candidates(candidates)
    if not normalized:
        raise ValueError("at least one candidate is required")
    stages = normalize_stages(budgets)
    worker_count = normalize_worker_count(workers, len(normalized))
    prior = resume_index(resume_records)
    active = normalized
    all_records: list[dict[str, JsonValue]] = []
    stage_summaries: list[dict[str, JsonValue]] = []

    for stage_index, stage in enumerate(stages):
        stage_records: list[dict[str, JsonValue]] = []
        pending: list[Candidate] = []
        stage_budget_hash = budget_hash(stage.budget)
        for candidate in active:
            key = (candidate.candidate_id, stage.name, stage_budget_hash)
            existing = prior.get(key)
            if (
                existing is not None
                and existing.get("candidate_hash") == candidate.candidate_hash
                and existing.get("status") == "ok"
                and isinstance(existing.get("result"), Mapping)
            ):
                resumed = dict(existing)
                resumed["resumed"] = True
                stage_records.append(resumed)
            else:
                pending.append(candidate)

        generated: list[dict[str, JsonValue]] = []
        if worker_count == 1 or len(pending) < 2:
            generated = [
                _evaluate_one(evaluator, scorer, candidate, stage, stage_index)
                for candidate in pending
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=normalize_worker_count(worker_count, len(pending)),
                thread_name_prefix="tune",
            ) as executor:
                futures = {
                    candidate.candidate_id: executor.submit(
                        _evaluate_one,
                        evaluator,
                        scorer,
                        candidate,
                        stage,
                        stage_index,
                    )
                    for candidate in pending
                }
                generated = [
                    futures[candidate_id].result()
                    for candidate_id in sorted(futures)
                ]
        stage_records.extend(generated)
        stage_records.sort(key=lambda record: str(record["candidate_id"]))
        for record in stage_records:
            if record.get("status") != "ok":
                continue
            try:
                metric_value(record["result"], metric)
            except (KeyError, TypeError, ValueError) as error:
                record["status"] = "error"
                record["error"] = f"{type(error).__name__}: {error}"
        generated_ids = {id(record) for record in generated}
        if record_callback is not None:
            for record in stage_records:
                if id(record) in generated_ids:
                    record_callback(dict(record))
        all_records.extend(stage_records)

        ranked = rank_records(stage_records, metric, maximize)
        if not ranked:
            errors = "; ".join(
                f"{record.get('candidate_id')}: {record.get('error', 'invalid metric')}"
                for record in stage_records[:5]
            )
            raise RuntimeError(
                f"stage {stage.name!r} produced no rankable results"
                + (f" ({errors})" if errors else "")
            )
        keep = _retained_count(stage, len(ranked), retention_fraction)
        selected_ids = [str(record["candidate_id"]) for record in ranked[:keep]]
        candidate_by_id = {candidate.candidate_id: candidate for candidate in active}
        active = [candidate_by_id[candidate_id] for candidate_id in selected_ids]
        stage_summaries.append(
            {
                "name": stage.name,
                "budget": stage.budget,
                "evaluated": len(stage_records),
                "resumed": sum(bool(record.get("resumed")) for record in stage_records),
                "successful": len(ranked),
                "kept": len(active),
                "selected_ids": selected_ids,
                "ranking": [
                    {
                        "candidate_id": record["candidate_id"],
                        "metric": record["metric"],
                        "rank": record["rank"],
                    }
                    for record in ranked
                ],
            }
        )

    return TuningResult(selected=active, records=all_records, stages=stage_summaries)


def _read_text_argument(value: str) -> str:
    if value.startswith("@"):
        path = pathlib.Path(value[1:])
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"cannot read {path}: {error}") from error
    return value


def _load_json_or_jsonl(path: os.PathLike[str] | str) -> list[JsonValue]:
    source = pathlib.Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"cannot read {source}: {error}") from error
    stripped = text.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        values: list[JsonValue] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{source}:{line_number}: invalid JSONL: {error.msg}"
                ) from error
        return values
    return value if isinstance(value, list) else [value]


def load_candidates(paths: Iterable[str]) -> list[dict[str, JsonValue]]:
    """Load candidate dictionaries from JSON arrays/objects or JSONL files."""

    candidates: list[dict[str, JsonValue]] = []
    for path in paths:
        for value in _load_json_or_jsonl(path):
            if not isinstance(value, Mapping):
                raise TypeError(f"{path}: each candidate must be a JSON object")
            if "parameters" in value and isinstance(value["parameters"], Mapping):
                candidates.append(dict(value["parameters"]))
            else:
                candidates.append(dict(value))
    return candidates


def load_grids(paths: Iterable[str]) -> list[dict[str, JsonValue]]:
    """Load and expand grid dictionaries from JSON or JSONL files."""

    candidates: list[dict[str, JsonValue]] = []
    for path in paths:
        for value in _load_json_or_jsonl(path):
            if not isinstance(value, Mapping):
                raise TypeError(f"{path}: each grid must be a JSON object")
            candidates.extend(expand_grid(value))
    return candidates


def load_resume_records(paths: Iterable[str]) -> list[dict[str, JsonValue]]:
    """Load prior JSONL/JSON evaluation records."""

    records: list[dict[str, JsonValue]] = []
    for path in paths:
        for value in _load_json_or_jsonl(path):
            if isinstance(value, Mapping):
                records.append(dict(value))
    return records


def parse_ablation_specs(specifications: Iterable[str]) -> dict[str, list[JsonValue]]:
    """Parse ``NAME``, ``NAME=JSON``, or ``NAME=JSON_ARRAY`` ablations."""

    ablations: dict[str, list[JsonValue]] = {}
    for specification in specifications:
        if "=" not in specification:
            name = specification.strip()
            if name not in DEFAULT_ABLATIONS:
                known = ", ".join(sorted(DEFAULT_ABLATIONS))
                raise ValueError(
                    f"bare ablation {name!r} is unknown; known names: {known}"
                )
            values = list(DEFAULT_ABLATIONS[name])
        else:
            name, encoded = specification.split("=", 1)
            name = name.strip()
            if not name:
                raise ValueError("ablation name must not be empty")
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSON for ablation {name!r}: {error.msg}"
                ) from error
            values = decoded if isinstance(decoded, list) else [decoded]
        if not values:
            raise ValueError(f"ablation {name!r} has no values")
        ablations[name] = values
    return ablations


def parse_budgets(encoded: str | None, repeated: Iterable[str]) -> list[JsonValue]:
    """Parse a JSON stage array plus optional repeated JSON stage values."""

    values: list[JsonValue] = []
    if encoded is not None:
        decoded = json.loads(_read_text_argument(encoded))
        if not isinstance(decoded, list):
            raise TypeError("--budgets must decode to a JSON array")
        values.extend(decoded)
    for item in repeated:
        values.append(json.loads(_read_text_argument(item)))
    if not values:
        raise ValueError("provide --budgets or at least one --budget")
    return values


def resolve_callable(
    specification: str,
    role: str,
    auto_modules: Sequence[str],
    auto_names: Sequence[str],
) -> Callable[..., Any]:
    """Resolve ``module:callable`` or lazily discover a sibling artifact API."""

    module_name: str
    attribute_name: str | None
    if specification == "auto":
        errors: list[str] = []
        for module_name in auto_modules:
            try:
                module = importlib.import_module(module_name)
            except ImportError as error:
                errors.append(f"{module_name}: {error}")
                continue
            for name in auto_names:
                value = getattr(module, name, None)
                if callable(value):
                    return value
            errors.append(
                f"{module_name}: none of {', '.join(auto_names)} is available"
            )
        detail = "; ".join(errors)
        raise RuntimeError(
            f"cannot auto-resolve {role}; install/create the sibling artifact or "
            f"pass --{role} module:callable ({detail})"
        )
    if ":" not in specification:
        raise ValueError(f"--{role} must be 'auto' or module:callable")
    module_name, attribute_name = specification.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise RuntimeError(
            f"cannot import {role} module {module_name!r}: {error}"
        ) from error
    value = getattr(module, attribute_name, None)
    if not callable(value):
        raise RuntimeError(
            f"{role} {specification!r} does not resolve to a callable"
        )
    return value


def resolve_evaluator(specification: str) -> Evaluator:
    if specification != "auto":
        return resolve_callable(specification, "evaluator", (), ())
    try:
        return resolve_callable(
            specification,
            "evaluator",
            ("tournament_tools.benchmark", "benchmark"),
            ("evaluate_candidate", "benchmark_candidate", "run_candidate"),
        )
    except RuntimeError as primary_error:
        module = None
        for module_name in ("tournament_tools.benchmark", "benchmark"):
            try:
                module = importlib.import_module(module_name)
                break
            except ImportError:
                continue
        if module is None:
            raise primary_error
        run_benchmark = getattr(module, "run_benchmark", None)
        build_cases = getattr(module, "build_cases", None)
        if not callable(run_benchmark) or not callable(build_cases):
            raise primary_error

        def benchmark_adapter(
            parameters: dict[str, JsonValue],
            budget: JsonValue,
            context: dict[str, JsonValue],
        ) -> list[JsonValue]:
            unsupported = sorted(set(parameters) - {"controller_path", "observe"})
            if unsupported:
                raise ValueError(
                    "tournament_tools.benchmark cannot inject candidate "
                    f"parameters {unsupported}; use a custom --evaluator that "
                    "constructs a controller from the parameter dictionary"
                )
            controller_path = parameters.get("controller_path")
            if not isinstance(controller_path, str) or not controller_path:
                raise ValueError(
                    "benchmark auto-adapter requires candidate key "
                    "'controller_path'; parameterized controllers should use a "
                    "custom --evaluator callable"
                )
            if isinstance(budget, Mapping):
                mode = budget.get("mode", "smoke")
                observe = bool(
                    budget.get("observe", parameters.get("observe", False))
                )
            else:
                mode = budget
                observe = bool(parameters.get("observe", False))
            if not isinstance(mode, str):
                raise TypeError("benchmark stage budget mode must be a string")
            cases = build_cases(mode)
            records = run_benchmark(
                controller_path,
                cases,
                candidate_id=context["candidate_id"],
                observe=observe,
            )
            return [_json_compatible_value(record) for record in records]

        return benchmark_adapter


def resolve_scorer(specification: str) -> Scorer:
    if specification != "auto":
        return resolve_callable(specification, "scorer", (), ())
    errors: list[str] = []
    for module_name in ("tournament_tools.tournament_score", "tournament_score"):
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            errors.append(f"{module_name}: {error}")
            continue
        for name in ("score_candidate", "aggregate_candidate", "tournament_score"):
            value = getattr(module, name, None)
            if callable(value):
                return value
        for name in ("score_records", "rank_candidates", "aggregate_candidates"):
            value = getattr(module, name, None)
            if not callable(value):
                continue

            def score_records_adapter(
                records: Any,
                aggregate: Callable[..., Any] = value,
            ) -> Any:
                return aggregate(records, expected_family_count=None)

            return score_records_adapter
        errors.append(
            f"{module_name}: no supported candidate aggregation callable is available"
        )
    raise RuntimeError(
        "cannot auto-resolve scorer; install/create "
        "tournament_tools.tournament_score or pass --scorer module:callable "
        f"({'; '.join(errors)})"
    )


class JsonlWriter:
    """Thread-safe append-only JSONL record sink."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def __call__(self, record: Mapping[str, JsonValue]) -> None:
        line = canonical_json(dict(record))
        try:
            with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(line + "\n")
        except OSError as error:
            raise RuntimeError(f"cannot append to {self.path}: {error}") from error


def _write_json(path: str, value: JsonValue) -> None:
    target = pathlib.Path(path)
    try:
        target.write_text(
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise RuntimeError(f"cannot write {target}: {error}") from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic successive-halving tuner. It passes parameter "
            "dictionaries to an evaluator and never rewrites controller source."
        )
    )
    parser.add_argument(
        "--candidates",
        action="append",
        default=[],
        metavar="FILE",
        help="JSON/JSONL candidate file; repeatable",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="JSON",
        help="inline candidate JSON object; repeatable",
    )
    parser.add_argument(
        "--grid",
        action="append",
        default=[],
        metavar="FILE",
        help="JSON/JSONL parameter-grid file; repeatable",
    )
    parser.add_argument(
        "--ablation",
        action="append",
        default=[],
        metavar="NAME[=JSON]",
        help=(
            "cross candidates with an ablation; bare mandatory names use their "
            "standard values, explicit values may be JSON arrays"
        ),
    )
    parser.add_argument(
        "--budgets",
        metavar="JSON_OR_@FILE",
        help="JSON array of stage budgets/specifications",
    )
    parser.add_argument(
        "--budget",
        action="append",
        default=[],
        metavar="JSON_OR_@FILE",
        help="one stage budget/specification; repeatable",
    )
    parser.add_argument(
        "--retention",
        type=float,
        default=0.2,
        help="default fraction retained after each stage (default: 0.2)",
    )
    parser.add_argument(
        "--metric",
        default="score",
        help="dotted numeric result field used for ranking (default: score)",
    )
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument(
        "--maximize", dest="maximize", action="store_true", default=True
    )
    direction.add_argument("--minimize", dest="maximize", action="store_false")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=f"evaluator threads, clamped to {MAX_WORKERS} (default: 1)",
    )
    parser.add_argument(
        "--evaluator",
        default="auto",
        metavar="MODULE:CALLABLE",
        help=(
            "evaluator API accepting (candidate, budget[, context]); 'auto' "
            "discovers tournament_tools.benchmark (default: auto)"
        ),
    )
    parser.add_argument(
        "--scorer",
        metavar="MODULE:CALLABLE",
        help=(
            "optional scorer accepting evaluator output; use 'auto' to discover "
            "tournament_tools.tournament_score"
        ),
    )
    parser.add_argument(
        "--resume",
        action="append",
        default=[],
        metavar="JSONL",
        help="reuse successful matching prior records; repeatable",
    )
    parser.add_argument(
        "--results-jsonl",
        metavar="FILE",
        help="append each newly evaluated result record",
    )
    parser.add_argument(
        "--selected-output",
        metavar="FILE",
        help="write selected candidates and stage summaries as JSON",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run dependency-free unit-level smoke checks",
    )
    return parser


def run_self_tests() -> dict[str, JsonValue]:
    """Run small deterministic checks without importing sibling artifacts."""

    first = make_candidate({"b": 2, "a": 1})
    second = make_candidate({"a": 1, "b": 2})
    assert first == second
    grid = expand_grid({"z": [2, 1], "a": [False, True]})
    assert grid == [
        {"a": False, "z": 2},
        {"a": False, "z": 1},
        {"a": True, "z": 2},
        {"a": True, "z": 1},
    ]
    ablations = parse_ablation_specs(
        ("global_sync", 'lane_aggregation=["mean","blended"]')
    )
    assert len(apply_ablation_grid([{"x": 1}], ablations)) == 4

    calls: list[tuple[int, int]] = []

    def evaluator(
        parameters: dict[str, JsonValue],
        budget: int,
        _context: dict[str, JsonValue],
    ) -> dict[str, float]:
        calls.append((parameters["value"], budget))
        return {"robust": float(parameters["value"] * budget)}

    candidates = [{"value": value} for value in range(6)]
    stages: list[JsonValue] = [
        {"name": "smoke", "budget": 1, "keep": 3},
        {"name": "full", "budget": 3, "keep": 1},
    ]
    initial = successive_halving(
        candidates,
        evaluator,
        stages,
        retention_fraction=0.5,
        metric="robust",
        workers=2,
    )
    assert initial.selected[0].parameters == {"value": 5}
    assert len(calls) == 9
    calls.clear()
    resumed = successive_halving(
        candidates,
        evaluator,
        stages,
        retention_fraction=0.5,
        metric="robust",
        workers=2,
        resume_records=initial.records,
    )
    assert resumed.selected == initial.selected
    assert calls == []

    tied = rank_records(
        [
            {
                "candidate_id": "c-b",
                "status": "ok",
                "result": {"score": 1},
            },
            {
                "candidate_id": "c-a",
                "status": "ok",
                "result": {"score": 1},
            },
        ],
        "score",
    )
    assert [record["candidate_id"] for record in tied] == ["c-a", "c-b"]
    return {
        "status": "ok",
        "checks": 7,
        "selected_candidate_id": initial.selected[0].candidate_id,
        "evaluations": len(initial.records),
    }


def _collect_cli_candidates(arguments: argparse.Namespace) -> list[dict[str, JsonValue]]:
    candidates = load_candidates(arguments.candidates)
    candidates.extend(load_grids(arguments.grid))
    for encoded in arguments.candidate:
        value = json.loads(_read_text_argument(encoded))
        if not isinstance(value, Mapping):
            raise TypeError("--candidate must decode to a JSON object")
        candidates.append(dict(value))
    if not candidates:
        raise ValueError("provide --candidates, --candidate, or --grid")
    ablations = parse_ablation_specs(arguments.ablation)
    if ablations:
        candidates = apply_ablation_grid(candidates, ablations)
    return candidates


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.self_test:
            print(json.dumps(run_self_tests(), sort_keys=True))
            return 0
        candidates = _collect_cli_candidates(arguments)
        budgets = parse_budgets(arguments.budgets, arguments.budget)
        evaluator = resolve_evaluator(arguments.evaluator)
        scorer = resolve_scorer(arguments.scorer) if arguments.scorer else None
        resume_records = load_resume_records(arguments.resume)
        writer = JsonlWriter(arguments.results_jsonl) if arguments.results_jsonl else None
        result = successive_halving(
            candidates,
            evaluator,
            budgets,
            retention_fraction=arguments.retention,
            metric=arguments.metric,
            maximize=arguments.maximize,
            workers=arguments.workers,
            resume_records=resume_records,
            scorer=scorer,
            record_callback=writer,
        )
        summary = result.as_dict()
        summary["results"] = result.records
        summary["metric"] = arguments.metric
        summary["maximize"] = arguments.maximize
        if arguments.selected_output:
            _write_json(arguments.selected_output, summary)
        print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        parser.exit(2, f"tune.py: error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
