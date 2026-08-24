#!/usr/bin/env python3
"""Predecision-bound conservative transaction-cost diagnostics.

The model estimates a hypothetical per-side research cost from an observed spread and a
square-root market-impact proxy. It never previews an order, predicts a fill, reads a broker,
or supplies a fallback when an input is missing. Outputs are offline diagnostics only.
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
from quant import features as qf  # noqa: E402

TOOL = "quant.cost_model"
POLICY_SCHEMA = "quant_cost_policy/1.0"
ESTIMATE_SCHEMA = "quant_cost_estimate/1.0"
POLICY_DEFAULT = ROOT / "config" / "quant_cost_policy.json"
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM = (int, float)
_MAX_BYTES = 2 * 1024 * 1024

POLICY_SAFETY = {
    "research_only": True,
    "diagnostic_only": True,
    "execution_enabled": False,
    "activation_authority": False,
    "feeds_production": False,
    "strategy_change_allowed": False,
    "broker_access": "none",
    "actual_order_simulation": False,
    "fill_probability_claim_allowed": False,
    "fallback_cost_allowed": False,
}
OUTPUT_SAFETY = {
    **qf.SAFETY_FIELDS,
    "broker_access": "none",
    "actual_order_simulation": False,
    "fill_probability_claim_allowed": False,
    "fallback_cost_used": False,
}
_POLICY_KEYS = {
    "schema", "version", "methodology", "spread_component_rule",
    "impact_component_rule", "impact_coefficient", "minimum_cost_bps_per_side",
    "fixed_fee_bps_per_side", "maximum_spread_bps", "maximum_fraction_of_adv",
    "maximum_daily_volatility_bps", "stress_multipliers", "missing_input_rule",
    "future_input_rule", "capacity_rule", "fill_probability_rule", "safety",
}
_POLICY_FIXED = {
    "schema": POLICY_SCHEMA,
    "methodology": "half_spread_plus_square_root_impact_with_floor",
    "spread_component_rule": "half_observed_spread_bps",
    "impact_component_rule": (
        "impact_coefficient_times_daily_volatility_bps_times_sqrt_fraction_of_adv"),
    "missing_input_rule": "unestimable_never_impute_zero_or_assume_fill",
    "future_input_rule": "reject_observed_after_decision_cutoff",
    "capacity_rule": "reject_when_spread_participation_or_volatility_exceeds_limit",
    "fill_probability_rule": "never_estimated_without_order_book_and_queue_data",
}
_CONTEXT_KEYS = {
    "observed_at_utc", "decision_cutoff_utc", "spread_bps", "daily_volatility_bps",
    "fraction_of_adv", "source_artifact_sha256",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _payload(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "estimate_sha256"}


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


def validate_policy(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["cost_policy_not_object"]
    errors: list[str] = []
    if set(document) != _POLICY_KEYS:
        errors.append("cost_policy_top_level_keys_invalid")
    for key, expected in _POLICY_FIXED.items():
        if document.get(key) != expected:
            errors.append(f"cost_policy_rule_invalid:{key}")
    if not isinstance(document.get("version"), str) or not _SEMVER_RE.fullmatch(
            document.get("version", "")):
        errors.append("cost_policy_version_invalid")
    for key in ("impact_coefficient", "maximum_spread_bps", "maximum_fraction_of_adv",
                "maximum_daily_volatility_bps"):
        if not _number(document.get(key), positive=True):
            errors.append(f"cost_policy_number_invalid:{key}")
    for key in ("minimum_cost_bps_per_side", "fixed_fee_bps_per_side"):
        if not _number(document.get(key), nonnegative=True):
            errors.append(f"cost_policy_number_invalid:{key}")
    if _number(document.get("maximum_fraction_of_adv"), positive=True) and float(
            document["maximum_fraction_of_adv"]) > 1:
        errors.append("cost_policy_fraction_of_adv_above_one")
    multipliers = document.get("stress_multipliers")
    if (not isinstance(multipliers, list) or len(multipliers) < 2
            or any(not _number(value) or float(value) < 1 for value in multipliers)
            or [float(value) for value in multipliers] != sorted(
                set(float(value) for value in multipliers))):
        errors.append("cost_policy_stress_multipliers_invalid")
    if document.get("safety") != POLICY_SAFETY:
        errors.append("cost_policy_safety_invalid")
    return sorted(set(errors))


def load_policy(path: Path = POLICY_DEFAULT) -> tuple[dict | None, list[str]]:
    try:
        document = _read(path)
    except Exception:
        return None, ["cost_policy_unreadable"]
    return document, validate_policy(document)


def policy_summary(path: Path = POLICY_DEFAULT) -> dict:
    policy, errors = load_policy(path)
    return {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": policy is not None and not errors,
        "valid": policy is not None and not errors,
        "errors": errors,
        "version": policy.get("version") if isinstance(policy, dict) else None,
        "policy_sha256": sha256_obj(policy) if isinstance(policy, dict) else None,
    }


def validate_context(context: Any) -> list[str]:
    if not isinstance(context, dict):
        return ["cost_context_not_object"]
    errors: list[str] = []
    if set(context) != _CONTEXT_KEYS:
        errors.append("cost_context_keys_invalid")
    observed = qf._parse_utc(context.get("observed_at_utc"))
    cutoff = qf._parse_utc(context.get("decision_cutoff_utc"))
    if observed is None:
        errors.append("cost_context_observed_at_invalid")
    if cutoff is None:
        errors.append("cost_context_decision_cutoff_invalid")
    digest = context.get("source_artifact_sha256")
    if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
        errors.append("cost_context_source_hash_invalid")
    for key in ("spread_bps", "daily_volatility_bps", "fraction_of_adv"):
        value = context.get(key)
        if value is not None and not _number(value, positive=True):
            errors.append(f"cost_context_value_invalid:{key}")
    return sorted(set(errors))


def estimate(context: dict, policy: dict, stress_multiplier: float = 1.0) -> dict:
    policy_errors = validate_policy(policy)
    if policy_errors:
        return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                "error": "cost_policy_invalid", "errors": policy_errors}
    context_errors = validate_context(context)
    if context_errors:
        return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                "error": "cost_context_invalid", "errors": context_errors}
    allowed = [float(value) for value in policy["stress_multipliers"]]
    if not _number(stress_multiplier) or float(stress_multiplier) not in allowed:
        return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                "error": "stress_multiplier_not_allowed"}

    observed = qf._parse_utc(context["observed_at_utc"])
    cutoff = qf._parse_utc(context["decision_cutoff_utc"])
    missing = sorted(key for key in ("spread_bps", "daily_volatility_bps",
                                     "fraction_of_adv") if context[key] is None)
    status = "estimated"
    reasons: list[str] = []
    if observed is None or cutoff is None or observed > cutoff:
        status = "rejected_future_input"
        reasons.append("cost_input_observed_after_decision_cutoff")
    elif missing:
        status = "unestimable"
        reasons.extend(f"missing:{key}" for key in missing)
    else:
        checks = (
            ("spread_above_policy_limit", context["spread_bps"],
             policy["maximum_spread_bps"]),
            ("fraction_of_adv_above_policy_limit", context["fraction_of_adv"],
             policy["maximum_fraction_of_adv"]),
            ("daily_volatility_above_policy_limit", context["daily_volatility_bps"],
             policy["maximum_daily_volatility_bps"]),
        )
        reasons.extend(reason for reason, observed_value, limit in checks
                       if float(observed_value) > float(limit))
        if reasons:
            status = "capacity_rejected"

    spread_component = impact_component = unstressed = modeled = None
    if status == "estimated":
        spread_component = float(context["spread_bps"]) / 2.0
        impact_component = (float(policy["impact_coefficient"])
                            * float(context["daily_volatility_bps"])
                            * math.sqrt(float(context["fraction_of_adv"])))
        raw_cost = (float(policy["fixed_fee_bps_per_side"]) + spread_component
                    + impact_component)
        unstressed = max(float(policy["minimum_cost_bps_per_side"]), raw_cost)
        modeled = unstressed * float(stress_multiplier)

    document = {
        **OUTPUT_SAFETY,
        "tool": TOOL,
        "ok": True,
        "schema": ESTIMATE_SCHEMA,
        "policy_version": policy["version"],
        "policy_sha256": sha256_obj(policy),
        "context_sha256": sha256_obj(context),
        "observed_at_utc": context["observed_at_utc"],
        "decision_cutoff_utc": context["decision_cutoff_utc"],
        "status": status,
        "usable_for_portfolio_backtest": status == "estimated",
        "reasons": reasons,
        "missing_inputs": missing,
        "stress_multiplier": float(stress_multiplier),
        "components_bps_per_side": {
            "half_spread": _round(spread_component),
            "square_root_impact": _round(impact_component),
            "fixed_fee": float(policy["fixed_fee_bps_per_side"]),
            "minimum_floor": float(policy["minimum_cost_bps_per_side"]),
        },
        "unstressed_cost_bps_per_side": _round(unstressed),
        "modeled_cost_bps_per_side": _round(modeled),
        "fill_probability": None,
        "fill_probability_reason": policy["fill_probability_rule"],
        "actual_orders_count": 0,
        "estimate_sha256": "",
    }
    document["estimate_sha256"] = sha256_obj(_payload(document))
    errors = validate_estimate(document)
    if errors:
        return {**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                "error": "cost_estimate_self_validation_failed", "errors": errors}
    return document


def validate_estimate(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["cost_estimate_not_object"]
    errors: list[str] = []
    for key, expected in OUTPUT_SAFETY.items():
        if document.get(key) != expected:
            errors.append(f"cost_estimate_safety_invalid:{key}")
    if document.get("ok") is not True or document.get("schema") != ESTIMATE_SCHEMA:
        errors.append("cost_estimate_identity_invalid")
    if document.get("status") not in {
            "estimated", "unestimable", "capacity_rejected", "rejected_future_input"}:
        errors.append("cost_estimate_status_invalid")
    usable = document.get("status") == "estimated"
    if document.get("usable_for_portfolio_backtest") is not usable:
        errors.append("cost_estimate_usability_mismatch")
    if usable and not _number(document.get("modeled_cost_bps_per_side"), nonnegative=True):
        errors.append("cost_estimate_modeled_cost_missing")
    if not usable and document.get("modeled_cost_bps_per_side") is not None:
        errors.append("cost_estimate_rejected_has_cost")
    if document.get("fill_probability") is not None:
        errors.append("cost_estimate_claims_fill_probability")
    if document.get("actual_orders_count") != 0:
        errors.append("cost_estimate_claims_actual_orders")
    for key in ("policy_sha256", "context_sha256", "estimate_sha256"):
        if not isinstance(document.get(key), str) or not _SHA_RE.fullmatch(
                document.get(key, "")):
            errors.append(f"cost_estimate_hash_invalid:{key}")
    if document.get("estimate_sha256") != sha256_obj(_payload(document)):
        errors.append("cost_estimate_sha256_mismatch")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conservative offline cost diagnostics.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--policy", default=str(POLICY_DEFAULT))
    run = sub.add_parser("estimate")
    run.add_argument("--policy", default=str(POLICY_DEFAULT))
    run.add_argument("--context", required=True)
    run.add_argument("--stress", type=float, default=1.0)
    run.add_argument("--out")
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            output = policy_summary(Path(args.policy))
        elif args.command == "validate":
            document = _read(Path(args.artifact))
            errors = validate_estimate(document)
            output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": not errors, "errors": errors,
                      "estimate_sha256": document.get("estimate_sha256")}
        else:
            policy, policy_errors = load_policy(Path(args.policy))
            if policy is None or policy_errors:
                output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                          "error": "cost_policy_invalid", "errors": policy_errors}
            else:
                output = estimate(_read(Path(args.context)), policy, args.stress)
                if output.get("ok") and args.out:
                    _atomic_write_once(Path(args.out), output)
                    output = {**OUTPUT_SAFETY, "tool": TOOL, "ok": True,
                              "written": str(Path(args.out)),
                              "status": output["status"],
                              "estimate_sha256": output["estimate_sha256"]}
        print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
        return 0 if output.get("ok") else 1
    except Exception as exc:
        print(json.dumps({**OUTPUT_SAFETY, "tool": TOOL, "ok": False,
                          "error": type(exc).__name__, "detail": str(exc)[:300]},
                         indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
