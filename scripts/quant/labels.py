#!/usr/bin/env python3
"""Deterministic point-in-time forward labels for the offline quant lab.

The feature dataset fixes what was knowable before an event session. This tool attaches
retrospective 1/3/5/10-session outcomes from a separate bar source without modifying those
features. Exact market-calendar sessions are required: missing bars never shorten a horizon.

Generic bar archives may produce diagnostics, but only a hash-valid ``total_return`` adjusted
bar view may mark a complete label economically comparable. Nothing here feeds production,
the research factory, strategy promotion, official metrics, or financial execution.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import adjustment_policy  # noqa: E402
import validate_market_day  # noqa: E402
from quant import DIAGNOSTIC_FIELDS  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.labels"
POLICY_SCHEMA = "quant_label_policy/1.0"
DATASET_SCHEMA = "quant_forward_label_dataset/1.0"
ROW_SCHEMA = "quant_forward_label_row/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_label_policy.json"
BENCHMARK = "SPY"
HORIZONS = (1, 3, 5, 10)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_INPUT_BYTES = 128 * 1024 * 1024

SAFETY_FIELDS = {
    **DIAGNOSTIC_FIELDS,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "execution_enabled": False,
    "executed_trade_performance": False,
    "feeds_research_factory": False,
    "feeds_buy_gate": False,
    "promotion_allowed": False,
    "profitability_claim_allowed": False,
    "counts_in_metrics": False,
    "account_data_allowed": False,
    "broker_access": "none",
    "network_invoked": False,
    "model_invoked": False,
}

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "feeds_research_factory": False,
    "feeds_buy_gate": False,
    "promotion_allowed": False,
    "strategy_change_allowed": False,
    "profitability_claim_allowed": False,
    "counts_in_metrics": False,
    "account_data_allowed": False,
    "broker_access": "none",
    "network_invoked": False,
    "model_invoked": False,
}

_POLICY_KEYS = {
    "schema", "version", "dataset_schema", "row_schema", "feature_dataset_schema",
    "adjusted_bar_view_schema", "benchmark", "horizons_sessions", "maximum_rows",
    "maximum_calendar_search_days", "output_decimal_places", "decision_baseline",
    "entry_baseline", "exit_baseline", "session_spine",
    "economic_return_eligible_views", "allow_unverified_archive_diagnostics",
    "require_exact_session_alignment", "require_prior_bar_prefix_binding",
    "ignore_bars_after_as_of", "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "dataset_schema": DATASET_SCHEMA,
    "row_schema": ROW_SCHEMA,
    "feature_dataset_schema": qf.DATASET_SCHEMA,
    "adjusted_bar_view_schema": adjustment_policy.VIEW_SCHEMA,
    "benchmark": BENCHMARK,
    "horizons_sessions": list(HORIZONS),
    "decision_baseline": "previous_session_close",
    "entry_baseline": "event_session_open",
    "exit_baseline": "horizon_session_close",
    "session_spine": "versioned_market_calendar",
    "economic_return_eligible_views": ["total_return"],
    "allow_unverified_archive_diagnostics": True,
    "require_exact_session_alignment": True,
    "require_prior_bar_prefix_binding": True,
    "ignore_bars_after_as_of": True,
}

LABEL_CONTRACT = {
    "feature_boundary": "immutable_feature_rows_with_bars_strictly_before_event_date",
    "session_alignment": "exact_versioned_market_calendar_sessions_no_compression",
    "previous_close_return": "horizon_close_divided_by_previous_session_close_minus_one",
    "event_open_return": "horizon_close_divided_by_event_session_open_minus_one",
    "excess_return": "symbol_return_minus_SPY_return_on_identical_baseline_and_exit",
    "maximum_favorable_excursion": "max_session_high_divided_by_event_open_minus_one",
    "maximum_adverse_excursion": "min_session_low_divided_by_event_open_minus_one",
    "economic_return_eligibility": "validated_total_return_adjusted_bar_view_only",
    "pending_rule": "exit_session_after_as_of_date",
    "missing_data_rule": "incomplete_never_shift_or_compress_horizon",
}
_DATASET_KEYS = {
    *SAFETY_FIELDS.keys(), "tool", "ok", "schema", "row_schema", "as_of_date",
    "benchmark", "horizons_sessions", "policy_version", "policy_sha256",
    "feature_dataset_sha256", "bars_source", "label_contract", "row_count",
    "status_counts", "complete_label_count", "economic_label_count", "rows",
    "rows_payload_sha256", "dataset_sha256",
}
_ROW_KEYS = {
    *SAFETY_FIELDS.keys(), "tool", "schema", "row_id", "feature_row_id", "event_id",
    "symbol", "event_date", "as_of_date", "benchmark", "feature_row_sha256", "status",
    "status_reason", "economic_return_eligible", "session_spine", "baseline", "labels",
    "row_sha256",
}


def _base() -> dict:
    return {**SAFETY_FIELDS, "tool": TOOL}


def _refuse(error: str, **extra) -> dict:
    return {**_base(), "ok": False, "error": error, **extra}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")


def _sha256_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_once(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("output_already_exists")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if not math.isfinite(float(value)):
        return False
    return not positive or value > 0


def _rounded(value: float, places: int) -> float:
    output = round(float(value), places)
    return 0.0 if output == 0 else output


def _within_rounding_tolerance(actual: float, expected: float, places: int) -> bool:
    return abs(float(actual) - float(expected)) <= 2.0 * (10.0 ** -places)


def validate_policy(policy: Any) -> list[str]:
    if not isinstance(policy, dict):
        return ["policy_not_object"]
    errors: list[str] = []
    if set(policy) != _POLICY_KEYS:
        errors.append("policy_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if policy.get(key) != expected:
            errors.append(f"policy_fixed_value_invalid:{key}")
    if not isinstance(policy.get("version"), str) or not _SEMVER_RE.fullmatch(
            str(policy.get("version") or "")):
        errors.append("policy_version_invalid")
    for key, minimum, maximum in (
        ("maximum_rows", 1, 100000),
        ("maximum_calendar_search_days", 10, 366),
        ("output_decimal_places", 4, 12),
    ):
        value = policy.get(key)
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < minimum or value > maximum):
            errors.append(f"policy_integer_invalid:{key}")
    if policy.get("safety") != POLICY_SAFETY:
        errors.append("policy_safety_invalid_or_weakened")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        policy = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"policy_unreadable:{type(exc).__name__}"]
    errors = validate_policy(policy)
    return (None, errors) if errors else (policy, [])


def _bounded_generic_archive(source: dict, as_of: dt.date) -> tuple[dict | None, list[str]]:
    if not isinstance(source, dict) or not isinstance(source.get("bars"), dict):
        return None, ["bars_mapping_missing_or_empty"]
    bounded: dict[str, list[dict]] = {}
    errors: list[str] = []
    for symbol, rows in source["bars"].items():
        if not isinstance(rows, list):
            errors.append(f"bars.{symbol}.rows_not_list")
            continue
        kept = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"bars.{symbol}[{index}].not_object")
                continue
            day = _parse_date(row.get("date"))
            if day is None:
                errors.append(f"bars.{symbol}[{index}].date_invalid")
            elif day <= as_of:
                kept.append(row)
        if kept:
            bounded[symbol] = kept
    archive = {"bars": bounded}
    errors.extend(qf.validate_bars_archive(archive))
    return (None, sorted(set(errors))) if errors else (archive, [])


def _classify_bars(source: Any, as_of: dt.date, file_sha256: str | None) -> tuple[dict | None, list[str]]:
    if not isinstance(source, dict):
        return None, ["bars_source_not_object"]
    if source.get("schema") == adjustment_policy.VIEW_SCHEMA:
        errors = adjustment_policy.validate_view_artifact(source)
        source_as_of = _parse_date(source.get("as_of_date"))
        if source_as_of is not None and as_of > source_as_of:
            errors.append("requested_as_of_after_adjusted_view_as_of")
        if source.get("view") != "total_return":
            errors.append("adjusted_view_not_total_return")
        if source.get("quant_compatible") is not True:
            errors.append("adjusted_view_not_quant_compatible")
        if errors:
            return None, sorted(set(errors))
        bounded, bounded_errors = _bounded_generic_archive({"bars": source["bars"]}, as_of)
        if bounded_errors:
            return None, bounded_errors
        return {
            "bars": bounded["bars"],
            "metadata": {
                "kind": "adjusted_bar_view",
                "schema": adjustment_policy.VIEW_SCHEMA,
                "view": source["view"],
                "source_provider": source["source_provider"],
                "source_sha256": _sha256_obj(bounded),
                "file_sha256": file_sha256,
                "economic_return_eligible": True,
                "bars_after_as_of_ignored": True,
            },
        }, []

    bounded, errors = _bounded_generic_archive(source, as_of)
    if errors:
        return None, errors
    return {
        "bars": bounded["bars"],
        "metadata": {
            "kind": "unverified_archive",
            "schema": source.get("schema") if isinstance(source.get("schema"), str) else None,
            "view": "unverified_archive",
            "source_provider": None,
            "source_sha256": _sha256_obj(bounded),
            "file_sha256": file_sha256,
            "economic_return_eligible": False,
            "bars_after_as_of_ignored": True,
        },
    }, []


def _market_sessions(start: dt.date, count: int, policy: dict) -> tuple[list[str], list[str]]:
    holidays, early = validate_market_day.load_calendar(validate_market_day.CONFIG)
    sessions: list[str] = []
    errors: list[str] = []
    day = start
    for _ in range(policy["maximum_calendar_search_days"] + 1):
        session = validate_market_day.verdict(day, holidays, early)
        if not session["calendar_covers_year"]:
            errors.append(f"market_calendar_year_not_covered:{day.year}")
            break
        if session["is_trading_day"]:
            sessions.append(day.isoformat())
            if len(sessions) == count:
                break
        day += dt.timedelta(days=1)
    if len(sessions) != count:
        errors.append(f"market_calendar_session_spine_incomplete:{len(sessions)}_of_{count}")
    if not sessions or sessions[0] != start.isoformat():
        errors.append(f"event_date_not_trading_session:{start.isoformat()}")
    return sessions, sorted(set(errors))


def _bar_map(rows: list[dict]) -> dict[str, dict]:
    return {row["date"]: row for row in rows}


def _row_payload(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "row_sha256"}


def _dataset_payload(dataset: dict) -> dict:
    return {key: value for key, value in dataset.items() if key != "dataset_sha256"}


def _binding_errors(features: dict, bars: dict[str, list[dict]]) -> list[str]:
    errors: list[str] = []
    benchmark_rows = bars.get(BENCHMARK, [])
    for index, row in enumerate(features.get("rows", [])):
        symbol = row.get("symbol")
        event_date = row.get("event_date")
        if symbol not in bars:
            errors.append(f"row[{index}].symbol_missing_from_label_bars:{symbol}")
            continue
        prior = [bar for bar in bars[symbol] if bar["date"] < event_date]
        benchmark_prior = [bar for bar in benchmark_rows if bar["date"] < event_date]
        lineage = row.get("prior_bar_lineage", {})
        if _sha256_obj(prior) != lineage.get("symbol_bars_sha256"):
            errors.append(f"row[{index}].symbol_prior_bar_prefix_mismatch")
        if _sha256_obj(benchmark_prior) != lineage.get("benchmark_bars_sha256"):
            errors.append(f"row[{index}].benchmark_prior_bar_prefix_mismatch")
        if row.get("usable_bar_cutoff") != (prior[-1]["date"] if prior else None):
            errors.append(f"row[{index}].symbol_prior_cutoff_mismatch")
        if row.get("benchmark_bar_cutoff") != (
                benchmark_prior[-1]["date"] if benchmark_prior else None):
            errors.append(f"row[{index}].benchmark_prior_cutoff_mismatch")
    return sorted(set(errors))


def _empty_label(horizon: int, exit_date: str, status: str, reason: str) -> dict:
    return {
        "horizon_sessions": horizon,
        "exit_session_date": exit_date,
        "status": status,
        "reason": reason,
        "metrics": None,
        "economic_return_eligible": False,
    }


def _label_metrics(symbol_rows: list[dict], benchmark_rows: list[dict],
                   symbol_previous_close: float, benchmark_previous_close: float,
                   places: int) -> dict:
    symbol_open = float(symbol_rows[0]["open"])
    benchmark_open = float(benchmark_rows[0]["open"])
    symbol_exit = float(symbol_rows[-1]["close"])
    benchmark_exit = float(benchmark_rows[-1]["close"])
    symbol_previous_return = symbol_exit / symbol_previous_close - 1.0
    benchmark_previous_return = benchmark_exit / benchmark_previous_close - 1.0
    symbol_open_return = symbol_exit / symbol_open - 1.0
    benchmark_open_return = benchmark_exit / benchmark_open - 1.0
    return {
        "symbol_previous_close_return": _rounded(symbol_previous_return, places),
        "benchmark_previous_close_return": _rounded(benchmark_previous_return, places),
        "previous_close_excess_return": _rounded(
            symbol_previous_return - benchmark_previous_return, places),
        "symbol_event_open_return": _rounded(symbol_open_return, places),
        "benchmark_event_open_return": _rounded(benchmark_open_return, places),
        "event_open_excess_return": _rounded(
            symbol_open_return - benchmark_open_return, places),
        "maximum_favorable_excursion_from_event_open": _rounded(
            max(float(row["high"]) for row in symbol_rows) / symbol_open - 1.0, places),
        "maximum_adverse_excursion_from_event_open": _rounded(
            min(float(row["low"]) for row in symbol_rows) / symbol_open - 1.0, places),
    }


def _build_row(feature_row: dict, bars: dict[str, list[dict]], as_of: dt.date,
               economic_source: bool, policy: dict) -> tuple[dict | None, list[str]]:
    event_date = dt.date.fromisoformat(feature_row["event_date"])
    spine, calendar_errors = _market_sessions(event_date, max(HORIZONS), policy)
    if calendar_errors:
        return None, calendar_errors

    symbol_map = _bar_map(bars[feature_row["symbol"]])
    benchmark_map = _bar_map(bars[BENCHMARK])
    previous_date = feature_row.get("usable_bar_cutoff")
    benchmark_previous_date = feature_row.get("benchmark_bar_cutoff")
    previous_bar = symbol_map.get(previous_date) if previous_date else None
    benchmark_previous_bar = benchmark_map.get(benchmark_previous_date) if benchmark_previous_date else None
    event_symbol = symbol_map.get(feature_row["event_date"])
    event_benchmark = benchmark_map.get(feature_row["event_date"])
    baseline = {
        "previous_session_date": previous_date,
        "event_session_date": feature_row["event_date"],
        "symbol_previous_close": previous_bar.get("close") if previous_bar else None,
        "benchmark_previous_close": (
            benchmark_previous_bar.get("close") if benchmark_previous_bar else None),
        "symbol_event_open": event_symbol.get("open") if event_symbol else None,
        "benchmark_event_open": event_benchmark.get("open") if event_benchmark else None,
    }

    labels: dict[str, dict] = {}
    for horizon in HORIZONS:
        exit_date = spine[horizon - 1]
        key = str(horizon)
        if not feature_row.get("eligible_for_modeling"):
            labels[key] = _empty_label(
                horizon, exit_date, "excluded", "feature_row_not_eligible_for_modeling")
            continue
        if dt.date.fromisoformat(exit_date) > as_of:
            labels[key] = _empty_label(
                horizon, exit_date, "pending", "horizon_not_observable_as_of")
            continue
        expected = spine[:horizon]
        missing_symbol = [day for day in expected if day not in symbol_map]
        missing_benchmark = [day for day in expected if day not in benchmark_map]
        baseline_missing = [name for name, value in baseline.items()
                            if name != "event_session_date" and value is None]
        if missing_symbol or missing_benchmark or baseline_missing:
            reasons = []
            if missing_symbol:
                reasons.append("missing_symbol_sessions:" + ",".join(missing_symbol))
            if missing_benchmark:
                reasons.append("missing_benchmark_sessions:" + ",".join(missing_benchmark))
            if baseline_missing:
                reasons.append("missing_baseline_fields:" + ",".join(baseline_missing))
            labels[key] = _empty_label(horizon, exit_date, "incomplete", ";".join(reasons))
            continue
        symbol_window = [symbol_map[day] for day in expected]
        benchmark_window = [benchmark_map[day] for day in expected]
        labels[key] = {
            "horizon_sessions": horizon,
            "exit_session_date": exit_date,
            "status": "complete",
            "reason": None,
            "metrics": _label_metrics(
                symbol_window, benchmark_window, float(baseline["symbol_previous_close"]),
                float(baseline["benchmark_previous_close"]),
                policy["output_decimal_places"]),
            "economic_return_eligible": economic_source,
        }

    statuses = {label["status"] for label in labels.values()}
    if statuses == {"excluded"}:
        status, reason = "excluded", "feature_row_not_eligible_for_modeling"
    elif "incomplete" in statuses:
        status, reason = "incomplete", "one_or_more_observable_horizons_incomplete"
    elif "pending" in statuses:
        status, reason = "pending", "one_or_more_horizons_not_observable_as_of"
    else:
        status, reason = "complete", None

    row = {
        **_base(),
        "schema": ROW_SCHEMA,
        "row_id": f"{feature_row['row_id']}:labels:{as_of.isoformat()}",
        "feature_row_id": feature_row["row_id"],
        "event_id": feature_row["event_id"],
        "symbol": feature_row["symbol"],
        "event_date": feature_row["event_date"],
        "as_of_date": as_of.isoformat(),
        "benchmark": BENCHMARK,
        "feature_row_sha256": feature_row["row_sha256"],
        "status": status,
        "status_reason": reason,
        "economic_return_eligible": (
            economic_source and any(label["economic_return_eligible"] for label in labels.values())),
        "session_spine": spine,
        "baseline": baseline,
        "labels": labels,
    }
    row["row_sha256"] = _sha256_obj(_row_payload(row))
    return row, []


def build_dataset(features: Any, bars_source: Any, *, as_of_date: str,
                  policy: dict | None = None, bars_file_sha256: str | None = None) -> dict:
    """Build a hash-bound label dataset or a fail-closed diagnostic refusal."""
    active_policy = policy
    if active_policy is None:
        active_policy, policy_errors = load_policy()
        if policy_errors:
            return _refuse("invalid_label_policy", errors=policy_errors)
    else:
        policy_errors = validate_policy(active_policy)
        if policy_errors:
            return _refuse("invalid_label_policy", errors=policy_errors)
    as_of = _parse_date(as_of_date)
    if as_of is None:
        return _refuse("as_of_date_invalid")
    if bars_file_sha256 is not None and not _SHA_RE.fullmatch(bars_file_sha256):
        return _refuse("bars_file_sha256_invalid")
    feature_errors = qf.validate_dataset(features)
    if feature_errors:
        return _refuse("feature_dataset_invalid", errors=feature_errors)
    if len(features["rows"]) > active_policy["maximum_rows"]:
        return _refuse("maximum_rows_exceeded", row_count=len(features["rows"]),
                       maximum_rows=active_policy["maximum_rows"])
    future_events = [row["event_date"] for row in features["rows"]
                     if row["event_date"] > as_of.isoformat()]
    if future_events:
        return _refuse("as_of_precedes_feature_event", event_dates=sorted(set(future_events)))

    classified, bars_errors = _classify_bars(bars_source, as_of, bars_file_sha256)
    if bars_errors:
        return _refuse("bars_source_invalid", errors=bars_errors)
    bars = classified["bars"]
    binding_errors = _binding_errors(features, bars)
    if binding_errors:
        return _refuse("feature_prior_bar_binding_failed", errors=binding_errors)

    rows: list[dict] = []
    calendar_errors: list[str] = []
    for feature_row in features["rows"]:
        row, errors = _build_row(
            feature_row, bars, as_of,
            classified["metadata"]["economic_return_eligible"], active_policy)
        if errors:
            calendar_errors.extend(f"{feature_row['row_id']}:{error}" for error in errors)
        elif row is not None:
            rows.append(row)
    if calendar_errors:
        return _refuse("market_calendar_invalid", errors=sorted(set(calendar_errors)))

    status_counts = {
        status: sum(1 for row in rows if row["status"] == status)
        for status in ("complete", "pending", "incomplete", "excluded")
    }
    labels = [label for row in rows for label in row["labels"].values()]
    dataset = {
        **_base(),
        "ok": True,
        "schema": DATASET_SCHEMA,
        "row_schema": ROW_SCHEMA,
        "as_of_date": as_of.isoformat(),
        "benchmark": BENCHMARK,
        "horizons_sessions": list(HORIZONS),
        "policy_version": active_policy["version"],
        "policy_sha256": _sha256_obj(active_policy),
        "feature_dataset_sha256": features["dataset_sha256"],
        "bars_source": classified["metadata"],
        "label_contract": dict(LABEL_CONTRACT),
        "row_count": len(rows),
        "status_counts": status_counts,
        "complete_label_count": sum(1 for label in labels if label["status"] == "complete"),
        "economic_label_count": sum(
            1 for label in labels if label["economic_return_eligible"]),
        "rows": rows,
        "rows_payload_sha256": _sha256_obj(rows),
    }
    dataset["dataset_sha256"] = _sha256_obj(_dataset_payload(dataset))
    validation_errors = validate_dataset(dataset, active_policy)
    if validation_errors:
        return _refuse("internal_dataset_validation_failed", errors=validation_errors)
    return dataset


def _safety_errors(document: dict, prefix: str) -> list[str]:
    return [f"{prefix}.safety_field_incorrect:{key}" for key, expected in SAFETY_FIELDS.items()
            if document.get(key) != expected]


def validate_dataset(document: Any, policy: dict | None = None) -> list[str]:
    """Validate safety fields, temporal semantics, counts, and the complete hash chain."""
    if not isinstance(document, dict):
        return ["dataset_not_object"]
    errors = _safety_errors(document, "dataset")
    if set(document) != _DATASET_KEYS:
        errors.append("dataset_keys_invalid")
    if document.get("ok") is not True:
        errors.append("dataset_ok_must_be_true")
    if document.get("tool") != TOOL:
        errors.append("dataset_tool_mismatch")
    if document.get("schema") != DATASET_SCHEMA:
        errors.append("dataset_schema_mismatch")
    if document.get("row_schema") != ROW_SCHEMA:
        errors.append("row_schema_mismatch")
    as_of = _parse_date(document.get("as_of_date"))
    if as_of is None:
        errors.append("as_of_date_invalid")
    if document.get("benchmark") != BENCHMARK:
        errors.append("benchmark_mismatch")
    if document.get("horizons_sessions") != list(HORIZONS):
        errors.append("horizons_mismatch")
    if document.get("label_contract") != LABEL_CONTRACT:
        errors.append("label_contract_invalid_or_weakened")
    active_policy = policy
    if active_policy is None:
        active_policy, policy_errors = load_policy()
        errors.extend(policy_errors)
    else:
        errors.extend(validate_policy(active_policy))
    if active_policy is not None:
        if document.get("policy_version") != active_policy["version"]:
            errors.append("policy_version_mismatch")
        if document.get("policy_sha256") != _sha256_obj(active_policy):
            errors.append("policy_sha256_mismatch")
    if not isinstance(document.get("feature_dataset_sha256"), str) or not _SHA_RE.fullmatch(
            str(document.get("feature_dataset_sha256") or "")):
        errors.append("feature_dataset_sha256_invalid")
    source = document.get("bars_source")
    source_economic = False
    if not isinstance(source, dict):
        errors.append("bars_source_invalid")
    else:
        expected_keys = {
            "kind", "schema", "view", "source_provider", "source_sha256", "file_sha256",
            "economic_return_eligible", "bars_after_as_of_ignored",
        }
        if set(source) != expected_keys:
            errors.append("bars_source_keys_invalid")
        if source.get("kind") not in {"adjusted_bar_view", "unverified_archive"}:
            errors.append("bars_source_kind_invalid")
        if not isinstance(source.get("source_sha256"), str) or not _SHA_RE.fullmatch(
                str(source.get("source_sha256") or "")):
            errors.append("bars_source_sha256_invalid")
        file_sha = source.get("file_sha256")
        if file_sha is not None and (not isinstance(file_sha, str) or not _SHA_RE.fullmatch(file_sha)):
            errors.append("bars_file_sha256_invalid")
        source_economic = (
            source.get("kind") == "adjusted_bar_view"
            and source.get("schema") == adjustment_policy.VIEW_SCHEMA
            and source.get("view") == "total_return"
            and source.get("economic_return_eligible") is True
        )
        if source.get("economic_return_eligible") is not source_economic:
            errors.append("bars_source_economic_eligibility_invalid")
        if source.get("bars_after_as_of_ignored") is not True:
            errors.append("bars_after_as_of_rule_weakened")

    rows = document.get("rows")
    if not isinstance(rows, list):
        return sorted(set(errors + ["rows_not_list"]))
    if document.get("row_count") != len(rows):
        errors.append("row_count_mismatch")
    if active_policy is not None and len(rows) > active_policy["maximum_rows"]:
        errors.append("maximum_rows_exceeded")
    status_counts = {
        status: sum(1 for row in rows if isinstance(row, dict) and row.get("status") == status)
        for status in ("complete", "pending", "incomplete", "excluded")
    }
    if document.get("status_counts") != status_counts:
        errors.append("status_counts_mismatch")

    all_labels: list[dict] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"row[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix}.not_object")
            continue
        errors.extend(_safety_errors(row, prefix))
        if set(row) != _ROW_KEYS:
            errors.append(f"{prefix}.keys_invalid")
        if row.get("schema") != ROW_SCHEMA:
            errors.append(f"{prefix}.schema_mismatch")
        if row.get("tool") != TOOL:
            errors.append(f"{prefix}.tool_mismatch")
        row_id = row.get("row_id")
        feature_row_id = row.get("feature_row_id")
        if not isinstance(row_id, str) or not row_id:
            errors.append(f"{prefix}.row_id_invalid")
        elif row_id in seen_ids:
            errors.append(f"{prefix}.row_id_duplicate")
        else:
            seen_ids.add(row_id)
        if (not isinstance(feature_row_id, str) or not feature_row_id
                or (as_of is not None
                    and row_id != f"{feature_row_id}:labels:{as_of.isoformat()}")):
            errors.append(f"{prefix}.row_id_not_derived_from_feature")
        if as_of is not None and row.get("as_of_date") != as_of.isoformat():
            errors.append(f"{prefix}.as_of_mismatch")
        if row.get("benchmark") != BENCHMARK:
            errors.append(f"{prefix}.benchmark_mismatch")
        event_date = _parse_date(row.get("event_date"))
        if event_date is None or (as_of is not None and event_date > as_of):
            errors.append(f"{prefix}.event_date_invalid_or_after_as_of")
        feature_sha = row.get("feature_row_sha256")
        if not isinstance(feature_sha, str) or not _SHA_RE.fullmatch(feature_sha):
            errors.append(f"{prefix}.feature_row_sha256_invalid")
        spine = row.get("session_spine")
        if (not isinstance(spine, list) or len(spine) != max(HORIZONS)
                or any(_parse_date(day) is None for day in spine)
                or spine != sorted(spine) or len(set(spine)) != len(spine)):
            errors.append(f"{prefix}.session_spine_invalid")
            spine = []
        if spine and event_date is not None and active_policy is not None:
            expected_spine, calendar_errors = _market_sessions(
                event_date, max(HORIZONS), active_policy)
            if calendar_errors or spine != expected_spine:
                errors.append(f"{prefix}.session_spine_not_market_calendar_exact")
        baseline = row.get("baseline")
        baseline_keys = {
            "previous_session_date", "event_session_date", "symbol_previous_close",
            "benchmark_previous_close", "symbol_event_open", "benchmark_event_open",
        }
        if not isinstance(baseline, dict) or set(baseline) != baseline_keys:
            errors.append(f"{prefix}.baseline_invalid")
        else:
            previous = _parse_date(baseline.get("previous_session_date"))
            if (event_date is not None and previous is not None and previous >= event_date):
                errors.append(f"{prefix}.baseline_previous_date_not_before_event")
            if baseline.get("event_session_date") != row.get("event_date"):
                errors.append(f"{prefix}.baseline_event_date_mismatch")
            for name in (
                    "symbol_previous_close", "benchmark_previous_close", "symbol_event_open",
                    "benchmark_event_open"):
                value = baseline.get(name)
                if value is not None and not _finite_number(value, positive=True):
                    errors.append(f"{prefix}.baseline_value_invalid:{name}")
        labels = row.get("labels")
        if not isinstance(labels, dict) or set(labels) != {str(value) for value in HORIZONS}:
            errors.append(f"{prefix}.labels_invalid")
            labels = {}
        statuses = set()
        row_economic = False
        for horizon in HORIZONS:
            label = labels.get(str(horizon))
            label_prefix = f"{prefix}.label[{horizon}]"
            if not isinstance(label, dict):
                errors.append(f"{label_prefix}.not_object")
                continue
            if set(label) != {
                    "horizon_sessions", "exit_session_date", "status", "reason", "metrics",
                    "economic_return_eligible"}:
                errors.append(f"{label_prefix}.keys_invalid")
            all_labels.append(label)
            status = label.get("status")
            statuses.add(status)
            if label.get("horizon_sessions") != horizon:
                errors.append(f"{label_prefix}.horizon_mismatch")
            if spine and label.get("exit_session_date") != spine[horizon - 1]:
                errors.append(f"{label_prefix}.exit_date_mismatch")
            if status not in {"complete", "pending", "incomplete", "excluded"}:
                errors.append(f"{label_prefix}.status_invalid")
            metrics = label.get("metrics")
            if status == "complete":
                expected_metric_keys = {
                    "symbol_previous_close_return", "benchmark_previous_close_return",
                    "previous_close_excess_return", "symbol_event_open_return",
                    "benchmark_event_open_return", "event_open_excess_return",
                    "maximum_favorable_excursion_from_event_open",
                    "maximum_adverse_excursion_from_event_open",
                }
                if (not isinstance(metrics, dict) or set(metrics) != expected_metric_keys
                        or not all(_finite_number(value) for value in metrics.values())):
                    errors.append(f"{label_prefix}.metrics_invalid")
                if label.get("reason") is not None:
                    errors.append(f"{label_prefix}.complete_reason_not_null")
                exit_day = _parse_date(label.get("exit_session_date"))
                if as_of is not None and (exit_day is None or exit_day > as_of):
                    errors.append(f"{label_prefix}.complete_after_as_of")
                if isinstance(metrics, dict) and set(metrics) == expected_metric_keys:
                    places = active_policy["output_decimal_places"]
                    if not _within_rounding_tolerance(
                            metrics["previous_close_excess_return"],
                            metrics["symbol_previous_close_return"]
                            - metrics["benchmark_previous_close_return"], places):
                        errors.append(f"{label_prefix}.previous_close_excess_identity_invalid")
                    if not _within_rounding_tolerance(
                            metrics["event_open_excess_return"],
                            metrics["symbol_event_open_return"]
                            - metrics["benchmark_event_open_return"], places):
                        errors.append(f"{label_prefix}.event_open_excess_identity_invalid")
                    if metrics["maximum_favorable_excursion_from_event_open"] < 0:
                        errors.append(f"{label_prefix}.mfe_direction_invalid")
                    if metrics["maximum_adverse_excursion_from_event_open"] > 0:
                        errors.append(f"{label_prefix}.mae_direction_invalid")
            else:
                if metrics is not None or not isinstance(label.get("reason"), str):
                    errors.append(f"{label_prefix}.noncomplete_payload_invalid")
                exit_day = _parse_date(label.get("exit_session_date"))
                if (status == "pending" and as_of is not None
                        and (exit_day is None or exit_day <= as_of)):
                    errors.append(f"{label_prefix}.pending_not_after_as_of")
                if (status == "incomplete" and as_of is not None
                        and (exit_day is None or exit_day > as_of)):
                    errors.append(f"{label_prefix}.incomplete_after_as_of")
            label_economic = label.get("economic_return_eligible")
            expected_economic = status == "complete" and source_economic
            if label_economic is not expected_economic:
                errors.append(f"{label_prefix}.economic_eligibility_invalid")
            row_economic = row_economic or expected_economic
        expected_status = (
            "excluded" if statuses == {"excluded"}
            else "incomplete" if "incomplete" in statuses
            else "pending" if "pending" in statuses
            else "complete" if statuses == {"complete"}
            else None
        )
        if row.get("status") != expected_status:
            errors.append(f"{prefix}.status_invalid")
        expected_reason = {
            "excluded": "feature_row_not_eligible_for_modeling",
            "incomplete": "one_or_more_observable_horizons_incomplete",
            "pending": "one_or_more_horizons_not_observable_as_of",
            "complete": None,
        }.get(expected_status)
        if row.get("status_reason") != expected_reason:
            errors.append(f"{prefix}.status_reason_invalid")
        if row.get("economic_return_eligible") is not row_economic:
            errors.append(f"{prefix}.economic_eligibility_invalid")
        if row.get("row_sha256") != _sha256_obj(_row_payload(row)):
            errors.append(f"{prefix}.row_sha256_mismatch")

    if document.get("complete_label_count") != sum(
            1 for label in all_labels if label.get("status") == "complete"):
        errors.append("complete_label_count_mismatch")
    if document.get("economic_label_count") != sum(
            1 for label in all_labels if label.get("economic_return_eligible") is True):
        errors.append("economic_label_count_mismatch")
    if document.get("rows_payload_sha256") != _sha256_obj(rows):
        errors.append("rows_payload_sha256_mismatch")
    if document.get("dataset_sha256") != _sha256_obj(_dataset_payload(document)):
        errors.append("dataset_sha256_mismatch")
    return sorted(set(errors))


def verify_dataset(document: Any, features: Any, bars_source: Any, *, as_of_date: str,
                   policy: dict | None = None, bars_file_sha256: str | None = None) -> dict:
    errors = validate_dataset(document, policy)
    if errors:
        return _refuse("artifact_invalid", errors=errors)
    rebuilt = build_dataset(
        features, bars_source, as_of_date=as_of_date, policy=policy,
        bars_file_sha256=bars_file_sha256)
    if not rebuilt.get("ok"):
        return _refuse("rebuild_failed", detail=rebuilt)
    if rebuilt != document:
        return _refuse("artifact_rederivation_mismatch",
                       expected_dataset_sha256=rebuilt.get("dataset_sha256"),
                       actual_dataset_sha256=document.get("dataset_sha256"))
    return {**_base(), "ok": True, "verified": True,
            "dataset_sha256": document["dataset_sha256"]}


def _print(document: dict) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(POLICY_DEFAULT))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")

    build = subparsers.add_parser("build")
    build.add_argument("--features", required=True)
    build.add_argument("--bars", required=True)
    build.add_argument("--as-of", required=True)
    build.add_argument("--out", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--artifact", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--features", required=True)
    verify.add_argument("--bars", required=True)
    verify.add_argument("--as-of", required=True)

    args = parser.parse_args(argv)
    policy, policy_errors = load_policy(Path(args.policy))
    if policy_errors:
        _print(_refuse("invalid_label_policy", errors=policy_errors))
        return 2
    if args.command == "check":
        _print({**_base(), "ok": True, "policy_valid": True,
                "policy_version": policy["version"], "policy_sha256": _sha256_obj(policy)})
        return 0
    try:
        if args.command == "validate":
            artifact = _read_json(Path(args.artifact))
            errors = validate_dataset(artifact, policy)
            result = ({**_base(), "ok": True, "valid": True,
                       "dataset_sha256": artifact.get("dataset_sha256")}
                      if not errors else _refuse("artifact_invalid", errors=errors))
        else:
            features = _read_json(Path(args.features))
            bars_path = Path(args.bars)
            bars = _read_json(bars_path)
            bars_file_sha = _sha256_file(bars_path)
            if args.command == "build":
                result = build_dataset(
                    features, bars, as_of_date=args.as_of, policy=policy,
                    bars_file_sha256=bars_file_sha)
                if result.get("ok"):
                    _write_once(Path(args.out), result)
                    result = {**_base(), "ok": True, "written": str(Path(args.out)),
                              "dataset_sha256": result["dataset_sha256"]}
            else:
                artifact = _read_json(Path(args.artifact))
                result = verify_dataset(
                    artifact, features, bars, as_of_date=args.as_of, policy=policy,
                    bars_file_sha256=bars_file_sha)
    except (OSError, ValueError, json.JSONDecodeError, FileExistsError) as exc:
        result = _refuse("io_or_input_failure", detail=str(exc)[:400])
    _print(result)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
