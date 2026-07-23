"""Versioned JSONL results ledger for local tournament experiments.

The canonical v1 record is deliberately flat for grouping and aggregation:

``schema_version``, ``record_type``, ``timestamp``, ``source_revision``,
``candidate_id``, ``controller_family``, ``parameters``, ``config_hash``,
``candidate_hash``, scenario identity, raw metrics, ``diagnostics``,
``aggregates``, and ``metadata``. Compatibility aliases (``scenario_id`` /
``scenario`` and ``family`` / ``scenario_family``) let normalized rows pass
directly to the local tournament scorer.

The normalizer also accepts common nested benchmark output (``candidate``,
``scenario`` and ``metrics`` mappings). Rewrites use a temporary file followed
by ``os.replace``. Importing this module never creates or changes files.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Iterator, Mapping


SCHEMA_NAME = "traffic-lights-result"
SCHEMA_VERSION = 1
RECORD_TYPE = "scenario_result"

REQUIRED_FIELDS = (
    "schema_version",
    "record_type",
    "timestamp",
    "source_revision",
    "candidate_id",
    "controller_family",
    "parameters",
    "config_hash",
    "candidate_hash",
    "scenario",
    "scenario_id",
    "scenario_family",
    "family",
    "variant",
    "seed",
    "cost",
    "wait_ticks",
    "unfinished",
    "completed",
    "diagnostics",
    "aggregates",
    "metadata",
)
NULLABLE_FIELDS = (
    "spawned",
    "baseline_cost",
    "public_score",
    "elapsed_seconds",
)
CANONICAL_FIELDS = (
    *REQUIRED_FIELDS[:15],
    "spawned",
    "cost",
    "baseline_cost",
    "public_score",
    "wait_ticks",
    "unfinished",
    "completed",
    "elapsed_seconds",
    *REQUIRED_FIELDS[19:],
)
RESULT_SCHEMA = {
    "name": SCHEMA_NAME,
    "version": SCHEMA_VERSION,
    "record_type": RECORD_TYPE,
    "required_fields": REQUIRED_FIELDS,
    "nullable_fields": NULLABLE_FIELDS,
}

_INPUT_FIELDS = frozenset(
    CANONICAL_FIELDS
    + (
        "candidate",
        "config",
        "metrics",
        "observer_diagnostics",
        "aggregate",
        "ranking",
        "revision",
        "scenario_id",
    )
)
_IDENTITY_METADATA_FIELDS = ("scenario_hash", "suite", "run_length")


class ResultValidationError(ValueError):
    """Raised when a result cannot be represented by the current schema."""


def utc_timestamp() -> str:
    """Return the current UTC time in canonical ISO-8601 form."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _json_value(value: Any, path: str = "$") -> Any:
    """Copy *value* into a deterministic, finite JSON-compatible structure."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResultValidationError(f"{path} must not contain NaN or infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultValidationError(f"{path} has a non-string object key")
            normalized[key] = _json_value(item, f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ResultValidationError(
        f"{path} contains unsupported value type {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Serialize a value deterministically for hashing or JSONL output."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_hash(value: Any, *, namespace: str = SCHEMA_NAME) -> str:
    """Return a stable SHA-256 hex digest for JSON-compatible *value*."""

    payload = namespace.encode("utf-8") + b"\0" + canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stable_config_hash(parameters: Mapping[str, Any]) -> str:
    """Hash a complete controller parameter mapping independent of key order."""

    if not isinstance(parameters, Mapping):
        raise ResultValidationError("parameters must be a mapping")
    return stable_hash(parameters, namespace=f"{SCHEMA_NAME}:config:v{SCHEMA_VERSION}")


def stable_candidate_hash(
    controller_family: str, parameters: Mapping[str, Any]
) -> str:
    """Hash the policy family and complete parameters into a candidate identity."""

    if not isinstance(controller_family, str) or not controller_family.strip():
        raise ResultValidationError("controller_family must be a non-empty string")
    return stable_hash(
        {
            "controller_family": controller_family.strip(),
            "parameters": parameters,
        },
        namespace=f"{SCHEMA_NAME}:candidate:v{SCHEMA_VERSION}",
    )


# Short names are useful to callers constructing benchmark records.
config_hash = stable_config_hash
candidate_hash = stable_candidate_hash


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ResultValidationError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultValidationError(f"{field} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ResultValidationError(f"{field} must be finite")
    if value < 0:
        raise ResultValidationError(f"{field} must be non-negative")
    return 0.0 if isinstance(value, float) and value == 0.0 else value


def _optional_nonnegative_number(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    return _nonnegative_number(value, field)


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultValidationError(f"{field} must be an integer")
    if value < 0:
        raise ResultValidationError(f"{field} must be non-negative")
    return value


def _optional_nonnegative_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, field)


def _consistent_alias(
    primary: Any, alias: Any, primary_name: str, alias_name: str
) -> Any:
    if primary is not None and alias is not None and primary != alias:
        raise ResultValidationError(
            f"{primary_name} and {alias_name} must identify the same value"
        )
    return primary if primary is not None else alias


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            )
        except ValueError as error:
            raise ResultValidationError("timestamp must be valid ISO-8601") from error
    else:
        raise ResultValidationError("timestamp must be an ISO-8601 string or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResultValidationError("timestamp must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def normalize_record(
    record: Mapping[str, Any], *, add_timestamp: bool = True
) -> dict[str, Any]:
    """Return one validated canonical v1 scenario-result record.

    Nested ``candidate``, ``scenario`` and ``metrics`` objects are accepted.
    Unrecognized input fields are retained under ``metadata.extra_fields`` so
    normalization is lossless. Missing timestamps are generated only when
    ``add_timestamp`` is true.
    """

    if not isinstance(record, Mapping):
        raise ResultValidationError("record must be an object")

    requested_version = record.get("schema_version", SCHEMA_VERSION)
    if requested_version != SCHEMA_VERSION:
        raise ResultValidationError(
            f"unsupported schema_version {requested_version!r}; expected {SCHEMA_VERSION}"
        )
    requested_type = record.get("record_type", RECORD_TYPE)
    if requested_type != RECORD_TYPE:
        raise ResultValidationError(
            f"unsupported record_type {requested_type!r}; expected {RECORD_TYPE!r}"
        )

    candidate = _mapping(record.get("candidate"), "candidate")
    metrics = _mapping(record.get("metrics"), "metrics")

    scenario_input = record.get("scenario")
    if isinstance(scenario_input, Mapping):
        scenario_data = scenario_input
        scenario_name = scenario_data.get("id", scenario_data.get("name"))
    else:
        scenario_data = {}
        scenario_name = scenario_input
    scenario_name = _consistent_alias(
        scenario_name,
        record.get("scenario_id"),
        "scenario",
        "scenario_id",
    )

    parameters = record.get(
        "parameters",
        record.get("config", candidate.get("parameters", candidate.get("config"))),
    )
    if not isinstance(parameters, Mapping):
        raise ResultValidationError("parameters must be a complete controller mapping")
    parameters_copy = _json_value(parameters, "$.parameters")

    family = record.get(
        "controller_family",
        candidate.get("family"),
    )
    family = _required_text(family, "controller_family")
    calculated_config_hash = stable_config_hash(parameters_copy)
    calculated_candidate_hash = stable_candidate_hash(family, parameters_copy)

    supplied_config_hash = record.get(
        "config_hash", candidate.get("config_hash")
    )
    if supplied_config_hash is not None and supplied_config_hash != calculated_config_hash:
        raise ResultValidationError("config_hash does not match parameters")
    supplied_candidate_hash = record.get(
        "candidate_hash", candidate.get("candidate_hash")
    )
    if (
        supplied_candidate_hash is not None
        and supplied_candidate_hash != calculated_candidate_hash
    ):
        raise ResultValidationError(
            "candidate_hash does not match controller_family and parameters"
        )

    timestamp_value = record.get("timestamp")
    if timestamp_value is None:
        if not add_timestamp:
            raise ResultValidationError("timestamp is required")
        timestamp_value = utc_timestamp()

    source_revision = record.get("source_revision", record.get("revision"))
    candidate_id = record.get("candidate_id", candidate.get("id"))
    if candidate_id is None:
        candidate_id = f"candidate-{calculated_candidate_hash[:12]}"

    scenario_family = _consistent_alias(
        record.get("scenario_family", scenario_data.get("family")),
        record.get("family"),
        "scenario_family",
        "family",
    )
    scenario_family = _required_text(scenario_family, "scenario_family")

    variant = record.get("variant", scenario_data.get("variant", "default"))
    seed = record.get("seed", scenario_data.get("seed"))
    spawned = record.get("spawned", metrics.get("spawned"))
    cost = record.get("cost", metrics.get("cost"))
    baseline_cost = record.get("baseline_cost", metrics.get("baseline_cost"))
    public_score = record.get("public_score", metrics.get("public_score"))
    wait_ticks = record.get(
        "wait_ticks", metrics.get("wait_ticks", metrics.get("waiting_ticks"))
    )
    unfinished = record.get("unfinished", metrics.get("unfinished"))
    completed = record.get("completed", metrics.get("completed"))
    elapsed_seconds = record.get(
        "elapsed_seconds", metrics.get("elapsed_seconds")
    )

    diagnostics = record.get(
        "diagnostics",
        record.get("observer_diagnostics", metrics.get("diagnostics", {})),
    )
    aggregates = record.get(
        "aggregates", record.get("aggregate", record.get("ranking", {}))
    )
    metadata = dict(_mapping(record.get("metadata"), "metadata"))
    extra_fields = {
        key: value for key, value in record.items() if key not in _INPUT_FIELDS
    }
    if extra_fields:
        existing_extra = metadata.get("extra_fields")
        if existing_extra is not None:
            existing_extra = dict(_mapping(existing_extra, "metadata.extra_fields"))
            existing_extra.update(extra_fields)
            extra_fields = existing_extra
        metadata["extra_fields"] = extra_fields

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "timestamp": _timestamp(timestamp_value),
        "source_revision": _required_text(source_revision, "source_revision"),
        "candidate_id": _required_text(candidate_id, "candidate_id"),
        "controller_family": family,
        "parameters": parameters_copy,
        "config_hash": calculated_config_hash,
        "candidate_hash": calculated_candidate_hash,
        "scenario": _required_text(scenario_name, "scenario"),
        "scenario_id": _required_text(scenario_name, "scenario_id"),
        "scenario_family": scenario_family,
        "family": scenario_family,
        "variant": _required_text(variant, "variant"),
        "seed": _nonnegative_integer(seed, "seed"),
        "spawned": _optional_nonnegative_integer(spawned, "spawned"),
        "cost": _nonnegative_number(cost, "cost"),
        "baseline_cost": _optional_nonnegative_number(
            baseline_cost, "baseline_cost"
        ),
        "public_score": _optional_nonnegative_number(public_score, "public_score"),
        "wait_ticks": _nonnegative_integer(wait_ticks, "wait_ticks"),
        "unfinished": _nonnegative_integer(unfinished, "unfinished"),
        "completed": _nonnegative_integer(completed, "completed"),
        "elapsed_seconds": _optional_nonnegative_number(
            elapsed_seconds, "elapsed_seconds"
        ),
        "diagnostics": _json_value(
            _mapping(diagnostics, "diagnostics"), "$.diagnostics"
        ),
        "aggregates": _json_value(
            _mapping(aggregates, "aggregates"), "$.aggregates"
        ),
        "metadata": _json_value(metadata, "$.metadata"),
    }
    return normalized


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical record without inventing a timestamp."""

    return normalize_record(record, add_timestamp=False)


