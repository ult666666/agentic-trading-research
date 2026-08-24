#!/usr/bin/env python3
"""Fail-closed supervisor for the existing research runners.

This is a control plane, not a replacement for the established morning/post-open/post-close
logic. It validates a versioned workflow contract, records one cross-session state artifact,
applies trading-day/dependency/time/broker-permission gates, executes exactly one allowlisted
runner without a shell, bounds the whole process group, and verifies the runner's existing
artifact contract afterward.

It grants no trading or strategy authority:
  * research_only=true and execution_enabled=false are exact config requirements;
  * every invocation re-runs validate_broker_permissions.py before a runner can start;
  * only five exact repository runners are accepted; no dynamic command or shell string exists;
  * no retry is added around a runner; the macro continuity path may launch only a workflow that
    has zero recorded attempts, during one fixed pre-Morning usefulness window;
  * state contains status codes, paths, hashes, PIDs, and timestamps only -- never prices,
    balances, positions, account identifiers, model output, credentials, or raw tool payloads.

Activation is deliberately opt-in. The existing runners delegate here only when
TRADING_AUTONOMY_ENABLE=1 and never when TRADING_AUTONOMY_CHILD=1. The supervisor sets the child
marker before invoking the same runner, preventing recursion. Existing installed LaunchAgents
remain unchanged until their plists are explicitly reinstalled.

CLI:
  autonomy_orchestrator.py check
  autonomy_orchestrator.py status [--target YYYY-MM-DD]
  autonomy_orchestrator.py run --workflow intake|macro_refresh|morning|post_open|post_close
  autonomy_orchestrator.py recover-macro --trigger watchdog|morning

Exit codes:
  0   completed or deterministic clean skip
  1   runner/postcondition failure
  2   malformed CLI/config/state
  75  active/concurrent workflow blocked
  78  safety/authority gate blocked before runner start
  124 supervisor timeout (whole child process group terminated)
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import artifact_provenance as provenance  # noqa: E402
import candidate_artifact_health  # noqa: E402
import data_lake_intake  # noqa: E402
import macro_actual_refresh  # noqa: E402
import morning_authority  # noqa: E402
import morning_audit_supervisor  # noqa: E402
import post_close_finality  # noqa: E402
import post_close_maintenance_receipt  # noqa: E402
import post_open_finality  # noqa: E402
import system_state_publish  # noqa: E402
import terminal_reconcile  # noqa: E402
import tzutil  # noqa: E402

CONFIG_DEFAULT = ROOT / "config" / "autonomy_workflows.json"
STATE_ROOT_DEFAULT = ROOT / "data" / "runtime" / "autonomy"
CONFIG_SCHEMA = "autonomy_workflows/1.1"
CONFIG_VERSION = "1.5.0"
STATE_SCHEMA = "autonomy_daily_state/1.1"
MANIFEST_GATE_TIMEOUT_SECONDS = 120
MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS = 90
MACRO_RECOVERY_WINDOWS_PT = {
    # Watchdog runs one minute after the ordinary 05:45 terminal objective. A recovered run is
    # still late for that SLO, but remains useful to the scheduled Morning research input.
    "watchdog": (dtime(5, 46), dtime(5, 48)),
    # If sleep/wake delivery also missed the watchdog, the Morning supervisor gets one final
    # pre-child opportunity. It starts before the Morning runner/model and is tightly bounded.
    "morning": (dtime(5, 49), dtime(5, 52)),
}
WORKFLOWS = ("intake", "macro_refresh", "morning", "post_open", "post_close")
# ProcessType is a THROUGHPUT declaration, not a safety property, and it is per-lane.
# `Background` puts a job in the macOS discretionary QoS band, where CPU and I/O are
# throttled. That is correct for the watchdog and for the short macro/post-* lanes. It is
# wrong for the 05:00 intake: a CPU-bound data-lake ingest + ~13k-symbol security-master
# rollforward on a HARD 900s supervisor budget. Measured on the deployment machine
# 2026-08-12, the rollforward stage alone costs 22.1s unthrottled vs 102.1s under
# `taskpolicy -b` (4.6x), and that session was SIGTERMed at the budget (exit 124) with the
# rollforward receipt written 15s before the kill. `Standard` is launchd's DEFAULT band;
# this removes an explicit de-prioritisation rather than granting elevated priority.
# The contract below stays an EXACT match against a declared value -- a lane absent from
# this map still MUST be Background, and an unknown/missing ProcessType still fails.
DEFAULT_PROCESS_TYPE = "Background"
EXPECTED_PROCESS_TYPE = {
    "intake": "Standard",
}
ALLOWED_RUNNERS = {
    "intake": "scripts/run_data_intake.sh",
    "macro_refresh": "scripts/run_macro_actual_refresh.sh",
    "morning": "scripts/run_morning_scan.sh",
    "post_open": "scripts/run_premarket_refresh.sh",
    "post_close": "scripts/run_post_close_review.sh",
}
ALLOWED_TASK_KEYS = {
    "intake": "data_intake",
    "macro_refresh": "macro_actual_refresh",
    "morning": "morning_scan",
    "post_open": "premarket_refresh",
    "post_close": "post_close_review",
}
EXPECTED_LABELS = {
    "intake": "com.charles.trading.intake",
    "macro_refresh": "com.charles.trading.macro-refresh",
    "morning": "com.charles.trading.morning",
    "post_open": "com.charles.trading.premarket",
    "post_close": "com.charles.trading.postclose",
}
EXPECTED_SCHEDULES = {
    "intake": "05:00", "macro_refresh": "05:35", "morning": "05:38",
    "post_open": "06:45", "post_close": "13:15",
}
EXPECTED_PHASE_GATES = {
    "intake": "pre_market_intake",
    "macro_refresh": "post_release_macro_refresh",
    "morning": "delegate_to_runner",
    "post_open": "post_open_regular_session",
    "post_close": "after_session_close",
}
EXPECTED_CONTRACTS = {
    "intake": "data_readiness_receipt",
    "macro_refresh": "macro_actual_refresh_receipt",
    "morning": "morning_terminal_singleton",
    "post_open": "post_open_terminal_receipt_or_skip",
    "post_close": "post_close_report_or_maintenance_receipt",
}
EXPECTED_DEPENDENCY_POLICIES = {
    "intake": "none",
    "macro_refresh": "require_data_readiness_receipt",
    "morning": "none",
    "post_open": "require_official_morning_artifact",
    # A valid Official keeps the narrative path. Exact canonical absence admits only the
    # supervised deterministic maintenance path; any present-invalid state still blocks.
    "post_close": "require_official_or_exact_absence_maintenance",
}
ALLOWED_CONTRACTS = {
    "data_readiness_receipt",
    "macro_actual_refresh_receipt",
    "morning_terminal_singleton",
    "post_open_terminal_receipt_or_skip",
    "post_close_report_or_maintenance_receipt",
}
ALLOWED_PHASE_GATES = {
    "pre_market_intake",
    "post_release_macro_refresh",
    "delegate_to_runner",
    "post_open_regular_session",
    "after_session_close",
}
TERMINAL_STATES = {"completed", "degraded", "skipped", "failed", "blocked"}
MAX_EVENTS = 1000
MAX_ATTEMPTS_PER_WORKFLOW = 100
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,180}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MACRO_RUN_ID_RE = re.compile(r"^macro-\d{8}T\d{6}Z-[0-9a-f]{12}$")
RUN_ID_PREFIX = {
    "intake": "intake", "macro_refresh": "macro", "morning": "morning",
    "post_open": "postopen",
    "post_close": "postclose",
}
TOP_LEVEL_CONFIG_KEYS = {
    "schema", "version", "operating_timezone", "activation", "safety", "workflows",
}
WORKFLOW_CONFIG_KEYS = {
    "runner", "task_state_key", "launchd_label", "scheduled_pt", "phase_gate",
    "timeout_seconds", "dependencies", "dependency_policy", "artifact_contract",
    "run_id_prefix",
}

# Explicit child environment surface. TRADING_* operator controls are retained except anything
# secret-like. Direct provider credentials are loaded by the existing runner only for its bounded
# deterministic collector and are never inherited from the supervisor environment.
BASE_ENV_KEYS = {
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "PYTHONUNBUFFERED",
}
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH", "CREDENTIAL")


class ControlError(RuntimeError):
    """A safe, expected fail-closed control-plane refusal."""


def _safe_token(value: Any, fallback: str = "invalid_token") -> str:
    text = str(value or "")
    return text if SAFE_TOKEN.fullmatch(text) else fallback


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@contextlib.contextmanager
def _state_lock(state_path: Path) -> Iterator[None]:
    """Cross-process lock for one target state file; the persistent lock file has no data."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.parent / f".{state_path.name}.lock"
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _initial_state(target: str, config_sha256: str) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "target_session_date": target,
        "operating_timezone": "America/Los_Angeles",
        "research_only": True,
        "execution_enabled": False,
        "activation_authority": False,
        "strategy_change_allowed": False,
        "config_sha256": config_sha256,
        "revision": 0,
        "overall_state": "pending",
        "workflows": {
            name: {"state": "pending", "latest_run_id": None, "attempts": []}
            for name in WORKFLOWS
        },
        "events": [],
        "updated_utc": tzutil.stamps()["utc_iso"],
    }


def _validate_state(doc: dict, target: str) -> None:
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        raise ControlError("state_schema_invalid")
    if doc.get("target_session_date") != target:
        raise ControlError("state_target_mismatch")
    if doc.get("research_only") is not True or doc.get("execution_enabled") is not False:
        raise ControlError("state_safety_flags_invalid")
    workflows = doc.get("workflows")
    if not isinstance(workflows, dict) or set(workflows) != set(WORKFLOWS):
        raise ControlError("state_workflows_invalid")
    if not isinstance(doc.get("events"), list):
        raise ControlError("state_events_invalid")
    for name in WORKFLOWS:
        wf = workflows[name]
        if not isinstance(wf, dict) or not isinstance(wf.get("attempts"), list):
            raise ControlError(f"state_workflow_invalid:{name}")


def _load_state(path: Path, target: str, config_sha256: str) -> dict:
    if not path.exists():
        return _initial_state(target, config_sha256)
    try:
        doc = json.loads(path.read_text())
    except Exception as exc:
        raise ControlError("state_unreadable") from exc
    # Additive v1.2 migration: preserve all prior attempts/events and introduce the new
    # deterministic macro lane as pending. Existing historical state is never relabeled.
    workflows = doc.get("workflows") if isinstance(doc, dict) else None
    legacy = {"intake", "morning", "post_open", "post_close"}
    if isinstance(workflows, dict) and set(workflows) == legacy:
        workflows["macro_refresh"] = {
            "state": "pending", "latest_run_id": None, "attempts": []}
    _validate_state(doc, target)
    return doc


