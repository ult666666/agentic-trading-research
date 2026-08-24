#!/usr/bin/env python3
"""Capital-constrained overlapping portfolio diagnostics for offline quant research.

Consumes a validated hypothetical backtest, its original frozen plans, a point-in-time bars
archive, predecision allocation metadata, and an experiment manifest that already fingerprints
every allocation/policy input. Capital is normalized to 1.0. No account data, share recommendation,
broker operation, current order preview, or production activation is possible.
"""
from __future__ import annotations

import argparse
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
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from quant import backtest as qb  # noqa: E402
from quant import cost_model as cm  # noqa: E402
from quant import experiment_manifest as em  # noqa: E402
from quant import features as qf  # noqa: E402

TOOL = "quant.portfolio_backtest"
POLICY_SCHEMA = "quant_portfolio_policy/1.0"
PLAN_SCHEMA = "quant_portfolio_plan/1.0"
RESULT_SCHEMA = "quant_portfolio_backtest/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_portfolio_policy.json"
COST_POLICY_DEFAULT = ROOT / "config" / "quant_cost_policy.json"
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,95}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_NUM = (int, float)
_MAX_BYTES = 128 * 1024 * 1024

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "strategy_change_allowed": False,
    "broker_access": "none",
    "normalized_capital_only": True,
    "account_data_allowed": False,
    "share_recommendation_allowed": False,
    "actual_order_simulation": False,
}
OUTPUT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "normalized_capital_only": True,
    "contains_account_data": False,
    "share_recommendation": False,
    "actual_order_simulation": False,
}
_POLICY_KEYS = {
    "schema", "version", "allocation_method", "initial_normalized_nav",
    "maximum_gross_exposure", "maximum_net_exposure", "maximum_position_weight",
    "minimum_position_weight", "maximum_sector_weight", "maximum_catalyst_weight",
    "maximum_aggregate_stop_risk", "maximum_open_positions",
    "maximum_entries_per_session", "one_position_per_symbol", "direction",
    "leverage_allowed", "shorting_allowed", "entry_order_rule", "exit_proceeds_rule",
    "mark_to_market_rule", "cost_application_rule", "missing_data_rule", "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "allocation_method": "priority_order_constrained_requested_weight",
    "initial_normalized_nav": 1.0,
    "one_position_per_symbol": True,
    "direction": "long_only",
    "leverage_allowed": False,
    "shorting_allowed": False,
    "entry_order_rule": "ascending_priority_then_scenario_id",
    "exit_proceeds_rule": "not_reusable_for_same_session_entries",
    "mark_to_market_rule": "daily_close_with_modeled_exit_price_on_exit_date",
    "cost_application_rule": "maximum_of_backtest_floor_and_predecision_cost_estimate",
    "missing_data_rule": "skip_candidate_or_fail_artifact_never_impute_zero",
}
_PLAN_KEYS = {
    "schema", "version", "source_backtest_plan_sha256", "cost_policy_sha256",
    "portfolio_policy_sha256", "allocations", "safety",
}
_ALLOCATION_KEYS = {
    "scenario_id", "symbol", "event_date", "specified_at_utc", "priority",
    "requested_weight", "sector", "catalyst_type", "cost_context",
}
_SOURCE_HASH_KEYS = {
    "backtest_file_sha256", "bars_file_sha256", "source_plans_file_sha256",
    "portfolio_plan_file_sha256", "cost_policy_file_sha256",
    "portfolio_policy_file_sha256", "manifest_file_sha256",
}


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
    if positive and value <= 0:
        return False
    if nonnegative and value < 0:
        return False
    return True


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    result = round(float(value), 10)
    return 0.0 if result == 0 else result


