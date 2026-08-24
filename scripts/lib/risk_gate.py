#!/usr/bin/env python3
"""Fail-closed pre-trade risk validator. Standard library only.

LIMITS AUTHORITY (2026-07-08 hardening): the PROTECTED config/risk_limits.yaml `limits:` block
is the authority for every limit. Caller-supplied `data["limits"]` values may only TIGHTEN the
protected values — a loosening attempt is ignored and surfaced as a warning, so a model-authored
proposal document can never widen its own risk budget. If the protected file is missing or
unparseable the gate FAILS CLOSED (rule `risk_limits_config_unreadable`) rather than falling
back to built-in defaults: a Buy must never be sized against limits nobody approved. DEFAULTS
below remain only as the base the protected file overlays (and for any key the file omits).
"""
from __future__ import annotations
import argparse, hashlib, json, math, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_LIMITS_CONFIG = _REPO_ROOT / "config" / "risk_limits.yaml"

DEFAULTS = {
    "max_positions": 2,
    "max_position_pct": 0.50,
    "max_invested_pct": 0.80,
    "min_cash_pct": 0.20,
    "max_trade_risk_pct": 0.025,
    "max_trade_risk_dollars": 1.00,
    "max_session_loss_pct": 0.05,
    "max_drawdown_pct": 0.06,
    "max_entries": 3,
    "min_reward_risk": 2.0,
    "min_price": 3.0,
    "max_quote_age_seconds": 30,
}

# Strictness direction per limit: for max_* smaller is stricter; for min_* larger is stricter.
_TIGHTER_IS_LARGER = {"min_cash_pct", "min_reward_risk", "min_price"}

ALLOWED_SECURITY_TYPES = {"common_stock", "etf"}
DISALLOWED_FLAGS = {"margin", "leveraged", "inverse", "short", "option", "futures",
                    "event_contract", "crypto", "otc", "warrant"}


def _parse_limits_minimal(text: str) -> dict | None:
    """Stdlib parser for the `limits:` block of config/risk_limits.yaml (PyYAML may be absent
    in launchd/system python). Numeric scalars only; any anomaly (conflict markers, tabs,
    duplicate keys, non-numeric value) rejects the WHOLE file — fail closed, never best-effort."""
    limits: dict[str, float] = {}
    in_limits = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(("<<<<<<<", ">>>>>>>")) or raw.rstrip() == "=======" or raw[:1] == "\t":
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            in_limits = stripped.split("#", 1)[0].strip() == "limits:"
            continue
        if not in_limits or ":" not in stripped or stripped.startswith("- "):
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if key in limits:
            return None
        try:
            limits[key] = float(value)
        except ValueError:
            return None
    return limits or None


def load_protected_limits(path: Path | None = None) -> dict:
    """Read the protected limits file. Returns {"limits": dict|None, "sha256": str|None,
    "reason": str}. limits is None whenever the file cannot be read AND parsed cleanly."""
    p = path or RISK_LIMITS_CONFIG
    try:
        text = p.read_text()
    except Exception:
        return {"limits": None, "sha256": None, "reason": "file_unreadable"}
    sha = hashlib.sha256(text.encode()).hexdigest()
    try:
        import yaml  # type: ignore
    except ImportError:
        parsed = _parse_limits_minimal(text)
        if parsed is None:
            return {"limits": None, "sha256": sha, "reason": "minimal_parse_failed"}
        return {"limits": parsed, "sha256": sha, "reason": "minimal_stdlib"}
    try:
        raw = yaml.safe_load(text)
    except Exception:
        return {"limits": None, "sha256": sha, "reason": "pyyaml_parse_error"}
    block = raw.get("limits") if isinstance(raw, dict) else None
    if not isinstance(block, dict) or not block:
        return {"limits": None, "sha256": sha, "reason": "limits_block_missing"}
    return {"limits": block, "sha256": sha, "reason": "pyyaml"}