def _aware_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _watchdog_no_dispatch_handoff_run_id(
        doc: dict, target: str, now: datetime,
        current_config_sha256: str) -> str | None:
    """Return the sole watchdog reservation eligible for a Morning handoff.

    This recognizes one exact no-child terminal produced only when all ordinary preflight gates
    passed but the watchdog's final just-in-time dispatch check crossed 05:48 PT. Any ambiguity,
    extra macro event, altered metadata, child evidence, or other terminal remains non-retryable.
    """
    if (not isinstance(doc, dict)
            or not isinstance(current_config_sha256, str)
            or SHA256_RE.fullmatch(current_config_sha256) is None
            or doc.get("config_sha256") != current_config_sha256):
        return None
    workflows = doc.get("workflows")
    workflow = workflows.get("macro_refresh") if isinstance(workflows, dict) else None
    if not isinstance(workflow, dict) or set(workflow) != {
            "state", "latest_run_id", "attempts"}:
        return None
    attempts = workflow.get("attempts")
    if (workflow.get("state") != "skipped" or not isinstance(attempts, list)
            or len(attempts) != 1 or not isinstance(attempts[0], dict)):
        return None
    attempt = attempts[0]
    attempt_keys = {
        "run_id", "config_sha256", "state", "outcome", "dry_run", "owner_pid",
        "child_pid", "started_utc", "finished_utc", "exit_code", "preflight",
        "postconditions",
    }
    if set(attempt) != attempt_keys:
        return None
    run_id = attempt.get("run_id")
    if (not isinstance(run_id, str) or not MACRO_RUN_ID_RE.fullmatch(run_id)
            or workflow.get("latest_run_id") != run_id
            or attempt.get("config_sha256") != current_config_sha256
            or attempt.get("state") != "skipped"
            or attempt.get("outcome") != "after_watchdog_macro_recovery_window"
            or attempt.get("dry_run") is not False
            or not isinstance(attempt.get("owner_pid"), int)
            or isinstance(attempt.get("owner_pid"), bool)
            or attempt.get("owner_pid") <= 1
            or attempt.get("child_pid") is not None
            or attempt.get("exit_code") != 0
            or attempt.get("postconditions") is not None):
        return None
    preflight = attempt.get("preflight")
    expected_preflight_keys = {
        "continuity_recovery", "recovery_trigger", "recovery_only_if_never_started",
        "recovery_child_timeout_seconds", "trading_day", "market_day_reason",
        "integrity_manifest", "manifest_validator_returncode", "manifest_check",
        "broker_permissions", "broker_validator_returncode", "strict_read_only",
        "dependency_gate", "dependency_reason", "phase_gate", "phase_reason",
        "recovery_dispatch_gate", "recovery_dispatch_reason",
    }
    if not isinstance(preflight, dict) or set(preflight) != expected_preflight_keys:
        return None
    market_day_reason = preflight.get("market_day_reason")
    if market_day_reason not in {"regular_trading_day", "early_close_trading_day"}:
        return None
    expected_preflight = {
        "continuity_recovery": True,
        "recovery_trigger": "watchdog",
        "recovery_only_if_never_started": True,
        "recovery_child_timeout_seconds": MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS,
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
    if preflight != expected_preflight:
        return None
    started = _aware_iso(attempt.get("started_utc"))
    finished = _aware_iso(attempt.get("finished_utc"))
    if started is None or finished is None or finished < started:
        return None
    started_pt = tzutil.to_pt(started)
    finished_pt = tzutil.to_pt(finished)
    now_utc = tzutil.to_utc(now)
    if (started_pt.date().isoformat() != target
            or finished_pt.date().isoformat() != target
            or not (dtime(5, 46) <= started_pt.time() <= dtime(5, 48))
            or not (dtime(5, 48) < finished_pt.time() <= dtime(5, 52))
            or tzutil.to_utc(finished) > now_utc):
        return None
    events = doc.get("events")
    event_keys = {"seq", "utc", "workflow", "run_id", "event", "reason"}
    if (not isinstance(events, list)
            or any(not isinstance(event, dict) or set(event) != event_keys
                   for event in events)
            or any(not isinstance(event.get("seq"), int)
                   or isinstance(event.get("seq"), bool) for event in events)
            or [event.get("seq") for event in events] != list(range(len(events)))):
        return None
    macro_events = [
        event for event in events if event.get("workflow") == "macro_refresh"]
    if len(macro_events) != 2:
        return None
    started_event, finished_event = macro_events
    if (started_event.get("run_id") != run_id
            or started_event.get("event") != "attempt_started"
            or started_event.get("reason") != "production"
            or started_event.get("utc") != attempt.get("started_utc")
            or finished_event.get("run_id") != run_id
            or finished_event.get("event") != "attempt_finished"
            or finished_event.get("reason") != attempt.get("outcome")
            or finished_event.get("utc") != attempt.get("finished_utc")
            or not isinstance(started_event.get("seq"), int)
            or isinstance(started_event.get("seq"), bool)
            or not isinstance(finished_event.get("seq"), int)
            or isinstance(finished_event.get("seq"), bool)
            or started_event["seq"] >= finished_event["seq"]):
        return None
    return run_id


def _append_event(doc: dict, workflow: str, run_id: str, event: str,
                  reason: str, now: datetime) -> None:
    events = doc["events"]
    if len(events) >= MAX_EVENTS:
        raise ControlError("state_event_limit_reached")
    events.append({
        "seq": len(events),
        "utc": tzutil.to_utc(now).isoformat(),
        "workflow": workflow,
        "run_id": run_id,
        "event": _safe_token(event),
        "reason": _safe_token(reason),
    })


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _overall_state(doc: dict) -> str:
    states = [doc["workflows"][name]["state"] for name in WORKFLOWS]
    if any(s in {"failed", "blocked"} for s in states):
        return "at_issue"
    if all(s in TERMINAL_STATES for s in states):
        return "complete"
    if any(s == "running" for s in states):
        return "running"
    if any(s in TERMINAL_STATES for s in states):
        return "in_progress"
    return "pending"


def _persist_state(path: Path, doc: dict) -> None:
    doc["revision"] = int(doc.get("revision", 0)) + 1
    doc["overall_state"] = _overall_state(doc)
    doc["updated_utc"] = tzutil.stamps()["utc_iso"]
    _atomic_write_json(path, doc)


def _start_attempt(path: Path, target: str, workflow: str, run_id: str,
                   config_sha256: str, dry_run: bool, now: datetime,
                   *, only_if_never_started: bool = False,
                   allow_watchdog_no_dispatch_handoff: bool = False) -> tuple[dict, dict]:
    with _state_lock(path):
        doc = _load_state(path, target, config_sha256)
        wf = doc["workflows"][workflow]
        attempts = wf["attempts"]
        pristine = (not attempts and wf.get("state") == "pending"
                    and wf.get("latest_run_id") is None)
        handoff_from = None
        if (only_if_never_started and not pristine
                and allow_watchdog_no_dispatch_handoff and workflow == "macro_refresh"):
            handoff_from = _watchdog_no_dispatch_handoff_run_id(
                doc, target, now, config_sha256)
        if workflow == "macro_refresh" and not pristine and handoff_from is None:
            # Macro refresh is one-shot across ordinary and continuity invocations. A dead owner,
            # inconsistent top-level state, or any other recorded history is ambiguous evidence
            # that real BLS work may already have happened, so generic stale-owner recovery must
            # never turn it into a replay. The sole exception is the exact, hash-bound watchdog
            # no-dispatch handoff proven above.
            if only_if_never_started:
                raise ControlError("workflow_already_attempted")
            if (isinstance(attempts, list) and attempts
                    and isinstance(attempts[-1], dict)
                    and wf.get("state") == "running"
                    and wf.get("latest_run_id") == attempts[-1].get("run_id")
                    and attempts[-1].get("state") == "running"
                    and attempts[-1].get("outcome") is None
                    and attempts[-1].get("finished_utc") is None
                    and attempts[-1].get("exit_code") is None):
                raise ControlError("workflow_already_running")
            if (isinstance(attempts, list) and attempts
                    and isinstance(attempts[-1], dict)
                    and wf.get("state") in TERMINAL_STATES
                    and wf.get("latest_run_id") == attempts[-1].get("run_id")
                    and attempts[-1].get("state") == wf.get("state")
                    and isinstance(attempts[-1].get("outcome"), str)
                    and _aware_iso(attempts[-1].get("finished_utc")) is not None
                    and (isinstance(attempts[-1].get("exit_code"), int)
                         and not isinstance(attempts[-1].get("exit_code"), bool))):
                raise ControlError("workflow_already_terminal")
            raise ControlError("workflow_prior_state_ambiguous")
        if workflow == "post_close" and not pristine:
            # Post-close is also one-shot. Its canonical report, immutable cutoff envelope, and
            # terminal receipt all describe exactly one supervised attempt; a replay would make
            # those evidence planes disagree. Preserve any prior bytes and surface running or
            # malformed history explicitly instead of applying generic stale-owner recovery.
            latest = attempts[-1] if (isinstance(attempts, list) and len(attempts) == 1
                                      and isinstance(attempts[-1], dict)) else None
            if (latest is not None and wf.get("state") == "running"
                    and wf.get("latest_run_id") == latest.get("run_id")
                    and latest.get("state") == "running"
                    and latest.get("outcome") is None
                    and latest.get("finished_utc") is None
                    and latest.get("exit_code") is None):
                raise ControlError("workflow_already_running")
            if (latest is not None and wf.get("state") in TERMINAL_STATES
                    and wf.get("latest_run_id") == latest.get("run_id")
                    and latest.get("state") == wf.get("state")
                    and isinstance(latest.get("outcome"), str)
                    and _aware_iso(latest.get("finished_utc")) is not None
                    and isinstance(latest.get("exit_code"), int)
                    and not isinstance(latest.get("exit_code"), bool)):
                raise ControlError("workflow_already_terminal")
            raise ControlError("post_close_prior_state_ambiguous")
        if only_if_never_started and not pristine and handoff_from is None:
            # The continuity lane is missing-run recovery, never retry authority. This check is
            # under the same lock as attempt creation. The sole exception is a Morning handoff
            # from an exact watchdog reservation that never dispatched a child.
            raise ControlError("workflow_already_attempted")
        if len(attempts) >= MAX_ATTEMPTS_PER_WORKFLOW:
            raise ControlError("workflow_attempt_limit_reached")
        if wf.get("state") == "running" and attempts:
            prior = attempts[-1]
            if _pid_alive(prior.get("owner_pid")):
                raise ControlError("workflow_already_running")
            prior.update({
                "state": "failed",
                "outcome": "stale_owner_recovered",
                "finished_utc": tzutil.to_utc(now).isoformat(),
                "exit_code": None,
            })
            _append_event(doc, workflow, str(prior.get("run_id") or "unknown"),
                          "stale_owner_recovered", "owner_pid_not_alive", now)
        if doc.get("config_sha256") != config_sha256:
            _append_event(doc, workflow, run_id, "config_revision_changed",
                          "config_sha256_changed", now)
            doc["config_sha256"] = config_sha256
        attempt = {
            "run_id": run_id,
            "config_sha256": config_sha256,
            "state": "running",
            "outcome": None,
            "dry_run": bool(dry_run),
            "owner_pid": os.getpid(),
            "child_pid": None,
            "started_utc": tzutil.to_utc(now).isoformat(),
            "finished_utc": None,
            "exit_code": None,
            "preflight": ({
                "watchdog_no_dispatch_handoff": True,
                "handoff_from_run_id": handoff_from,
            } if handoff_from is not None else {}),
            "postconditions": None,
        }
        attempts.append(attempt)
        wf["state"] = "running"
        wf["latest_run_id"] = run_id
        _append_event(doc, workflow, run_id, "attempt_started",
                      "dry_run" if dry_run else "production", now)
        if handoff_from is not None:
            _append_event(doc, workflow, run_id, "watchdog_no_dispatch_handoff",
                          "prior_watchdog_window_expired", now)
        _persist_state(path, doc)
        return doc, attempt


def _update_attempt(path: Path, target: str, workflow: str, run_id: str,
                    config_sha256: str, updates: dict, event: str | None = None,
                    reason: str = "updated", now: datetime | None = None) -> dict:
    now = now or tzutil.now_utc()
    with _state_lock(path):
        doc = _load_state(path, target, config_sha256)
        wf = doc["workflows"][workflow]
        attempt = next((a for a in reversed(wf["attempts"])
                        if a.get("run_id") == run_id), None)
        if attempt is None:
            raise ControlError("attempt_not_found")
        attempt.update(updates)
        if "state" in updates:
            wf["state"] = updates["state"]
        if event:
            _append_event(doc, workflow, run_id, event, reason, now)
        _persist_state(path, doc)
        return doc


def validate_config(config_path: Path = CONFIG_DEFAULT, root: Path = ROOT) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    try:
        doc = json.loads(config_path.read_text())
    except Exception:
        return None, ["config_unreadable"]
    if not isinstance(doc, dict) or doc.get("schema") != CONFIG_SCHEMA:
        errors.append("config_schema_invalid")
        return doc if isinstance(doc, dict) else None, errors
    if set(doc) != TOP_LEVEL_CONFIG_KEYS:
        errors.append("config_top_level_keys_invalid")
    if doc.get("version") != CONFIG_VERSION:
        errors.append("config_version_invalid")
    if doc.get("operating_timezone") != "America/Los_Angeles":
        errors.append("operating_timezone_invalid")
    activation = doc.get("activation") or {}
    if activation != {"mode": "opt_in_environment",
                       "environment_variable": "TRADING_AUTONOMY_ENABLE",
                       "required_value": "1"}:
        errors.append("activation_contract_invalid")
    safety = doc.get("safety") or {}
    if not isinstance(safety, dict):
        errors.append("safety_object_invalid")
        safety = {}
    expected_safety = {
        "research_only": True,
        "execution_enabled": False,
        "broker_access": "strict_read_only",
        "integrity_manifest_required": True,
        "subprocess_shell": False,
        "supervisor_max_attempts": 1,
        "supervisor_retry_enabled": False,
        "allow_dynamic_commands": False,
        "allow_strategy_activation": False,
        "allow_risk_limit_changes": False,
    }
    if set(safety) != set(expected_safety):
        errors.append("safety_keys_invalid")
    for key, expected in expected_safety.items():
        if safety.get(key) != expected:
            errors.append(f"safety_flag_invalid:{key}")
    workflows = doc.get("workflows")
    if not isinstance(workflows, dict) or set(workflows) != set(WORKFLOWS):
        errors.append("workflow_set_invalid")
        return doc, errors
    for name in WORKFLOWS:
        wf = workflows.get(name) or {}
        if not isinstance(wf, dict):
            errors.append(f"workflow_object_invalid:{name}")
            wf = {}
        if set(wf) != WORKFLOW_CONFIG_KEYS:
            errors.append(f"workflow_keys_invalid:{name}")
        if wf.get("runner") != ALLOWED_RUNNERS[name]:
            errors.append(f"runner_not_allowlisted:{name}")
        if wf.get("task_state_key") != ALLOWED_TASK_KEYS[name]:
            errors.append(f"task_state_key_invalid:{name}")
        if wf.get("launchd_label") != EXPECTED_LABELS[name]:
            errors.append(f"launchd_label_invalid:{name}")
        if wf.get("run_id_prefix") != RUN_ID_PREFIX[name]:
            errors.append(f"run_id_prefix_invalid:{name}")
        if (wf.get("artifact_contract") not in ALLOWED_CONTRACTS
                or wf.get("artifact_contract") != EXPECTED_CONTRACTS[name]):
            errors.append(f"artifact_contract_invalid:{name}")
        if (wf.get("phase_gate") not in ALLOWED_PHASE_GATES
                or wf.get("phase_gate") != EXPECTED_PHASE_GATES[name]):
            errors.append(f"phase_gate_invalid:{name}")
        if (not TIME_RE.fullmatch(str(wf.get("scheduled_pt") or ""))
                or wf.get("scheduled_pt") != EXPECTED_SCHEDULES[name]):
            errors.append(f"schedule_invalid:{name}")
        timeout = wf.get("timeout_seconds")
        if not isinstance(timeout, int) or not 30 <= timeout <= 5400:
            errors.append(f"timeout_invalid:{name}")
        deps = wf.get("dependencies")
        if not isinstance(deps, list) or any(d not in WORKFLOWS or d == name for d in deps):
            errors.append(f"dependencies_invalid:{name}")
        expected_deps = (["intake"] if name == "macro_refresh" else
                         ["morning"] if name in ("post_open", "post_close") else [])
        if deps != expected_deps:
            errors.append(f"dependencies_contract_invalid:{name}")
        if wf.get("dependency_policy") != EXPECTED_DEPENDENCY_POLICIES[name]:
            errors.append(f"dependency_policy_invalid:{name}")
        runner = (root / ALLOWED_RUNNERS[name]).resolve()
        try:
            runner.relative_to(root.resolve())
        except ValueError:
            errors.append(f"runner_outside_root:{name}")
        if not runner.is_file():
            errors.append(f"runner_missing:{name}")
    # Explicit DAG cycle check even though the allowed workflow set is small.
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            errors.append("dependency_cycle")
            return
        if node in visited:
            return
        visiting.add(node)
        node_config = workflows.get(node)
        deps = node_config.get("dependencies", []) if isinstance(node_config, dict) else []
        deps = deps if isinstance(deps, list) else []
        for dep in deps:
            if dep in WORKFLOWS:
                visit(dep)
        visiting.remove(node)
        visited.add(node)
    for name in WORKFLOWS:
        visit(name)
    return doc, sorted(set(errors))


def _parse_json_stdout(proc: subprocess.CompletedProcess[str]) -> dict:
    try:
        out = json.loads(proc.stdout)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _run_gate(command: list[str], *, cwd: Path, timeout: int = 30) -> tuple[int, dict]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              cwd=str(cwd), env=_child_env(False, control_only=True))
        return proc.returncode, _parse_json_stdout(proc)
    except subprocess.TimeoutExpired:
        return 124, {}
    except Exception:
        return 127, {}


