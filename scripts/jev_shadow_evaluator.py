"""Read-only evaluator for JEV shadow entry outcomes.

The evaluator never mutates its input ledgers and writes no report files. It
joins review metadata to outcome rows, separates incompatible semantics/cost
cohorts, and makes insufficient evidence explicit before showing metrics.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from r20_backend.time_utils import parse_beijing

DEFAULT_REVIEWS = Path("data/jev_shadow_reviews.jsonl")
DEFAULT_OUTCOMES = Path("data/jev_shadow_entry_outcomes.jsonl")
REPORT_VERSION = 1
ENTRY_ACTIONS = {"BUY_LONG", "SELL_SHORT"}
WEAK_STATUSES = {
    "low_confidence", "ambiguous", "not_ready",
    "missing_action_votes", "missing_execution_ready",
    "code_state_incomplete", "code_state_inconsistent",
    "invalid_data", "missing_data_valid",
}
MEASURED_SOURCES = {
    "mark_to_market", "actual_close", "trading_ledger",
    "live_position_mark_net", "live_position_close_net",
}
TIME_SENSITIVE_SOURCES = {"mark_to_market", "live_position_mark_net"}
CATEGORIES = (
    "AGREE",
    "WAIT_VS_ENTRY",
    "OPPOSITE_DIRECTION",
    "MAIN_WAIT_JEV_ENTRY",
    "BOTH_WAIT_NO_EDGE",
    "INSUFFICIENT_DATA",
    "AUDIT_REJECT",
)
HORIZONS = {
    "1cycle": ("pnl_after_1_cycle", "main_pnl_after_1_cycle", "cycle_seconds", 900.0),
    "4h": ("pnl_after_4h", "main_pnl_after_4h", "horizon_4h_seconds", 14400.0),
}
ENTRY_MODES = {"initial", "scale_in"}


class EvaluationError(RuntimeError):
    """Raised when inputs or CLI arguments cannot produce a valid report."""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalize_action(value: Any) -> str:
    action = str(value or "").strip().upper()
    aliases = {
        "BUY": "BUY_LONG", "LONG": "BUY_LONG",
        "SELL": "SELL_SHORT", "SHORT": "SELL_SHORT",
        "HOLD": "WAIT", "NONE": "WAIT",
    }
    return aliases.get(action, action)


def _raw_action_label(value: Any, default: str = "UNKNOWN") -> str:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().upper()


def _parse_timestamp(value: Any) -> Optional[float]:
    # parse_beijing preserves epoch seconds, normalizes millisecond epochs, and
    # applies the project-wide Beijing contract to naive business timestamps.
    parsed = parse_beijing(value)
    return parsed.timestamp() if parsed is not None else None


def _iso_timestamp(value: float) -> str:
    parsed = parse_beijing(value)
    if parsed is None:
        raise EvaluationError(f"invalid report timestamp: {value}")
    return parsed.isoformat()


def load_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not path.exists():
        raise EvaluationError(f"input file does not exist: {path}")
    rows: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append({"line": line_no, "reason": f"invalid_json: {exc.msg}"})
                continue
            if not isinstance(value, dict):
                issues.append({"line": line_no, "reason": "row_is_not_object"})
                continue
            row = dict(value)
            row["_line_no"] = line_no
            rows.append(row)
    return rows, issues


def flatten_reviews(reviews: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []
    for review in reviews:
        review_ts = (
            _parse_timestamp(review.get("review_started_timestamp"))
            or _parse_timestamp(review.get("timestamp"))
        )
        for instrument in review.get("instrument_reviews") or []:
            if not isinstance(instrument, Mapping):
                continue
            row = dict(instrument)
            row["_semantics_version"] = review.get("question_semantics_version")
            row["_review_timestamp"] = review_ts
            row["_review_line_no"] = review.get("_line_no")
            row.setdefault("cycle_id", review.get("cycle_id"))
            flattened.append(row)
    return flattened


def _unique_index(rows: Iterable[Mapping[str, Any]], key: str) -> Tuple[Dict[str, Dict[str, Any]], List[str], int]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    missing = 0
    for original in rows:
        value = str(original.get(key) or "").strip()
        if not value:
            missing += 1
            continue
        grouped[value].append(dict(original))
    duplicates = sorted(value for value, items in grouped.items() if len(items) > 1)
    index = {value: items[0] for value, items in grouped.items() if len(items) == 1}
    return index, duplicates, missing


def _semantics_label(value: Any) -> str:
    return "legacy" if value is None else str(value)


def _selected_semantics(rows: Sequence[Mapping[str, Any]], requested: str) -> List[Any]:
    versions = {row.get("_semantics_version") for row in rows}
    if requested == "all":
        return sorted(versions, key=lambda value: (-1 if value is None else int(value)))
    if requested == "latest":
        numbered = [int(value) for value in versions if value is not None]
        if numbered:
            return [max(numbered)]
        if None in versions:
            return [None]
        raise EvaluationError("no semantics versions found")
    try:
        wanted = int(requested)
    except ValueError as exc:
        raise EvaluationError("--semantics-version must be latest, all, or an integer") from exc
    if wanted not in versions:
        raise EvaluationError(f"semantics version {wanted} not found")
    return [wanted]


def _entry_mode(review: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    # Legacy rows predate explicit scope and represented entry candidates.
    return str(outcome.get("entry_mode") or review.get("entry_mode") or "initial")


def derive_category(review: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    if _entry_mode(review, outcome) not in ENTRY_MODES:
        return "OUT_OF_SCOPE"
    main = _normalize_action(review.get("main_action") or outcome.get("main_action"))
    jev = _normalize_action(
        review.get("jev_independent_action")
        or review.get("independent_action")
        or review.get("suggested_action")
        or outcome.get("jev_action")
    )
    status = str(review.get("jev_action_status") or outcome.get("jev_action_status") or "")
    data_status = str(review.get("jev_independent_data_status") or "valid")
    audit = str(review.get("jev_audit_verdict") or review.get("audit_verdict") or "").upper()

    if main in ENTRY_ACTIONS and audit == "REJECT":
        return "AUDIT_REJECT"
    if (data_status not in {"valid", "accepted"}
            or jev in {"", "UNKNOWN", "INSUFFICIENT_DATA"}
            or status in WEAK_STATUSES):
        return "INSUFFICIENT_DATA"
    if main in ENTRY_ACTIONS and jev == main and status == "accepted":
        return "AGREE"
    if main in ENTRY_ACTIONS and jev == "WAIT" and status == "no_edge":
        return "WAIT_VS_ENTRY"
    if main in ENTRY_ACTIONS and jev in ENTRY_ACTIONS and jev != main and status == "accepted":
        return "OPPOSITE_DIRECTION"
    if main == "WAIT" and jev in ENTRY_ACTIONS and status == "accepted":
        return "MAIN_WAIT_JEV_ENTRY"
    if main == "WAIT" and jev == "WAIT" and status == "no_edge":
        return "BOTH_WAIT_NO_EDGE"
    return "OUT_OF_SCOPE"


def _expected_relation(category: str) -> Optional[str]:
    return {
        "INSUFFICIENT_DATA": "ABSTAIN",
        "AUDIT_REJECT": "AUDIT_REJECT",
        "AGREE": "AGREE",
        "WAIT_VS_ENTRY": "WAIT_VS_ENTRY",
        "OPPOSITE_DIRECTION": "OPPOSITE_DIRECTION",
        "MAIN_WAIT_JEV_ENTRY": "MAIN_WAIT_JEV_ENTRY",
        "BOTH_WAIT_NO_EDGE": "AGREE",
    }.get(category)


def _cost_signature(outcome: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    raw = outcome.get("cost_assumptions")
    assumptions = dict(raw) if isinstance(raw, Mapping) else {"status": "missing"}
    signature = json.dumps(assumptions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return signature, assumptions


def _percentile(values: Sequence[float], probability: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> Optional[List[float]]:
    if total <= 0:
        return None
    p = wins / total
    denominator = 1.0 + (z * z / total)
    center = (p + z * z / (2.0 * total)) / denominator
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / denominator
    return [max(0.0, center - spread), min(1.0, center + spread)]


def _series_stats(
    series: Sequence[Tuple[float, float]], *,
    drawdown_meaningful: bool = True, drawdown_overlap_factor: float = 1.0,
) -> Optional[Dict[str, Any]]:
    clean = sorted(
        ((timestamp, value) for timestamp, value in series if math.isfinite(value)),
        key=lambda item: item[0],
    )
    if not clean:
        return None
    values = [value for _timestamp, value in clean]
    wins = sum(value > 0 for value in values)
    values_by_timestamp: Dict[float, float] = defaultdict(float)
    for timestamp, value in clean:
        values_by_timestamp[timestamp] += value
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for timestamp in sorted(values_by_timestamp):
        cumulative += values_by_timestamp[timestamp]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    timestamp_collisions = len(values) - len(values_by_timestamp)
    return {
        "n": len(values),
        "sum_net": sum(values),
        "mean_net": statistics.fmean(values),
        "median_net": statistics.median(values),
        "win_rate": wins / len(values),
        "win_rate_wilson_95": _wilson_interval(wins, len(values)),
        "best": max(values),
        "worst": min(values),
        "p05": _percentile(values, 0.05),
        "p10": _percentile(values, 0.10),
        "sample_sequence_max_drawdown": max_drawdown,
        "drawdown_sequence_points": len(values_by_timestamp),
        "drawdown_timestamp_collisions": timestamp_collisions,
        "drawdown_meaningful": drawdown_meaningful,
        "drawdown_overlap_factor": drawdown_overlap_factor,
        "drawdown_interpretation": (
            "approximate_sequence_proxy"
            if drawdown_meaningful else "descriptive_only_high_overlap"
        ),
    }


def _evidence_status(n: int, exploratory_min: int, decision_min: int) -> str:
    if n < exploratory_min:
        return "INSUFFICIENT_EVIDENCE"
    if n < decision_min:
        return "EXPLORATORY_ONLY"
    return "DECISION_EVALUABLE"


def _outcome_timestamp(review: Mapping[str, Any], outcome: Mapping[str, Any]) -> float:
    return (
        _parse_timestamp(outcome.get("review_timestamp"))
        or _parse_timestamp(review.get("_review_timestamp"))
        or 0.0
    )


def _target_timestamp(outcome: Mapping[str, Any], horizon: str) -> Optional[float]:
    jev_field, _main_field, duration_key, duration_default = HORIZONS[horizon]
    explicit = _parse_timestamp(outcome.get(f"{jev_field}_target_at"))
    if explicit is not None:
        return explicit
    started = _parse_timestamp(outcome.get("review_timestamp"))
    assumptions = outcome.get("cost_assumptions")
    duration = None
    if isinstance(assumptions, Mapping):
        duration = _number(assumptions.get(duration_key))
    if started is None:
        return None
    return started + (duration if duration is not None else duration_default)


def _first_value(review: Mapping[str, Any], outcome: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = outcome.get(key)
    if value is not None and value != "":
        return value
    value = review.get(key)
    return default if value is None or value == "" else value


def _confidence_bucket(value: Any) -> str:
    confidence = _number(value)
    if confidence is None:
        return "missing"
    if confidence < 50:
        return "lt_50"
    if confidence < 60:
        return "50_59"
    if confidence < 70:
        return "60_69"
    if confidence < 80:
        return "70_79"
    if confidence < 90:
        return "80_89"
    return "90_100"


def _age_bucket(review: Mapping[str, Any], outcome: Mapping[str, Any], now_ts: float) -> str:
    started = _outcome_timestamp(review, outcome)
    age = max(0.0, now_ts - started)
    assumptions = outcome.get("cost_assumptions")
    assumptions = assumptions if isinstance(assumptions, Mapping) else {}
    cycle = _number(assumptions.get("cycle_seconds")) or 900.0
    horizon = _number(assumptions.get("horizon_4h_seconds")) or 14400.0
    if age < cycle:
        return "before_1cycle"
    if age < horizon:
        return "after_1cycle_before_4h"
    return "after_4h"


def _measurement_availability(outcome: Mapping[str, Any], horizon: str, now_ts: float) -> str:
    jev_field, _main_field, _duration_key, _duration_default = HORIZONS[horizon]
    kind, _value, source = _pnl_value(outcome, jev_field)
    if kind == "measured":
        return "measured"
    if kind == "baseline":
        return "policy_baseline"
    if kind == "invalid":
        return f"invalid_source:{source}"
    target = _target_timestamp(outcome, horizon)
    if target is not None and now_ts < target:
        return "pending_maturity"
    status = str(outcome.get("jev_delayed_entry_status") or "missing_status")
    return f"matured_without_measurement:{status}"


def _population_diagnostics(
    records: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any], str]],
    *, now_ts: float, horizons: Sequence[str],
) -> Dict[str, Any]:
    counts = {
        "category_counts": Counter(),
        "entry_mode_counts": Counter(),
        "jev_delayed_entry_status_counts": Counter(),
        "main_action_counts": Counter(),
        "main_raw_action_counts": Counter(),
        "main_decision_outcome_source_counts": Counter(),
        "main_decision_rejection_code_counts": Counter(),
        "main_raw_confidence_buckets": Counter(),
        "age_buckets": Counter(),
        "out_of_scope_reason_counts": Counter(),
    }
    availability = {horizon: Counter() for horizon in horizons}
    for review, outcome, category in records:
        counts["category_counts"][category] += 1
        counts["entry_mode_counts"][_entry_mode(review, outcome)] += 1
        counts["jev_delayed_entry_status_counts"][str(
            outcome.get("jev_delayed_entry_status") or "missing")] += 1
        counts["main_action_counts"][_normalize_action(
            _first_value(review, outcome, "main_action", "UNKNOWN"))] += 1
        counts["main_raw_action_counts"][_raw_action_label(
            _first_value(review, outcome, "main_raw_action", "UNKNOWN"))] += 1
        counts["main_decision_outcome_source_counts"][str(
            _first_value(review, outcome, "main_decision_outcome_source", "missing"))] += 1
        counts["main_decision_rejection_code_counts"][str(
            _first_value(review, outcome, "main_decision_rejection_code", "none"))] += 1
        raw_confidence = _first_value(
            review, outcome, "main_raw_confidence",
            _first_value(review, outcome, "main_confidence"))
        counts["main_raw_confidence_buckets"][_confidence_bucket(raw_confidence)] += 1
        counts["age_buckets"][_age_bucket(review, outcome, now_ts)] += 1
        if category == "OUT_OF_SCOPE":
            counts["out_of_scope_reason_counts"][
                f"entry_mode:{_entry_mode(review, outcome)}"] += 1
        for horizon in horizons:
            availability[horizon][_measurement_availability(outcome, horizon, now_ts)] += 1
    result = {key: dict(sorted(value.items())) for key, value in counts.items()}
    result["jev_measurement_availability"] = {
        horizon: dict(sorted(values.items())) for horizon, values in availability.items()
    }
    return result


def _pnl_value(outcome: Mapping[str, Any], field: str) -> Tuple[str, Optional[float], str]:
    source = str(outcome.get(f"{field}_source") or "")
    value = _number(outcome.get(field))
    if source in TIME_SENSITIVE_SOURCES:
        lag_key = f"{field}_lag_seconds"
        if lag_key in outcome:
            lag = _number(outcome.get(lag_key))
            if lag is None or lag < 0:
                return "invalid", None, "observation_lag_invalid"
            assumptions = outcome.get("cost_assumptions")
            assumptions = assumptions if isinstance(assumptions, Mapping) else {}
            cycle_seconds = _number(assumptions.get("cycle_seconds")) or 900.0
            if lag > cycle_seconds + 1e-9:
                return "invalid", None, "observation_lag_exceeded"
    if (outcome.get(f"{field}_measurement_status") == "missed_target_window"
            or outcome.get(f"{field}_status") == "missed_target_window"):
        return "invalid", None, "missed_target_window"
    if source == "no_entry_baseline":
        if value is None:
            return "invalid", None, "baseline_missing_value"
        if value != 0.0:
            return "invalid", None, "baseline_nonzero_value"
        return "baseline", value, source
    if source in MEASURED_SOURCES and value is not None:
        return "measured", value, source
    if value is not None and not source:
        return "invalid", None, "missing_source"
    if value is not None and source not in MEASURED_SOURCES:
        return "invalid", None, f"unknown_source:{source}"
    return "missing", None, source or "missing"


def _drawdown_metadata(
    records: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]], horizon: str,
) -> Tuple[bool, float]:
    _jev_field, _main_field, duration_key, duration_default = HORIZONS[horizon]
    assumptions: Mapping[str, Any] = {}
    if records:
        raw = records[0][1].get("cost_assumptions")
        if isinstance(raw, Mapping):
            assumptions = raw
    cycle_seconds = _number(assumptions.get("cycle_seconds")) or 900.0
    duration_seconds = _number(assumptions.get(duration_key)) or duration_default
    overlap_factor = max(1.0, duration_seconds / max(cycle_seconds, 1.0))
    return overlap_factor <= 1.0 + 1e-9, overlap_factor


def _evaluate_bucket(
    records: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    category: str,
    horizon: str,
    now_ts: float,
    exploratory_min: int,
    decision_min: int,
) -> Dict[str, Any]:
    jev_field, main_field, _duration_key, _duration_default = HORIZONS[horizon]
    drawdown_meaningful, drawdown_overlap_factor = _drawdown_metadata(records, horizon)
    main_series: List[Tuple[float, float]] = []
    jev_series: List[Tuple[float, float]] = []
    delta_series: List[Tuple[float, float]] = []
    source_counts: Counter[str] = Counter()
    matured = baseline_rows = baseline_source_rows = excluded = pending = 0
    main_measured = jev_measured = paired = 0

    for review, outcome in records:
        timestamp = _outcome_timestamp(review, outcome)
        target = _target_timestamp(outcome, horizon)
        main_action = _normalize_action(review.get("main_action") or outcome.get("main_action"))
        jev_action = _normalize_action(
            review.get("jev_independent_action")
            or review.get("independent_action")
            or outcome.get("jev_action")
        )
        main_kind, main_value, main_source = _pnl_value(outcome, main_field)
        jev_kind, jev_value, jev_source = _pnl_value(outcome, jev_field)
        source_counts[f"main:{main_source}"] += 1
        source_counts[f"jev:{jev_source}"] += 1
        if main_kind == "baseline" or jev_kind == "baseline":
            baseline_source_rows += 1
        is_mature = (
            main_kind in {"measured", "baseline"}
            or jev_kind in {"measured", "baseline"}
            or (target is not None and now_ts >= target)
        )
        if not is_mature:
            pending += 1
            continue
        matured += 1

        main_policy: Optional[float] = None
        jev_policy: Optional[float] = None
        if (main_action == "WAIT"
                and category == "MAIN_WAIT_JEV_ENTRY"
                and main_kind == "baseline"):
            main_policy = main_value
            baseline_rows += 1
        elif main_action in ENTRY_ACTIONS and main_kind == "measured":
            main_policy = main_value
            main_measured += 1

        if (jev_action == "WAIT"
                and category == "WAIT_VS_ENTRY"
                and jev_kind == "baseline"):
            jev_policy = jev_value
            baseline_rows += 1
        elif (jev_action in ENTRY_ACTIONS
              and category in {"AGREE", "OPPOSITE_DIRECTION", "MAIN_WAIT_JEV_ENTRY"}
              and jev_kind == "measured"):
            jev_policy = jev_value
            jev_measured += 1

        if main_policy is not None:
            main_series.append((timestamp, main_policy))
        if jev_policy is not None:
            jev_series.append((timestamp, jev_policy))
        if main_policy is not None and jev_policy is not None:
            paired += 1
            delta_series.append((timestamp, jev_policy - main_policy))

        if category in {"AGREE", "WAIT_VS_ENTRY", "OPPOSITE_DIRECTION", "MAIN_WAIT_JEV_ENTRY"}:
            if main_policy is None or jev_policy is None:
                excluded += 1
        elif category in {"INSUFFICIENT_DATA", "AUDIT_REJECT"}:
            if main_policy is None:
                excluded += 1

    comparable_categories = {
        "AGREE", "WAIT_VS_ENTRY", "OPPOSITE_DIRECTION", "MAIN_WAIT_JEV_ENTRY"
    }
    primary_n = paired if category in comparable_categories else main_measured
    evidence_status = (
        "DESCRIPTIVE_ONLY" if category == "BOTH_WAIT_NO_EDGE"
        else _evidence_status(primary_n, exploratory_min, decision_min)
    )
    return {
        "n_total": len(records),
        "n_matured": matured,
        "n_pending": pending,
        "n_main_measured": main_measured,
        "n_jev_measured": jev_measured,
        "n_paired": paired,
        "n_measured": primary_n,
        "n_baseline_rows": baseline_source_rows,
        "n_baseline_assignments": baseline_rows,
        "n_excluded_after_maturity": excluded,
        "coverage_rate": (primary_n / len(records)) if records else 0.0,
        "evidence_status": evidence_status,
        "comparison_kind": (
            "no_entry_agreement" if category == "BOTH_WAIT_NO_EDGE"
            else "return_comparison"
        ),
        "drawdown_meaningful": drawdown_meaningful,
        "drawdown_overlap_factor": drawdown_overlap_factor,
        "main_policy_return": _series_stats(
            main_series, drawdown_meaningful=drawdown_meaningful,
            drawdown_overlap_factor=drawdown_overlap_factor),
        "jev_policy_return": _series_stats(
            jev_series, drawdown_meaningful=drawdown_meaningful,
            drawdown_overlap_factor=drawdown_overlap_factor),
        "jev_minus_main": _series_stats(
            delta_series, drawdown_meaningful=drawdown_meaningful,
            drawdown_overlap_factor=drawdown_overlap_factor),
        "source_counts": dict(sorted(source_counts.items())),
    }


def build_report(
    reviews: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    semantics_version: str = "latest",
    since: Optional[float] = None,
    until: Optional[float] = None,
    horizons: Sequence[str] = ("1cycle", "4h"),
    exploratory_min: int = 30,
    decision_min: int = 100,
    now_ts: Optional[float] = None,
    review_parse_issues: Sequence[Mapping[str, Any]] = (),
    outcome_parse_issues: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    if exploratory_min < 1 or decision_min < exploratory_min:
        raise EvaluationError("sample gates require 1 <= exploratory_min <= decision_min")
    unknown_horizons = set(horizons) - set(HORIZONS)
    if unknown_horizons:
        raise EvaluationError(f"unknown horizons: {sorted(unknown_horizons)}")

    flat = flatten_reviews(reviews)
    selected_versions = _selected_semantics(flat, semantics_version)
    selected = [row for row in flat if row.get("_semantics_version") in selected_versions]
    if since is not None:
        selected = [row for row in selected if (row.get("_review_timestamp") or 0) >= since]
    if until is not None:
        selected = [row for row in selected if (row.get("_review_timestamp") or 0) <= until]

    all_review_index, all_review_duplicates, all_review_missing = _unique_index(flat, "decision_id")
    review_index, review_duplicates, review_missing = _unique_index(selected, "decision_id")
    outcome_index, outcome_duplicates, outcome_missing = _unique_index(outcomes, "decision_id")
    duplicate_keys = set(review_duplicates) | set(outcome_duplicates)
    matched: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    identity_mismatches = 0
    for decision_id, review in review_index.items():
        if decision_id in duplicate_keys:
            continue
        outcome = outcome_index.get(decision_id)
        if outcome is None:
            continue
        if (str(review.get("cycle_id") or "") != str(outcome.get("cycle_id") or "")
                or str(review.get("instId") or "") != str(outcome.get("instId") or "")):
            identity_mismatches += 1
            continue
        matched.append((review, outcome))

    now_value = time.time() if now_ts is None else now_ts
    grouped: Dict[Tuple[Any, str], List[Tuple[Dict[str, Any], Dict[str, Any], str]]] = defaultdict(list)
    relation_mismatches = 0
    validated_relation_matches = 0
    missing_recorded_relation = 0
    no_expected_relation = 0
    relation_mismatch_rows: List[Tuple[float, str, str, str]] = []
    unvalidated_relation_rows: List[Tuple[float, str, str]] = []
    population: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for review, outcome in matched:
        category = derive_category(review, outcome)
        population.append((review, outcome, category))
        expected = _expected_relation(category)
        recorded = str(review.get("jev_relation_to_main") or review.get("decision_relation") or "")
        timestamp = _outcome_timestamp(review, outcome)
        decision_id = str(review.get("decision_id") or "")
        if expected is None:
            no_expected_relation += 1
            unvalidated_relation_rows.append((timestamp, decision_id, "no_expected_relation"))
        elif not recorded:
            missing_recorded_relation += 1
            unvalidated_relation_rows.append((timestamp, decision_id, "missing_recorded_relation"))
        elif recorded != expected:
            relation_mismatches += 1
            relation_mismatch_rows.append((timestamp, decision_id, expected, recorded))
        else:
            validated_relation_matches += 1
        if category == "OUT_OF_SCOPE":
            continue
        cost_key, _assumptions = _cost_signature(outcome)
        grouped[(review.get("_semantics_version"), cost_key)].append((review, outcome, category))
    out_of_scope = sum(category == "OUT_OF_SCOPE" for _review, _outcome, category in population)

    cohorts: List[Dict[str, Any]] = []
    for (version, cost_key), items in sorted(
        grouped.items(), key=lambda item: (_semantics_label(item[0][0]), item[0][1])
    ):
        assumptions = json.loads(cost_key)
        category_rows: Dict[str, List[Tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
        for review, outcome, category in items:
            category_rows[category].append((review, outcome))
        categories: Dict[str, Any] = {}
        for category in CATEGORIES:
            records = category_rows.get(category, [])
            categories[category] = {
                horizon: _evaluate_bucket(
                    records, category=category, horizon=horizon, now_ts=now_value,
                    exploratory_min=exploratory_min, decision_min=decision_min,
                )
                for horizon in horizons
            }
        cohorts.append({
            "question_semantics_version": version,
            "semantics_label": _semantics_label(version),
            "cost_assumptions": assumptions,
            "n_records": len(items),
            "diagnostics": _population_diagnostics(
                items, now_ts=now_value, horizons=horizons),
            "categories": categories,
        })

    matched_timeline = sorted(
        (_outcome_timestamp(review, outcome), str(review.get("decision_id") or ""))
        for review, outcome in matched
    )
    matched_timestamps = [timestamp for timestamp, _decision_id in matched_timeline]
    mismatch_timestamps = sorted(row[0] for row in relation_mismatch_rows)
    unvalidated_timeline = sorted(unvalidated_relation_rows)
    unvalidated_timestamps = [row[0] for row in unvalidated_timeline]
    validated_timeline = sorted(
        (_outcome_timestamp(review, outcome), str(review.get("decision_id") or ""))
        for review, outcome in matched
        if _expected_relation(derive_category(review, outcome)) is not None
        and str(review.get("jev_relation_to_main") or review.get("decision_relation") or "")
        and str(review.get("jev_relation_to_main") or review.get("decision_relation") or "")
            == _expected_relation(derive_category(review, outcome))
    )
    validated_timestamps = [timestamp for timestamp, _decision_id in validated_timeline]
    latest_matched = matched_timestamps[-1] if matched_timestamps else None
    first_mismatch = mismatch_timestamps[0] if mismatch_timestamps else None
    last_mismatch = mismatch_timestamps[-1] if mismatch_timestamps else None
    records_after_last_mismatch = (
        sum(timestamp > last_mismatch for timestamp in validated_timestamps)
        if last_mismatch is not None else 0
    )
    unvalidated_after_last_mismatch = (
        sum(timestamp > last_mismatch for timestamp in unvalidated_timestamps)
        if last_mismatch is not None else 0
    )
    mismatch_hours: Counter[str] = Counter()
    for timestamp in mismatch_timestamps:
        parsed = parse_beijing(timestamp)
        if parsed is not None:
            mismatch_hours[parsed.strftime("%Y-%m-%dT%H:00:00+08:00")] += 1
    mismatch_scope = "none"
    if mismatch_timestamps:
        if unvalidated_after_last_mismatch:
            mismatch_scope = "historical_segment_with_unvalidated_tail"
        elif (validated_timestamps
                and last_mismatch is not None
                and last_mismatch < min(validated_timestamps)):
            mismatch_scope = "historical_prefix"
        elif records_after_last_mismatch > 0:
            mismatch_scope = "historical_segment_with_clean_tail"
        else:
            mismatch_scope = "reaches_latest_record"
    relation_mismatch_diagnostics = {
        "count": relation_mismatches,
        "validated_match_count": validated_relation_matches,
        "missing_recorded_relation_count": missing_recorded_relation,
        "no_expected_relation_count": no_expected_relation,
        "unvalidated_relation_count": missing_recorded_relation + no_expected_relation,
        "unvalidated_records_after_last_mismatch": unvalidated_after_last_mismatch,
        "scope": mismatch_scope,
        "first_mismatch_at": (
            _iso_timestamp(first_mismatch) if first_mismatch is not None else None),
        "last_mismatch_at": (
            _iso_timestamp(last_mismatch) if last_mismatch is not None else None),
        "latest_matched_at": (
            _iso_timestamp(latest_matched) if latest_matched is not None else None),
        "records_after_last_mismatch": records_after_last_mismatch,
        "mismatch_counts_by_beijing_hour": dict(sorted(mismatch_hours.items())),
    }

    matched_ids = {review.get("decision_id") for review, _outcome in matched}
    selected_ids = set(review_index)
    return {
        "report_version": REPORT_VERSION,
        "generated_at": _iso_timestamp(now_value),
        "filters": {
            "semantics_version": semantics_version,
            "selected_versions": selected_versions,
            "since": _iso_timestamp(since) if since is not None else None,
            "until": _iso_timestamp(until) if until is not None else None,
            "horizons": list(horizons),
            "exploratory_min": exploratory_min,
            "decision_min": decision_min,
        },
        "data_quality": {
            "review_rows": len(reviews),
            "outcome_rows": len(outcomes),
            "selected_review_candidates": len(selected),
            "matched_records": len(matched),
            "unmatched_selected_reviews": len(selected_ids - matched_ids),
            "outcomes_excluded_by_semantics_filter": len(
                (set(outcome_index) & set(all_review_index)) - selected_ids
            ),
            "outcomes_without_any_review": len(set(outcome_index) - set(all_review_index)),
            "review_parse_errors": len(review_parse_issues),
            "outcome_parse_errors": len(outcome_parse_issues),
            "all_review_missing_decision_id": all_review_missing,
            "review_missing_decision_id": review_missing,
            "outcome_missing_decision_id": outcome_missing,
            "all_review_duplicate_decision_ids": all_review_duplicates,
            "review_duplicate_decision_ids": review_duplicates,
            "outcome_duplicate_decision_ids": outcome_duplicates,
            "identity_mismatches": identity_mismatches,
            "relation_mismatches": relation_mismatches,
            "relation_mismatch_diagnostics": relation_mismatch_diagnostics,
            "out_of_scope_records": out_of_scope,
        },
        "population_diagnostics": _population_diagnostics(
            population, now_ts=now_value, horizons=horizons),
        "unsupported_metrics": {
            "average_r": "outcomes do not contain a uniform risk-unit denominator for JEV paper entries",
            "choice_calibration_error": "current independent Noul scores are not a mutually exclusive Choice probability distribution",
        },
        "cohorts": cohorts,
        "warnings": [
            "no_entry_baseline is excluded from measured-return aggregates and used only as an explicit policy baseline",
            "1cycle sample-sequence drawdown is an approximate sequence proxy when overlap factor is 1x",
            "4h sample-sequence drawdown is descriptive only; default 15-minute cadence creates about 16x overlap",
            "zero candidates or insufficient evidence must not be interpreted as strategy validation",
        ],
    }


def render_summary(report: Mapping[str, Any]) -> str:
    filters = report["filters"]
    quality = report["data_quality"]
    lines = [
        "JEV shadow evaluation (read-only)",
        f"versions={filters['selected_versions']} matched={quality['matched_records']} "
        f"review_parse_errors={quality['review_parse_errors']} outcome_parse_errors={quality['outcome_parse_errors']}",
    ]
    mismatch = quality.get("relation_mismatch_diagnostics", {})
    lines.append(
        f"relation_mismatches={mismatch.get('count', 0)} "
        f"scope={mismatch.get('scope', 'unknown')} "
        f"last={mismatch.get('last_mismatch_at') or '-'} "
        f"validated_matches={mismatch.get('validated_match_count', 0)} "
        f"missing_relation={mismatch.get('missing_recorded_relation_count', 0)} "
        f"unvalidated_tail={mismatch.get('unvalidated_records_after_last_mismatch', 0)} "
        f"newer_records={mismatch.get('records_after_last_mismatch', 0)}")
    population = report.get("population_diagnostics", {})
    lines.append(
        f"entry_modes={population.get('entry_mode_counts', {})} "
        f"paper_status={population.get('jev_delayed_entry_status_counts', {})}")
    lines.append(
        f"main_sources={population.get('main_decision_outcome_source_counts', {})} "
        f"out_of_scope={quality['out_of_scope_records']}")
    for cohort in report["cohorts"]:
        lines.append(
            f"\nsemantics={cohort['semantics_label']} records={cohort['n_records']} "
            f"costs={json.dumps(cohort['cost_assumptions'], ensure_ascii=False, sort_keys=True)}"
        )
        lines.append("category                 horizon total mature baseline measured status                 mean_delta")
        for category in CATEGORIES:
            for horizon, result in cohort["categories"][category].items():
                delta = result.get("jev_minus_main")
                mean_delta = "-" if not delta else f"{delta['mean_net']:.6f}"
                lines.append(
                    f"{category:<24} {horizon:<7} {result['n_total']:>5} "
                    f"{result['n_matured']:>6} {result['n_baseline_rows']:>8} "
                    f"{result['n_measured']:>8} "
                    f"{result['evidence_status']:<22} {mean_delta:>10}"
                )
    lines.append("\nUnsupported: average_r, choice_calibration_error")
    lines.append("Evidence gate: <30 insufficient, 30-99 exploratory, >=100 decision-evaluable")
    return "\n".join(lines)


def _parse_cli_time(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid timestamp: {value}")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only JEV shadow outcome evaluator")
    parser.add_argument("--reviews", default=str(DEFAULT_REVIEWS))
    parser.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    parser.add_argument("--semantics-version", default="latest")
    parser.add_argument("--since", type=_parse_cli_time)
    parser.add_argument("--until", type=_parse_cli_time)
    parser.add_argument("--horizon", choices=("both", "1cycle", "4h"), default="both")
    parser.add_argument("--format", choices=("summary", "json"), default="summary")
    parser.add_argument("--exploratory-min", type=int, default=30)
    parser.add_argument("--decision-min", type=int, default=100)
    args = parser.parse_args(argv)

    try:
        reviews, review_issues = load_jsonl(Path(args.reviews))
        outcomes, outcome_issues = load_jsonl(Path(args.outcomes))
        horizons = ("1cycle", "4h") if args.horizon == "both" else (args.horizon,)
        report = build_report(
            reviews, outcomes, semantics_version=args.semantics_version,
            since=args.since, until=args.until, horizons=horizons,
            exploratory_min=args.exploratory_min, decision_min=args.decision_min,
            review_parse_issues=review_issues, outcome_parse_issues=outcome_issues,
        )
    except EvaluationError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(render_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
