#!/usr/bin/env python3
"""Cost-matched benchmark artifacts and preregistered factory export packages.

The module is offline and diagnostic-only. It never mutates the strategy registry, evaluates
profitability, accesses a broker, creates an actual order simulation, or activates a strategy.
Legacy factory bundles without a pre-holdout portfolio export contract receive a blocked receipt
instead of a post-hoc reconstruction.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import adjustment_policy as ap  # noqa: E402
from quant import backtest as qb  # noqa: E402
from quant import cost_model as cm  # noqa: E402
from quant import experiment_manifest as em  # noqa: E402
from quant import features as qf  # noqa: E402
from quant import portfolio_backtest as pb  # noqa: E402
from quant import research_factory as rf  # noqa: E402

TOOL = "quant.strategy_artifacts"
POLICY_SCHEMA = "quant_strategy_artifact_policy/1.0"
BENCHMARK_SCHEMA = "quant_benchmark_reference/1.0"
CONTRACT_SCHEMA = "quant_factory_portfolio_export_contract/1.0"
RECEIPT_SCHEMA = "quant_factory_strategy_export_receipt/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_strategy_artifact_policy.json"
COST_POLICY_DEFAULT = ROOT / "config" / "quant_cost_policy.json"
PORTFOLIO_POLICY_DEFAULT = ROOT / "config" / "quant_portfolio_policy.json"
EXPORT_ROOT_DEFAULT = ROOT / "data" / "runtime" / "quant" / "strategy_artifacts"
_MAX_BYTES = 128 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NUM = (int, float)

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "feeds_research_factory": False,
    "strategy_change_allowed": False,
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
    "actual_order_simulation": False,
}

OUTPUT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "feeds_research_factory": False,
    "promotion_gate_change_allowed": False,
    "profitability_claim_allowed": False,
    "automatic_promotion_allowed": False,
    "broker_access": "none",
    "broker_tool_invoked": False,
    "model_invoked": False,
    "account_data_allowed": False,
    "order_data_allowed": False,
    "network_allowed": False,
    "actual_order_simulation": False,
}

_POLICY_KEYS = {
    "schema", "version", "objective", "candidate_source_schema",
    "benchmark_output_schema", "benchmark_metadata_schema", "benchmark_symbol",
    "benchmark_adjustment_view", "benchmark_allocation_rule", "benchmark_entry_rule",
    "benchmark_mark_rule", "benchmark_exit_rule", "benchmark_cost_rule",
    "date_alignment_rule", "missing_bar_rule", "factory_bundle_schema",
    "preregistered_contract_schema", "required_preregistered_input_refs",
    "bundle_export_section_keys", "contract_lock_rule", "manifest_binding_rule",
    "strategy_artifact_root", "history_overwrite_allowed", "registry_mutation_allowed",
    "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "candidate_source_schema": pb.RESULT_SCHEMA,
    "benchmark_output_schema": pb.RESULT_SCHEMA,
    "benchmark_metadata_schema": BENCHMARK_SCHEMA,
    "benchmark_symbol": "SPY",
    "benchmark_adjustment_view": "total_return",
    "benchmark_allocation_rule": "portfolio_policy_maximum_gross_exposure",
    "benchmark_entry_rule": "first_candidate_session_open",
    "benchmark_mark_rule": "total_return_adjusted_daily_close",
    "benchmark_exit_rule": "final_candidate_session_close",
    "benchmark_cost_rule": (
        "cost_policy_minimum_bps_per_side_times_candidate_stress_on_entry_and_exit"),
    "date_alignment_rule": "exact_candidate_daily_path_dates_without_imputation",
    "missing_bar_rule": "fail_closed",
    "factory_bundle_schema": rf.BUNDLE_SCHEMA,
    "preregistered_contract_schema": CONTRACT_SCHEMA,
    "required_preregistered_input_refs": [
        "source_bars", "source_plans", "portfolio_plan", "cost_policy",
        "portfolio_policy", "benchmark_adjusted_view",
    ],
    "bundle_export_section_keys": ["contract", "candidate_portfolio", "source_backtest"],
    "contract_lock_rule": (
        "contract_specified_and_manifest_fingerprinted_at_or_before_holdout_lock"),
    "manifest_binding_rule": (
        "contract_and_every_preregistered_input_hash_must_appear_in_"
        "experiment_manifest_data_snapshots"),
    "strategy_artifact_root": "data/runtime/quant/strategy_artifacts",
    "history_overwrite_allowed": False,
    "registry_mutation_allowed": False,
}
_CONTRACT_KEYS = {
    "schema", "version", "experiment_id", "program_id", "program_version",
    "specified_at_utc", "cost_stress_multiplier", "benchmark_symbol", "input_refs",
    "safety", "contract_sha256",
}
_REF_KEYS = {"path", "sha256"}
_BENCHMARK_KEYS = {
    "schema", "artifact_policy_version", "artifact_policy_sha256", "symbol",
    "adjustment_view", "allocation_rule", "allocation_weight", "entry_rule", "mark_rule",
    "exit_rule", "cost_rule", "base_cost_bps_per_side", "applied_cost_bps_per_side",
    "candidate_result_sha256", "candidate_content_sha256",
    "candidate_daily_path_sha256", "candidate_dates", "candidate_dates_sha256",
    "candidate_source_backtest_plan_sha256", "candidate_portfolio_plan_sha256",
    "candidate_observation_count", "first_date", "last_date", "adjusted_view_sha256",
    "bars_payload_sha256", "aligned_spy_bars", "aligned_spy_bars_sha256", "cost_policy",
    "portfolio_policy", "comparison_only", "candidate_constraint_exemptions",
    "profitability_status", "actual_orders_count",
}
_RECEIPT_STATUSES = {
    "blocked_missing_preregistered_portfolio_export_contract",
    "blocked_invalid_preregistered_portfolio_export_contract",
    "ready",
    "exported",
}
_RECEIPT_KEYS = {
    *OUTPUT_SAFETY,
    "tool", "ok", "schema", "bundle_id", "bundle_sha256", "experiment_id", "status",
    "blockers", "preregistration", "source_artifacts", "exported_artifacts",
    "registry_mutated", "performance_evaluated", "profitability_status", "receipt_sha256",
}
_PREREGISTRATION_KEYS = {
    "contract", "contract_sha256", "specified_at_utc", "holdout_locked_at_utc",
    "locked_before_or_at_holdout", "contract_manifest_bound",
    "all_inputs_manifest_bound",
}
_SOURCE_ARTIFACT_KEYS = {
    "candidate_portfolio", "source_backtest", "benchmark_adjusted_view",
    "cost_stress_multiplier",
}
_EXPORTED_ARTIFACT_KEYS = {"candidate_portfolio", "benchmark_reference"}
_EXPORTED_REF_KEYS = {"path", "sha256", "result_sha256"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload(value: dict, hash_key: str) -> dict:
    return {key: item for key, item in value.items() if key != hash_key}


def _number(value: Any, *, positive: bool = False, nonnegative: bool = False) -> bool:
    if not isinstance(value, _NUM) or isinstance(value, bool) or not math.isfinite(float(value)):
        return False
    if positive and float(value) <= 0:
        return False
    if nonnegative and float(value) < 0:
        return False
    return True


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _atomic_write_once(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
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


def _refuse(error: str, **extra: Any) -> dict:
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["strategy_artifact_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("strategy_artifact_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"strategy_artifact_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            document.get("version", "")):
        errors.append("strategy_artifact_policy_version_invalid")
    if not isinstance(document.get("objective"), str) or not document.get(
            "objective", "").strip():
        errors.append("strategy_artifact_policy_objective_invalid")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("strategy_artifact_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read(path)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"strategy_artifact_policy_unreadable:{type(exc).__name__}"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    document, errors = load_policy(path)
    return {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": document is not None and not errors,
        "valid": document is not None and not errors,
        "errors": errors,
        "version": document.get("version") if isinstance(document, dict) else None,
        "policy_sha256": sha256_obj(document) if isinstance(document, dict) else None,
    }


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _validate_ref(value: Any, prefix: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != _REF_KEYS:
        return [f"{prefix}:artifact_ref_shape_invalid"]
    errors = []
    if not _safe_relative(value.get("path")):
        errors.append(f"{prefix}:artifact_ref_path_invalid")
    if not isinstance(value.get("sha256"), str) or not _SHA_RE.fullmatch(
            value.get("sha256", "")):
        errors.append(f"{prefix}:artifact_ref_sha256_invalid")
    return errors


def build_export_contract(experiment_id: str, program_id: str, program_version: str,
                          specified_at_utc: str, cost_stress_multiplier: float,
                          input_refs: dict[str, dict], policy: dict) -> dict:
    document = {
        "schema": CONTRACT_SCHEMA,
        "version": policy.get("version"),
        "experiment_id": experiment_id,
        "program_id": program_id,
        "program_version": program_version,
        "specified_at_utc": specified_at_utc,
        "cost_stress_multiplier": float(cost_stress_multiplier),
        "benchmark_symbol": policy.get("benchmark_symbol"),
        "input_refs": input_refs,
        "safety": dict(POLICY_SAFETY),
        "contract_sha256": "",
    }
    document["contract_sha256"] = sha256_obj(_payload(document, "contract_sha256"))
    return document


def validate_export_contract(document: Any, policy: dict) -> list[str]:
    errors = validate_policy(policy)
    if errors:
        return [f"policy:{value}" for value in errors]
    if not isinstance(document, dict):
        return ["export_contract_not_object"]
    errors = []
    if set(document) != _CONTRACT_KEYS:
        errors.append("export_contract_top_level_keys_invalid")
    if document.get("schema") != CONTRACT_SCHEMA or document.get("version") != policy["version"]:
        errors.append("export_contract_identity_invalid")
    for key in ("experiment_id", "program_id", "program_version"):
        if not isinstance(document.get(key), str) or not _ID_RE.fullmatch(document.get(key, "")):
            errors.append(f"export_contract_id_invalid:{key}")
    if _parse_utc(document.get("specified_at_utc")) is None:
        errors.append("export_contract_specified_at_invalid")
    stress = document.get("cost_stress_multiplier")
    if not _number(stress) or float(stress) < 1:
        errors.append("export_contract_cost_stress_invalid")
    if document.get("benchmark_symbol") != policy["benchmark_symbol"]:
        errors.append("export_contract_benchmark_invalid")
    refs = document.get("input_refs")
    expected_refs = set(policy["required_preregistered_input_refs"])
    if not isinstance(refs, dict) or set(refs) != expected_refs:
        errors.append("export_contract_input_refs_invalid")
    else:
        for key, ref in refs.items():
            errors.extend(_validate_ref(ref, f"input_ref:{key}"))
    if document.get("safety") != POLICY_SAFETY:
        errors.append("export_contract_safety_invalid")
    if document.get("contract_sha256") != sha256_obj(_payload(document, "contract_sha256")):
        errors.append("export_contract_sha256_mismatch")
    return sorted(set(errors))


def _candidate_contract_errors(candidate: Any, cost_policy: dict,
                               portfolio_policy: dict) -> list[str]:
    errors = [f"candidate:{value}" for value in pb.validate_result(candidate)]
    if not isinstance(candidate, dict):
        return errors
    lineage = candidate.get("lineage") if isinstance(candidate.get("lineage"), dict) else {}
    versions = (candidate.get("policy_versions")
                if isinstance(candidate.get("policy_versions"), dict) else {})
    if lineage.get("cost_policy_sha256") != cm.sha256_obj(cost_policy):
        errors.append("candidate:cost_policy_hash_mismatch")
    if lineage.get("portfolio_policy_sha256") != pb.sha256_obj(portfolio_policy):
        errors.append("candidate:portfolio_policy_hash_mismatch")
    if versions.get("cost") != cost_policy.get("version"):
        errors.append("candidate:cost_policy_version_mismatch")
    if versions.get("portfolio") != portfolio_policy.get("version"):
        errors.append("candidate:portfolio_policy_version_mismatch")
    for key in ("source_backtest_plan_sha256", "portfolio_plan_sha256"):
        value = lineage.get(key)
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            errors.append(f"candidate:replay_spec_hash_missing:{key}")
    stress = candidate.get("cost_stress_multiplier")
    allowed = [float(value) for value in cost_policy.get("stress_multipliers", [])
               if _number(value)]
    if not _number(stress) or float(stress) not in allowed:
        errors.append("candidate:cost_stress_not_allowlisted")
    return sorted(set(errors))


def _benchmark_components(aligned_bars: list[dict], cost_policy: dict,
                          portfolio_policy: dict, stress_multiplier: float
                          ) -> tuple[list[dict], list[dict], dict]:
    initial_nav = float(portfolio_policy["initial_normalized_nav"])
    weight = float(portfolio_policy["maximum_gross_exposure"])
    base_cost_bps = float(cost_policy["minimum_cost_bps_per_side"])
    applied_cost_bps = base_cost_bps * float(stress_multiplier)
    cost_rate = applied_cost_bps / 10000.0
    scenario_id = "benchmark-spy-total-return-reference"
    entry_notional = initial_nav * weight
    entry_cost = entry_notional * cost_rate
    units = entry_notional / float(aligned_bars[0]["open"])
    cash = initial_nav - entry_notional - entry_cost
    previous_nav = initial_nav
    peak_nav = initial_nav
    daily: list[dict] = []
    total_exit_notional = 0.0
    total_exit_cost = 0.0
    for index, bar in enumerate(aligned_bars):
        gross = units * float(bar["close"])
        entries = [scenario_id] if index == 0 else []
        exits: list[str] = []
        if index == len(aligned_bars) - 1:
            total_exit_notional = gross
            total_exit_cost = gross * cost_rate
            cash += gross - total_exit_cost
            gross = 0.0
            units = 0.0
            exits = [scenario_id]
        nav = cash + gross
        peak_nav = max(peak_nav, nav)
        exposure = gross / nav if gross else 0.0
        breaches = (["gross_exposure_market_move"] if exposure > weight + 1e-10 else [])
        daily.append({
            "date": bar["date"],
            "normalized_nav": pb._round(nav),
            "daily_return": pb._round(nav / previous_nav - 1.0),
            "drawdown": pb._round(nav / peak_nav - 1.0),
            "cash_weight": pb._round(cash / nav),
            "gross_exposure": pb._round(exposure),
            "net_exposure": pb._round(exposure),
            "entry_gross_exposure_peak": pb._round(weight if index == 0 else 0.0),
            "aggregate_stop_risk": 0.0,
            "open_position_count": 1 if gross else 0,
            "sector_weights": ({"US_equity_market": pb._round(exposure)} if gross else {}),
            "catalyst_weights": ({"benchmark_reference": pb._round(exposure)} if gross else {}),
            "entries": entries,
            "exits": exits,
            "post_entry_constraint_breaches_from_market_moves": breaches,
        })
        previous_nav = nav
    cost_descriptor = {
        "symbol": "SPY", "base_cost_bps_per_side": base_cost_bps,
        "stress_multiplier": float(stress_multiplier),
        "applied_cost_bps_per_side": applied_cost_bps,
        "entry_notional": pb._round(entry_notional),
    }
    decision = {
        "scenario_id": scenario_id, "symbol": "SPY",
        "event_date": aligned_bars[0]["date"], "source_simulation_status": "completed",
        "priority": 1, "requested_weight": pb._round(weight),
        "sector": "US_equity_market", "catalyst_type": "benchmark_reference",
        "status": "allocated", "reason": "passive_total_return_reference_allocated",
        "allocated_weight": pb._round(weight),
        "applied_cost_bps_per_side": pb._round(applied_cost_bps),
        "cost_estimate_sha256": sha256_obj(cost_descriptor),
        "binding_constraints": ["benchmark_reference_gross_exposure_rule"],
        "entry_date": aligned_bars[0]["date"], "exit_date": aligned_bars[-1]["date"],
        "fill_guaranteed": False, "actual_order_exists": False, "decision_sha256": "",
    }
    decision["decision_sha256"] = pb.sha256_obj(_payload(decision, "decision_sha256"))
    final_nav = daily[-1]["normalized_nav"]
    summary = {
        "source_scenario_count": 1, "source_completed_count": 1, "allocated_count": 1,
        "not_allocated_count": 0, "final_normalized_nav": final_nav,
        "total_return": pb._round(final_nav / initial_nav - 1.0),
        "maximum_drawdown": min(row["drawdown"] for row in daily),
        "turnover_on_initial_nav": pb._round(
            (entry_notional + total_exit_notional) / initial_nav),
        "modeled_cost_on_initial_nav": pb._round(
            (entry_cost + total_exit_cost) / initial_nav),
        "maximum_entry_gross_exposure": pb._round(weight),
        "maximum_end_of_day_gross_exposure": max(row["gross_exposure"] for row in daily),
        "market_move_constraint_breach_session_count": sum(
            bool(row["post_entry_constraint_breaches_from_market_moves"]) for row in daily),
        "actual_orders_count": 0,
    }
    return [decision], daily, summary


def build_benchmark(candidate: Any, adjusted_view: Any, cost_policy: Any,
                    portfolio_policy: Any, policy: Any) -> dict:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return _refuse("strategy_artifact_policy_invalid", errors=policy_errors)
    errors = _candidate_contract_errors(candidate, cost_policy, portfolio_policy)
    errors.extend(f"cost_policy:{value}" for value in cm.validate_policy(cost_policy))
    errors.extend(f"portfolio_policy:{value}" for value in pb.validate_policy(portfolio_policy))
    errors.extend(f"adjusted_view:{value}" for value in ap.validate_view_artifact(adjusted_view))
    if errors:
        return _refuse("benchmark_inputs_invalid", errors=sorted(set(errors)))
    if adjusted_view.get("view") != policy["benchmark_adjustment_view"]:
        return _refuse("benchmark_adjusted_view_not_total_return")
    if adjusted_view.get("quant_compatible") is not True:
        return _refuse("benchmark_adjusted_view_not_quant_compatible")
    bars = adjusted_view.get("bars", {}).get(policy["benchmark_symbol"])
    if not isinstance(bars, list) or not bars:
        return _refuse("benchmark_symbol_missing_from_adjusted_view")
    dates = [row.get("date") for row in candidate["daily_path"]]
    if any(not _valid_date(value) for value in dates) or dates != sorted(set(dates)):
        return _refuse("candidate_daily_dates_invalid")
    by_date = {row.get("date"): row for row in bars if isinstance(row, dict)}
    missing = [value for value in dates if value not in by_date]
    if missing:
        return _refuse("benchmark_dates_missing", dates=missing)
    aligned: list[dict] = []
    for date in dates:
        row = by_date[date]
        if not _number(row.get("open"), positive=True) or not _number(
                row.get("close"), positive=True):
            return _refuse("benchmark_open_or_close_invalid", date=date)
        aligned.append({"date": date, "open": row["open"], "close": row["close"]})
    stress = float(candidate["cost_stress_multiplier"])
    decisions, daily, summary = _benchmark_components(
        aligned, cost_policy, portfolio_policy, stress)
    metadata = {
        "schema": BENCHMARK_SCHEMA,
        "artifact_policy_version": policy["version"],
        "artifact_policy_sha256": sha256_obj(policy),
        "symbol": policy["benchmark_symbol"],
        "adjustment_view": policy["benchmark_adjustment_view"],
        "allocation_rule": policy["benchmark_allocation_rule"],
        "allocation_weight": float(portfolio_policy["maximum_gross_exposure"]),
        "entry_rule": policy["benchmark_entry_rule"],
        "mark_rule": policy["benchmark_mark_rule"],
        "exit_rule": policy["benchmark_exit_rule"],
        "cost_rule": policy["benchmark_cost_rule"],
        "base_cost_bps_per_side": float(cost_policy["minimum_cost_bps_per_side"]),
        "applied_cost_bps_per_side": (
            float(cost_policy["minimum_cost_bps_per_side"]) * stress),
        "candidate_result_sha256": candidate["result_sha256"],
        "candidate_content_sha256": sha256_obj(candidate),
        "candidate_daily_path_sha256": candidate["daily_path_sha256"],
        "candidate_source_backtest_plan_sha256": candidate["lineage"][
            "source_backtest_plan_sha256"],
        "candidate_portfolio_plan_sha256": candidate["lineage"][
            "portfolio_plan_sha256"],
        "candidate_dates": dates, "candidate_dates_sha256": sha256_obj(dates),
        "candidate_observation_count": len(dates), "first_date": dates[0],
        "last_date": dates[-1], "adjusted_view_sha256": adjusted_view["view_sha256"],
        "bars_payload_sha256": adjusted_view["bars_payload_sha256"],
        "aligned_spy_bars": aligned, "aligned_spy_bars_sha256": sha256_obj(aligned),
        "cost_policy": cost_policy, "portfolio_policy": portfolio_policy,
        "comparison_only": True,
        "candidate_constraint_exemptions": [
            "single_asset_position_weight_cap", "single_asset_sector_weight_cap",
            "single_asset_catalyst_weight_cap",
            "aggregate_stop_risk_not_applicable_to_passive_reference",
        ],
        "profitability_status": "not_established", "actual_orders_count": 0,
    }
    result = {
        **pb.OUTPUT_SAFETY, "tool": TOOL, "ok": True, "schema": pb.RESULT_SCHEMA,
        "experiment_id": candidate["experiment_id"],
        "variant_id": "SPY_TOTAL_RETURN_REFERENCE", "cost_stress_multiplier": stress,
        "policy_versions": {
            "cost": cost_policy["version"], "portfolio": portfolio_policy["version"],
            "plan": f"benchmark-reference-{policy['version']}",
        },
        "lineage": {
            "source_candidate_result_sha256": candidate["result_sha256"],
            "source_candidate_content_sha256": sha256_obj(candidate),
            "source_candidate_daily_path_sha256": candidate["daily_path_sha256"],
            "source_candidate_backtest_plan_sha256": candidate["lineage"][
                "source_backtest_plan_sha256"],
            "source_candidate_portfolio_plan_sha256": candidate["lineage"][
                "portfolio_plan_sha256"],
            "source_adjusted_view_sha256": adjusted_view["view_sha256"],
            "source_bars_payload_sha256": adjusted_view["bars_payload_sha256"],
            "source_spy_aligned_bars_sha256": sha256_obj(aligned),
            "artifact_policy_sha256": sha256_obj(policy),
            "cost_policy_sha256": cm.sha256_obj(cost_policy),
            "portfolio_policy_sha256": pb.sha256_obj(portfolio_policy),
        },
        "conservative_rules": [
            "exact_candidate_date_denominator_without_imputation",
            "spy_total_return_adjusted_bars_only",
            "same_cost_policy_and_stress_contract_as_candidate",
            "cost_floor_applied_on_entry_and_exit",
            "initial_allocation_capped_at_candidate_portfolio_maximum_gross_exposure",
            "single_passive_reference_not_candidate_portfolio_eligibility",
            "normalized_capital_only_without_account_or_order_data",
        ],
        "summary": summary, "decisions": decisions, "daily_path": daily,
        "decisions_sha256": pb.sha256_obj(decisions),
        "daily_path_sha256": pb.sha256_obj(daily),
        "benchmark_reference": metadata, "result_sha256": "",
    }
    result["result_sha256"] = pb.sha256_obj(_payload(result, "result_sha256"))
    result_errors = validate_benchmark(result, policy)
    if result_errors:
        return _refuse("benchmark_self_validation_failed", errors=result_errors)
    return result


def validate_benchmark(document: Any, policy: Any) -> list[str]:
    errors = [f"portfolio:{value}" for value in pb.validate_result(document)]
    errors.extend(f"policy:{value}" for value in validate_policy(policy))
    if not isinstance(document, dict) or not isinstance(policy, dict):
        return sorted(set(errors))
    if document.get("variant_id") != "SPY_TOTAL_RETURN_REFERENCE":
        errors.append("benchmark_variant_id_invalid")
    metadata = document.get("benchmark_reference")
    if not isinstance(metadata, dict) or set(metadata) != _BENCHMARK_KEYS:
        errors.append("benchmark_metadata_shape_invalid")
        return sorted(set(errors))
    fixed = {
        "schema": BENCHMARK_SCHEMA, "artifact_policy_version": policy.get("version"),
        "artifact_policy_sha256": sha256_obj(policy), "symbol": policy.get("benchmark_symbol"),
        "adjustment_view": policy.get("benchmark_adjustment_view"),
        "allocation_rule": policy.get("benchmark_allocation_rule"),
        "entry_rule": policy.get("benchmark_entry_rule"),
        "mark_rule": policy.get("benchmark_mark_rule"),
        "exit_rule": policy.get("benchmark_exit_rule"),
        "cost_rule": policy.get("benchmark_cost_rule"), "comparison_only": True,
        "candidate_constraint_exemptions": [
            "single_asset_position_weight_cap", "single_asset_sector_weight_cap",
            "single_asset_catalyst_weight_cap",
            "aggregate_stop_risk_not_applicable_to_passive_reference",
        ],
        "profitability_status": "not_established", "actual_orders_count": 0,
    }
    for key, expected in fixed.items():
        if metadata.get(key) != expected:
            errors.append(f"benchmark_metadata_rule_invalid:{key}")
    for key in (
            "candidate_result_sha256", "candidate_content_sha256",
            "candidate_daily_path_sha256", "candidate_dates_sha256",
            "candidate_source_backtest_plan_sha256",
            "candidate_portfolio_plan_sha256",
            "adjusted_view_sha256", "bars_payload_sha256", "aligned_spy_bars_sha256"):
        if not isinstance(metadata.get(key), str) or not _SHA_RE.fullmatch(metadata.get(key, "")):
            errors.append(f"benchmark_metadata_hash_invalid:{key}")
    cost_policy = metadata.get("cost_policy")
    portfolio_policy = metadata.get("portfolio_policy")
    errors.extend(f"cost_policy:{value}" for value in cm.validate_policy(cost_policy))
    errors.extend(f"portfolio_policy:{value}" for value in pb.validate_policy(portfolio_policy))
    dates = metadata.get("candidate_dates")
    aligned = metadata.get("aligned_spy_bars")
    if (not isinstance(dates, list) or not dates
            or any(not _valid_date(value) for value in dates)
            or dates != sorted(set(dates))):
        errors.append("benchmark_candidate_dates_invalid")
        dates = []
    if not isinstance(aligned, list) or not aligned:
        errors.append("benchmark_aligned_bars_invalid")
        aligned = []
    else:
        for index, row in enumerate(aligned):
            if (not isinstance(row, dict) or set(row) != {"date", "open", "close"}
                    or not _valid_date(row.get("date"))
                    or not _number(row.get("open"), positive=True)
                    or not _number(row.get("close"), positive=True)):
                errors.append(f"benchmark_aligned_bar_invalid:{index}")
    if dates and aligned and [row.get("date") for row in aligned] != dates:
        errors.append("benchmark_aligned_dates_mismatch")
    if dates:
        if metadata.get("candidate_dates_sha256") != sha256_obj(dates):
            errors.append("benchmark_candidate_dates_sha256_mismatch")
        if metadata.get("candidate_observation_count") != len(dates):
            errors.append("benchmark_candidate_count_mismatch")
        if metadata.get("first_date") != dates[0] or metadata.get("last_date") != dates[-1]:
            errors.append("benchmark_candidate_date_range_mismatch")
        if [row.get("date") for row in document.get("daily_path", [])] != dates:
            errors.append("benchmark_output_date_denominator_mismatch")
    if aligned and metadata.get("aligned_spy_bars_sha256") != sha256_obj(aligned):
        errors.append("benchmark_aligned_bars_sha256_mismatch")
    if isinstance(cost_policy, dict) and isinstance(portfolio_policy, dict):
        stress = document.get("cost_stress_multiplier")
        allowed = [float(value) for value in cost_policy.get("stress_multipliers", [])
                   if _number(value)]
        if not _number(stress) or float(stress) not in allowed:
            errors.append("benchmark_cost_stress_not_allowlisted")
        else:
            base = float(cost_policy["minimum_cost_bps_per_side"])
            if metadata.get("base_cost_bps_per_side") != base:
                errors.append("benchmark_base_cost_mismatch")
            if metadata.get("applied_cost_bps_per_side") != base * float(stress):
                errors.append("benchmark_applied_cost_mismatch")
            if metadata.get("allocation_weight") != float(
                    portfolio_policy["maximum_gross_exposure"]):
                errors.append("benchmark_allocation_weight_mismatch")
            if aligned:
                decisions, daily, summary = _benchmark_components(
                    aligned, cost_policy, portfolio_policy, float(stress))
                if document.get("decisions") != decisions:
                    errors.append("benchmark_decisions_rederivation_mismatch")
                if document.get("daily_path") != daily:
                    errors.append("benchmark_daily_path_rederivation_mismatch")
                if document.get("summary") != summary:
                    errors.append("benchmark_summary_rederivation_mismatch")
        lineage = document.get("lineage") if isinstance(document.get("lineage"), dict) else {}
        expected_lineage = {
            "source_candidate_result_sha256": metadata.get("candidate_result_sha256"),
            "source_candidate_content_sha256": metadata.get("candidate_content_sha256"),
            "source_candidate_daily_path_sha256": metadata.get("candidate_daily_path_sha256"),
            "source_candidate_backtest_plan_sha256": metadata.get(
                "candidate_source_backtest_plan_sha256"),
            "source_candidate_portfolio_plan_sha256": metadata.get(
                "candidate_portfolio_plan_sha256"),
            "source_adjusted_view_sha256": metadata.get("adjusted_view_sha256"),
            "source_bars_payload_sha256": metadata.get("bars_payload_sha256"),
            "source_spy_aligned_bars_sha256": metadata.get("aligned_spy_bars_sha256"),
            "artifact_policy_sha256": sha256_obj(policy),
            "cost_policy_sha256": cm.sha256_obj(cost_policy),
            "portfolio_policy_sha256": pb.sha256_obj(portfolio_policy),
        }
        for key, expected in expected_lineage.items():
            if lineage.get(key) != expected:
                errors.append(f"benchmark_lineage_mismatch:{key}")
        versions = (document.get("policy_versions")
                    if isinstance(document.get("policy_versions"), dict) else {})
        if versions.get("cost") != cost_policy.get("version"):
            errors.append("benchmark_cost_policy_version_mismatch")
        if versions.get("portfolio") != portfolio_policy.get("version"):
            errors.append("benchmark_portfolio_policy_version_mismatch")
        if versions.get("plan") != f"benchmark-reference-{policy.get('version')}":
            errors.append("benchmark_plan_version_mismatch")
    return sorted(set(errors))


def verify_benchmark(document: Any, candidate: Any, adjusted_view: Any,
                     cost_policy: Any, portfolio_policy: Any, policy: Any) -> list[str]:
    errors = validate_benchmark(document, policy)
    if errors:
        return errors
    rebuilt = build_benchmark(candidate, adjusted_view, cost_policy, portfolio_policy, policy)
    if not rebuilt.get("ok"):
        return [f"benchmark_rebuild_failed:{rebuilt.get('error')}"]
    return [] if canonical_bytes(rebuilt) == canonical_bytes(document) else [
        "benchmark_semantic_rebuild_mismatch"]


def _bundle_refs(bundle: dict) -> list[tuple[str, dict]]:
    refs = [("manifest", bundle["manifest"]), ("walk_forward", bundle["walk_forward"]),
            ("selection_bias", bundle["selection_bias"])]
    for index, fold in enumerate(bundle["folds"]):
        refs.extend([(f"fold[{index}].backtest", fold["backtest"]),
                     (f"fold[{index}].diagnostics", fold["diagnostics"])])
    refs.extend([("holdout.backtest", bundle["holdout"]["backtest"]),
                 ("holdout.diagnostics", bundle["holdout"]["diagnostics"])])
    for index, stress in enumerate(bundle["cost_stress"]):
        refs.extend([(f"cost_stress[{index}].backtest", stress["backtest"]),
                     (f"cost_stress[{index}].diagnostics", stress["diagnostics"])])
    return refs


def _load_ref(bundle_dir: Path, experiment_root: Path, ref: dict,
              prefix: str) -> tuple[Any | None, list[str]]:
    errors = _validate_ref(ref, prefix)
    if errors:
        return None, errors
    candidate = bundle_dir / ref["path"]
    try:
        root = experiment_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None, [f"{prefix}:artifact_path_escape_or_missing"]
    if candidate.is_symlink() or not resolved.is_file():
        return None, [f"{prefix}:artifact_not_regular_file"]
    if resolved.stat().st_size > _MAX_BYTES:
        return None, [f"{prefix}:artifact_too_large"]
    if sha256_file(resolved) != ref["sha256"]:
        return None, [f"{prefix}:artifact_sha256_mismatch"]
    try:
        return json.loads(resolved.read_text(encoding="utf-8")), []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{prefix}:artifact_unreadable:{type(exc).__name__}"]


def _receipt(bundle: dict, status: str, blockers: list[str], preregistration: dict | None,
             source_artifacts: dict | None, exported_artifacts: dict | None = None) -> dict:
    document = {
        **OUTPUT_SAFETY, "tool": TOOL, "ok": True, "schema": RECEIPT_SCHEMA,
        "bundle_id": bundle["bundle_id"], "bundle_sha256": bundle["bundle_sha256"],
        "experiment_id": bundle["experiment_id"], "status": status,
        "blockers": sorted(set(blockers)), "preregistration": preregistration,
        "source_artifacts": source_artifacts, "exported_artifacts": exported_artifacts,
        "registry_mutated": False, "performance_evaluated": False,
        "profitability_status": "not_established", "receipt_sha256": "",
    }
    document["receipt_sha256"] = sha256_obj(_payload(document, "receipt_sha256"))
    return document


def _validate_preregistration(value: Any, *, ready: bool) -> list[str]:
    if not isinstance(value, dict) or set(value) != _PREREGISTRATION_KEYS:
        return ["strategy_export_receipt_preregistration_shape_invalid"]
    errors = _validate_ref(value.get("contract"), "receipt_preregistration.contract")
    contract_sha = value.get("contract_sha256")
    if not isinstance(contract_sha, str) or not _SHA_RE.fullmatch(contract_sha):
        errors.append("strategy_export_receipt_contract_sha_invalid")
    specified = _parse_utc(value.get("specified_at_utc"))
    locked = _parse_utc(value.get("holdout_locked_at_utc"))
    if specified is None:
        errors.append("strategy_export_receipt_specified_at_invalid")
    if locked is None:
        errors.append("strategy_export_receipt_holdout_locked_at_invalid")
    for key in ("locked_before_or_at_holdout", "contract_manifest_bound",
                "all_inputs_manifest_bound"):
        if not isinstance(value.get(key), bool):
            errors.append(f"strategy_export_receipt_preregistration_flag_invalid:{key}")
    if specified is not None and locked is not None:
        expected = specified <= locked
        if value.get("locked_before_or_at_holdout") is not expected:
            errors.append("strategy_export_receipt_holdout_order_flag_mismatch")
    if ready and any(value.get(key) is not True for key in (
            "locked_before_or_at_holdout", "contract_manifest_bound",
            "all_inputs_manifest_bound")):
        errors.append("strategy_export_receipt_ready_preregistration_not_fully_bound")
    return errors


def _validate_source_artifacts(value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != _SOURCE_ARTIFACT_KEYS:
        return ["strategy_export_receipt_source_artifacts_shape_invalid"]
    errors: list[str] = []
    for key in ("candidate_portfolio", "source_backtest", "benchmark_adjusted_view"):
        errors.extend(_validate_ref(value.get(key), f"receipt_source_artifacts.{key}"))
    stress = value.get("cost_stress_multiplier")
    if not _number(stress, positive=True) or float(stress) < 1:
        errors.append("strategy_export_receipt_cost_stress_invalid")
    return errors


def _validate_exported_artifacts(value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != _EXPORTED_ARTIFACT_KEYS:
        return ["strategy_export_receipt_exported_artifacts_shape_invalid"]
    errors: list[str] = []
    for key in sorted(_EXPORTED_ARTIFACT_KEYS):
        ref = value.get(key)
        if not isinstance(ref, dict) or set(ref) != _EXPORTED_REF_KEYS:
            errors.append(f"strategy_export_receipt_exported_ref_shape_invalid:{key}")
            continue
        errors.extend(_validate_ref(
            {"path": ref.get("path"), "sha256": ref.get("sha256")},
            f"receipt_exported_artifacts.{key}"))
        result_sha = ref.get("result_sha256")
        if not isinstance(result_sha, str) or not _SHA_RE.fullmatch(result_sha):
            errors.append(f"strategy_export_receipt_exported_result_sha_invalid:{key}")
    return errors


def validate_receipt(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["strategy_export_receipt_not_object"]
    errors = []
    if set(document) != _RECEIPT_KEYS:
        errors.append("strategy_export_receipt_top_level_keys_invalid")
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"strategy_export_receipt_safety_invalid:{key}")
    if document.get("tool") != TOOL or document.get("ok") is not True or document.get(
            "schema") != RECEIPT_SCHEMA:
        errors.append("strategy_export_receipt_identity_invalid")
    for key in ("bundle_id", "experiment_id"):
        if not isinstance(document.get(key), str) or not _ID_RE.fullmatch(document.get(key, "")):
            errors.append(f"strategy_export_receipt_id_invalid:{key}")
    if not isinstance(document.get("bundle_sha256"), str) or not _SHA_RE.fullmatch(
            document.get("bundle_sha256", "")):
        errors.append("strategy_export_receipt_bundle_sha_invalid")
    status = document.get("status")
    blockers = document.get("blockers")
    if status not in _RECEIPT_STATUSES:
        errors.append("strategy_export_receipt_status_invalid")
    if (not isinstance(blockers, list)
            or any(not isinstance(value, str) or not value for value in blockers)
            or blockers != sorted(set(blockers))):
        errors.append("strategy_export_receipt_blockers_invalid")
        blockers = []
    preregistration = document.get("preregistration")
    source_artifacts = document.get("source_artifacts")
    exported_artifacts = document.get("exported_artifacts")
    if status in {"ready", "exported"}:
        if blockers:
            errors.append("strategy_export_receipt_ready_has_blockers")
        errors.extend(_validate_preregistration(preregistration, ready=True))
        errors.extend(_validate_source_artifacts(source_artifacts))
    elif status == "blocked_missing_preregistered_portfolio_export_contract":
        if preregistration is not None or source_artifacts is not None:
            errors.append("strategy_export_receipt_missing_contract_has_sources")
    elif status == "blocked_invalid_preregistered_portfolio_export_contract":
        if (preregistration is None) != (source_artifacts is None):
            errors.append("strategy_export_receipt_partial_blocked_sources")
        elif preregistration is not None:
            errors.extend(_validate_preregistration(preregistration, ready=False))
            errors.extend(_validate_source_artifacts(source_artifacts))
    if status and status.startswith("blocked_") and not blockers:
        errors.append("strategy_export_receipt_blocked_without_blocker")
    if status == "exported":
        errors.extend(_validate_exported_artifacts(exported_artifacts))
        if (isinstance(source_artifacts, dict)
                and isinstance(exported_artifacts, dict)
                and isinstance(source_artifacts.get("candidate_portfolio"), dict)
                and isinstance(exported_artifacts.get("candidate_portfolio"), dict)
                and source_artifacts["candidate_portfolio"].get("sha256")
                != exported_artifacts["candidate_portfolio"].get("sha256")):
            errors.append("strategy_export_receipt_candidate_export_source_mismatch")
    elif exported_artifacts is not None:
        errors.append("strategy_export_receipt_unexpected_exported_artifacts")
    if document.get("registry_mutated") is not False or document.get(
            "performance_evaluated") is not False or document.get(
                "profitability_status") != "not_established":
        errors.append("strategy_export_receipt_authority_or_performance_claim_invalid")
    if document.get("receipt_sha256") != sha256_obj(_payload(document, "receipt_sha256")):
        errors.append("strategy_export_receipt_sha256_mismatch")
    return sorted(set(errors))


def _expected_source_backtest_ref(bundle: dict, stress: float) -> dict | None:
    if stress == 1.0:
        return bundle["holdout"]["backtest"]
    matches = [item["backtest"] for item in bundle["cost_stress"]
               if float(item["cost_multiplier"]) == stress]
    return matches[0] if len(matches) == 1 else None


def _inspect_factory_bundle(bundle_path: Path, experiment_root: Path,
                            policy: dict) -> tuple[dict, dict | None]:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return _refuse("strategy_artifact_policy_invalid", errors=policy_errors), None
    try:
        bundle = _read(bundle_path)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse("factory_bundle_unreadable", detail=type(exc).__name__), None
    bundle_errors = rf.validate_bundle_structure(bundle)
    if bundle_errors:
        return _refuse("factory_bundle_invalid", errors=bundle_errors), None
    try:
        root_resolved = experiment_root.resolve(strict=True)
        bundle_path.resolve(strict=True).relative_to(root_resolved)
    except (OSError, ValueError):
        return _refuse("factory_bundle_outside_experiment_root"), None
    bundle_dir = bundle_path.parent
    standard_docs: dict[str, Any] = {}
    ref_errors: list[str] = []
    for name, ref in _bundle_refs(bundle):
        document, errors = _load_ref(bundle_dir, experiment_root, ref, name)
        ref_errors.extend(errors)
        if not errors:
            standard_docs[name] = document
    if ref_errors:
        return _refuse("factory_bundle_referenced_artifacts_invalid",
                       errors=sorted(set(ref_errors))), None
    manifest = standard_docs["manifest"]
    manifest_errors = em.validate_manifest(manifest)
    if manifest_errors:
        return _refuse("factory_bundle_manifest_invalid", errors=manifest_errors), None
    export = bundle.get("portfolio_export")
    if export is None:
        return _receipt(
            bundle, "blocked_missing_preregistered_portfolio_export_contract",
            ["portfolio_export_section_missing"], None, None), None
    blockers: list[str] = []
    expected_export_keys = set(policy["bundle_export_section_keys"])
    if not isinstance(export, dict) or set(export) != expected_export_keys:
        blockers.append("portfolio_export_section_shape_invalid")
        return _receipt(
            bundle, "blocked_invalid_preregistered_portfolio_export_contract",
            blockers, None, None), None
    export_docs: dict[str, Any] = {}
    for name in policy["bundle_export_section_keys"]:
        document, errors = _load_ref(
            bundle_dir, experiment_root, export[name], f"portfolio_export.{name}")
        blockers.extend(errors)
        if not errors:
            export_docs[name] = document
    contract = export_docs.get("contract")
    if contract is not None:
        blockers.extend(validate_export_contract(contract, policy))
    if blockers or not isinstance(contract, dict):
        return _receipt(
            bundle, "blocked_invalid_preregistered_portfolio_export_contract",
            blockers or ["portfolio_export_contract_missing"], None, None), None
    if contract.get("experiment_id") != bundle["experiment_id"]:
        blockers.append("contract_experiment_id_mismatch")
    if contract.get("program_id") != bundle["program_id"] or contract.get(
            "program_version") != bundle["program_version"]:
        blockers.append("contract_program_identity_mismatch")
    specified = _parse_utc(contract.get("specified_at_utc"))
    holdout_locked = _parse_utc(bundle.get("holdout_locked_at_utc"))
    if specified is None or holdout_locked is None or specified > holdout_locked:
        blockers.append("contract_specified_after_holdout_lock")
    available_manifest_hashes = {
        item.get("sha256") for item in manifest.get("data_snapshots", [])
        if isinstance(item, dict)
    }
    if export["contract"]["sha256"] not in available_manifest_hashes:
        blockers.append("contract_not_fingerprinted_in_manifest")
    input_docs: dict[str, Any] = {}
    for name, ref in contract["input_refs"].items():
        document, errors = _load_ref(bundle_dir, experiment_root, ref, f"contract_input.{name}")
        blockers.extend(errors)
        if ref["sha256"] not in available_manifest_hashes:
            blockers.append(f"contract_input_not_fingerprinted_in_manifest:{name}")
        if not errors:
            input_docs[name] = document
    candidate = export_docs.get("candidate_portfolio")
    source_backtest = export_docs.get("source_backtest")
    if candidate is not None:
        blockers.extend(f"candidate:{value}" for value in pb.validate_result(candidate))
    if source_backtest is not None:
        blockers.extend(f"source_backtest:{value}" for value in qb.validate_result(
            source_backtest))
    cost_policy = input_docs.get("cost_policy")
    portfolio_policy = input_docs.get("portfolio_policy")
    source_plans = input_docs.get("source_plans")
    portfolio_plan = input_docs.get("portfolio_plan")
    source_bars = input_docs.get("source_bars")
    adjusted_view = input_docs.get("benchmark_adjusted_view")
    if cost_policy is not None:
        blockers.extend(f"cost_policy:{value}" for value in cm.validate_policy(cost_policy))
    if portfolio_policy is not None:
        blockers.extend(f"portfolio_policy:{value}" for value in pb.validate_policy(
            portfolio_policy))
    if source_bars is not None:
        blockers.extend(f"source_bars:{value}" for value in qf.validate_bars_archive(
            source_bars))
    if all(value is not None for value in (
            portfolio_plan, source_plans, cost_policy, portfolio_policy)):
        blockers.extend(f"portfolio_plan:{value}" for value in pb.validate_plan(
            portfolio_plan, source_plans, cost_policy, portfolio_policy))
    if adjusted_view is not None:
        blockers.extend(f"benchmark_adjusted_view:{value}" for value in
                        ap.validate_view_artifact(adjusted_view))
        if adjusted_view.get("view") != policy["benchmark_adjustment_view"]:
            blockers.append("benchmark_adjusted_view_not_total_return")
        if policy["benchmark_symbol"] not in adjusted_view.get("bars", {}):
            blockers.append("benchmark_symbol_missing_from_adjusted_view")
    stress = float(contract["cost_stress_multiplier"])
    expected_source_ref = _expected_source_backtest_ref(bundle, stress)
    if expected_source_ref is None:
        blockers.append("contract_cost_stress_not_uniquely_present_in_bundle")
    elif export["source_backtest"] != expected_source_ref:
        blockers.append("export_source_backtest_not_bound_to_declared_stress")
    if candidate is not None and cost_policy is not None and portfolio_policy is not None:
        blockers.extend(_candidate_contract_errors(candidate, cost_policy, portfolio_policy))
        if float(candidate.get("cost_stress_multiplier", -1)) != stress:
            blockers.append("candidate_cost_stress_contract_mismatch")
        if candidate.get("experiment_id") != bundle["experiment_id"]:
            blockers.append("candidate_experiment_id_mismatch")
    if all(value is not None for value in (
            candidate, source_backtest, source_bars, source_plans, portfolio_plan,
            cost_policy, portfolio_policy)):
        lineage = candidate.get("lineage", {})
        file_bindings = {
            "backtest_file_sha256": export["source_backtest"]["sha256"],
            "bars_file_sha256": contract["input_refs"]["source_bars"]["sha256"],
            "source_plans_file_sha256": contract["input_refs"]["source_plans"]["sha256"],
            "portfolio_plan_file_sha256": contract["input_refs"]["portfolio_plan"]["sha256"],
            "cost_policy_file_sha256": contract["input_refs"]["cost_policy"]["sha256"],
            "portfolio_policy_file_sha256": contract["input_refs"]["portfolio_policy"][
                "sha256"],
            "manifest_file_sha256": bundle["manifest"]["sha256"],
        }
        object_bindings = {
            "source_backtest_result_sha256": source_backtest.get("result_sha256"),
            "source_backtest_plan_sha256": qb._sha256_obj(source_plans),
            "portfolio_plan_sha256": pb.sha256_obj(portfolio_plan),
            "cost_policy_sha256": cm.sha256_obj(cost_policy),
            "portfolio_policy_sha256": pb.sha256_obj(portfolio_policy),
            "experiment_manifest_sha256": qb._sha256_obj(manifest),
        }
        for key, expected in {**file_bindings, **object_bindings}.items():
            if lineage.get(key) != expected:
                blockers.append(f"candidate_lineage_binding_mismatch:{key}")
    preregistration = {
        "contract": export["contract"], "contract_sha256": contract["contract_sha256"],
        "specified_at_utc": contract["specified_at_utc"],
        "holdout_locked_at_utc": bundle["holdout_locked_at_utc"],
        "locked_before_or_at_holdout": specified is not None and holdout_locked is not None
        and specified <= holdout_locked,
        "contract_manifest_bound": export["contract"]["sha256"] in available_manifest_hashes,
        "all_inputs_manifest_bound": all(
            ref["sha256"] in available_manifest_hashes
            for ref in contract["input_refs"].values()),
    }
    source_artifacts = {
        "candidate_portfolio": export["candidate_portfolio"],
        "source_backtest": export["source_backtest"],
        "benchmark_adjusted_view": contract["input_refs"]["benchmark_adjusted_view"],
        "cost_stress_multiplier": stress,
    }
    if blockers:
        return _receipt(
            bundle, "blocked_invalid_preregistered_portfolio_export_contract",
            blockers, preregistration, source_artifacts), None
    context = {
        "bundle": bundle, "bundle_dir": bundle_dir, "candidate": candidate,
        "adjusted_view": adjusted_view, "cost_policy": cost_policy,
        "portfolio_policy": portfolio_policy,
        "candidate_source_path": bundle_dir / export["candidate_portfolio"]["path"],
    }
    return _receipt(bundle, "ready", [], preregistration, source_artifacts), context


def inspect_factory_bundle(bundle_path: Path, experiment_root: Path, policy: dict) -> dict:
    receipt, _context = _inspect_factory_bundle(bundle_path, experiment_root, policy)
    if receipt.get("schema") == RECEIPT_SCHEMA:
        errors = validate_receipt(receipt)
        if errors:
            return _refuse("strategy_export_receipt_self_validation_failed", errors=errors)
    return receipt


def export_factory_bundle(bundle_path: Path, experiment_root: Path, output_root: Path,
                          policy: dict) -> dict:
    receipt, context = _inspect_factory_bundle(bundle_path, experiment_root, policy)
    if receipt.get("status") != "ready" or context is None:
        return receipt
    benchmark = build_benchmark(
        context["candidate"], context["adjusted_view"], context["cost_policy"],
        context["portfolio_policy"], policy)
    if not benchmark.get("ok"):
        return _refuse("factory_benchmark_build_failed", detail=benchmark.get("error"),
                       errors=benchmark.get("errors"))
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        return _refuse("strategy_artifact_output_root_invalid")
    destination = output_root / context["bundle"]["bundle_id"]
    if destination.exists():
        return _refuse("strategy_artifact_package_already_exists")
    temporary: Path | None = Path(tempfile.mkdtemp(
        prefix=f".{context['bundle']['bundle_id']}.tmp-", dir=str(output_root)))
    try:
        candidate_destination = temporary / "candidate_portfolio.json"
        shutil.copyfile(context["candidate_source_path"], candidate_destination)
        benchmark_destination = temporary / "spy_benchmark_reference.json"
        _write_json(benchmark_destination, benchmark)
        exported = {
            "candidate_portfolio": {
                "path": "candidate_portfolio.json",
                "sha256": sha256_file(candidate_destination),
                "result_sha256": context["candidate"]["result_sha256"],
            },
            "benchmark_reference": {
                "path": "spy_benchmark_reference.json",
                "sha256": sha256_file(benchmark_destination),
                "result_sha256": benchmark["result_sha256"],
            },
        }
        exported_receipt = _receipt(
            context["bundle"], "exported", [], receipt["preregistration"],
            receipt["source_artifacts"], exported)
        receipt_errors = validate_receipt(exported_receipt)
        if receipt_errors:
            return _refuse("strategy_export_receipt_self_validation_failed",
                           errors=receipt_errors)
        _write_json(temporary / "export_receipt.json", exported_receipt)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return {
        **OUTPUT_SAFETY, "tool": TOOL, "ok": True, "status": "exported",
        "package_path": str(destination), "receipt": exported_receipt,
    }


def verify_export_package(bundle_path: Path, experiment_root: Path, output_root: Path,
                          policy: dict) -> dict:
    """Verify an existing write-once package against its source bundle and rederived benchmark."""
    ready_receipt, context = _inspect_factory_bundle(bundle_path, experiment_root, policy)
    if ready_receipt.get("status") != "ready" or context is None:
        return ready_receipt
    try:
        root = output_root.resolve(strict=True)
    except OSError:
        return _refuse("strategy_artifact_output_root_missing")
    if output_root.is_symlink() or not root.is_dir():
        return _refuse("strategy_artifact_output_root_invalid")
    destination = output_root / context["bundle"]["bundle_id"]
    try:
        resolved_destination = destination.resolve(strict=True)
        resolved_destination.relative_to(root)
    except (OSError, ValueError):
        return _refuse("strategy_artifact_package_missing_or_path_escape")
    if destination.is_symlink() or not resolved_destination.is_dir():
        return _refuse("strategy_artifact_package_invalid")
    expected_names = {
        "candidate_portfolio.json", "spy_benchmark_reference.json", "export_receipt.json",
    }
    try:
        names = {path.name for path in resolved_destination.iterdir()}
    except OSError as exc:
        return _refuse("strategy_artifact_package_unreadable", detail=type(exc).__name__)
    if names != expected_names:
        return _refuse("strategy_artifact_package_file_set_invalid")
    candidate_path = resolved_destination / "candidate_portfolio.json"
    benchmark_path = resolved_destination / "spy_benchmark_reference.json"
    receipt_path = resolved_destination / "export_receipt.json"
    try:
        candidate = _read(candidate_path)
        benchmark = _read(benchmark_path)
        exported_receipt = _read(receipt_path)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse("strategy_artifact_package_artifact_unreadable",
                       detail=type(exc).__name__)
    errors: list[str] = []
    source_ref = context["bundle"]["portfolio_export"]["candidate_portfolio"]
    if sha256_file(candidate_path) != source_ref["sha256"]:
        errors.append("staged_candidate_file_sha256_mismatch")
    if candidate != context["candidate"]:
        errors.append("staged_candidate_content_mismatch")
    errors.extend(f"staged_candidate:{value}" for value in pb.validate_result(candidate))
    errors.extend(f"staged_benchmark:{value}" for value in validate_benchmark(
        benchmark, policy))
    errors.extend(f"staged_benchmark_verify:{value}" for value in verify_benchmark(
        benchmark, context["candidate"], context["adjusted_view"],
        context["cost_policy"], context["portfolio_policy"], policy))
    errors.extend(f"staged_receipt:{value}" for value in validate_receipt(exported_receipt))
    exported_artifacts = {
        "candidate_portfolio": {
            "path": "candidate_portfolio.json",
            "sha256": sha256_file(candidate_path),
            "result_sha256": candidate.get("result_sha256") if isinstance(candidate, dict)
            else None,
        },
        "benchmark_reference": {
            "path": "spy_benchmark_reference.json",
            "sha256": sha256_file(benchmark_path),
            "result_sha256": benchmark.get("result_sha256") if isinstance(benchmark, dict)
            else None,
        },
    }
    expected_receipt = _receipt(
        context["bundle"], "exported", [], ready_receipt["preregistration"],
        ready_receipt["source_artifacts"], exported_artifacts)
    if exported_receipt != expected_receipt:
        errors.append("staged_export_receipt_rederivation_mismatch")
    if errors:
        return _refuse("strategy_artifact_package_verification_failed",
                       errors=sorted(set(errors)))
    return {
        **OUTPUT_SAFETY, "tool": TOOL, "ok": True, "status": "verified_existing",
        "package_path": str(resolved_destination), "receipt": exported_receipt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic SPY benchmark and preregistered factory artifact bridge.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    build = sub.add_parser("build-benchmark")
    build.add_argument("--candidate", required=True)
    build.add_argument("--adjusted-view", required=True)
    build.add_argument("--cost-policy", default=str(COST_POLICY_DEFAULT))
    build.add_argument("--portfolio-policy", default=str(PORTFOLIO_POLICY_DEFAULT))
    build.add_argument("--policy", default=str(POLICY_DEFAULT))
    build.add_argument("--out", required=True)
    validate = sub.add_parser("validate-benchmark")
    validate.add_argument("--artifact", required=True)
    validate.add_argument("--policy", default=str(POLICY_DEFAULT))
    verify = sub.add_parser("verify-benchmark")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--adjusted-view", required=True)
    verify.add_argument("--cost-policy", default=str(COST_POLICY_DEFAULT))
    verify.add_argument("--portfolio-policy", default=str(PORTFOLIO_POLICY_DEFAULT))
    verify.add_argument("--policy", default=str(POLICY_DEFAULT))
    inspect = sub.add_parser("inspect-bundle")
    inspect.add_argument("--bundle", required=True)
    inspect.add_argument("--experiment-root", required=True)
    inspect.add_argument("--policy", default=str(POLICY_DEFAULT))
    inspect.add_argument("--out")
    export = sub.add_parser("export-bundle")
    export.add_argument("--bundle", required=True)
    export.add_argument("--experiment-root", required=True)
    export.add_argument("--output-root", default=str(EXPORT_ROOT_DEFAULT))
    export.add_argument("--policy", default=str(POLICY_DEFAULT))
    receipt = sub.add_parser("validate-receipt")
    receipt.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate-receipt":
            document = _read(Path(args.artifact))
            errors = validate_receipt(document)
            output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                      "receipt_sha256": document.get("receipt_sha256")}
        else:
            policy, policy_errors = load_policy(Path(args.policy))
            if policy is None or policy_errors:
                output = _refuse("strategy_artifact_policy_invalid", errors=policy_errors)
            elif args.command == "build-benchmark":
                output = build_benchmark(
                    _read(Path(args.candidate)), _read(Path(args.adjusted_view)),
                    _read(Path(args.cost_policy)), _read(Path(args.portfolio_policy)), policy)
                if output.get("ok"):
                    _atomic_write_once(Path(args.out), output)
                    output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": True,
                              "written": str(Path(args.out)),
                              "result_sha256": output["result_sha256"],
                              "summary": output["summary"]}
            elif args.command == "validate-benchmark":
                document = _read(Path(args.artifact))
                errors = validate_benchmark(document, policy)
                output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors,
                          "errors": errors, "result_sha256": document.get("result_sha256")}
            elif args.command == "verify-benchmark":
                document = _read(Path(args.artifact))
                errors = verify_benchmark(
                    document, _read(Path(args.candidate)), _read(Path(args.adjusted_view)),
                    _read(Path(args.cost_policy)), _read(Path(args.portfolio_policy)), policy)
                output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors,
                          "errors": errors, "result_sha256": document.get("result_sha256")}
            elif args.command == "inspect-bundle":
                output = inspect_factory_bundle(
                    Path(args.bundle), Path(args.experiment_root), policy)
                if args.out and output.get("schema") == RECEIPT_SCHEMA:
                    _atomic_write_once(Path(args.out), output)
            else:
                output = export_factory_bundle(
                    Path(args.bundle), Path(args.experiment_root), Path(args.output_root), policy)
        print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
        return 0 if output.get("ok") else 1
    except Exception as exc:
        print(json.dumps(_refuse(type(exc).__name__, detail=str(exc)[:400]),
                         indent=2, sort_keys=True, allow_nan=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
