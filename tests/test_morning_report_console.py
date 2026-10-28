#!/usr/bin/env python3
"""Deterministic acceptance tests for the console-native morning-report surface."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import morning_report_console as reports  # noqa: E402
    import operator_console as console  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report(target: str, classification: str, candidate_body: str = "None.",
            decision: str = "WATCH. No manual order is proposed.") -> str:
    return f"""# Frozen Morning Research — {target}

- Generated (UTC): {target}T13:00:00+00:00
- Data cutoff: same-day validated snapshot

## Market Regime
Range-bound; source supported.

## Best Actionable Setups
{candidate_body}

## Final Decision
**{decision}**

## Buy Block Reasons
- independent_buy_gate_blocked
"""


def _official(root: Path, target: str, classification: str,
              candidate_body: str = "None.", decision: str = "WATCH.",
              buy_authorized: bool = False) -> Path:
    path = root / "reports" / "morning" / "official_research" / f"{target}.md"
    _write(path, _report(target, classification, candidate_body, decision))
    receipt = {
        "schema": "research_freeze_receipt/1.0",
        "target_session_date": target,
        "official": True,
        "classification": classification,
        "report_sha256": _sha(path),
        "frozen_utc": f"{target}T13:01:00+00:00",
        "live_price_authority": "none",
        "buy_authorized": buy_authorized,
        "buy_block_reasons": [] if buy_authorized else ["independent_buy_gate_blocked"],
    }
    receipt_path = root / "data" / "runtime" / "research" / target / "freeze_receipt.json"
    _write(receipt_path, json.dumps(receipt))
    return path


def _probe(classification: str) -> dict:
    return {
        "schema": "freeze_completion_probe/1.0",
        "completed": True,
        "reason": "research_freeze_receipt_valid",
        "kind": "research_freeze_receipt",
        "official": True,
        "classification": classification,
    }


class FakeCache:
    def get(self, force: bool = False) -> dict:
        return {"schema": console.OVERVIEW_SCHEMA, "safety": dict(console.SAFETY)}


class FakeControl:
    def snapshot(self, token: str) -> dict:
        return {"schema": "operator_control_state/1.0", "session_token": token,
                "safety": {"execution_enabled": False}}


class MorningReportViewTests(unittest.TestCase):
    def test_official_research_does_not_imply_buy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            _official(root, target, "OFFICIAL_RESEARCH_WATCH")
            with mock.patch.object(reports.freeze_completion_probe, "probe",
                                   return_value=_probe("OFFICIAL_RESEARCH_WATCH")):
                view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "OFFICIAL")
        self.assertTrue(view["official_research"])
        self.assertFalse(view["buy_authorized"])
        self.assertNotEqual(view["final_action"], "Buy")

    def test_official_no_trade_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            _official(root, target, "OFFICIAL_RESEARCH_NO_TRADE", decision="NO TRADE.")
            with mock.patch.object(reports.freeze_completion_probe, "probe",
                                   return_value=_probe("OFFICIAL_RESEARCH_NO_TRADE")):
                view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "OFFICIAL — NO TRADE")
        self.assertEqual(view["final_action"], "No Trade")
        self.assertFalse(view["buy_authorized"])

    def test_preliminary_timeout_is_no_buy_and_no_trade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-10"
            path = root / "reports/morning/preliminary/2026-08-10_timeout_checkpoint_preliminary_0612PT.md"
            _write(path, """# PRELIMINARY — Timeout Checkpoint
- Generated (UTC): 2026-08-10T13:12:02+00:00
- Classification: **PRELIMINARY_TIMEOUT_CHECKPOINT_NO_TRADE**
## Best Actionable Setups
None.
## Final Decision
**NO TRADE.** Timed out.
## Buy Block Reasons
- timeout_incomplete_pipeline_no_freeze
""")
            view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "PRELIMINARY — NO BUY")
        self.assertEqual(view["final_action"], "No Trade")
        self.assertFalse(view["official_research"])
        self.assertFalse(view["buy_authorized"])
        self.assertEqual(view["candidate_count"], 0)

    def test_absent_artifact_is_not_ready_not_failure_recommendation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "reports/morning").mkdir(parents=True)
            view = reports.report_for_date(root, "2026-08-11")
        self.assertEqual(view["status"], "NOT READY")
        self.assertIsNone(view["final_action"])
        self.assertFalse(view["buy_authorized"])

    def test_no_freeze_receipt_cannot_render_official(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            path = root / "reports/morning/official_research" / f"{target}.md"
            _write(path, _report(target, "OFFICIAL_RESEARCH_WATCH"))
            view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "REPORT FAILURE")
        self.assertFalse(view["official_research"])
        self.assertIsNone(view["final_action"])

    def test_report_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            path = _official(root, target, "OFFICIAL_RESEARCH_WATCH")
            path.write_text(path.read_text() + "\ntampered\n")
            view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "REPORT FAILURE")
        self.assertIsNone(view["final_action"])

    def test_incomplete_finality_chain_cannot_render_official(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            _official(root, target, "OFFICIAL_RESEARCH_WATCH")
            invalid = _probe("OFFICIAL_RESEARCH_WATCH")
            invalid.update({"completed": False, "official": None,
                            "reason": "research_freeze_commit_invalid"})
            with mock.patch.object(reports.freeze_completion_probe, "probe", return_value=invalid):
                view = reports.report_for_date(root, target)
        self.assertEqual(view["status"], "REPORT FAILURE")
        self.assertFalse(view["official_research"])
        self.assertIsNone(view["final_action"])

    def test_candidate_order_count_and_supported_fields_are_exact(self):
        body = """### ZZZ — Zeta Corp
