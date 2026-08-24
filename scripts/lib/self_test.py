#!/usr/bin/env python3
"""Synthetic tests for the trading suite."""
from __future__ import annotations
import json, tempfile
from pathlib import Path
from risk_gate import validate
from trade_score import score
from session_controller import load_state, transition
from reconcile_order import reconcile

def proposal():
    return {
        "account_value": 50.0,
        "buying_power": 50.0,
        "currently_invested": 0.0,
        "session_start_value": 50.0,
        "session_pnl": 0.0,
        "high_water_mark": 50.0,
        "current_account_value": 50.0,
        "entry_price": 10.0,
        "invalidation_price": 9.6,
        "target_price": 10.8,
        # 10.0 @ 4% stop distance risks $0.40 <= the PROTECTED budget min(1% * $50, $100k) =
        # $0.50 (config/risk_limits.yaml is the limits authority since the 2026-07-08 hardening).
        "proposed_position_dollars": 10.0,
        "quote_age_seconds": 5,
        "open_positions": 0,
        "entries_this_session": 0,
        "account_verified": True,
        "buying_power_verified": True,
        "quote_timestamp_verified": True,
        "tradable": True,
        "fractional_verified": True,
        "catalyst_verified": True,
        "halted": False,
        "averaging_down": False,
        "security_type": "common_stock",
        "security_flags": [],
        "spread_pct": 0.002,
    }

tests = []

def check(name, condition):
    tests.append((name, bool(condition)))

p = proposal()
check("valid proposal passes", validate(p)["decision"] == "PASS")

q = proposal(); q["quote_age_seconds"] = 90
check("stale quote fails", "stale_quote" in validate(q)["failed_rules"])

q = proposal(); q["proposed_position_dollars"] = 40
check("oversized position fails", validate(q)["decision"] == "FAIL")

q = proposal(); q["security_flags"] = ["option"]
check("option flag fails", "disallowed_security_flags" in validate(q)["failed_rules"])

q = proposal(); q["target_price"] = 10.3
check("sub-2-to-1 reward risk fails", "reward_risk_below_minimum" in validate(q)["failed_rules"])

q = proposal(); q["session_pnl"] = -2.5
check("session loss limit fails", "session_loss_limit_reached" in validate(q)["failed_rules"])

s = score({"catalyst": 22, "trend_relative_strength": 17, "liquidity_volume": 13,
           "entry_quality": 12, "reward_risk": 12, "data_confidence": 8})
check("score calculation", s["score"] == 84 and s["eligible_for_risk_review"])

with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "state.json"
    check("missing state defaults read only", load_state(f)["state"] == "READ_ONLY")
    r = transition(f, "AWAITING_CONFIRMATION", "synthetic proposal")
    check("valid state transition", r["accepted"])
    r2 = transition(f, "CLOSED", "invalid direct close")
    check("invalid transition rejected", not r2["accepted"])

intent = {"symbol":"XYZ","side":"buy","order_type":"limit","dollar_amount":20,"quantity":None,"limit_price":10}
same = dict(intent)
rec = reconcile({"intent":intent,"reviewed":same,"submitted":same,"status":"submitted"})
check("matching order reconciles", rec["matched"])
bad = dict(same); bad["dollar_amount"] = 25
rec2 = reconcile({"intent":intent,"reviewed":same,"submitted":bad,"status":"submitted"})
check("mismatch pauses", rec2["decision"] == "PAUSE")

passed = sum(v for _, v in tests)
for name, ok in tests:
    print(("PASS" if ok else "FAIL") + " - " + name)
print(f"\n{passed}/{len(tests)} tests passed")
raise SystemExit(0 if passed == len(tests) else 1)
