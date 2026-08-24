#!/usr/bin/env python3
"""Deterministic options payoff/exposure analytics for the research lane (v1).

Standard library only. No wall clock, no network, no broker, no option-pricing model.

WHAT THIS IS. Every number here is either (a) closed-form intrinsic payoff arithmetic at
EXPIRATION, or (b) a vendor-supplied Greek multiplied out into dollars and labeled as such.
Nothing is an option repricing model, so nothing here can fabricate an interim option price.
`scripts/options_shadow_runner.py` therefore still emits option_target_1/2 as
UNAVAILABLE_MODEL_UNVERIFIED; what this module adds is the model-free half that WAS
computable all along and was previously missing: breakeven, bounded maximum profit,
scenario P&L at expiration, the underlying move required, dollarized decay/vega exposure,
catalyst-vs-expiration timing, and an explicitly-labeled probability PROXY.

WHAT THIS IS NOT. Computing a payoff is not authorization to trade a structure.
`analyze()` accepts EXACTLY the ratified structures (CLAUDE.md sec.3c,
config/options_risk_limits.yaml allowlists.allowed_strategies): one long single-leg
standard equity/ETF call or put, debit only. Multi-leg structures - including every
defined-risk debit spread - are REFUSED here and stay refused until a SECOND explicit user
ratification; see data/options_pending_authorizations.json. The generic payoff helpers
below are pure mathematics used by tests and by the ratification packet's worked examples;
they are unreachable from the runtime lane, which never assembles more than one leg.

GROSS OF FEES, ALWAYS. No figure here is net of transaction costs. A caller may supply a
named official fee REFERENCE and it is recorded as such, but nothing is ever subtracted:
`fees.costs_modeled` is a hard-coded False and `fees.all_figures_are_gross_of_fees` a
hard-coded True. A complete, independently validated net-cost model is a separately
ratified future change; until it exists, estimated net figures are unavailable.

FAIL-CLOSED. Missing, stale, non-finite, or internally inconsistent input yields
status "BLOCKED" with named reason codes and zero size - never a best-effort number.
Options execution is permanently disabled; nothing here is actionable.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

SCHEMA_VERSION = "options_analytics/1.0"

RATIFIED_STRATEGIES = ("long_call", "long_put")
PENDING_AUTHORIZATION_RECORD = "data/options_pending_authorizations.json"

BASIS = "expiration_intrinsic_only"
FEE_MODEL_UNAVAILABLE = "UNAVAILABLE_NO_OFFICIAL_MODEL"
NET_FIGURES_UNAVAILABLE = "UNAVAILABLE_INCOMPLETE_COST_MODEL"
UNAVAILABLE_GREEK = "UNAVAILABLE_GREEK_MISSING"
UNBOUNDED = "UNBOUNDED_ABOVE"

# A fee REFERENCE is not a cost MODEL. This module performs no net-of-fees arithmetic
# anywhere - not on premium at risk, maximum loss, maximum profit, scenario P&L, returns,
# breakeven or risk/reward - so `costs_modeled` is a hard-coded False and
# `all_figures_are_gross_of_fees` a hard-coded True. Naming an official fee schedule does
# not change one cent of the arithmetic, and claiming otherwise would be a false
# disclosure. Complete cost semantics means the model states the OPENING, the CLOSING and
# the ROUND-TRIP cost separately; one ambiguous per-contract number does not, and never
# licenses a net figure. A validated net-cost model is a separately ratified future change.
COSTS_MODELED = False
ALL_FIGURES_ARE_GROSS_OF_FEES = True
FEE_SEMANTICS_REQUIRED = ("opening_per_contract_dollars", "closing_per_contract_dollars",
                          "round_trip_per_contract_dollars")
FEE_MODEL_ALLOWED_FIELDS = (("official_source", "per_contract_dollars")
                            + FEE_SEMANTICS_REQUIRED)

# Modeled decay is a straight-line extrapolation of ONE vendor theta point estimate. Theta
# is not constant - it accelerates into expiration - so the figure is an approximation and
# is labeled as one everywhere it appears.
THETA_METHOD = "CONSTANT_THETA_LINEAR_APPROXIMATION"
THETA_NOTE = ("linear extrapolation of a single vendor theta point estimate "
              "(theta x multiplier x contracts x intended holding days). Theta is NOT "
              "constant: it accelerates as expiration approaches, so this approximation "
              "can materially UNDERSTATE realized decay near expiry. It is not a repricing "
              "model, not a forecast, and not a guaranteed path.")

# Mirrors config/options_risk_limits.yaml (PROTECTED). Defense in depth at the analytics
# boundary: scripts/lib/options_risk_gate.py already enforces the same window, and
# scripts/test_options_analytics.py pins these constants against the protected file so the
# two can never silently drift apart. Never widen these here.
MIN_INTENDED_HOLDING_DAYS = 1
MAX_INTENDED_HOLDING_DAYS = 10

_STRATEGY_TO_TYPE = {"long_call": "call", "long_put": "put"}
_DIRECTION_TO_STRATEGY = {"bullish": "long_call", "bearish": "long_put"}

REQUIRED_NUMBERS = ("underlying_spot", "debit_per_contract", "underlying_target_1",
                    "underlying_target_2", "underlying_invalidation", "strike")
REQUIRED_INTS = ("contracts", "contract_multiplier", "dte", "intended_holding_days")
REQUIRED_STRINGS = ("strategy", "direction", "option_type", "expiration", "market_date")


def finite(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Pure payoff mathematics (leg sets). Mathematics only - see the module docstring: these
# helpers grant no authorization, and analyze() refuses every structure but the ratified
# single long leg.
# --------------------------------------------------------------------------------------

def intrinsic(option_type: str, strike: float, underlying_price: float) -> float:
    """Intrinsic value per share at expiration. Deterministic, model-free."""
    if option_type == "call":
        return max(underlying_price - strike, 0.0)
    if option_type == "put":
        return max(strike - underlying_price, 0.0)
    raise ValueError(f"unknown option_type: {option_type!r}")


def _leg_signed_quantity(leg: dict) -> float:
    return float(leg["quantity"]) * (1.0 if leg["side"] == "long" else -1.0)


def payoff_at_expiration(legs: list, underlying_price: float,
                         multiplier: int = 100) -> float:
    """Net P&L in dollars of a vanilla leg set at expiration, premiums included.

    Long legs pay their premium (a debit); short legs receive it. Intrinsic-only: no time
    value survives expiration, so no pricing model is involved.
    """
    total = 0.0
    for leg in legs:
        signed = _leg_signed_quantity(leg)
        value = intrinsic(leg["option_type"], float(leg["strike"]), float(underlying_price))
        total += signed * (value - float(leg["premium"])) * multiplier
    return total


def net_debit(legs: list, multiplier: int = 100) -> float:
    """Positive when the structure is paid for (a debit); negative for a net credit."""
    return sum(_leg_signed_quantity(leg) * float(leg["premium"]) for leg in legs) * multiplier


def _breakpoints(legs: list) -> list:
    return sorted({0.0} | {float(leg["strike"]) for leg in legs})


def _slope_above(legs: list, price: float, multiplier: int) -> float:
    """d(payoff)/d(underlying) just above `price`."""
    slope = 0.0
    for leg in legs:
        strike = float(leg["strike"])
        signed = _leg_signed_quantity(leg)
        if leg["option_type"] == "call" and price >= strike:
            slope += signed
        elif leg["option_type"] == "put" and price < strike:
            slope -= signed
    return slope * multiplier


def max_profit(legs: list, multiplier: int = 100):
    """Bounded maximum profit in dollars, or the UNBOUNDED sentinel.

    Payoff is piecewise linear in the underlying, so the maximum over [0, inf) is attained
    at a breakpoint unless the slope above the highest strike is positive.
    """
    points = _breakpoints(legs)
    top = points[-1]
    if _slope_above(legs, top + 1.0, multiplier) > 1e-12:
        return UNBOUNDED
    return max(payoff_at_expiration(legs, price, multiplier) for price in points)


def max_loss(legs: list, multiplier: int = 100):
    """Worst-case loss in dollars (negative), or the UNBOUNDED sentinel below zero."""
    points = _breakpoints(legs)
    # The underlying cannot fall below zero, so the downside is always attained at a
    # breakpoint. Only a net-short call above the highest strike makes the loss unbounded.
    if _slope_above(legs, points[-1] + 1.0, multiplier) < -1e-12:
        return UNBOUNDED
    return min(payoff_at_expiration(legs, price, multiplier) for price in points)


def breakevens(legs: list, multiplier: int = 100) -> list:
    """Underlying prices where the expiration payoff crosses zero, ascending."""
    points = _breakpoints(legs)
    found: list = []
    for index in range(len(points) - 1):
        low, high = points[index], points[index + 1]
        p_low = payoff_at_expiration(legs, low, multiplier)
        p_high = payoff_at_expiration(legs, high, multiplier)
        if abs(p_low) < 1e-9:
            found.append(low)
        if (p_low < 0 < p_high) or (p_low > 0 > p_high):
            found.append(low + (high - low) * (-p_low) / (p_high - p_low))
    last = points[-1]
    p_last = payoff_at_expiration(legs, last, multiplier)
    if abs(p_last) < 1e-9:
        found.append(last)
    slope = _slope_above(legs, last + 1.0, multiplier)
    if abs(slope) > 1e-12:
        crossing = last - p_last / slope
        if crossing > last and (p_last < 0) == (slope > 0):
            found.append(crossing)
    return sorted(round(value, 6) for value in dict.fromkeys(found))


# --------------------------------------------------------------------------------------
# Ratified-structure analysis
# --------------------------------------------------------------------------------------

def _block(out: dict, code: str) -> None:
    if code not in out["reason_codes"]:
        out["reason_codes"].append(code)


def _empty_result() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "BLOCKED",
        "reason_codes": [],
        "warnings": [],
        "basis": BASIS,
        "not_a_repricing_model": True,
        "strategy": None,
        "legs": [],
        "fees": gross_only_fees(),
        "underlying_spot": None,
        "premium_at_risk_dollars": 0.0,
        "max_loss_dollars": 0.0,
        "max_profit_dollars": None,
        "max_profit_bounded": False,
        "max_profit_note": None,
        "breakeven_underlying": None,
        "breakeven_move_pct": None,
        "underlying_move_required_pct": None,
        "scenario_pnl": {},
        "risk_reward_at_expiration": None,
        "time_decay": UNAVAILABLE_GREEK,
        "volatility_sensitivity": UNAVAILABLE_GREEK,
        "catalyst_timing": {},
        "probability_estimates": {},
        "assignment_exercise_risk": {},
        "data_completeness": {},
        "options_execution_enabled": False,
        "broker_write_authority": False,
        "actionable": False,
        "shadow": True,
    }


def gross_only_fees(*, reference_present: bool = False, source: object = None,
                    per_contract: object = None, semantics_complete: bool = False) -> dict:
    """The ONLY fee block this module emits. Costs are never modeled in this version.

    A named official fee schedule is recorded as a REFERENCE. It is never applied: no figure
    produced anywhere in this module subtracts a commission, exchange, regulatory or
    slippage cost, so `costs_modeled` and `all_figures_are_gross_of_fees` are constants and
    `estimated_net_figures` is permanently unavailable. Consumers deciding whether costs
    were actually deducted must read `costs_modeled` - never `official_model_present`.
    """
    named = str(source)[:128] if reference_present else None
    return {
        "model": named if reference_present else FEE_MODEL_UNAVAILABLE,
        "fee_reference_present": bool(reference_present),
        "fee_reference_source": named,
        "fee_reference_per_contract_dollars": (
            float(per_contract) if finite(per_contract) else None),
        "fee_semantics_complete": bool(semantics_complete),
        "costs_modeled": COSTS_MODELED,
        "all_figures_are_gross_of_fees": ALL_FIGURES_ARE_GROSS_OF_FEES,
        "estimated_net_figures": NET_FIGURES_UNAVAILABLE,
        # Retained for API compatibility. It has only ever meant "a named official fee
        # REFERENCE is present"; it has never meant that fees were applied to any figure.
        "official_model_present": bool(reference_present),
        "note": ("every payoff, scenario, return, breakeven and risk/reward figure in this "
                 "result is GROSS OF FEES: no commission, exchange, regulatory or slippage "
                 "cost is subtracted anywhere, and realized results will be lower"),
    }


def _check_fee_model(position: dict, out: dict) -> None:
    """Record the fee REFERENCE. Never applies a cost and never produces a net figure."""
    model = position.get("fee_model")
    if model is None:
        out["warnings"].append("fee_model_unavailable_all_figures_gross_of_fees")
        return
    if not isinstance(model, dict) or not isinstance(model.get("official_source"), str) \
            or not model.get("official_source"):
        # An estimate without a named official source would be a fabricated cost figure.
        _block(out, "blocked_fee_estimate_without_official_model")
        return
    if any(key not in FEE_MODEL_ALLOWED_FIELDS for key in model):
        # Unknown fee fields fail closed: an unrecognized field may carry cost semantics
        # this version does not understand, and guessing would produce a false disclosure.
        _block(out, "blocked_fee_model_unknown_field")
        return
    for key in FEE_MODEL_ALLOWED_FIELDS:
        if key == "official_source" or key not in model:
            continue
        value = model[key]
        # finite() rejects bool, NaN and infinity; negatives are rejected explicitly.
        if not finite(value) or float(value) < 0:
            _block(out, "blocked_fee_model_malformed")
            return
    semantics_complete = all(key in model for key in FEE_SEMANTICS_REQUIRED)
    if "per_contract_dollars" not in model and not semantics_complete:
        _block(out, "blocked_fee_model_malformed")
        return
    if not semantics_complete:
        # An ambiguous single per-contract number states neither the opening nor the closing
        # nor the round-trip cost, so no net figure can be derived from it.
        out["warnings"].append("fee_model_semantics_incomplete_net_figures_unavailable")
    out["warnings"].append("fee_reference_present_but_costs_not_modeled_figures_stay_gross")
    out["fees"] = gross_only_fees(reference_present=True,
                                  source=model["official_source"],
                                  per_contract=model.get("per_contract_dollars"),
                                  semantics_complete=semantics_complete)


def _check_structure(position: dict, out: dict) -> str | None:
    strategy = position.get("strategy")
    legs = position.get("legs")
    if strategy not in RATIFIED_STRATEGIES:
        _block(out, "blocked_strategy_not_ratified")
        out["warnings"].append(f"pending_authorization_record:{PENDING_AUTHORIZATION_RECORD}")
        return None
    if legs is not None:
        if not isinstance(legs, list) or len(legs) != 1:
            _block(out, "blocked_multi_leg_not_ratified")
            return None
        leg = legs[0]
        if not isinstance(leg, dict) or leg.get("side") != "long":
            _block(out, "blocked_short_leg_not_permitted")
            return None
    return strategy


def analyze(position: object) -> dict:
    """Analyze ONE ratified long single-leg debit position. Fail-closed."""
    out = _empty_result()
    if not isinstance(position, dict):
        _block(out, "blocked_position_not_an_object")
        return out

    strategy = _check_structure(position, out)
    _check_fee_model(position, out)

    for key in REQUIRED_NUMBERS:
        if not finite(position.get(key)):
            _block(out, f"blocked_missing_or_invalid:{key}")
    for key in REQUIRED_INTS:
        if not _positive_int(position.get(key)):
            _block(out, f"blocked_missing_or_invalid:{key}")
    # Defense in depth at the analytics boundary: the protected limits require a hold of at
    # least one and at most ten trading days. _positive_int already rejects bool and
    # non-integers; this rejects a structurally valid integer outside the ratified window
    # (a zero-day "hold" is not a swing-trade expression and can never be analyzed as one).
    holding_days = position.get("intended_holding_days")
    if _positive_int(holding_days) and not (
            MIN_INTENDED_HOLDING_DAYS <= int(holding_days) <= MAX_INTENDED_HOLDING_DAYS):
        _block(out, "blocked_intended_holding_days_outside_policy")
    for key in REQUIRED_STRINGS:
        if not isinstance(position.get(key), str) or not position.get(key):
            _block(out, f"blocked_missing_or_invalid:{key}")
    if out["reason_codes"] or strategy is None:
        return out

    out["strategy"] = strategy
    option_type = _STRATEGY_TO_TYPE[strategy]
    if position["option_type"].lower() != option_type:
        _block(out, "blocked_direction_strategy_mismatch")
    if _DIRECTION_TO_STRATEGY.get(position["direction"].lower()) != strategy:
        _block(out, "blocked_direction_strategy_mismatch")

    multiplier = int(position["contract_multiplier"])
    if multiplier != 100:
        _block(out, "blocked_nonstandard_multiplier")
    contracts = int(position["contracts"])
    if contracts < 1:
        # Zero size is the correct fail-closed answer upstream; it is never an analyzable
        # position, so it never becomes a research candidate.
        _block(out, "blocked_zero_contracts")
    debit = float(position["debit_per_contract"])
    if debit <= 0:
        _block(out, "blocked_non_positive_premium")
    strike = float(position["strike"])
    spot = float(position["underlying_spot"])
    if strike <= 0 or spot <= 0:
        _block(out, "blocked_non_positive_price_input")

    # Greeks / IV completeness. A vendor Greek is only meaningful once its units are
    # verified; the caller asserts that separately (greek_units_verified).
    completeness = {}
    for field in ("implied_volatility", "delta", "gamma", "theta", "vega"):
        completeness[field] = finite(position.get(field))
    completeness["complete"] = all(completeness[field] for field in
                                   ("implied_volatility", "delta", "theta", "vega"))
    out["data_completeness"] = completeness
    if not completeness["implied_volatility"]:
        _block(out, "blocked_missing_iv")
    for greek in ("delta", "theta", "vega"):
        if not completeness[greek]:
            _block(out, f"blocked_missing_greek:{greek}")
    if not completeness["gamma"]:
        out["warnings"].append("gamma_missing_convexity_not_quantified")
    if position.get("greek_units_verified") is not True:
        _block(out, "blocked_greek_units_unverified")

    # Timing: the expression must outlive the thesis-driving event.
    expiration = _iso_date(position["expiration"])
    market_date = _iso_date(position["market_date"])
    catalyst = position.get("catalyst_date")
    catalyst_date = _iso_date(catalyst) if catalyst not in (None, "") else None
    if expiration is None or market_date is None:
        _block(out, "blocked_invalid_date_input")
    if catalyst not in (None, "") and catalyst_date is None:
        _block(out, "blocked_invalid_catalyst_date")
    holding = int(position["intended_holding_days"])
    dte = int(position["dte"])
    timing = {"expiration": position["expiration"], "market_date": position["market_date"],
              "catalyst_date": catalyst if catalyst else None, "dte": dte,
              "intended_holding_days": holding}
    if expiration is not None and market_date is not None:
        if dte < holding:
            _block(out, "blocked_expiration_before_planned_exit")
        if catalyst_date is not None:
            if catalyst_date > expiration:
                _block(out, "blocked_catalyst_after_expiration")
            elif catalyst_date == expiration:
                _block(out, "blocked_expiration_not_after_catalyst")
            elif catalyst_date < market_date:
                out["warnings"].append("catalyst_already_occurred_before_target_session")
            timing["expiration_after_catalyst"] = catalyst_date < expiration
            timing["catalyst_inside_holding_window"] = (
                market_date <= catalyst_date <= expiration)
        else:
            out["warnings"].append("catalyst_date_absent_timing_fit_unverified")
            timing["expiration_after_catalyst"] = None
            timing["catalyst_inside_holding_window"] = None
    out["catalyst_timing"] = timing

    if out["reason_codes"]:
        return out

    legs = [{"side": "long", "option_type": option_type, "strike": strike,
             "quantity": contracts, "premium": debit}]
    out["legs"] = legs

    out["underlying_spot"] = round(spot, 6)
    premium_at_risk = round(debit * multiplier * contracts, 4)
    out["premium_at_risk_dollars"] = premium_at_risk
    out["max_loss_dollars"] = premium_at_risk
    profit = max_profit(legs, multiplier)
    if profit == UNBOUNDED:
        out["max_profit_dollars"] = None
        out["max_profit_bounded"] = False
        out["max_profit_note"] = ("unbounded above at expiration; an unbounded MODEL "
                                  "maximum is not an expected or attainable outcome")
    else:
        out["max_profit_dollars"] = round(float(profit), 4)
        out["max_profit_bounded"] = True
        out["max_profit_note"] = ("bounded at an underlying of 0.00; a long put cannot "
                                  "gain more than its strike less the premium paid")

    crossings = breakevens(legs, multiplier)
    breakeven = crossings[0] if crossings else None
    out["breakeven_underlying"] = breakeven
    if breakeven is not None:
        move = (breakeven - spot) / spot if option_type == "call" else (spot - breakeven) / spot
        out["breakeven_move_pct"] = round(move * 100.0, 4)
        out["underlying_move_required_pct"] = round(move * 100.0, 4)

    scenarios = {}
    for label, level in (("target_1", position["underlying_target_1"]),
                         ("target_2", position["underlying_target_2"]),
                         ("invalidation", position["underlying_invalidation"])):
        price = float(level)
        pnl = payoff_at_expiration(legs, price, multiplier)
        scenarios[label] = {
            "underlying": round(price, 6),
            "pnl_dollars_at_expiration": round(pnl, 4),
            "return_on_premium_pct": round(pnl / premium_at_risk * 100.0, 4)
            if premium_at_risk else None,
            "basis": BASIS,
            # Constants, not derived: no scenario figure has a cost subtracted from it.
            "gross_of_fees": ALL_FIGURES_ARE_GROSS_OF_FEES,
            "costs_modeled": COSTS_MODELED,
        }
    out["scenario_pnl"] = scenarios

    target_pnl = scenarios["target_1"]["pnl_dollars_at_expiration"]
    if target_pnl <= 0:
        # At expiration the primary target does not even repay the premium: the structure
        # is a modeled loss at the thesis target. That is not a research candidate.
        _block(out, "blocked_target_1_below_breakeven")
        return out
    out["risk_reward_at_expiration"] = round(target_pnl / premium_at_risk, 4)

    theta = float(position["theta"])
    vega = float(position["vega"])
    out["time_decay"] = {
        "vendor_theta_per_share_per_day": theta,
        "dollars_per_day": round(theta * multiplier * contracts, 4),
        "modeled_decay_over_holding_dollars": round(theta * multiplier * contracts * holding,
                                                    4),
        "method": THETA_METHOD,
        "assumes_theta_constant_over_holding_period": True,
        "assumes_static_underlying_and_iv": True,
        "is_a_repricing_model": False,
        "is_a_forecast": False,
        "can_understate_decay_near_expiration": True,
        "note": THETA_NOTE,
    }
    out["volatility_sensitivity"] = {
        "vendor_vega_per_share": vega,
        "dollars_per_one_iv_point": round(vega * multiplier * contracts, 4),
        "implied_volatility": float(position["implied_volatility"]),
        "expansion_helps_long_premium": True,
        "contraction_hurts_long_premium": True,
        "units_source": "vendor_supplied_greek_units_verified",
    }
    out["probability_estimates"] = {
        "itm_proxy_abs_delta": round(abs(float(position["delta"])), 6),
        "label": "ESTIMATE_NOT_A_FACT",
        "source": "vendor delta used as a rough risk-neutral ITM proxy",
        "caveat": ("delta is not a probability; it is a hedge ratio that only loosely "
                   "approximates risk-neutral ITM likelihood and says nothing about the "
                   "real-world distribution"),
    }
    out["assignment_exercise_risk"] = {
        "early_assignment_risk": "none_long_only_holder_controls_exercise",
        "auto_exercise_if_itm_at_expiry": True,
        "mitigation": "research rule closes the proposal well before expiry (hard_exit_dte)",
        "exercise_workflow_permitted": False,
        "note": "all exercise/assignment handling is manual and user-owned; this system "
                "can never exercise, roll, or close an option position",
    }
    out["status"] = "OK"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON position file, or - for stdin")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
        result = analyze(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - CLI boundary, fail closed
        result = {"schema_version": SCHEMA_VERSION, "status": "BLOCKED",
                  "reason_codes": ["blocked_input_error"], "warnings": [str(exc)]}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
