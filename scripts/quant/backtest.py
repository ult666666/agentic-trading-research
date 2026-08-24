#!/usr/bin/env python3
"""Conservative manual-entry simulator for the offline quant research lab.

This module consumes a validated point-in-time feature dataset, daily bars, a frozen plan
document, and an experiment manifest.  It simulates hypothetical price paths only.  It never
creates an order intent, never calls a broker, and never treats a touch or modeled fill as an
actual execution.

Supported long-only entry models:
* ``next_open``: event-session open plus configured adverse slippage.
* ``limit_touch``: daily low touched a frozen buy limit; fill remains explicitly unguaranteed.
* ``post_open_validation``: an exogenous timestamped trigger print, with same-day exits omitted
  because daily bars cannot establish sequence after the trigger.

Every experiment must supply a valid ``quant.experiment_manifest`` that fingerprints the bars,
features, and plans and records the total number of trials.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import experiment_manifest as em  # noqa: E402
from quant import feature_corpus as qfc  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.backtest"
PLAN_SCHEMA = "quant_backtest_plan/1.0"
RESULT_SCHEMA = "quant_backtest_result/1.0"
RECORD_SCHEMA = "quant_backtest_record/1.0"
ENTRY_MODELS = ("next_open", "limit_touch", "post_open_validation")
INSTRUMENT_TYPES = ("common_stock", "unleveraged_etf")
STATUSES = ("completed", "no_fill", "excluded", "pending")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)


def _base() -> dict:
    return {**qf.SAFETY_FIELDS, "tool": TOOL}


def _refuse(error: str, **extra) -> dict:
    return {**_base(), "ok": False, "error": error, **extra}


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256_obj(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _number(value, *, positive=False, nonnegative=False) -> bool:
    if not isinstance(value, _NUM) or isinstance(value, bool):
        return False
    if not math.isfinite(float(value)):
        return False
    if positive and value <= 0:
        return False
    if nonnegative and value < 0:
        return False
    return True


def _round(value) -> float | None:
    if value is None:
        return None
    out = round(float(value), 10)
    return 0.0 if out == 0 else out


def _record_payload(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "record_sha256"}


def _result_payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "result_sha256"}


def _event_rows(features: dict) -> dict[str, dict]:
    return {row["event_id"]: row for row in features.get("rows", []) if isinstance(row, dict)}


def _feature_input_sha256(features: dict) -> str | None:
    if features.get("schema") == qfc.SCHEMA:
        return features.get("corpus_sha256")
    return features.get("dataset_sha256")


def _feature_input_errors(features: dict) -> list[str]:
    if isinstance(features, dict) and features.get("schema") == qfc.SCHEMA:
        errors = qfc.validate_corpus(features)
        if features.get("status") != "reviewable":
            errors.append("feature_corpus_not_reviewable")
        return errors
    return qf.validate_dataset(features)


def _market_bounds_utc(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    open_local = dt.datetime.combine(day, dt.time(9, 30), tzinfo=qf.MARKET_TZ)
    close_local = dt.datetime.combine(day, dt.time(16, 0), tzinfo=qf.MARKET_TZ)
    return (open_local.astimezone(dt.timezone.utc),
            close_local.astimezone(dt.timezone.utc))


def validate_plans(plans, features: dict) -> list[str]:
    """Validate plans against immutable feature rows and their prior-close cutoff."""
    errors: list[str] = []
    if not isinstance(plans, dict):
        return ["plans_not_object"]
    if plans.get("schema") != PLAN_SCHEMA:
        errors.append("plans_schema_mismatch")
    if plans.get("feature_dataset_sha256") != _feature_input_sha256(features):
        errors.append("plans_feature_dataset_sha256_mismatch")
    if not isinstance(plans.get("variant_id"), str) or not _ID_RE.fullmatch(
            plans.get("variant_id", "")):
        errors.append("plans_variant_id_invalid")
    scenarios = plans.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return errors + ["scenarios_missing_or_empty"]
    rows = _event_rows(features)
    seen: set[str] = set()
    for i, scenario in enumerate(scenarios):
        prefix = f"scenario[{i}]"
        if not isinstance(scenario, dict):
            errors.append(f"{prefix}.not_object")
            continue
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str) or not _ID_RE.fullmatch(scenario_id):
            errors.append(f"{prefix}.scenario_id_invalid")
        elif scenario_id in seen:
            errors.append(f"{prefix}.scenario_id_duplicate:{scenario_id}")
        else:
            seen.add(scenario_id)
        event_id = scenario.get("event_id")
        row = rows.get(event_id)
        if row is None:
            errors.append(f"{prefix}.event_id_not_in_feature_dataset")
            continue
        for field in ("symbol", "event_date", "feature_row_sha256"):
            expected_field = "row_sha256" if field == "feature_row_sha256" else field
            if scenario.get(field) != row.get(expected_field):
                errors.append(f"{prefix}.{field}_mismatch")
        if scenario.get("direction") != "long":
            errors.append(f"{prefix}.direction_must_be_long")
        if scenario.get("instrument_type") not in INSTRUMENT_TYPES:
            errors.append(f"{prefix}.instrument_type_prohibited_or_unknown")
        model = scenario.get("entry_model")
        if model not in ENTRY_MODELS:
            errors.append(f"{prefix}.entry_model_invalid")
        specified = qf._parse_utc(scenario.get("specified_at_utc"))
        cutoff = qf._parse_utc(row.get("knowledge_cutoff_utc"))
        if specified is None:
            errors.append(f"{prefix}.specified_at_utc_invalid")
        elif cutoff is not None and specified > cutoff:
            errors.append(f"{prefix}.plan_specified_after_feature_cutoff")

        for field in ("max_entry", "stop_price", "target_price"):
            if not _number(scenario.get(field), positive=True):
                errors.append(f"{prefix}.{field}_invalid")
        max_entry, stop, target = (scenario.get("max_entry"), scenario.get("stop_price"),
                                   scenario.get("target_price"))
        if all(_number(value, positive=True) for value in (max_entry, stop, target)):
            if not stop < max_entry < target:
                errors.append(f"{prefix}.planned_levels_not_stop_lt_max_entry_lt_target")
        hold = scenario.get("max_hold_sessions")
        if not isinstance(hold, int) or isinstance(hold, bool) or not 1 <= hold <= 10:
            errors.append(f"{prefix}.max_hold_sessions_invalid")
        for field in ("entry_slippage_bps", "exit_slippage_bps",
                      "stop_slippage_bps", "transaction_cost_bps_per_side"):
            if not _number(scenario.get(field), nonnegative=True) or scenario.get(field) > 500:
                errors.append(f"{prefix}.{field}_invalid")

        if model == "limit_touch":
            if not _number(scenario.get("limit_price"), positive=True):
                errors.append(f"{prefix}.limit_price_required")
            elif _number(max_entry, positive=True) and scenario["limit_price"] > max_entry:
                errors.append(f"{prefix}.limit_price_above_max_entry")
            if scenario.get("entry_slippage_bps") != 0:
                errors.append(f"{prefix}.limit_touch_entry_slippage_must_be_zero")
        if model == "post_open_validation":
            if not _number(scenario.get("trigger_price"), positive=True):
                errors.append(f"{prefix}.trigger_price_required")
            if not isinstance(scenario.get("trigger_source"), str) or not scenario[
                    "trigger_source"].strip():
                errors.append(f"{prefix}.trigger_source_required")
            trigger_hash = scenario.get("trigger_artifact_sha256")
            if not isinstance(trigger_hash, str) or not _SHA_RE.fullmatch(trigger_hash):
                errors.append(f"{prefix}.trigger_artifact_sha256_required")
            trigger_time = qf._parse_utc(scenario.get("trigger_observed_at_utc"))
            event_day = qf._parse_date(row.get("event_date"))
            if trigger_time is None or event_day is None:
                errors.append(f"{prefix}.trigger_observed_at_utc_invalid")
            else:
                market_open, market_close = _market_bounds_utc(event_day)
                if not market_open <= trigger_time <= market_close:
                    errors.append(f"{prefix}.trigger_time_outside_regular_session")
    return errors


def _manifest_binding_errors(manifest, source_hashes: dict, plans: dict) -> list[str]:
    errors = [f"manifest:{value}" for value in em.validate_manifest(manifest)]
    if errors:
        return errors
    available = {row.get("sha256") for row in manifest["data_snapshots"]}
    for key in ("features_file_sha256", "bars_file_sha256", "plans_file_sha256"):
        if source_hashes.get(key) not in available:
            errors.append(f"manifest_missing_bound_snapshot:{key}")
    for scenario in plans.get("scenarios", []):
        if (scenario.get("entry_model") == "post_open_validation"
                and scenario.get("trigger_artifact_sha256") not in available):
            errors.append(
                f"manifest_missing_trigger_snapshot:{scenario.get('scenario_id', 'unknown')}"
            )
    return errors


def _prior_bar_binding_errors(features: dict, archive: dict) -> list[str]:
    """Allow future outcome bars while proving every feature-time bar stayed unchanged."""
    errors: list[str] = []
    bars = archive.get("bars", {})
    benchmark_rows = bars.get(qf.BENCHMARK, [])
    for row in features.get("rows", []):
        event_id = row.get("event_id", "unknown")
        event_date = row.get("event_date")
        symbol = row.get("symbol")
        lineage = row.get("prior_bar_lineage") or {}
        symbol_prior = [bar for bar in bars.get(symbol, [])
                        if isinstance(bar, dict) and bar.get("date", "") < event_date]
        benchmark_prior = [bar for bar in benchmark_rows
                           if isinstance(bar, dict) and bar.get("date", "") < event_date]
        if _sha256_obj(symbol_prior) != lineage.get("symbol_bars_sha256"):
            errors.append(f"prior_symbol_bars_changed_or_missing:{event_id}")
        if _sha256_obj(benchmark_prior) != lineage.get("benchmark_bars_sha256"):
            errors.append(f"prior_benchmark_bars_changed_or_missing:{event_id}")
    return errors


def _common_record(scenario: dict) -> dict:
    return {
        **_base(),
        "schema": RECORD_SCHEMA,
        "scenario_id": scenario["scenario_id"],
        "event_id": scenario["event_id"],
        "symbol": scenario["symbol"],
        "event_date": scenario["event_date"],
        "entry_model": scenario["entry_model"],
        "instrument_type": scenario["instrument_type"],
        "direction": "long",
        "simulation_status": None,
        "hypothetical_simulation": True,
        "hypothetical_fill_assumed": False,
        "fill_guaranteed": False,
        "actual_order_exists": False,
        "order_submitted": False,
        "entry_price": None,
        "entry_date": None,
        "exit_price": None,
        "exit_date": None,
        "exit_reason": None,
        "gross_return": None,
        "net_return_after_modeled_costs": None,
        "return_r": None,
        "mfe_pct": None,
        "mae_pct": None,
        "benchmark_return": None,
        "duration_sessions": None,
        "gap_through_stop": False,
        "same_bar_ambiguity": False,
        "same_day_outcome_omitted": False,
        "exclusion_reasons": [],
        "limitations": [
            "daily_bars_do_not_prove_intraday_sequence_or_fill",
            "modeled_costs_are_not_observed_execution_costs",
            "retrospective_diagnostic_not_live_performance",
        ],
    }


def _entry_assumption(scenario: dict, event_bar: dict) -> tuple[float | None, str | None, str]:
    model = scenario["entry_model"]
    entry_slippage = float(scenario["entry_slippage_bps"]) / 10000.0
    if model == "next_open":
        return (float(event_bar["open"]) * (1.0 + entry_slippage), None,
                "event_open_plus_adverse_slippage")
    if model == "limit_touch":
        limit_price = float(scenario["limit_price"])
        if float(event_bar["low"]) > limit_price:
            return None, "buy_limit_not_touched", "daily_low_touch_not_guaranteed_fill"
        # A buy limit above the entire daily range could fill at the open, but never at an
        # impossible price above the observed high.  Use the less favorable observed bound.
        return (min(limit_price, float(event_bar["high"])), None,
                "daily_low_touched_frozen_limit_fill_not_guaranteed")
    trigger = float(scenario["trigger_price"])
    if not float(event_bar["low"]) <= trigger <= float(event_bar["high"]):
        return None, "trigger_price_outside_daily_range", "external_trigger_not_corroborated_by_bar"
    return (trigger * (1.0 + entry_slippage), None,
            "timestamped_external_trigger_plus_adverse_slippage_fill_not_guaranteed")


def _simulate_scenario(scenario: dict, feature_row: dict, symbol_rows: list[dict],
                       benchmark_rows: list[dict]) -> dict:
    record = _common_record(scenario)
    if feature_row.get("eligible_for_modeling") is not True:
        record["simulation_status"] = "excluded"
        record["exclusion_reasons"] = ["feature_row_ineligible"] + list(
            feature_row.get("exclusion_reasons", []))
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record

    dates = [row["date"] for row in symbol_rows]
    event_date = scenario["event_date"]
    if event_date not in dates:
        record["simulation_status"] = "excluded"
        record["exclusion_reasons"] = ["event_date_not_in_symbol_bars"]
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record
    event_index = dates.index(event_date)
    event_bar = symbol_rows[event_index]
    entry, no_fill_reason, fill_assumption = _entry_assumption(scenario, event_bar)
    record["fill_assumption"] = fill_assumption
    if no_fill_reason:
        record["simulation_status"] = "no_fill"
        record["exclusion_reasons"] = [no_fill_reason]
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record
    entry = float(entry)
    if entry > float(scenario["max_entry"]):
        record["simulation_status"] = "no_fill"
        record["exclusion_reasons"] = ["modeled_entry_above_frozen_max_entry"]
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record
    stop, target = float(scenario["stop_price"]), float(scenario["target_price"])
    if not stop < entry < target:
        record["simulation_status"] = "excluded"
        record["exclusion_reasons"] = ["realized_levels_not_stop_lt_entry_lt_target"]
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record

    record.update({
        "hypothetical_fill_assumed": True,
        "entry_price": _round(entry),
        "entry_date": event_date,
        "stop_price": stop,
        "target_price": target,
        "entry_slippage_bps": scenario["entry_slippage_bps"],
        "exit_slippage_bps": scenario["exit_slippage_bps"],
        "stop_slippage_bps": scenario["stop_slippage_bps"],
        "transaction_cost_bps_per_side": scenario["transaction_cost_bps_per_side"],
        "max_hold_sessions": scenario["max_hold_sessions"],
    })

    start_index = event_index
    if scenario["entry_model"] == "post_open_validation":
        start_index += 1
        record["same_day_outcome_omitted"] = True
        record["trigger_provenance"] = {
            "source": scenario["trigger_source"],
            "artifact_sha256": scenario["trigger_artifact_sha256"],
            "observed_at_utc": scenario["trigger_observed_at_utc"],
        }
        record["limitations"].append("event_day_high_low_precedes_or_follows_trigger_unknown")
    available = symbol_rows[start_index:start_index + scenario["max_hold_sessions"]]
    if not available:
        record["simulation_status"] = "pending"
        record["exclusion_reasons"] = ["no_post_entry_outcome_bars"]
        record["record_sha256"] = _sha256_obj(_record_payload(record))
        return record

    stop_slippage = float(scenario["stop_slippage_bps"]) / 10000.0
    exit_slippage = float(scenario["exit_slippage_bps"]) / 10000.0
    exit_price = exit_date = exit_reason = None
    mfe = -math.inf
    mae = math.inf
    duration = 0
    for duration, bar in enumerate(available, start=1):
        open_price = float(bar["open"])
        high, low = float(bar["high"]), float(bar["low"])
        mfe = max(mfe, high / entry - 1.0)
        mae = min(mae, low / entry - 1.0)
        if open_price <= stop:
            exit_price = open_price * (1.0 - stop_slippage)
            exit_date, exit_reason = bar["date"], "gap_through_stop"
            record["gap_through_stop"] = True
            break
        stop_touched, target_touched = low <= stop, high >= target
        if stop_touched:
            exit_price = stop * (1.0 - stop_slippage)
            exit_date = bar["date"]
            if target_touched:
                exit_reason = "stop_first_same_bar_ambiguity"
                record["same_bar_ambiguity"] = True
            else:
                exit_reason = "stop"
            break
        if open_price >= target or target_touched:
            exit_price = target * (1.0 - exit_slippage)
            exit_date, exit_reason = bar["date"], "target"
            break

    if exit_price is None:
        if len(available) < scenario["max_hold_sessions"]:
            record["simulation_status"] = "pending"
            record["exclusion_reasons"] = ["insufficient_future_bars_for_time_exit"]
            record["mfe_pct"], record["mae_pct"] = _round(mfe), _round(mae)
            record["record_sha256"] = _sha256_obj(_record_payload(record))
            return record
        last = available[-1]
        exit_price = float(last["close"]) * (1.0 - exit_slippage)
        exit_date, exit_reason = last["date"], "time_exit"

    gross = exit_price / entry - 1.0
    costs = 2.0 * float(scenario["transaction_cost_bps_per_side"]) / 10000.0
    net = gross - costs
    risk = entry - stop
    benchmark_by_date = {row["date"]: row for row in benchmark_rows}
    benchmark_event, benchmark_exit = benchmark_by_date.get(event_date), benchmark_by_date.get(exit_date)
    benchmark_return = None
    if benchmark_event is not None and benchmark_exit is not None:
        benchmark_return = (float(benchmark_exit["close"]) /
                            float(benchmark_event["open"]) - 1.0)
    record.update({
        "simulation_status": "completed",
        "exit_price": _round(exit_price),
        "exit_date": exit_date,
        "exit_reason": exit_reason,
        "gross_return": _round(gross),
        "net_return_after_modeled_costs": _round(net),
        "return_r": _round((exit_price - entry - costs * entry) / risk),
        "mfe_pct": _round(mfe),
        "mae_pct": _round(mae),
        "benchmark_return": _round(benchmark_return),
        "duration_sessions": duration,
    })
    record["record_sha256"] = _sha256_obj(_record_payload(record))
    return record


def _summary(records: list[dict]) -> dict:
    return {
        "scenario_count": len(records),
        "completed_count": sum(row["simulation_status"] == "completed" for row in records),
        "no_fill_count": sum(row["simulation_status"] == "no_fill" for row in records),
        "excluded_count": sum(row["simulation_status"] == "excluded" for row in records),
        "pending_count": sum(row["simulation_status"] == "pending" for row in records),
        "gap_through_stop_count": sum(row["gap_through_stop"] for row in records),
        "same_bar_ambiguity_count": sum(row["same_bar_ambiguity"] for row in records),
        "actual_orders_count": 0,
    }


def run_backtest(features, archive, plans, manifest, source_hashes: dict) -> dict:
    """Run all frozen scenarios or return a diagnostic refusal."""
    feature_errors = _feature_input_errors(features)
    if feature_errors:
        return _refuse("invalid_feature_input", errors=feature_errors)
    bar_errors = qf.validate_bars_archive(archive)
    if bar_errors:
        return _refuse("invalid_bars_archive", errors=bar_errors)
    prior_bar_errors = _prior_bar_binding_errors(features, archive)
    if prior_bar_errors:
        return _refuse("feature_and_backtest_prior_bar_mismatch",
                       errors=prior_bar_errors)
    plan_errors = validate_plans(plans, features)
    if plan_errors:
        return _refuse("invalid_backtest_plans", errors=plan_errors)
    if not isinstance(source_hashes, dict) or any(
            not isinstance(source_hashes.get(key), str)
            or not _SHA_RE.fullmatch(source_hashes[key])
            for key in ("features_file_sha256", "bars_file_sha256", "plans_file_sha256")):
        return _refuse("invalid_source_hashes")
    manifest_errors = _manifest_binding_errors(manifest, source_hashes, plans)
    if manifest_errors:
        return _refuse("invalid_or_unbound_experiment_manifest", errors=manifest_errors)

    feature_rows = _event_rows(features)
    bars = archive["bars"]
    records = [
        _simulate_scenario(scenario, feature_rows[scenario["event_id"]],
                           bars[scenario["symbol"]], bars[qf.BENCHMARK])
        for scenario in sorted(plans["scenarios"], key=lambda item: item["scenario_id"])
    ]
    summary = _summary(records)
    result = {
        **_base(),
        "ok": True,
        "schema": RESULT_SCHEMA,
        "record_schema": RECORD_SCHEMA,
        "experiment_id": manifest["experiment_id"],
        "variant_id": plans["variant_id"],
        "n_trials": manifest["n_trials"],
        "lineage": {
            **source_hashes,
            "feature_dataset_sha256": _feature_input_sha256(features),
            "feature_input_schema": features.get("schema"),
            "feature_prior_bar_contract": "exact_prefix_with_future_outcome_extension_allowed",
            "plans_payload_sha256": _sha256_obj(plans),
            "bars_payload_sha256": _sha256_obj(archive),
            "experiment_manifest_sha256": _sha256_obj(manifest),
        },
        "entry_models": list(ENTRY_MODELS),
        "conservative_rules": [
            "long_only_allowed_instruments",
            "frozen_plan_timestamp_not_after_feature_cutoff",
            "max_entry_never_chased",
            "limit_touch_fill_not_guaranteed",
            "same_bar_stop_and_target_assumes_stop_first",
            "gap_through_stop_uses_gap_open_minus_stop_slippage",
            "post_open_trigger_omits_same_day_outcome_without_intraday_sequence",
            "incomplete_hold_window_remains_pending",
        ],
        "summary": summary,
        "records": records,
        "records_sha256": _sha256_obj(records),
    }
    result["result_sha256"] = _sha256_obj(_result_payload(result))
    errors = validate_result(result)
    if errors:
        return _refuse("internal_backtest_validation_failed", errors=errors)
    return result


def subset_result(result: dict, scenario_ids: list[str], subset_label: str) -> dict:
    """Create a hash-valid child result bound to one validated parent result."""
    errors = validate_result(result)
    if errors:
        return _refuse("invalid_parent_backtest_result", errors=errors)
    if (not isinstance(scenario_ids, list) or not scenario_ids
            or any(not isinstance(value, str) or not _ID_RE.fullmatch(value)
                   for value in scenario_ids)
            or len(set(scenario_ids)) != len(scenario_ids)):
        return _refuse("subset_scenario_ids_invalid")
    if not isinstance(subset_label, str) or not _ID_RE.fullmatch(subset_label):
        return _refuse("subset_label_invalid")
    by_id = {record["scenario_id"]: record for record in result["records"]}
    missing = sorted(set(scenario_ids) - set(by_id))
    if missing:
        return _refuse("subset_scenario_ids_missing", missing=missing)
    records = [copy.deepcopy(by_id[value]) for value in scenario_ids]
    child = copy.deepcopy(result)
    child["lineage"]["parent_result_sha256"] = result["result_sha256"]
    child["subset_label"] = subset_label
    child["records"] = records
    child["records_sha256"] = _sha256_obj(records)
    child["summary"] = _summary(records)
    child["result_sha256"] = _sha256_obj(_result_payload(child))
    final_errors = validate_result(child)
    if final_errors:
        return _refuse("internal_subset_validation_failed", errors=final_errors)
    return child


def _safety_errors(value: dict, prefix: str) -> list[str]:
    return [f"{prefix}.safety_field_incorrect:{key}" for key, expected in qf.SAFETY_FIELDS.items()
            if value.get(key) != expected]


def validate_result(result) -> list[str]:
    if not isinstance(result, dict):
        return ["result_not_object"]
    errors = _safety_errors(result, "result")
    if result.get("ok") is not True:
        errors.append("result_ok_must_be_true")
    if result.get("schema") != RESULT_SCHEMA:
        errors.append("result_schema_mismatch")
    if result.get("record_schema") != RECORD_SCHEMA:
        errors.append("record_schema_mismatch")
    if not isinstance(result.get("variant_id"), str) or not _ID_RE.fullmatch(
            result.get("variant_id", "")):
        errors.append("variant_id_invalid")
    if not isinstance(result.get("n_trials"), int) or result["n_trials"] < 1:
        errors.append("n_trials_invalid")
    lineage = result.get("lineage")
    if not isinstance(lineage, dict):
        errors.append("lineage_missing")
    else:
        for key, value in lineage.items():
            if key.endswith("sha256") and (not isinstance(value, str) or not _SHA_RE.fullmatch(value)):
                errors.append(f"lineage_hash_invalid:{key}")
    records = result.get("records")
    if not isinstance(records, list):
        return errors + ["records_not_list"]
    seen: set[str] = set()
    for i, record in enumerate(records):
        prefix = f"record[{i}]"
        if not isinstance(record, dict):
            errors.append(f"{prefix}.not_object")
            continue
        errors.extend(_safety_errors(record, prefix))
        if record.get("schema") != RECORD_SCHEMA:
            errors.append(f"{prefix}.schema_mismatch")
        scenario_id = record.get("scenario_id")
        if not isinstance(scenario_id, str) or not _ID_RE.fullmatch(scenario_id):
            errors.append(f"{prefix}.scenario_id_invalid")
        elif scenario_id in seen:
            errors.append(f"{prefix}.scenario_id_duplicate")
        else:
            seen.add(scenario_id)
        if record.get("simulation_status") not in STATUSES:
            errors.append(f"{prefix}.status_invalid")
        for key, expected in (("hypothetical_simulation", True), ("fill_guaranteed", False),
                              ("actual_order_exists", False), ("order_submitted", False)):
            if record.get(key) is not expected:
                errors.append(f"{prefix}.{key}_incorrect")
        completed = record.get("simulation_status") == "completed"
        if (record.get("entry_model") == "post_open_validation"
                and record.get("hypothetical_fill_assumed") is True):
            provenance = record.get("trigger_provenance")
            if not isinstance(provenance, dict):
                errors.append(f"{prefix}.trigger_provenance_missing")
            else:
                if not isinstance(provenance.get("source"), str) or not provenance["source"].strip():
                    errors.append(f"{prefix}.trigger_provenance_source_invalid")
                digest = provenance.get("artifact_sha256")
                if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
                    errors.append(f"{prefix}.trigger_provenance_hash_invalid")
                if qf._parse_utc(provenance.get("observed_at_utc")) is None:
                    errors.append(f"{prefix}.trigger_provenance_time_invalid")
        if completed:
            if record.get("hypothetical_fill_assumed") is not True:
                errors.append(f"{prefix}.completed_without_hypothetical_fill")
            for key in ("entry_price", "exit_price", "gross_return",
                        "net_return_after_modeled_costs", "return_r", "mfe_pct", "mae_pct"):
                if not _number(record.get(key)):
                    errors.append(f"{prefix}.{key}_missing_or_invalid")
        elif record.get("net_return_after_modeled_costs") is not None:
            errors.append(f"{prefix}.noncompleted_has_return")
        if record.get("record_sha256") != _sha256_obj(_record_payload(record)):
            errors.append(f"{prefix}.record_sha256_mismatch")
    summary = result.get("summary")
    if not isinstance(summary, dict) or summary.get("scenario_count") != len(records):
        errors.append("summary_missing_or_count_mismatch")
    else:
        expected_counts = {
            "completed_count": sum(r.get("simulation_status") == "completed" for r in records),
            "no_fill_count": sum(r.get("simulation_status") == "no_fill" for r in records),
            "excluded_count": sum(r.get("simulation_status") == "excluded" for r in records),
            "pending_count": sum(r.get("simulation_status") == "pending" for r in records),
            "gap_through_stop_count": sum(r.get("gap_through_stop") is True for r in records),
            "same_bar_ambiguity_count": sum(r.get("same_bar_ambiguity") is True for r in records),
        }
        for key, expected in expected_counts.items():
            if summary.get(key) != expected:
                errors.append(f"summary_count_mismatch:{key}")
        if summary.get("actual_orders_count") != 0:
            errors.append("summary_claims_actual_orders")
    if result.get("records_sha256") != _sha256_obj(records):
        errors.append("records_sha256_mismatch")
    if result.get("result_sha256") != _sha256_obj(_result_payload(result)):
        errors.append("result_sha256_mismatch")
    return errors


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "file_missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"file_unreadable_or_malformed:{type(exc).__name__}"


def _atomic_write(path: Path, value: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_quant_backtest_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return None


def _print(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Conservative diagnostic backtest; no orders.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--features", required=True)
    run.add_argument("--bars", required=True)
    run.add_argument("--plans", required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--out", default="-")
    run.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    if args.command == "validate":
        path = Path(args.path)
        doc, problem = _read(path)
        if problem:
            _print(_refuse("result_read_failed", detail=problem, path=str(path)))
            return 1
        errors = validate_result(doc)
        _print({**_base(), "ok": not errors, "errors": errors, "path": str(path),
                "result_sha256": doc.get("result_sha256")})
        return 0 if not errors else 1

    paths = {name: Path(getattr(args, name)) for name in ("features", "bars", "plans", "manifest")}
    docs, problems = {}, {}
    for name, path in paths.items():
        docs[name], problem = _read(path)
        if problem:
            problems[name] = problem
    if problems:
        _print(_refuse("input_read_failed", errors=problems))
        return 1
    source_hashes = {
        "features_file_sha256": _sha256_file(paths["features"]),
        "bars_file_sha256": _sha256_file(paths["bars"]),
        "plans_file_sha256": _sha256_file(paths["plans"]),
    }
    result = run_backtest(docs["features"], docs["bars"], docs["plans"],
                          docs["manifest"], source_hashes)
    if result.get("ok") is not True:
        _print(result)
        return 1
    if args.out == "-":
        _print(result)
        return 0
    out = Path(args.out)
    try:
        problem = _atomic_write(out, result, args.replace)
    except OSError as exc:
        _print(_refuse("output_write_failed", detail=type(exc).__name__, path=str(out)))
        return 1
    if problem:
        _print(_refuse(problem, path=str(out)))
        return 1
    _print({**_base(), "ok": True, "written": str(out),
            "result_sha256": result["result_sha256"], "summary": result["summary"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
