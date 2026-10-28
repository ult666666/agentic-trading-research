#!/usr/bin/env python3
"""Focused tests for the autonomous research supervisor.

All execution fixtures live in temporary directories. No real LaunchAgent, broker tool, model,
report, ledger, runtime state, or production runner is invoked.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_ROOT = Path.home() / "trading-system"  # = launchd_contract.CANONICAL_INSTALL_ROOT
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import autonomy_orchestrator as ao  # noqa: E402
import deterministic_no_live_terminal as deterministic_terminal  # noqa: E402
import freeze_candidates  # noqa: E402
import legacy_morning_receipt  # noqa: E402
from test_legacy_morning_receipt import stage_authoritative_inputs  # noqa: E402
import test_free_ensemble as free_ensemble_fixture  # noqa: E402
import data_lake_intake  # noqa: E402
import tzutil  # noqa: E402

POST_CLOSE_TEST_RUN_ID = "postclose-20260709T201500Z-0123456789ab"


def production_config() -> dict:
    return json.loads((ROOT / "config" / "autonomy_workflows.json").read_text())


def write_config(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def write_runner_tree(root: Path, *, broker_rc: int = 0, integrity_rc: int = 0,
                      trading: bool = True, runner_rc: int = 0) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    market_rc = 0 if trading else 1
    market_payload = json.dumps({
        "is_trading_day": trading,
        "reason": "regular_trading_day" if trading else "market_holiday",
        "session_close_et": "16:00",
    })
    (scripts / "validate_market_day.py").write_text(
        "#!/usr/bin/env python3\n"
        f"print({market_payload!r})\n"
        f"raise SystemExit({market_rc})\n")
    (scripts / "validate_broker_permissions.py").write_text(
        "#!/usr/bin/env python3\n"
        f"raise SystemExit({broker_rc})\n")
    manifest_payload = json.dumps({"check": "PASS" if integrity_rc == 0 else "FAIL"})
    (scripts / "build_manifest.py").write_text(
        "#!/usr/bin/env python3\n"
        f"print({manifest_payload!r})\n"
        f"raise SystemExit({integrity_rc})\n")
    recorder = scripts / "record_test_state.py"
    recorder.write_text(
        "import json, sys\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "root, task = Path(sys.argv[1]), sys.argv[2]\n"
        "path = root / 'data' / 'system_state.json'\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "path.write_text(json.dumps({'runs': {task: {\n"
        "    'status': 'dry_run_ok', 'detail': 'pipeline_only',\n"
        "    'timestamp_utc': datetime.now(timezone.utc).isoformat(),\n"
        "}}}))\n")
    marker = root / "runner_invoked.txt"
    for workflow, rel in ao.ALLOWED_RUNNERS.items():
        runner = (
            "#!/bin/bash\n"
            "set -eu\n"
            "[ \"${TRADING_AUTONOMY_CHILD:-}\" = 1 ]\n"
            f"printf '%s' \"${{TRADING_AUTONOMY_WORKFLOW:-}}\" > {marker!s}\n"
            f"printf '%s' \"${{TRADING_POST_CLOSE_MODE:-}}\" > {root!s}/post_close_mode.txt\n"
            f"{sys.executable} {recorder!s} {root!s} {ao.ALLOWED_TASK_KEYS[workflow]}\n"
            f"exit {runner_rc}\n"
        )
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(runner)
    config_path = root / "config" / "autonomy_workflows.json"
    write_config(config_path, production_config())
    return config_path


def seed_task_state(root: Path, task: str, status: str = "success",
                    timestamp: str | None = None) -> None:
    path = root / "data" / "system_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({
        "runs": {task: {"status": status, "detail": "test", "timestamp_utc": timestamp}}
    }))


def seed_post_close_contract_receipt(root: Path, report: Path,
                                     target: str = "2026-07-09", ok: bool = True,
                                     run_id: str = POST_CLOSE_TEST_RUN_ID) -> Path:
    receipt = root / "data" / "runtime" / "health" / f"{target}_post_close_report_contract.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema": "post_close_report_contract/1.1",
        "ok": ok,
        "target_session_date": target,
        "report_path": str(report.relative_to(root)),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "post_close_status_at_report_cutoff": {
            "run_mode": "supervised", "run_id": run_id,
        },
        "safety": {"activation_authority": False, "execution_enabled": False},
    }))
    return receipt


def seed_health_manifest(root: Path) -> None:
    relative = "scripts/autonomy_service_health.py"
    script = root / relative
    manifest = root / "data" / "MANIFEST.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "critical_missing": [],
        "critical_sha256": {
            relative: hashlib.sha256(script.read_bytes()).hexdigest(),
        },
    }))


def seed_watchdog_no_dispatch_reservation(
        state_path: Path, config_sha: str, *, target: str = "2026-07-09",
        market_day_reason: str = "regular_trading_day") -> str:
    run_id = "macro-20260709T124700Z-abcdef123456"
    started = datetime(2026, 7, 9, 5, 47, tzinfo=tzutil.PT)
    finished = datetime(2026, 7, 9, 5, 48, 30, tzinfo=tzutil.PT)
    ao._start_attempt(
        state_path, target, "macro_refresh", run_id, config_sha, False, started,
        only_if_never_started=True)
    preflight = {
        "continuity_recovery": True,
        "recovery_trigger": "watchdog",
        "recovery_only_if_never_started": True,
        "recovery_child_timeout_seconds": ao.MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS,
        "trading_day": "pass",
        "market_day_reason": market_day_reason,
        "integrity_manifest": "pass",
        "manifest_validator_returncode": 0,
        "manifest_check": "PASS",
        "broker_permissions": "pass",
        "broker_validator_returncode": 0,
        "strict_read_only": True,
        "dependency_gate": "pass",
        "dependency_reason": "data_readiness_receipt_valid",
        "phase_gate": "pass",
        "phase_reason": "watchdog_missing_macro_recovery_window",
        "recovery_dispatch_gate": "skip",
        "recovery_dispatch_reason": "after_watchdog_macro_recovery_window",
    }
    ao._update_attempt(
        state_path, target, "macro_refresh", run_id, config_sha, {
            "state": "skipped",
            "outcome": "after_watchdog_macro_recovery_window",
            "finished_utc": tzutil.to_utc(finished).isoformat(),
            "exit_code": 0,
            "preflight": preflight,
        }, event="attempt_finished", reason="after_watchdog_macro_recovery_window",
        now=finished)
    return run_id


def seed_macro_receipt(root: Path, *, target: str = "2026-07-09",
                       status: str = "no_due_events",
                       generated: datetime | None = None) -> Path:
    source = {
        "readiness_path": None, "readiness_file_sha256": None,
        "readiness_receipt_sha256": None, "calendar_manifest_path": None,
        "calendar_manifest_file_sha256": None, "calendar_batch_id": None,
        "baseline_manifest_path": None, "baseline_manifest_file_sha256": None,
        "baseline_batch_id": None, "refresh_manifest_path": None,
        "refresh_manifest_file_sha256": None, "refresh_batch_id": None,
    }
    receipt = ao.macro_actual_refresh._build_receipt(
        target=target,
        now=generated or datetime(2026, 7, 9, 12, 40, tzinfo=timezone.utc),
        status=status, source=source, events=[], reason_codes=[])
    path = root / "data" / "runtime" / "macro_actual_refresh" / f"{target}.json"
    ao.macro_actual_refresh._atomic_write(path, receipt)
    return path


def seed_readiness_receipt(root: Path, *, target: str = "2026-07-09") -> Path:
    receipt = {
        "schema": data_lake_intake.SCHEMA,
        "target_session_date": target,
        "status": "degraded",
        "research_ready": False,
        **data_lake_intake.SAFETY,
    }
    receipt["receipt_sha256"] = data_lake_intake.base.sha256_obj(receipt)
    path = root / "data" / "runtime" / "data_lake" / "readiness" / f"{target}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def seed_v2_official(
        root: Path, target: str = free_ensemble_fixture.T) -> Path:
    """Freeze one real, fully bound zero-catalyst governance-v2 fixture.

    A hand-written report/receipt pair is deliberately insufficient: the production resolver
    revalidates the receipt together with its evidence artifact and live-source snapshot. Reuse
    the freeze-layer full fixture so a passing autonomy test exercises that complete binding.
    """
    if target != free_ensemble_fixture.T:
        raise ValueError(f"v2_fixture_target_unsupported:{target}")
    draft = free_ensemble_fixture.full_fixture(root)
    result = free_ensemble_fixture.freeze(
        root, root / "reports", draft, "watch")
    if result.get("official") is not True \
            or result.get("classification") != "OFFICIAL_RESEARCH_WATCH":
        raise AssertionError(f"v2_fixture_did_not_freeze:{result}")
    report = Path(result["path"])
    if not report.is_file() or not Path(result["receipt"]).is_file():
        raise AssertionError("v2_fixture_artifacts_missing")
    return report


class TestConfigContract(unittest.TestCase):
    def test_shipped_config_validates(self):
        doc, errors = ao.validate_config()
        self.assertIsInstance(doc, dict)
        self.assertEqual(errors, [])

    def _mutate(self, fn) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = write_runner_tree(root)
            doc = json.loads(config_path.read_text())
            fn(doc)
            write_config(config_path, doc)
            _loaded, errors = ao.validate_config(config_path, root)
            return errors

    def test_every_safety_grant_fails_closed(self):
        mutations = {
            "execution_enabled": True,
            "research_only": False,
            "broker_access": "write",
            "integrity_manifest_required": False,
            "subprocess_shell": True,
            "supervisor_max_attempts": 2,
            "supervisor_retry_enabled": True,
            "allow_dynamic_commands": True,
            "allow_strategy_activation": True,
            "allow_risk_limit_changes": True,
        }
        for key, value in mutations.items():
            errors = self._mutate(lambda d, k=key, v=value: d["safety"].__setitem__(k, v))
            self.assertIn(f"safety_flag_invalid:{key}", errors, key)

    def test_version_and_unknown_fields_fail_closed(self):
        self.assertIn("config_version_invalid", self._mutate(
            lambda d: d.__setitem__("version", "2.0.0")))
        self.assertIn("config_top_level_keys_invalid", self._mutate(
            lambda d: d.__setitem__("future_authority", True)))
        self.assertIn("safety_keys_invalid", self._mutate(
            lambda d: d["safety"].__setitem__("future_authority", True)))
        self.assertIn("workflow_keys_invalid:morning", self._mutate(
            lambda d: d["workflows"]["morning"].__setitem__("command", "/bin/echo")))

    def test_malformed_nested_objects_return_errors_without_exception(self):
        self.assertIn("safety_object_invalid", self._mutate(
            lambda d: d.__setitem__("safety", "invalid")))
        self.assertIn("workflow_object_invalid:morning", self._mutate(
            lambda d: d["workflows"].__setitem__("morning", "invalid")))
        self.assertIn("dependencies_invalid:post_open", self._mutate(
            lambda d: d["workflows"]["post_open"].__setitem__("dependencies", None)))

    def test_runner_schedule_label_phase_and_contract_are_locked(self):
        cases = [
            (lambda d: d["workflows"]["morning"].__setitem__("runner", "/bin/echo"),
             "runner_not_allowlisted:morning"),
            (lambda d: d["workflows"]["morning"].__setitem__("scheduled_pt", "06:00"),
             "schedule_invalid:morning"),
            (lambda d: d["workflows"]["post_open"].__setitem__("launchd_label", "x"),
             "launchd_label_invalid:post_open"),
            (lambda d: d["workflows"]["post_close"].__setitem__(
                "phase_gate", "delegate_to_runner"), "phase_gate_invalid:post_close"),
            (lambda d: d["workflows"]["post_open"].__setitem__(
                "artifact_contract", "post_close_report"),
             "artifact_contract_invalid:post_open"),
            (lambda d: d["workflows"]["post_open"].__setitem__("dependencies", []),
             "dependencies_contract_invalid:post_open"),
        ]
        for mutate, expected in cases:
            self.assertIn(expected, self._mutate(mutate), expected)

    def test_dependency_cycle_detected(self):
        def mutate(doc):
            doc["workflows"]["morning"]["dependencies"] = ["post_close"]
            doc["workflows"]["post_close"]["dependencies"] = ["morning"]
        self.assertIn("dependency_cycle", self._mutate(mutate))

    def test_unreadable_config_fails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("not-json")
            doc, errors = ao.validate_config(path, Path(td))
            self.assertIsNone(doc)
            self.assertEqual(errors, ["config_unreadable"])


class TestStateMachine(unittest.TestCase):
    def test_new_state_is_research_only(self):
        state = ao._initial_state("2026-07-09", "abc")
        self.assertTrue(state["research_only"])
        self.assertFalse(state["execution_enabled"])
        self.assertFalse(state["activation_authority"])
        self.assertEqual(set(state["workflows"]), set(ao.WORKFLOWS))

    def test_corrupt_state_blocks_without_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-07-09.json"
            path.write_text("broken")
            before = path.read_bytes()
            with self.assertRaisesRegex(ao.ControlError, "state_unreadable"):
                ao._load_state(path, "2026-07-09", "abc")
            self.assertEqual(path.read_bytes(), before)

    def test_live_owner_blocks_second_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-07-09.json"
            now = datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT)
            ao._start_attempt(path, "2026-07-09", "morning", "morning-one",
                              "cfg", False, now)
            with self.assertRaisesRegex(ao.ControlError, "workflow_already_running"):
                ao._start_attempt(path, "2026-07-09", "morning", "morning-two",
                                  "cfg", False, now)

    def test_dead_owner_is_recovered_and_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-07-09.json"
            doc = ao._initial_state("2026-07-09", "cfg")
            prior = {"run_id": "morning-old", "state": "running", "outcome": None,
                     "owner_pid": 99999999, "started_utc": "2026-07-09T12:00:00+00:00"}
            doc["workflows"]["morning"].update(
                {"state": "running", "latest_run_id": "morning-old", "attempts": [prior]})
            ao._atomic_write_json(path, doc)
            now = datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT)
            out, _ = ao._start_attempt(path, "2026-07-09", "morning", "morning-new",
                                       "cfg", False, now)
            attempts = out["workflows"]["morning"]["attempts"]
            self.assertEqual(attempts[0]["outcome"], "stale_owner_recovered")
            self.assertEqual(attempts[0]["state"], "failed")
            self.assertEqual(attempts[1]["run_id"], "morning-new")

    def test_config_revision_is_evented_and_bound_to_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-07-09.json"
            ao._atomic_write_json(path, ao._initial_state("2026-07-09", "old"))
            now = datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT)
            out, attempt = ao._start_attempt(path, "2026-07-09", "morning",
                                             "morning-new", "new", False, now)
            self.assertEqual(out["config_sha256"], "new")
            self.assertEqual(attempt["config_sha256"], "new")
            self.assertIn("config_revision_changed", [e["event"] for e in out["events"]])

    def test_missing_run_recovery_refuses_any_prior_attempt_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-07-09.json"
            doc = ao._initial_state("2026-07-09", "cfg")
            prior = {
                "run_id": "macro-prior", "state": "failed", "outcome": "runner_exit_1",
                "owner_pid": 99999999, "started_utc": "2026-07-09T12:35:00+00:00",
                "finished_utc": "2026-07-09T12:36:00+00:00", "exit_code": 1,
            }
            doc["workflows"]["macro_refresh"].update({
                "state": "failed", "latest_run_id": "macro-prior", "attempts": [prior]})
            ao._atomic_write_json(path, doc)
            before = path.read_bytes()
            with self.assertRaisesRegex(ao.ControlError, "workflow_already_attempted"):
                ao._start_attempt(
                    path, "2026-07-09", "macro_refresh", "macro-recovery", "cfg",
                    False, datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT),
                    only_if_never_started=True)
            self.assertEqual(path.read_bytes(), before)

    def test_ordinary_macro_is_one_shot_across_dead_or_inconsistent_history(self):
        target = "2026-07-09"
        now = datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT)
        cases = {
            "dead_owner_with_child": ({
                "state": "running", "latest_run_id": "macro-prior",
                "attempts": [{
                    "run_id": "macro-prior", "state": "running", "outcome": None,
                    "owner_pid": 99999999, "child_pid": 424242,
                    "started_utc": "2026-07-09T12:35:00+00:00",
                    "finished_utc": None, "exit_code": None,
                }],
            }, 75, "workflow_already_running"),
            "completed_attempt_top_pending": ({
                "state": "pending", "latest_run_id": "macro-prior",
                "attempts": [{
                    "run_id": "macro-prior", "state": "completed",
                    "outcome": "contract_satisfied", "owner_pid": 99999999,
                    "child_pid": 424242,
                    "started_utc": "2026-07-09T12:35:00+00:00",
                    "finished_utc": "2026-07-09T12:36:00+00:00", "exit_code": 0,
                }],
            }, 2, "workflow_prior_state_ambiguous"),
            "empty_attempts_nonpending": ({
                "state": "failed", "latest_run_id": None, "attempts": [],
            }, 2, "workflow_prior_state_ambiguous"),
            "latest_id_mismatch": ({
                "state": "completed", "latest_run_id": "macro-other",
                "attempts": [{
                    "run_id": "macro-prior", "state": "completed",
                    "outcome": "contract_satisfied", "owner_pid": 99999999,
                    "child_pid": 424242,
                    "started_utc": "2026-07-09T12:35:00+00:00",
                    "finished_utc": "2026-07-09T12:36:00+00:00", "exit_code": 0,
                }],
            }, 2, "workflow_prior_state_ambiguous"),
        }
        for label, (workflow_state, expected_rc, expected_error) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                config = write_runner_tree(root)
                config_sha = ao._sha256_file(config)
                self.assertIsNotNone(config_sha)
                state_root = root / "runtime" / "autonomy"
                state_path = state_root / f"{target}.json"
                document = ao._initial_state(target, config_sha)
                document["workflows"]["macro_refresh"] = copy.deepcopy(workflow_state)
                ao._atomic_write_json(state_path, document)
                before = state_path.read_bytes()

                result, rc = ao.run_workflow(
                    "macro_refresh", target=target, config_path=config,
                    state_root=state_root, root=root, dry_run=True, now=now)

                self.assertEqual(rc, expected_rc, result)
                self.assertEqual(result["error"], expected_error)
                self.assertFalse(result["runner_invoked"])
                self.assertFalse((root / "runner_invoked.txt").exists())
                self.assertEqual(state_path.read_bytes(), before)

    def test_watchdog_no_dispatch_handoff_predicate_is_exact_and_tamper_closed(self):
        target = "2026-07-09"
        now = datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{target}.json"
            run_id = seed_watchdog_no_dispatch_reservation(path, "a" * 64, target=target)
            base = json.loads(path.read_text())
            self.assertEqual(
                ao._watchdog_no_dispatch_handoff_run_id(
                    base, target, now, "a" * 64), run_id)

            early_path = Path(td) / f"early-{target}.json"
            early_run = seed_watchdog_no_dispatch_reservation(
                early_path, "a" * 64, target=target,
                market_day_reason="early_close_trading_day")
            early = json.loads(early_path.read_text())
            self.assertEqual(
                ao._watchdog_no_dispatch_handoff_run_id(
                    early, target, now, "a" * 64), early_run)

            cases = {}
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["child_pid"] = 123
            cases["child_was_dispatched"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["dry_run"] = True
            cases["prior_was_dry_run"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["postconditions"] = {}
            cases["postconditions_present"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["outcome"] = \
                "before_watchdog_macro_recovery_window"
            cases["wrong_skip_reason"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["preflight"][
                "dependency_gate"] = "fail"
            cases["dependency_blocked"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["preflight"][
                "recovery_trigger"] = "morning"
            cases["ordinary_or_wrong_trigger"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["preflight"][
                "recovery_dispatch_gate"] = "pass"
            cases["child_dispatch_was_authorized"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["preflight"][
                "extra"] = "ambiguous"
            cases["extra_preflight_metadata"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"].append(copy.deepcopy(
                changed["workflows"]["macro_refresh"]["attempts"][0]))
            cases["ambiguous_attempt_history"] = changed
            changed = copy.deepcopy(base)
            changed["events"].append({
                "seq": 2, "utc": changed["events"][0]["utc"],
                "workflow": "macro_refresh", "run_id": run_id,
                "event": "runner_started", "reason": "allowlisted_runner",
            })
            cases["runner_event_present"] = changed
            changed = copy.deepcopy(base)
            future = tzutil.to_utc(datetime(
                2026, 7, 9, 5, 51, tzinfo=tzutil.PT)).isoformat()
            changed["workflows"]["macro_refresh"]["attempts"][0]["finished_utc"] = future
            changed["events"][1]["utc"] = future
            cases["future_terminal"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0]["unexpected"] = True
            cases["attempt_shape_ambiguous"] = changed
            changed = copy.deepcopy(base)
            changed["workflows"]["macro_refresh"]["attempts"][0][
                "config_sha256"] = "b" * 64
            cases["attempt_config_drift"] = changed
            changed = copy.deepcopy(base)
            changed["config_sha256"] = "b" * 64
            cases["state_config_drift"] = changed

            for label, document in cases.items():
                with self.subTest(label=label):
                    ao._atomic_write_json(path, document)
                    before = path.read_bytes()
                    self.assertIsNone(
                        ao._watchdog_no_dispatch_handoff_run_id(
                            document, target, now, "a" * 64))
                    with self.assertRaisesRegex(
                            ao.ControlError, "workflow_already_attempted"):
                        ao._start_attempt(
                            path, target, "macro_refresh",
                            "macro-20260709T125000Z-fedcba654321", "a" * 64,
                            False, now, only_if_never_started=True,
                            allow_watchdog_no_dispatch_handoff=True)
                    self.assertEqual(path.read_bytes(), before)

            ao._atomic_write_json(path, base)
            before = path.read_bytes()
            self.assertIsNone(ao._watchdog_no_dispatch_handoff_run_id(
                base, target, now, "b" * 64))
            with self.assertRaisesRegex(
                    ao.ControlError, "workflow_already_attempted"):
                ao._start_attempt(
                    path, target, "macro_refresh",
                    "macro-20260709T125000Z-fedcba654321", "b" * 64,
                    False, now, only_if_never_started=True,
                    allow_watchdog_no_dispatch_handoff=True)
            self.assertEqual(path.read_bytes(), before)

    def test_post_close_is_one_shot_and_ambiguous_history_is_write_free(self):
        target = "2026-07-09"
        now = datetime(2026, 7, 9, 13, 15, tzinfo=tzutil.PT)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{target}.json"
            ao._start_attempt(
                path, target, "post_close", POST_CLOSE_TEST_RUN_ID,
                "a" * 64, False, now)
            running = path.read_bytes()
            with self.assertRaisesRegex(ao.ControlError, "workflow_already_running"):
                ao._start_attempt(
                    path, target, "post_close",
                    "postclose-20260709T201501Z-deadbeefdead",
                    "a" * 64, False, now)
            self.assertEqual(path.read_bytes(), running)

            ao._update_attempt(
                path, target, "post_close", POST_CLOSE_TEST_RUN_ID, "a" * 64, {
                    "state": "completed", "outcome": "contract_satisfied",
                    "finished_utc": tzutil.to_utc(now).isoformat(), "exit_code": 0,
                }, event="attempt_finished", reason="contract_satisfied", now=now)
            terminal = path.read_bytes()
            with self.assertRaisesRegex(ao.ControlError, "workflow_already_terminal"):
                ao._start_attempt(
                    path, target, "post_close",
                    "postclose-20260709T201502Z-feedfacecafe",
                    "a" * 64, False, now)
            self.assertEqual(path.read_bytes(), terminal)

            document = json.loads(path.read_text())
            document["workflows"]["post_close"]["latest_run_id"] = "different-run"
            ao._atomic_write_json(path, document)
            ambiguous = path.read_bytes()
            with self.assertRaisesRegex(
                    ao.ControlError, "post_close_prior_state_ambiguous"):
                ao._start_attempt(
                    path, target, "post_close",
                    "postclose-20260709T201503Z-012301230123",
                    "a" * 64, False, now)
            self.assertEqual(path.read_bytes(), ambiguous)

class TestDeterministicGates(unittest.TestCase):
    MARKET = {"session_close_et": "16:00"}

    def _dt(self, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime(2026, 7, 9, hh, mm, tzinfo=tzutil.PT)

    def test_post_open_window(self):
        self.assertEqual(ao._phase_gate("post_open", "2026-07-09", self._dt("06:44"),
                                        self.MARKET, False)[0], "skip")
        self.assertEqual(ao._phase_gate("post_open", "2026-07-09", self._dt("06:45"),
                                        self.MARKET, False), ("pass", "post_open_window"))
        self.assertEqual(ao._phase_gate("post_open", "2026-07-09", self._dt("13:00"),
                                        self.MARKET, False)[1], "after_session_close")

    def test_intake_window(self):
        self.assertEqual(ao._phase_gate("intake", "2026-07-09", self._dt("04:44"),
                                        self.MARKET, False)[1], "before_intake_window")
        self.assertEqual(ao._phase_gate("intake", "2026-07-09", self._dt("05:00"),
                                        self.MARKET, False),
                         ("pass", "pre_market_intake_window"))
        self.assertEqual(ao._phase_gate("intake", "2026-07-09", self._dt("05:31"),
                                        self.MARKET, False)[1], "after_intake_window")

    def test_macro_recovery_windows_are_fixed_and_target_bound(self):
        self.assertEqual(
            ao._macro_recovery_gate("watchdog", "2026-07-09", self._dt("05:45")),
            ("skip", "before_watchdog_macro_recovery_window"))
        self.assertEqual(
            ao._macro_recovery_gate("watchdog", "2026-07-09", self._dt("05:46")),
            ("pass", "watchdog_missing_macro_recovery_window"))
        self.assertEqual(
            ao._macro_recovery_gate("watchdog", "2026-07-09", self._dt("05:49")),
            ("skip", "after_watchdog_macro_recovery_window"))
        self.assertEqual(
            ao._macro_recovery_gate("morning", "2026-07-09", self._dt("05:50")),
            ("pass", "morning_missing_macro_recovery_window"))
        self.assertEqual(
            ao._macro_recovery_gate("morning", "2026-07-09", self._dt("05:53")),
            ("skip", "after_morning_macro_recovery_window"))
        self.assertEqual(
            ao._macro_recovery_gate("unknown", "2026-07-09", self._dt("05:46")),
            ("fail", "macro_recovery_trigger_invalid"))
        self.assertEqual(
            ao._macro_recovery_gate("watchdog", "2026-07-08", self._dt("05:46")),
            ("fail", "target_not_current_pt_date"))

    def test_post_close_window_and_early_close(self):
        self.assertEqual(ao._phase_gate("post_close", "2026-07-09", self._dt("12:59"),
                                        self.MARKET, False)[1], "before_session_close")
        self.assertEqual(ao._phase_gate("post_close", "2026-07-09", self._dt("13:00"),
                                        self.MARKET, False)[0], "pass")
        early = {"session_close_et": "13:00"}
        self.assertEqual(ao._phase_gate("post_close", "2026-07-09", self._dt("10:00"),
                                        early, False)[0], "pass")

    def test_target_mismatch_blocks_except_dry_run(self):
        self.assertEqual(ao._phase_gate("morning", "2026-07-08", self._dt("06:00"),
                                        self.MARKET, False)[1],
                         "target_not_current_pt_date")
        self.assertEqual(ao._phase_gate("morning", "2026-07-08", self._dt("06:00"),
                                        self.MARKET, True)[0], "pass")

    def test_post_open_dependency_rejects_legacy_and_accepts_receipt_bound_v2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(ao._dependency_gate("post_open", "2026-07-09", root, False),
                             ("skip", "no_official_morning_artifact"))
            target = "2026-07-09"
            moment = datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc)
            stage_authoritative_inputs(root, target=target, moment=moment)
            legacy_morning_receipt.create(target, root=root, now=moment)
            self.assertEqual(ao._dependency_gate("post_open", "2026-07-09", root, False),
                             ("fail", "legacy_official_historical_audit_only"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = free_ensemble_fixture.T
            report = seed_v2_official(root, target)
            self.assertEqual(ao._dependency_gate("post_open", target, root, False),
                             ("pass", "official_morning_present"))
            resolved, kind, reason = ao._official_morning_artifact(
                target, root)
            self.assertEqual((resolved, kind, reason),
                             (report, "official_research_slot", "official_morning_present"))

    def test_post_open_dependency_rejects_unanchored_or_ambiguous_v2_slot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = free_ensemble_fixture.T
            report = seed_v2_official(root, target)
            report.write_text("tampered\n")
            status, reason = ao._dependency_gate("post_open", target, root, False)
            self.assertEqual(status, "fail")
            self.assertEqual(
                reason,
                "official_research_receipt_binding_invalid:receipt_report_hash_mismatch")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = free_ensemble_fixture.T
            seed_v2_official(root, target)
            legacy = root / "reports" / "morning" / f"{target}.md"
            legacy.write_text("legacy official\n")
            self.assertEqual(
                ao._dependency_gate("post_open", target, root, False),
                ("fail", "multiple_official_morning_artifacts"))

    def test_child_environment_strips_credentials(self):
        env = {
            "HOME": "/tmp/home",
            "PATH": "/usr/bin:/bin",
            "FMP_API_KEY": "secret",
            "TRADING_ACCESS_TOKEN": "secret",
            "TRADING_MAIN_CLAUDE_MODEL": "stable-model",
            "CLAUDE_BIN": "/tmp/untrusted-override",
            "UNRELATED": "drop",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            child = ao._child_env(False)
        self.assertNotIn("FMP_API_KEY", child)
        self.assertNotIn("TRADING_ACCESS_TOKEN", child)
        self.assertNotIn("CLAUDE_BIN", child)
        self.assertNotIn("UNRELATED", child)
        self.assertEqual(child["TRADING_MAIN_CLAUDE_MODEL"], "stable-model")
        self.assertEqual(child["TRADING_AUTONOMY_CHILD"], "1")

    def test_manifest_gate_has_a_cold_start_budget_and_keeps_exact_pass_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                    ao, "_run_gate", return_value=(0, {"check": "PASS"})) as run_gate:
                status, detail = ao._integrity_manifest_gate(root)
        self.assertEqual(status, "pass")
        self.assertEqual(detail, {"returncode": 0, "manifest_check": "PASS"})
        self.assertEqual(
            run_gate.call_args.kwargs["timeout"], ao.MANIFEST_GATE_TIMEOUT_SECONDS)
        self.assertEqual(ao.MANIFEST_GATE_TIMEOUT_SECONDS, 120)

    def test_manifest_gate_timeout_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ao, "_run_gate", return_value=(124, {})):
                status, detail = ao._integrity_manifest_gate(Path(td))
        self.assertEqual(status, "fail")
        self.assertEqual(detail, {"returncode": 124, "manifest_check": "unavailable"})


class TestArtifactContracts(unittest.TestCase):
    def test_intake_requires_fresh_hash_valid_receipt_and_preserves_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            seed_task_state(root, "data_intake", "warning")
            receipt = {
                "schema": data_lake_intake.SCHEMA,
                "status": "degraded",
                "research_ready": False,
                **data_lake_intake.SAFETY,
            }
            receipt["receipt_sha256"] = data_lake_intake.base.sha256_obj(receipt)
            path = (root / "data" / "runtime" / "data_lake" / "readiness"
                    / "2026-07-09.json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt))
            output = ao._postconditions("intake", "2026-07-09", root, start,
                                        False, "data_intake")
            self.assertEqual(output["status"], "warn")
            self.assertEqual(output["reason"], "data_readiness_degraded")
            tampered = json.loads(path.read_text())
            tampered["status"] = "ready"
            path.write_text(json.dumps(tampered))
            failed = ao._postconditions("intake", "2026-07-09", root, start,
                                        False, "data_intake")
            self.assertEqual(failed["status"], "fail")
            self.assertEqual(failed["reason"], "data_readiness_receipt_invalid")

    def test_macro_postcondition_accepts_only_explicit_byte_preserving_runner_detail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = seed_macro_receipt(root)
            old = time.time() - 120
            os.utime(artifact, (old, old))
            run_start = time.time()
            seed_task_state(
                root, "macro_actual_refresh", "success",
                datetime.now(timezone.utc).isoformat())
            state_path = root / "data" / "system_state.json"
            state = json.loads(state_path.read_text())
            state["runs"]["macro_actual_refresh"][
                "detail"] = "existing_macro_receipt_no_due_events_preserved"
            state_path.write_text(json.dumps(state))

            preserved = ao._postconditions(
                "macro_refresh", "2026-07-09", root, run_start,
                False, "macro_actual_refresh")
            self.assertEqual(preserved["status"], "pass")
            self.assertEqual(preserved["reason"], "macro_refresh_no_due_events")

            state["runs"]["macro_actual_refresh"]["detail"] = "no_due_macro_events"
            state_path.write_text(json.dumps(state))
            stale = ao._postconditions(
                "macro_refresh", "2026-07-09", root, run_start,
                False, "macro_actual_refresh")
            self.assertEqual(stale["status"], "fail")
            self.assertEqual(stale["reason"], "macro_refresh_receipt_stale")

    def test_macro_observation_rejects_structurally_valid_future_vintage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed_macro_receipt(
                root,
                generated=datetime(2026, 7, 9, 12, 53, tzinfo=timezone.utc))
            observed = ao._macro_receipt_observation(
                "2026-07-09", root,
                now=datetime(2026, 7, 9, 12, 46, tzinfo=timezone.utc))
            self.assertEqual(observed["status"], "invalid")
            self.assertFalse(observed["input_available"])
            self.assertEqual(
                observed["validation_errors"], ["receipt_generated_in_future"])

    def test_morning_requires_receipt_anchored_singleton_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdir = root / "reports" / "morning" / "preliminary"
            pdir.mkdir(parents=True)
            start = time.time() - 1
            seed_task_state(root, "morning_scan")
            one = pdir / "2026-07-09_one_0600PT.md"
            one.write_text("one")
            result = ao._postconditions("morning", "2026-07-09", root, start,
                                        False, "morning_scan")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["reason"], "unanchored_preliminary_rejected")
            self.assertIsNone(result["accepted_terminal_artifact"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frozen = datetime(2026, 7, 9, 12, 55, tzinfo=timezone.utc)
            start = frozen.timestamp() - 1
            seed_task_state(root, "morning_scan")
            preflight = root / "data" / "runtime" / "preflight" / "2026-07-09.json"
            preflight.parent.mkdir(parents=True)
            preflight.write_text(json.dumps({
                "target_session_date": "2026-07-09",
                "determination": {
                    "fresh_same_day_data": False,
                    "coverage_label": "none",
                },
            }))
            deterministic_terminal.emit(
                target="2026-07-09", stage="official_window",
                preflight_path=preflight, primary_fresh=False, coverage="none",
                rh_status="ROBINHOOD_UNAVAILABLE", rh_secondary_live=False,
                root=root, now=frozen)
            result = ao._postconditions("morning", "2026-07-09", root, start,
                                        False, "morning_scan")
            self.assertEqual(result["status"], "pass", result)
            self.assertEqual(result["accepted_kind"],
                             "deterministic_terminal_receipt_valid")

            pdir = root / "reports" / "morning" / "preliminary"
            two = pdir / "2026-07-09_two_0601PT.md"
            two.write_text("two")
            result2 = ao._postconditions("morning", "2026-07-09", root, start,
                                         False, "morning_scan")
            self.assertEqual(result2["status"], "fail")
            self.assertEqual(result2["duplicate_count"], 1)

    def test_receipt_anchored_morning_requires_complete_candidate_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = free_ensemble_fixture.T
            start = time.time() - 1
            seed_task_state(root, "morning_scan")
            seed_v2_official(root, target)
            receipt_path = (root / "data" / "runtime" / "research" / target
                            / "freeze_receipt.json")
            receipt = json.loads(receipt_path.read_text())
            candidate_path = (root / "data" / "runtime" / "research" / target
                              / "candidates.json")
            self.assertTrue(candidate_path.is_file())
            canonical = json.loads(candidate_path.read_text())
            candidate_path.unlink()

            missing = ao._postconditions(
                "morning", target, root, start, False, "morning_scan")
            self.assertEqual(missing["status"], "fail")
            self.assertEqual(missing["reason"], "candidate_artifact_missing")

            built = freeze_candidates.build_for_receipt(receipt, root=root)
            self.assertTrue(built["written"], built)
            passed = ao._postconditions(
                "morning", target, root, start, False, "morning_scan")
            self.assertEqual(passed["status"], "pass")
            self.assertEqual(passed["reason"], "terminal_singleton_candidate_bound")
            self.assertEqual(passed["candidate_count"], 0)

            candidate_path.unlink()
            draft_path = candidate_path.parent / "candidates_draft.json"
            draft_path.write_text(json.dumps({
                "target_session_date": target,
                "candidates": [{"classification": "Watch"}],
            }))
            incomplete_build = freeze_candidates.build_for_receipt(receipt, root=root)
            self.assertTrue(incomplete_build["written"], incomplete_build)
            self.assertEqual(incomplete_build["dropped_count"], 1)
            incomplete = ao._postconditions(
                "morning", target, root, start, False, "morning_scan")
            self.assertEqual(incomplete["status"], "fail")
            self.assertEqual(incomplete["reason"], "candidate_artifact_incomplete")

            draft_path.unlink()
            corrupted = copy.deepcopy(canonical)
            corrupted["unexpected"] = True
            candidate_path.write_text(json.dumps(corrupted))
            invalid = ao._postconditions(
                "morning", target, root, start, False, "morning_scan")
            self.assertEqual(invalid["status"], "fail")
            self.assertEqual(invalid["reason"], "candidate_artifact_invalid")

    def test_post_open_requires_exact_run_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            seed_task_state(root, "premarket_refresh", status="skipped")
            skipped = ao._postconditions("post_open", "2026-07-09", root, start,
                                         False, "premarket_refresh")
            self.assertEqual(skipped["status"], "skip")
            seed_task_state(root, "premarket_refresh")
            target = "2026-07-09"
            moment = datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc)
            stage_authoritative_inputs(root, target=target, moment=moment)
            legacy_morning_receipt.create(target, root=root, now=moment)
            run_id = "postopen-20260709T134500Z-0123456789ab"
            missing = ao._postconditions(
                "post_open", "2026-07-09", root, start,
                False, "premarket_refresh", run_id=run_id)
            self.assertEqual(missing["status"], "fail")
            artifact = root / "reports" / "intraday" / "2026-07-09-postopen.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("post open")
            bare_file = ao._postconditions(
                "post_open", "2026-07-09", root, start,
                False, "premarket_refresh", run_id=run_id)
            self.assertEqual(bare_file["status"], "fail")
            terminal = {
                "report_path": str(artifact.relative_to(root)),
                "report_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "artifact_sha256": "a" * 64,
                "snapshot_path": (
                    f"data/runtime/post_open_runs/{target}/{run_id}/market_snapshot.json"),
                "snapshot_file_sha256": "b" * 64,
                "accepted_morning_path": f"reports/morning/{target}.md",
                "accepted_morning_kind": "legacy_v1",
            }
            with mock.patch.object(
                    ao.post_open_finality, "read_validated_terminal",
                    return_value=(terminal, [])):
                passed = ao._postconditions(
                    "post_open", "2026-07-09", root, start,
                    False, "premarket_refresh", run_id=run_id)
            self.assertEqual(passed["status"], "pass")
            self.assertEqual(passed["reason"], "post_open_terminal_receipt_validated")

    def test_post_open_postconditions_accept_receipt_bound_v2_official(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = free_ensemble_fixture.T
            start = time.time() - 1
            seed_task_state(root, "premarket_refresh")
            official = seed_v2_official(root, target)
            run_id = "postopen-20260702T134500Z-0123456789ab"
            missing = ao._postconditions(
                "post_open", target, root, start,
                False, "premarket_refresh", run_id=run_id)
            self.assertEqual(missing["status"], "fail")
            artifact = root / "reports" / "intraday" / f"{target}-postopen.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("post open\n")
            terminal = {
                "report_path": str(artifact.relative_to(root)),
                "report_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "artifact_sha256": "a" * 64,
                "snapshot_path": (
                    f"data/runtime/post_open_runs/{target}/{run_id}/market_snapshot.json"),
                "snapshot_file_sha256": "b" * 64,
                "accepted_morning_path": str(official.relative_to(root)),
                "accepted_morning_kind": "governance_v2",
            }
            with mock.patch.object(
                    ao.post_open_finality, "read_validated_terminal",
                    return_value=(terminal, [])):
                passed = ao._postconditions(
                    "post_open", target, root, start,
                    False, "premarket_refresh", run_id=run_id)
            self.assertEqual(passed["status"], "pass")
            self.assertEqual(passed["official_morning_artifact"],
                             str(official.relative_to(root)))

    def test_post_close_requires_fresh_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            seed_task_state(root, "post_close_review")
            missing = ao._postconditions("post_close", "2026-07-09", root, start,
                                         False, "post_close_review",
                                         run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(missing["status"], "fail")
            report = root / "reports" / "post_close" / "2026-07-09.md"
            report.parent.mkdir(parents=True)
            report.write_text("close")
            without_receipt = ao._postconditions(
                "post_close", "2026-07-09", root, start, False, "post_close_review",
                run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(without_receipt["status"], "fail")
            self.assertEqual(without_receipt["reason"],
                             "post_close_contract_receipt_missing_or_stale")
            seed_post_close_contract_receipt(root, report)
            with mock.patch.object(
                    ao.post_close_finality, "validate_cutoff",
                    return_value=["cutoff_missing"]):
                unsealed = ao._postconditions(
                    "post_close", "2026-07-09", root, start,
                    False, "post_close_review", run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(unsealed["status"], "fail")
            self.assertEqual(unsealed["reason"], "post_close_cutoff_receipt_invalid")
            with mock.patch.object(ao.post_close_finality, "validate_cutoff", return_value=[]):
                passed = ao._postconditions(
                    "post_close", "2026-07-09", root, start,
                    False, "post_close_review", run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(passed["status"], "pass")
            self.assertEqual(passed["reason"], "post_close_report_contract_validated")

    def test_post_close_rejects_invalid_or_hash_mismatched_contract_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            seed_task_state(root, "post_close_review")
            report = root / "reports" / "post_close" / "2026-07-09.md"
            report.parent.mkdir(parents=True)
            report.write_text("close")
            seed_post_close_contract_receipt(root, report, ok=False)
            invalid = ao._postconditions("post_close", "2026-07-09", root, start,
                                         False, "post_close_review",
                                         run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(invalid["reason"], "post_close_contract_receipt_invalid")
            seed_post_close_contract_receipt(root, report)
            report.write_text("tampered")
            mismatch = ao._postconditions("post_close", "2026-07-09", root, start,
                                          False, "post_close_review",
                                          run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(mismatch["reason"], "post_close_contract_receipt_invalid")

    def test_dry_run_requires_fresh_runner_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed_task_state(root, "morning_scan", "dry_run_ok",
                            "2026-07-09T00:00:00+00:00")
            out = ao._postconditions("morning", "2026-07-09", root, time.time(),
                                     True, "morning_scan")
            self.assertEqual(out["status"], "fail")
            self.assertEqual(out["reason"], "dry_run_state_missing_or_stale")

    def test_fresh_warning_maps_to_degraded_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            seed_task_state(root, "post_close_review", "warning")
            report = root / "reports" / "post_close" / "2026-07-09.md"
            report.parent.mkdir(parents=True)
            report.write_text("close")
            seed_post_close_contract_receipt(root, report)
            with mock.patch.object(ao.post_close_finality, "validate_cutoff", return_value=[]):
                out = ao._postconditions(
                    "post_close", "2026-07-09", root, start,
                    False, "post_close_review", run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(out["status"], "warn")


class TestSupervisedRun(unittest.TestCase):
    @staticmethod
    def _post_close_pass() -> dict:
        return {
            "task_status": "success", "task_detail": "done",
            "task_state_fresh": True, "activation_authority": False,
            "execution_enabled": False, "status": "pass",
            "reason": "post_close_report_contract_validated",
            "artifact": "reports/post_close/2026-07-09.md",
            "contract_receipt":
                "data/runtime/health/2026-07-09_post_close_report_contract.json",
            "contract_receipt_sha256": "a" * 64,
        }

    def test_post_close_seals_terminal_once_and_duplicate_preserves_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "data" / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 13, 15, tzinfo=tzutil.PT)
            sealed = {"ok": True, "action": "created", "artifact_sha256": "b" * 64}
            morning_ok = (root / "reports" / "morning" / "official_research" / "2026-07-09.md",
                          "official_research_slot", "official_morning_present")
            with mock.patch.object(ao, "_official_morning_artifact",
                                   return_value=morning_ok), \
                    mock.patch.object(ao, "_postconditions", return_value=self._post_close_pass()), \
                    mock.patch.object(
                        ao.post_close_finality, "seal_terminal", return_value=sealed) as seal:
                result, rc = ao.run_workflow(
                    "post_close", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)
            self.assertEqual(rc, 0, result)
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["post_close_finality"], sealed)
            seal.assert_called_once()
            state_path = state_root / "2026-07-09.json"
            before = state_path.read_bytes()

            duplicate, duplicate_rc = ao.run_workflow(
                "post_close", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=False, now=now)
            self.assertEqual(duplicate_rc, 0, duplicate)
            self.assertFalse(duplicate["runner_invoked"])
            self.assertEqual(duplicate["outcome"], "duplicate_terminal_preserved")
            self.assertEqual(state_path.read_bytes(), before)

    def test_post_close_terminal_seal_failure_is_recorded_not_completed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "data" / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 13, 15, tzinfo=tzutil.PT)
            morning_ok = (root / "reports" / "morning" / "official_research" / "2026-07-09.md",
                          "official_research_slot", "official_morning_present")
            with mock.patch.object(ao, "_official_morning_artifact",
                                   return_value=morning_ok), \
                    mock.patch.object(ao, "_postconditions", return_value=self._post_close_pass()), \
                    mock.patch.object(
                        ao.post_close_finality, "seal_terminal",
                        side_effect=ao.post_close_finality.FinalityError("seal_collision")):
                result, rc = ao.run_workflow(
                    "post_close", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)
            self.assertEqual(rc, 1, result)
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["outcome"], "post_close_terminal_receipt_failed")
            state = json.loads((state_root / "2026-07-09.json").read_text())
            workflow = state["workflows"]["post_close"]
            self.assertEqual(workflow["state"], "failed")
            self.assertEqual(len(workflow["attempts"]), 1)
            self.assertEqual(
                workflow["attempts"][0]["outcome"],
                "post_close_terminal_receipt_failed")
            self.assertIn("terminal_receipt_failed", [event["event"] for event in state["events"]])

    def test_health_refresh_accepts_at_issue_receipt_without_changing_authority(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "scripts" / "autonomy_service_health.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "import argparse, json\n"
                "from pathlib import Path\n"
                "p=argparse.ArgumentParser()\n"
                "p.add_argument('--target'); p.add_argument('--root')\n"
                "p.add_argument('--autonomy-state'); p.add_argument('--now')\n"
                "p.add_argument('--out'); a=p.parse_args()\n"
                "d={'schema':'autonomy_service_health/1.0',\n"
                "   'target_session_date':a.target,'generated_utc':a.now,\n"
                "   'overall_status':'at_issue','artifact_sha256':'1'*64}\n"
                "Path(a.out).parent.mkdir(parents=True,exist_ok=True)\n"
                "Path(a.out).write_text(json.dumps(d))\n"
                "raise SystemExit(1)\n")
            state_path = root / "state" / "2026-07-09.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}")
            seed_health_manifest(root)
            result = ao._refresh_service_health(
                root=root, target="2026-07-09", state_path=state_path,
                now=datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["overall_status"], "at_issue")
        self.assertTrue(result["fresh"])
        self.assertTrue(result["runner_outcome_unchanged"])
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["broker_invoked"])

    def test_structural_health_refresh_failure_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "scripts" / "autonomy_service_health.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise SystemExit(2)\n")
            state_path = root / "state" / "2026-07-09.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}")
            seed_health_manifest(root)
            result = ao._refresh_service_health(
                root=root, target="2026-07-09", state_path=state_path,
                now=datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 2)
        self.assertTrue(result["runner_outcome_unchanged"])

    def test_health_refresh_is_suppressed_before_unverified_code_executes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "executed.txt"
            script = root / "scripts" / "autonomy_service_health.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                f"open({str(marker)!r},'w').write('executed')\n")
            state_path = root / "state" / "2026-07-09.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}")
            result = ao._refresh_service_health(
                root=root, target="2026-07-09", state_path=state_path,
                now=datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc))
            executed = marker.exists()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("critical_manifest_unavailable", result["reason"])
        self.assertFalse(executed)

    def test_dry_run_invokes_exact_child_once_and_records_terminal_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT)
            out, rc = ao.run_workflow("morning", target="2026-07-09", config_path=config,
                                      state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(rc, 0, out)
            self.assertTrue(out["ok"])
            self.assertEqual(out["state"], "completed")
            self.assertEqual((root / "runner_invoked.txt").read_text(), "morning")
            state = json.loads((state_root / "2026-07-09.json").read_text())
            attempts = state["workflows"]["morning"]["attempts"]
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["postconditions"]["reason"], "dry_run_contract")
            self.assertFalse(state["execution_enabled"])

    def test_macro_continuity_runs_once_then_preserves_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT)
            first, first_rc = ao.recover_missing_macro_refresh(
                trigger="watchdog", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(first_rc, 0, first)
            self.assertTrue(first["runner_invoked"])
            self.assertEqual(first["state"], "completed")
            state_path = state_root / "2026-07-09.json"
            state = json.loads(state_path.read_text())
            attempts = state["workflows"]["macro_refresh"]["attempts"]
            self.assertEqual(len(attempts), 1)
            preflight = attempts[0]["preflight"]
            self.assertTrue(preflight["continuity_recovery"])
            self.assertTrue(preflight["recovery_only_if_never_started"])
            self.assertEqual(preflight["recovery_trigger"], "watchdog")
            self.assertEqual(
                preflight["recovery_child_timeout_seconds"],
                ao.MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS)
            before = state_path.read_bytes()

            second, second_rc = ao.recover_missing_macro_refresh(
                trigger="watchdog", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(second_rc, 0, second)
            self.assertFalse(second["runner_invoked"])
            self.assertEqual(second["outcome"], "continuity_not_needed_prior_attempt")
            self.assertEqual(second["prior_state"], "completed")
            self.assertEqual(second["prior_attempt_count"], 1)
            self.assertEqual(second["input_status"], "absent")
            self.assertFalse(second["input_available"])
            self.assertEqual(state_path.read_bytes(), before)

            delayed, delayed_rc = ao.run_workflow(
                "macro_refresh", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(delayed_rc, 0, delayed)
            self.assertFalse(delayed["runner_invoked"])
            self.assertEqual(delayed["outcome"], "duplicate_terminal_preserved")
            self.assertEqual(state_path.read_bytes(), before)

    def test_recovery_preserves_valid_direct_receipt_without_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            seed_readiness_receipt(root)
            receipt_path = seed_macro_receipt(root)
            before = receipt_path.read_bytes()
            now = datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT)

            with mock.patch.object(ao.tzutil, "now_utc", return_value=now):
                result, rc = ao.recover_missing_macro_refresh(
                    trigger="watchdog", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)

            self.assertEqual(rc, 0, result)
            self.assertFalse(result["runner_invoked"])
            self.assertTrue(result["existing_receipt_preserved"])
            self.assertEqual(result["outcome"], "existing_macro_receipt_preserved")
            self.assertEqual(receipt_path.read_bytes(), before)
            self.assertFalse((root / "runner_invoked.txt").exists())
            state = json.loads((state_root / "2026-07-09.json").read_text())
            attempt = state["workflows"]["macro_refresh"]["attempts"][0]
            self.assertEqual(attempt["state"], "completed")
            self.assertEqual(attempt["preflight"]["existing_macro_receipt"][
                "receipt_status"], "no_due_events")

    def test_recovery_preserves_invalid_direct_receipt_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            seed_readiness_receipt(root)
            receipt_path = root / "data" / "runtime" / "macro_actual_refresh" / \
                "2026-07-09.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_bytes(b"ambiguous-existing-bytes\n")
            before = receipt_path.read_bytes()
            now = datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT)

            with mock.patch.object(ao.tzutil, "now_utc", return_value=now):
                result, rc = ao.recover_missing_macro_refresh(
                    trigger="watchdog", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)

            self.assertEqual(rc, 78, result)
            self.assertFalse(result["runner_invoked"])
            self.assertEqual(result["outcome"], "preexisting_macro_receipt_invalid")
            self.assertEqual(receipt_path.read_bytes(), before)
            self.assertFalse((root / "runner_invoked.txt").exists())
            state = json.loads((state_root / "2026-07-09.json").read_text())
            self.assertEqual(state["workflows"]["macro_refresh"]["state"], "blocked")

    def test_macro_continuity_after_usefulness_window_is_a_write_free_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            out, rc = ao.recover_missing_macro_refresh(
                trigger="morning", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True,
                now=datetime(2026, 7, 9, 5, 53, tzinfo=tzutil.PT))
            self.assertEqual(rc, 0, out)
            self.assertFalse(out["runner_invoked"])
            self.assertEqual(out["outcome"], "after_morning_macro_recovery_window")
            self.assertFalse((state_root / "2026-07-09.json").exists())

    def test_morning_handoff_preserves_watchdog_no_dispatch_history_and_runs_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            config_sha = ao._sha256_file(config)
            self.assertIsNotNone(config_sha)
            state_root = root / "runtime" / "autonomy"
            state_path = state_root / "2026-07-09.json"
            prior_run = seed_watchdog_no_dispatch_reservation(
                state_path, config_sha, target="2026-07-09")
            prior = copy.deepcopy(json.loads(state_path.read_text())[
                "workflows"]["macro_refresh"]["attempts"][0])

            out, rc = ao.recover_missing_macro_refresh(
                trigger="morning", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True,
                now=datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT))
            self.assertEqual(rc, 0, out)
            self.assertTrue(out["runner_invoked"])
            state = json.loads(state_path.read_text())
            attempts = state["workflows"]["macro_refresh"]["attempts"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], prior)
            self.assertEqual(attempts[1]["state"], "completed")
            self.assertTrue(attempts[1]["preflight"][
                "watchdog_no_dispatch_handoff"])
            self.assertEqual(attempts[1]["preflight"][
                "handoff_from_run_id"], prior_run)
            handoff_events = [event for event in state["events"]
                              if event["event"] == "watchdog_no_dispatch_handoff"]
            self.assertEqual(len(handoff_events), 1)
            self.assertEqual(handoff_events[0]["reason"],
                             "prior_watchdog_window_expired")

            before = state_path.read_bytes()
            duplicate, duplicate_rc = ao.recover_missing_macro_refresh(
                trigger="morning", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True,
                now=datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT))
            self.assertEqual(duplicate_rc, 0, duplicate)
            self.assertEqual(duplicate["outcome"],
                             "continuity_not_needed_prior_attempt")
            self.assertFalse(duplicate["runner_invoked"])
            self.assertEqual(state_path.read_bytes(), before)

    def test_macro_continuity_final_dispatch_cutoff_prevents_late_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 5, 47, tzinfo=tzutil.PT)
            with mock.patch.object(ao, "_macro_recovery_gate", side_effect=[
                    ("pass", "watchdog_missing_macro_recovery_window"),
                    ("skip", "after_watchdog_macro_recovery_window")]):
                out, rc = ao.run_workflow(
                    "macro_refresh", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=True, now=now,
                    recovery_trigger="watchdog")
            self.assertEqual(rc, 0, out)
            self.assertFalse(out["runner_invoked"])
            self.assertEqual(out["outcome"], "after_watchdog_macro_recovery_window")
            self.assertFalse((root / "runner_invoked.txt").exists())
            state = json.loads((state_root / "2026-07-09.json").read_text())
            attempt = state["workflows"]["macro_refresh"]["attempts"][0]
            self.assertEqual(attempt["state"], "skipped")
            self.assertEqual(attempt["preflight"]["recovery_dispatch_gate"], "skip")

    def test_ordinary_macro_rechecks_phase_at_dispatch_and_starts_no_child_when_late(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            seed_readiness_receipt(root)
            invoked = datetime(2026, 7, 9, 5, 49, 50, tzinfo=tzutil.PT)
            late = datetime(2026, 7, 9, 5, 50, 1, tzinfo=tzutil.PT)
            with mock.patch.object(ao.tzutil, "now_utc", return_value=late), \
                    mock.patch.object(ao, "_phase_gate", side_effect=[
                        ("pass", "post_release_macro_refresh_window"),
                        ("skip", "after_macro_refresh_window"),
                    ]) as phase:
                result, rc = ao.run_workflow(
                    "macro_refresh", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=invoked)

            self.assertEqual(rc, 0, result)
            self.assertFalse(result["runner_invoked"])
            self.assertEqual(result["outcome"], "after_macro_refresh_window")
            self.assertEqual(phase.call_count, 2)
            self.assertFalse((root / "runner_invoked.txt").exists())
            state = json.loads((state_root / "2026-07-09.json").read_text())
            attempt = state["workflows"]["macro_refresh"]["attempts"][0]
            self.assertEqual(attempt["preflight"]["ordinary_dispatch_gate"], "skip")
            self.assertEqual(
                datetime.fromisoformat(attempt["finished_utc"]), tzutil.to_utc(late))

    def test_running_macro_race_is_not_retried_and_normal_duplicate_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            state_path = state_root / "2026-07-09.json"
            now = datetime(2026, 7, 9, 5, 46, tzinfo=tzutil.PT)
            ao._start_attempt(
                state_path, "2026-07-09", "macro_refresh", "macro-live", "cfg",
                True, now)
            before = state_path.read_bytes()

            recovery, recovery_rc = ao.recover_missing_macro_refresh(
                trigger="watchdog", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(recovery_rc, 0, recovery)
            self.assertEqual(recovery["outcome"], "continuity_not_needed_prior_attempt")
            self.assertFalse(recovery["runner_invoked"])
            self.assertEqual(recovery["prior_state"], "running")
            self.assertEqual(recovery["prior_attempt_count"], 1)
            self.assertEqual(recovery["input_status"], "absent")
            self.assertFalse(recovery["input_available"])
            self.assertEqual(state_path.read_bytes(), before)

            ordinary, ordinary_rc = ao.run_workflow(
                "macro_refresh", target="2026-07-09", config_path=config,
                state_root=state_root, root=root, dry_run=True, now=now)
            self.assertEqual(ordinary_rc, 75, ordinary)
            self.assertEqual(ordinary["error"], "workflow_already_running")
            self.assertFalse(ordinary["runner_invoked"])
            self.assertFalse((root / "runner_invoked.txt").exists())
            self.assertEqual(state_path.read_bytes(), before)

    def test_morning_preflight_requests_supervised_macro_recovery_before_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT)
            recovery_result = ({
                "ok": True, "workflow": "macro_refresh", "target": "2026-07-09",
                "state": "completed", "outcome": "contract_satisfied",
                "runner_invoked": True, "execution_enabled": False,
            }, 0)
            with mock.patch.object(
                    ao, "recover_missing_macro_refresh",
                    return_value=recovery_result) as recover_mock, mock.patch.object(
                    ao, "_postconditions", return_value={
                        "status": "pass", "reason": "test_terminal"}):
                out, rc = ao.run_workflow(
                    "morning", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)
            self.assertEqual(rc, 0, out)
            self.assertTrue(out["runner_invoked"])
            recover_mock.assert_called_once_with(
                trigger="morning", target="2026-07-09", config_path=config,
                state_root=state_root, root=root)
            state = json.loads((state_root / "2026-07-09.json").read_text())
            continuity = state["workflows"]["morning"]["attempts"][0][
                "preflight"]["macro_continuity"]
            self.assertEqual(continuity["status"], "completed")
            self.assertEqual(continuity["outcome"], "contract_satisfied")
            self.assertTrue(continuity["runner_invoked"])
            self.assertEqual(continuity["input_status"], "absent")
            self.assertFalse(continuity["input_available"])
            self.assertFalse(continuity["slo_repair_claimed"])
            self.assertEqual((root / "runner_invoked.txt").read_text(), "morning")

    def test_morning_continuity_separates_no_dispatch_from_validated_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            seed_macro_receipt(root)
            recovery_result = ({
                "ok": True, "workflow": "macro_refresh", "target": "2026-07-09",
                "state": "unchanged", "outcome": "continuity_not_needed_prior_attempt",
                "runner_invoked": False, "execution_enabled": False,
            }, 0)
            with mock.patch.object(
                    ao, "recover_missing_macro_refresh",
                    return_value=recovery_result), mock.patch.object(
                    ao, "_postconditions", return_value={
                        "status": "pass", "reason": "test_terminal"}):
                result, rc = ao.run_workflow(
                    "morning", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False,
                    now=datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT))

            self.assertEqual(rc, 0, result)
            state = json.loads((state_root / "2026-07-09.json").read_text())
            continuity = state["workflows"]["morning"]["attempts"][0][
                "preflight"]["macro_continuity"]
            self.assertEqual(continuity["status"], "no_dispatch")
            self.assertTrue(continuity["input_available"])
            self.assertEqual(continuity["input_status"], "valid")
            self.assertEqual(continuity["input_receipt_status"], "no_due_events")
            self.assertRegex(continuity["input_artifact_file_sha256"], r"^[0-9a-f]{64}$")

    def test_morning_continuity_marks_recovery_error_at_issue_without_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "runtime" / "autonomy"
            recovery_result = ({
                "ok": False, "workflow": "macro_refresh", "target": "2026-07-09",
                "error": "integrity_manifest_gate_failed", "runner_invoked": False,
                "execution_enabled": False,
            }, 78)
            with mock.patch.object(
                    ao, "recover_missing_macro_refresh",
                    return_value=recovery_result), mock.patch.object(
                    ao, "_postconditions", return_value={
                        "status": "pass", "reason": "test_terminal"}):
                result, rc = ao.run_workflow(
                    "morning", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False,
                    now=datetime(2026, 7, 9, 5, 50, tzinfo=tzutil.PT))

            self.assertEqual(rc, 0, result)
            state = json.loads((state_root / "2026-07-09.json").read_text())
            continuity = state["workflows"]["morning"]["attempts"][0][
                "preflight"]["macro_continuity"]
            self.assertEqual(continuity["status"], "at_issue")
            self.assertFalse(continuity["input_available"])
            self.assertEqual(continuity["input_status"], "absent")

    def test_non_trading_day_skips_without_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root, trading=False)
            out, rc = ao.run_workflow("morning", target="2026-07-04", config_path=config,
                                      state_root=root / "state", root=root, dry_run=True,
                                      now=datetime(2026, 7, 4, 6, 0, tzinfo=tzutil.PT))
            self.assertEqual(rc, 0, out)
            self.assertFalse(out["runner_invoked"])
            self.assertEqual(out["outcome"], "non_trading_day")
            self.assertFalse((root / "runner_invoked.txt").exists())

    def test_broker_permission_failure_blocks_before_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root, broker_rc=1)
            out, rc = ao.run_workflow("morning", target="2026-07-09", config_path=config,
                                      state_root=root / "state", root=root, dry_run=True,
                                      now=datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT))
            self.assertEqual(rc, 78, out)
            self.assertFalse(out["runner_invoked"])
            self.assertEqual(out["outcome"], "broker_permission_gate_failed")
            self.assertFalse((root / "runner_invoked.txt").exists())

    def test_manifest_failure_blocks_before_broker_and_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root, integrity_rc=1)
            out, rc = ao.run_workflow("morning", target="2026-07-09", config_path=config,
                                      state_root=root / "state", root=root, dry_run=True,
                                      now=datetime(2026, 7, 9, 6, 0, tzinfo=tzutil.PT))
            self.assertEqual(rc, 78, out)
            self.assertFalse(out["runner_invoked"])
            self.assertEqual(out["outcome"], "integrity_manifest_gate_failed")
            self.assertFalse((root / "runner_invoked.txt").exists())

    def test_runner_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root, runner_rc=9)
            out, rc = ao.run_workflow("post_close", target="2026-07-09", config_path=config,
                                      state_root=root / "state", root=root, dry_run=True,
                                      now=datetime(2026, 7, 9, 14, 0, tzinfo=tzutil.PT))
            self.assertEqual(rc, 1, out)
            self.assertEqual(out["runner_exit_code"], 9)
            self.assertEqual(
                out["service_health_refresh"]["status"], "unavailable")
            self.assertTrue(
                out["service_health_refresh"]["runner_outcome_unchanged"])
            state = json.loads((root / "state" / "2026-07-09.json").read_text())
            self.assertEqual(len(state["workflows"]["post_close"]["attempts"]), 1)

    def test_spawned_group_is_terminated_if_runner_started_state_write_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            runner = root / ao.ALLOWED_RUNNERS["post_close"]
            completed_marker = root / "child_completed.txt"
            runner.write_text(
                "#!/bin/bash\n"
                "set -eu\n"
                "sleep 30\n"
                f"printf complete > {completed_marker!s}\n")
            real_update = ao._update_attempt
            child_pids = []

            def fail_runner_started(*args, **kwargs):
                if kwargs.get("event") == "runner_started":
                    child_pids.append(args[5]["child_pid"])
                    raise OSError("simulated_state_write_failure")
                return real_update(*args, **kwargs)

            with mock.patch.object(ao, "_update_attempt", side_effect=fail_runner_started):
                result, rc = ao.run_workflow(
                    "post_close", target="2026-07-09", config_path=config,
                    state_root=root / "state", root=root, dry_run=True,
                    now=datetime(2026, 7, 9, 14, 0, tzinfo=tzutil.PT))

            self.assertEqual(rc, 1, result)
            self.assertEqual(result["runner_exit_code"], 127)
            self.assertEqual(len(child_pids), 1)
            self.assertFalse(ao._pid_alive(child_pids[0]))
            self.assertFalse(completed_marker.exists())
            state = json.loads((root / "state" / "2026-07-09.json").read_text())
            attempt = state["workflows"]["post_close"]["attempts"][0]
            self.assertEqual(attempt["state"], "failed")
            self.assertEqual(attempt["outcome"], "runner_exit_127")

    def test_source_has_no_shell_execution_or_order_commands(self):
        source = (ROOT / "scripts" / "autonomy_orchestrator.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertIn("runner_outcome_unchanged", source)
        self.assertIn("--autonomy-state", source)
        self.assertIn("recovery_only_if_never_started", source)
        self.assertIn("MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS", source)
        for token in ("place_equity_order", "cancel_equity_order", "review_equity_order",
                      "place_option_order", "cancel_option_order"):
            self.assertNotIn(token, source)

    def test_post_close_exact_absence_requires_valid_maintenance_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = time.time() - 1
            path = root / "data" / "system_state.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "runs": {"post_close_review": {
                    "status": "skipped",
                    "detail": "no_official_morning_artifact",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                }}
            }))
            receipt = ao.post_close_maintenance_receipt.receipt_path(
                "2026-07-09", POST_CLOSE_TEST_RUN_ID, root)
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({
                "artifact_sha256": "b" * 64,
                "stages": [
                    {"invoked": True, "returncode": 0} for _ in range(20)
                ],
                "output_observations": [
                    {"state": "absent"} for _ in range(22)
                ],
            }))
            with mock.patch.object(
                    ao.post_close_maintenance_receipt, "validate_receipt", return_value=[]):
                passed = ao._postconditions(
                    "post_close", "2026-07-09", root, start, False,
                    "post_close_review", run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(passed["status"], "skip")
            self.assertEqual(passed["reason"], "no_official_morning_artifact")
            self.assertEqual(passed["maintenance_attempted_stage_count"], 20)
            self.assertEqual(passed["maintenance_zero_return_stage_count"], 20)
            self.assertEqual(passed["maintenance_output_absent_count"], 22)
            with mock.patch.object(
                    ao.post_close_maintenance_receipt, "validate_receipt",
                    return_value=["tampered"]):
                refused = ao._postconditions(
                    "post_close", "2026-07-09", root, start, False,
                    "post_close_review", run_id=POST_CLOSE_TEST_RUN_ID)
            self.assertEqual(refused["status"], "fail")
            self.assertEqual(refused["reason"],
                             "post_close_maintenance_receipt_invalid")

    def test_post_close_maintenance_skip_never_seals_narrative_finality(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root)
            state_root = root / "data" / "runtime" / "autonomy"
            now = datetime(2026, 7, 9, 13, 15, tzinfo=tzutil.PT)
            absent = (None, "none", "no_official_morning_artifact")
            post = {
                "task_status": "skipped", "task_detail": "no_official_morning_artifact",
                "task_state_fresh": True, "activation_authority": False,
                "execution_enabled": False, "status": "skip",
                "reason": "no_official_morning_artifact",
            }
            with mock.patch.object(ao, "_official_morning_artifact",
                                   return_value=absent), \
                    mock.patch.object(ao, "_postconditions", return_value=post), \
                    mock.patch.object(ao.post_close_finality, "seal_terminal") as seal:
                result, rc = ao.run_workflow(
                    "post_close", target="2026-07-09", config_path=config,
                    state_root=state_root, root=root, dry_run=False, now=now)
            self.assertEqual(rc, 0, result)
            self.assertEqual(result["state"], "skipped")
            self.assertEqual(result["outcome"], "no_official_morning_artifact")
            self.assertIsNone(result["post_close_finality"])
            seal.assert_not_called()
            self.assertEqual((root / "post_close_mode.txt").read_text(), "maintenance")
            attempt = json.loads((state_root / "2026-07-09.json").read_text())[
                "workflows"]["post_close"]["attempts"][0]
            self.assertEqual(attempt["preflight"]["post_close_mode"], "maintenance")
            self.assertEqual(attempt["preflight"]["post_close_morning_reason"],
                             "no_official_morning_artifact")

    def test_post_close_runner_authority_refusal_preserves_exit_78(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_runner_tree(root, runner_rc=78)
            absent = (None, "none", "no_official_morning_artifact")
            with mock.patch.object(ao, "_official_morning_artifact", return_value=absent):
                result, rc = ao.run_workflow(
                    "post_close", target="2026-07-09", config_path=config,
                    state_root=root / "data" / "runtime" / "autonomy", root=root,
                    dry_run=False,
                    now=datetime(2026, 7, 9, 13, 15, tzinfo=tzutil.PT))
            self.assertEqual(rc, 78, result)
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["outcome"], "runner_authority_gate_blocked")

class TestRepositoryWiring(unittest.TestCase):
    def test_common_delegates_only_by_opt_in_and_child_guard(self):
        common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
        self.assertIn("delegate_to_autonomy()", common)
        self.assertIn('TRADING_AUTONOMY_ENABLE:-0}', common)
        self.assertIn('TRADING_AUTONOMY_CHILD:-0}', common)
        self.assertIn('exec "$py" "${args[@]}"', common)

    def test_each_existing_runner_delegates_to_its_exact_workflow(self):
        expected = {
            "run_data_intake.sh": "delegate_to_autonomy intake",
            "run_morning_scan.sh": "delegate_to_autonomy morning",
            "run_premarket_refresh.sh": "delegate_to_autonomy post_open",
            "run_post_close_review.sh": "delegate_to_autonomy post_close",
        }
        for filename, call in expected.items():
            text = (ROOT / "scripts" / filename).read_text()
            self.assertEqual(text.count(call), 1, filename)
            self.assertLess(text.index(call), text.index("acquire_lock"), filename)

    def test_repository_launchagents_opt_in_with_canonical_deployment_paths(self):
        config = production_config()
        for workflow, wf in config["workflows"].items():
            path = ROOT / "launchd" / f"{wf['launchd_label']}.plist"
            with path.open("rb") as fh:
                doc = plistlib.load(fh)
            self.assertEqual((doc.get("EnvironmentVariables") or {}).get(
                "TRADING_AUTONOMY_ENABLE"), "1", workflow)
            self.assertEqual((doc.get("ProgramArguments") or [])[1],
                             str(DEPLOYMENT_ROOT / wf["runner"]), workflow)

    def test_installer_preflight_requires_supervisor_opt_in(self):
        installer = (ROOT / "scripts" / "install_launchd.sh").read_text()
        self.assertIn("autonomy_supervisor_not_enabled", installer)
        self.assertIn('env.get("TRADING_AUTONOMY_ENABLE") != "1"', installer)

    def test_cli_rejects_unsafe_status_target_and_bad_now(self):
        self.assertEqual(ao.main(["status", "--target", "../../escape"]), 2)
        self.assertEqual(ao.main(["run", "--workflow", "morning", "--dry-run",
                                  "--now", "not-a-time"]), 2)


if __name__ == "__main__":
    unittest.main()
