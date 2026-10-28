#!/usr/bin/env python3
"""Cover for F4 — a persistent total outage must escalate instead of decaying into silence.

The 2026-07-29..07-31 outage produced the SAME three condition codes every morning, so the
alert fingerprint never moved, `transition` stayed `unchanged`, and `notification_required`
stayed false for three consecutive lost sessions. These tests pin the fix: duration is derived
from durable per-session health receipts and the exchange calendar, escalation happens on the
second consecutive missed EXPECTED session, and the anti-spam contract is untouched.

Read-only toward every trading surface: no broker, no model, no network, no order.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import autonomy_service_alert as alert  # noqa: E402
    import autonomy_service_health  # noqa: E402
    import slo_persistence  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc

WORKFLOW = "morning"


def _health_document(target: str, statuses: dict[str, str], *,
                     generated: str | None = None) -> dict:
    return {
        "target_session_date": target,
        "generated_utc": generated or f"{target}T21:16:00+00:00",
        "service_slo_health": {name: {"status": status}
                               for name, status in statuses.items()},
    }


class HistoryWalkTests(unittest.TestCase):
    """Streak derivation from durable receipts + the real exchange calendar."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data" / "runtime" / "health").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, target: str, statuses: dict[str, str], **kwargs) -> Path:
        path = (self.root / "data" / "runtime" / "health"
                / f"{target}_autonomy_service_health.json")
        path.write_text(json.dumps(_health_document(target, statuses, **kwargs)),
                        encoding="utf-8")
        return path

    def _streak(self, target: str, workflow: str = WORKFLOW,
                current: str = "missed") -> dict:
        return slo_persistence.consecutive_missed(
            workflow=workflow, target=target, current_status=current, root=self.root)

    # --- the escalation ladder ---------------------------------------------------------
    def test_first_missed_session_is_streak_one(self) -> None:
        self._write("2026-07-30", {WORKFLOW: "met"})
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 1)
        self.assertFalse(slo_persistence.is_persistent(row["streak"]))
        self.assertTrue(row["history_complete"])

    def test_second_consecutive_missed_session_is_persistent(self) -> None:
        self._write("2026-07-29", {WORKFLOW: "met"})
        self._write("2026-07-30", {WORKFLOW: "missed"})
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 2)
        self.assertTrue(slo_persistence.is_persistent(row["streak"]))
        self.assertEqual(row["sessions"], ["2026-07-31", "2026-07-30"])

    def test_a_met_session_ends_the_streak(self) -> None:
        self._write("2026-07-28", {WORKFLOW: "missed"})
        self._write("2026-07-29", {WORKFLOW: "met"})
        self._write("2026-07-30", {WORKFLOW: "missed"})
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 2, "an old outage must not contaminate a new one")

    def test_recovery_resets_the_streak_to_zero(self) -> None:
        self._write("2026-07-29", {WORKFLOW: "missed"})
        self._write("2026-07-30", {WORKFLOW: "missed"})
        row = self._streak("2026-07-31", current="met")
        self.assertEqual(row["streak"], 0)
        self.assertEqual(row["sessions"], [])

    # --- calendar semantics ------------------------------------------------------------
    def test_a_weekend_does_not_advance_or_break_a_streak(self) -> None:
        # Monday 2026-08-03; the previous session is Friday 2026-07-31, not Sunday.
        self._write("2026-07-31", {WORKFLOW: "missed"})
        row = self._streak("2026-08-03")
        self.assertEqual(row["streak"], 2)
        self.assertEqual(row["sessions"], ["2026-08-03", "2026-07-31"])

    def test_an_exchange_holiday_does_not_advance_or_break_a_streak(self) -> None:
        # Monday 2026-07-06; 2026-07-03 is an exchange holiday, so the prior session is 07-02.
        self._write("2026-07-02", {WORKFLOW: "missed"})
        row = self._streak("2026-07-06")
        self.assertEqual(row["streak"], 2)
        self.assertEqual(row["sessions"], ["2026-07-06", "2026-07-02"])
        self.assertNotIn("2026-07-03", row["sessions"])

    def test_calendar_days_are_never_used_as_sessions(self) -> None:
        sessions = slo_persistence.prior_trading_sessions("2026-08-03", 3)
        self.assertNotIn("2026-08-01", sessions)   # Saturday
        self.assertNotIn("2026-08-02", sessions)   # Sunday
        self.assertEqual(sessions[0], "2026-07-31")

    # --- neutral sessions --------------------------------------------------------------
    def test_a_not_applicable_session_is_neutral(self) -> None:
        """The repository's own 'not required this session' verdict, e.g. a disabled lane."""
        self._write("2026-07-29", {WORKFLOW: "missed"})
        self._write("2026-07-30", {WORKFLOW: "not_applicable"})
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 2, "neutral sessions pass through, never count")
        self.assertEqual(row["sessions"], ["2026-07-31", "2026-07-29"])

    def test_a_pending_session_is_neutral(self) -> None:
        self._write("2026-07-29", {WORKFLOW: "met"})
        self._write("2026-07-30", {WORKFLOW: "pending"})
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 1)

    def test_an_absent_workflow_row_is_neutral(self) -> None:
        self._write("2026-07-30", {"intake": "met"})     # no `morning` row at all
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 1)
        self.assertFalse(row["history_complete"])

    # --- fail-closed history -----------------------------------------------------------
    def test_a_missing_receipt_stops_the_walk_and_says_so(self) -> None:
        row = self._streak("2026-07-31")
        self.assertEqual(row["streak"], 1)
        self.assertFalse(row["history_complete"])
        self.assertTrue(any(item.startswith("history_gap:") for item in row["diagnostics"]),
                        row["diagnostics"])

    def test_a_malformed_receipt_fails_closed(self) -> None:
        path = (self.root / "data" / "runtime" / "health"
                / "2026-07-30_autonomy_service_health.json")
        path.write_text("{not json", encoding="utf-8")
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"])
        self.assertIn("history_unreadable:2026-07-30", row["diagnostics"])

    def test_a_receipt_filed_under_the_wrong_session_fails_closed(self) -> None:
        """A duplicated receipt copied onto another date cannot be attributed to either."""
        path = (self.root / "data" / "runtime" / "health"
                / "2026-07-30_autonomy_service_health.json")
        path.write_text(json.dumps(_health_document("2026-07-29", {WORKFLOW: "missed"})),
                        encoding="utf-8")
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"])
        self.assertIn("history_target_mismatch:2026-07-30", row["diagnostics"])

    def test_an_out_of_order_timestamp_fails_closed(self) -> None:
        self._write("2026-07-31", {WORKFLOW: "missed"},
                    generated="2026-07-31T12:00:00+00:00")
        self._write("2026-07-30", {WORKFLOW: "missed"},
                    generated="2026-08-05T12:00:00+00:00")   # later than the target's
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"])
        self.assertIn("history_out_of_order:2026-07-30", row["diagnostics"])

    def test_an_invalid_prior_session_fails_closed(self) -> None:
        self._write("2026-07-30", {WORKFLOW: "invalid"})
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"])
        self.assertIn("history_session_invalid:2026-07-30", row["diagnostics"])

    def test_an_unknown_status_token_fails_closed(self) -> None:
        self._write("2026-07-30", {WORKFLOW: "wobbly"})
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"])
        self.assertIn("history_status_unknown:2026-07-30", row["diagnostics"])

    def test_absence_is_not_a_conflict_but_corruption_is(self) -> None:
        row = self._streak("2026-07-31")
        self.assertFalse(row["history_complete"], "no older receipt exists")
        self.assertFalse(row["history_conflict"], "absence is not corruption")

        path = (self.root / "data" / "runtime" / "health"
                / "2026-07-30_autonomy_service_health.json")
        path.write_text("{ not json", encoding="utf-8")
        row = self._streak("2026-07-31")
        self.assertTrue(row["history_conflict"])

    def test_workflows_keep_independent_streaks(self) -> None:
        self._write("2026-07-29", {"morning": "missed", "intake": "met"})
        self._write("2026-07-30", {"morning": "missed", "intake": "met"})
        morning = self._streak("2026-07-31", workflow="morning")
        intake = self._streak("2026-07-31", workflow="intake")
        self.assertEqual(morning["streak"], 3)
        self.assertEqual(intake["streak"], 1)