- **Action:** Watch
- **Catalyst:** Verified event
- **Catalyst Date:** 2026-08-12
- **Entry Zone:** 未验证
### AAA — Alpha Inc
- **Action**: Avoid
- **Bear Case**: Dilution risk
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            _official(root, target, "OFFICIAL_RESEARCH_WATCH", body)
            with mock.patch.object(reports.freeze_completion_probe, "probe",
                                   return_value=_probe("OFFICIAL_RESEARCH_WATCH")):
                view = reports.report_for_date(root, target)
        self.assertEqual(view["top_symbols"], ["ZZZ", "AAA"])
        self.assertEqual(view["candidate_count"], 2)
        self.assertEqual([row["symbol"] for row in view["candidates"]], ["ZZZ", "AAA"])
        self.assertEqual(view["candidates"][0]["company"], "Zeta Corp")
        self.assertNotIn("target_1", view["candidates"][0]["fields"])
        self.assertNotIn("conviction", view["candidates"][1]["fields"])

    def test_conflicting_candidate_field_is_marked_unverified(self):
        body = """### ZZZ — Zeta Corp
- Action: Watch
- Action: Avoid
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            _official(root, target, "OFFICIAL_RESEARCH_WATCH", body)
            with mock.patch.object(reports.freeze_completion_probe, "probe",
                                   return_value=_probe("OFFICIAL_RESEARCH_WATCH")):
                view = reports.report_for_date(root, target)
        self.assertEqual(view["candidates"][0]["fields"]["action"], "未验证（冲突）")
        self.assertEqual(view["candidates"][0]["conflicting_fields"], ["action"])

    def test_sensitive_content_never_reaches_api_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = "2026-08-11"
            path = root / "reports/morning/preliminary/2026-08-11_timeout_checkpoint_0612PT.md"
            secret = "refresh_token: SUPER_PRIVATE_FIXTURE"
            _write(path, f"""# PRELIMINARY
