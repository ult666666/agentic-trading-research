#!/usr/bin/env python3
"""Tests for scripts/candidate_artifact_health_rollup.py — adoption rollup over health artifacts.

Locks: label counting + adoption-rate definition (empty excluded from the denominator),
consecutive-non-healthy walk (empty skipped, healthy breaks), the fail-closed recommendation
ladder (invalid / repeated missing / repeated fallback / no data => investigate; single
fallback/missing or non-healthy latest => monitor; healthy requires latest-healthy + clean
window), malformed artifacts counted invalid (never silently ignored), window filters, and the
no-performance/no-execution framing on every path.

All fixtures in temp dirs. Run: python3 scripts/test_candidate_artifact_health_rollup.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import candidate_artifact_health_rollup as roll  # noqa: E402

PY = sys.executable or "python3"


def write_artifact(hdir: Path, target: str, label: str, **over) -> None:
    doc = {"target_session_date": target, "health_label": label,
           "source": {"healthy": "structured_draft", "fallback": "report_extractor_fallback",
                      "empty": "none"}.get(label, "unavailable"),
           "action_hint": f"hint for {label}",
           "diagnostic_only": True, "execution_enabled": False}
    doc.update(over)
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / f"{target}_candidate_artifact_health.json").write_text(json.dumps(doc))


DATES = [f"2026-07-{d:02d}" for d in range(10, 20)]


class TestRecommendationLadder(unittest.TestCase):
    def test_all_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            for t in DATES[:5]:
                write_artifact(hdir, t, "healthy")
            out = roll.rollup(health_dir=hdir)
            self.assertTrue(out["ok"])
            self.assertEqual(out["recommendation_label"], "healthy")
            self.assertEqual(out["adoption_rate"], 1.0)
            self.assertEqual(out["sessions_checked"], 5)
            self.assertEqual(out["structured_draft_count"], 5)
            self.assertEqual(out["consecutive_non_healthy_count"], 0)
            self.assertEqual(out["latest_health_label"], "healthy")
            self.assertEqual(out["window_start"], DATES[0])
            self.assertEqual(out["window_end"], DATES[4])

    def test_latest_fallback_is_monitor(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            for t in DATES[:4]:
                write_artifact(hdir, t, "healthy")
            write_artifact(hdir, DATES[4], "fallback")
            out = roll.rollup(health_dir=hdir)
            self.assertTrue(out["ok"], "fallback alone keeps ok=true")
            self.assertEqual(out["recommendation_label"], "monitor")
            self.assertEqual(out["latest_health_label"], "fallback")
            self.assertEqual(out["consecutive_non_healthy_count"], 1)
            self.assertEqual(out["adoption_rate"], 0.8)

    def test_repeated_fallback_is_investigate(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            for t in DATES[1:4]:
                write_artifact(hdir, t, "fallback")
            out = roll.rollup(health_dir=hdir)
            self.assertTrue(out["ok"])
            self.assertEqual(out["recommendation_label"], "investigate")
            self.assertEqual(out["fallback_count"], 3)
            self.assertEqual(out["consecutive_non_healthy_count"], 3)

    def test_invalid_present_is_investigate_and_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            write_artifact(hdir, DATES[1], "invalid")
            write_artifact(hdir, DATES[2], "healthy")
            out = roll.rollup(health_dir=hdir)
            self.assertFalse(out["ok"])
            self.assertEqual(out["recommendation_label"], "investigate")
            self.assertEqual(out["invalid_count"], 1)

    def test_single_missing_monitor_repeated_missing_investigate(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "missing")
            write_artifact(hdir, DATES[1], "healthy")
            out = roll.rollup(health_dir=hdir)
            self.assertFalse(out["ok"], "missing is a pipeline failure => ok=false")
            self.assertEqual(out["recommendation_label"], "monitor")
            write_artifact(hdir, DATES[2], "missing")
            out2 = roll.rollup(health_dir=hdir)
            self.assertEqual(out2["recommendation_label"], "investigate")
            self.assertEqual(out2["missing_count"], 2)

    def test_empty_no_trade_not_a_failure_and_skipped_in_streak(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            write_artifact(hdir, DATES[1], "empty")     # blackout no-trade day
            write_artifact(hdir, DATES[2], "healthy")
            write_artifact(hdir, DATES[3], "empty")     # latest is empty
            out = roll.rollup(health_dir=hdir)
            self.assertTrue(out["ok"])
            self.assertEqual(out["empty_count"], 2)
            self.assertEqual(out["adoption_rate"], 1.0, "empty excluded from denominator")
            self.assertEqual(out["consecutive_non_healthy_count"], 0, "empty skipped in streak")
            # latest is empty, not healthy => spec forbids the healthy label.
            self.assertEqual(out["recommendation_label"], "monitor")

    def test_empty_window_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            out = roll.rollup(health_dir=Path(td))
            self.assertFalse(out["ok"])
            self.assertEqual(out["recommendation_label"], "investigate")
            self.assertEqual(out["sessions_checked"], 0)
            self.assertIsNone(out["adoption_rate"])
            self.assertIsNone(out["latest_target"])


class TestFailClosedParsing(unittest.TestCase):
    def test_malformed_artifact_counted_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            (hdir / f"{DATES[1]}_candidate_artifact_health.json").write_text("{corrupt")
            out = roll.rollup(health_dir=hdir)
            self.assertFalse(out["ok"])
            self.assertEqual(out["invalid_count"], 1)
            self.assertEqual(out["recommendation_label"], "investigate")
            self.assertTrue(any("unreadable" in h for h in out["action_hints"]))

    def test_target_mismatch_and_unknown_label_counted_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy",
                           target_session_date="1999-01-01")   # filename/embedded mismatch
            write_artifact(hdir, DATES[1], "excellent")        # unknown label
            out = roll.rollup(health_dir=hdir)
            self.assertEqual(out["invalid_count"], 2)
            self.assertFalse(out["ok"])

    def test_non_matching_filenames_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            (hdir / "2026-07-10_run_status.json").write_text("{}")
            (hdir / "notes.txt").write_text("x")
            out = roll.rollup(health_dir=hdir)
            self.assertEqual(out["sessions_checked"], 1)


class TestWindowFilters(unittest.TestCase):
    def _seed(self, hdir: Path):
        labels = ["healthy", "fallback", "healthy", "empty", "healthy"]
        for t, lab in zip(DATES[:5], labels):
            write_artifact(hdir, t, lab)

    def test_from_to_and_limit(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            self._seed(hdir)
            out = roll.rollup(health_dir=hdir, date_from=DATES[1], date_to=DATES[3])
            self.assertEqual(out["sessions_checked"], 3)
            self.assertEqual(out["window_start"], DATES[1])
            self.assertEqual(out["window_end"], DATES[3])
            out2 = roll.rollup(health_dir=hdir, limit=2)
            self.assertEqual(out2["sessions_checked"], 2)
            self.assertEqual(out2["window_start"], DATES[3])
            self.assertEqual(out2["latest_target"], DATES[4])

    def test_cli_bad_dates_exit_2(self):
        proc = subprocess.run(
            [PY, str(ROOT / "scripts" / "candidate_artifact_health_rollup.py"),
             "--from", "07/10/2026"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        proc2 = subprocess.run(
            [PY, str(ROOT / "scripts" / "candidate_artifact_health_rollup.py"),
             "--from", "2026-07-12", "--to", "2026-07-10"], capture_output=True, text=True)
        self.assertEqual(proc2.returncode, 2)


class TestFraming(unittest.TestCase):
    def test_no_performance_or_execution_claims_on_any_path(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            for stage in ("empty", "seeded"):
                if stage == "seeded":
                    write_artifact(hdir, DATES[0], "healthy")
                    write_artifact(hdir, DATES[1], "invalid")
                out = roll.rollup(health_dir=hdir)
                self.assertTrue(out["diagnostic_only"])
                self.assertFalse(out["execution_enabled"])
                self.assertFalse(out["activation_authority"])
                text = json.dumps(out).lower()
                for forbidden in ("profit", "expectancy", "win rate", "executed trade",
                                  "place the order", "buy now"):
                    self.assertNotIn(forbidden, text)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            write_artifact(hdir, DATES[0], "healthy")
            ok = subprocess.run(
                [PY, str(ROOT / "scripts" / "candidate_artifact_health_rollup.py"),
                 "--health-dir", str(hdir)], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            write_artifact(hdir, DATES[1], "missing")
            bad = subprocess.run(
                [PY, str(ROOT / "scripts" / "candidate_artifact_health_rollup.py"),
                 "--health-dir", str(hdir)], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(json.loads(bad.stdout)["recommendation_label"], "monitor")


if __name__ == "__main__":
    unittest.main(verbosity=1)
