#!/usr/bin/env python3
"""Fail-closed OPTIONS-RESEARCH risk gate (shadow lane v0). Standard library only.

PARALLEL to scripts/lib/risk_gate.py — it neither imports, replaces, nor weakens the equity
gate. The equity gate continues to reject every option instrument via DISALLOWED_FLAGS; an
option object presented to the equity gate must still FAIL there. This gate exists only for
the ratified research-only shadow lane (journal/2026-08-02_options_kickstarter_ratification.md).

LIMITS AUTHORITY: the PROTECTED config/options_risk_limits.yaml is the authority for every
limit. Caller-supplied `limits` may only TIGHTEN numeric values; flags and allowlists are never
caller-overridable. Missing/unparseable protected file => the gate FAILS CLOSED
(`options_limits_config_unreadable`).

OUTPUT STATUSES — exactly one of:
    BLOCKED | PREOPEN_WATCH_ONLY | SHADOW_OPTIONS_CANDIDATE
This module can never emit an actionable, official, or order-like status. Options execution is
permanently disabled; no input can change that.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
OPTIONS_LIMITS_CONFIG = _REPO_ROOT / "config" / "options_risk_limits.yaml"

STATUS_BLOCKED = "BLOCKED"
STATUS_PREOPEN = "PREOPEN_WATCH_ONLY"
STATUS_SHADOW = "SHADOW_OPTIONS_CANDIDATE"

# Base values the protected file overlays. Deliberately identical to the ratified v0 file so a
# drifted default can never be LOOSER than the protected config.
DEFAULT_LIMITS = {
    "preferred_entry_dte_min": 30.0,
    "preferred_entry_dte_max": 45.0,
    "hard_entry_dte_min": 21.0,
    "hard_entry_dte_max": 60.0,
    "hard_exit_dte": 10.0,
    "preferred_abs_delta_min": 0.40,
    "preferred_abs_delta_max": 0.60,
    "hard_abs_delta_min": 0.35,
    "hard_abs_delta_max": 0.65,
    "minimum_open_interest": 500.0,
    "minimum_same_session_volume": 100.0,
    "maximum_relative_spread": 0.10,
    "maximum_quote_age_seconds": 60.0,
    "post_open_delay_minutes": 15.0,
    "maximum_premium_risk_pct_nav": 0.005,
    "maximum_total_options_premium_risk_pct_nav": 0.01,
    "maximum_contracts_per_candidate": 1.0,
    "minimum_intended_holding_days": 1.0,
    "maximum_intended_holding_days": 10.0,
    "required_contract_multiplier": 100.0,
}
DEFAULT_FLAGS = {
    "require_full_settled_cash_coverage": True,
    "allow_adjusted_contracts": False,
    "allow_nonstandard_multiplier": False,
    "allow_index_options": False,
    "allow_short_legs": False,
    "allow_multi_leg": False,
    "allow_margin": False,
    "allow_rolling": False,
    "allow_exercise_workflow": False,
    "allow_premarket_actionability": False,
    "allow_pre_event_long_premium": False,
}
DEFAULT_ALLOWLISTS = {
    "allowed_underlying_types": ["equity", "etf"],
    "allowed_strategies": ["long_call", "long_put"],
}

# Strictness direction for tighten-only caller overrides: for these, LARGER is stricter.
_TIGHTER_IS_LARGER = {
    "preferred_entry_dte_min", "hard_entry_dte_min", "hard_exit_dte",
    "preferred_abs_delta_min", "hard_abs_delta_min", "minimum_open_interest",
    "minimum_same_session_volume", "post_open_delay_minutes",
    "minimum_intended_holding_days",
}
# required_contract_multiplier is an EQUALITY constraint, never overridable.
_NEVER_OVERRIDABLE = {"required_contract_multiplier"}

_DIRECTION_TO_STRATEGY = {"bullish": "long_call", "bearish": "long_put"}
_STRATEGY_TO_OPTION_TYPE = {"long_call": "call", "long_put": "put"}


def finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _parse_options_limits(text: str) -> dict | None:
    """Stdlib parser for config/options_risk_limits.yaml (PyYAML may be absent in launchd
    python). Understands exactly the three blocks `limits:` (numeric), `flags:` (boolean) and
    `allowlists:` (lists of plain strings). Any anomaly — conflict markers, tabs, duplicate
    keys, unknown block content, non-numeric limit, non-boolean flag — rejects the WHOLE file:
    fail closed, never best-effort."""
    limits: dict[str, float] = {}
    flags: dict[str, bool] = {}
    allowlists: dict[str, list[str]] = {}
    section = None
    current_list: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(("<<<<<<<", ">>>>>>>")) or raw.rstrip() == "=======" or "\t" in raw:
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            head = stripped.split("#", 1)[0].strip()
            section = head[:-1] if head.endswith(":") else None
            current_list = None
            continue
        body = stripped.split("#", 1)[0].strip()
        if not body:
            continue
        if section == "limits":
            if ":" not in body or body.startswith("- "):
                return None
            key, value = (part.strip() for part in body.split(":", 1))
            if key in limits or not value:
                return None
            try:
                limits[key] = float(value)
            except ValueError:
                return None
        elif section == "flags":
            if ":" not in body or body.startswith("- "):
                return None
            key, value = (part.strip() for part in body.split(":", 1))
            if key in flags or value not in ("true", "false"):
                return None
            flags[key] = value == "true"
        elif section == "allowlists":
            if body.startswith("- "):
                if current_list is None:
                    return None
                item = body[2:].strip()
                if not item or not item.replace("_", "").isalnum():
                    return None
                allowlists[current_list].append(item)
            elif body.endswith(":"):
                current_list = body[:-1].strip()
                if not current_list or current_list in allowlists:
                    return None
                allowlists[current_list] = []
            else:
                return None
        # other sections (schema:, notes:) are ignored by content but must not crash
    if not limits or not flags or not allowlists:
        return None
    return {"limits": limits, "flags": flags, "allowlists": allowlists}


def load_protected_limits(path: Path | None = None) -> dict:
    p = path or OPTIONS_LIMITS_CONFIG
    try:
        text = p.read_text()
    except Exception:
        return {"parsed": None, "sha256": None, "reason": "file_unreadable"}
    sha = hashlib.sha256(text.encode()).hexdigest()
    parsed = _parse_options_limits(text)
    if parsed is None:
        return {"parsed": None, "sha256": sha, "reason": "parse_failed"}
    return {"parsed": parsed, "sha256": sha, "reason": "minimal_stdlib"}


def effective_limits(caller_limits: dict | None,
                     path: Path | None = None) -> tuple[dict, dict, dict, dict]:
    """Returns (limits, flags, allowlists, meta). Protected file overlays DEFAULTS; caller
    numeric limits apply TIGHTEN-ONLY; flags/allowlists come only from defaults+file."""
    meta = {"config_loaded": False, "config_sha256": None, "config_parser": None,
            "ignored_loosening": [], "ignored_invalid": [], "ignored_unknown": []}
    limits = dict(DEFAULT_LIMITS)
    flags = dict(DEFAULT_FLAGS)
    allowlists = {key: list(value) for key, value in DEFAULT_ALLOWLISTS.items()}
    loaded = load_protected_limits(path)
    meta["config_sha256"] = loaded["sha256"]
    meta["config_parser"] = loaded["reason"]
    if loaded["parsed"] is not None:
        meta["config_loaded"] = True
        for key, value in loaded["parsed"]["limits"].items():
            if key in limits and finite_number(value):
                limits[key] = float(value)
        for key, value in loaded["parsed"]["flags"].items():
            if key in flags and isinstance(value, bool):
                # A protected-file flag may only ever REMOVE permission relative to defaults.
                flags[key] = value if key == "require_full_settled_cash_coverage" \
                    else (flags[key] and value)
        for key, value in loaded["parsed"]["allowlists"].items():
            if key in allowlists:
                allowed = [item for item in value if item in DEFAULT_ALLOWLISTS[key]]
                allowlists[key] = allowed
    for key, value in (caller_limits or {}).items():
        key = str(key)
        if key not in limits or key in _NEVER_OVERRIDABLE:
            meta["ignored_unknown"].append(key)
            continue
        if not finite_number(value):
            meta["ignored_invalid"].append(key)
            continue
        value = float(value)
        stricter = max(limits[key], value) if key in _TIGHTER_IS_LARGER \
            else min(limits[key], value)
        if stricter != limits[key]:
            limits[key] = stricter
        elif (value > limits[key] if key not in _TIGHTER_IS_LARGER else value < limits[key]):
            meta["ignored_loosening"].append(key)
    return limits, flags, allowlists, meta


def _block(out: dict, code: str) -> None:
    if code not in out["reason_codes"]:
        out["reason_codes"].append(code)


REQUIRED_NUMBERS = [
    "nav", "settled_cash", "aggregate_open_options_premium",
    "underlying_target_1", "underlying_target_2", "underlying_invalidation",
    "abs_delta", "relative_spread", "quote_age_seconds", "bid", "ask",
    "proposed_max_debit_per_contract",
]
REQUIRED_INTS = ["dte", "open_interest", "same_session_volume", "contract_multiplier",
                 "intended_holding_days"]
REQUIRED_BOOLS = [
    "thesis_final", "thesis_blocked", "thesis_stale", "catalyst_provenance_verified",
    "provenance_complete", "nav_verified", "nav_fresh", "settled_cash_verified",
    "options_approval_verified", "aggregate_exposure_verified",
    "correlated_exposure_blocked", "binary_event_in_window", "greek_units_verified",
    "adjusted_contract", "same_session_consistent",
]
REQUIRED_STRINGS = ["thesis_direction", "option_type", "account_type",
                    "underlying_instrument_type", "snapshot_validation_status",
                    "market_session"]


def validate(data: dict, limits_path: Path | None = None) -> dict:
    limits, flags, allowlists, meta = effective_limits(data.get("limits") or {}, limits_path)
    out = {
        "status": STATUS_BLOCKED,
        "reason_codes": [],
        "warnings": [],
        "sizing": {
            "risk_budget_dollars": 0.0,
            "contract_cost_dollars": 0.0,
            "contracts_raw": 0,
            "contracts": 0,
            "max_premium_loss_dollars": 0.0,
        },
        "strategy": None,
        "limits_effective": dict(limits),
        "flags_effective": dict(flags),
        "allowlists_effective": {k: list(v) for k, v in allowlists.items()},
        "limits_authority": meta,
        "options_execution_enabled": False,
        "actionable": False,
        "shadow": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not meta["config_loaded"]:
        _block(out, "options_limits_config_unreadable")
    for key in meta["ignored_loosening"]:
        out["warnings"].append(f"caller_limit_loosening_ignored:{key}")
    for key in meta["ignored_invalid"]:
        out["warnings"].append(f"caller_limit_invalid_ignored:{key}")

    for key in REQUIRED_NUMBERS:
        if not finite_number(data.get(key)):
            _block(out, f"blocked_missing_or_invalid:{key}")
    for key in REQUIRED_INTS:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _block(out, f"blocked_missing_or_invalid:{key}")
    for key in REQUIRED_BOOLS:
        if not isinstance(data.get(key), bool):
            _block(out, f"blocked_missing_or_invalid:{key}")
    for key in REQUIRED_STRINGS:
        if not isinstance(data.get(key), str) or not data.get(key):
            _block(out, f"blocked_missing_or_invalid:{key}")
    if out["reason_codes"]:
        return out

    direction = data["thesis_direction"].lower()
    strategy = _DIRECTION_TO_STRATEGY.get(direction)
    if strategy is None:
        _block(out, "blocked_unknown_thesis_direction")
        return out
    if strategy not in allowlists["allowed_strategies"]:
        _block(out, "blocked_strategy_not_allowlisted")
    expected_type = _STRATEGY_TO_OPTION_TYPE[strategy]
    if data["option_type"].lower() != expected_type:
        _block(out, "blocked_direction_strategy_mismatch")
    out["strategy"] = strategy

    if data["underlying_instrument_type"].lower() not in allowlists["allowed_underlying_types"]:
        _block(out, "blocked_underlying_type_not_allowlisted")

    # Thesis / provenance gates — the options lane is an expression overlay only.
    if not data["thesis_final"]:
        _block(out, "blocked_thesis_not_final")
    if data["thesis_blocked"]:
        _block(out, "blocked_thesis_blocked")
    if data["thesis_stale"]:
        _block(out, "blocked_thesis_stale")
    if not data["catalyst_provenance_verified"]:
        _block(out, "blocked_catalyst_provenance_unverified")
    if not data["provenance_complete"]:
        _block(out, "blocked_provenance_incomplete")
    if data["snapshot_validation_status"] != "VALID":
        _block(out, "blocked_snapshot_not_valid")
    if not data["greek_units_verified"]:
        _block(out, "blocked_greek_units_unverified")

    # Account gates. Displayed buying power is NEVER settled cash; only the explicit
    # settled_cash field (with its verification flag) counts.
    if not data["nav_verified"]:
        _block(out, "blocked_nav_unverified")
    if not data["nav_fresh"]:
        _block(out, "blocked_nav_stale")
    if not data["settled_cash_verified"]:
        _block(out, "blocked_settled_cash_unverified")
    if not data["options_approval_verified"]:
        _block(out, "blocked_options_approval_unverified")
    if data["account_type"].lower() != "cash":
        _block(out, "blocked_account_type_not_cash")
    if not data["aggregate_exposure_verified"]:
        _block(out, "blocked_aggregate_exposure_unknown")
    if data["correlated_exposure_blocked"]:
        _block(out, "blocked_correlated_exposure")
    if data["binary_event_in_window"] and not flags["allow_pre_event_long_premium"]:
        _block(out, "blocked_binary_event_in_window")

    nav = float(data["nav"])
    settled_cash = float(data["settled_cash"])
    aggregate_open = float(data["aggregate_open_options_premium"])
    if nav <= 0:
        _block(out, "blocked_nav_nonpositive")
    if settled_cash < 0 or aggregate_open < 0:
        _block(out, "blocked_negative_account_value")

    # Underlying thesis geometry.
    t1 = float(data["underlying_target_1"])
    t2 = float(data["underlying_target_2"])
    inval = float(data["underlying_invalidation"])
    if min(t1, t2, inval) <= 0:
        _block(out, "blocked_invalid_underlying_levels")
    elif direction == "bullish" and not (inval < t1 <= t2):
        _block(out, "blocked_invalid_underlying_levels")
    elif direction == "bearish" and not (inval > t1 >= t2):
        _block(out, "blocked_invalid_underlying_levels")

    holding = int(data["intended_holding_days"])
    if not (limits["minimum_intended_holding_days"] <= holding
            <= limits["maximum_intended_holding_days"]):
        _block(out, "blocked_holding_period_invalid")

    # Contract hard screens.
    dte = int(data["dte"])
    if not (limits["hard_entry_dte_min"] <= dte <= limits["hard_entry_dte_max"]):
        _block(out, "blocked_dte_out_of_hard_range")
    if dte - holding < limits["hard_exit_dte"]:
        _block(out, "blocked_exit_deadline_violation")
    abs_delta = float(data["abs_delta"])
    if not (limits["hard_abs_delta_min"] <= abs_delta <= limits["hard_abs_delta_max"]):
        _block(out, "blocked_delta_out_of_hard_range")
    if int(data["open_interest"]) < limits["minimum_open_interest"]:
        _block(out, "blocked_open_interest_below_minimum")
    if int(data["same_session_volume"]) < limits["minimum_same_session_volume"]:
        _block(out, "blocked_volume_below_minimum")
    if float(data["relative_spread"]) > limits["maximum_relative_spread"] + 1e-12:
        _block(out, "blocked_spread_above_maximum")
    if int(data["contract_multiplier"]) != int(limits["required_contract_multiplier"]):
        _block(out, "blocked_nonstandard_multiplier")
    if data["adjusted_contract"] and not flags["allow_adjusted_contracts"]:
        _block(out, "blocked_adjusted_contract")
    bid = float(data["bid"])
    ask = float(data["ask"])
    if bid <= 0:
        _block(out, "blocked_zero_bid_no_executable_liquidity")
    if ask <= 0 or ask < bid:
        _block(out, "blocked_crossed_or_invalid_market")
    debit = float(data["proposed_max_debit_per_contract"])
    if debit <= 0 or (ask > 0 and debit > ask + 1e-9):
        _block(out, "blocked_debit_not_within_market")

    # Session gates. Premarket data can never produce a shadow candidate.
    session = data["market_session"].lower()
    if session == "regular":
        if not data["same_session_consistent"]:
            _block(out, "blocked_session_inconsistent_quotes")
        if float(data["quote_age_seconds"]) > limits["maximum_quote_age_seconds"] + 1e-9:
            _block(out, "blocked_quote_stale")
    elif session != "premarket":
        _block(out, "blocked_session_not_eligible")

    if out["reason_codes"]:
        return out

    if session == "premarket":
        out["status"] = STATUS_PREOPEN
        out["warnings"].append("premarket_marks_not_actionable")
        return out

    # Sizing — premium paid IS the modeled maximum loss (it is not a guaranteed maximum
    # realized outcome timing-wise: the full premium can be lost, and faster than an equity
    # stop implies).
    per_candidate_budget = nav * limits["maximum_premium_risk_pct_nav"]
    total_capacity = nav * limits["maximum_total_options_premium_risk_pct_nav"] - aggregate_open
    if total_capacity <= 0:
        _block(out, "blocked_aggregate_premium_cap_reached")
        return out
    risk_budget = min(per_candidate_budget, total_capacity)
    contract_cost = debit * float(data["contract_multiplier"])
    out["sizing"]["risk_budget_dollars"] = round(risk_budget, 4)
    out["sizing"]["contract_cost_dollars"] = round(contract_cost, 4)
    if contract_cost <= 0:
        _block(out, "blocked_invalid_contract_cost")
        return out
    contracts_raw = int(math.floor(risk_budget / contract_cost))
    contracts = min(contracts_raw, int(limits["maximum_contracts_per_candidate"]))
    out["sizing"]["contracts_raw"] = contracts_raw
    if contracts < 1:
        _block(out, "blocked_insufficient_risk_budget")
        return out
    total_debit = contracts * contract_cost
    if flags["require_full_settled_cash_coverage"] and settled_cash + 1e-9 < total_debit:
        _block(out, "blocked_settled_cash_insufficient")
        return out
    out["sizing"]["contracts"] = contracts
    out["sizing"]["max_premium_loss_dollars"] = round(total_debit, 4)
    out["status"] = STATUS_SHADOW
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON file containing gate input, or - for stdin")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("input must be a JSON object")
        print(json.dumps(validate(data), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": STATUS_BLOCKED,
                          "reason_codes": ["blocked_input_error"],
                          "warnings": [str(exc)],
                          "timestamp": datetime.now(timezone.utc).isoformat()}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