def validation_errors(record: Mapping[str, Any]) -> list[str]:
    """Return validation messages, or an empty list for a valid record."""

    try:
        validate_record(record)
    except (ResultValidationError, TypeError) as error:
        return [str(error)]
    return []


def is_valid_record(record: Mapping[str, Any]) -> bool:
    """Return whether *record* validates against the current schema."""

    return not validation_errors(record)


def result_identity(record: Mapping[str, Any]) -> str:
    """Return the default deduplication identity for one experiment.

    Timestamp, metrics and diagnostics are intentionally excluded. The
    candidate, scenario, seed and revision identify a repeat; scenario hash,
    suite and run length are included when present in metadata.
    """

    normalized = normalize_record(record)
    metadata = normalized["metadata"]
    return stable_hash(
        {
            "candidate_hash": normalized["candidate_hash"],
            "scenario_id": normalized["scenario_id"],
            "scenario_family": normalized["scenario_family"],
            "variant": normalized["variant"],
            "seed": normalized["seed"],
            "source_revision": normalized["source_revision"],
            "run_context": {
                name: metadata[name]
                for name in _IDENTITY_METADATA_FIELDS
                if name in metadata
            },
        },
        namespace=f"{SCHEMA_NAME}:result-identity:v{SCHEMA_VERSION}",
    )


