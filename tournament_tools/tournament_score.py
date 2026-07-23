"""Deterministic local scoring for tournament benchmark JSONL records.

The scorer deliberately works with normalized cost ratios.  It does not try to
reconstruct the competition's hidden target-cost profiles.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from numbers import Real
from pathlib import Path
from typing import Any, TextIO


PUBLIC_WEIGHT = 0.20
PRIVATE_WEIGHT = 0.80
BASELINE_SCORE = 10_000.0
DEFAULT_FAMILY_COUNT = 6

Record = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    """Validated fields used by the scorer."""

    candidate_id: str
    scenario_id: str
    family: str
    variant: str
    cost: float
    baseline_cost: float
    public_score: float | None

    @property
    def ratio(self) -> float:
        return normalized_improvement_ratio(self.baseline_cost, self.cost)

    @property
    def map_key(self) -> tuple[str, str]:
        return self.scenario_id, self.variant


@dataclass(frozen=True, slots=True)
class MapScore:
    scenario_id: str
    family: str
    variant: str
    cost: float
    baseline_cost: float
    ratio: float


@dataclass(frozen=True, slots=True)
class FamilyScore:
    family: str
    minimum_ratio: float
    worst_map: MapScore


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate_id: str
    scenario_ratios: tuple[MapScore, ...]
    family_minima: tuple[FamilyScore, ...]
    family_geometric_mean: float
    worst_map: MapScore
    worst_family: FamilyScore
    public_geometric_mean: float | None
    private_proxy_score: float
    combined_estimate: float | None


def geometric_mean(values: Iterable[float]) -> float:
    """Return a numerically stable geometric mean of positive finite values."""

    numbers = tuple(
        _positive_number(value, f"geometric mean value at index {index}")
        for index, value in enumerate(values)
    )
    if not numbers:
        raise ValueError("geometric mean requires at least one value")
    return math.exp(math.fsum(math.log(value) for value in numbers) / len(numbers))


def normalized_improvement_ratio(baseline_cost: float, cost: float) -> float:
    """Compute ``baseline_cost / cost`` after validating both inputs."""

    baseline = _positive_number(baseline_cost, "baseline_cost")
    candidate = _positive_number(cost, "cost")
    return baseline / candidate


def record_improvement_ratio(record: Record) -> float:
    """Compute the normalized ratio directly from a benchmark record."""

    return normalized_improvement_ratio(
        _required_number(record, "baseline_cost", "record"),
        _required_number(record, "cost", "record"),
    )


def public_score_geometric_mean(public_scores: Iterable[float]) -> float:
    """Geometrically aggregate supplied public competition scores."""

    return geometric_mean(public_scores)


def combined_score_estimate(
    public_geometric_mean: float,
    private_score_estimate: float,
    *,
    public_weight: float = PUBLIC_WEIGHT,
) -> float:
    """Combine supplied public and local-private estimates on the same scale.

    The function applies only the documented arithmetic weighting.  In
    particular, it does not derive a hidden competition score from local costs.
    Callers are responsible for supplying estimates expressed on a compatible
    scale.
    """

    public = _positive_number(public_geometric_mean, "public_geometric_mean")
    private = _positive_number(private_score_estimate, "private_score_estimate")
    weight = _weight(public_weight)
    return weight * public + (1.0 - weight) * private


def validate_records(
    records: Iterable[Record],
    *,
    expected_family_count: int | None = DEFAULT_FAMILY_COUNT,
) -> tuple[BenchmarkRecord, ...]:
    """Validate and normalize records without mutating caller-owned mappings."""

    if expected_family_count is not None:
        if (
            isinstance(expected_family_count, bool)
            or not isinstance(expected_family_count, int)
            or expected_family_count <= 0
        ):
            raise ValueError("expected_family_count must be a positive integer or None")

    validated: list[BenchmarkRecord] = []
    seen: set[tuple[str, str, str]] = set()
    catalog: dict[tuple[str, str], tuple[str, float]] = {}
    candidate_maps: dict[str, set[tuple[str, str]]] = {}

    for index, raw in enumerate(records):
        location = f"record {index}"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{location} must be a dictionary-like mapping")

        candidate_id = _required_text(raw, "candidate_id", location)
        scenario_id = _required_text(raw, "scenario_id", location)
        family = _required_text(raw, "family", location)
        variant = _required_text(raw, "variant", location)
        cost = _required_number(raw, "cost", location)
        baseline_cost = _required_number(raw, "baseline_cost", location)
        public_score = _optional_number(raw, "public_score", location)

        duplicate_key = candidate_id, scenario_id, variant
        if duplicate_key in seen:
            raise ValueError(
                f"duplicate record for candidate {candidate_id!r}, "
                f"scenario {scenario_id!r}, variant {variant!r}"
            )
        seen.add(duplicate_key)

        map_key = scenario_id, variant
        map_metadata = family, baseline_cost
        previous_metadata = catalog.get(map_key)
        if previous_metadata is not None and previous_metadata != map_metadata:
            previous_family, previous_baseline = previous_metadata
            raise ValueError(
                f"inconsistent map metadata for scenario {scenario_id!r}, "
                f"variant {variant!r}: expected family {previous_family!r} and "
                f"baseline_cost {previous_baseline}, got family {family!r} and "
                f"baseline_cost {baseline_cost}"
            )
        catalog[map_key] = map_metadata
        candidate_maps.setdefault(candidate_id, set()).add(map_key)

        validated.append(
            BenchmarkRecord(
                candidate_id=candidate_id,
                scenario_id=scenario_id,
                family=family,
                variant=variant,
                cost=cost,
                baseline_cost=baseline_cost,
                public_score=public_score,
            )
        )

    if not validated:
        raise ValueError("at least one benchmark record is required")

    expected_maps = set(catalog)
    for candidate_id in sorted(candidate_maps):
        missing_maps = expected_maps - candidate_maps[candidate_id]
        if missing_maps:
            raise ValueError(
                f"candidate {candidate_id!r} is missing benchmark maps: "
                f"{_format_map_keys(missing_maps)}"
            )

        families = {
            record.family
            for record in validated
            if record.candidate_id == candidate_id
        }
        if expected_family_count is not None and len(families) != expected_family_count:
            raise ValueError(
                f"candidate {candidate_id!r} has {len(families)} families; "
                f"expected {expected_family_count}"
            )

    return tuple(validated)


def aggregate_candidates(
    records: Iterable[Record],
    *,
    expected_family_count: int | None = DEFAULT_FAMILY_COUNT,
    include_combined_estimate: bool = False,
    public_weight: float = PUBLIC_WEIGHT,
) -> tuple[CandidateScore, ...]:
    """Aggregate validated records, sorted deterministically by candidate ID."""

    validated = validate_records(
        records, expected_family_count=expected_family_count
    )
    weight = _weight(public_weight)
    by_candidate: dict[str, list[BenchmarkRecord]] = {}
    for record in validated:
        by_candidate.setdefault(record.candidate_id, []).append(record)

    results = [
        _aggregate_candidate(
            candidate_id,
            candidate_records,
            include_combined_estimate=include_combined_estimate,
            public_weight=weight,
        )
        for candidate_id, candidate_records in sorted(by_candidate.items())
    ]
    return tuple(results)


def rank_candidates(
    records: Iterable[Record],
    *,
    expected_family_count: int | None = DEFAULT_FAMILY_COUNT,
    include_combined_estimate: bool = False,
    public_weight: float = PUBLIC_WEIGHT,
) -> tuple[CandidateScore, ...]:
    """Rank by family geometric mean, then public score, then candidate ID."""

    scores = aggregate_candidates(
        records,
        expected_family_count=expected_family_count,
        include_combined_estimate=include_combined_estimate,
        public_weight=public_weight,
    )
    return tuple(sorted(scores, key=_ranking_key))


def score_records(
    records: Iterable[Record],
    *,
    expected_family_count: int | None = DEFAULT_FAMILY_COUNT,
    include_combined_estimate: bool = False,
    public_weight: float = PUBLIC_WEIGHT,
) -> tuple[CandidateScore, ...]:
    """Convenience alias for :func:`rank_candidates`."""

    return rank_candidates(
        records,
        expected_family_count=expected_family_count,
        include_combined_estimate=include_combined_estimate,
        public_weight=public_weight,
    )


def load_jsonl(stream: TextIO) -> list[dict[str, object]]:
    """Read benchmark records from a JSON Lines text stream."""

    records: list[dict[str, object]] = []
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON on line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {line_number} must contain an object")
        records.append(value)
    if not records:
        raise ValueError("JSONL input contains no records")
    return records


def candidate_score_dict(score: CandidateScore, *, rank: int | None = None) -> dict[str, Any]:
    """Convert a score to a JSON-serializable dictionary."""

    result: dict[str, Any] = asdict(score)
    if rank is not None:
        result = {"rank": rank, **result}
    return result


def _aggregate_candidate(
    candidate_id: str,
    records: Sequence[BenchmarkRecord],
    *,
    include_combined_estimate: bool,
    public_weight: float,
) -> CandidateScore:
    map_scores = tuple(
        sorted(
            (
                MapScore(
                    scenario_id=record.scenario_id,
                    family=record.family,
                    variant=record.variant,
                    cost=record.cost,
                    baseline_cost=record.baseline_cost,
                    ratio=record.ratio,
                )
                for record in records
            ),
            key=lambda score: (score.family, score.scenario_id, score.variant),
        )
    )

    by_family: dict[str, list[MapScore]] = {}
    for score in map_scores:
        by_family.setdefault(score.family, []).append(score)
    family_scores = tuple(
        FamilyScore(
            family=family,
            minimum_ratio=min(
                scores,
                key=lambda score: (score.ratio, score.scenario_id, score.variant),
            ).ratio,
            worst_map=min(
                scores,
                key=lambda score: (score.ratio, score.scenario_id, score.variant),
            ),
        )
        for family, scores in sorted(by_family.items())
    )

    family_geometric_mean = geometric_mean(
        family.minimum_ratio for family in family_scores
    )
    worst_map = min(
        map_scores,
        key=lambda score: (
            score.ratio,
            score.family,
            score.scenario_id,
            score.variant,
        ),
    )
    worst_family = min(
        family_scores, key=lambda score: (score.minimum_ratio, score.family)
    )

    supplied_public_scores = tuple(
        record.public_score
        for record in sorted(records, key=lambda item: item.map_key)
        if record.public_score is not None
    )
    public_geometric_mean = (
        public_score_geometric_mean(supplied_public_scores)
        if supplied_public_scores
        else None
    )
    # A ratio of 1.0 means baseline performance, which corresponds to 10,000
    # competition points. This linear baseline-relative proxy needs no unknown
    # target ("gold") costs and is intentionally labeled as an estimate.
    private_proxy_score = BASELINE_SCORE * family_geometric_mean
    combined_estimate: float | None = None
    if include_combined_estimate:
        if public_geometric_mean is None:
            raise ValueError(
                f"candidate {candidate_id!r} has no public_score values; "
                "cannot compute combined estimate"
            )
        combined_estimate = combined_score_estimate(
            public_geometric_mean,
            private_proxy_score,
            public_weight=public_weight,
        )

    return CandidateScore(
        candidate_id=candidate_id,
        scenario_ratios=map_scores,
        family_minima=family_scores,
        family_geometric_mean=family_geometric_mean,
        worst_map=worst_map,
        worst_family=worst_family,
        public_geometric_mean=public_geometric_mean,
        private_proxy_score=private_proxy_score,
        combined_estimate=combined_estimate,
    )


def _ranking_key(score: CandidateScore) -> tuple[float, float, str]:
    public_tiebreak = (
        score.public_geometric_mean
        if score.public_geometric_mean is not None
        else float("-inf")
    )
    return -score.family_geometric_mean, -public_tiebreak, score.candidate_id


def _required_text(record: Record, key: str, location: str) -> str:
    if key not in record:
        raise ValueError(f"{location} is missing required key {key!r}")
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} key {key!r} must be a non-empty string")
    return value


def _required_number(record: Record, key: str, location: str) -> float:
    if key not in record:
        raise ValueError(f"{location} is missing required key {key!r}")
    return _positive_number(record[key], f"{location} key {key!r}")


def _optional_number(record: Record, key: str, location: str) -> float | None:
    if key not in record or record[key] is None:
        return None
    return _positive_number(record[key], f"{location} key {key!r}")


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


def _weight(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("public_weight must be a finite number from 0 to 1")
    weight = float(value)
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        raise ValueError("public_weight must be a finite number from 0 to 1")
    return weight


def _format_map_keys(keys: Iterable[tuple[str, str]]) -> str:
    return ", ".join(
        f"{scenario_id!r}/{variant!r}"
        for scenario_id, variant in sorted(keys)
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank tournament benchmark records from JSONL."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="JSONL file path, or - for stdin (default)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print complete ranking details as JSON",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="include the optional 20/80 public/local combined estimate",
    )
    parser.add_argument(
        "--expected-families",
        type=int,
        default=DEFAULT_FAMILY_COUNT,
        metavar="N",
        help="required family count per candidate (default: 6)",
    )
    return parser


def _read_input(path: str) -> list[dict[str, object]]:
    if path == "-":
        return load_jsonl(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as stream:
        return load_jsonl(stream)


def _format_text(rank: int, score: CandidateScore) -> str:
    public = (
        f"{score.public_geometric_mean:.6g}"
        if score.public_geometric_mean is not None
        else "n/a"
    )
    combined = (
        f" combined={score.combined_estimate:.6g}"
        if score.combined_estimate is not None
        else ""
    )
    return (
        f"{rank}. {score.candidate_id} "
        f"family_gmean={score.family_geometric_mean:.6g} "
        f"worst_map={score.worst_map.scenario_id}/{score.worst_map.variant}"
        f"({score.worst_map.ratio:.6g}) "
        f"worst_family={score.worst_family.family}"
        f"({score.worst_family.minimum_ratio:.6g}) "
        f"public_gmean={public}{combined}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        rankings = rank_candidates(
            _read_input(args.input),
            expected_family_count=args.expected_families,
            include_combined_estimate=args.combined,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.json:
        payload = {
            "rankings": [
                candidate_score_dict(score, rank=rank)
                for rank, score in enumerate(rankings, start=1)
            ]
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for rank, score in enumerate(rankings, start=1):
            print(_format_text(rank, score))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