## System Status
{secret}
## Final Decision
NO TRADE.
""")
            view = reports.report_for_date(root, target)
        encoded = json.dumps(view)
        self.assertEqual(view["status"], "REPORT FAILURE")
        self.assertNotIn("SUPER_PRIVATE_FIXTURE", encoded)

    def test_symlink_hardlink_casefold_and_traversal_are_rejected(self):
        self.assertTrue(reports._has_casefold_collision([
            "2026-08-11_TIMEOUT_0612PT.md", "2026-08-11_timeout_0612pt.MD"]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.md"
            _write(outside, "# PRELIMINARY\n## Final Decision\nNO TRADE.\n")
            directory = root / "reports/morning/preliminary"
            directory.mkdir(parents=True)
            symlink = directory / "2026-08-11_timeout_checkpoint_0612PT.md"
            symlink.symlink_to(outside)
            view = reports.report_for_date(root, "2026-08-11")
            self.assertEqual(view["status"], "REPORT FAILURE")
            symlink.unlink()
            first = directory / "2026-08-11_timeout_checkpoint_0612PT.md"
            _write(first, "# PRELIMINARY\n## Final Decision\nNO TRADE.\n")
            second = directory / "2026-08-11_other_0612PT.md"
            os.link(first, second)
            view = reports.report_for_date(root, "2026-08-11")
            self.assertEqual(view["status"], "REPORT FAILURE")
            second.unlink()
            bad = reports.report_for_date(root, "../../secrets")
            self.assertEqual(bad["status"], "REPORT FAILURE")

    def test_actual_timeout_checkpoint_is_truthful_read_only(self):
        path = ROOT / "reports/morning/preliminary/2026-08-10_timeout_checkpoint_preliminary_0612PT.md"
        before = _sha(path)
        view = reports.report_for_date(ROOT, "2026-08-10")
        after = _sha(path)
        self.assertEqual(before, after)
        self.assertEqual(view["status"], "PRELIMINARY — NO BUY")
        self.assertEqual(view["final_action"], "No Trade")
        self.assertEqual(view["candidate_count"], 0)
        self.assertFalse(view["buy_authorized"])
        self.assertFalse(view["execution_enabled"])
        self.assertFalse(view["options_execution_enabled"])
        self.assertFalse(view["safety"]["execution_enabled"])
        self.assertFalse(view["safety"]["options_execution_enabled"])


class MorningReportHttpTests(unittest.TestCase):
    def test_home_latest_history_and_date_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "web/dist"
            static.mkdir(parents=True)
            _write(static / "index.html", "<!doctype html><title>Console Report</title>")
            target = "2026-08-10"
            _write(root / "reports/morning/preliminary/2026-08-10_timeout_checkpoint_0612PT.md",
                   "# PRELIMINARY\n## Best Actionable Setups\nNone.\n## Final Decision\nNO TRADE.\n")
            policy = dict(console.EXPECTED_POLICY)
            handler = console.make_handler(
                root=root, policy=policy, cache=FakeCache(), context_cache=FakeCache(),
                control_plane=FakeControl(), session_token="x" * 40, quiet=True)
            server = console.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                def get(path: str):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    raw = response.read()
                    connection.close()
                    return response.status, raw

                for page in ("/", "/reports/morning", "/reports/morning/latest",
                             f"/reports/morning/{target}"):
                    status, raw = get(page)
                    self.assertEqual(status, 200)
                    self.assertIn(b"Console Report", raw)
                status, raw = get(f"/api/v1/morning-reports/{target}")
                payload = json.loads(raw)
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "PRELIMINARY — NO BUY")
                # The historical target is past the audit deadline and has no observer state.
                # Surface that engineering terminal as FAILED without changing the valid
                # Preliminary market classification.
                self.assertEqual(payload["engineering_audit"]["state"], "FAILED")
                self.assertFalse(payload["engineering_audit"]["execution_enabled"])
                self.assertFalse(payload["engineering_audit"]["options_execution_enabled"])
                status, raw = get("/api/v1/morning-reports")
                self.assertEqual(status, 200)
                history = json.loads(raw)
                self.assertEqual(history["reports"][0]["target_session_date"], target)
                self.assertEqual(history["reports"][0]["engineering_audit_state"], "FAILED")
                self.assertEqual(get("/api/v1/morning-reports/%2e%2e")[0], 404)
                self.assertEqual(get("/api/v1/morning-reports/2026-08-10/extra")[0], 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_frontend_escapes_report_content_and_has_no_pdf_or_external_call(self):
        source = (ROOT / "web/src/main.js").read_text(encoding="utf-8")
        self.assertIn("renderMorningPanel", source)
        self.assertIn("renderMorningReport", source)
        self.assertIn("renderMorningHistory", source)
        self.assertIn("renderEngineeringAudit", source)
        self.assertIn("Separate engineering layer", source)
        self.assertIn("Audit supervisor", source)
        self.assertIn("Iteration receipt", source)
        self.assertIn("observer_iteration_backlog", source)
        self.assertIn("Runtime binding", source)
        self.assertIn("Exact snapshot", source)
        self.assertIn("Validated outputs", source)
        self.assertIn("expected_output_drift", source)
        self.assertIn("Engineering health never changes market classification or Buy authority", source)
        self.assertIn('esc(section.body)', source)
        self.assertIn('.replaceAll("&", "&amp;").replaceAll("<", "&lt;")', source)
        self.assertIn('esc(value)', source)
        self.assertIn("/api/v1/morning-reports/latest", source)
        self.assertNotIn("/api/v1/morning-reports/export", source)
        self.assertEqual(source.count("fetch("), 2)
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)

    def test_console_report_lane_imports_no_transport_or_trading_surface(self):
        console_source = (ROOT / "scripts/lib/morning_report_console.py").read_text(encoding="utf-8")
        self.assertNotIn("report_pdf_", console_source)
        self.assertFalse((ROOT / "scripts/lib/report_pdf_document.py").exists())
        self.assertFalse((ROOT / "scripts/lib/report_pdf_renderer.py").exists())
        paths = [
            ROOT / "scripts/lib/morning_report_console.py",
            ROOT / "scripts/lib/report_source_adapter.py",
            ROOT / "scripts/morning_finality_guard.py",
            ROOT / "scripts/morning_engineering_audit.py",
            ROOT / "scripts/morning_audit_supervisor.py",
        ]
        aggregate = "\n".join(path.read_text(encoding="utf-8").lower() for path in paths)
        for prohibited in (
                "gmail", "oauth", "smtp", "apple mail", "users.messages.send",
                "place_order", "submit_order", "cancel_order", "private_mcp"):
            self.assertNotIn(prohibited, aggregate)
        policy = json.loads((ROOT / "config/operator_console.json").read_text())
        self.assertFalse(policy["safety"]["execution_enabled"])
        self.assertFalse(policy["safety"]["options_execution_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
