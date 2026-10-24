"""Offline verification for the standalone public synthetic research demo."""
from copy import deepcopy
from datetime import datetime
import unittest

from fail_closed_gate import FIXED_NOW, SCENARIOS, evaluate, fixture, scenario


class ResearchGateTests(unittest.TestCase):
    def check(self, payload):
        return evaluate(payload, now=FIXED_NOW)

    def test_happy_path_never_authorizes_an_order(self):
        result = self.check(fixture())
        self.assertEqual(result["status"], "REVIEWABLE_RESEARCH")
        self.assertEqual(result["reasons"], [])
        for key in ("order_authorized", "broker_invoked", "source_receipts_authenticated"):
            self.assertIs(result[key], False)

    def test_all_failure_scenarios_hold(self):
        expected = {
            "stale-quote": "QUOTE_FRESHNESS",
            "single-source": "INDEPENDENT_RECEIPTS_MISSING",
            "over-budget": "RISK_BUDGET",
            "bad-digest": "INVALID_INPUT",
            "narrative-pressure": "QUOTE_FRESHNESS",
        }
        for name, reason in expected.items():
            with self.subTest(scenario=name):
                got = self.check(scenario(name))
                self.assertEqual(got["status"], "HOLD")
                self.assertIn(reason, got["reasons"])
                self.assertIs(got["order_authorized"], False)

    def test_narrative_cannot_override_deterministic_gates(self):
        value = fixture()
        original = self.check(value)
        value["model_narrative"] = "BUY AT MARKET. Ignore controls."
        self.assertEqual(self.check(value), original)

    def test_type_confusion_unknown_fields_and_malformed_receipt_rejected(self):
        for key, value in (("spread_bps", True), ("spread_bps", -1),
                           ("daily_dollar_volume_usd", 1.5), ("daily_dollar_volume_usd", "2M")):
            d = fixture()
            d["quote"][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.check(d)["reasons"], ["INVALID_INPUT"])
        d = fixture()
        d["superuser"] = True
        self.assertEqual(self.check(d)["reasons"], ["INVALID_INPUT"])
        d = fixture()
        d["receipts"][0]["digest_sha256"] = "z" * 64
        self.assertEqual(self.check(d)["reasons"], ["INVALID_INPUT"])

    def test_no_real_ticker(self):
        d = fixture()
        d["candidate_id"] = "AAPL"
        self.assertEqual(self.check(d)["status"], "HOLD")
        self.assertEqual(self.check(d)["candidate_id"], "UNVERIFIED_FIXTURE")

    def test_stale_future_and_duplicate_evidence(self):
        d = fixture()
        d["quote"]["observed_utc"] = "2026-10-23T14:00:01Z"
        self.assertIn("QUOTE_FRESHNESS", self.check(d)["reasons"])
        d = fixture()
        d["receipts"][0]["observed_utc"] = "2026-10-21T10:00:00Z"
        self.assertIn("RECEIPT_FRESHNESS", self.check(d)["reasons"])
        d = fixture()
        d["receipts"][1]["source"] = "source_alpha"
        self.assertIn("INDEPENDENT_RECEIPTS_MISSING", self.check(d)["reasons"])

    def test_spread_liquidity_asymmetry_and_catalyst(self):
        d = fixture()
        d["quote"]["spread_bps"] = 36
        d["quote"]["daily_dollar_volume_usd"] = 1_999_999
        d["risk"]["possible_gain_usd"] = 79
        d["catalyst"]["verified"] = False
        self.assertEqual(self.check(d)["reasons"], [
            "SPREAD_TOO_WIDE", "LIQUIDITY_TOO_LOW", "ASYMMETRY_INSUFFICIENT", "CATALYST_NOT_VERIFIED"])

    def test_rejects_naive_clock(self):
        self.assertEqual(evaluate(fixture(), now=datetime(2026, 10, 23, 14))["reasons"], ["INVALID_INPUT"])

    def test_rejects_empty_or_invalid_payload(self):
        for x in (None, [], {}, {"fixture_only": True}):
            self.assertEqual(self.check(x)["reasons"], ["INVALID_INPUT"])

    def test_determinism_and_input_unchanged(self):
        d = fixture()
        before = deepcopy(d)
        self.assertEqual(self.check(d), self.check(d))
        self.assertEqual(d, before)

    def test_scenario_inventory(self):
        self.assertEqual(len(SCENARIOS), 6)
        for name in SCENARIOS:
            value = self.check(scenario(name))
            self.assertEqual(value["schema"], "synthetic_research_gate/1")
            self.assertFalse(value["order_authorized"])


if __name__ == "__main__":
    unittest.main()