def _date(value: Any):
    return qf._parse_date(value)


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input_not_regular_file")
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _refuse(error: str, **extra) -> dict:
    return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False, "error": error, **extra}


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["portfolio_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("portfolio_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"portfolio_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            document.get("version", "")):
        errors.append("portfolio_policy_version_invalid")
    fractions = (
        "maximum_gross_exposure", "maximum_net_exposure", "maximum_position_weight",
        "minimum_position_weight", "maximum_sector_weight", "maximum_catalyst_weight",
        "maximum_aggregate_stop_risk",
    )
    for key in fractions:
        if not _number(document.get(key), positive=True) or float(document.get(key, 2)) > 1:
            errors.append(f"portfolio_policy_fraction_invalid:{key}")
    if all(_number(document.get(key), positive=True) for key in fractions):
        if document["maximum_net_exposure"] > document["maximum_gross_exposure"]:
            errors.append("portfolio_policy_net_above_gross")
        if document["minimum_position_weight"] > document["maximum_position_weight"]:
            errors.append("portfolio_policy_minimum_above_maximum_position")
        for key in ("maximum_sector_weight", "maximum_catalyst_weight",
                    "maximum_gross_exposure"):
            if document["maximum_position_weight"] > document[key]:
                errors.append(f"portfolio_policy_position_above:{key}")
    for key in ("maximum_open_positions", "maximum_entries_per_session"):
        if not isinstance(document.get(key), int) or isinstance(document.get(key), bool) or (
                document[key] < 1):
            errors.append(f"portfolio_policy_integer_invalid:{key}")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("portfolio_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read(path)
    except Exception:
        return None, ["portfolio_policy_unreadable"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    return {
        **OUTPUT_SAFETY, "tool": TOOL, "ok": policy is not None and not errors,
        "valid": policy is not None and not errors, "errors": errors,
        "version": policy.get("version") if isinstance(policy, dict) else None,
        "policy_sha256": sha256_obj(policy) if isinstance(policy, dict) else None,
    }


def validate_plan(plan: Any, source_plans: dict, cost_policy: dict,
                  portfolio_policy: dict) -> list[str]:
    if not isinstance(plan, dict):
        return ["portfolio_plan_not_object"]
    errors: list[str] = []
    if set(plan) != _PLAN_KEYS:
        errors.append("portfolio_plan_top_level_keys_invalid")
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append("portfolio_plan_schema_invalid")
    if not isinstance(plan.get("version"), str) or not _SEMVER_RE.fullmatch(
            plan.get("version", "")):
        errors.append("portfolio_plan_version_invalid")
    if plan.get("source_backtest_plan_sha256") != qb._sha256_obj(source_plans):
        errors.append("portfolio_plan_source_plan_hash_mismatch")
    if plan.get("cost_policy_sha256") != cm.sha256_obj(cost_policy):
        errors.append("portfolio_plan_cost_policy_hash_mismatch")
    if plan.get("portfolio_policy_sha256") != sha256_obj(portfolio_policy):
        errors.append("portfolio_plan_policy_hash_mismatch")
    if plan.get("safety") != POLICY_SAFETY:
        errors.append("portfolio_plan_safety_invalid")
    source_scenarios = source_plans.get("scenarios") if isinstance(source_plans, dict) else None
    if not isinstance(source_scenarios, list) or not source_scenarios:
        return sorted(set(errors + ["source_backtest_scenarios_missing"]))
    source_by_id = {row.get("scenario_id"): row for row in source_scenarios
                    if isinstance(row, dict)}
    allocations = plan.get("allocations")
    if not isinstance(allocations, list) or not allocations:
        return sorted(set(errors + ["portfolio_plan_allocations_missing"]))
    seen: set[str] = set()
    for index, allocation in enumerate(allocations):
        prefix = f"allocation[{index}]"
        if not isinstance(allocation, dict) or set(allocation) != _ALLOCATION_KEYS:
            errors.append(f"{prefix}.shape_invalid")
            continue
        scenario_id = allocation.get("scenario_id")
        source = source_by_id.get(scenario_id)
        if source is None:
            errors.append(f"{prefix}.scenario_not_in_source_plan")
            continue
        if scenario_id in seen:
            errors.append(f"{prefix}.scenario_duplicate")
        seen.add(scenario_id)
        for key in ("symbol", "event_date"):
            if allocation.get(key) != source.get(key):
                errors.append(f"{prefix}.{key}_mismatch")
        if not isinstance(allocation.get("symbol"), str) or not _SYMBOL_RE.fullmatch(
                allocation.get("symbol", "")):
            errors.append(f"{prefix}.symbol_invalid")
        if _date(allocation.get("event_date")) is None:
            errors.append(f"{prefix}.event_date_invalid")
        specified = qf._parse_utc(allocation.get("specified_at_utc"))
        source_specified = qf._parse_utc(source.get("specified_at_utc"))
        if specified is None or source_specified is None or specified > source_specified:
            errors.append(f"{prefix}.specified_after_source_plan")
        priority = allocation.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
            errors.append(f"{prefix}.priority_invalid")
        weight = allocation.get("requested_weight")
        if not _number(weight, positive=True) or float(weight) > 1:
            errors.append(f"{prefix}.requested_weight_invalid")
        for key in ("sector", "catalyst_type"):
            if not isinstance(allocation.get(key), str) or not _TOKEN_RE.fullmatch(
                    allocation.get(key, "")):
                errors.append(f"{prefix}.{key}_invalid")
        context = allocation.get("cost_context")
        errors.extend(f"{prefix}.{value}" for value in cm.validate_context(context))
        if isinstance(context, dict) and context.get("decision_cutoff_utc") != allocation.get(
                "specified_at_utc"):
            errors.append(f"{prefix}.cost_cutoff_not_allocation_specified_time")
    if set(source_by_id) != seen:
        errors.append("portfolio_plan_not_complete_for_all_source_scenarios")
    return sorted(set(errors))


def _source_binding_errors(backtest: dict, bars: dict, source_plans: dict, plan: dict,
                           manifest: dict, cost_policy: dict, portfolio_policy: dict,
                           source_hashes: dict) -> list[str]:
    errors: list[str] = []
    if set(source_hashes) != _SOURCE_HASH_KEYS or any(
            not isinstance(value, str) or not _SHA_RE.fullmatch(value)
            for value in source_hashes.values()):
        return ["source_hashes_invalid"]
    if source_hashes["bars_file_sha256"] != backtest.get("lineage", {}).get(
            "bars_file_sha256"):
        errors.append("bars_file_not_backtest_source")
    if source_hashes["source_plans_file_sha256"] != backtest.get("lineage", {}).get(
            "plans_file_sha256"):
        errors.append("source_plans_file_not_backtest_source")
    if qb._sha256_obj(bars) != backtest.get("lineage", {}).get("bars_payload_sha256"):
        errors.append("bars_payload_not_backtest_source")
    if qb._sha256_obj(source_plans) != backtest.get("lineage", {}).get(
            "plans_payload_sha256"):
        errors.append("source_plan_payload_not_backtest_source")
    if qb._sha256_obj(manifest) != backtest.get("lineage", {}).get(
            "experiment_manifest_sha256"):
        errors.append("manifest_not_backtest_source")
    manifest_errors = em.validate_manifest(manifest)
    errors.extend(f"manifest:{value}" for value in manifest_errors)
    predecision_hashes = {
        source_hashes["bars_file_sha256"], source_hashes["source_plans_file_sha256"],
        source_hashes["portfolio_plan_file_sha256"],
        source_hashes["cost_policy_file_sha256"],
        source_hashes["portfolio_policy_file_sha256"],
    }
    available = {row.get("sha256") for row in manifest.get("data_snapshots", [])
                 if isinstance(row, dict)}
    if not predecision_hashes.issubset(available):
        errors.append("manifest_missing_predecision_portfolio_inputs")
    if plan.get("cost_policy_sha256") != cm.sha256_obj(cost_policy):
        errors.append("cost_policy_object_hash_mismatch")
    if plan.get("portfolio_policy_sha256") != sha256_obj(portfolio_policy):
        errors.append("portfolio_policy_object_hash_mismatch")
    source_ids = {row.get("scenario_id") for row in source_plans.get("scenarios", [])
                  if isinstance(row, dict)}
    result_ids = {row.get("scenario_id") for row in backtest.get("records", [])
                  if isinstance(row, dict)}
    if source_ids != result_ids:
        errors.append("backtest_result_not_complete_for_source_plan_denominator")
    return sorted(set(errors))


def _bar_maps(bars: dict) -> dict[str, dict[str, dict]]:
    return {symbol: {row["date"]: row for row in rows}
            for symbol, rows in bars.get("bars", {}).items()}


def _market_value(position: dict, price: float) -> float:
    return float(position["units"]) * float(price)


def _exposures(positions: dict[str, dict], values: dict[str, float]) -> tuple[float, dict, dict]:
    gross = sum(values.values())
    sectors: dict[str, float] = {}
    catalysts: dict[str, float] = {}
    for scenario_id, position in positions.items():
        value = values.get(scenario_id, 0.0)
        sectors[position["sector"]] = sectors.get(position["sector"], 0.0) + value
        catalysts[position["catalyst_type"]] = (
            catalysts.get(position["catalyst_type"], 0.0) + value)
    return gross, sectors, catalysts


def _decision_base(record: dict, allocation: dict) -> dict:
    return {
        "scenario_id": record["scenario_id"],
        "symbol": record["symbol"],
        "event_date": record["event_date"],
        "source_simulation_status": record["simulation_status"],
        "priority": allocation["priority"],
        "requested_weight": allocation["requested_weight"],
        "sector": allocation["sector"],
        "catalyst_type": allocation["catalyst_type"],
        "status": None,
        "reason": None,
        "allocated_weight": None,
        "applied_cost_bps_per_side": None,
        "cost_estimate_sha256": None,
        "binding_constraints": [],
        "entry_date": record.get("entry_date"),
        "exit_date": record.get("exit_date"),
        "fill_guaranteed": False,
        "actual_order_exists": False,
        "decision_sha256": "",
    }


def _seal_decision(document: dict) -> dict:
    document["decision_sha256"] = sha256_obj(_payload(document, "decision_sha256"))
    return document


def _prevalidate_paths(completed: list[dict], maps: dict[str, dict[str, dict]],
                       calendar: list[str]) -> list[str]:
    errors: list[str] = []
    calendar_index = {value: index for index, value in enumerate(calendar)}
    for record in completed:
        symbol = record["symbol"]
        entry, exit_date = record.get("entry_date"), record.get("exit_date")
        if symbol not in maps:
            errors.append(f"portfolio_symbol_bars_missing:{record['scenario_id']}")
            continue
        if entry not in calendar_index or exit_date not in calendar_index:
            errors.append(f"portfolio_entry_or_exit_not_trading_session:{record['scenario_id']}")
            continue
        if calendar_index[exit_date] < calendar_index[entry]:
            errors.append(f"portfolio_exit_before_entry:{record['scenario_id']}")
            continue
        for date in calendar[calendar_index[entry]:calendar_index[exit_date] + 1]:
            if date not in maps[symbol]:
                errors.append(f"portfolio_mark_bar_missing:{record['scenario_id']}:{date}")
    return sorted(set(errors))


def run_portfolio(backtest: dict, bars: dict, source_plans: dict, plan: dict,
                  manifest: dict, cost_policy: dict, portfolio_policy: dict,
                  source_hashes: dict, stress_multiplier: float = 1.0) -> dict:
    errors = [f"backtest:{value}" for value in qb.validate_result(backtest)]
    errors.extend(f"bars:{value}" for value in qf.validate_bars_archive(bars))
    errors.extend(f"cost_policy:{value}" for value in cm.validate_policy(cost_policy))
    errors.extend(f"portfolio_policy:{value}" for value in validate_policy(portfolio_policy))
    errors.extend(f"portfolio_plan:{value}" for value in validate_plan(
        plan, source_plans, cost_policy, portfolio_policy))
    errors.extend(_source_binding_errors(backtest, bars, source_plans, plan, manifest,
                                         cost_policy, portfolio_policy, source_hashes))
    if errors:
        return _refuse("portfolio_inputs_invalid", errors=sorted(set(errors)))
    if float(stress_multiplier) not in [float(value) for value in cost_policy["stress_multipliers"]]:
        return _refuse("portfolio_stress_multiplier_not_allowed")

    records = {row["scenario_id"]: row for row in backtest["records"]}
    allocations = {row["scenario_id"]: row for row in plan["allocations"]}
    completed = [row for row in records.values() if row["simulation_status"] == "completed"]
    if not completed:
        return _refuse("portfolio_no_completed_hypothetical_paths")
    maps = _bar_maps(bars)
    calendar = [row["date"] for row in bars["bars"][qf.BENCHMARK]]
    path_errors = _prevalidate_paths(completed, maps, calendar)
    if path_errors:
        return _refuse("portfolio_mark_to_market_path_invalid", errors=path_errors)
    first_date = min(row["entry_date"] for row in completed)
    last_date = max(row["exit_date"] for row in completed)
    calendar = [value for value in calendar if first_date <= value <= last_date]

    decisions: dict[str, dict] = {}
    estimated_costs: dict[str, dict] = {}
    for scenario_id, record in sorted(records.items()):
        allocation = allocations[scenario_id]
        decision = _decision_base(record, allocation)
        if record["simulation_status"] != "completed":
            decision["status"] = "not_allocated"
            decision["reason"] = f"source_{record['simulation_status']}"
            decisions[scenario_id] = _seal_decision(decision)
            continue
        estimate = cm.estimate(allocation["cost_context"], cost_policy, stress_multiplier)
        if not estimate.get("ok"):
            return _refuse("portfolio_cost_estimate_failed", scenario_id=scenario_id,
                           detail=estimate.get("error"))
        estimated_costs[scenario_id] = estimate
        decision["cost_estimate_sha256"] = estimate["estimate_sha256"]
        if not estimate["usable_for_portfolio_backtest"]:
            decision["status"] = "not_allocated"
            decision["reason"] = f"cost_{estimate['status']}"
            decisions[scenario_id] = _seal_decision(decision)
    entries_by_date: dict[str, list[dict]] = {}
    for record in completed:
        if record["scenario_id"] not in decisions:
            entries_by_date.setdefault(record["entry_date"], []).append(record)

    initial_nav = float(portfolio_policy["initial_normalized_nav"])
    cash = initial_nav
    previous_nav = initial_nav
    peak_nav = initial_nav
    positions: dict[str, dict] = {}
    last_values: dict[str, float] = {}
    daily: list[dict] = []
    total_entry_notional = 0.0
    total_exit_notional = 0.0
    total_modeled_cost = 0.0

    for date in calendar:
        opening_nav = cash + sum(last_values.values())
        opening_values = dict(last_values)
        current_gross, sectors, catalysts = _exposures(positions, opening_values)
        current_risk = sum(position["planned_stop_risk"] for position in positions.values())
        entries_today: list[str] = []
        exits_today: list[str] = []
        entry_gross_peak = current_gross
        candidates = sorted(entries_by_date.get(date, []), key=lambda row: (
            allocations[row["scenario_id"]]["priority"], row["scenario_id"]))
        for record in candidates:
            scenario_id = record["scenario_id"]
            allocation = allocations[scenario_id]
            decision = _decision_base(record, allocation)
            estimate = estimated_costs[scenario_id]
            decision["cost_estimate_sha256"] = estimate["estimate_sha256"]
            baseline_cost = float(record["transaction_cost_bps_per_side"])
            applied_cost = max(baseline_cost, float(estimate["modeled_cost_bps_per_side"]))
            decision["applied_cost_bps_per_side"] = _round(applied_cost)
            hard_reason = None
            if len(entries_today) >= portfolio_policy["maximum_entries_per_session"]:
                hard_reason = "maximum_entries_per_session"
            elif len(positions) >= portfolio_policy["maximum_open_positions"]:
                hard_reason = "maximum_open_positions"
            elif portfolio_policy["one_position_per_symbol"] and any(
                    item["symbol"] == record["symbol"] for item in positions.values()):
                hard_reason = "symbol_already_open"
            if hard_reason:
                decision["status"] = "not_allocated"
                decision["reason"] = hard_reason
                decisions[scenario_id] = _seal_decision(decision)
                continue

            cost_rate = applied_cost / 10000.0
            entry_price = float(record["entry_price"])
            stop_price = float(record["stop_price"])
            unit_stop_risk = (entry_price - stop_price) / entry_price + 2.0 * cost_rate
            if unit_stop_risk <= 0:
                return _refuse("portfolio_unit_stop_risk_invalid", scenario_id=scenario_id)
            requested = float(allocation["requested_weight"]) * opening_nav
            caps = {
                "maximum_position_weight": (
                    float(portfolio_policy["maximum_position_weight"]) * opening_nav),
                "maximum_gross_exposure": max(
                    0.0, float(portfolio_policy["maximum_gross_exposure"]) * opening_nav
                    - current_gross),
                "maximum_net_exposure": max(
                    0.0, float(portfolio_policy["maximum_net_exposure"]) * opening_nav
                    - current_gross),
                "maximum_sector_weight": max(
                    0.0, float(portfolio_policy["maximum_sector_weight"]) * opening_nav
                    - sectors.get(allocation["sector"], 0.0)),
                "maximum_catalyst_weight": max(
                    0.0, float(portfolio_policy["maximum_catalyst_weight"]) * opening_nav
                    - catalysts.get(allocation["catalyst_type"], 0.0)),
                "maximum_aggregate_stop_risk": max(
                    0.0, (float(portfolio_policy["maximum_aggregate_stop_risk"])
                          * opening_nav - current_risk) / unit_stop_risk),
                "available_opening_cash": max(0.0, cash / (1.0 + cost_rate)),
            }
            allocation_notional = min([requested, *caps.values()])
            minimum_notional = float(portfolio_policy["minimum_position_weight"]) * opening_nav
            if allocation_notional + 1e-12 < minimum_notional:
                decision["status"] = "not_allocated"
                decision["reason"] = "constrained_below_minimum_position_weight"
                decision["binding_constraints"] = sorted(
                    key for key, value in caps.items() if value <= allocation_notional + 1e-12)
                decisions[scenario_id] = _seal_decision(decision)
                continue
            binding = sorted(key for key, value in caps.items()
                             if allocation_notional < requested - 1e-12
                             and abs(value - allocation_notional) <= 1e-10)
            entry_cost = allocation_notional * cost_rate
            units = allocation_notional / entry_price
            cash -= allocation_notional + entry_cost
            if cash < -1e-10:
                return _refuse("portfolio_negative_cash_after_entry", scenario_id=scenario_id)
            cash = max(0.0, cash)
            planned_risk = allocation_notional * unit_stop_risk
            position = {
                "scenario_id": scenario_id,
                "symbol": record["symbol"],
                "sector": allocation["sector"],
                "catalyst_type": allocation["catalyst_type"],
                "units": units,
                "entry_notional": allocation_notional,
                "entry_price": entry_price,
                "exit_price": float(record["exit_price"]),
                "exit_date": record["exit_date"],
                "cost_rate": cost_rate,
                "planned_stop_risk": planned_risk,
            }
            positions[scenario_id] = position
            opening_values[scenario_id] = allocation_notional
            last_values[scenario_id] = allocation_notional
            current_gross += allocation_notional
            sectors[allocation["sector"]] = sectors.get(allocation["sector"], 0.0) + allocation_notional
            catalysts[allocation["catalyst_type"]] = (
                catalysts.get(allocation["catalyst_type"], 0.0) + allocation_notional)
            current_risk += planned_risk
            entry_gross_peak = max(entry_gross_peak, current_gross)
            total_entry_notional += allocation_notional
            total_modeled_cost += entry_cost
            entries_today.append(scenario_id)
            decision["status"] = "allocated"
            decision["reason"] = "passed_entry_constraints"
            decision["allocated_weight"] = _round(allocation_notional / opening_nav)
            decision["binding_constraints"] = binding
            decisions[scenario_id] = _seal_decision(decision)

        ending_values: dict[str, float] = {}
        for scenario_id, position in sorted(list(positions.items())):
            if position["exit_date"] == date:
                gross_proceeds = _market_value(position, position["exit_price"])
                exit_cost = gross_proceeds * position["cost_rate"]
                cash += gross_proceeds - exit_cost
                total_exit_notional += gross_proceeds
                total_modeled_cost += exit_cost
                exits_today.append(scenario_id)
                del positions[scenario_id]
                continue
            close = float(maps[position["symbol"]][date]["close"])
            ending_values[scenario_id] = _market_value(position, close)
        last_values = ending_values
        gross, sector_values, catalyst_values = _exposures(positions, ending_values)
        nav = cash + gross
        if nav <= 0 or cash < -1e-10:
            return _refuse("portfolio_nav_or_cash_invalid", date=date)
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1.0
        ending_risk = sum(position["planned_stop_risk"] for position in positions.values())
        breaches: list[str] = []
        if gross / nav > portfolio_policy["maximum_gross_exposure"] + 1e-10:
            breaches.append("gross_exposure_market_move")
        if any(value / nav > portfolio_policy["maximum_sector_weight"] + 1e-10
               for value in sector_values.values()):
            breaches.append("sector_exposure_market_move")
        if any(value / nav > portfolio_policy["maximum_catalyst_weight"] + 1e-10
               for value in catalyst_values.values()):
            breaches.append("catalyst_exposure_market_move")
        if ending_risk / nav > portfolio_policy["maximum_aggregate_stop_risk"] + 1e-10:
            breaches.append("aggregate_stop_risk_nav_move")
        daily.append({
            "date": date,
            "normalized_nav": _round(nav),
            "daily_return": _round(nav / previous_nav - 1.0),
            "drawdown": _round(drawdown),
            "cash_weight": _round(cash / nav),
            "gross_exposure": _round(gross / nav),
            "net_exposure": _round(gross / nav),
            "entry_gross_exposure_peak": _round(entry_gross_peak / opening_nav),
            "aggregate_stop_risk": _round(ending_risk / nav),
            "open_position_count": len(positions),
            "sector_weights": {key: _round(value / nav)
                               for key, value in sorted(sector_values.items())},
            "catalyst_weights": {key: _round(value / nav)
                                 for key, value in sorted(catalyst_values.items())},
            "entries": entries_today,
            "exits": exits_today,
            "post_entry_constraint_breaches_from_market_moves": sorted(breaches),
        })
        previous_nav = nav

    if positions:
        return _refuse("portfolio_positions_remain_after_last_exit",
                       scenario_ids=sorted(positions))
    decision_list = [decisions[key] for key in sorted(decisions)]
    if len(decision_list) != len(records):
        return _refuse("portfolio_decision_denominator_incomplete")
    allocated_count = sum(row["status"] == "allocated" for row in decision_list)
    final_nav = daily[-1]["normalized_nav"]
    summary = {
        "source_scenario_count": len(records),
        "source_completed_count": len(completed),
        "allocated_count": allocated_count,
        "not_allocated_count": len(records) - allocated_count,
        "final_normalized_nav": final_nav,
        "total_return": _round(final_nav / initial_nav - 1.0),
        "maximum_drawdown": min(row["drawdown"] for row in daily),
        "turnover_on_initial_nav": _round(
            (total_entry_notional + total_exit_notional) / initial_nav),
        "modeled_cost_on_initial_nav": _round(total_modeled_cost / initial_nav),
        "maximum_entry_gross_exposure": max(
            row["entry_gross_exposure_peak"] for row in daily),
        "maximum_end_of_day_gross_exposure": max(row["gross_exposure"] for row in daily),
        "market_move_constraint_breach_session_count": sum(
            bool(row["post_entry_constraint_breaches_from_market_moves"]) for row in daily),
        "actual_orders_count": 0,
    }
    result = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": RESULT_SCHEMA,
        "experiment_id": backtest["experiment_id"],
        "variant_id": backtest["variant_id"],
        "cost_stress_multiplier": float(stress_multiplier),
        "policy_versions": {
            "cost": cost_policy["version"],
            "portfolio": portfolio_policy["version"],
            "plan": plan["version"],
        },
        "lineage": {
            **source_hashes,
            "source_backtest_result_sha256": backtest["result_sha256"],
            "source_backtest_plan_sha256": qb._sha256_obj(source_plans),
            "portfolio_plan_sha256": sha256_obj(plan),
            "cost_policy_sha256": cm.sha256_obj(cost_policy),
            "portfolio_policy_sha256": sha256_obj(portfolio_policy),
            "experiment_manifest_sha256": qb._sha256_obj(manifest),
        },
        "conservative_rules": [
            "all_source_scenarios_present_in_predecision_plan",
            "portfolio_plan_and_policies_fingerprinted_before_backtest",
            "missing_cost_inputs_skip_candidate",
            "same_session_exit_proceeds_not_reused_for_entries",
            "one_open_position_per_symbol",
            "priority_then_scenario_id_tie_break",
            "long_only_normalized_capital_without_leverage",
            "daily_close_mark_to_market",
        ],
        "summary": summary,
        "decisions": decision_list,
        "daily_path": daily,
        "decisions_sha256": sha256_obj(decision_list),
        "daily_path_sha256": sha256_obj(daily),
        "result_sha256": "",
    }
    result["result_sha256"] = sha256_obj(_payload(result, "result_sha256"))
    result_errors = validate_result(result)
    if result_errors:
        return _refuse("portfolio_result_self_validation_failed", errors=result_errors)
    return result


def validate_result(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["portfolio_result_not_object"]
    errors: list[str] = []
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"portfolio_result_safety_invalid:{key}")
    if document.get("ok") is not True or document.get("schema") != RESULT_SCHEMA:
        errors.append("portfolio_result_identity_invalid")
    lineage = document.get("lineage")
    if not isinstance(lineage, dict):
        errors.append("portfolio_result_lineage_missing")
    else:
        for key, value in lineage.items():
            if key.endswith("sha256") and (
                    not isinstance(value, str) or not _SHA_RE.fullmatch(value)):
                errors.append(f"portfolio_result_lineage_hash_invalid:{key}")
    decisions = document.get("decisions")
    daily = document.get("daily_path")
    if not isinstance(decisions, list) or not decisions:
        errors.append("portfolio_result_decisions_invalid")
        decisions = []
    if not isinstance(daily, list) or not daily:
        errors.append("portfolio_result_daily_path_invalid")
        daily = []
    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            errors.append(f"portfolio_decision_not_object:{index}")
            continue
        scenario_id = decision.get("scenario_id")
        if scenario_id in seen:
            errors.append(f"portfolio_decision_duplicate:{scenario_id}")
        seen.add(scenario_id)
        if decision.get("status") not in {"allocated", "not_allocated"}:
            errors.append(f"portfolio_decision_status_invalid:{index}")
        if decision.get("fill_guaranteed") is not False or decision.get(
                "actual_order_exists") is not False:
            errors.append(f"portfolio_decision_claims_order_or_fill:{index}")
        if decision.get("decision_sha256") != sha256_obj(
                _payload(decision, "decision_sha256")):
            errors.append(f"portfolio_decision_sha256_mismatch:{index}")
    previous_date = None
    for index, row in enumerate(daily):
        if not isinstance(row, dict) or _date(row.get("date")) is None:
            errors.append(f"portfolio_daily_row_invalid:{index}")
            continue
        if previous_date is not None and row["date"] <= previous_date:
            errors.append(f"portfolio_daily_dates_not_increasing:{index}")
        previous_date = row["date"]
        for key in ("normalized_nav", "cash_weight", "gross_exposure", "net_exposure"):
            if not _number(row.get(key), nonnegative=True):
                errors.append(f"portfolio_daily_number_invalid:{index}:{key}")
        if row.get("net_exposure") != row.get("gross_exposure"):
            errors.append(f"portfolio_daily_long_only_exposure_mismatch:{index}")
    summary = document.get("summary")
    if not isinstance(summary, dict):
        errors.append("portfolio_result_summary_missing")
    else:
        allocated = sum(row.get("status") == "allocated" for row in decisions)
        if summary.get("source_scenario_count") != len(decisions):
            errors.append("portfolio_summary_scenario_count_mismatch")
        if summary.get("allocated_count") != allocated:
            errors.append("portfolio_summary_allocated_count_mismatch")
        if summary.get("not_allocated_count") != len(decisions) - allocated:
            errors.append("portfolio_summary_not_allocated_count_mismatch")
        if summary.get("actual_orders_count") != 0:
            errors.append("portfolio_summary_claims_actual_orders")
        if daily and summary.get("final_normalized_nav") != daily[-1].get("normalized_nav"):
            errors.append("portfolio_summary_final_nav_mismatch")
    if document.get("decisions_sha256") != sha256_obj(decisions):
        errors.append("portfolio_decisions_sha256_mismatch")
    if document.get("daily_path_sha256") != sha256_obj(daily):
        errors.append("portfolio_daily_path_sha256_mismatch")
    if document.get("result_sha256") != sha256_obj(_payload(document, "result_sha256")):
        errors.append("portfolio_result_sha256_mismatch")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline overlapping portfolio diagnostics.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    run = sub.add_parser("run")
    run.add_argument("--backtest", required=True)
    run.add_argument("--bars", required=True)
    run.add_argument("--source-plans", required=True)
    run.add_argument("--portfolio-plan", required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--cost-policy", default=str(COST_POLICY_DEFAULT))
    run.add_argument("--portfolio-policy", default=str(POLICY_DEFAULT))
    run.add_argument("--stress", type=float, default=1.0)
    run.add_argument("--out", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate":
            document = _read(Path(args.artifact))
            errors = validate_result(document)
            output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                      "result_sha256": document.get("result_sha256")}
        else:
            paths = {
                "backtest": Path(args.backtest), "bars": Path(args.bars),
                "source_plans": Path(args.source_plans),
                "portfolio_plan": Path(args.portfolio_plan),
                "manifest": Path(args.manifest), "cost_policy": Path(args.cost_policy),
                "portfolio_policy": Path(args.portfolio_policy),
            }
            docs = {key: _read(path) for key, path in paths.items()}
            hashes = {f"{key}_file_sha256": sha256_file(path) for key, path in paths.items()}
            source_hashes = {
                "backtest_file_sha256": hashes["backtest_file_sha256"],
                "bars_file_sha256": hashes["bars_file_sha256"],
                "source_plans_file_sha256": hashes["source_plans_file_sha256"],
                "portfolio_plan_file_sha256": hashes["portfolio_plan_file_sha256"],
                "cost_policy_file_sha256": hashes["cost_policy_file_sha256"],
                "portfolio_policy_file_sha256": hashes["portfolio_policy_file_sha256"],
                "manifest_file_sha256": hashes["manifest_file_sha256"],
            }
            output = run_portfolio(
                docs["backtest"], docs["bars"], docs["source_plans"],
                docs["portfolio_plan"], docs["manifest"], docs["cost_policy"],
                docs["portfolio_policy"], source_hashes, args.stress)
            if output.get("ok"):
                _atomic_write_once(Path(args.out), output)
                output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": True,
                          "written": str(Path(args.out)),
                          "result_sha256": output["result_sha256"],
                          "summary": output["summary"]}
        print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
        return 0 if output.get("ok") else 1
    except Exception as exc:
        print(json.dumps(_refuse(type(exc).__name__, detail=str(exc)[:400]),
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
