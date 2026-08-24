#!/usr/bin/env python3
"""Tests: parallel options risk gate (shadow lane v0).

Locks the contracts: protected-file limits authority + tighten-only callers; every hard
screen fails closed with a stable reason code; sizing floor and one-contract cap; premarket
can never exceed PREOPEN_WATCH_ONLY; output can never carry an actionable/order-like status.
All filesystem effects confined to temp dirs. Run:
    python3 scripts/test_options_risk_gate.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import options_fixtures as fx  # noqa: E402
import options_risk_gate as gate  # noqa: E402

FORBIDDEN_OUTPUT_TOKENS = ("BUY", "OFFICIAL", "APPROVED_ORDER", "READY_TO_SEND",
                           "ACTIONABLE_TICKET")


class _WithConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "options_risk_limits.yaml"
        self.config.write_text(fx.OPTIONS_LIMITS_YAML)

    def tearDown(self):
        self._tmp.cleanup()

    def run_gate(self, **over):
        data = fx.gate_input(**over)
        return gate.validate(data, self.config)


class TestLimitsAuthority(_WithConfig):
    def test_happy_path_shadow_candidate(self):
        out = self.run_gate()
        self.assertEqual(out["status"], "SHADOW_OPTIONS_CANDIDATE")
        self.assertEqual(out["reason_codes"], [])
        self.assertEqual(out["sizing"]["contracts"], 1)
        self.assertAlmostEqual(out["sizing"]["max_premium_loss_dollars"], 120.0)
        self.assertAlmostEqual(out["sizing"]["risk_budget_dollars"], 150.0)

    def test_missing_config_fails_closed(self):
        out = gate.validate(fx.gate_input(), Path(self._tmp.name) / "absent.yaml")
        self.assertEqual(out["status"], "BLOCKED")
        self.assertIn("options_limits_config_unreadable", out["reason_codes"])

    def test_malformed_config_fails_closed(self):
        self.config.write_text("limits:\n\tmax: 1\n")
        out = self.run_gate()
        self.assertIn("options_limits_config_unreadable", out["reason_codes"])

    def test_caller_loosening_ignored(self):
        out = self.run_gate(limits={"maximum_relative_spread": 0.50})
        self.assertEqual(out["status"], "SHADOW_OPTIONS_CANDIDATE")
        self.assertIn("caller_limit_loosening_ignored:maximum_relative_spread",
                      out["warnings"])
        self.assertAlmostEqual(out["limits_effective"]["maximum_relative_spread"], 0.10)

    def test_caller_tightening_applies(self):
        out = self.run_gate(limits={"maximum_relative_spread": 0.05})
        self.assertIn("blocked_spread_above_maximum", out["reason_codes"])

    def test_caller_cannot_touch_multiplier_equality(self):
        out = self.run_gate(limits={"required_contract_multiplier": 50})
        self.assertEqual(out["status"], "SHADOW_OPTIONS_CANDIDATE")
        self.assertIn("required_contract_multiplier",
                      out["limits_authority"]["ignored_unknown"])

    def test_exit_deadline_via_tightened_exit_dte(self):
        out = self.run_gate(limits={"hard_exit_dte": 35})
        self.assertIn("blocked_exit_deadline_violation", out["reason_codes"])


class TestHardScreens(_WithConfig):
    def _expect(self, code, **over):
        out = self.run_gate(**over)
        self.assertEqual(out["status"], "BLOCKED", f"expected BLOCKED for {code}")
        self.assertIn(code, out["reason_codes"])

    def test_direction_strategy_mismatch(self):
        self._expect("blocked_direction_strategy_mismatch", option_type="put")

    def test_bearish_requires_put(self):
        out = self.run_gate(thesis_direction="bearish", option_type="put",
                            underlying_invalidation=236.0, underlying_target_1=222.0,
                            underlying_target_2=216.0, abs_delta=0.48)
        self.assertEqual(out["status"], "SHADOW_OPTIONS_CANDIDATE")

    def test_unknown_direction(self):
        self._expect("blocked_unknown_thesis_direction", thesis_direction="hold")

    def test_underlying_type_not_allowlisted(self):
        self._expect("blocked_underlying_type_not_allowlisted",
                     underlying_instrument_type="index")

    def test_thesis_gates(self):
        self._expect("blocked_thesis_not_final", thesis_final=False)
        self._expect("blocked_thesis_blocked", thesis_blocked=True)
        self._expect("blocked_thesis_stale", thesis_stale=True)
        self._expect("blocked_catalyst_provenance_unverified",
                     catalyst_provenance_verified=False)
        self._expect("blocked_provenance_incomplete", provenance_complete=False)

    def test_snapshot_not_valid(self):
        self._expect("blocked_snapshot_not_valid", snapshot_validation_status="REJECTED")

    def test_greek_units_unverified(self):
        self._expect("blocked_greek_units_unverified", greek_units_verified=False)

    def test_account_gates(self):
        self._expect("blocked_nav_unverified", nav_verified=False)
        self._expect("blocked_nav_stale", nav_fresh=False)
        self._expect("blocked_settled_cash_unverified", settled_cash_verified=False)
        self._expect("blocked_options_approval_unverified",
                     options_approval_verified=False)
        self._expect("blocked_account_type_not_cash", account_type="margin")
        self._expect("blocked_aggregate_exposure_unknown",
                     aggregate_exposure_verified=False)
        self._expect("blocked_correlated_exposure", correlated_exposure_blocked=True)

    def test_binary_event_blackout(self):
        self._expect("blocked_binary_event_in_window", binary_event_in_window=True)

    def test_invalid_underlying_levels(self):
        self._expect("blocked_invalid_underlying_levels", underlying_invalidation=240.0)

    def test_holding_period_bounds(self):
        self._expect("blocked_holding_period_invalid", intended_holding_days=11)
        self._expect("blocked_holding_period_invalid", intended_holding_days=0)

    def test_contract_screens(self):
        self._expect("blocked_dte_out_of_hard_range", dte=20)
        self._expect("blocked_dte_out_of_hard_range", dte=61)
        self._expect("blocked_delta_out_of_hard_range", abs_delta=0.30)
        self._expect("blocked_delta_out_of_hard_range", abs_delta=0.70)
        self._expect("blocked_open_interest_below_minimum", open_interest=499)
        self._expect("blocked_volume_below_minimum", same_session_volume=99)
        self._expect("blocked_spread_above_maximum", relative_spread=0.11)
        self._expect("blocked_nonstandard_multiplier", contract_multiplier=50)
        self._expect("blocked_adjusted_contract", adjusted_contract=True)
        self._expect("blocked_zero_bid_no_executable_liquidity", bid=0.0)
        self._expect("blocked_crossed_or_invalid_market", ask=1.10)
        self._expect("blocked_debit_not_within_market",
                     proposed_max_debit_per_contract=1.30)

    def test_stale_quote_regular_session(self):
        self._expect("blocked_quote_stale", quote_age_seconds=61.0)

    def test_session_inconsistent(self):
        self._expect("blocked_session_inconsistent_quotes", same_session_consistent=False)

    def test_other_session_not_eligible(self):
        self._expect("blocked_session_not_eligible", market_session="postmarket")

    def test_missing_field_fails_closed(self):
        data = fx.gate_input()
        del data["settled_cash"]
        out = gate.validate(data, self.config)
        self.assertIn("blocked_missing_or_invalid:settled_cash", out["reason_codes"])


class TestPremarket(_WithConfig):
    def test_premarket_caps_at_preopen_watch_only(self):
        out = self.run_gate(market_session="premarket", same_session_consistent=False,
                            quote_age_seconds=50000.0)
        self.assertEqual(out["status"], "PREOPEN_WATCH_ONLY")
        self.assertIn("premarket_marks_not_actionable", out["warnings"])
        self.assertEqual(out["sizing"]["contracts"], 0)

    def test_premarket_with_hard_failure_blocks(self):
        out = self.run_gate(market_session="premarket", open_interest=10)
        self.assertEqual(out["status"], "BLOCKED")


class TestSizing(_WithConfig):
    def test_insufficient_budget_blocks(self):
        out = self.run_gate(nav=20000.0)     # budget 100 < contract cost 120
        self.assertEqual(out["status"], "BLOCKED")
        self.assertIn("blocked_insufficient_risk_budget", out["reason_codes"])

    def test_one_contract_cap(self):
        out = self.run_gate(nav=1_000_000.0, settled_cash=100000.0)
        self.assertEqual(out["status"], "SHADOW_OPTIONS_CANDIDATE")
        self.assertGreater(out["sizing"]["contracts_raw"], 1)
        self.assertEqual(out["sizing"]["contracts"], 1)

    def test_aggregate_cap_reached(self):
        out = self.run_gate(aggregate_open_options_premium=300.0)  # cap = 1% of 30k = 300
        self.assertIn("blocked_aggregate_premium_cap_reached", out["reason_codes"])

    def test_aggregate_capacity_tightens_budget(self):
        out = self.run_gate(aggregate_open_options_premium=200.0)
        # remaining capacity 100 < per-candidate 150 -> budget 100 < cost 120 -> blocked
        self.assertIn("blocked_insufficient_risk_budget", out["reason_codes"])

    def test_settled_cash_insufficient(self):
        out = self.run_gate(settled_cash=100.0)
        self.assertIn("blocked_settled_cash_insufficient", out["reason_codes"])

    def test_buying_power_is_never_settled_cash(self):
        out = self.run_gate(settled_cash=100.0, buying_power=999999.0)
        self.assertIn("blocked_settled_cash_insufficient", out["reason_codes"])


class TestOutputDiscipline(_WithConfig):
    def test_no_forbidden_status_tokens_ever(self):
        for over in ({}, {"nav": 20000.0}, {"market_session": "premarket"},
                     {"thesis_final": False}):
            out = self.run_gate(**over)
            self.assertIn(out["status"], ("BLOCKED", "PREOPEN_WATCH_ONLY",
                                          "SHADOW_OPTIONS_CANDIDATE"))
            dumped = json.dumps(out)
            for token in FORBIDDEN_OUTPUT_TOKENS:
                self.assertNotIn(token, dumped)
            self.assertFalse(out["actionable"])
            self.assertTrue(out["shadow"])
            self.assertFalse(out["options_execution_enabled"])

    def test_equity_gate_still_rejects_options(self):
        """The bounded exception must not leak: an option object presented to the EQUITY
        gate still fails on instrument grounds."""
        sys.path.insert(0, str(ROOT / "scripts" / "lib"))
        import risk_gate as equity_gate  # noqa: E402
        proposal = {
            "account_value": 50000.0, "buying_power": 50000.0, "currently_invested": 0.0,
            "session_start_value": 50000.0, "session_pnl": 0.0, "high_water_mark": 50000.0,
            "current_account_value": 50000.0, "entry_price": 1.20,
            "invalidation_price": 0.60, "target_price": 2.60,
            "proposed_position_dollars": 120.0, "quote_age_seconds": 5,
            "open_positions": 0, "entries_this_session": 0, "account_verified": True,
            "buying_power_verified": True, "quote_timestamp_verified": True,
            "tradable": True, "fractional_verified": True, "catalyst_verified": True,
            "halted": False, "averaging_down": False,
            "security_type": "option", "security_flags": ["option"],
        }
        out = equity_gate.validate(proposal)
        self.assertEqual(out["decision"], "FAIL")
        self.assertIn("security_type_not_allowed", out["failed_rules"])
        self.assertIn("disallowed_security_flags", out["failed_rules"])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    raise SystemExit(0 if result.wasSuccessful() else 1)