def _reject_constant(text: str) -> None:
    raise ResultValidationError(f"non-finite JSON number {text!r} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResultValidationError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _decode_json_line(line: str, line_number: int, path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            line,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ResultValidationError) as error:
        raise ResultValidationError(
            f"{path}:{line_number}: invalid JSON: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise ResultValidationError(
            f"{path}:{line_number}: each JSONL row must be an object"
        )
    return value


def iter_records(
    path: str | os.PathLike[str], *, validate: bool = True
) -> Iterator[dict[str, Any]]:
    """Yield records from *path*; a missing ledger is treated as empty."""

    ledger_path = Path(path)
    if not ledger_path.exists():
        return
    with ledger_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            raw = _decode_json_line(line, line_number, ledger_path)
            if not validate:
                yield dict(raw)
                continue
            try:
                yield normalize_record(raw, add_timestamp=False)
            except ResultValidationError as error:
                raise ResultValidationError(
                    f"{ledger_path}:{line_number}: {error}"
                ) from error


def read_records(
    path: str | os.PathLike[str], *, validate: bool = True
) -> list[dict[str, Any]]:
    """Read all JSONL records from *path*."""

    return list(iter_records(path, validate=validate))


def append_record(
    path: str | os.PathLike[str], record: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and append one record, returning its canonical representation."""

    normalized = normalize_record(record)
    payload = (canonical_json(normalized) + "\n").encode("utf-8")
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(ledger_path, flags, 0o644)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(
                f"short append to {ledger_path}: wrote {written} of {len(payload)} bytes"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return normalized


def write_records_atomic(
    path: str | os.PathLike[str], records: Iterable[Mapping[str, Any]]
) -> int:
    """Validate and atomically replace *path* with canonical JSONL records."""

    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [normalize_record(record) for record in records]

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{ledger_path.name}.",
        suffix=".tmp",
        dir=ledger_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        if ledger_path.exists():
            os.chmod(temporary_path, ledger_path.stat().st_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            for record in normalized:
                stream.write(canonical_json(record))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, ledger_path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return len(normalized)


def deduplicate_records(
    path: str | os.PathLike[str],
    *,
    key: Callable[[Mapping[str, Any]], Hashable] | None = None,
    keep: str = "last",
) -> int:
    """Atomically remove duplicate experiments and return the removal count.

    ``keep`` may be ``"first"`` or ``"last"``. By default records with the
    same :func:`result_identity` are duplicates and the newest row wins.
    """

    if keep not in {"first", "last"}:
        raise ValueError("keep must be 'first' or 'last'")
    records = read_records(path)
    key_function = key or result_identity
    indexes = range(len(records))
    if keep == "last":
        indexes = reversed(range(len(records)))

    seen: set[Hashable] = set()
    retained_indexes: list[int] = []
    for index in indexes:
        identity = key_function(records[index])
        try:
            duplicate = identity in seen
        except TypeError as error:
            raise TypeError("deduplication key must return a hashable value") from error
        if not duplicate:
            seen.add(identity)
            retained_indexes.append(index)
    retained_indexes.sort()
    retained = [records[index] for index in retained_indexes]
    removed = len(records) - len(retained)
    write_records_atomic(path, retained)
    return removed


# Domain-friendly aliases for callers that use "result" rather than "record".
normalize_result = normalize_record
validate_result = validate_record
append_result = append_record
read_results = read_records
deduplicate_results = deduplicate_records


def _smoke_tests() -> None:
    """Exercise hashes, validation, JSONL append/read and atomic deduplication."""

    base = {
        "timestamp": "2026-07-23T18:00:00Z",
        "source_revision": "local-test-revision",
        "candidate_id": "family-representative-01",
        "controller_family": "coordinated-adaptive",
        "parameters": {"alpha": 0.08, "green_budget": 32},
        "scenario": {
            "id": "balanced-small",
            "family": "balanced",
            "variant": "steady",
            "seed": 17,
        },
        "metrics": {
            "spawned": 100,
            "cost": 1234.5,
            "baseline_cost": 1500,
            "wait_ticks": 334,
            "unfinished": 3,
            "completed": 97,
            "elapsed_seconds": 0.2,
        },
        "diagnostics": {"switch_count": 9},
        "metadata": {"suite": "smoke", "run_length": 500},
    }
    assert stable_config_hash({"b": 2, "a": 1}) == stable_config_hash(
        {"a": 1, "b": 2}
    )

    with tempfile.TemporaryDirectory() as directory:
        ledger = Path(directory) / "nested" / "results.jsonl"
        first = append_record(ledger, base)
        assert first["scenario_family"] == "balanced"
        assert first["scenario_id"] == first["scenario"]
        assert first["family"] == first["scenario_family"]
        assert read_records(ledger) == [first]

        repeated = dict(base)
        repeated["timestamp"] = "2026-07-23T18:01:00Z"
        repeated["metrics"] = dict(base["metrics"], cost=1200)
        append_record(ledger, repeated)
        assert deduplicate_records(ledger, keep="last") == 1
        remaining = read_records(ledger)
        assert len(remaining) == 1 and remaining[0]["cost"] == 1200

        copied = Path(directory) / "copy.jsonl"
        assert write_records_atomic(copied, remaining) == 1
        assert read_records(copied) == remaining

        invalid = dict(first, config_hash="not-the-parameter-hash")
        assert not is_valid_record(invalid)


if __name__ == "__main__":
    _smoke_tests()
    print("results.py smoke tests passed")
