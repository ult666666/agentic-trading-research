#!/usr/bin/env python3
"""Hermetic tests for multi-plane autonomy health reconciliation."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import autonomy_service_health as health  # noqa: E402
    import broker_runtime_audit as broker_audit  # noqa: E402
    import candidate_quote_hydrator as quote_hydrator  # noqa: E402
    import data_preflight  # noqa: E402
    import free_source_health as source_health  # noqa: E402
    import free_source_health_alert as source_alert  # noqa: E402
    import legacy_morning_receipt  # noqa: E402
    import macro_actual_refresh  # noqa: E402
    import power_schedule_health as power_health  # noqa: E402
    import release_snapshot  # noqa: E402
    import runtime_provenance  # noqa: E402
    import research_authority  # noqa: E402
    import research_freeze  # noqa: E402
    import snapshot_gate  # noqa: E402
    import source_matrix  # noqa: E402
    import test_free_ensemble as free_ensemble_fixture  # noqa: E402
    from provider_adapters import base as adapter_base  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc


def scheduler(ok: bool = True) -> dict:
    statuses = {
        "com.charles.trading.intake": "healthy",
        "com.charles.trading.macro-refresh": "healthy",
        "com.charles.trading.morning": "healthy",
        "com.charles.trading.premarket": "healthy",
        "com.charles.trading.postclose": "healthy",
        "com.charles.trading.watchdog": "healthy",
    }
    if not ok:
        statuses["com.charles.trading.morning"] = "drifted"
    return {
        "schema": "launchd_scheduler_health/1.1",
        "ok": ok,
        "scheduler_config_ok": ok,
        "labels": [{"label": label, "scheduler_config_status": status}
                   for label, status in statuses.items()],
    }


CONFIG_SHA256 = "a" * 64


def attempt(state: str, outcome: str | None, exit_code: int | None,
            finished: str | None, started: str | None = None, *,
            workflow: str = "morning", dry_run: bool = False) -> dict:
    attempts = [] if state == "pending" else [{
        "run_id": f"fixture-{workflow}-run", "config_sha256": CONFIG_SHA256,
        "state": state, "outcome": outcome, "dry_run": dry_run,
        "owner_pid": 4242, "child_pid": 4243,
        "exit_code": exit_code, "started_utc": started or finished,
        "finished_utc": finished, "preflight": {}, "postconditions": None,
    }]
    return {"state": state,
            "latest_run_id": f"fixture-{workflow}-run" if attempts else None,
            "attempts": attempts}


def daily_state(target: str, morning_state: str = "failed",
                morning_outcome: str = "runner_exit_124") -> dict:
    document = {
        "schema": "autonomy_daily_state/1.1",
        "target_session_date": target,
        "operating_timezone": "America/Los_Angeles",
        "research_only": True,
        "execution_enabled": False,
        "activation_authority": False,
        "strategy_change_allowed": False,
        "config_sha256": CONFIG_SHA256,
        "revision": 4,
        "overall_state": "in_progress",
        "workflows": {
            "intake": attempt("degraded", "data_readiness_degraded", 0,
                              "2026-07-10T12:00:06+00:00",
                              "2026-07-10T12:00:02+00:00", workflow="intake"),
            "macro_refresh": attempt("completed", "contract_satisfied", 0,
                                     "2026-07-10T12:40:00+00:00",
                                     "2026-07-10T12:35:01+00:00",
                                     workflow="macro_refresh"),
            "morning": attempt(morning_state, morning_outcome,
                               124 if morning_state == "failed" else 0,
                               "2026-07-10T13:18:05+00:00",
                               "2026-07-10T12:32:05+00:00", workflow="morning"),
            "post_open": attempt("skipped", "no_official_morning_artifact", 0,
                                 "2026-07-10T13:45:01+00:00",
                                 "2026-07-10T13:45:00+00:00",
                                 workflow="post_open"),
            "post_close": attempt("pending", None, None, None,
                                  workflow="post_close"),
        },
        "events": [],
        "updated_utc": "2026-07-10T23:30:00+00:00",
    }
    # Production appends lifecycle events in real time and the chain check is time-sorted, so
    # workflow order never matters. (These fixtures predate the 2026-07-18 05:38 move and keep a
    # morning start BEFORE the 05:35 macro attempt — a legal interleave that must stay accepted.)
    sync_lifecycle_events(document)
    return document


def sync_lifecycle_events(state: dict) -> None:
    staged: list[tuple[str, int, str, str, str, str]] = []
    for name, workflow in state["workflows"].items():
        for attempt_row in workflow.get("attempts") or []:
            run_id = attempt_row["run_id"]
            start_reason = "dry_run" if attempt_row.get("dry_run") is True else "production"
            staged.append((attempt_row["started_utc"], 0, name, run_id,
                           "attempt_started", start_reason))
            if attempt_row.get("child_pid") is not None:
                staged.append((attempt_row["started_utc"], 1, name, run_id,
                               "runner_started", "allowlisted_runner"))
            if attempt_row.get("finished_utc") is not None:
                staged.append((attempt_row["finished_utc"], 2, name, run_id,
                               "attempt_finished", attempt_row["outcome"]))
    staged.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    state["events"] = [
        {"seq": index, "utc": timestamp, "workflow": name, "run_id": run_id,
         "event": event, "reason": reason}
        for index, (timestamp, _priority, name, run_id, event, reason)
        in enumerate(staged)
    ]


def safe_run_status(target: str) -> dict:
    return {
        "target_session_date": target,
        "status": "failed",
        "failure_class": "claude_timeout",
        "no_order_or_mutation_invoked": True,
        "execution_enabled": False,
        "counts_in_metrics": False,
        "ledger_incremented": False,
        "preliminary_checkpoint_written": True,
    }


def reseal(document: dict) -> dict:
    document["artifact_sha256"] = hashlib.sha256(
        health._canonical(health._payload(document))).hexdigest()
    return document


def broker_sli(root: Path, target: str, status: str = "zero_verified") -> dict:
    audit_root = root / "data" / "runtime" / "broker_audit"
    started = dt.datetime.fromisoformat(f"{target}T15:55:00+00:00")
    context = broker_audit.start_invocation(
        audit_root=audit_root, target=target, workflow="morning",
        run_id="morning-fixture", invocation_id="invocation", now=started)
    broker_audit._append_event(
        context,
        broker_audit._event(
            context, "session_start", session_id="fixture-session",
            now=started + dt.timedelta(seconds=1)))
    broker_audit._append_event(
        context,
        broker_audit._event(
            context, "mcp_tool_attempt",
            tool_name="mcp__robinhood-trading__get_equity_quotes",
            tool_input={"symbol": "SPY"}, classification="broker_read_only",
            now=started + dt.timedelta(seconds=2)))
    if status == "violation":
        broker_audit._append_event(
            context,
            broker_audit._event(
                context, "mcp_tool_attempt",
                tool_name="mcp__robinhood-trading__review_equity_order",
                tool_input={"symbol": "SPY"},
                classification="broker_preview_or_write",
                now=started + dt.timedelta(seconds=3)))
    broker_audit.finalize_invocation(
        context, child_exit_code=0,
        started_utc=broker_audit._utc(started),
        finished_utc=broker_audit._utc(started + dt.timedelta(seconds=4)))
    return broker_audit.summarize_target(
        audit_root=audit_root, target=target,
        now=started + dt.timedelta(minutes=5),
        out=audit_root / target / "sli.json")


def release_gate(status: str = "match", *, runtime_binding: str | None = None,
                 runtime_ok: bool | None = None,
                 reason_codes: list[str] | None = None) -> dict:
    comparisons = {key: (status == "match" or key in {
        "head_commit", "head_ref", "staged_patch"}) for key in (
        "head_commit", "head_ref", "status_v2_z", "staged_patch", "unstaged_patch",
        "untracked_inventory")}
    forensic = {
        "schema": release_snapshot.GATE_SCHEMA,
        "generated_utc": "2026-07-10T16:00:00Z",
        "status": status,
        "release_reproducible": status == "match",
        "snapshot_id": "release-fixture",
        "snapshot_receipt_sha256": "1" * 64,
        "snapshot_location_sha256": "2" * 64,
        # 2026-07-28 head binding: a genuinely CURRENT gate carries the current repo HEAD;
        # the stale-head path is covered by test_p0_guard_observability.
        "head_commit": (health._current_repo_head() or "3" * 40),
        "comparisons": comparisons,
        "drift": ([] if status == "match" else
                  sorted(key for key, matched in comparisons.items() if not matched)),
        "errors": [],
        "safety": dict(release_snapshot.SAFETY),
    }
    forensic["gate_sha256"] = release_snapshot._sha_bytes(
        release_snapshot._canonical(forensic))
    binding = runtime_binding or ("match" if status == "match" else "blocked")
    ok = (binding in {"match", "expected_output_drift"}
          if runtime_ok is None else runtime_ok)
    reasons = ([] if ok else (reason_codes or ["runtime_source_modified"]))
    return {
        "schema": runtime_provenance.SCHEMA,
        "ok": ok,
        "status": "ready" if ok else "blocked",
        "reason_codes": sorted(set(reasons)),
        "branch": "main",
        "expected_branch": "main",
        "head_commit": forensic["head_commit"],
        "release_snapshot_id": forensic["snapshot_id"],
        "release_status": status,
        "runtime_binding_status": binding,
        "release_snapshot_verification": forensic,
        "release_drift": forensic["drift"],
        "release_errors": [],
        "modified_runtime_paths": [],
        "staged_runtime_paths": [],
        "all_staged_paths": [],
        "untracked_runtime_paths": [],
        "critical_missing": [],
        "modified_critical_paths": [],
        "critical_head_verification_available": True,
        "validated_generated_output_paths": (
            ["reports/post_close/2026-07-10.md"]
            if binding == "expected_output_drift" else []),
        "output_drift_reasons": [],
    }


def legacy_release_gate(status: str = "match") -> dict:
    return release_gate(status)["release_snapshot_verification"]


def power_receipt(time_pt: str = "04:55") -> dict:
    policy = json.loads(power_health.CONFIG_PATH.read_text())
    hour, minute = time_pt.split(":")
    stdout = (
        "Repeating power events:\n"
        f"  wakepoweron at {int(hour)}:{minute}AM weekdays only\n"
        "Scheduled power events:\n")
    document = power_health.build_health(
        stdout=stdout, exit_code=0, policy=policy,
        now=dt.datetime(2026, 7, 10, 16, 0, tzinfo=dt.timezone.utc))
    assert power_health.validate_receipt(document, policy) == []
    return document


def candidate_health(target: str, label: str = "healthy") -> dict:
    ok = label in {"healthy", "fallback", "empty"}
    return {
        "ok": ok,
        "target_session_date": target,
        "artifact_present": label != "missing",
        "artifact_valid": label in {"healthy", "fallback", "empty"},
        "source": "structured_draft" if label == "healthy" else "unavailable",
        "warnings": (["artifact_receipt_report_hash_mismatch"]
                     if label == "invalid" else []),
        "health_label": label,
        "diagnostic_only": True,
        "execution_enabled": False,
        "activation_authority": False,
    }


def candidate_quote_health(target: str, status: str = "ready") -> dict:
    requested = 0 if status in {"empty", "missing"} else 2
    fresh = requested if status == "ready" else (1 if status == "partial" else 0)
    actionable = fresh
    present = status not in {"missing"}
    document = {
        "schema": quote_hydrator.HEALTH_SCHEMA,
        "target_session_date": target,
        "generated_utc": "2026-07-10T16:00:00+00:00",
        "status": status,
        "ok": status in {"ready", "partial", "empty"},
        "request_present": present,
        "bundle_present": present,
        "requested_count": requested,
        "fresh_count": fresh,
        "actionable_count": actionable,
        "unavailable_count": requested - fresh,
        "reason_codes": ([] if status in {"ready", "empty"}
                         else [f"candidate_quotes_{status}"]),
        "source": "robinhood_readonly",
        "request_sha256": "1" * 64 if present else None,
        "bundle_sha256": "2" * 64 if present else None,
        **quote_hydrator.HEALTH_SAFETY,
    }
    return quote_hydrator._seal(document)


def _v2_research_receipt(root: Path, target: str, *, intent: str = "watch",
                         force_preliminary: bool = False) -> dict:
    """Stage a real zero-catalyst v2 freeze for any July 2026 target.

    Sparse hand-written receipts are intentionally not fixtures: every downstream consumer now
    re-anchors evidence, live-source bytes, authority, prose/title, and candidate-quote context.
    ``force_preliminary`` removes the live-price artifact immediately before freeze so the real
    production writer emits a fully bound fail-closed Preliminary receipt.
    """
    target_date = dt.date.fromisoformat(target)
    prior = (target_date - dt.timedelta(days=1)).isoformat()
    frozen = dt.datetime.combine(target_date, dt.time(12, 55), dt.timezone.utc)
    quote_time = dt.datetime.combine(target_date, dt.time(12, 40), dt.timezone.utc)

    preflight = snapshot_gate.snapshot_path(target, root)
    preflight.parent.mkdir(parents=True, exist_ok=True)
    preflight.write_text(json.dumps({
        "target_session_date": target,
        "generated_utc": (frozen - dt.timedelta(minutes=7)).isoformat(),
        "primary_provider": "fmp",
        "fields": [{
            "symbol": "SPY", "category": "index_quote", "source": "fmp",
            "status": "ok", "same_day": False, "vintage": "previous_close",
            "market_timestamp": f"{prior} 16:00:00 EDT",
        }],
        "determination": {
            "coverage_label": "insufficient", "sufficient_for_buy": False,
            "catalyst_sources": [],
        },
    }), encoding="utf-8")

    robinhood = research_authority.rh_artifact_path(target, root)
    robinhood.parent.mkdir(parents=True, exist_ok=True)
    robinhood.write_text(json.dumps({
        "schema": "robinhood_verifier/1.0", "target_session_date": target,
        "validated_utc": (frozen - dt.timedelta(minutes=6)).isoformat(),
        "status": "ROBINHOOD_VERIFIED", "no_order_or_mutation_invoked": True,
        "spy_quote": {
            "last": 620.5, "bid": 620.4, "ask": 620.6, "spread_abs": 0.2,
            "spread_pct": 0.000322, "spread_bps": 3.22,
            "market_timestamp_utc": quote_time.isoformat(),
            "market_timestamp_et": f"{target} 08:40:00 EDT",
            "age_minutes": 15.0, "vintage": "premarket",
            "session_reported": "premarket",
        },
        "tradability": {"tradeable": True, "fractional": True},
        "account": {
            "agentic_allowed": True, "type": "cash", "match_count": 1,
            "cash_account_confirmed": True,
        },
        "portfolio": {
            "total_value": 10000.0, "cash": 10000.0, "buying_power": 10000.0,
            "sizing_basis_usd": 5000.0,
        },
        "portfolio_complete": True, "positions": [], "positions_complete": True,
        "open_orders_checked": True, "open_order_count": 0,
        "buy_block_reasons": [], "errors": [],
    }), encoding="utf-8")

    research_dir = root / "data" / "runtime" / "research" / target
    agents_dir = research_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    matrix = source_matrix.load_matrix()
    agents: dict[str, dict] = {}
    for name in list(matrix["required_agents"]) + list(matrix["attempt_required_agents"]):
        path = agents_dir / f"{name}.json"
        path.write_text(json.dumps({
            "agent": name, "status": "completed", "target_session_date": target,
            "generated_utc": (frozen - dt.timedelta(minutes=1)).isoformat(),
        }), encoding="utf-8")
        agents[name] = {
            "status": "completed",
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (research_dir / "evidence.json").write_text(json.dumps({
        "schema": "research_evidence/1.0", "governance_version": "v2.0",
        "target_session_date": target, "generated_utc": frozen.isoformat(),
        "claims": [], "catalysts": [], "live_price": None, "agents": agents,
        "conflicts": [],
        "risk_review": {
            "completed": True, "by": "portfolio-risk-agent+skeptic-agent",
        },
    }), encoding="utf-8")

    draft = research_dir / "draft.md"
    prose = (free_ensemble_fixture.HEADER.format(t=free_ensemble_fixture.T)
             + free_ensemble_fixture.SECTIONS)
    prose = prose.replace("2026-07-01", prior).replace(free_ensemble_fixture.T, target)
    draft.write_text(prose, encoding="utf-8")
    if force_preliminary:
        robinhood.unlink()
    # Keep this a receipt fixture: callers that test candidate derivation must be able to stage
    # their own candidates_draft.json before invoking the candidate builder.
    import freeze_candidates as candidate_builder
    original_builder = candidate_builder.build_for_receipt
    candidate_builder.build_for_receipt = lambda *_a, **_k: {
        "written": False, "reason": "fixture_candidate_build_deferred"}
    try:
        # These tests exercise receipt consumers, not the repository-wide change-control gate;
        # make the fixture hermetic even while another test edits protected configuration.
        with mock.patch.object(
                research_freeze.validate_trial_baseline, "check",
                return_value={"ok": True, "drift": []}):
            result = research_freeze.freeze(
                draft, target, intent, is_trading_day=True, real_now=frozen,
                reports_root=root / "reports", root=root)
    finally:
        candidate_builder.build_for_receipt = original_builder
    assert result.get("official") is (not force_preliminary), result
    return json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))


def v2_official_receipt(root: Path, target: str, *, intent: str = "watch") -> dict:
    """Stage a real, fully re-anchorable Official Research receipt."""
    return _v2_research_receipt(root, target, intent=intent)


def v2_preliminary_receipt(root: Path, target: str, *, intent: str = "watch") -> dict:
    """Stage a real, fully re-anchorable Preliminary Research receipt."""
    return _v2_research_receipt(
        root, target, intent=intent, force_preliminary=True)


def legacy_official_receipt(root: Path, target: str) -> dict:
    """Stage one independently valid v1 Official terminal binding in an isolated root."""
    moment = dt.datetime.fromisoformat(f"{target}T13:00:00+00:00")
    config = data_preflight.load_config()
    provider = config["primary_provider"]
    symbols = ["SPY", "QQQ", "DIA", "IWM", "VTI", "MDY"]
    fields = [{
        "symbol": symbol,
        "category": "index_quote",
        "source": provider,
        "status": "ok",
        "same_day": True,
        "vintage": "premarket",
        "value": 100.0,
        "market_timestamp": f"{target} 08:55:00 EDT",
    } for symbol in symbols[:config["min_index_anchors_fresh"]]]
    preflight = snapshot_gate.snapshot_path(target, root)
    preflight.parent.mkdir(parents=True, exist_ok=True)
    preflight.write_text(json.dumps({
        "target_session_date": target,
        "generated_utc": moment.isoformat(),
        "primary_provider": provider,
        "fields": fields,
        "determination": {
            "coverage_label": "single_source",
            "sufficient_for_buy": False,
            "catalyst_sources": [],
        },
    }))
    report = root / "reports" / "morning" / f"{target}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Official Morning Report\nNo Trade.\n")
    return legacy_morning_receipt.create(
        target, root=root, now=moment)["receipt"]


def deterministic_terminal_receipt(root: Path, target: str) -> None:
    preflight = root / "data" / "runtime" / "preflight" / f"{target}.json"
    preflight.parent.mkdir(parents=True, exist_ok=True)
    preflight.write_text(json.dumps({
        "target_session_date": target,
        "determination": {
            "fresh_same_day_data": False,
            "coverage_label": "none",
        },
    }))
    health.deterministic_no_live_terminal.emit(
        target=target,
        stage="official_window",
        preflight_path=preflight,
        primary_fresh=False,
        coverage="none",
        rh_status="ROBINHOOD_UNAVAILABLE",
        rh_secondary_live=False,
        root=root,
        now=dt.datetime(2026, 7, 10, 12, 50, 44, tzinfo=dt.timezone.utc),
    )


class TestHealthPlanes(unittest.TestCase):
    def setUp(self):
        # Most tests in this class isolate non-macro planes. Dedicated tests below exercise the
        # canonical macro receipt binding without this patch.
        self._macro_binding_patch = mock.patch.object(
            health, "_macro_receipt_binding_errors", return_value=[])
        self._macro_binding_patch.start()

    def tearDown(self):
        self._macro_binding_patch.stop()
    TARGET = "2026-07-10"
    NOW = dt.datetime(2026, 7, 10, 16, 30, tzinfo=dt.timezone.utc)

    def test_completed_post_close_without_terminal_receipt_is_at_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = daily_state(self.TARGET)
            state["workflows"]["post_close"] = attempt(
                "completed", "contract_satisfied", 0,
                "2026-07-10T21:00:00+00:00",
                "2026-07-10T20:15:00+00:00", workflow="post_close")
            sync_lifecycle_events(state)
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=safe_run_status(self.TARGET), freeze_receipt=None,
                now=dt.datetime(2026, 7, 10, 22, 0, tzinfo=dt.timezone.utc))
        post_close = document["workflow_health"]["post_close"]
        self.assertEqual(post_close["status"], "at_issue")
        self.assertEqual(post_close["reason"], "post_close_terminal_receipt_invalid")
        self.assertEqual(document["overall_status"], "at_issue")
        self.assertFalse(document["ok"])
        self.assertEqual(health.validate(document), [])

    def test_validated_post_close_terminal_receipt_restores_healthy_workflow_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = daily_state(self.TARGET)
            state["workflows"]["post_close"] = attempt(
                "completed", "contract_satisfied", 0,
                "2026-07-10T21:00:00+00:00",
                "2026-07-10T20:15:00+00:00", workflow="post_close")
            sync_lifecycle_events(state)
            with mock.patch.object(
                    health.post_close_finality, "validate_terminal", return_value=[]), \
                    mock.patch.object(
                        health.post_open_finality, "validate_terminal", return_value=[]):
                document = health.compose(
                    target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                    run_status=safe_run_status(self.TARGET), freeze_receipt=None,
                    now=dt.datetime(2026, 7, 10, 22, 0, tzinfo=dt.timezone.utc))
        post_close = document["workflow_health"]["post_close"]
        self.assertEqual(post_close["status"], "healthy")
        self.assertEqual(post_close["reason"], "post_close_terminal_receipt_validated")
        self.assertEqual(health.validate(document), [])

    def test_config_healthy_workflow_failed_safe_slo_missed(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW)
        self.assertEqual(document["scheduler_config_health"]["status"], "healthy")
        self.assertEqual(document["workflow_health"]["morning"]["status"], "failed_safe")
        self.assertEqual(document["service_slo_health"]["morning"]["status"], "missed")
        self.assertEqual(document["overall_status"], "at_issue")
        self.assertFalse(document["ok"])
        self.assertEqual(health.validate(document), [])

    def test_no_live_terminal_makes_missing_candidates_expected_degradation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deterministic_terminal_receipt(root, self.TARGET)
            plane = health._research_artifact_plane(
                target=self.TARGET, root=root, workflow_plane={},
                candidate_health=candidate_health(self.TARGET, "missing"),
                candidate_quote_health=candidate_quote_health(self.TARGET, "missing"))
        self.assertEqual(plane["status"], "degraded")
        self.assertEqual(plane["reason_codes"], [])
        self.assertEqual(plane["candidate_artifact"]["status"], "degraded")

    def test_validated_freeze_before_deadline_meets_slo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports" / "morning" / "preliminary" / "report.md"
            report.parent.mkdir(parents=True)
            report.write_text("fixture report\n")
            receipt = {
                "schema": "research_freeze_receipt/1.0",
                "target_session_date": self.TARGET,
                "frozen_utc": "2026-07-10 12:57:22 UTC",
                "report_path": str(report),
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "execution_enabled": False,
            }
            state = daily_state(self.TARGET, "completed", "research_freeze_completed")
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=None, freeze_receipt=receipt, now=self.NOW)
        morning = document["service_slo_health"]["morning"]
        self.assertEqual(morning["status"], "met")
        self.assertEqual(morning["terminal_source"], "validated_freeze_receipt")
        self.assertEqual(morning["receipt_errors"], [])

    def test_validated_legacy_official_receipt_is_historical_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_official_receipt(root, self.TARGET)
            state = daily_state(
                self.TARGET, "completed", "contract_satisfied")
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=None, freeze_receipt=None, now=self.NOW)
        morning = document["service_slo_health"]["morning"]
        self.assertEqual(morning["status"], "missed")
        self.assertEqual(morning["terminal_status"], "missed")
        self.assertIsNone(morning["terminal_source"])

    def test_legacy_receipt_cannot_block_deterministic_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_official_receipt(root, self.TARGET)
            deterministic_terminal_receipt(root, self.TARGET)
            state = daily_state(
                self.TARGET, "completed", "contract_satisfied")
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=None, freeze_receipt=None, now=self.NOW)
        morning = document["service_slo_health"]["morning"]
        self.assertEqual(morning["status"], "met")
        self.assertEqual(morning["terminal_source"],
                         "validated_deterministic_terminal_receipt")
        self.assertEqual(morning["receipt_errors"], [])

    def test_validated_freeze_after_deadline_misses_slo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports" / "morning" / "preliminary" / "late.md"
            report.parent.mkdir(parents=True)
            report.write_text("late fixture\n")
            receipt = {
                "schema": "research_freeze_receipt/1.0",
                "target_session_date": self.TARGET,
                "frozen_utc": "2026-07-10 13:15:55 UTC",
                "report_path": str(report),
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "execution_enabled": False,
            }
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET, "completed", "research_freeze_completed"),
                run_status=None, freeze_receipt=receipt, now=self.NOW)
        self.assertEqual(document["service_slo_health"]["morning"]["status"], "missed")
        self.assertEqual(document["service_slo_health"]["morning"]["reason"],
                         "validated_freeze_after_deadline")

    def test_scheduler_drift_is_independent_of_workflow_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = daily_state(self.TARGET, "completed", "research_freeze_completed")
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(False), state=state,
                run_status=None, freeze_receipt=None, now=self.NOW)
        self.assertEqual(document["scheduler_config_health"]["status"], "at_issue")
        self.assertEqual(document["workflow_health"]["morning"]["status"], "healthy")
        self.assertEqual(document["overall_status"], "at_issue")

    def test_safe_failure_requires_every_nonmutation_proof(self):
        status = safe_run_status(self.TARGET)
        status["ledger_incremented"] = True
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=status,
                freeze_receipt=None, now=self.NOW)
        self.assertEqual(document["workflow_health"]["morning"]["status"], "failed")

    def test_hash_and_safety_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW)
        self.assertEqual(health.validate(document), [])
        changed = json.loads(json.dumps(document))
        changed["overall_status"] = "healthy"
        self.assertIn("artifact_sha256_mismatch", health.validate(changed))
        text = json.dumps(document)
        for forbidden in ("account_number", "buying_power", "api_key", "Bearer"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(document["read_only"] and document["diagnostic_only"])
        self.assertFalse(document["execution_enabled"] or document["activation_authority"])

    def test_pending_no_attempt_state_remains_valid(self):
        pending = health.orchestrator._initial_state(self.TARGET, CONFIG_SHA256)
        valid, errors = health._valid_state(pending, self.TARGET)
        self.assertIs(valid, pending)
        self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=pending, run_status=None, freeze_receipt=None,
                now=dt.datetime(2026, 7, 10, 11, 30, tzinfo=dt.timezone.utc))
        self.assertTrue(all(row["status"] == "pending"
                            for row in document["workflow_health"].values()))
        self.assertEqual(document["overall_status"], "in_progress")
        self.assertEqual(health.validate(document), [])

    def test_valid_sequential_event_chain_remains_accepted(self):
        # The event chain is validated time-sorted regardless of workflow order; this fixture
        # keeps a pre-05:38-era morning-before-macro interleave, which must stay accepted.
        state = daily_state(self.TARGET)
        sync_lifecycle_events(state)
        valid, errors = health._valid_state(state, self.TARGET)
        self.assertIs(valid, state)
        self.assertEqual(errors, [])

    def test_state_safety_and_latest_attempt_mismatches_fail_closed(self):
        mutations = {
            "missing_safety": (
                lambda state: state.pop("research_only"),
                "autonomy_state_safety_invalid:research_only"),
            "wrong_safety": (
                lambda state: state.update(execution_enabled=True),
                "autonomy_state_safety_invalid:execution_enabled"),
            "latest_run": (
                lambda state: state["workflows"]["morning"].update(
                    latest_run_id="forged-latest-run"),
                "autonomy_state_latest_run_id_mismatch:morning"),
            "latest_state": (
                lambda state: state["workflows"]["morning"].update(state="completed"),
                "autonomy_state_latest_state_mismatch:morning"),
        }
        for name, (mutate, expected) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                state = daily_state(self.TARGET)
                mutate(state)
                valid, errors = health._valid_state(state, self.TARGET)
                self.assertIsNone(valid)
                self.assertIn(expected, errors)


                document = health.compose(
                    target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                    state=state, run_status=safe_run_status(self.TARGET),
                    freeze_receipt=None, now=self.NOW)
                self.assertEqual(document["overall_status"], "at_issue")
                self.assertFalse(document["ok"])
                self.assertTrue(all(row["status"] == "unknown"
                                    for row in document["workflow_health"].values()))
                self.assertEqual(health.validate(document), [])

    def test_state_attempt_limits_identity_time_dry_run_and_events_fail_closed(self):
        cases = []

        def duplicate_run_id(state: dict) -> None:
            duplicate = state["workflows"]["intake"]["latest_run_id"]
            state["workflows"]["macro_refresh"]["attempts"][0]["run_id"] = duplicate
            state["workflows"]["macro_refresh"]["latest_run_id"] = duplicate

        def unaware_timestamp(state: dict) -> None:
            state["workflows"]["intake"]["attempts"][0]["started_utc"] = \
                "2026-07-10T12:00:02"

        def reversed_timestamp(state: dict) -> None:
            state["workflows"]["intake"]["attempts"][0]["finished_utc"] = \
                "2026-07-10T11:59:59+00:00"

        def invalid_dry_run(state: dict) -> None:
            state["workflows"]["morning"]["attempts"][0]["dry_run"] = "false"

        def excess_macro(state: dict) -> None:
            record = state["workflows"]["macro_refresh"]
            for index, (started, finished) in enumerate((
                    ("2026-07-10T12:41:00+00:00", "2026-07-10T12:42:00+00:00"),
                    ("2026-07-10T12:43:00+00:00", "2026-07-10T12:44:00+00:00")), 1):
                row = copy.deepcopy(record["attempts"][0])
                row.update(run_id=f"fixture-macro-extra-{index}",
                           started_utc=started, finished_utc=finished)
                record["attempts"].append(row)
            record["latest_run_id"] = record["attempts"][-1]["run_id"]

        def excess_post_close(state: dict) -> None:
            first = attempt(
                "completed", "contract_satisfied", 0,
                "2026-07-10T20:30:00+00:00", "2026-07-10T20:15:00+00:00",
                workflow="post_close")
            second = copy.deepcopy(first["attempts"][0])
            second.update(run_id="fixture-post-close-extra",
                          started_utc="2026-07-10T20:31:00+00:00",
                          finished_utc="2026-07-10T20:45:00+00:00")
            first["attempts"].append(second)
            first["latest_run_id"] = second["run_id"]
            state["workflows"]["post_close"] = first

        def malformed_events(state: dict) -> None:
            state["events"] = [{
                "seq": 3, "utc": "2026-07-10T12:00:02",
                "workflow": "intake", "run_id": "fixture-intake-run",
                "event": "attempt_started", "reason": "production",
            }]

        cases.extend([
            ("workflow_set", lambda state: state["workflows"].pop("post_close"),
             "autonomy_state_workflows_invalid"),
            ("workflow_state", lambda state: state["workflows"]["intake"].update(
                state="impossible"), "autonomy_state_workflow_state_invalid:intake"),
            ("attempt_list", lambda state: state["workflows"]["intake"].update(
                attempts={}), "autonomy_state_attempts_invalid:intake"),
            ("duplicate_run_id", duplicate_run_id,
             "autonomy_state_duplicate_run_id:fixture-intake-run"),
            ("unaware_timestamp", unaware_timestamp,
             "autonomy_state_attempt_started_utc_invalid:intake:0"),
            ("reversed_timestamp", reversed_timestamp,
             "autonomy_state_attempt_finished_before_start:intake:0"),
            ("invalid_dry_run", invalid_dry_run,
             "autonomy_state_attempt_dry_run_invalid:morning:0"),
            ("macro_attempt_limit", excess_macro,
             "autonomy_state_macro_attempt_limit_exceeded"),
            ("post_close_attempt_limit", excess_post_close,
             "autonomy_state_post_close_attempt_limit_exceeded"),
            ("malformed_events", malformed_events,
             "autonomy_state_event_sequence_invalid:0"),
            ("event_shape", lambda state: state.update(events=[{"seq": 0}]),
             "autonomy_state_event_shape_invalid:0"),
        ])
        for name, mutate, expected in cases:
            with self.subTest(name=name):
                state = daily_state(self.TARGET)
                mutate(state)
                valid, errors = health._valid_state(state, self.TARGET)
                self.assertIsNone(valid)
                self.assertIn(expected, errors)

    def test_validate_rejects_resealed_workflow_health_forgery(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW)
        for name, mutate, expected in (
                ("shape", lambda doc: doc["workflow_health"]["intake"].update(
                    unexpected=True), "workflow_health_shape_invalid:intake"),
                ("state_status", lambda doc: doc["workflow_health"]["morning"].update(
                    status="healthy"), "workflow_health_state_status_mismatch:morning"),
                ("hidden_failure", lambda doc: doc.update(
                    overall_status="healthy", ok=True),
                 "workflow_health_issue_hidden_by_overall_status"),
                ("duplicate_id", lambda doc: doc["workflow_health"]["macro_refresh"].update(
                    latest_run_id=doc["workflow_health"]["intake"]["latest_run_id"]),
                 "workflow_health_duplicate_run_id:fixture-intake-run"),
                ("malformed_id", lambda doc: doc["workflow_health"]["intake"].update(
                    latest_run_id="bad run id"),
                 "workflow_health_run_id_invalid:intake"),
                ("bad_timestamp", lambda doc: doc["workflow_health"]["intake"].update(
                    finished_utc="2026-07-10T11:59:59+00:00"),
                 "workflow_health_finished_before_start:intake")):
            with self.subTest(name=name):
                forged = copy.deepcopy(original)
                mutate(forged)
                reseal(forged)
                errors = health.validate(forged)
                self.assertIn(expected, errors)
                self.assertNotIn("artifact_sha256_mismatch", errors)

    def test_foundation_safety_is_independent_and_healthy_only_with_both_proofs(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(Path(temporary), self.TARGET),
                release_gate=release_gate())
        foundation = document["foundation_safety"]
        self.assertEqual(foundation["status"], "healthy")
        self.assertFalse(foundation["production_gate_enforced"])
        self.assertTrue(foundation["broker_runtime"]["zero_preview_write_verified"])
        self.assertTrue(foundation["release_provenance"]["release_reproducible"])
        self.assertEqual(
            foundation["release_provenance"]["runtime_binding_status"], "match")
        self.assertTrue(
            foundation["release_provenance"]["runtime_authority_passed"])
        self.assertEqual(foundation["free_source_health"]["status"], "unavailable")
        self.assertEqual(foundation["free_source_alert"]["status"], "unavailable")
        self.assertEqual(document["overall_status"], "at_issue")

    def test_missing_foundation_proofs_are_unverified_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW)
        foundation = document["foundation_safety"]
        self.assertEqual(foundation["status"], "unverified")
        self.assertFalse(foundation["broker_runtime"]["zero_preview_write_verified"])
        self.assertFalse(foundation["release_provenance"]["release_reproducible"])

    def test_expected_output_drift_preserves_forensic_truth_and_runtime_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(root, self.TARGET),
                release_gate=release_gate(
                    "drift", runtime_binding="expected_output_drift", runtime_ok=True))
        foundation = document["foundation_safety"]
        release = foundation["release_provenance"]
        self.assertEqual(foundation["status"], "healthy")
        self.assertEqual(release["release_status"], "drift")
        self.assertFalse(release["release_reproducible"])
        self.assertEqual(release["runtime_binding_status"], "expected_output_drift")
        self.assertTrue(release["runtime_provenance_ok"])
        self.assertTrue(release["runtime_authority_passed"])
        self.assertEqual(health.validate(document), [])

    def test_legacy_gate_override_is_forensic_only_and_drift_remains_unsafe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = broker_sli(root, self.TARGET)
            common = dict(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW, broker_sli=broker)
            matched = health.compose(
                **common, release_gate=legacy_release_gate("match"))
            drifted = health.compose(
                **common, release_gate=legacy_release_gate("drift"))
        match_release = matched["foundation_safety"]["release_provenance"]
        drift_release = drifted["foundation_safety"]["release_provenance"]
        self.assertEqual(matched["foundation_safety"]["status"], "unverified")
        self.assertFalse(match_release["runtime_authority_passed"])
        self.assertEqual(match_release["runtime_binding_status"], "unavailable")
        self.assertEqual(drifted["foundation_safety"]["status"], "at_issue")
        self.assertFalse(drift_release["runtime_authority_passed"])
        self.assertEqual(drift_release["runtime_binding_status"], "blocked")
        self.assertEqual(health.validate(matched), [])
        self.assertEqual(health.validate(drifted), [])

    def test_historical_health_projection_validates_under_exact_match_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(root, self.TARGET), release_gate=release_gate())
        document["foundation_safety"]["release_provenance"] = {
            "status": "match", "release_reproducible": True,
            "reason": "validated_release_snapshot_current_state_comparison",
            "snapshot_id": "release-fixture", "gate_sha256": "1" * 64,
            "drift": [], "head_commit": health._current_repo_head(),
            "current_head_commit": health._current_repo_head(),
        }
        reseal(document)
        self.assertEqual(health.validate(document), [])

        document["foundation_safety"]["release_provenance"]["status"] = "drift"
        document["foundation_safety"]["release_provenance"]["release_reproducible"] = False
        document["foundation_safety"]["status"] = "at_issue"
        reseal(document)
        self.assertEqual(health.validate(document), [])

    def test_runtime_gate_unusable_stale_or_unresolvable_head_never_stays_green(self):
        unusable = release_gate()
        unusable["head_commit"] = None
        self.assertFalse(health._release_foundation(unusable)["runtime_authority_passed"])
        self.assertEqual(health._release_foundation(unusable)["status"], "invalid")

        current_gate = release_gate()
        with mock.patch.object(health, "_current_repo_head", return_value=None):
            unavailable = health._release_foundation(current_gate)
        self.assertEqual(unavailable["status"], "invalid")
        self.assertFalse(unavailable["runtime_authority_passed"])

        with mock.patch.object(health, "_current_repo_head", return_value="f" * 40):
            stale = health._release_foundation(current_gate)
        self.assertEqual(stale["status"], "invalid")
        self.assertFalse(stale["runtime_authority_passed"])

    def test_runtime_source_drift_blocks_foundation_despite_snapshot_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(root, self.TARGET),
                release_gate=release_gate(
                    "match", runtime_binding="match", runtime_ok=False,
                    reason_codes=["runtime_source_modified"]))
        foundation = document["foundation_safety"]
        release = foundation["release_provenance"]
        self.assertEqual(foundation["status"], "at_issue")
        self.assertEqual(release["release_status"], "match")
        self.assertTrue(release["release_reproducible"])
        self.assertFalse(release["runtime_authority_passed"])
        self.assertIn("runtime_source_modified", release["runtime_reason_codes"])
        self.assertEqual(health.validate(document), [])

    def test_valid_free_source_health_surfaces_aggregates_without_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_receipt = source_health.build_health(
                readiness_dir=root / "readiness", plan_root=root / "plans",
                now=self.NOW)
            self.assertEqual(source_health.validate_receipt(source_receipt), [])
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(root, self.TARGET), release_gate=release_gate(),
                free_source_receipt=source_receipt)
        surface = document["foundation_safety"]["free_source_health"]
        self.assertEqual(surface["status"], "collecting")
        self.assertEqual(surface["window"]["valid_session_count"], 0)
        self.assertEqual(surface["provider_health_counts"], {"unobserved": 9})
        self.assertEqual(surface["maximum_external_spend_usd"], "0.00")
        self.assertFalse(surface["source_authority_granted"])
        self.assertFalse(surface["buy_authority_granted"])
        self.assertFalse(surface["execution_enabled"])
        self.assertFalse(surface["production_gate_enforced"])
        self.assertEqual(document["foundation_safety"]["status"], "healthy")
        self.assertEqual(health.validate(document), [])

    def test_invalid_free_source_health_is_visible_and_cannot_grant_authority(self):
        forged = source_health.build_health(
            readiness_dir=Path("/path/that/does/not/exist"),
            plan_root=Path("/path/that/does/not/exist"), now=self.NOW)
        forged["execution_enabled"] = True
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                free_source_receipt=forged)
        surface = document["foundation_safety"]["free_source_health"]
        self.assertEqual(surface["status"], "invalid")
        self.assertFalse(surface["execution_enabled"])
        self.assertFalse(surface["source_authority_granted"])
        self.assertIn("receipt_safety_field_invalid:execution_enabled", surface["errors"])

    def test_valid_free_source_alert_surfaces_decision_not_delivery_or_authority(self):
        policy = json.loads(source_alert.CONFIG_PATH.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_receipt = source_health.build_health(
                readiness_dir=root / "readiness", plan_root=root / "plans",
                now=self.NOW)
            previous = source_alert._initial_state(self.NOW)
            alert_receipt, _ = source_alert.evaluate(
                health=source_receipt, previous=previous,
                policy=policy, now=self.NOW)
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                free_source_receipt=source_receipt,
                free_source_alert_receipt=alert_receipt)
        surface = document["foundation_safety"]["free_source_alert"]
        self.assertEqual(surface["status"], "clear")
        self.assertEqual(surface["transition"], "clear")
        self.assertFalse(surface["notification_required"])
        self.assertFalse(surface["notification_delivery_confirmed"])
        self.assertFalse(surface["production_gate_enforced"])
        self.assertFalse(surface["execution_enabled"])
        self.assertEqual(health.validate(document), [])

    def test_broker_violation_surfaces_without_granting_production_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW,
                broker_sli=broker_sli(Path(temporary), self.TARGET, "violation"),
                release_gate=release_gate())
        foundation = document["foundation_safety"]
        self.assertEqual(foundation["status"], "at_issue")
        self.assertEqual(foundation["broker_runtime"]["broker_preview_write_attempt_total"], 1)
        self.assertFalse(foundation["production_gate_enforced"])

    def test_broker_sli_cannot_hide_source_preview_attempt_after_reseal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            forged = broker_sli(root, self.TARGET, "violation")
            forged["broker_preview_write_attempt_total"] = 0
            forged["status"] = "zero_verified"
            forged["zero_preview_write_verified"] = True
            forged["sli_sha256"] = broker_audit._sha(
                {key: value for key, value in forged.items() if key != "sli_sha256"})
            self.assertEqual(broker_audit.validate_sli(forged), [])
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW, broker_sli=forged,
                release_gate=release_gate())
        broker = document["foundation_safety"]["broker_runtime"]
        self.assertEqual(broker["status"], "invalid")
        self.assertFalse(broker["zero_preview_write_verified"])
        self.assertIn(
            "sli_source_projection_mismatch:broker_preview_write_attempt_total",
            broker["errors"])

    def test_missing_power_health_is_visible_but_optional_for_library_callers(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW)
        surface = document["power_schedule_health"]
        self.assertEqual(surface["status"], "unavailable")
        self.assertFalse(surface["system_settings_changed"])
        self.assertFalse(surface["execution_enabled"])
        self.assertFalse(surface["source_authority_granted"])
        self.assertEqual(health.validate(document), [])

    def test_ready_power_preserves_health_and_late_power_forces_at_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports" / "morning" / "official.md"
            report.parent.mkdir(parents=True)
            report.write_text("fixture official research report\n")
            receipt = {
                "schema": "research_freeze_receipt/1.0",
                "target_session_date": self.TARGET,
                "frozen_utc": "2026-07-10T13:00:00+00:00",
                "report_path": str(report),
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "execution_enabled": False,
            }
            state = daily_state(self.TARGET, "completed", "research_freeze_completed")
            state["workflows"]["intake"] = attempt(
                "completed", "completed", 0,
                "2026-07-10T12:10:00+00:00",
                "2026-07-10T12:00:02+00:00", workflow="intake")
            state["workflows"]["post_open"] = attempt(
                "completed", "completed", 0,
                "2026-07-10T13:55:00+00:00",
                "2026-07-10T13:45:01+00:00", workflow="post_open")
            state["workflows"]["post_close"] = attempt(
                "completed", "completed", 0,
                "2026-07-10T20:45:00+00:00",
                "2026-07-10T20:15:01+00:00", workflow="post_close")
            sync_lifecycle_events(state)
            after_close = dt.datetime(
                2026, 7, 10, 22, 0, tzinfo=dt.timezone.utc)
            with mock.patch.object(
                    health.post_close_finality, "validate_terminal", return_value=[]), \
                    mock.patch.object(
                        health.post_open_finality, "validate_terminal", return_value=[]):
                ready = health.compose(
                    target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                    run_status=None, freeze_receipt=receipt, now=after_close,
                    power_schedule=power_receipt("04:55"))
                late = health.compose(
                    target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                    run_status=None, freeze_receipt=receipt, now=after_close,
                    power_schedule=power_receipt("05:43"))
        self.assertEqual(ready["overall_status"], "healthy")
        self.assertEqual(ready["power_schedule_health"]["status"], "ready")
        self.assertEqual(late["overall_status"], "at_issue")
        self.assertEqual(late["power_schedule_health"]["lead_minutes"], -43)
        self.assertEqual(late["power_schedule_health"]["minutes_after_required"], 48)
        self.assertTrue(late["power_schedule_health"]["manual_admin_required"])
        self.assertEqual(health.validate(ready), [])
        self.assertEqual(health.validate(late), [])

    def test_rehashed_power_authority_forgery_is_invalid_and_cannot_escape(self):
        forged = power_receipt("04:55")
        forged["execution_enabled"] = True
        forged["artifact_sha256"] = adapter_base.sha256_obj({
            key: value for key, value in forged.items()
            if key != "artifact_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=safe_run_status(self.TARGET),
                freeze_receipt=None, now=self.NOW, power_schedule=forged)
        surface = document["power_schedule_health"]
        self.assertEqual(surface["status"], "invalid")
        self.assertFalse(surface["execution_enabled"])
        self.assertIn(
            "receipt_safety_invalid:execution_enabled", surface["errors"])

    def test_candidate_invalidity_and_false_post_open_skip_are_not_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = v2_official_receipt(root, self.TARGET)
            state = daily_state(self.TARGET, "completed", "research_freeze_completed")
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=None, freeze_receipt=receipt, now=self.NOW,
                candidate_artifact_health=candidate_health(self.TARGET, "invalid"))
        surface = document["research_artifact_health"]
        self.assertEqual(surface["status"], "at_issue")
        self.assertIn("candidate_artifact_invalid", surface["reason_codes"])
        self.assertIn("post_open_skip_contradicts_official_morning",
                      surface["reason_codes"])
        self.assertEqual(surface["post_open_dependency"]["status"], "contradiction")
        self.assertIn(
            surface["post_open_dependency"]["accepted_morning_kind"],
            {"official_research_slot", "official_research_slot_receipt_valid"})
        self.assertEqual(document["overall_status"], "at_issue")
        self.assertFalse(document["ok"])
        self.assertEqual(health.validate(document), [])

    def test_v2_official_terminal_requires_full_receipt_reanchor(self):
        mutations = ("evidence", "live_source", "prose", "candidate_quote")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                receipt = v2_official_receipt(root, self.TARGET)
                terminal, errors = health._validate_freeze_receipt(
                    receipt, self.TARGET, root, now=self.NOW)
                self.assertIsNotNone(terminal, errors)
                self.assertEqual(errors, [])

                if mutation == "evidence":
                    evidence = (root / "data" / "runtime" / "research" / self.TARGET
                                / "evidence.json")
                    evidence.write_text('{"tampered":true}\n', encoding="utf-8")
                    receipt["evidence_sha256"] = hashlib.sha256(
                        evidence.read_bytes()).hexdigest()
                elif mutation == "live_source":
                    source = research_authority.rh_artifact_path(self.TARGET, root)
                    document = json.loads(source.read_text(encoding="utf-8"))
                    document["status"] = "ROBINHOOD_ACCOUNT_CONTEXT_UNAVAILABLE"
                    source.write_text(json.dumps(document), encoding="utf-8")
                    digest = hashlib.sha256(source.read_bytes()).hexdigest()
                    receipt["rh_artifact_sha256"] = digest
                    receipt["live_price_source_artifact_sha256"] = digest
                elif mutation == "prose":
                    report = Path(receipt["report_path"])
                    report.write_text(
                        report.read_text(encoding="utf-8").replace(
                            "Execution: DISABLED", "Execution: ENABLED"),
                        encoding="utf-8")
                    receipt["report_sha256"] = hashlib.sha256(
                        report.read_bytes()).hexdigest()
                else:
                    receipt["candidate_quote_summary"]["status"] = "ready"

                canonical = (root / "data" / "runtime" / "research" / self.TARGET
                             / "freeze_receipt.json")
                canonical.write_text(
                    json.dumps(receipt, indent=1, sort_keys=True), encoding="utf-8")
                terminal, errors = health._validate_freeze_receipt(
                    receipt, self.TARGET, root, now=self.NOW)
                self.assertIsNone(terminal)
                self.assertTrue(any("freeze_official_reanchor_failed" in error
                                    for error in errors), errors)

    def test_v2_official_receipt_swap_during_reanchor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = v2_official_receipt(root, self.TARGET)
            canonical = (root / "data" / "runtime" / "research" / self.TARGET
                         / "freeze_receipt.json")

            def swap_receipt(*_args, **_kwargs):
                swapped = copy.deepcopy(receipt)
                swapped["buy_block_reasons"] = ["swapped_during_probe"]
                canonical.write_text(
                    json.dumps(swapped, indent=1, sort_keys=True), encoding="utf-8")
                return {
                    "completed": True, "kind": "research_freeze_receipt",
                    "official": True, "reason": "fixture_probe_valid",
                }

            with mock.patch.object(
                    health.freeze_completion_probe, "probe", side_effect=swap_receipt):
                terminal, errors = health._validate_freeze_receipt(
                    receipt, self.TARGET, root, now=self.NOW)
            self.assertIsNone(terminal)
            self.assertIn("freeze_canonical_receipt_changed_during_reanchor", errors)

    def test_healthy_candidate_surface_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=None, freeze_receipt=None,
                now=self.NOW,
                candidate_artifact_health=candidate_health(self.TARGET, "healthy"))
        surface = document["research_artifact_health"]
        self.assertEqual(surface["status"], "healthy")
        self.assertEqual(surface["reason_codes"], [])
        self.assertFalse(surface["production_gate_enforced"])
        self.assertFalse(surface["execution_enabled"])
        self.assertEqual(health.validate(document), [])

    def test_candidate_quote_ready_and_partial_are_aggregate_non_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=None, freeze_receipt=None,
                now=self.NOW, candidate_quote_health=candidate_quote_health(self.TARGET))
            partial = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=None, freeze_receipt=None,
                now=self.NOW,
                candidate_quote_health=candidate_quote_health(self.TARGET, "partial"))
        ready_surface = ready["research_artifact_health"]["candidate_quote_coverage"]
        self.assertEqual(ready_surface["status"], "healthy")
        self.assertEqual(ready_surface["fresh_count"], 2)
        self.assertNotIn("quotes", ready_surface)
        self.assertFalse(ready_surface["buy_authority_granted"])
        self.assertFalse(ready_surface["execution_enabled"])
        partial_surface = partial["research_artifact_health"]["candidate_quote_coverage"]
        self.assertEqual(partial_surface["status"], "degraded")
        self.assertEqual(partial_surface["fresh_count"], 1)
        self.assertEqual(partial_surface["unavailable_count"], 1)
        self.assertEqual(health.validate(ready), [])
        self.assertEqual(health.validate(partial), [])

    def test_candidate_quote_invalid_or_tampered_receipt_is_at_issue(self):
        invalid = candidate_quote_health(self.TARGET, "at_issue")
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=None, freeze_receipt=None,
                now=self.NOW, candidate_quote_health=invalid)
        surface = document["research_artifact_health"]
        self.assertEqual(surface["status"], "at_issue")
        self.assertIn("candidate_quote_artifact_invalid", surface["reason_codes"])
        self.assertEqual(document["overall_status"], "at_issue")
        self.assertEqual(health.validate(document), [])

        tampered = candidate_quote_health(self.TARGET)
        tampered["execution_enabled"] = True
        tampered = quote_hydrator._seal(tampered)
        with tempfile.TemporaryDirectory() as temporary:
            document = health.compose(
                target=self.TARGET, root=Path(temporary), scheduler=scheduler(),
                state=daily_state(self.TARGET), run_status=None, freeze_receipt=None,
                now=self.NOW, candidate_quote_health=tampered)
        self.assertIn(
            "candidate_quote_health_receipt_invalid",
            document["research_artifact_health"]["reason_codes"])
        self.assertEqual(health.validate(document), [])

    def _maintenance_state(self):
        state = daily_state(self.TARGET)
        state["workflows"]["post_close"] = attempt(
            "skipped", "no_official_morning_artifact", 0,
            "2026-07-10T21:00:00+00:00",
            "2026-07-10T20:15:00+00:00", workflow="post_close")
        sync_lifecycle_events(state)
        return state

    def _compose_maintenance(self, root, receipt_errors, effective="2026-01-01"):
        with mock.patch.object(
                health, "MAINTENANCE_RECEIPT_EFFECTIVE_DATE", effective), \
                mock.patch.object(
                    health.post_close_maintenance_receipt, "validate_receipt",
                    return_value=receipt_errors):
            return health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(),
                state=self._maintenance_state(),
                run_status=safe_run_status(self.TARGET), freeze_receipt=None,
                now=dt.datetime(2026, 7, 10, 22, 0, tzinfo=dt.timezone.utc))

    def test_post_close_expected_skip_requires_validated_maintenance_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = self._compose_maintenance(Path(temporary), [])
        post_close = document["workflow_health"]["post_close"]
        self.assertEqual(post_close["status"], "skipped_expected")
        self.assertEqual(post_close["reason"],
                         "post_close_maintenance_receipt_validated")
        self.assertEqual(document["service_slo_health"]["post_close"]["terminal_status"],
                         "met")
        self.assertEqual(health.validate(document), [])

    def test_post_close_bare_expected_skip_is_at_issue_not_slo_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = self._compose_maintenance(Path(temporary), ["receipt_missing"])
        post_close = document["workflow_health"]["post_close"]
        self.assertEqual(post_close["status"], "at_issue")
        self.assertEqual(post_close["reason"],
                         "post_close_maintenance_receipt_invalid")
        self.assertNotEqual(document["service_slo_health"]["post_close"]["terminal_status"],
                            "met")
        self.assertEqual(health.validate(document), [])

    def test_post_close_skips_before_the_effective_date_are_not_retroactively_at_issue(self):
        """Days that settled under the old one-mode contract can never acquire a receipt.

        Demanding one retroactively would mark every already-terminal post_close skip
        `at_issue` forever. The gate is date-scoped, so a target BEFORE the effective date
        stays the plain expected skip even when receipt validation would fail outright.
        """
        with tempfile.TemporaryDirectory() as temporary:
            document = self._compose_maintenance(
                Path(temporary), ["receipt_missing"], effective="2026-08-18")
        post_close = document["workflow_health"]["post_close"]
        self.assertEqual(post_close["status"], "skipped_expected")
        self.assertNotEqual(post_close["reason"],
                            "post_close_maintenance_receipt_invalid")
        self.assertEqual(health.validate(document), [])
        self.assertLess(self.TARGET, "2026-08-18")

class TestMacroReceiptAndHistoryBinding(unittest.TestCase):
    TARGET = "2026-07-10"

    @staticmethod
    def _stage_receipt(root: Path) -> dict:
        source = {
            "readiness_path": None, "readiness_file_sha256": None,
            "readiness_receipt_sha256": None, "calendar_manifest_path": None,
            "calendar_manifest_file_sha256": None, "calendar_batch_id": None,
            "baseline_manifest_path": None, "baseline_manifest_file_sha256": None,
            "baseline_batch_id": None, "refresh_manifest_path": None,
            "refresh_manifest_file_sha256": None, "refresh_batch_id": None,
        }
        receipt = macro_actual_refresh._build_receipt(
            target=TestMacroReceiptAndHistoryBinding.TARGET,
            now=dt.datetime(2026, 7, 10, 12, 36, tzinfo=dt.timezone.utc),
            status="no_due_events", source=source, events=[], reason_codes=[])
        path = (root / "data" / "runtime" / "macro_actual_refresh" /
                f"{TestMacroReceiptAndHistoryBinding.TARGET}.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt

    def _bound_state(self, root: Path) -> dict:
        receipt = self._stage_receipt(root)
        state = daily_state(self.TARGET)
        macro = state["workflows"]["macro_refresh"]["attempts"][0]
        macro["postconditions"] = {
            "task_status": "success",
            "task_detail": "macro_refresh_no_due_events",
            "task_state_fresh": True,
            "activation_authority": False,
            "execution_enabled": False,
            "status": "pass",
            "reason": "macro_refresh_no_due_events",
            "artifact": f"data/runtime/macro_actual_refresh/{self.TARGET}.json",
            "artifact_sha256": receipt["artifact_sha256"],
        }
        return state

    def test_completed_macro_requires_canonical_receipt_and_exact_postcondition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._bound_state(root)
            plane, errors = health._workflow_plane(state, self.TARGET, None, root)
            self.assertEqual(errors, [])
            self.assertEqual(plane["macro_refresh"]["status"], "healthy")
            receipt_path = (root / "data" / "runtime" / "macro_actual_refresh" /
                            f"{self.TARGET}.json")
            receipt_path.unlink()
            plane, _ = health._workflow_plane(state, self.TARGET, None, root)
            self.assertEqual(plane["macro_refresh"]["status"], "at_issue")
            self.assertEqual(plane["macro_refresh"]["reason"],
                             "macro_refresh_receipt_missing_or_symlink")

    def test_macro_at_issue_row_passes_document_self_validation(self):
        # Regression 2026-07-17: a real macro binding failure must degrade the macro row while
        # the composed document still passes validate(); previously ANY macro at_issue failed
        # the whole surface (workflow_health_at_issue_scope_invalid) and the watchdog exited
        # rc=2 for the rest of the day.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._bound_state(root)
            receipt_path = (root / "data" / "runtime" / "macro_actual_refresh" /
                            f"{self.TARGET}.json")
            receipt_path.unlink()
            document = health.compose(
                target=self.TARGET, root=root, scheduler=scheduler(), state=state,
                run_status=safe_run_status(self.TARGET), freeze_receipt=None,
                now=dt.datetime(2026, 7, 10, 16, 30, tzinfo=dt.timezone.utc))
        macro = document["workflow_health"]["macro_refresh"]
        self.assertEqual(macro["status"], "at_issue")
        self.assertEqual(macro["reason"], "macro_refresh_receipt_missing_or_symlink")
        self.assertEqual(document["overall_status"], "at_issue")
        self.assertFalse(document["ok"])
        self.assertEqual(health.validate(document), [])

    def test_arbitrary_second_macro_attempt_and_missing_events_fail_state(self):
        state = daily_state(self.TARGET)
        macro_record = state["workflows"]["macro_refresh"]
        second = copy.deepcopy(macro_record["attempts"][0])
        second.update({
            "run_id": "fixture-macro-second-run",
            "started_utc": "2026-07-10T12:41:00+00:00",
            "finished_utc": "2026-07-10T12:42:00+00:00",
        })
        macro_record["attempts"].append(second)
        macro_record["latest_run_id"] = second["run_id"]
        for event, timestamp, reason in (
                ("attempt_started", second["started_utc"], "production"),
                ("runner_started", second["started_utc"], "allowlisted_runner"),
                ("attempt_finished", second["finished_utc"], second["outcome"])):
            state["events"].append({
                "seq": len(state["events"]), "utc": timestamp,
                "workflow": "macro_refresh", "run_id": second["run_id"],
                "event": event, "reason": reason,
            })
        valid, errors = health._valid_state(state, self.TARGET)
        self.assertIsNone(valid)
        self.assertIn("autonomy_state_macro_handoff_invalid", errors)

        missing_events = daily_state(self.TARGET)
        missing_events["events"] = []
        valid, errors = health._valid_state(missing_events, self.TARGET)
        self.assertIsNone(valid)
        self.assertTrue(any("_event_invalid" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