def effective_limits(caller_limits: dict | None, path: Path | None = None) -> tuple[dict, dict]:
    """Overlay protected config limits on DEFAULTS, then apply caller limits TIGHTEN-ONLY.
    Returns (cfg, meta); meta records provenance and every ignored loosening attempt."""
    meta = {"config_loaded": False, "config_sha256": None, "config_parser": None,
            "ignored_loosening": [], "ignored_invalid": [], "ignored_unknown": []}
    cfg = dict(DEFAULTS)
    loaded = load_protected_limits(path)
    meta["config_sha256"] = loaded["sha256"]
    meta["config_parser"] = loaded["reason"]
    if loaded["limits"] is not None:
        meta["config_loaded"] = True
        for key, value in loaded["limits"].items():
            if key in cfg and finite_number(value):
                cfg[key] = float(value)
    for key, value in (caller_limits or {}).items():
        key = str(key)
        if key not in cfg:
            meta["ignored_unknown"].append(key)
            continue
        if not finite_number(value):
            meta["ignored_invalid"].append(key)
            continue
        value = float(value)
        stricter = max(cfg[key], value) if key in _TIGHTER_IS_LARGER else min(cfg[key], value)
        if stricter != cfg[key]:
            cfg[key] = stricter
        elif (value > cfg[key] if key not in _TIGHTER_IS_LARGER else value < cfg[key]):
            meta["ignored_loosening"].append(key)
    return cfg, meta

def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))

def fail(result: dict, rule: str, warning: str | None = None) -> None:
    if rule not in result["failed_rules"]:
        result["failed_rules"].append(rule)
    if warning and warning not in result["warnings"]:
        result["warnings"].append(warning)

