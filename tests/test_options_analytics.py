#!/usr/bin/env python3
"""Tests: deterministic options analytics (v1) - payoff mathematics and fail-closed gates.

    python3 scripts/test_options_analytics.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import options_analytics as oa  # noqa: E402
import options_fixtures as fx  # noqa: E402


class TestPayoffMathematics(unittest.TestCase):
    """Pure intrinsic arithmetic. Computing a payoff is not authorization to trade it."""

    def test_long_call_intrinsic(self):
        self.assertEqual(oa.intrinsic("call", 100.0, 130.0), 30.0)
        self.assertEqual(oa.intrinsic("call", 100.0, 90.0), 0.0)

    def test_long_put_intrinsic(self):
        self.assertEqual(oa.intrinsic("put", 100.0, 70.0), 30.0)
        self.assertEqual(oa.intrinsic("put", 100.0, 130.0), 0.0)

    def test_unknown_option_type_raises(self):
        with self.assertRaises(ValueError):
            oa.intrinsic("straddle", 100.0, 100.0)

    def test_long_call_payoff_max_loss_and_breakeven(self):
        legs = [{"side": "long", "option_type": "call", "strike": 230.0,
                 "quantity": 1, "premium": 1.20}]
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 229.0), -120.0)
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 231.20), 0.0)
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 238.0), 680.0)
        self.assertEqual(oa.max_loss(legs), -120.0)
        self.assertEqual(oa.max_profit(legs), oa.UNBOUNDED)
        self.assertEqual(oa.breakevens(legs), [231.2])

    def test_long_put_max_profit_is_bounded_at_zero_underlying(self):
        legs = [{"side": "long", "option_type": "put", "strike": 220.0,
                 "quantity": 1, "premium": 1.20}]
        self.assertEqual(oa.max_profit(legs), 22000.0 - 120.0)
        self.assertEqual(oa.max_loss(legs), -120.0)
        self.assertEqual(oa.breakevens(legs), [218.8])

    def test_call_debit_spread_max_profit_loss_and_breakeven(self):
        """Defined-risk call debit spread mathematics. NOT a ratified structure: see
        analyze() and data/options_pending_authorizations.json."""
        legs = [
            {"side": "long", "option_type": "call", "strike": 100.0,
             "quantity": 1, "premium": 6.0},
            {"side": "short", "option_type": "call", "strike": 110.0,
             "quantity": 1, "premium": 2.0},
        ]
        self.assertAlmostEqual(oa.net_debit(legs), 400.0)
        self.assertAlmostEqual(oa.max_profit(legs), 600.0)     # (110-100-4) * 100
        self.assertAlmostEqual(oa.max_loss(legs), -400.0)      # net debit paid
        self.assertEqual(oa.breakevens(legs), [104.0])
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 120.0), 600.0)
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 95.0), -400.0)

    def test_put_debit_spread_max_profit_loss_and_breakeven(self):
        legs = [
            {"side": "long", "option_type": "put", "strike": 100.0,
             "quantity": 1, "premium": 6.0},
            {"side": "short", "option_type": "put", "strike": 90.0,
             "quantity": 1, "premium": 2.0},
        ]
        self.assertAlmostEqual(oa.net_debit(legs), 400.0)
        self.assertAlmostEqual(oa.max_profit(legs), 600.0)
        self.assertAlmostEqual(oa.max_loss(legs), -400.0)
        self.assertEqual(oa.breakevens(legs), [96.0])
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 80.0), 600.0)

    def test_payoff_scales_with_quantity(self):
        legs = [{"side": "long", "option_type": "call", "strike": 100.0,
                 "quantity": 3, "premium": 2.0}]
        self.assertAlmostEqual(oa.payoff_at_expiration(legs, 110.0), 2400.0)


class TestRatifiedStructureOnly(unittest.TestCase):
    def test_only_long_call_and_long_put_are_ratified(self):
        self.assertEqual(oa.RATIFIED_STRATEGIES, ("long_call", "long_put"))

    def test_debit_spread_is_refused_by_analyze(self):
        for strategy in ("call_debit_spread", "put_debit_spread", "iron_condor",
                         "covered_call", "short_put", "calendar"):
            result = oa.analyze(fx.analytics_position(strategy=strategy))
            self.assertEqual(result["status"], "BLOCKED", strategy)
            self.assertIn("blocked_strategy_not_ratified", result["reason_codes"], strategy)
            self.assertTrue(any("pending_authorization_record" in w
                                for w in result["warnings"]), strategy)

    def test_multi_leg_payload_is_refused(self):
        legs = [
            {"side": "long", "option_type": "call", "strike": 230.0,
             "quantity": 1, "premium": 1.20},
            {"side": "short", "option_type": "call", "strike": 240.0,
             "quantity": 1, "premium": 0.40},
        ]
        result = oa.analyze(fx.analytics_position(legs=legs))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_multi_leg_not_ratified", result["reason_codes"])

    def test_short_leg_is_refused(self):
        legs = [{"side": "short", "option_type": "call", "strike": 230.0,
                 "quantity": 1, "premium": 1.20}]
        result = oa.analyze(fx.analytics_position(legs=legs))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_short_leg_not_permitted", result["reason_codes"])


class TestValidLongSingleLeg(unittest.TestCase):
    def test_valid_long_call(self):
        result = oa.analyze(fx.analytics_position())
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertEqual(result["strategy"], "long_call")
        self.assertEqual(result["max_loss_dollars"], 120.0)
        self.assertEqual(result["premium_at_risk_dollars"], 120.0)
        self.assertFalse(result["max_profit_bounded"])
        self.assertIsNone(result["max_profit_dollars"])
        self.assertEqual(result["breakeven_underlying"], 231.2)
        self.assertAlmostEqual(result["underlying_move_required_pct"], 0.5217, places=3)
        self.assertEqual(
            result["scenario_pnl"]["target_1"]["pnl_dollars_at_expiration"], 680.0)
        self.assertEqual(
            result["scenario_pnl"]["invalidation"]["pnl_dollars_at_expiration"], -120.0)
        self.assertAlmostEqual(result["risk_reward_at_expiration"], 5.6667, places=3)
        self.assertEqual(result["basis"], "expiration_intrinsic_only")
        self.assertTrue(result["not_a_repricing_model"])
        self.assertFalse(result["options_execution_enabled"])
        self.assertFalse(result["actionable"])

    def test_valid_long_put(self):
        result = oa.analyze(fx.analytics_position(
            strategy="long_put", direction="bearish", option_type="put", strike=220.0,
            delta=-0.48, underlying_target_1=210.0, underlying_target_2=204.0,
            underlying_invalidation=236.0))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertTrue(result["max_profit_bounded"])
        self.assertEqual(result["max_profit_dollars"], 21880.0)
        self.assertEqual(result["breakeven_underlying"], 218.8)
        self.assertEqual(
            result["scenario_pnl"]["target_1"]["pnl_dollars_at_expiration"], 880.0)

    def test_maximum_loss_scales_with_contracts(self):
        result = oa.analyze(fx.analytics_position(contracts=3))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertEqual(result["max_loss_dollars"], 360.0)
        self.assertEqual(
            result["scenario_pnl"]["target_1"]["pnl_dollars_at_expiration"], 2040.0)

    def test_time_decay_and_vega_are_dollarized_and_labeled(self):
        result = oa.analyze(fx.analytics_position())
        decay = result["time_decay"]
        self.assertAlmostEqual(decay["dollars_per_day"], -3.0)
        self.assertAlmostEqual(decay["modeled_decay_over_holding_dollars"], -15.0)
        self.assertTrue(decay["assumes_static_underlying_and_iv"])
        vol = result["volatility_sensitivity"]
        self.assertAlmostEqual(vol["dollars_per_one_iv_point"], 11.0)
        self.assertTrue(vol["contraction_hurts_long_premium"])

    def test_probability_is_labeled_an_estimate_not_a_fact(self):
        result = oa.analyze(fx.analytics_position())
        probability = result["probability_estimates"]
        self.assertEqual(probability["label"], "ESTIMATE_NOT_A_FACT")
        self.assertAlmostEqual(probability["itm_proxy_abs_delta"], 0.52)
        self.assertIn("not a probability", probability["caveat"])

    def test_assignment_risk_record_forbids_exercise_workflow(self):
        result = oa.analyze(fx.analytics_position())
        assignment = result["assignment_exercise_risk"]
        self.assertFalse(assignment["exercise_workflow_permitted"])
        self.assertTrue(assignment["auto_exercise_if_itm_at_expiry"])

    def test_spot_used_is_published_for_downstream_consumers(self):
        result = oa.analyze(fx.analytics_position())
        self.assertEqual(result["underlying_spot"], 230.0)
        # breakeven - spot, expressed against spot, is exactly the published required move.
        implied = (result["breakeven_underlying"] - result["underlying_spot"]) \
            / result["underlying_spot"] * 100.0
        self.assertAlmostEqual(implied, result["underlying_move_required_pct"], places=4)

    def test_deterministic_for_identical_input(self):
        first = oa.analyze(fx.analytics_position())
        second = oa.analyze(fx.analytics_position())
        self.assertEqual(first, second)


class TestFailClosedGates(unittest.TestCase):
    def _blocked(self, **over) -> dict:
        result = oa.analyze(fx.analytics_position(**over))
        self.assertEqual(result["status"], "BLOCKED", result)
        return result

    def test_position_not_an_object(self):
        result = oa.analyze(["not", "a", "dict"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_position_not_an_object", result["reason_codes"])

    def test_missing_spot(self):
        result = self._blocked(underlying_spot=None)
        self.assertIn("blocked_missing_or_invalid:underlying_spot", result["reason_codes"])

    def test_missing_expiration(self):
        result = self._blocked(expiration=None)
        self.assertIn("blocked_missing_or_invalid:expiration", result["reason_codes"])

    def test_missing_multiplier(self):
        result = self._blocked(contract_multiplier=None)
        self.assertIn("blocked_missing_or_invalid:contract_multiplier",
                      result["reason_codes"])

    def test_nonstandard_multiplier(self):
        result = self._blocked(contract_multiplier=50)
        self.assertIn("blocked_nonstandard_multiplier", result["reason_codes"])

    def test_negative_premium(self):
        result = self._blocked(debit_per_contract=-1.20)
        self.assertIn("blocked_non_positive_premium", result["reason_codes"])

    def test_zero_premium(self):
        result = self._blocked(debit_per_contract=0.0)
        self.assertIn("blocked_non_positive_premium", result["reason_codes"])

    def test_zero_contracts_is_fail_closed(self):
        result = self._blocked(contracts=0)
        self.assertIn("blocked_zero_contracts", result["reason_codes"])
        self.assertEqual(result["max_loss_dollars"], 0.0)

    def test_missing_iv(self):
        result = self._blocked(implied_volatility=None)
        self.assertIn("blocked_missing_iv", result["reason_codes"])
        self.assertFalse(result["data_completeness"]["implied_volatility"])

    def test_missing_greeks(self):
        for greek in ("delta", "theta", "vega"):
            result = self._blocked(**{greek: None})
            self.assertIn(f"blocked_missing_greek:{greek}", result["reason_codes"], greek)

    def test_missing_gamma_is_a_warning_not_a_block(self):
        result = oa.analyze(fx.analytics_position(gamma=None))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertIn("gamma_missing_convexity_not_quantified", result["warnings"])
        self.assertFalse(result["data_completeness"]["gamma"])

    def test_unverified_greek_units_block(self):
        result = self._blocked(greek_units_verified=False)
        self.assertIn("blocked_greek_units_unverified", result["reason_codes"])

    def test_expiration_before_catalyst(self):
        result = self._blocked(catalyst_date="2026-10-01")
        self.assertIn("blocked_catalyst_after_expiration", result["reason_codes"])

    def test_catalyst_exactly_on_expiration(self):
        result = self._blocked(catalyst_date=fx.EXPIRY)
        self.assertIn("blocked_expiration_not_after_catalyst", result["reason_codes"])

    def test_expiration_before_planned_exit(self):
        result = self._blocked(dte=3, intended_holding_days=10)
        self.assertIn("blocked_expiration_before_planned_exit", result["reason_codes"])

    def test_malformed_catalyst_date(self):
        result = self._blocked(catalyst_date="not-a-date")
        self.assertIn("blocked_invalid_catalyst_date", result["reason_codes"])

    def test_absent_catalyst_is_a_warning(self):
        result = oa.analyze(fx.analytics_position(catalyst_date=None))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertIn("catalyst_date_absent_timing_fit_unverified", result["warnings"])
        self.assertIsNone(result["catalyst_timing"]["expiration_after_catalyst"])

    def test_direction_strategy_mismatch(self):
        result = self._blocked(direction="bearish")
        self.assertIn("blocked_direction_strategy_mismatch", result["reason_codes"])

    def test_option_type_mismatch(self):
        result = self._blocked(option_type="put")
        self.assertIn("blocked_direction_strategy_mismatch", result["reason_codes"])

    def test_target_below_breakeven_blocks(self):
        result = self._blocked(underlying_target_1=230.5, underlying_target_2=231.0)
        self.assertIn("blocked_target_1_below_breakeven", result["reason_codes"])
        self.assertIsNone(result["risk_reward_at_expiration"])

    def test_non_finite_inputs_block(self):
        result = self._blocked(underlying_spot=float("nan"))
        self.assertIn("blocked_missing_or_invalid:underlying_spot", result["reason_codes"])


REFERENCE_ONLY_FEE_MODEL = {"official_source": "broker published fee schedule (verified)",
                            "per_contract_dollars": 0.03}

# Every figure a reader could mistake for a net result. None of them may move because a fee
# reference was named, and none of them may be labeled net of fees.
PAYOFF_FIELDS = ("premium_at_risk_dollars", "max_loss_dollars", "max_profit_dollars",
                 "max_profit_bounded", "breakeven_underlying", "breakeven_move_pct",
                 "underlying_move_required_pct", "risk_reward_at_expiration",
                 "scenario_pnl")


class TestFeeModel(unittest.TestCase):
    """The lane models NO transaction cost. A fee REFERENCE is not a cost MODEL, and every
    figure stays explicitly gross of fees until a separately ratified, complete and
    independently tested net-cost model exists."""

    def test_absent_fee_model_is_labeled_gross(self):
        result = oa.analyze(fx.analytics_position())
        fees = result["fees"]
        self.assertEqual(fees["model"], oa.FEE_MODEL_UNAVAILABLE)
        self.assertTrue(fees["all_figures_are_gross_of_fees"])
        self.assertFalse(fees["fee_reference_present"])
        self.assertIsNone(fees["fee_reference_source"])
        self.assertFalse(fees["fee_semantics_complete"])
        self.assertFalse(fees["costs_modeled"])
        self.assertEqual(fees["estimated_net_figures"], oa.NET_FIGURES_UNAVAILABLE)
        self.assertFalse(fees["official_model_present"])
        self.assertIn("fee_model_unavailable_all_figures_gross_of_fees", result["warnings"])
        self.assertTrue(result["scenario_pnl"]["target_1"]["gross_of_fees"])
        self.assertFalse(result["scenario_pnl"]["target_1"]["costs_modeled"])

    def test_fee_estimate_without_official_model_is_blocked(self):
        result = oa.analyze(fx.analytics_position(
            fee_model={"per_contract_dollars": 0.65}))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_fee_estimate_without_official_model", result["reason_codes"])

    def test_official_reference_is_recorded_but_costs_are_not_modeled(self):
        result = oa.analyze(fx.analytics_position(
            fee_model=dict(REFERENCE_ONLY_FEE_MODEL)))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        fees = result["fees"]
        self.assertTrue(fees["fee_reference_present"])
        self.assertEqual(fees["fee_reference_source"],
                         REFERENCE_ONLY_FEE_MODEL["official_source"])
        self.assertAlmostEqual(fees["fee_reference_per_contract_dollars"], 0.03)
        # The reference is present; the costs are NOT modeled and the figures stay gross.
        self.assertFalse(fees["costs_modeled"])
        self.assertTrue(fees["all_figures_are_gross_of_fees"])
        self.assertEqual(fees["estimated_net_figures"], oa.NET_FIGURES_UNAVAILABLE)
        self.assertTrue(result["scenario_pnl"]["target_1"]["gross_of_fees"])
        self.assertFalse(result["scenario_pnl"]["target_1"]["costs_modeled"])
        self.assertIn("fee_reference_present_but_costs_not_modeled_figures_stay_gross",
                      result["warnings"])

    def test_ambiguous_per_contract_field_leaves_semantics_incomplete(self):
        result = oa.analyze(fx.analytics_position(
            fee_model=dict(REFERENCE_ONLY_FEE_MODEL)))
        # One per-contract number states neither the opening, nor the closing, nor the
        # round-trip cost, so no net figure can be derived from it.
        self.assertFalse(result["fees"]["fee_semantics_complete"])
        self.assertIn("fee_model_semantics_incomplete_net_figures_unavailable",
                      result["warnings"])

    def test_official_source_without_complete_cost_semantics_yields_no_net_figures(self):
        result = oa.analyze(fx.analytics_position(fee_model={
            "official_source": "broker published fee schedule (verified)",
            "per_contract_dollars": 0.03,
            "opening_per_contract_dollars": 0.03}))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertFalse(result["fees"]["fee_semantics_complete"])
        self.assertEqual(result["fees"]["estimated_net_figures"],
                         oa.NET_FIGURES_UNAVAILABLE)
        self.assertFalse(result["fees"]["costs_modeled"])

    def test_complete_semantics_still_model_no_costs_in_this_version(self):
        result = oa.analyze(fx.analytics_position(fee_model={
            "official_source": "broker published fee schedule (verified)",
            "opening_per_contract_dollars": 0.03,
            "closing_per_contract_dollars": 0.03,
            "round_trip_per_contract_dollars": 0.06}))
        self.assertEqual(result["status"], "OK", result["reason_codes"])
        self.assertTrue(result["fees"]["fee_semantics_complete"])
        # Complete SEMANTICS still do not produce net numbers: no validated net calculation
        # exists in this version, so nothing is subtracted and nothing is labeled net.
        self.assertFalse(result["fees"]["costs_modeled"])
        self.assertTrue(result["fees"]["all_figures_are_gross_of_fees"])
        self.assertEqual(result["fees"]["estimated_net_figures"],
                         oa.NET_FIGURES_UNAVAILABLE)

    def test_every_payoff_number_is_identical_with_and_without_a_fee_reference(self):
        without = oa.analyze(fx.analytics_position())
        with_reference = oa.analyze(fx.analytics_position(
            fee_model=dict(REFERENCE_ONLY_FEE_MODEL)))
        self.assertEqual(without["status"], "OK")
        self.assertEqual(with_reference["status"], "OK")
        for field in PAYOFF_FIELDS:
            self.assertEqual(without[field], with_reference[field], field)

    def test_no_result_ever_claims_figures_are_net_of_fees(self):
        for position in (fx.analytics_position(),
                         fx.analytics_position(fee_model=dict(REFERENCE_ONLY_FEE_MODEL)),
                         fx.analytics_position(contracts=0),
                         fx.analytics_position(strategy="call_debit_spread")):
            result = oa.analyze(position)
            self.assertIs(result["fees"]["all_figures_are_gross_of_fees"], True)
            self.assertIs(result["fees"]["costs_modeled"], False)
            for scenario in (result.get("scenario_pnl") or {}).values():
                self.assertIs(scenario["gross_of_fees"], True)
                self.assertIs(scenario["costs_modeled"], False)

    def test_module_constants_forbid_a_net_claim(self):
        self.assertIs(oa.COSTS_MODELED, False)
        self.assertIs(oa.ALL_FIGURES_ARE_GROSS_OF_FEES, True)

    def test_a_mutated_result_claiming_costs_modeled_is_not_reproducible(self):
        """A caller can mutate a returned dict, but re-running analyze() never reproduces
        the claim: the module has no code path that sets costs_modeled True."""
        result = oa.analyze(fx.analytics_position(
            fee_model=dict(REFERENCE_ONLY_FEE_MODEL)))
        mutated = json.loads(json.dumps(result))
        mutated["fees"]["costs_modeled"] = True
        mutated["fees"]["all_figures_are_gross_of_fees"] = False
        self.assertTrue(mutated["fees"]["costs_modeled"])
        fresh = oa.analyze(fx.analytics_position(
            fee_model=dict(REFERENCE_ONLY_FEE_MODEL)))
        self.assertFalse(fresh["fees"]["costs_modeled"])
        self.assertTrue(fresh["fees"]["all_figures_are_gross_of_fees"])
        source = (ROOT / "scripts" / "lib" / "options_analytics.py").read_text()
        for forbidden in ('"costs_modeled": True', "'costs_modeled': True",
                          '"all_figures_are_gross_of_fees": False',
                          "'all_figures_are_gross_of_fees': False"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_negative_fee_is_blocked(self):
        result = oa.analyze(fx.analytics_position(
            fee_model={"official_source": "x", "per_contract_dollars": -1}))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_fee_model_malformed", result["reason_codes"])

    def test_non_finite_fee_is_blocked(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = oa.analyze(fx.analytics_position(
                fee_model={"official_source": "x", "per_contract_dollars": bad}))
            self.assertEqual(result["status"], "BLOCKED", bad)
            self.assertIn("blocked_fee_model_malformed", result["reason_codes"])

    def test_bool_masquerading_as_a_fee_number_is_blocked(self):
        for bad in (True, False):
            result = oa.analyze(fx.analytics_position(
                fee_model={"official_source": "x", "per_contract_dollars": bad}))
            self.assertEqual(result["status"], "BLOCKED", bad)
            self.assertIn("blocked_fee_model_malformed", result["reason_codes"])

    def test_unknown_fee_field_is_blocked(self):
        result = oa.analyze(fx.analytics_position(fee_model={
            "official_source": "x", "per_contract_dollars": 0.03,
            "net_of_fees_applied": True}))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_fee_model_unknown_field", result["reason_codes"])

    def test_malformed_fee_model_shapes_are_blocked(self):
        for bad in ("official schedule", ["official schedule"], 0.65, True):
            result = oa.analyze(fx.analytics_position(fee_model=bad))
            self.assertEqual(result["status"], "BLOCKED", bad)
            self.assertIn("blocked_fee_estimate_without_official_model",
                          result["reason_codes"])

    def test_non_string_official_source_is_blocked(self):
        result = oa.analyze(fx.analytics_position(
            fee_model={"official_source": 1, "per_contract_dollars": 0.03}))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_fee_estimate_without_official_model", result["reason_codes"])


class TestReasonCodeCoverage(unittest.TestCase):
    """Reason codes the module can emit but nothing exercised end to end."""

    def _blocked(self, **over) -> dict:
        result = oa.analyze(fx.analytics_position(**over))
        self.assertEqual(result["status"], "BLOCKED", result)
        return result

    def _assert_non_actionable(self, result: dict) -> None:
        self.assertEqual(result["premium_at_risk_dollars"], 0.0)
        self.assertEqual(result["max_loss_dollars"], 0.0)
        self.assertEqual(result["legs"], [])
        self.assertFalse(result["options_execution_enabled"])
        self.assertFalse(result["broker_write_authority"])
        self.assertFalse(result["actionable"])
        self.assertTrue(result["shadow"])

    def test_non_positive_strike_emits_blocked_non_positive_price_input(self):
        result = self._blocked(strike=-1.0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_non_positive_price_input", result["reason_codes"])
        self._assert_non_actionable(result)

    def test_zero_spot_emits_blocked_non_positive_price_input(self):
        result = self._blocked(underlying_spot=0.0)
        self.assertIn("blocked_non_positive_price_input", result["reason_codes"])
        self._assert_non_actionable(result)

    def test_invalid_expiration_emits_blocked_invalid_date_input(self):
        result = self._blocked(expiration="2026-13-45")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_invalid_date_input", result["reason_codes"])
        self._assert_non_actionable(result)

    def test_invalid_market_date_emits_blocked_invalid_date_input(self):
        result = self._blocked(market_date="03-08-2026")
        self.assertIn("blocked_invalid_date_input", result["reason_codes"])
        self._assert_non_actionable(result)


class TestHoldingPeriodBoundary(unittest.TestCase):
    """Defense in depth: the analytics boundary enforces the same 1..10 trading-day window
    as the PROTECTED config/options_risk_limits.yaml. It never widens it."""

    def test_module_constants_mirror_the_protected_limits_file(self):
        text = (ROOT / "config" / "options_risk_limits.yaml").read_text()
        found = {}
        for line in text.splitlines():
            for key in ("minimum_intended_holding_days", "maximum_intended_holding_days"):
                if line.strip().startswith(key + ":"):
                    found[key] = int(line.split(":", 1)[1].split("#")[0].strip())
        self.assertEqual(found.get("minimum_intended_holding_days"),
                         oa.MIN_INTENDED_HOLDING_DAYS)
        self.assertEqual(found.get("maximum_intended_holding_days"),
                         oa.MAX_INTENDED_HOLDING_DAYS)

    def test_zero_holding_days_is_blocked(self):
        result = oa.analyze(fx.analytics_position(intended_holding_days=0))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_intended_holding_days_outside_policy", result["reason_codes"])
        self.assertEqual(result["max_loss_dollars"], 0.0)

    def test_above_policy_maximum_is_blocked(self):
        result = oa.analyze(fx.analytics_position(intended_holding_days=11, dte=60))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_intended_holding_days_outside_policy", result["reason_codes"])

    def test_bool_holding_days_is_blocked(self):
        for bad in (True, False):
            result = oa.analyze(fx.analytics_position(intended_holding_days=bad))
            self.assertEqual(result["status"], "BLOCKED", bad)
            self.assertIn("blocked_missing_or_invalid:intended_holding_days",
                          result["reason_codes"])

    def test_non_integer_holding_days_is_blocked(self):
        for bad in (5.0, "5", None, [5]):
            result = oa.analyze(fx.analytics_position(intended_holding_days=bad))
            self.assertEqual(result["status"], "BLOCKED", bad)
            self.assertIn("blocked_missing_or_invalid:intended_holding_days",
                          result["reason_codes"])

    def test_negative_holding_days_is_blocked(self):
        result = oa.analyze(fx.analytics_position(intended_holding_days=-1))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocked_missing_or_invalid:intended_holding_days",
                      result["reason_codes"])

    def test_boundary_values_inside_the_window_are_accepted(self):
        for days in (oa.MIN_INTENDED_HOLDING_DAYS, oa.MAX_INTENDED_HOLDING_DAYS):
            result = oa.analyze(fx.analytics_position(intended_holding_days=days, dte=39))
            self.assertEqual(result["status"], "OK", (days, result["reason_codes"]))


class TestThetaDisclosure(unittest.TestCase):
    """The modeled decay is a transparent approximation, and says so in machine-readable
    and human-readable form. It is not a repricing model and not a forecast."""

    def test_decay_is_labeled_a_constant_theta_linear_approximation(self):
        decay = oa.analyze(fx.analytics_position())["time_decay"]
        self.assertEqual(decay["method"], "CONSTANT_THETA_LINEAR_APPROXIMATION")
        self.assertEqual(decay["method"], oa.THETA_METHOD)
        self.assertIs(decay["assumes_theta_constant_over_holding_period"], True)
        self.assertIs(decay["assumes_static_underlying_and_iv"], True)
        self.assertIs(decay["is_a_repricing_model"], False)
        self.assertIs(decay["is_a_forecast"], False)
        self.assertIs(decay["can_understate_decay_near_expiration"], True)
        for phrase in ("Theta is NOT", "constant", "UNDERSTATE", "not a repricing model",
                       "not a forecast"):
            self.assertIn(phrase, decay["note"], phrase)

    def test_the_arithmetic_is_unchanged_and_still_linear(self):
        decay = oa.analyze(fx.analytics_position())["time_decay"]
        self.assertAlmostEqual(decay["dollars_per_day"], -3.0)
        self.assertAlmostEqual(decay["modeled_decay_over_holding_dollars"], -15.0)
        self.assertAlmostEqual(decay["modeled_decay_over_holding_dollars"],
                               decay["dollars_per_day"] * 5)

    def test_result_is_still_not_a_repricing_model(self):
        result = oa.analyze(fx.analytics_position())
        self.assertIs(result["not_a_repricing_model"], True)
        self.assertEqual(result["basis"], "expiration_intrinsic_only")


class TestNoExecutionAuthority(unittest.TestCase):
    def test_module_imports_no_network_broker_or_process_machinery(self):
        source = (ROOT / "scripts" / "lib" / "options_analytics.py").read_text()
        for forbidden in ("import requests", "import urllib", "import socket",
                          "import http", "subprocess", "os.system", "robin_stocks",
                          "place_option_order", "submit_order"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_result_never_carries_execution_authority(self):
        for position in (fx.analytics_position(),
                         fx.analytics_position(contracts=0),
                         fx.analytics_position(strategy="call_debit_spread")):
            result = oa.analyze(position)
            self.assertFalse(result["options_execution_enabled"])
            self.assertFalse(result["broker_write_authority"])
            self.assertFalse(result["actionable"])
            self.assertTrue(result["shadow"])

    def test_no_forbidden_status_tokens(self):
        for position in (fx.analytics_position(), fx.analytics_position(contracts=0)):
            status = oa.analyze(position)["status"]
            for token in ("BUY", "OFFICIAL", "ORDER", "SUBMIT", "FILL", "ACTIONABLE"):
                self.assertNotIn(token, status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
