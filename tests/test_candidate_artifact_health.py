#!/usr/bin/env python3
"""Tests for scripts/candidate_artifact_health.py — structured-candidate adoption observability.

Locks the fail-closed health ladder: missing/invalid => ok=false; fallback/empty => ok=true with
the honest label; structured_draft (even with an EMPTY list on No-Trade) => healthy. Read-only:
the checker never writes, never claims performance, never carries execution language.

All fixtures in temp roots. Run: python3 scripts/test_candidate_artifact_health.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import candidate_artifact_health as cah                    # noqa: E402
    import freeze_candidates as fc                             # noqa: E402
    import legacy_morning_receipt                              # noqa: E402
    import test_autonomy_service_health as health_fixtures     # noqa: E402
    from test_legacy_morning_receipt import stage_authoritative_inputs  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc

PY = sys.executable or "python3"
T = "2026-07-08"


class Repo:
    """Temp root with a real production-writer receipt and candidate helpers."""

    def __init__(self, td: str, rec: dict | None = None):
        self.root = Path(td)
        self.rec = rec or health_fixtures.v2_preliminary_receipt(self.root, T)
        d = fc.research_dir(T, self.root)
        d.mkdir(parents=True, exist_ok=True)
        (d / "freeze_receipt.json").write_text(json.dumps(self.rec))

    def write_draft(self, candidates: list) -> None:
        (fc.research_dir(T, self.root) / fc.DRAFT_NAME).write_text(
            json.dumps({"target_session_date": T, "candidates": candidates}))

    def build(self) -> dict:
        return fc.build_for_receipt(self.rec, root=self.root)

    def check(self, **kw) -> dict:
        kw.setdefault("ledger_path", Path("/nonexistent-disabled"))
        return cah.check(T, root=self.root, **kw)


class TestHealthLadder(unittest.TestCase):
    def test_legacy_official_receipt_is_historical_and_never_active_health(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            moment = dt.datetime(2026, 7, 8, 13, 0, tzinfo=dt.timezone.utc)
            stage_authoritative_inputs(root, target=T, moment=moment)
            result = legacy_morning_receipt.create(T, root=root, now=moment)
            rec = result["receipt"]
            rdir = fc.research_dir(T, root)
            (rdir / fc.DRAFT_NAME).write_text(json.dumps({
                "target_session_date": T, "candidates": []}))
            self.assertTrue(fc.build_for_receipt(rec, root=root)["written"])
            out = cah.check(T, root=root, ledger_path=Path("/nonexistent-disabled"))
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["health_label"], "invalid")
            self.assertIsNone(out["receipt_classification"])
            self.assertIn("freeze_receipt_missing_or_unreadable", out["warnings"])

    def test_structured_draft_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.write_draft([{"symbol": "DAL", "classification": "Watch", "score": 68}])
            self.assertTrue(repo.build()["written"])
            out = repo.check()
            self.assertTrue(out["ok"])
            self.assertEqual(out["health_label"], "healthy")
            self.assertEqual(out["source"], "structured_draft")
            self.assertTrue(out["artifact_present"] and out["artifact_valid"])
            self.assertEqual(out["candidate_count"], 1)
            self.assertEqual(out["receipt_classification"], repo.rec["classification"])
            self.assertFalse(out["buy_authorized"])

    def test_empty_structured_list_on_no_trade_is_still_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            official = health_fixtures.v2_official_receipt(
                root, T, intent="no_trade")
            repo = Repo(td, official)
            repo.write_draft([])
            self.assertTrue(repo.build()["written"])
            out = repo.check()
            self.assertTrue(out["ok"])
            self.assertEqual(out["health_label"], "healthy")
            self.assertEqual(out["candidate_count"], 0)
            self.assertTrue(out["official_research"])

    def test_extractor_fallback_labeled_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            out = repo.check()
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["health_label"], "fallback")
            self.assertEqual(out["source"], "report_extractor_fallback")
            self.assertIn("check prompt", out["action_hint"])

    def test_coherently_resealed_source_none_cannot_hide_source_rederivation(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            path = fc.artifact_path(T, repo.root)
            document = json.loads(path.read_text())
            document.update({
                "source": "none", "candidates_draft_sha256": None,
                "candidate_count": 0, "dropped_count": 0,
                "warnings": [], "candidates": [],
            })
            path.write_text(json.dumps(document))
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertIn("artifact_source_rederivation_mismatch", out["warnings"])

    def test_missing_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)   # receipt only; no candidates.json
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "missing")
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["source"], "unavailable")

    def test_unreadable_artifact_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            fc.artifact_path(T, repo.root).write_text("{corrupt")
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertIn("artifact_unreadable", out["warnings"])

    def test_symlink_artifact_is_invalid_even_when_target_bytes_are_valid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.write_draft([{"symbol": "DAL", "classification": "Watch"}])
            self.assertTrue(repo.build()["written"])
            canonical = fc.artifact_path(T, repo.root)
            real = canonical.with_name("candidate-bytes.json")
            canonical.replace(real)
            canonical.symlink_to(real)
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertIn("artifact_symlink_or_nonfile", out["warnings"])

    def test_validation_failure_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            path = fc.artifact_path(T, repo.root)
            doc = json.loads(path.read_text())
            doc["execution_enabled"] = True   # forbidden claim
            path.write_text(json.dumps(doc))
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertTrue(any("execution_enabled" in w for w in out["warnings"]))

    def test_receipt_hash_mismatch_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            path = fc.artifact_path(T, repo.root)
            doc = json.loads(path.read_text())
            doc["report_sha256"] = "f" * 64   # artifact no longer matches its receipt
            path.write_text(json.dumps(doc))
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertIn("artifact_receipt_report_hash_mismatch", out["warnings"])

    def test_missing_receipt_invalidates_an_existing_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            (fc.research_dir(T, repo.root) / "freeze_receipt.json").unlink()
            out = repo.check()
            self.assertFalse(out["ok"])
            self.assertEqual(out["health_label"], "invalid")
            self.assertIn("freeze_receipt_missing_or_unreadable", out["warnings"])
            self.assertIsNone(out["receipt_classification"])

    def test_preliminary_report_tamper_fails_shared_completion_probe(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.write_draft([])
            self.assertTrue(repo.build()["written"])
            report = Path(repo.rec["report_path"])
            report.write_text(report.read_text(encoding="utf-8") + "\ntampered\n",
                              encoding="utf-8")
            out = repo.check()
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["health_label"], "invalid")
            self.assertTrue(any("preliminary_completion_reanchor_failed" in warning
                                for warning in out["warnings"]), out["warnings"])

    def test_preliminary_source_swap_during_health_check_is_caught_by_final_reanchor(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.write_draft([])
            self.assertTrue(repo.build()["written"])
            report = Path(repo.rec["report_path"])
            original = fc.validate_completion_binding_for_candidates
            calls = 0

            def mutate_after_first_probe(*args, **kwargs):
                nonlocal calls
                result = original(*args, **kwargs)
                calls += 1
                if calls == 1:
                    report.write_text(
                        report.read_text(encoding="utf-8") + "\nswapped-after-probe\n",
                        encoding="utf-8")
                return result

            with mock.patch.object(
                    fc, "validate_completion_binding_for_candidates",
                    side_effect=mutate_after_first_probe):
                out = repo.check()
            self.assertGreaterEqual(calls, 2)
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["health_label"], "invalid")
            self.assertTrue(any("preliminary_completion_reanchor_failed" in warning
                                for warning in out["warnings"]), out["warnings"])

    def test_official_authority_sources_are_reanchored_before_labels_are_copied(self):
        for mutation in ("evidence", "live_source", "prose", "candidate_quote"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                official = health_fixtures.v2_official_receipt(root, T)
                repo = Repo(td, official)
                repo.write_draft([])
                self.assertTrue(repo.build()["written"])
                if mutation == "evidence":
                    evidence = fc.research_dir(T, root) / "evidence.json"
                    evidence.write_text('{"tampered":true}\n', encoding="utf-8")
                    official["evidence_sha256"] = fc._sha256_file(evidence)
                elif mutation == "live_source":
                    source = health_fixtures.research_authority.rh_artifact_path(T, root)
                    source_doc = json.loads(source.read_text(encoding="utf-8"))
                    source_doc["status"] = "ROBINHOOD_ACCOUNT_CONTEXT_UNAVAILABLE"
                    source.write_text(json.dumps(source_doc), encoding="utf-8")
                    digest = fc._sha256_file(source)
                    official["rh_artifact_sha256"] = digest
                    official["live_price_source_artifact_sha256"] = digest
                elif mutation == "prose":
                    report = Path(official["report_path"])
                    report.write_text(
                        report.read_text(encoding="utf-8").replace(
                            "Execution: DISABLED", "Execution: ENABLED"),
                        encoding="utf-8")
                    official["report_sha256"] = fc._sha256_file(report)
                else:
                    official["candidate_quote_summary"]["status"] = "ready"
                (fc.research_dir(T, root) / "freeze_receipt.json").write_text(
                    json.dumps(official, indent=1, sort_keys=True), encoding="utf-8")

                out = repo.check()
                self.assertFalse(out["ok"], out)
                self.assertEqual(out["health_label"], "invalid")
                self.assertIsNone(out["receipt_classification"])
                self.assertIsNone(out["official_research"])
                self.assertIsNone(out["buy_authorized"])
                self.assertTrue(any("official_reanchor_failed" in warning
                                    for warning in out["warnings"]), out["warnings"])


class TestFramingAndCLI(unittest.TestCase):
    def test_no_execution_or_performance_claims_on_any_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            for stage in ("missing", "built"):
                if stage == "built":
                    repo.build()
                out = repo.check()
                self.assertTrue(out["diagnostic_only"])
                self.assertFalse(out["execution_enabled"])
                self.assertFalse(out["activation_authority"])
                text = json.dumps(out).lower()
                for forbidden in ("executed trade", "profit", "expectancy", "place the order"):
                    self.assertNotIn(forbidden, text)

    def test_cli_exit_codes_and_ledger_crosscheck(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            ledger = repo.root / "ledger.json"
            ledger.write_text(json.dumps({"entries": [{"target_session_date": "1999-01-01"}]}))
            proc = subprocess.run(
                [PY, str(ROOT / "scripts" / "candidate_artifact_health.py"),
                 "--target", T, "--root", str(repo.root), "--ledger", str(ledger)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = json.loads(proc.stdout)
            self.assertIn("no_research_ledger_entry_for_target", out["warnings"])
            # missing artifact => exit 1
            proc2 = subprocess.run(
                [PY, str(ROOT / "scripts" / "candidate_artifact_health.py"),
                 "--target", "2026-01-01", "--root", str(repo.root), "--ledger", "-"],
                capture_output=True, text=True)
            self.assertEqual(proc2.returncode, 1)
            self.assertEqual(json.loads(proc2.stdout)["health_label"], "missing")

    def test_checker_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Repo(td)
            repo.build()
            rdir = fc.research_dir(T, repo.root)
            before = {str(p.relative_to(rdir)): p.read_bytes()
                      for p in rdir.rglob("*") if p.is_file()}
            repo.check()
            after = {str(p.relative_to(rdir)): p.read_bytes()
                     for p in rdir.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "health check must never modify runtime artifacts")


if __name__ == "__main__":
    unittest.main(verbosity=1)