def _trading_day_gate(target: str, root: Path) -> tuple[str, dict]:
    script = root / "scripts" / "validate_market_day.py"
    rc, out = _run_gate([sys.executable, str(script), "--date", target], cwd=root)
    if rc == 0 and out.get("is_trading_day") is True:
        return "pass", out
    if rc == 1 and out.get("is_trading_day") is False:
        return "skip", out
    return "fail", out


def _broker_permission_gate(root: Path) -> tuple[str, dict]:
    script = root / "scripts" / "validate_broker_permissions.py"
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                              timeout=30, cwd=str(root), env=_child_env(False, control_only=True))
        return ("pass" if proc.returncode == 0 else "fail"), {
            "returncode": proc.returncode,
            "strict_read_only": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return "fail", {"returncode": 124, "strict_read_only": False}
    except Exception:
        return "fail", {"returncode": 127, "strict_read_only": False}


def _integrity_manifest_gate(root: Path) -> tuple[str, dict]:
    script = root / "scripts" / "build_manifest.py"
    # A cold Mac can launch several missed workflows together after wake. Each process performs
    # the same full repository hash audit, so the generic 30-second gate budget is too small under
    # transient disk contention. The integrity criteria stay identical and fail closed; only this
    # bounded wait is widened so a valid audit can finish before the runner is considered.
    rc, out = _run_gate(
        [sys.executable, str(script), "--check"],
        cwd=root,
        timeout=MANIFEST_GATE_TIMEOUT_SECONDS,
    )
    passed = rc == 0 and out.get("check") == "PASS"
    return ("pass" if passed else "fail"), {
        "returncode": rc,
        "manifest_check": out.get("check") or "unavailable",
    }


def _session_close_pt(target: str, market_day: dict) -> datetime:
    close_et = str(market_day.get("session_close_et") or "")
    if not TIME_RE.fullmatch(close_et):
        raise ControlError("session_close_unavailable")
    hh, mm = (int(x) for x in close_et.split(":"))
    et = datetime.fromisoformat(target).replace(hour=hh, minute=mm, tzinfo=tzutil.ET)
    return tzutil.to_pt(et)


def _phase_gate(workflow: str, target: str, now: datetime, market_day: dict,
                dry_run: bool) -> tuple[str, str]:
    if dry_run:
        return "pass", "dry_run_time_gate_bypass"
    pt = tzutil.to_pt(now)
    if pt.date().isoformat() != target:
        return "fail", "target_not_current_pt_date"
    if workflow == "intake":
        earliest = pt.replace(hour=4, minute=45, second=0, microsecond=0)
        latest = pt.replace(hour=5, minute=30, second=0, microsecond=0)
        if pt < earliest:
            return "skip", "before_intake_window"
        if pt > latest:
            return "skip", "after_intake_window"
        return "pass", "pre_market_intake_window"
    if workflow == "macro_refresh":
        earliest = pt.replace(hour=5, minute=30, second=0, microsecond=0)
        latest = pt.replace(hour=5, minute=49, second=59, microsecond=999999)
        if pt < earliest:
            return "skip", "before_macro_refresh_window"
        if pt > latest:
            return "skip", "after_macro_refresh_window"
        return "pass", "post_release_macro_refresh_window"
    if workflow == "morning":
        return "pass", "delegated_to_morning_stage_gate"
    close_pt = _session_close_pt(target, market_day)
    if workflow == "post_open":
        earliest = pt.replace(hour=6, minute=45, second=0, microsecond=0)
        if pt < earliest:
            return "skip", "before_post_open_window"
        if pt >= close_pt:
            return "skip", "after_session_close"
        return "pass", "post_open_window"
    if workflow == "post_close":
        if pt < close_pt:
            return "skip", "before_session_close"
        return "pass", "after_session_close"
    return "fail", "unknown_workflow"


def _dependency_gate(workflow: str, target: str, root: Path, dry_run: bool) -> tuple[str, str]:
    if dry_run:
        return "pass", "not_required"
    if workflow == "macro_refresh":
        path = root / "data" / "runtime" / "data_lake" / "readiness" / f"{target}.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "fail", "data_readiness_receipt_unavailable"
        if data_lake_intake.validate_receipt(receipt) \
                or receipt.get("target_session_date") != target:
            return "fail", "data_readiness_receipt_invalid"
        return "pass", "data_readiness_receipt_valid"
    if workflow not in ("post_open", "post_close"):
        return "pass", "not_required"
    try:
        official, _kind, reason = _official_morning_artifact(target, root)
    except Exception as exc:
        # Fail CLOSED rather than letting the exception escape: an escaping error would leave
        # the attempt row in `running` and wedge the one-shot post_close workflow for the whole
        # session with no PID-liveness recovery (2026-07-28 review A13).
        return "fail", f"dependency_gate_error:{type(exc).__name__}"
    if official is not None:
        return "pass", "official_morning_present"
    if reason == "no_official_morning_artifact":
        # post_open has no useful artifact contract without Morning authority and retains its
        # pre-child skip. Post-close may run only its fixed deterministic maintenance stages;
        # the second resolver and shell re-check below prevent an absence/presence TOCTOU.
        return ("pass", reason) if workflow == "post_close" else ("skip", reason)
    return "fail", reason


def _official_morning_artifact(target: str, root: Path) -> tuple[Path | None, str, str]:
    """Resolve only a fully re-anchored governance-v2 morning artifact.

    Delegates to the shared canonical predicate (scripts/morning_authority.py) so post_open,
    post_close, and the shell defense-in-depth gates can never diverge (2026-07-28 incident).
    """
    return morning_authority.resolve_official_morning_artifact(
        target, root, now=datetime.now(timezone.utc))


def _system_task_state(root: Path, task_key: str) -> dict:
    try:
        doc = json.loads((root / "data" / "system_state.json").read_text())
        state = (doc.get("runs") or {}).get(task_key) or {}
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _task_state_is_fresh(task_state: dict, run_start_epoch: float) -> bool:
    try:
        stamp = datetime.fromisoformat(str(task_state.get("timestamp_utc") or ""))
        if stamp.tzinfo is None:
            return False
        return stamp.timestamp() >= run_start_epoch
    except (TypeError, ValueError):
        return False


def _macro_receipt_observation(target: str, root: Path,
                               now: datetime | None = None) -> dict:
    """Observe one target macro receipt without modifying or trusting its bytes."""
    artifact = root / "data" / "runtime" / "macro_actual_refresh" / f"{target}.json"
    base = {
        "status": "absent",
        "receipt_status": None,
        "input_available": False,
        "artifact": str(artifact.relative_to(root)),
        "artifact_file_sha256": None,
        "receipt_artifact_sha256": None,
        "validation_errors": [],
    }
    if not artifact.exists() and not artifact.is_symlink():
        return base
    file_sha = _sha256_file(artifact)
    receipt, errors = macro_actual_refresh.load_valid_receipt(artifact, target=target)
    if receipt is None or errors:
        return {
            **base,
            "status": "invalid",
            "artifact_file_sha256": file_sha,
            "validation_errors": sorted(set(errors or ["receipt_unreadable"])),
        }
    generated = _aware_iso(receipt.get("generated_utc"))
    observed_at = now or tzutil.now_utc()
    if generated is None or tzutil.to_utc(generated) > tzutil.to_utc(observed_at) + timedelta(
            minutes=1):
        return {
            **base,
            "status": "invalid",
            "artifact_file_sha256": file_sha,
            "validation_errors": ["receipt_generated_in_future"],
        }
    receipt_status = receipt.get("status")
    return {
        **base,
        "status": "valid",
        "receipt_status": receipt_status,
        "input_available": receipt_status in {"ready", "degraded", "no_due_events"},
        "artifact_file_sha256": file_sha,
        "receipt_artifact_sha256": receipt.get("artifact_sha256"),
    }


def _macro_prior_summary(state_path: Path, target: str, config_sha256: str) -> dict:
    """Return bounded current macro history after a no-dispatch refusal."""
    try:
        with _state_lock(state_path):
            document = _load_state(state_path, target, config_sha256)
            workflow = document["workflows"]["macro_refresh"]
            attempts = workflow.get("attempts")
            latest = attempts[-1] if isinstance(attempts, list) and attempts \
                and isinstance(attempts[-1], dict) else {}
            return {
                "prior_state": _safe_token(workflow.get("state"), "invalid"),
                "prior_attempt_count": len(attempts) if isinstance(attempts, list) else None,
                "prior_latest_run_id": _safe_token(
                    workflow.get("latest_run_id"), "unavailable"),
                "prior_outcome": _safe_token(latest.get("outcome"), "unavailable"),
            }
    except ControlError as exc:
        return {
            "prior_state": "invalid",
            "prior_attempt_count": None,
            "prior_latest_run_id": "unavailable",
            "prior_outcome": _safe_token(str(exc), "state_unavailable"),
        }


def _postconditions(workflow: str, target: str, root: Path, run_start_epoch: float,
                    dry_run: bool, task_key: str, *, run_id: str | None = None) -> dict:
    task_state = _system_task_state(root, task_key)
    task_status = str(task_state.get("status") or "unknown")
    task_detail = _safe_token(task_state.get("detail"), "unavailable")
    task_state_fresh = _task_state_is_fresh(task_state, run_start_epoch)
    base = {"task_status": task_status, "task_detail": task_detail,
            "task_state_fresh": task_state_fresh,
            "activation_authority": False, "execution_enabled": False}
    if dry_run:
        if task_status == "dry_run_ok" and task_state_fresh:
            return {**base, "status": "pass", "reason": "dry_run_contract"}
        return {**base, "status": "fail", "reason": "dry_run_state_missing_or_stale"}
    if not task_state_fresh:
        return {**base, "status": "fail", "reason": "runner_state_missing_or_stale"}
    if (workflow == "post_close" and task_status == "skipped"
            and task_detail == post_close_maintenance_receipt.ABSENT_REASON):
        if not isinstance(run_id, str) or not run_id:
            return {**base, "status": "fail",
                    "reason": "post_close_supervisor_run_id_missing"}
        receipt_path = post_close_maintenance_receipt.receipt_path(
            target, run_id, root)
        receipt_errors = post_close_maintenance_receipt.validate_receipt(
            target=target, outer_run_id=run_id, root=root, expected_state="running")
        if receipt_errors:
            return {
                **base,
                "status": "fail",
                "reason": "post_close_maintenance_receipt_invalid",
                "maintenance_receipt": str(receipt_path.relative_to(root.resolve())),
            }
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {**base, "status": "fail",
                    "reason": "post_close_maintenance_receipt_unreadable"}
        relative = str(receipt_path.relative_to(root.resolve()))
        return {
            **base,
            "status": "skip",
            "reason": post_close_maintenance_receipt.ABSENT_REASON,
            "artifact": relative,
            "maintenance_receipt": relative,
            "maintenance_receipt_sha256": _sha256_file(receipt_path),
            "maintenance_artifact_sha256": receipt.get("artifact_sha256"),
            "maintenance_attempted_stage_count": sum(
                1 for row in receipt.get("stages") or [] if row.get("invoked") is True),
            "maintenance_zero_return_stage_count": sum(
                1 for row in receipt.get("stages") or []
                if row.get("invoked") is True and row.get("returncode") == 0),
            "maintenance_nonzero_return_stage_count": sum(
                1 for row in receipt.get("stages") or []
                if row.get("invoked") is True and row.get("returncode") != 0),
            "maintenance_output_regular_file_count": sum(
                1 for row in receipt.get("output_observations") or []
                if row.get("state") == "regular_file"),
            "maintenance_output_absent_count": sum(
                1 for row in receipt.get("output_observations") or []
                if row.get("state") == "absent"),
        }
    if task_status == "skipped":
        return {**base, "status": "skip", "reason": "runner_recorded_skip"}
    if task_status == "failed":
        return {**base, "status": "fail", "reason": "runner_recorded_failure"}
    # `completed_degraded` (2026-08-04): the runner reached its own terminal state but the day's
    # research/authorization outcome was degraded. It is a first-class, ACCEPTED runner status —
    # not an unexpected one — and it maps to `warn`, so the workflow terminal becomes
    # state="degraded" with the postcondition reason as its outcome. It can never reach the
    # orchestrator's `contract_satisfied` branch.
    if task_status not in {"success", "warning", "completed_degraded"}:
        return {**base, "status": "fail", "reason": "runner_state_unexpected"}
    if workflow == "morning" and task_detail == "late_window_no_report":
        return {**base, "status": "skip", "reason": "late_window_no_report"}
    accepted_status = "warn" if task_status in {"warning", "completed_degraded"} else "pass"
    if workflow == "intake":
        artifact = root / "data" / "runtime" / "data_lake" / "readiness" / f"{target}.json"
        if not artifact.is_file() or artifact.stat().st_mtime < run_start_epoch:
            return {**base, "status": "fail", "reason": "data_readiness_receipt_missing"}
        try:
            receipt = json.loads(artifact.read_text())
        except Exception:
            return {**base, "status": "fail", "reason": "data_readiness_receipt_unreadable"}
        if data_lake_intake.validate_receipt(receipt):
            return {**base, "status": "fail", "reason": "data_readiness_receipt_invalid"}
        receipt_status = receipt.get("status")
        if receipt_status == "ready" and task_status == "success":
            return {**base, "status": "pass", "reason": "data_readiness_ready",
                    "artifact": str(artifact.relative_to(root)),
                    "receipt_sha256": receipt.get("receipt_sha256")}
        if receipt_status == "degraded" and task_status == "warning":
            return {**base, "status": "warn", "reason": "data_readiness_degraded",
                    "artifact": str(artifact.relative_to(root)),
                    "receipt_sha256": receipt.get("receipt_sha256")}
        return {**base, "status": "fail", "reason": "data_readiness_state_mismatch"}
    if workflow == "macro_refresh":
        artifact = root / "data" / "runtime" / "macro_actual_refresh" / f"{target}.json"
        preserved_details = {
            "existing_macro_receipt_ready_preserved": "ready",
            "existing_macro_receipt_degraded_preserved": "degraded",
            "existing_macro_receipt_no_due_events_preserved": "no_due_events",
        }
        if not artifact.is_file():
            return {**base, "status": "fail", "reason": "macro_refresh_receipt_missing"}
        receipt, errors = macro_actual_refresh.load_valid_receipt(artifact, target=target)
        if receipt is None or errors:
            return {**base, "status": "fail", "reason": "macro_refresh_receipt_invalid"}
        receipt_status = receipt.get("status")
        preserved_status = preserved_details.get(task_detail)
        if artifact.stat().st_mtime < run_start_epoch and preserved_status != receipt_status:
            return {**base, "status": "fail", "reason": "macro_refresh_receipt_stale"}
        if receipt_status in {"ready", "no_due_events"} and task_status == "success":
            return {**base, "status": "pass", "reason": f"macro_refresh_{receipt_status}",
                    "artifact": str(artifact.relative_to(root)),
                    "artifact_sha256": receipt.get("artifact_sha256")}
        if receipt_status == "degraded" and task_status == "warning":
            return {**base, "status": "warn", "reason": "macro_refresh_degraded",
                    "artifact": str(artifact.relative_to(root)),
                    "artifact_sha256": receipt.get("artifact_sha256")}
        return {**base, "status": "fail", "reason": "macro_refresh_state_mismatch"}
    if workflow == "morning":
        res = terminal_reconcile.reconcile(
            target,
            reports_root=root / "reports",
            research_dir=root / "data" / "runtime" / "research" / target,
            run_start_epoch=run_start_epoch,
            apply=False,
        )
        record = res["record"]
        if res["rc"] == 0:
            accepted_kind = record.get("accepted_kind")
            candidate = None
            if accepted_kind in {
                    "v1_official_slot_receipt_valid",
                    "official_research_slot_receipt_valid",
                    "receipt_anchored_preliminary"}:
                candidate = candidate_artifact_health.check(
                    target, root=root,
                    ledger_path=root / "data" / "research_ledger.json")
                if candidate.get("artifact_present") is not True:
                    return {**base, "status": "fail",
                            "reason": "candidate_artifact_missing",
                            "accepted_terminal_artifact":
                                record.get("accepted_terminal_artifact"),
                            "accepted_kind": accepted_kind}
                if candidate.get("ok") is not True \
                        or candidate.get("artifact_valid") is not True:
                    return {**base, "status": "fail",
                            "reason": "candidate_artifact_invalid",
                            "accepted_terminal_artifact":
                                record.get("accepted_terminal_artifact"),
                            "accepted_kind": accepted_kind,
                            "candidate_health_label": candidate.get("health_label")}
                dropped = candidate.get("dropped_count")
                if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped != 0:
                    return {**base, "status": "fail",
                            "reason": "candidate_artifact_incomplete",
                            "accepted_terminal_artifact":
                                record.get("accepted_terminal_artifact"),
                            "accepted_kind": accepted_kind,
                            "candidate_dropped_count": dropped}
            response = {
                **base, "status": accepted_status,
                "reason": "terminal_singleton_candidate_bound"
                          if candidate is not None else "terminal_singleton",
                "accepted_terminal_artifact": record.get("accepted_terminal_artifact"),
                "accepted_kind": accepted_kind,
            }
            if candidate is not None:
                artifact = (root / "data" / "runtime" / "research" / target
                            / "candidates.json")
                response.update({
                    "candidate_artifact": str(artifact.relative_to(root)),
                    "candidate_artifact_sha256": _sha256_file(artifact),
                    "candidate_count": candidate.get("candidate_count"),
                    "candidate_source": candidate.get("source"),
                })
            return response
        return {**base, "status": "fail", "reason": _safe_token(record.get("status")),
                "accepted_terminal_artifact": record.get("accepted_terminal_artifact"),
                "duplicate_count": len(record.get("duplicates") or [])}
    if workflow == "post_open":
        if not isinstance(run_id, str) or not run_id:
            return {**base, "status": "fail",
                    "reason": "post_open_supervisor_run_id_missing"}
        terminal, terminal_errors = post_open_finality.read_validated_terminal(
            target=target, run_id=run_id, root=root,
            now=datetime.now(timezone.utc))
        if terminal is None or terminal_errors:
            detail = _safe_token(
                terminal_errors[0] if terminal_errors else "missing",
                "invalid")
            return {
                **base,
                "status": "fail",
                "reason": f"post_open_terminal_receipt_invalid:{detail}",
                "terminal_receipt": str(
                    post_open_finality.terminal_path(target, run_id, root)
                    .relative_to(root.resolve())),
            }
        receipt_path = post_open_finality.terminal_path(target, run_id, root)
        return {
            **base,
            "status": accepted_status,
            "reason": "post_open_terminal_receipt_validated",
            "artifact": terminal["report_path"],
            "artifact_sha256": terminal["report_sha256"],
            "terminal_receipt": str(receipt_path.relative_to(root.resolve())),
            "terminal_receipt_sha256": _sha256_file(receipt_path),
            "terminal_artifact_sha256": terminal["artifact_sha256"],
            "snapshot": terminal["snapshot_path"],
            "snapshot_sha256": terminal["snapshot_file_sha256"],
            "official_morning_artifact": terminal["accepted_morning_path"],
            "official_morning_kind": terminal["accepted_morning_kind"],
        }
    if workflow == "post_close":
        if not isinstance(run_id, str) or not run_id:
            return {**base, "status": "fail",
                    "reason": "post_close_supervisor_run_id_missing"}
        artifacts = sorted((root / "reports" / "post_close").glob(f"{target}*.md"))
        fresh = [p for p in artifacts if p.stat().st_mtime >= run_start_epoch]
        if fresh:
            report = fresh[-1]
            receipt_path = (root / "data" / "runtime" / "health"
                            / f"{target}_post_close_report_contract.json")
            if (not receipt_path.is_file()
                    or receipt_path.stat().st_mtime < run_start_epoch):
                return {**base, "status": "fail",
                        "reason": "post_close_contract_receipt_missing_or_stale",
                        "artifact": str(report.relative_to(root))}
            try:
                receipt = json.loads(receipt_path.read_text())
            except Exception:
                return {**base, "status": "fail",
                        "reason": "post_close_contract_receipt_unreadable",
                        "artifact": str(report.relative_to(root))}
            report_relative = str(report.relative_to(root))
            if (not isinstance(receipt, dict) or receipt.get("ok") is not True
                    or receipt.get("schema") != "post_close_report_contract/1.1"
                    or receipt.get("target_session_date") != target
                    or receipt.get("report_path") != report_relative
                    or receipt.get("report_sha256") != _sha256_file(report)
                    or ((receipt.get("post_close_status_at_report_cutoff") or {})
                        .get("run_mode")) != "supervised"
                    or ((receipt.get("post_close_status_at_report_cutoff") or {})
                        .get("run_id")) != run_id
                    or (receipt.get("safety") or {}).get("activation_authority") is not False
                    or (receipt.get("safety") or {}).get("execution_enabled") is not False):
                return {**base, "status": "fail",
                        "reason": "post_close_contract_receipt_invalid",
                        "artifact": report_relative}
            cutoff_errors = post_close_finality.validate_cutoff(
                target=target, supervised_run_id=run_id, root=root)
            if cutoff_errors:
                return {**base, "status": "fail",
                        "reason": "post_close_cutoff_receipt_invalid",
                        "artifact": report_relative}
            return {**base, "status": accepted_status,
                    "reason": "post_close_report_contract_validated",
                    "artifact": report_relative,
                    "contract_receipt": str(receipt_path.relative_to(root)),
                    "contract_receipt_sha256": _sha256_file(receipt_path)}
        return {**base, "status": "fail", "reason": "post_close_artifact_missing"}
    return {**base, "status": "fail", "reason": "unknown_artifact_contract"}


def _child_env(dry_run: bool, *, control_only: bool = False) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in BASE_ENV_KEYS:
            env[key] = value
            continue
        if not control_only and key.startswith("TRADING_") and not any(
                marker in key.upper() for marker in SECRET_ENV_MARKERS):
            env[key] = value
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    if not control_only:
        env["TRADING_AUTONOMY_CHILD"] = "1"
        env["TRADING_AUTONOMY_ENABLE"] = "1"
        if dry_run:
            env["DRY_RUN"] = "1"
    return env


def _terminate_group(proc: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    with contextlib.suppress(Exception):
        proc.wait(timeout=grace_seconds)


def _direct_critical_integrity(root: Path) -> tuple[bool, str]:
    """Verify stored critical hashes without executing another repository script."""
    try:
        manifest = json.loads(
            (root / "data" / "MANIFEST.json").read_text(encoding="utf-8"))
        critical = manifest.get("critical_sha256")
        if (manifest.get("critical_missing") != []
                or not isinstance(critical, dict)
                or not 1 <= len(critical) <= 1000
                or "scripts/autonomy_service_health.py" not in critical):
            return False, "critical_manifest_invalid"
        for relative, expected in critical.items():
            if (not isinstance(relative, str)
                    or not isinstance(expected, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
                return False, "critical_manifest_entry_invalid"
            parts = Path(relative).parts
            if not parts or Path(relative).is_absolute() or ".." in parts:
                return False, "critical_manifest_path_invalid"
            path = root / relative
            if path.is_symlink() or _sha256_file(path) != expected:
                return False, "critical_manifest_hash_mismatch"
        return True, "critical_manifest_verified"
    except (OSError, TypeError, json.JSONDecodeError):
        return False, "critical_manifest_unavailable"


def _refresh_service_health(*, root: Path, target: str, state_path: Path,
                            now: datetime) -> dict:
    """Refresh local diagnostic health without changing the workflow result."""
    base = {
        "status": "unavailable",
        "returncode": None,
        "overall_status": None,
        "artifact": None,
        "artifact_sha256": None,
        "fresh": False,
        "read_only": True,
        "diagnostic_only": True,
        "runner_outcome_unchanged": True,
        "execution_enabled": False,
        "broker_invoked": False,
        "contains_account_data": False,
        "contains_credentials": False,
    }
    script = root / "scripts" / "autonomy_service_health.py"
    if not script.is_file():
        return {**base, "reason": "service_health_script_unavailable"}
    integrity_ok, integrity_reason = _direct_critical_integrity(root)
    if not integrity_ok:
        return {
            **base,
            "reason": f"service_health_suppressed_{integrity_reason}",
        }
    destination = (
        root / "data" / "runtime" / "health" /
        f"{target}_autonomy_service_health.json")
    expected_generated = tzutil.to_utc(now)
    command = [
        sys.executable, str(script),
        "--target", target,
        "--root", str(root),
        "--autonomy-state", str(state_path),
        "--now", expected_generated.isoformat(),
        "--out", str(destination),
    ]
    try:
        completed = subprocess.run(
            command, cwd=str(root), env=_child_env(False, control_only=True),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return {**base, "status": "failed", "returncode": 124,
                "reason": "service_health_refresh_timeout"}
    except OSError:
        return {**base, "status": "failed", "returncode": 127,
                "reason": "service_health_refresh_invocation_failed"}
    if completed.returncode not in {0, 1}:
        return {**base, "status": "failed",
                "returncode": completed.returncode,
                "reason": "service_health_refresh_structural_failure"}
    try:
        document = json.loads(destination.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(document.get("generated_utc")))
        fresh = (
            generated.tzinfo is not None
            and tzutil.to_utc(generated) == expected_generated
            and document.get("schema") == "autonomy_service_health/1.0"
            and document.get("target_session_date") == target
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(document.get("artifact_sha256") or "")) is not None)
        if not fresh:
            raise ValueError("service_health_artifact_not_fresh")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**base, "status": "failed",
                "returncode": completed.returncode,
                "reason": "service_health_artifact_invalid_or_stale"}
    return {
        **base,
        "status": "written",
        "returncode": completed.returncode,
        "overall_status": _safe_token(
            document.get("overall_status"), "unavailable"),
        "artifact": str(destination.relative_to(root)),
        "artifact_sha256": document["artifact_sha256"],
        "fresh": True,
        "reason": "service_health_refreshed",
    }


def _finalize_before_child(path: Path, target: str, workflow: str, run_id: str,
                           config_sha: str, state: str, outcome: str, exit_code: int,
                           preflight: dict, now: datetime,
                           root: Path) -> dict:
    _update_attempt(path, target, workflow, run_id, config_sha, {
        "state": state,
        "outcome": outcome,
        "finished_utc": tzutil.to_utc(now).isoformat(),
        "exit_code": exit_code,
        "preflight": preflight,
    }, event="attempt_finished", reason=outcome, now=now)
    with contextlib.suppress(Exception):
        # Canonical pre-child terminal state (2026-07-28): outcomes decided before any child
        # runner exists (dependency skips, manifest/authority/ownership blocks) must still
        # reach data/system_state.json under canonical workflow names.
        system_state_publish.publish(root, target, state_path=path, now=now)
    health = _refresh_service_health(
        root=root, target=target, state_path=path, now=now)
    if workflow == "morning":
        # Post-attempt engineering observation is isolated from the market outcome.  A failure
        # here must never alter the already-durable Morning terminal or its exit code; the
        # watchdog invokes the same idempotency key after the deadline as a fallback.
        with contextlib.suppress(Exception):
            morning_audit_supervisor.supervise(
                root=root, target=target, run_id=run_id, trigger="orchestrator")
    return health


def _macro_recovery_gate(trigger: str, target: str,
                         now: datetime) -> tuple[str, str]:
    """Authorize only the two fixed, pre-Morning missing-run recovery windows.

    This is intentionally separate from the ordinary 05:30-05:49:59 macro phase gate. Recovery
    cannot make a late SLO look timely and cannot become a general rerun or off-hours surface.
    """
    window = MACRO_RECOVERY_WINDOWS_PT.get(trigger)
    if window is None:
        return "fail", "macro_recovery_trigger_invalid"
    pt = tzutil.to_pt(now)
    if pt.date().isoformat() != target:
        return "fail", "target_not_current_pt_date"
    earliest, latest = window
    current = pt.timetz().replace(tzinfo=None)
    if current < earliest:
        return "skip", f"before_{trigger}_macro_recovery_window"
    if current > latest:
        return "skip", f"after_{trigger}_macro_recovery_window"
    return "pass", f"{trigger}_missing_macro_recovery_window"


def recover_missing_macro_refresh(*, trigger: str, target: str | None = None,
                                  config_path: Path = CONFIG_DEFAULT,
                                  state_root: Path = STATE_ROOT_DEFAULT,
                                  root: Path = ROOT, dry_run: bool = False,
                                  now: datetime | None = None) -> tuple[dict, int]:
    """Run one macro refresh only when no supervised attempt exists.

    The atomic zero-attempt condition is enforced again by ``_start_attempt``. An ordinary delayed
    launch and a continuity launch can therefore race safely: at most one child is admitted.
    """
    gate_now = now or tzutil.now_utc()
    target = target or tzutil.to_pt(gate_now).date().isoformat()
    if not DATE_RE.fullmatch(target):
        return {"ok": False, "error": "target_invalid", "target": target,
                "runner_invoked": False, "execution_enabled": False}, 2
    gate_status, gate_reason = _macro_recovery_gate(trigger, target, gate_now)
    if gate_status == "skip":
        return {
            "ok": True,
            "workflow": "macro_refresh",
            "target": target,
            "state": "unchanged",
            "outcome": gate_reason,
            "recovery_trigger": _safe_token(trigger),
            "runner_invoked": False,
            "research_only": True,
            "execution_enabled": False,
            "activation_authority": False,
        }, 0
    if gate_status != "pass":
        return {
            "ok": False,
            "workflow": "macro_refresh",
            "target": target,
            "error": gate_reason,
            "recovery_trigger": _safe_token(trigger),
            "runner_invoked": False,
            "research_only": True,
            "execution_enabled": False,
        }, 78
    # Preserve deterministic virtual-clock tests without creating a production time-injection
    # surface. Production callers omit ``now`` and get a second real-clock check at dispatch.
    run_now = gate_now if now is not None else None
    return run_workflow(
        "macro_refresh", target=target, config_path=config_path,
        state_root=state_root, root=root, dry_run=dry_run, now=run_now,
        recovery_trigger=trigger)


def run_workflow(workflow: str, *, target: str | None = None,
                 config_path: Path = CONFIG_DEFAULT, state_root: Path = STATE_ROOT_DEFAULT,
                 root: Path = ROOT, dry_run: bool = False,
                 now: datetime | None = None,
                 recovery_trigger: str | None = None) -> tuple[dict, int]:
    clock_injected = now is not None
    now = now or tzutil.now_utc()
    target = target or tzutil.to_pt(now).date().isoformat()
    if workflow not in WORKFLOWS:
        return {"ok": False, "error": "workflow_not_allowlisted"}, 2
    if recovery_trigger is not None and (
            workflow != "macro_refresh" or recovery_trigger not in MACRO_RECOVERY_WINDOWS_PT):
        return {"ok": False, "error": "recovery_contract_invalid",
                "workflow": workflow, "runner_invoked": False,
                "execution_enabled": False}, 2
    if not DATE_RE.fullmatch(target):
        return {"ok": False, "error": "target_invalid"}, 2
    config, errors = validate_config(config_path, root)
    config_sha = _sha256_file(config_path) or "config_unreadable"
    if errors or config is None:
        return {"ok": False, "error": "config_invalid", "errors": errors,
                "research_only": True, "execution_enabled": False}, 78
    wf_cfg = config["workflows"][workflow]
    state_path = state_root / f"{target}.json"
    run_id = provenance.new_run_id(str(wf_cfg["run_id_prefix"]))
    try:
        _state, started_attempt = _start_attempt(
            state_path, target, workflow, run_id, config_sha, dry_run, now,
            only_if_never_started=recovery_trigger is not None,
            allow_watchdog_no_dispatch_handoff=recovery_trigger == "morning")
    except ControlError as exc:
        reason = str(exc)
        if reason == "workflow_already_attempted" and recovery_trigger is not None:
            prior = _macro_prior_summary(state_path, target, config_sha)
            macro_input = _macro_receipt_observation(target, root)
            return {
                "ok": True,
                "workflow": workflow,
                "target": target,
                "state": "unchanged",
                "outcome": "continuity_not_needed_prior_attempt",
                "recovery_trigger": recovery_trigger,
                "runner_invoked": False,
                **prior,
                "input_status": macro_input["status"],
                "input_receipt_status": macro_input["receipt_status"],
                "input_available": macro_input["input_available"],
                "input_artifact_file_sha256": macro_input["artifact_file_sha256"],
                "research_only": True,
                "execution_enabled": False,
                "activation_authority": False,
            }, 0
        if reason == "workflow_already_terminal":
            return {
                "ok": True,
                "workflow": workflow,
                "target": target,
                "state": "unchanged",
                "outcome": "duplicate_terminal_preserved",
                "recovery_trigger": recovery_trigger,
                "runner_invoked": False,
                "research_only": True,
                "execution_enabled": False,
                "activation_authority": False,
            }, 0
        code = 75 if reason == "workflow_already_running" else 2
        return {"ok": False, "error": str(exc), "workflow": workflow,
                "target": target, "state": "unchanged",
                "outcome": ("prior_macro_state_ambiguous"
                            if reason == "workflow_prior_state_ambiguous" else reason),
                "runner_invoked": False,
                "execution_enabled": False}, code

    preflight: dict[str, Any] = {}
    if recovery_trigger is not None:
        preflight.update({
            "continuity_recovery": True,
            "recovery_trigger": recovery_trigger,
            "recovery_only_if_never_started": True,
            "recovery_child_timeout_seconds": MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS,
        })
        started_preflight = started_attempt.get("preflight")
        if (isinstance(started_preflight, dict)
                and started_preflight.get("watchdog_no_dispatch_handoff") is True):
            preflight.update({
                "watchdog_no_dispatch_handoff": True,
                "handoff_from_run_id": _safe_token(
                    started_preflight.get("handoff_from_run_id"), "invalid_token"),
            })
    td_status, market_day = _trading_day_gate(target, root)
    preflight["trading_day"] = td_status
    preflight["market_day_reason"] = _safe_token(market_day.get("reason"), "unavailable")
    if td_status == "skip":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "skipped", "non_trading_day", 0, preflight, now, root)
        return {"ok": True, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": "non_trading_day",
                "runner_invoked": False, "execution_enabled": False,
                "service_health_refresh": health_refresh}, 0
    if td_status != "pass":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "blocked", "trading_day_gate_failed", 78, preflight, now, root)
        return {"ok": False, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": "trading_day_gate_failed",
                "runner_invoked": False, "execution_enabled": False,
                "service_health_refresh": health_refresh}, 78

    integrity_status, integrity = _integrity_manifest_gate(root)
    preflight["integrity_manifest"] = integrity_status
    preflight["manifest_validator_returncode"] = integrity.get("returncode")
    preflight["manifest_check"] = integrity.get("manifest_check")
    if integrity_status != "pass":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "blocked", "integrity_manifest_gate_failed", 78,
            preflight, now, root)
        return {"ok": False, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": "integrity_manifest_gate_failed",
                "runner_invoked": False, "execution_enabled": False,
                "service_health_refresh": health_refresh}, 78

    broker_status, broker = _broker_permission_gate(root)
    preflight["broker_permissions"] = broker_status
    preflight["broker_validator_returncode"] = broker.get("returncode")
    preflight["strict_read_only"] = broker.get("strict_read_only") is True
    if broker_status != "pass":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "blocked", "broker_permission_gate_failed", 78, preflight, now, root)
        return {"ok": False, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": "broker_permission_gate_failed",
                "runner_invoked": False, "execution_enabled": False,
                "service_health_refresh": health_refresh}, 78

    if workflow == "morning" and not dry_run:
        # The watchdog may itself be skipped by sleep/wake delivery. Recover before the Morning
        # child acquires the repository-wide runner lock, so the existing in-run macro fallback
        # consumes a supervised receipt instead of creating artifact/state divergence.
        recovery, recovery_rc = recover_missing_macro_refresh(
            trigger="morning", target=target, config_path=config_path,
            state_root=state_root, root=root)
        macro_input = _macro_receipt_observation(target, root)
        if recovery_rc != 0:
            continuity_status = "at_issue"
        elif recovery.get("runner_invoked") is True:
            continuity_status = "completed"
        else:
            continuity_status = "no_dispatch"
        preflight["macro_continuity"] = {
            "status": continuity_status,
            "outcome": _safe_token(
                recovery.get("outcome") or recovery.get("error"), "unavailable"),
            "runner_invoked": recovery.get("runner_invoked") is True,
            "returncode": recovery_rc,
            "input_status": macro_input["status"],
            "input_receipt_status": macro_input["receipt_status"],
            "input_available": macro_input["input_available"],
            "input_artifact_file_sha256": macro_input["artifact_file_sha256"],
            "input_receipt_artifact_sha256": macro_input["receipt_artifact_sha256"],
            "input_validation_errors": macro_input["validation_errors"],
            "slo_repair_claimed": False,
            "execution_enabled": False,
        }

    dep_status, dep_reason = _dependency_gate(workflow, target, root, dry_run)
    preflight["dependency_gate"] = dep_status
    preflight["dependency_reason"] = dep_reason
    post_close_mode: str | None = None
    post_close_morning_reason = "not_required"
    if workflow == "post_close":
        if dry_run:
            post_close_mode = "dry_run"
        elif dep_status == "pass" and dep_reason == "official_morning_present":
            post_close_mode = "narrative"
            post_close_morning_reason = dep_reason
        elif dep_status == "pass" and dep_reason == \
                post_close_maintenance_receipt.ABSENT_REASON:
            post_close_mode = "maintenance"
            post_close_morning_reason = dep_reason
        preflight["post_close_mode"] = post_close_mode
        preflight["post_close_morning_reason"] = post_close_morning_reason
    if dep_status == "skip":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "skipped", dep_reason, 0, preflight, now, root)
        return {"ok": True, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": dep_reason, "runner_invoked": False,
                "execution_enabled": False,
                "service_health_refresh": health_refresh}, 0
    if dep_status != "pass":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "blocked", dep_reason, 78, preflight, now, root)
        return {"ok": False, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": dep_reason, "runner_invoked": False,
                "execution_enabled": False,
                "service_health_refresh": health_refresh}, 78

    accepted_morning: Path | None = None
    accepted_morning_kind = "none"
    if workflow in ("post_open", "post_close") and not dry_run:
        accepted_morning, accepted_morning_kind, accepted_reason = \
            _official_morning_artifact(target, root)
        authority_stable = False
        if workflow == "post_open":
            authority_stable = accepted_morning is not None
        elif post_close_mode == "narrative":
            authority_stable = (
                accepted_morning is not None
                and accepted_reason == "official_morning_present")
        elif post_close_mode == "maintenance":
            authority_stable = (
                accepted_morning is None
                and accepted_morning_kind == "none"
                and accepted_reason == post_close_maintenance_receipt.ABSENT_REASON)
        if not authority_stable:
            reason = (accepted_reason if accepted_morning is None
                      else "post_close_morning_authority_changed")
            health_refresh = _finalize_before_child(
                state_path, target, workflow, run_id, config_sha,
                "blocked", reason, 78, preflight, now, root)
            return {"ok": False, "workflow": workflow, "target": target,
                    "run_id": run_id, "outcome": reason,
                    "runner_invoked": False, "execution_enabled": False,
                    "service_health_refresh": health_refresh}, 78
        if accepted_morning is not None:
            preflight["accepted_morning_artifact"] = str(accepted_morning.relative_to(root))
            preflight["accepted_morning_kind"] = accepted_morning_kind

    # Explicit ``now`` is reachable from the CLI only with --dry-run. Internal tests may inject it
    # while exercising production branches, so preserve that deterministic first gate; every real
    # production invocation omits it and therefore samples the current clock here.
    phase_now = now if clock_injected else tzutil.now_utc()
    if recovery_trigger is not None:
        phase_status, phase_reason = _macro_recovery_gate(
            recovery_trigger, target, phase_now)
    else:
        phase_status, phase_reason = _phase_gate(
            workflow, target, phase_now, market_day, dry_run)
    preflight["phase_gate"] = phase_status
    preflight["phase_reason"] = phase_reason
    if phase_status == "skip":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "skipped", phase_reason, 0, preflight, phase_now, root)
        return {"ok": True, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": phase_reason, "runner_invoked": False,
                "execution_enabled": False,
                "service_health_refresh": health_refresh}, 0
    if phase_status != "pass":
        health_refresh = _finalize_before_child(
            state_path, target, workflow, run_id, config_sha,
            "blocked", phase_reason, 78, preflight, phase_now, root)
        return {"ok": False, "workflow": workflow, "target": target,
                "run_id": run_id, "outcome": phase_reason, "runner_invoked": False,
                "execution_enabled": False,
                "service_health_refresh": health_refresh}, 78

    if recovery_trigger is not None:
        dispatch_now = now if dry_run else tzutil.now_utc()
        dispatch_status, dispatch_reason = _macro_recovery_gate(
            recovery_trigger, target, dispatch_now)
        preflight["recovery_dispatch_gate"] = dispatch_status
        preflight["recovery_dispatch_reason"] = dispatch_reason
        if dispatch_status != "pass":
            state = "skipped" if dispatch_status == "skip" else "blocked"
            code = 0 if dispatch_status == "skip" else 78
            health_refresh = _finalize_before_child(
                state_path, target, workflow, run_id, config_sha,
                state, dispatch_reason, code, preflight, dispatch_now, root)
            return {
                "ok": dispatch_status == "skip",
                "workflow": workflow,
                "target": target,
                "run_id": run_id,
                "outcome": dispatch_reason,
                "recovery_trigger": recovery_trigger,
                "runner_invoked": False,
                "research_only": True,
                "execution_enabled": False,
                "activation_authority": False,
                "service_health_refresh": health_refresh,
            }, code

    if workflow == "macro_refresh" and recovery_trigger is None and not dry_run:
        # The ordinary 05:35 lane gets the same just-in-time truth check as continuity. Expensive
        # integrity checks must not authorize a child from a stale invocation-time timestamp.
        dispatch_now = tzutil.now_utc()
        dispatch_status, dispatch_reason = _phase_gate(
            workflow, target, dispatch_now, market_day, False)
        preflight["ordinary_dispatch_gate"] = dispatch_status
        preflight["ordinary_dispatch_reason"] = dispatch_reason
        if dispatch_status != "pass":
            state = "skipped" if dispatch_status == "skip" else "blocked"
            code = 0 if dispatch_status == "skip" else 78
            health_refresh = _finalize_before_child(
                state_path, target, workflow, run_id, config_sha,
                state, dispatch_reason, code, preflight, dispatch_now, root)
            return {
                "ok": dispatch_status == "skip",
                "workflow": workflow,
                "target": target,
                "run_id": run_id,
                "outcome": dispatch_reason,
                "runner_invoked": False,
                "research_only": True,
                "execution_enabled": False,
                "activation_authority": False,
                "service_health_refresh": health_refresh,
            }, code

    if workflow == "macro_refresh" and not dry_run:
        # A target receipt may have been produced by an allowed direct/manual runner, or by one
        # that won a race before this child could acquire the shared runner lock. Never overwrite
        # or refetch around that evidence. Invalid bytes are equally non-retryable and fail closed.
        observed = _macro_receipt_observation(target, root)
        preflight["existing_macro_receipt"] = observed
        if observed["status"] != "absent":
            receipt_status = observed["receipt_status"]
            if observed["status"] == "valid" and receipt_status in {
                    "ready", "no_due_events"}:
                state, outcome, code = "completed", "existing_macro_receipt_preserved", 0
            elif observed["status"] == "valid" and receipt_status == "degraded":
                state, outcome, code = (
                    "degraded", "existing_degraded_macro_receipt_preserved", 0)
            elif observed["status"] == "valid":
                state, outcome, code = (
                    "blocked", "existing_macro_receipt_not_usable", 78)
            else:
                state, outcome, code = (
                    "blocked", "preexisting_macro_receipt_invalid", 78)
            observed_at = tzutil.now_utc()
            health_refresh = _finalize_before_child(
                state_path, target, workflow, run_id, config_sha,
                state, outcome, code, preflight, observed_at, root)
            return {
                "ok": code == 0,
                "workflow": workflow,
                "target": target,
                "run_id": run_id,
                "state": state,
                "outcome": outcome,
                "recovery_trigger": recovery_trigger,
                "runner_invoked": False,
                "existing_receipt_preserved": True,
                "existing_receipt": observed,
                "research_only": True,
                "execution_enabled": False,
                "activation_authority": False,
                "service_health_refresh": health_refresh,
            }, code

    runner = (root / str(wf_cfg["runner"])).resolve()
    started_epoch = datetime.now(timezone.utc).timestamp()
    command = ["/bin/bash", str(runner)]
    env = _child_env(dry_run)
    env["TRADING_AUTONOMY_RUN_ID"] = run_id
    env["TRADING_AUTONOMY_WORKFLOW"] = workflow
    env["TRADING_TARGET_SESSION"] = target
    if workflow == "post_close":
        env["TRADING_POST_CLOSE_MODE"] = str(post_close_mode)
        env["TRADING_POST_CLOSE_MORNING_REASON"] = post_close_morning_reason
    if recovery_trigger is not None:
        env["TRADING_AUTONOMY_RECOVERY_TRIGGER"] = recovery_trigger
    if accepted_morning is not None:
        env["TRADING_ACCEPTED_MORNING_REPORT"] = str(accepted_morning)
        env["TRADING_ACCEPTED_MORNING_KIND"] = accepted_morning_kind
    proc: subprocess.Popen | None = None
    timed_out = False
    try:
        proc = subprocess.Popen(command, cwd=str(root), env=env, start_new_session=True)
        runner_started_at = now if dry_run else tzutil.now_utc()
        _update_attempt(state_path, target, workflow, run_id, config_sha,
                        {"child_pid": proc.pid, "preflight": preflight},
                        event="runner_started", reason="allowlisted_runner",
                        now=runner_started_at)
        try:
            child_timeout = (MACRO_RECOVERY_CHILD_TIMEOUT_SECONDS
                             if recovery_trigger is not None
                             else int(wf_cfg["timeout_seconds"]))
            rc = proc.wait(timeout=child_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(proc)
            rc = 124
    except Exception:
        if proc is not None and proc.poll() is None:
            _terminate_group(proc)
        rc = 127

    finished = tzutil.now_utc()
    post = _postconditions(
        workflow, target, root, started_epoch, dry_run,
        str(wf_cfg["task_state_key"]), run_id=run_id) if rc == 0 else None
    if timed_out:
        final_state, outcome, final_rc = "failed", "supervisor_timeout", 124
    elif rc == 75:
        final_state, outcome, final_rc = "blocked", "runner_lock_contention", 75
    elif workflow == "post_close" and rc == 78:
        final_state, outcome, final_rc = "blocked", "runner_authority_gate_blocked", 78
    elif rc != 0:
        final_state, outcome, final_rc = "failed", f"runner_exit_{rc}", 1
    elif post and post.get("status") == "fail":
        final_state, outcome, final_rc = "failed", str(post.get("reason")), 1
    elif post and post.get("status") == "skip":
        final_state, outcome, final_rc = "skipped", str(post.get("reason")), 0
    elif post and post.get("status") == "warn":
        final_state, outcome, final_rc = "degraded", str(post.get("reason")), 0
    else:
        final_state, outcome, final_rc = "completed", "contract_satisfied", 0
    _update_attempt(state_path, target, workflow, run_id, config_sha, {
        "state": final_state,
        "outcome": _safe_token(outcome),
        "finished_utc": tzutil.to_utc(finished).isoformat(),
        "exit_code": rc,
        "preflight": preflight,
        "postconditions": post,
    }, event="attempt_finished", reason=_safe_token(outcome), now=finished)
    post_close_finality_result: dict | None = None
    if (workflow == "post_close" and not dry_run and final_rc == 0
            and final_state in {"completed", "degraded"}
            and post_close_mode == "narrative"):
        # The immutable terminal receipt can be written only after the exact terminal attempt is
        # durable. If sealing fails, the runner contract may have succeeded but the supervised
        # evidence chain did not; record that as a terminal workflow failure and never report a
        # clean completed state without the second-stage receipt.
        try:
            post_close_finality_result = post_close_finality.seal_terminal(
                target=target, supervised_run_id=run_id, root=root)
        except (post_close_finality.FinalityError, OSError) as exc:
            failure_time = tzutil.now_utc()
            post_close_finality_result = {
                "ok": False,
                "error": _safe_token(str(exc), "terminal_receipt_seal_failed"),
                "execution_enabled": False,
                "activation_authority": False,
            }
            final_state = "failed"
            outcome = "post_close_terminal_receipt_failed"
            final_rc = 1
            with contextlib.suppress(Exception):
                _update_attempt(state_path, target, workflow, run_id, config_sha, {
                    "state": final_state,
                    "outcome": outcome,
                }, event="terminal_receipt_failed", reason=outcome, now=failure_time)
    health_now = tzutil.now_utc()
    with contextlib.suppress(Exception):
        system_state_publish.publish(root, target, state_path=state_path, now=health_now)
    health_refresh = _refresh_service_health(
        root=root, target=target, state_path=state_path, now=health_now)
    morning_audit_result: dict | None = None
    if workflow == "morning" and not dry_run:
        try:
            morning_audit_result = morning_audit_supervisor.supervise(
                root=root, target=target, run_id=run_id, trigger="orchestrator")
        except Exception as exc:
            # Diagnostic-only: preserve the exact runner/finality result and let the existing
            # watchdog make one bounded, deduplicated fallback attempt.
            morning_audit_result = {
                "status": "failed", "reason": _safe_token(type(exc).__name__),
                "market_outcome_unchanged": True, "execution_enabled": False,
            }
    return {
        "ok": final_rc == 0,
        "workflow": workflow,
        "target": target,
        "run_id": run_id,
        "state": final_state,
        "outcome": _safe_token(outcome),
        "runner_invoked": True,
        "runner_exit_code": rc,
        "postconditions": post,
        "post_close_finality": post_close_finality_result,
        "state_path": str(state_path),
        "recovery_trigger": recovery_trigger,
        "research_only": True,
        "execution_enabled": False,
        "activation_authority": False,
        "service_health_refresh": health_refresh,
        "morning_engineering_audit": morning_audit_result,
    }, final_rc


def _check_launchd_contract(config: dict, root: Path) -> list[str]:
    errors: list[str] = []
    for workflow in WORKFLOWS:
        wf = config["workflows"][workflow]
        plist = root / "launchd" / f"{wf['launchd_label']}.plist"
        try:
            with plist.open("rb") as fh:
                doc = plistlib.load(fh)
            args = doc.get("ProgramArguments") or []
            cal = doc.get("StartCalendarInterval") or {}
            scheduled = f"{int(cal.get('Hour')):02d}:{int(cal.get('Minute')):02d}"
            if doc.get("Label") != wf["launchd_label"]:
                errors.append(f"launchd_label_mismatch:{workflow}")
            if args != ["/bin/bash", str(root / wf["runner"])]:
                errors.append(f"launchd_runner_mismatch:{workflow}")
            if scheduled != wf["scheduled_pt"]:
                errors.append(f"launchd_schedule_mismatch:{workflow}")
            if doc.get("WorkingDirectory") != str(root):
                errors.append(f"launchd_working_directory_mismatch:{workflow}")
            if doc.get("RunAtLoad") is not False:
                errors.append(f"launchd_run_at_load_invalid:{workflow}")
            if doc.get("ProcessType") != EXPECTED_PROCESS_TYPE.get(
                    workflow, DEFAULT_PROCESS_TYPE):
                errors.append(f"launchd_process_type_invalid:{workflow}")
            env = doc.get("EnvironmentVariables") or {}
            if set(env) != {"PATH", "TRADING_AUTONOMY_ENABLE"}:
                errors.append(f"launchd_environment_keys_invalid:{workflow}")
            if env.get("TRADING_AUTONOMY_ENABLE") != "1":
                errors.append(f"launchd_autonomy_opt_in_missing:{workflow}")
        except Exception:
            errors.append(f"launchd_plist_unreadable:{workflow}")
    return errors


def check_framework(config_path: Path, root: Path) -> tuple[dict, int]:
    config, errors = validate_config(config_path, root)
    if config is not None and not errors:
        errors.extend(_check_launchd_contract(config, root))
    integrity_status, integrity = _integrity_manifest_gate(root)
    if integrity_status != "pass":
        errors.append("integrity_manifest_validator_failed")
    broker_status, broker = _broker_permission_gate(root)
    if broker_status != "pass":
        errors.append("broker_permission_validator_failed")
    out = {
        "schema": "autonomy_framework_check/1.0",
        "ok": not errors,
        "errors": sorted(set(errors)),
        "config_sha256": _sha256_file(config_path),
        "workflows": list(WORKFLOWS),
        "integrity_manifest_gate": integrity_status,
        "manifest_validator_returncode": integrity.get("returncode"),
        "broker_permission_gate": broker_status,
        "broker_validator_returncode": broker.get("returncode"),
        "research_only": True,
        "execution_enabled": False,
        "activation_authority": False,
        "supervisor_retry_allowed": False,
        "dynamic_commands_allowed": False,
    }
    return out, 0 if out["ok"] else 1


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=tzutil.PT) if parsed.tzinfo is None else parsed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail-closed autonomous research workflow supervisor.")
    ap.add_argument("--config", default=str(CONFIG_DEFAULT))
    ap.add_argument("--state-root", default=str(STATE_ROOT_DEFAULT))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    status = sub.add_parser("status")
    status.add_argument("--target")
    recover = sub.add_parser(
        "recover-macro",
        help="bounded zero-attempt macro continuity recovery; never retries prior work")
    recover.add_argument("--trigger", required=True,
                         choices=tuple(MACRO_RECOVERY_WINDOWS_PT))
    recover.add_argument("--target")
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument(
        "--now", help="ISO8601 injection; allowed only with --dry-run.")
    run = sub.add_parser("run")
    run.add_argument("--workflow", required=True, choices=WORKFLOWS)
    run.add_argument("--target")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--now", help="ISO8601 injection; allowed only with --dry-run.")
    args = ap.parse_args(argv)
    config_path = Path(args.config)
    state_root = Path(args.state_root)
    if args.command == "check":
        out, rc = check_framework(config_path, ROOT)
        print(json.dumps(out, indent=2, sort_keys=True))
        return rc
    target = getattr(args, "target", None) or tzutil.pt_now().date().isoformat()
    if args.command == "status":
        if not DATE_RE.fullmatch(target):
            print(json.dumps({"ok": False, "error": "target_invalid", "target": target,
                              "execution_enabled": False}, indent=2, sort_keys=True))
            return 2
        state_path = state_root / f"{target}.json"
        try:
            config_sha = _sha256_file(config_path) or "config_unreadable"
            doc = _load_state(state_path, target, config_sha)
            print(json.dumps(doc, indent=2, sort_keys=True))
            return 0
        except ControlError as exc:
            print(json.dumps({"ok": False, "error": str(exc), "target": target,
                              "execution_enabled": False}, indent=2, sort_keys=True))
            return 2
    if args.command == "recover-macro":
        if args.now and not args.dry_run:
            print(json.dumps({"ok": False, "error": "now_injection_requires_dry_run",
                              "execution_enabled": False}, indent=2, sort_keys=True))
            return 2
        try:
            now = _parse_now(args.now)
        except ValueError:
            print(json.dumps({"ok": False, "error": "now_invalid",
                              "execution_enabled": False}, indent=2, sort_keys=True))
            return 2
        out, rc = recover_missing_macro_refresh(
            trigger=args.trigger, target=args.target, config_path=config_path,
            state_root=state_root, root=ROOT, dry_run=args.dry_run, now=now)
        print(json.dumps(out, indent=2, sort_keys=True))
        return rc
    if args.now and not args.dry_run:
        print(json.dumps({"ok": False, "error": "now_injection_requires_dry_run",
                          "execution_enabled": False}, indent=2, sort_keys=True))
        return 2
    try:
        now = _parse_now(args.now)
    except ValueError:
        print(json.dumps({"ok": False, "error": "now_invalid",
                          "execution_enabled": False}, indent=2, sort_keys=True))
        return 2
    out, rc = run_workflow(args.workflow, target=args.target, config_path=config_path,
                           state_root=state_root, root=ROOT, dry_run=args.dry_run, now=now)
    print(json.dumps(out, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