def validate(data: dict, limits_path: Path | None = None) -> dict:
    cfg, limits_meta = effective_limits(data.get("limits") or {}, limits_path)
    out = {
        "decision": "FAIL",
        "failed_rules": [],
        "warnings": [],
        "calculated_max_position": 0.0,
        "calculated_max_loss": 0.0,
        "remaining_risk_budget": 0.0,
        "limits_effective": dict(cfg),
        "limits_authority": limits_meta,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not limits_meta["config_loaded"]:
        # Fail closed: without the protected limits file there is no approved risk budget to
        # size against. (Watch/No-Trade research is unaffected — a FAILed gate only blocks Buy.)
        fail(out, "risk_limits_config_unreadable")
    for key in limits_meta["ignored_loosening"]:
        out["warnings"].append(f"caller_limit_loosening_ignored:{key}")
    for key in limits_meta["ignored_invalid"]:
        out["warnings"].append(f"caller_limit_invalid_ignored:{key}")

    required_numbers = [
        "account_value", "buying_power", "currently_invested",
        "session_start_value", "session_pnl", "high_water_mark",
        "current_account_value", "entry_price", "invalidation_price",
        "target_price", "proposed_position_dollars", "quote_age_seconds",
    ]
    for key in required_numbers:
        if not finite_number(data.get(key)):
            fail(out, f"missing_or_invalid:{key}")

    required_ints = ["open_positions", "entries_this_session"]
    for key in required_ints:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail(out, f"missing_or_invalid:{key}")

    required_bools = ["account_verified", "buying_power_verified", "quote_timestamp_verified",
                      "tradable", "fractional_verified", "catalyst_verified", "halted",
                      "averaging_down"]
    for key in required_bools:
        if not isinstance(data.get(key), bool):
            fail(out, f"missing_or_invalid:{key}")

    if out["failed_rules"]:
        return out

    account_value = float(data["account_value"])
    buying_power = float(data["buying_power"])
    invested = float(data["currently_invested"])
    session_start = float(data["session_start_value"])
    session_pnl = float(data["session_pnl"])
    high_water = float(data["high_water_mark"])
    current_value = float(data["current_account_value"])
    entry = float(data["entry_price"])
    stop = float(data["invalidation_price"])
    target = float(data["target_price"])
    proposed = float(data["proposed_position_dollars"])
    quote_age = float(data["quote_age_seconds"])

    for name, value in {
        "account_value": account_value, "buying_power": buying_power,
        "currently_invested": invested, "session_start_value": session_start,
        "high_water_mark": high_water, "current_account_value": current_value,
        "entry_price": entry, "target_price": target, "proposed_position_dollars": proposed,
        "quote_age_seconds": quote_age,
    }.items():
        if value < 0:
            fail(out, f"negative:{name}")

    if account_value <= 0 or session_start <= 0 or current_value <= 0 or entry <= 0:
        fail(out, "nonpositive_core_value")
        return out

    if not data["account_verified"]:
        fail(out, "account_not_verified")
    if not data["buying_power_verified"]:
        fail(out, "buying_power_not_verified")
    if not data["quote_timestamp_verified"]:
        fail(out, "quote_timestamp_not_verified")
    if quote_age > float(cfg["max_quote_age_seconds"]):
        fail(out, "stale_quote")
    if not data["tradable"]:
        fail(out, "not_tradable")
    if not data["fractional_verified"]:
        fail(out, "fractional_support_unverified")
    if not data["catalyst_verified"]:
        fail(out, "catalyst_unverified")
    if data["halted"]:
        fail(out, "security_halted")
    if data["averaging_down"]:
        fail(out, "averaging_down_forbidden")

    sec_type = str(data.get("security_type", "")).lower()
    if sec_type not in ALLOWED_SECURITY_TYPES:
        fail(out, "security_type_not_allowed")

    flags = {str(x).lower() for x in data.get("security_flags", [])}
    bad_flags = sorted(flags & DISALLOWED_FLAGS)
    if bad_flags:
        fail(out, "disallowed_security_flags", ",".join(bad_flags))

    if entry < float(cfg["min_price"]):
        fail(out, "price_below_minimum")
    if stop >= entry or stop <= 0:
        fail(out, "invalid_invalidation_for_long")
    if target <= entry:
        fail(out, "invalid_target_for_long")

    stop_distance_pct = (entry - stop) / entry if entry > 0 else math.inf
    reward_risk = (target - entry) / (entry - stop) if entry > stop else 0.0
    if reward_risk < float(cfg["min_reward_risk"]):
        fail(out, "reward_risk_below_minimum")

    risk_budget = min(account_value * float(cfg["max_trade_risk_pct"]),
                      float(cfg["max_trade_risk_dollars"]))
    out["remaining_risk_budget"] = round(max(0.0, risk_budget), 4)

    position_by_risk = risk_budget / stop_distance_pct if stop_distance_pct > 0 else 0.0
    max_position = min(position_by_risk,
                       account_value * float(cfg["max_position_pct"]),
                       buying_power)
    out["calculated_max_position"] = round(max(0.0, max_position), 4)
    modeled_loss = proposed * stop_distance_pct
    out["calculated_max_loss"] = round(max(0.0, modeled_loss), 4)

    if proposed <= 0:
        fail(out, "nonpositive_position")
    if proposed > account_value * float(cfg["max_position_pct"]) + 1e-9:
        fail(out, "position_allocation_exceeded")
    if proposed > max_position + 1e-9:
        fail(out, "risk_sized_position_exceeded")
    if proposed > buying_power + 1e-9:
        fail(out, "buying_power_exceeded")
    if invested + proposed > account_value * float(cfg["max_invested_pct"]) + 1e-9:
        fail(out, "portfolio_exposure_exceeded")
    if account_value - (invested + proposed) < account_value * float(cfg["min_cash_pct"]) - 1e-9:
        fail(out, "cash_reserve_breached")
    if modeled_loss > risk_budget + 1e-9:
        fail(out, "trade_loss_budget_exceeded")
    if int(data["open_positions"]) + 1 > int(cfg["max_positions"]):
        fail(out, "position_count_exceeded")
    if int(data["entries_this_session"]) >= int(cfg["max_entries"]):
        fail(out, "entry_count_exhausted")

    if session_pnl <= -(session_start * float(cfg["max_session_loss_pct"])):
        fail(out, "session_loss_limit_reached")
    drawdown = (high_water - current_value) / high_water if high_water > 0 else 1.0
    if drawdown >= float(cfg["max_drawdown_pct"]):
        fail(out, "drawdown_limit_reached")

    spread_pct = data.get("spread_pct")
    if spread_pct is not None:
        if not finite_number(spread_pct) or float(spread_pct) < 0:
            fail(out, "invalid_spread_pct")
        elif float(spread_pct) > 0.01:
            fail(out, "spread_excessive")
        elif float(spread_pct) > 0.005:
            out["warnings"].append("spread_is_elevated")

    if not out["failed_rules"]:
        out["decision"] = "PASS"
    return out

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="JSON file containing proposal data, or - for stdin")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("input must be a JSON object")
        print(json.dumps(validate(data), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "decision": "FAIL",
            "failed_rules": ["input_error"],
            "warnings": [str(exc)],
            "calculated_max_position": 0.0,
            "calculated_max_loss": 0.0,
            "remaining_risk_budget": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