class AlertEscalationTests(unittest.TestCase):
    """The alert layer's fingerprint must move when the SEMANTIC state changes — only then."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data" / "runtime" / "health").mkdir(parents=True)
        self.policy, errors = alert.load_policy()
        self.assertEqual(errors, [], "the shipped alert policy must stay valid")
        # `_condition_codes` re-validates the composite receipt; these tests supply a minimal
        # one on purpose, so only the persistence branch is under test.
        patcher = mock.patch.object(autonomy_service_health, "validate", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_history(self, target: str, statuses: dict[str, str]) -> None:
        path = (self.root / "data" / "runtime" / "health"
                / f"{target}_autonomy_service_health.json")
        path.write_text(json.dumps(_health_document(target, statuses)), encoding="utf-8")

    def _health(self, target: str, statuses: dict[str, str]) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        return {
            "target_session_date": target,
            "generated_utc": now.isoformat(),
            "overall_status": "at_issue",
            "service_slo_policy": {"status": "valid"},
            "scheduler_config_health": {"status": "healthy"},
            "service_slo_health": {name: {"status": status}
                                   for name, status in statuses.items()},
            "research_artifact_health": {"reason_codes": []},
            "artifact_sha256": "0" * 64,
        }

    def _evaluate(self, target: str, statuses: dict[str, str],
                  previous: dict | None = None) -> tuple[dict, dict]:
        now = dt.datetime.now(dt.timezone.utc)
        return alert.evaluate(
            health=self._health(target, statuses),
            previous=previous or alert._initial_state(now),
            policy=self.policy, target=target, now=now, root=self.root)

    def test_first_miss_stays_a_warning_with_the_original_code(self) -> None:
        self._write_history("2026-07-30", {"morning": "met"})
        receipt, state = self._evaluate("2026-07-31", {"morning": "missed"})
        self.assertIn("slo_missed:morning", receipt["active_codes"])
        self.assertNotIn("slo_missed_persistent:morning", receipt["active_codes"])
        self.assertEqual(receipt["severity"], "warning")
        self.assertTrue(receipt["notification_required"])
        self.assertEqual(alert.validate_receipt(receipt), [])
        self.assertEqual(alert.validate_state(state), [])

    def test_second_consecutive_miss_becomes_critical_and_changes_fingerprint(self) -> None:
        self._write_history("2026-07-29", {"morning": "met"})
        self._write_history("2026-07-30", {"morning": "missed"})
        first, first_state = self._evaluate("2026-07-30", {"morning": "missed"})
        second, second_state = self._evaluate(
            "2026-07-31", {"morning": "missed"}, previous=first_state)

        self.assertIn("slo_missed_persistent:morning", second["active_codes"])
        self.assertEqual(second["severity"], "critical")
        self.assertNotEqual(first["active_fingerprint"], second["active_fingerprint"])
        self.assertEqual(second["transition"], "changed_alert")
        self.assertTrue(second["notification_required"])
        self.assertEqual(second["notification_kind"], "alert")
        self.assertEqual(alert.validate_receipt(second), [])
        self.assertEqual(alert.validate_state(second_state), [])
        self.assertTrue(second["slo_persistence"]["morning"]["persistent"])
        self.assertEqual(
            second["slo_persistence"]["morning"]["consecutive_missed_sessions"], 2)

    def test_third_unchanged_miss_is_deduplicated_but_stays_critical(self) -> None:
        self._write_history("2026-07-29", {"morning": "missed"})
        self._write_history("2026-07-30", {"morning": "missed"})
        second, second_state = self._evaluate("2026-07-30", {"morning": "missed"})
        third, third_state = self._evaluate(
            "2026-07-30", {"morning": "missed"}, previous=second_state)
        self.assertEqual(third["transition"], "unchanged")
        self.assertFalse(third["notification_required"],
                         "anti-spam must survive the escalation")
        self.assertEqual(third["severity"], "critical",
                         "the critical state must remain plainly visible")
        self.assertEqual(third["active_fingerprint"], second["active_fingerprint"])
        self.assertEqual(alert.validate_receipt(third), [])

    def test_recovery_produces_the_recovery_transition(self) -> None:
        self._write_history("2026-07-29", {"morning": "missed"})
        self._write_history("2026-07-30", {"morning": "missed"})
        outage, outage_state = self._evaluate("2026-07-30", {"morning": "missed"})
        self.assertEqual(outage["severity"], "critical")
        recovered, recovered_state = self._evaluate(
            "2026-07-31", {"morning": "met"}, previous=outage_state)
        self.assertEqual(recovered["transition"], "resolved")
        self.assertEqual(recovered["active_codes"], [])
        self.assertIsNone(recovered["active_fingerprint"])
        self.assertEqual(recovered["notification_kind"], "recovery")
        self.assertEqual(alert.validate_receipt(recovered), [])
        self.assertEqual(alert.validate_state(recovered_state), [])

    def test_a_later_outage_starts_again_at_one(self) -> None:
        self._write_history("2026-07-28", {"morning": "missed"})
        self._write_history("2026-07-29", {"morning": "missed"})
        self._write_history("2026-07-30", {"morning": "met"})
        receipt, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        self.assertIn("slo_missed:morning", receipt["active_codes"])
        self.assertEqual(receipt["severity"], "warning")
        self.assertEqual(
            receipt["slo_persistence"]["morning"]["consecutive_missed_sessions"], 1)

    def test_independent_workflows_escalate_independently(self) -> None:
        self._write_history("2026-07-29", {"morning": "met", "intake": "met"})
        self._write_history("2026-07-30", {"morning": "missed", "intake": "met"})
        receipt, _ = self._evaluate(
            "2026-07-31", {"morning": "missed", "intake": "missed"})
        self.assertIn("slo_missed_persistent:morning", receipt["active_codes"])
        self.assertIn("slo_missed:intake", receipt["active_codes"])
        self.assertEqual(receipt["severity"], "critical")

    def test_absent_history_is_recorded_but_raises_no_code(self) -> None:
        """A fresh root has no older receipts. That is normal, not an incident."""
        receipt, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        row = receipt["slo_persistence"]["morning"]
        self.assertFalse(row["history_complete"])
        self.assertFalse(row["history_conflict"])
        self.assertTrue(any(item.startswith("history_gap:") for item in row["diagnostics"]),
                        row["diagnostics"])
        self.assertNotIn("slo_history_conflict:morning", receipt["active_codes"])
        self.assertEqual(receipt["active_codes"], ["slo_missed:morning"])
        self.assertEqual(alert.validate_receipt(receipt), [])

    def test_contradictory_history_raises_a_conflict_code(self) -> None:
        """Evidence that exists and contradicts itself must be as loud as the outage."""
        path = (self.root / "data" / "runtime" / "health"
                / "2026-07-30_autonomy_service_health.json")
        path.write_text("{ not json", encoding="utf-8")
        receipt, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        self.assertIn("slo_history_conflict:morning", receipt["active_codes"])
        self.assertTrue(receipt["slo_persistence"]["morning"]["history_conflict"])
        self.assertEqual(alert.validate_receipt(receipt), [])

    def test_fingerprint_is_stable_for_an_identical_semantic_state(self) -> None:
        self._write_history("2026-07-30", {"morning": "met"})
        first, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        second, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        self.assertEqual(first["active_fingerprint"], second["active_fingerprint"],
                         "the fingerprint must not absorb a volatile timestamp")

    def test_escalation_never_relaxes_the_anti_spam_policy(self) -> None:
        document = json.loads(alert.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIs(document["repeat_unchanged_alerts"], False)
        self.assertEqual(alert.validate_policy(document), [])

    def test_a_broken_history_read_never_invents_an_escalation(self) -> None:
        with mock.patch.object(slo_persistence, "consecutive_missed",
                               side_effect=RuntimeError("boom")):
            receipt, _ = self._evaluate("2026-07-31", {"morning": "missed"})
        self.assertIn("slo_missed:morning", receipt["active_codes"])
        self.assertNotIn("slo_missed_persistent:morning", receipt["active_codes"])
        self.assertIn("slo_history_conflict:morning", receipt["active_codes"])


if __name__ == "__main__":
    unittest.main()
