#!/usr/bin/env python3
"""Unattended, research-only quant experiment supervisor.

The factory turns the existing point-in-time lake and offline quant lab into one
deterministic state machine.  It validates the daily lake readiness receipt,
discovers pre-registered experiment bundles, independently validates every
referenced artifact and hash, applies conservative out-of-sample/holdout/cost/
selection-bias gates, and writes one self-hashed health artifact.

Its strongest possible conclusion is ``candidate_for_category_D_governance_review``.
It has no strategy activation or execution authority and imports no broker surface.
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import artifact_provenance as provenance  # noqa: E402
import data_lake_intake  # noqa: E402
from quant import DIAGNOSTIC_FIELDS  # noqa: E402
from quant import backtest as qb  # noqa: E402
from quant import experiment_manifest as em  # noqa: E402
from quant import feature_corpus as fc  # noqa: E402
from quant import features as qf  # noqa: E402
from quant import model_diagnostics as qmd  # noqa: E402
from quant import selection_bias as sb  # noqa: E402
from quant import walk_forward as wf  # noqa: E402

TOOL = "quant.research_factory"
CONFIG_SCHEMA = "quant_research_factory_config/1.0"
CONFIG_VERSION = "1.1.0"
BUNDLE_SCHEMA = "quant_research_bundle/1.0"
ARTIFACT_SCHEMA = "quant_research_factory/1.0"
CONFIG_DEFAULT = ROOT / "config" / "research_factory.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)

FACTORY_SAFETY = {
    **qf.SAFETY_FIELDS,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
}

EXPECTED_CONFIG_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "strategy_change_allowed": False,
    "feeds_production": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "allow_dynamic_commands": False,
    "allow_network": False,
}

READINESS_ACTIONS = {
    "required_dataset_missing:daily_bars": "ingest_point_in_time_adjusted_daily_bars",
    "required_dataset_missing:security_master": "populate_symbol_interval_security_master",
    "required_dataset_missing:corporate_actions": "ingest_point_in_time_corporate_actions",
    "required_dataset_missing:issuer_filings": "ingest_primary_source_issuer_filings",
    "corporate_action_coverage_missing": "verify_corporate_action_coverage",
    "daily_bar_adjustment_semantics_missing": "lock_bar_adjustment_semantics",
    "delisted_security_coverage_missing": "add_delisted_security_coverage",
    "macro_release_time_modeling_blocked": "capture_macro_publication_timestamps",
}


def _base() -> dict:
    return {**FACTORY_SAFETY, "tool": TOOL}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _artifact_payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "artifact_sha256"}


def _bundle_payload(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "bundle_sha256"}


def _parse_utc(value: Any) -> dt.datetime | None:
    return provenance.parse_utc_strict(value)


def _number(value: Any) -> bool:
    return (isinstance(value, _NUM) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _cartesian_count(grid: dict) -> int | None:
    if not isinstance(grid, dict) or not grid:
        return None
    total = 1
    for values in grid.values():
        if not isinstance(values, list) or not values:
            return None
        total *= len(values)
    return total


def validate_config(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return ["config_not_object"]
    errors: list[str] = []
    if config.get("schema") != CONFIG_SCHEMA:
        errors.append("config_schema_mismatch")
    if config.get("version") != CONFIG_VERSION:
        errors.append("config_version_mismatch")
    if config.get("operating_timezone") != "America/Los_Angeles":
        errors.append("operating_timezone_invalid")
    safety = config.get("safety")
    if not isinstance(safety, dict):
        errors.append("safety_missing")
    else:
        for key, expected in EXPECTED_CONFIG_SAFETY.items():
            if safety.get(key) != expected:
                errors.append(f"safety_field_incorrect:{key}")
    paths = config.get("paths")
    if not isinstance(paths, dict):
        errors.append("paths_missing")
    else:
        for key in ("readiness_root", "experiment_root", "session_feature_root",
                    "feature_corpus", "output_root"):
            if not _safe_relative(paths.get(key)):
                errors.append(f"path_invalid:{key}")
    scheduler = config.get("scheduler")
    if not isinstance(scheduler, dict) or scheduler.get("workflow") != "post_close":
        errors.append("scheduler_invalid")
    elif any(scheduler.get(key) is not True for key in (
            "fail_soft_for_narrative", "one_artifact_per_target",
            "replace_current_health_artifact")):
        errors.append("scheduler_safety_invalid")
    gates = config.get("evidence_gates")
    required_ints = ("minimum_completed_oos", "minimum_holdout_completed",
                     "minimum_walk_forward_folds")
    required_numbers = ("minimum_positive_fold_fraction", "minimum_fold_expectancy",
                        "minimum_holdout_net_expectancy",
                        "minimum_stressed_net_expectancy",
                        "minimum_holdout_profit_factor",
                        "maximum_holdout_drawdown_abs",
                        "minimum_deflated_sharpe_probability",
                        "maximum_probability_backtest_overfitting",
                        "minimum_cost_stress_multiplier")
    if not isinstance(gates, dict):
        errors.append("evidence_gates_missing")
    else:
        for key in required_ints:
            if not isinstance(gates.get(key), int) or isinstance(gates.get(key), bool) or (
                    gates[key] < 1):
                errors.append(f"gate_invalid:{key}")
        for key in required_numbers:
            if not _number(gates.get(key)):
                errors.append(f"gate_invalid:{key}")
        for key in ("require_complete_trial_matrix",
                    "require_locked_chronological_holdout",
                    "require_selection_bias_diagnostics",
                    "require_all_artifact_hashes"):
            if gates.get(key) is not True:
                errors.append(f"gate_must_be_true:{key}")
        for key in ("minimum_positive_fold_fraction",
                    "minimum_deflated_sharpe_probability",
                    "maximum_probability_backtest_overfitting"):
            if _number(gates.get(key)) and not 0 <= float(gates[key]) <= 1:
                errors.append(f"gate_out_of_range:{key}")
        if _number(gates.get("minimum_cost_stress_multiplier")) and (
                float(gates["minimum_cost_stress_multiplier"]) < 1):
            errors.append("gate_out_of_range:minimum_cost_stress_multiplier")
    programs = config.get("programs")
    if not isinstance(programs, list) or not programs:
        errors.append("programs_missing")
    else:
        seen: set[str] = set()
        for index, program in enumerate(programs):
            prefix = f"program[{index}]"
            if not isinstance(program, dict):
                errors.append(f"{prefix}.not_object")
                continue
            program_id = program.get("program_id")
            if not isinstance(program_id, str) or not _ID_RE.fullmatch(program_id):
                errors.append(f"{prefix}.id_invalid")
            elif program_id in seen:
                errors.append(f"{prefix}.id_duplicate")
            else:
                seen.add(program_id)
            if program.get("status") != "preregistered_data_waiting":
                errors.append(f"{prefix}.status_invalid")
            if program.get("decision_clock") != "prior_close":
                errors.append(f"{prefix}.decision_clock_invalid")
            if program.get("direction") != "long":
                errors.append(f"{prefix}.direction_invalid")
            if program.get("entry_model") not in qb.ENTRY_MODELS:
                errors.append(f"{prefix}.entry_model_invalid")
            if program.get("instrument_types") != ["common_stock", "unleveraged_etf"]:
                errors.append(f"{prefix}.instrument_types_invalid")
            count = _cartesian_count(program.get("parameter_grid"))
            if count is None or program.get("expected_trial_count") != count:
                errors.append(f"{prefix}.trial_count_mismatch")
            holdout = program.get("holdout_policy")
            if not isinstance(holdout, dict) or any(holdout.get(key) is not True for key in (
                    "locked_before_variant_selection",
                    "never_reused_for_parameter_selection")):
                errors.append(f"{prefix}.holdout_policy_invalid")
            elif (not isinstance(holdout.get("holdout_calendar_days"), int)
                  or holdout["holdout_calendar_days"] < 1
                  or not isinstance(holdout.get("minimum_completed_observations"), int)
                  or holdout["minimum_completed_observations"] < 1):
                errors.append(f"{prefix}.holdout_policy_invalid")
            assumptions = program.get("simulation_assumptions")
            if not isinstance(assumptions, dict) or any(
                    not isinstance(assumptions.get(key), int)
                    or isinstance(assumptions.get(key), bool)
                    or not 0 <= assumptions[key] <= 500
                    for key in ("entry_slippage_bps", "exit_slippage_bps",
                                "stop_slippage_bps", "base_transaction_cost_bps_per_side")):
                errors.append(f"{prefix}.simulation_assumptions_invalid")
            costs = program.get("modeled_costs_bps_per_side")
            if (not isinstance(costs, list) or len(costs) < 2
                    or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                           for value in costs)
                    or costs != sorted(set(costs))
                    or (isinstance(assumptions, dict)
                        and assumptions.get("base_transaction_cost_bps_per_side") != costs[0])):
                errors.append(f"{prefix}.modeled_costs_invalid")
            selection = program.get("selection_policy")
            if (not isinstance(selection, dict)
                    or selection.get("objective") !=
                    "sharpe_per_observation_not_annualized_then_mean_return"
                    or selection.get("tie_break") !=
                    "lexicographically_smallest_variant_id"
                    or selection.get("final_variant_rule") !=
                    "plurality_of_fold_selections_then_mean_fold_test_return"
                    or not isinstance(selection.get("train_shortlist_size"), int)
                    or not 1 <= selection["train_shortlist_size"] <= program.get(
                        "expected_trial_count", 0)):
                errors.append(f"{prefix}.selection_policy_invalid")
            bias = program.get("selection_bias_policy")
            if (not isinstance(bias, dict)
                    or not isinstance(bias.get("cscv_slices"), int)
                    or bias["cscv_slices"] < 4 or bias["cscv_slices"] % 2
                    or bias.get("remainder_policy") !=
                    "drop_oldest_pre_holdout_observations"
                    or bias.get("minimum_observations") != sb.MIN_OBSERVATIONS):
                errors.append(f"{prefix}.selection_bias_policy_invalid")
            export = program.get("portfolio_export_policy")
            expected_export_rules = {
                "enabled": True,
                "version": "1.0.0",
                "benchmark_symbol": "SPY",
                "benchmark_adjustment_view": "total_return",
                "cost_stress_multiplier": 1.0,
                "priority_rule": "event_date_then_event_id_then_scenario_id",
                "sector_bucket_rule": "unknown_sector_single_concentration_bucket",
                "daily_volatility_rule": "annualized_feature_div_sqrt_252_to_bps",
                "missing_microstructure_rule": "retain_null_then_cost_gate_not_allocated",
            }
            expected_export_keys = {
                *expected_export_rules,
                "requested_weight",
                "assumed_fraction_of_adv",
            }
            if not isinstance(export, dict) or set(export) != expected_export_keys:
                errors.append(f"{prefix}.portfolio_export_policy_invalid")
            else:
                if any(export.get(key) != value
                       for key, value in expected_export_rules.items()):
                    errors.append(f"{prefix}.portfolio_export_policy_invalid")
                if (not _number(export.get("requested_weight"))
                        or not 0 < float(export["requested_weight"]) <= 1):
                    errors.append(f"{prefix}.portfolio_export_weight_invalid")
                if (not _number(export.get("assumed_fraction_of_adv"))
                        or not 0 < float(export["assumed_fraction_of_adv"]) <= 0.01):
                    errors.append(f"{prefix}.portfolio_export_adv_fraction_invalid")
    governance = config.get("governance")
    expected_governance = {
        "best_possible_terminal": "candidate_for_category_D_governance_review",
        "human_approval_required_before_activation": True,
        "production_change_requires_separate_patch": True,
        "protected_risk_limits_unchanged": True,
        "buy_gate_unchanged": True,
        "execution_remains_disabled": True,
    }
    if not isinstance(governance, dict):
        errors.append("governance_missing")
    else:
        for key, expected in expected_governance.items():
            if governance.get(key) != expected:
                errors.append(f"governance_field_incorrect:{key}")
    return errors


def load_config(path: Path) -> tuple[dict | None, list[str]]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"config_unreadable_or_malformed:{type(exc).__name__}"]
    return config, validate_config(config)


def validate_bundle_structure(bundle: Any) -> list[str]:
    if not isinstance(bundle, dict):
        return ["bundle_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in qf.SAFETY_FIELDS.items()
              if bundle.get(key) != expected]
    if bundle.get("schema") != BUNDLE_SCHEMA:
        errors.append("bundle_schema_mismatch")
    for key in ("bundle_id", "experiment_id", "program_id", "program_version"):
        if not isinstance(bundle.get(key), str) or not _ID_RE.fullmatch(bundle.get(key, "")):
            errors.append(f"{key}_invalid")
    times = [bundle.get(key) for key in (
        "preregistered_at_utc", "holdout_locked_at_utc", "selection_completed_at_utc")]
    parsed = [_parse_utc(value) for value in times]
    if any(value is None for value in parsed):
        errors.append("bundle_timestamps_invalid")
    elif not parsed[0] <= parsed[1] <= parsed[2]:
        errors.append("holdout_not_locked_before_selection")
    for key in ("manifest", "walk_forward", "selection_bias"):
        ref = bundle.get(key)
        if not isinstance(ref, dict) or not _safe_relative(ref.get("path")) or (
                not isinstance(ref.get("sha256"), str)
                or not _SHA_RE.fullmatch(ref.get("sha256", ""))):
            errors.append(f"artifact_ref_invalid:{key}")
    folds = bundle.get("folds")
    if not isinstance(folds, list) or not folds:
        errors.append("folds_missing")
    else:
        for index, fold in enumerate(folds):
            if not isinstance(fold, dict) or not isinstance(fold.get("fold"), int):
                errors.append(f"fold_invalid:{index}")
                continue
            for key in ("backtest", "diagnostics"):
                ref = fold.get(key)
                if not isinstance(ref, dict) or not _safe_relative(ref.get("path")) or (
                        not isinstance(ref.get("sha256"), str)
                        or not _SHA_RE.fullmatch(ref.get("sha256", ""))):
                    errors.append(f"fold_ref_invalid:{index}:{key}")
    holdout = bundle.get("holdout")
    if not isinstance(holdout, dict):
        errors.append("holdout_missing")
    else:
        for key in ("backtest", "diagnostics"):
            ref = holdout.get(key)
            if not isinstance(ref, dict) or not _safe_relative(ref.get("path")) or (
                    not isinstance(ref.get("sha256"), str)
                    or not _SHA_RE.fullmatch(ref.get("sha256", ""))):
                errors.append(f"holdout_ref_invalid:{key}")
    stress = bundle.get("cost_stress")
    if not isinstance(stress, list) or not stress:
        errors.append("cost_stress_missing")
    else:
        for index, item in enumerate(stress):
            if not isinstance(item, dict) or not _number(item.get("cost_multiplier")):
                errors.append(f"cost_stress_invalid:{index}")
                continue
            for key in ("backtest", "diagnostics"):
                ref = item.get(key)
                if not isinstance(ref, dict) or not _safe_relative(ref.get("path")) or (
                        not isinstance(ref.get("sha256"), str)
                        or not _SHA_RE.fullmatch(ref.get("sha256", ""))):
                    errors.append(f"cost_stress_ref_invalid:{index}:{key}")
    portfolio_export = bundle.get("portfolio_export")
    if portfolio_export is not None:
        if not isinstance(portfolio_export, dict) or set(portfolio_export) != {
                "contract", "candidate_portfolio", "source_backtest"}:
            errors.append("portfolio_export_invalid")
        else:
            for key, ref in portfolio_export.items():
                if (not isinstance(ref, dict) or not _safe_relative(ref.get("path"))
                        or not isinstance(ref.get("sha256"), str)
                        or not _SHA_RE.fullmatch(ref.get("sha256", ""))):
                    errors.append(f"portfolio_export_ref_invalid:{key}")
    if bundle.get("bundle_sha256") != _sha256_obj(_bundle_payload(bundle)):
        errors.append("bundle_sha256_mismatch")
    return errors


def _load_ref(bundle_dir: Path, experiment_root: Path,
              ref: dict) -> tuple[Any | None, list[str]]:
    candidate = bundle_dir / ref["path"]
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = experiment_root.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None, ["artifact_path_escape_or_missing"]
    actual_sha = _sha256_file(resolved)
    if actual_sha != ref["sha256"]:
        return None, ["artifact_sha256_mismatch"]
    try:
        return json.loads(resolved.read_text(encoding="utf-8")), []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"artifact_unreadable_or_malformed:{type(exc).__name__}"]


def _load_pair(bundle_dir: Path, experiment_root: Path,
               pair: dict) -> tuple[dict | None, dict | None, list[str]]:
    backtest, errors = _load_ref(bundle_dir, experiment_root, pair["backtest"])
    diagnostics, diag_errors = _load_ref(bundle_dir, experiment_root, pair["diagnostics"])
    errors.extend(diag_errors)
    if errors:
        return None, None, errors
    errors.extend(f"backtest:{error}" for error in qb.validate_result(backtest))
    errors.extend(f"diagnostics:{error}" for error in qmd.validate_diagnostics(diagnostics))
    if not errors and diagnostics.get("source_result_sha256") != backtest.get("result_sha256"):
        errors.append("diagnostics_backtest_binding_mismatch")
    return backtest, diagnostics, errors


def _record_key(record: dict) -> str:
    return f"{record.get('event_date')}:{record.get('event_id')}"


def _metrics_from_bundle(bundle_path: Path, experiment_root: Path,
                         program: dict) -> tuple[dict | None, list[str]]:
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"bundle_unreadable_or_malformed:{type(exc).__name__}"]
    errors = validate_bundle_structure(bundle)
    if errors:
        return None, errors
    if bundle["program_id"] != program["program_id"] or (
            bundle["program_version"] != program["program_version"]):
        return None, ["bundle_program_version_mismatch"]
    bundle_dir = bundle_path.parent
    manifest, ref_errors = _load_ref(bundle_dir, experiment_root, bundle["manifest"])
    walk, walk_errors = _load_ref(bundle_dir, experiment_root, bundle["walk_forward"])
    selection, selection_errors = _load_ref(
        bundle_dir, experiment_root, bundle["selection_bias"])
    errors = ref_errors + walk_errors + selection_errors
    if errors:
        return None, errors
    expected_manifest_sha256 = qb._sha256_obj(manifest)
    errors.extend(f"manifest:{error}" for error in em.validate_manifest(manifest))
    errors.extend(f"selection_bias:{error}" for error in sb.validate_result(selection))
    if "error" in walk or walk.get("verification", {}).get("ok") is not True:
        errors.append("walk_forward_invalid_or_unverified")
    if any(walk.get(key) != expected for key, expected in DIAGNOSTIC_FIELDS.items()):
        errors.append("walk_forward_safety_invalid")
    if manifest.get("experiment_id") != bundle["experiment_id"] or (
            selection.get("experiment_id") != bundle["experiment_id"]):
        errors.append("experiment_id_binding_mismatch")
    if manifest.get("parameter_grid") != program["parameter_grid"] or (
            manifest.get("n_trials") != program["expected_trial_count"]):
        errors.append("manifest_not_bound_to_preregistered_grid")
    if selection.get("sample", {}).get("n_trials_declared") != manifest.get("n_trials"):
        errors.append("selection_bias_trial_count_mismatch")
    expected_walk = program["walk_forward"]
    expected_params = {
        "train_days": expected_walk["train_calendar_days"],
        "validate_days": expected_walk["validate_calendar_days"],
        "test_days": expected_walk["test_calendar_days"],
        "embargo_days": expected_walk["embargo_calendar_days"],
        "step_days": expected_walk["step_calendar_days"],
    }
    params = walk.get("params", {})
    if any(params.get(key) != value for key, value in expected_params.items()):
        errors.append("walk_forward_not_bound_to_preregistered_policy")
    if errors:
        return None, errors

    fold_docs = []
    fold_numbers = [item.get("fold") for item in bundle["folds"]]
    expected_numbers = [item.get("fold") for item in walk["folds"]]
    if fold_numbers != expected_numbers:
        errors.append("bundle_folds_do_not_match_walk_forward_plan")
    oos_keys: set[str] = set()
    for item in bundle["folds"]:
        backtest, diagnostics, pair_errors = _load_pair(
            bundle_dir, experiment_root, item)
        errors.extend(f"fold[{item['fold']}]:{error}" for error in pair_errors)
        if pair_errors:
            continue
        if backtest["experiment_id"] != bundle["experiment_id"]:
            errors.append(f"fold[{item['fold']}]:experiment_id_mismatch")
        if backtest.get("lineage", {}).get("experiment_manifest_sha256") != (
                expected_manifest_sha256):
            errors.append(f"fold[{item['fold']}]:manifest_binding_mismatch")
        plan = next((value for value in walk["folds"] if value["fold"] == item["fold"]), None)
        if plan is None:
            continue
        start, end = plan["test"]["start"], plan["test"]["end"]
        for record in backtest["records"]:
            if not start <= record["event_date"] <= end:
                errors.append(f"fold[{item['fold']}]:event_outside_test_window")
            key = _record_key(record)
            if key in oos_keys:
                errors.append(f"fold[{item['fold']}]:duplicate_oos_observation")
            oos_keys.add(key)
        fold_docs.append((backtest, diagnostics))

    holdout_backtest, holdout_diagnostics, pair_errors = _load_pair(
        bundle_dir, experiment_root, bundle["holdout"])
    errors.extend(f"holdout:{error}" for error in pair_errors)
    latest_test_end = max(value["test"]["end"] for value in walk["folds"])
    if not pair_errors:
        if holdout_backtest["experiment_id"] != bundle["experiment_id"]:
            errors.append("holdout:experiment_id_mismatch")
        if holdout_backtest.get("lineage", {}).get("experiment_manifest_sha256") != (
                expected_manifest_sha256):
            errors.append("holdout:manifest_binding_mismatch")
        for record in holdout_backtest["records"]:
            if record["event_date"] <= latest_test_end:
                errors.append("holdout_not_chronologically_after_walk_forward_tests")

    stress_docs = []
    holdout_records = ({_record_key(record): record for record in holdout_backtest["records"]}
                       if holdout_backtest else {})
    for index, item in enumerate(bundle["cost_stress"]):
        backtest, diagnostics, pair_errors = _load_pair(bundle_dir, experiment_root, item)
        errors.extend(f"cost_stress[{index}]:{error}" for error in pair_errors)
        if pair_errors:
            continue
        records = {_record_key(record): record for record in backtest["records"]}
        if backtest.get("lineage", {}).get("experiment_manifest_sha256") != (
                expected_manifest_sha256):
            errors.append(f"cost_stress[{index}]:manifest_binding_mismatch")
        if set(records) != set(holdout_records):
            errors.append(f"cost_stress[{index}]:observation_set_mismatch")
        for key, base_record in holdout_records.items():
            stress_record = records.get(key)
            if stress_record is None or stress_record.get("simulation_status") != (
                    base_record.get("simulation_status")):
                errors.append(f"cost_stress[{index}]:status_mismatch")
                continue
            if base_record.get("simulation_status") == "completed":
                base_cost = base_record.get("transaction_cost_bps_per_side")
                stress_cost = stress_record.get("transaction_cost_bps_per_side")
                if (not _number(base_cost) or not _number(stress_cost)
                        or float(stress_cost) < float(base_cost) * float(item["cost_multiplier"])):
                    errors.append(f"cost_stress[{index}]:cost_multiplier_not_realized")
        stress_docs.append((item, backtest, diagnostics))
    if errors:
        return None, errors

    fold_expectancies = [value[1]["metrics"]["expectancy"] for value in fold_docs]
    metrics = {
        "bundle_id": bundle["bundle_id"],
        "experiment_id": bundle["experiment_id"],
        "program_id": bundle["program_id"],
        "bundle_sha256": bundle["bundle_sha256"],
        "preregistered_at_utc": bundle["preregistered_at_utc"],
        "holdout_locked_at_utc": bundle["holdout_locked_at_utc"],
        "selection_completed_at_utc": bundle["selection_completed_at_utc"],
        "holdout_locked_before_selection": True,
        "walk_forward_fold_count": len(fold_docs),
        "walk_forward_completed_oos": sum(
            value[1]["sample"]["completed"] for value in fold_docs),
        "walk_forward_fold_expectancies": fold_expectancies,
        "holdout_completed": holdout_diagnostics["sample"]["completed"],
        "holdout_expectancy": holdout_diagnostics["metrics"]["expectancy"],
        "holdout_profit_factor": holdout_diagnostics["metrics"]["profit_factor"],
        "holdout_max_drawdown": holdout_diagnostics["metrics"]["max_compounded_drawdown"],
        "cost_stress": [{
            "name": item.get("name"),
            "cost_multiplier": item["cost_multiplier"],
            "completed": diagnostics["sample"]["completed"],
            "expectancy": diagnostics["metrics"]["expectancy"],
        } for item, _backtest, diagnostics in stress_docs],
        "trial_coverage_complete": selection["sample"]["trial_coverage_complete"],
        "deflated_sharpe_available": selection["deflated_sharpe_ratio"]["available"],
        "deflated_sharpe_probability": selection["deflated_sharpe_ratio"].get(
            "probability_selected_sharpe_exceeds_multiple_trial_hurdle"),
        "pbo_available": selection["cscv_pbo"]["available"],
        "probability_backtest_overfitting": selection["cscv_pbo"].get(
            "probability_of_backtest_overfitting"),
        "selection_bias_sha256": selection["selection_bias_sha256"],
    }
    return metrics, []


def evaluate_gate_metrics(metrics: dict, gates: dict) -> dict:
    fold_values = metrics.get("walk_forward_fold_expectancies") or []
    positive_fraction = (sum(_number(value) and float(value) > 0 for value in fold_values)
                         / len(fold_values) if fold_values else None)
    minimum_fold = min((float(value) for value in fold_values if _number(value)), default=None)
    stress = metrics.get("cost_stress") or []
    qualifying_stress = [item for item in stress
                         if _number(item.get("cost_multiplier")) and float(
                             item["cost_multiplier"]) >= gates["minimum_cost_stress_multiplier"]]
    worst_stress_expectancy = min((float(item["expectancy"]) for item in qualifying_stress
                                   if _number(item.get("expectancy"))), default=None)

    def gate(code: str, passed: bool, observed: Any, required: Any) -> dict:
        return {"code": code, "passed": bool(passed), "observed": observed,
                "required": required}

    holdout_expectancy = metrics.get("holdout_expectancy")
    profit_factor = metrics.get("holdout_profit_factor")
    drawdown = metrics.get("holdout_max_drawdown")
    dsr = metrics.get("deflated_sharpe_probability")
    pbo = metrics.get("probability_backtest_overfitting")
    checks = [
        gate("minimum_completed_oos",
             isinstance(metrics.get("walk_forward_completed_oos"), int)
             and metrics["walk_forward_completed_oos"] >= gates["minimum_completed_oos"],
             metrics.get("walk_forward_completed_oos"), gates["minimum_completed_oos"]),
        gate("minimum_holdout_completed",
             isinstance(metrics.get("holdout_completed"), int)
             and metrics["holdout_completed"] >= gates["minimum_holdout_completed"],
             metrics.get("holdout_completed"), gates["minimum_holdout_completed"]),
        gate("minimum_walk_forward_folds",
             isinstance(metrics.get("walk_forward_fold_count"), int)
             and metrics["walk_forward_fold_count"] >= gates["minimum_walk_forward_folds"],
             metrics.get("walk_forward_fold_count"), gates["minimum_walk_forward_folds"]),
        gate("positive_fold_fraction",
             _number(positive_fraction)
             and float(positive_fraction) >= gates["minimum_positive_fold_fraction"],
             positive_fraction, gates["minimum_positive_fold_fraction"]),
        gate("minimum_fold_expectancy",
             _number(minimum_fold)
             and float(minimum_fold) >= gates["minimum_fold_expectancy"],
             minimum_fold, gates["minimum_fold_expectancy"]),
        gate("positive_holdout_expectancy_after_costs",
             _number(holdout_expectancy)
             and float(holdout_expectancy) > gates["minimum_holdout_net_expectancy"],
             holdout_expectancy,
             f"> {gates['minimum_holdout_net_expectancy']}"),
        gate("minimum_holdout_profit_factor",
             _number(profit_factor)
             and float(profit_factor) >= gates["minimum_holdout_profit_factor"],
             profit_factor, gates["minimum_holdout_profit_factor"]),
        gate("maximum_holdout_drawdown",
             _number(drawdown)
             and abs(float(drawdown)) <= gates["maximum_holdout_drawdown_abs"],
             drawdown, f"abs <= {gates['maximum_holdout_drawdown_abs']}"),
        gate("positive_expectancy_under_cost_stress",
             bool(qualifying_stress) and _number(worst_stress_expectancy)
             and float(worst_stress_expectancy) > gates["minimum_stressed_net_expectancy"],
             worst_stress_expectancy,
             {"minimum_multiplier": gates["minimum_cost_stress_multiplier"],
              "expectancy": f"> {gates['minimum_stressed_net_expectancy']}"}),
        gate("complete_trial_matrix",
             metrics.get("trial_coverage_complete") is True,
             metrics.get("trial_coverage_complete"), True),
        gate("deflated_sharpe_probability",
             metrics.get("deflated_sharpe_available") is True and _number(dsr)
             and float(dsr) >= gates["minimum_deflated_sharpe_probability"],
             dsr, gates["minimum_deflated_sharpe_probability"]),
        gate("maximum_probability_backtest_overfitting",
             metrics.get("pbo_available") is True and _number(pbo)
             and float(pbo) <= gates["maximum_probability_backtest_overfitting"],
             pbo, gates["maximum_probability_backtest_overfitting"]),
        gate("locked_chronological_holdout",
             metrics.get("holdout_locked_before_selection") is True,
             metrics.get("holdout_locked_before_selection"), True),
    ]
    passed = all(item["passed"] for item in checks)
    return {
        "status": ("candidate_for_category_D_governance_review"
                   if passed else "rejected_or_incomplete"),
        "all_gates_passed": passed,
        "gate_count": len(checks),
        "passed_count": sum(item["passed"] for item in checks),
        "failed_codes": [item["code"] for item in checks if not item["passed"]],
        "gates": checks,
        "activation_authority": False,
        "profitability_established": False,
        "human_approval_required": True,
    }


def evaluate_bundle(bundle_path: Path, experiment_root: Path,
                    program: dict, gates: dict) -> dict:
    metrics, errors = _metrics_from_bundle(bundle_path, experiment_root, program)
    if errors:
        return {
            "bundle_path": str(bundle_path.relative_to(experiment_root)),
            "program_id": program["program_id"],
            "status": "invalid_bundle",
            "errors": sorted(set(errors)),
            "all_gates_passed": False,
            "activation_authority": False,
            "profitability_established": False,
        }
    evaluation = evaluate_gate_metrics(metrics or {}, gates)
    return {"bundle_path": str(bundle_path.relative_to(experiment_root)),
            "program_id": program["program_id"], "metrics": metrics, **evaluation}


def _readiness_actions(receipt: dict) -> list[dict]:
    quality = receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {}
    reasons = list(quality.get("readiness_blockers") or []) + list(
        quality.get("warnings") or []) + list(receipt.get("blockers") or [])
    action_ids = []
    for reason in reasons:
        action = READINESS_ACTIONS.get(reason)
        if action and action not in action_ids:
            action_ids.append(action)
    return [{
        "action_id": action,
        "execution_mode": "allowlisted_deterministic_pipeline_only",
        "dynamic_command_allowed": False,
        "broker_access": "none",
    } for action in action_ids]


def _new_artifact(target: str, run_id: str, generated_utc: str,
                  config_sha256: str | None) -> dict:
    return {
        **_base(),
        "ok": True,
        "schema": ARTIFACT_SCHEMA,
        "target_session_date": target,
        "post_close_run_id": run_id,
        "generated_utc": generated_utc,
        "config_sha256": config_sha256,
        "status": "blocked",
        "readiness": {},
        "feature_corpus": {
            "path": None, "status": "unavailable", "package_count": 0,
            "row_count": 0, "eligible_row_count": 0, "corpus_sha256": None,
            "valid": False, "errors": ["feature_corpus_not_evaluated"],
        },
        "programs": [],
        "experiments": [],
        "candidate_count": 0,
        "next_actions": [],
        "blockers": [],
        "governance": {
            "best_possible_terminal": "candidate_for_category_D_governance_review",
            "activation_authority": False,
            "strategy_change_allowed": False,
            "execution_enabled": False,
            "profitability_established": False,
            "human_approval_required_before_activation": True,
        },
    }


def run_factory(*, target: str, run_id: str, config_path: Path,
                readiness_path: Path | None = None,
                experiment_root: Path | None = None,
                generated_utc: str | None = None) -> tuple[dict, int]:
    generated_utc = generated_utc or dt.datetime.now(dt.timezone.utc).isoformat()
    config_sha = _sha256_file(config_path)
    artifact = _new_artifact(target, run_id, generated_utc, config_sha)
    structural_blockers = []
    try:
        dt.date.fromisoformat(target)
    except (TypeError, ValueError):
        structural_blockers.append("target_session_date_invalid")
    if not provenance.valid_postclose_run_id(run_id, target):
        structural_blockers.append("post_close_run_id_invalid_for_target")
    if _parse_utc(generated_utc) is None:
        structural_blockers.append("generated_utc_invalid")
    config, config_errors = load_config(config_path)
    structural_blockers.extend(config_errors)
    if config is None or config_errors:
        artifact["blockers"] = sorted(set(structural_blockers))
        artifact["artifact_sha256"] = _sha256_obj(_artifact_payload(artifact))
        return artifact, 1

    paths = config["paths"]
    readiness_path = readiness_path or ROOT / paths["readiness_root"] / f"{target}.json"
    experiment_root = experiment_root or ROOT / paths["experiment_root"]
    try:
        receipt = json.loads(readiness_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        receipt = None
        structural_blockers.append("readiness_receipt_missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        receipt = None
        structural_blockers.append(f"readiness_receipt_unreadable:{type(exc).__name__}")
    if receipt is not None:
        receipt_errors = data_lake_intake.validate_receipt(receipt)
        structural_blockers.extend(f"readiness:{error}" for error in receipt_errors)
        if receipt.get("target_session_date") != target:
            structural_blockers.append("readiness_target_mismatch")
        artifact["readiness"] = {
            "path": str(readiness_path),
            "receipt_sha256": receipt.get("receipt_sha256"),
            "status": receipt.get("status"),
            "research_ready": receipt.get("research_ready") is True,
            "quality_gate_pass": receipt.get("quality", {}).get("quality_gate_pass"),
            "readiness_blockers": receipt.get("quality", {}).get("readiness_blockers", []),
            "warnings": sorted(set((receipt.get("warnings") or [])
                                   + (receipt.get("quality", {}).get("warnings") or []))),
        }
        artifact["next_actions"] = _readiness_actions(receipt)
    if structural_blockers:
        artifact["blockers"] = sorted(set(structural_blockers))
        artifact["artifact_sha256"] = _sha256_obj(_artifact_payload(artifact))
        return artifact, 1

    programs_by_id = {program["program_id"]: program for program in config["programs"]}
    bundle_paths = sorted(experiment_root.glob("*/bundle.json")) if experiment_root.is_dir() else []
    session_feature_root = ROOT / paths["session_feature_root"]
    captured_packages = (sorted(session_feature_root.glob("*/package.json"))
                         if session_feature_root.is_dir() else [])
    corpus_path = ROOT / paths["feature_corpus"]
    if corpus_path.is_file():
        try:
            corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
            corpus_errors = fc.validate_corpus(corpus)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            corpus, corpus_errors = None, [f"unreadable_or_malformed:{type(exc).__name__}"]
        artifact["feature_corpus"] = {
            "path": str(corpus_path),
            "status": corpus.get("status") if corpus else "invalid",
            "package_count": corpus.get("package_count") if corpus else 0,
            "row_count": corpus.get("row_count") if corpus else 0,
            "eligible_row_count": corpus.get("eligible_row_count") if corpus else 0,
            "corpus_sha256": corpus.get("corpus_sha256") if corpus else None,
            "valid": not corpus_errors,
            "errors": corpus_errors,
        }
    else:
        artifact["feature_corpus"] = {
            "path": str(corpus_path), "status": "unavailable", "package_count": 0,
            "row_count": 0, "eligible_row_count": 0, "corpus_sha256": None,
            "valid": False, "errors": ["feature_corpus_missing"],
        }
    experiments = []
    for bundle_path in bundle_paths:
        try:
            header = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            experiments.append({"bundle_path": str(bundle_path),
                                "status": "invalid_bundle",
                                "errors": ["bundle_unreadable_or_malformed"],
                                "all_gates_passed": False,
                                "activation_authority": False,
                                "profitability_established": False})
            continue
        program = programs_by_id.get(header.get("program_id"))
        if program is None:
            experiments.append({"bundle_path": str(bundle_path.relative_to(experiment_root)),
                                "status": "unregistered_program",
                                "errors": ["program_not_preregistered"],
                                "all_gates_passed": False,
                                "activation_authority": False,
                                "profitability_established": False})
            continue
        experiments.append(evaluate_bundle(bundle_path, experiment_root, program,
                                           config["evidence_gates"]))
    artifact["experiments"] = experiments
    artifact["candidate_count"] = sum(
        value.get("status") == "candidate_for_category_D_governance_review"
        for value in experiments)
    artifact["programs"] = [{
        "program_id": program["program_id"],
        "program_version": program["program_version"],
        "preregistered": True,
        "expected_trial_count": program["expected_trial_count"],
        "captured_session_package_count": len(captured_packages),
        "corpus_eligible_row_count": artifact["feature_corpus"]["eligible_row_count"],
        "bundle_count": sum(value.get("program_id") == program["program_id"]
                            for value in experiments),
        "candidate_count": sum(
            value.get("program_id") == program["program_id"]
            and value.get("status") == "candidate_for_category_D_governance_review"
            for value in experiments),
        "activation_authority": False,
    } for program in config["programs"]]

    if artifact["candidate_count"]:
        artifact["status"] = "candidate_for_category_D_governance_review"
    elif experiments:
        artifact["status"] = "experiments_rejected_or_incomplete"
    elif receipt["research_ready"] is True:
        artifact["status"] = "awaiting_preregistered_experiments"
        artifact["next_actions"].extend([
            {"action_id": action,
             "execution_mode": "allowlisted_deterministic_pipeline_only",
             "dynamic_command_allowed": False, "broker_access": "none"}
            for action in ("materialize_point_in_time_event_corpus",
                           "accumulate_immutable_session_feature_packages",
                           "run_preregistered_walk_forward_grid",
                           "lock_chronological_holdout",
                           "compute_dsr_and_cscv_pbo")
        ])
    else:
        artifact["status"] = "collecting_point_in_time_data"
    artifact["artifact_sha256"] = _sha256_obj(_artifact_payload(artifact))
    errors = validate_artifact(artifact)
    if errors:
        fallback = _new_artifact(target, run_id, generated_utc, config_sha)
        fallback["blockers"] = [f"artifact_self_validation:{error}" for error in errors]
        fallback["artifact_sha256"] = _sha256_obj(_artifact_payload(fallback))
        return fallback, 1
    return artifact, 0


def validate_artifact(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["artifact_not_object"]
    errors = [f"safety_field_incorrect:{key}" for key, expected in FACTORY_SAFETY.items()
              if doc.get(key) != expected]
    if doc.get("ok") is not True:
        errors.append("artifact_ok_must_be_true")
    if doc.get("schema") != ARTIFACT_SCHEMA:
        errors.append("artifact_schema_mismatch")
    target = doc.get("target_session_date")
    if not isinstance(target, str) or not _DATE_RE.fullmatch(target):
        errors.append("target_session_date_invalid")
    if not provenance.valid_postclose_run_id(doc.get("post_close_run_id"), target or ""):
        errors.append("post_close_run_id_invalid")
    if _parse_utc(doc.get("generated_utc")) is None:
        errors.append("generated_utc_invalid")
    if doc.get("status") not in {
            "blocked", "collecting_point_in_time_data",
            "awaiting_preregistered_experiments", "experiments_rejected_or_incomplete",
            "candidate_for_category_D_governance_review"}:
        errors.append("status_invalid")
    for key in ("programs", "experiments", "next_actions", "blockers"):
        if not isinstance(doc.get(key), list):
            errors.append(f"{key}_invalid")
    corpus = doc.get("feature_corpus")
    if not isinstance(corpus, dict):
        errors.append("feature_corpus_invalid")
    else:
        for key in ("package_count", "row_count", "eligible_row_count"):
            if not isinstance(corpus.get(key), int) or isinstance(corpus.get(key), bool) \
                    or corpus[key] < 0:
                errors.append(f"feature_corpus_field_invalid:{key}")
        if corpus.get("valid") not in (True, False) or not isinstance(corpus.get("errors"), list):
            errors.append("feature_corpus_validation_state_invalid")
    if not isinstance(doc.get("candidate_count"), int) or isinstance(
            doc.get("candidate_count"), bool):
        errors.append("candidate_count_invalid")
    governance = doc.get("governance")
    if not isinstance(governance, dict) or any(governance.get(key) is not expected for key, expected in {
            "activation_authority": False,
            "strategy_change_allowed": False,
            "execution_enabled": False,
            "profitability_established": False,
            "human_approval_required_before_activation": True,
    }.items()):
        errors.append("governance_invalid")
    if doc.get("artifact_sha256") != _sha256_obj(_artifact_payload(doc)):
        errors.append("artifact_sha256_mismatch")
    return errors


def _atomic_write(path: Path, value: dict, replace: bool) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return "output_already_exists"
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_research_factory_",
                                     suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
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


def _emit(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Unattended offline quant research factory; never activates strategies.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--target", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--config", default=str(CONFIG_DEFAULT))
    run.add_argument("--readiness")
    run.add_argument("--experiments")
    run.add_argument("--out", required=True)
    run.add_argument("--replace", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            doc = json.loads(Path(args.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _emit({**_base(), "ok": False,
                   "error": f"artifact_unreadable_or_malformed:{type(exc).__name__}"})
            return 1
        errors = validate_artifact(doc)
        _emit({**_base(), "ok": not errors, "errors": errors, "path": args.path})
        return 0 if not errors else 1

    artifact, rc = run_factory(
        target=args.target, run_id=args.run_id, config_path=Path(args.config),
        readiness_path=Path(args.readiness) if args.readiness else None,
        experiment_root=Path(args.experiments) if args.experiments else None)
    write_error = _atomic_write(Path(args.out), artifact, args.replace)
    if write_error:
        _emit({**_base(), "ok": False, "error": write_error,
               "output_path": args.out})
        return 1
    _emit({**_base(), "ok": rc == 0, "written": args.out,
           "status": artifact["status"], "artifact_sha256": artifact["artifact_sha256"]})
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
